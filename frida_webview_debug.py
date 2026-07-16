#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frida WebView 调试注入器（Python 集成版）

在自动化脚本中调用，自动注入 Frida hook 开启大麦 APP 的 WebView 调试模式，
无需手动在终端运行 frida 命令。

使用方式 1 — 独立运行（先注入，再运行自动化脚本）：
  python frida_webview_debug.py

使用方式 2 — 在自动化脚本中集成：
  from frida_webview_debug import FridaWebViewDebugHook
  hook = FridaWebViewDebugHook()
  hook.attach("cn.damai")   # attach 到已运行的 APP
  # 或
  hook.spawn("cn.damai")    # spawn 模式（APP 启动前注入）

使用方式 3 — 作为上下文管理器：
  with FridaWebViewDebugHook.spawn("cn.damai") as hook:
      # 在此期间 WebView 调试已开启
      run_automation()
  # 退出时自动 detach

依赖：
  pip install frida frida-tools
  手机需运行 frida-server（版本与本地 frida 一致）
"""

from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional

try:
    import frida
except ImportError:
    sys.stderr.write(
        "缺少依赖 frida，请先执行：pip install frida frida-tools\n"
    )
    sys.exit(1)


# ─── Frida JS 脚本路径 ──────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
FRIDA_JS_PATH = SCRIPT_DIR / "frida_webview_debug.js"


# ─── 核心 Hook 类 ────────────────────────────────────────────────────────────

class FridaWebViewDebugHook:
    """Frida WebView 调试注入器。"""

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: 是否输出详细日志
        """
        self.verbose = verbose
        self._device: Optional[frida.core.Device] = None
        self._session: Optional[frida.core.Session] = None
        self._script: Optional[frida.core.Script] = None
        self._pid: Optional[int] = None
        self._messages: list = []
        self._attached = False

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [frida] {msg}")

    @staticmethod
    def _find_device() -> frida.core.Device:
        """查找 USB 连接的 Android 设备。"""
        try:
            device = frida.get_usb_device(timeout=5)
            return device
        except Exception:
            pass

        # 备用：尝试远程设备
        try:
            device = frida.get_remote_device()
            return device
        except Exception:
            pass

        # 列出所有设备
        devices = frida.enumerate_devices()
        usb_devices = [d for d in devices if d.type == "usb"]
        if usb_devices:
            return usb_devices[0]

        raise RuntimeError(
            "未找到 USB 设备。请确认：\n"
            "  1. 手机已通过 USB 连接电脑\n"
            "  2. 手机已开启 USB 调试\n"
            "  3. 手机上已运行 frida-server\n"
            "  4. frida-server 版本与本地 frida 一致"
        )

    def _load_js_script(self) -> str:
        """加载 Frida JS 脚本。"""
        if FRIDA_JS_PATH.is_file():
            return FRIDA_JS_PATH.read_text(encoding="utf-8")
        else:
            # 内嵌精简版脚本
            return self._get_inline_script()

    @staticmethod
    def _get_inline_script() -> str:
        """内嵌的精简版 Frida JS 脚本（当外部 JS 文件不存在时使用）。"""
        return r"""
"use strict";

function enableWebViewDebugging() {
    var enabled = false;
    var classes = [
        "android.webkit.WebView",
        "com.tencent.smtt.sdk.WebView",
        "com.uc.webview.SdkWebView",
    ];
    for (var i = 0; i < classes.length; i++) {
        try {
            var Cls = Java.use(classes[i]);
            Cls.setWebContentsDebuggingEnabled(true);
            console.log("[+] " + classes[i] + " 调试已开启");
            enabled = true;
        } catch (e) {}
    }
    return enabled;
}

Java.perform(function () {
    // 立即开启
    enableWebViewDebugging();

    // Hook setWebContentsDebuggingEnabled 防止关闭
    try {
        var WebView = Java.use("android.webkit.WebView");
        WebView.setWebContentsDebuggingEnabled.implementation = function (v) {
            this.setWebContentsDebuggingEnabled(true);
        };
    } catch (e) {}

    // Hook 构造函数
    try {
        var WebView2 = Java.use("android.webkit.WebView");
        WebView2.$init.overloads.forEach(function (overload) {
            overload.implementation = function () {
                Java.use("android.webkit.WebView").setWebContentsDebuggingEnabled(true);
                return this.$init.apply(this, arguments);
            };
        });
    } catch (e) {}

    console.log("[+] Frida WebView Debug Hook 注入完成");
});
"""

    def _on_message(self, message: dict, data: Optional[bytes]) -> None:
        """处理 Frida 脚本消息。"""
        self._messages.append(message)

        if message["type"] == "send":
            payload = message.get("payload", "")
            print(f"  [frida→py] {payload}")
        elif message["type"] == "error":
            stack = message.get("stack", "")
            description = message.get("description", "")
            print(f"  [frida error] {description}\n{stack}", file=sys.stderr)
        else:
            self._log(f"消息：{message}")

    def attach(self, package_name: str = "cn.damai") -> bool:
        """Attach 到已运行的 APP 进程。

        Args:
            package_name: APP 包名

        Returns:
            是否成功注入
        """
        print(f"🔌 正在 attach 到 {package_name}…")

        try:
            self._device = self._find_device()
            print(f"✅ 已连接设备：{self._device.name} (id={self._device.id})")
        except RuntimeError as e:
            print(f"❌ {e}")
            return False

        # 查找目标进程
        try:
            process = self._device.get_process(package_name)
            self._pid = process.pid
            print(f"✅ 找到进程：{process.name} (pid={process.pid})")
        except frida.ProcessNotFoundError:
            print(f"❌ 未找到进程 {package_name}，请先启动 APP")
            return False

        # Attach
        try:
            self._session = self._device.attach(self._pid)
            self._log(f"已 attach 到 pid={self._pid}")
        except Exception as e:
            print(f"❌ attach 失败：{e}")
            return False

        # 注入脚本
        return self._inject_script()

    def spawn(self, package_name: str = "cn.damai") -> bool:
        """Spawn 模式：在 APP 启动前注入（更可靠）。

        Args:
            package_name: APP 包名

        Returns:
            是否成功注入
        """
        print(f"🚀 正在 spawn {package_name}…")

        try:
            self._device = self._find_device()
            print(f"✅ 已连接设备：{self._device.name} (id={self._device.id})")
        except RuntimeError as e:
            print(f"❌ {e}")
            return False

        # Spawn
        try:
            self._pid = self._device.spawn([package_name])
            print(f"✅ 已 spawn 进程 (pid={self._pid})")
        except Exception as e:
            print(f"❌ spawn 失败：{e}")
            print("请确认：")
            print("  1. frida-server 版本与本地 frida 一致")
            print("  2. 手机上有足够权限运行 frida-server（需 root）")
            return False

        # Attach
        try:
            self._session = self._device.attach(self._pid)
            self._log(f"已 attach 到 pid={self._pid}")
        except Exception as e:
            print(f"❌ attach 失败：{e}")
            return False

        # 注入脚本
        if not self._inject_script():
            return False

        # Resume APP
        try:
            self._device.resume(self._pid)
            print(f"✅ APP 已 resume，WebView 调试已开启")
        except Exception as e:
            self._log(f"resume 失败（非致命）：{e}")

        return True

    def _inject_script(self) -> bool:
        """注入 Frida JS 脚本。"""
        js_code = self._load_js_script()
        self._log(f"JS 脚本长度：{len(js_code)} 字符")

        try:
            self._script = self._session.create_script(js_code)
            self._script.on("message", self._on_message)
            self._script.load()
            self._attached = True
            print("✅ Frida hook 脚本已注入")
            return True
        except Exception as e:
            print(f"❌ 脚本注入失败：{e}")
            return False

    def detach(self) -> None:
        """断开 Frida 连接。"""
        if self._script:
            try:
                self._script.unload()
            except Exception:
                pass
            self._script = None

        if self._session:
            try:
                self._session.detach()
            except Exception:
                pass
            self._session = None

        self._attached = False
        self._log("已 detach")

    def is_attached(self) -> bool:
        """是否已注入。"""
        return self._attached

    def wait_until_detached(self) -> None:
        """阻塞等待，直到 Frida 连接断开（APP 退出或手动 detach）。"""
        if not self._session:
            return

        print("⏳ Frida hook 已激活，按 Ctrl+C 退出…")
        try:
            # 保持 session 活跃
            while self._attached:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在断开…")
            self.detach()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.detach()
        return False


# ─── 辅助函数 ────────────────────────────────────────────────────────────────

def check_frida_server() -> bool:
    """检查设备上 frida-server 是否运行。"""
    try:
        device = frida.get_usb_device(timeout=3)
        # 尝试枚举进程，如果 frida-server 没运行会超时
        processes = device.enumerate_processes()
        frida_procs = [p for p in processes if "frida" in p.name.lower()]
        if frida_procs:
            print(f"✅ frida-server 已运行：{frida_procs}")
            return True
        else:
            print("⚠️ 未检测到 frida-server 进程")
            return False
    except Exception as e:
        print(f"❌ 无法连接设备：{e}")
        return False


def inject_and_wait(package_name: str = "cn.damai", spawn_mode: bool = True) -> None:
    """注入 Frida hook 并保持运行。

    Args:
        package_name: APP 包名
        spawn_mode: True=spawn 模式（推荐），False=attach 模式
    """
    hook = FridaWebViewDebugHook(verbose=True)

    if spawn_mode:
        ok = hook.spawn(package_name)
    else:
        ok = hook.attach(package_name)

    if not ok:
        print("\n❌ 注入失败。")
        print("请确认：")
        print("  1. 手机已 root 且已运行 frida-server")
        print("  2. frida-server 版本与本地 frida 一致")
        print("     检查方法：")
        print("       pip show frida          # 查看本地版本")
        print("       adb shell /data/local/tmp/frida-server --version  # 查看设备版本")
        print("  3. 如使用 Magisk，可安装 MagiskFrida 模块自动启动 frida-server")
        sys.exit(1)

    print()
    print("═" * 50)
    print("  WebView 调试已开启！")
    print("  现在可以在另一个终端运行自动化脚本：")
    print(f"    python damai_reserve_u2.py --verbose")
    print("═" * 50)
    print()

    # 保持 hook 活跃
    hook.wait_until_detached()


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Frida WebView 调试注入器 — 强制开启大麦 APP WebView DevTools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python frida_webview_debug.py                           # spawn 模式（推荐）\n"
            "  python frida_webview_debug.py --attach                  # attach 模式\n"
            "  python frida_webview_debug.py --package cn.damai        # 指定包名\n"
            "  python frida_webview_debug.py --check                   # 仅检查 frida-server\n"
            "\n"
            "前置条件:\n"
            "  1. 手机已 root\n"
            "  2. 已安装 frida-server 且版本与本地 frida 一致\n"
            "     pip install frida frida-tools\n"
            "     adb push frida-server /data/local/tmp/\n"
            "     adb shell 'chmod 755 /data/local/tmp/frida-server'\n"
            "     adb shell '/data/local/tmp/frida-server &'\n"
        ),
    )
    parser.add_argument("--package", type=str, default="cn.damai",
                        help="APP 包名（默认 cn.damai）")
    parser.add_argument("--attach", action="store_true",
                        help="使用 attach 模式（默认 spawn 模式）")
    parser.add_argument("--check", action="store_true",
                        help="仅检查 frida-server 是否运行")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志")
    args = parser.parse_args()

    # Windows 控制台 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("╔════════════════════════════════════════════════╗")
    print("║  Frida WebView Debug Hook                       ║")
    print("║  强制开启大麦 APP WebView 调试模式              ║")
    print("╚════════════════════════════════════════════════╝")
    print()

    # 检查 frida 版本
    print(f"📦 frida 版本：{frida.__version__}")

    if args.check:
        check_frida_server()
        return

    inject_and_wait(
        package_name=args.package,
        spawn_mode=not args.attach,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
