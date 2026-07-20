#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 uiautomator2 自动化核心模块

通过 uiautomator2 驱动 Android 设备上的大麦 APP，在其内嵌 WebView (H5) 中完成：
  1. 登录（手机号 + 短信验证码）
  2. 搜索演出
  3. 提取登录态 cookies → 注入 DamaiMonitor 查询余票

依赖：
  pip install uiautomator2
  设备需开启 USB 调试并通过 adb 连接
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import uiautomator2 as u2
except ImportError:
    sys.stderr.write(
        "缺少依赖 uiautomator2，请先执行：pip install uiautomator2\n"
    )
    sys.exit(1)

from human_sim import (
    HumanBehaviorConfig,
    human_browse,
    human_click,
    human_delay,
    human_idle_swipe,
    human_navigate_pause,
    human_scroll,
    human_type_text,
    human_warmup,
    set_default_config,
)

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None  # type: ignore


# ─── 常量 ────────────────────────────────────────────────────────────────

DAMAI_PACKAGE = "cn.damai"
DAMAI_ACTIVITY = "cn.damai.homepage.ui.MainActivity"

# 大麦 H5 登录页 URL
H5_LOGIN_URL = "https://passport.damai.cn/login.htm"
# 大麦 H5 首页
H5_HOME_URL = "https://m.damai.cn/"
# 大麦 H5 搜索页
H5_SEARCH_URL = "https://search.damai.cn/search.html"

# 等待超时（秒）
DEFAULT_TIMEOUT = 15
LONG_TIMEOUT = 30


# ─── CDPWebView：通过 Chrome DevTools Protocol 操作 WebView ─────────────

class CDPWebView:
    """通过 Chrome DevTools Protocol (CDP) 连接并操作 Android WebView。

    替代 u2.Device.webdriver（该属性不存在），通过 WebSocket 直连 CDP，
    提供 execute_script / page_source / current_url 等接口。

    依赖：
      pip install requests websocket-client
    """

    def __init__(self, ws_url: str, verbose: bool = False):
        """
        Args:
            ws_url: WebSocket Debugger URL（从 /json 接口获取）
            verbose: 是否输出详细日志
        """
        self._ws_url = ws_url
        self._verbose = verbose
        self._ws = None  # websocket 连接
        self._msg_id = 0
        self._connect()

    def _connect(self) -> None:
        """建立 WebSocket 连接。"""
        if websocket is None:
            raise RuntimeError("缺少 websocket-client 库，请执行：pip install websocket-client")
        self._ws = websocket.create_connection(self._ws_url, timeout=10)
        if self._verbose:
            print(f"  [CDP] WebSocket 已连接：{self._ws_url}")

    def _send_cdp(self, method: str, params: Optional[Dict] = None, timeout: float = 10) -> Any:
        """发送 CDP 命令并等待返回。

        Args:
            method: CDP 方法名，如 "Runtime.evaluate"
            params: 方法参数
            timeout: 超时秒数

        Returns:
            CDP 响应的 result 字段
        """
        if not self._ws:
            raise RuntimeError("WebSocket 未连接")

        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params

        self._ws.send(json.dumps(msg))

        # 等待匹配的响应
        import select
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                self._ws.settimeout(min(remaining, 1))
                raw = self._ws.recv()
                resp = json.loads(raw)
                # 跳过事件通知（无 id 字段）
                if "id" in resp and resp["id"] == self._msg_id:
                    if "error" in resp:
                        raise RuntimeError(f"CDP 错误：{resp['error']}")
                    return resp.get("result")
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                continue

        raise TimeoutError(f"CDP 命令 {method} 超时（{timeout}s）")

    # ── 核心接口（与 _wd 原有用法兼容）──

    def execute_script(self, script: str) -> Any:
        """执行 JavaScript 并返回结果。

        Args:
            script: JS 代码（支持 return 语句）

        Returns:
            JS 执行结果的 Python 值
        """
        # CDP Runtime.evaluate
        result = self._send_cdp("Runtime.evaluate", {
            "expression": script,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=15)

        # 解析返回值
        rv = result.get("result", {})
        value = rv.get("value")
        # 如果是 object 类型且 returnByValue 为 True，value 直接就是 Python 对象
        return value

    @property
    def page_source(self) -> str:
        """获取页面 HTML 源码。"""
        return self.execute_script("return document.documentElement.outerHTML;") or ""

    @property
    def current_url(self) -> str:
        """获取当前页面 URL。"""
        return self.execute_script("return window.location.href;") or ""

    @property
    def title(self) -> str:
        """获取当前页面标题。"""
        return self.execute_script("return document.title;") or ""

    def get(self, url: str) -> None:
        """导航到指定 URL。"""
        self.execute_script(f"window.location.href = '{url}';")
        time.sleep(2)

    def get_cookies(self) -> List[Dict[str, Any]]:
        """获取当前页面 cookies。"""
        try:
            result = self._send_cdp("Network.getCookies", {})
            return result.get("cookies", [])
        except Exception:
            # 备用：通过 JS 获取
            cookie_str = self.execute_script("return document.cookie;") or ""
            cookies = []
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".damai.cn",
                    "path": "/",
                })
            return cookies

    def close(self) -> None:
        """关闭 WebSocket 连接。"""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __del__(self) -> None:
        self.close()


# ─── 核心自动化类 ────────────────────────────────────────────────────────

class DamaiU2Automation:
    """大麦网 uiautomator2 自动化（APP 内 WebView）。"""

    def __init__(self, device_serial: Optional[str] = None, verbose: bool = False,
                 stealth_level: str = "medium"):
        """
        Args:
            device_serial: 设备序列号，None 则自动检测
            verbose: 是否输出详细日志
            stealth_level: 拟人化等级 "low" / "medium" / "high"
        """
        self.device_serial = device_serial
        self.verbose = verbose
        self.stealth_level = stealth_level
        self._human_cfg = HumanBehaviorConfig(stealth_level)
        set_default_config(self._human_cfg)
        self.d: Optional[u2.Device] = None
        self._wd = None  # WebDriver (chrome devtools)
        self._native_context: Optional[str] = None
        self._webview_warned = False  # 是否已提示过 WebView 不可用
        self._webview_context: Optional[str] = None
        self._cookies: List[Dict[str, Any]] = []
        self._has_root: Optional[bool] = None  # 设备 root 状态缓存（None=未检测）

    # ── 日志 ──────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [u2] {msg}")

    @staticmethod
    def _warn(msg: str) -> None:
        sys.stderr.write(f"[warn] {msg}\n")

    # ── 设备连接 ──────────────────────────────────────────────────────
    def connect_device(self) -> None:
        """连接 Android 设备并初始化 uiautomator2。"""
        print("📱 正在连接设备…")

        try:
            if self.device_serial:
                self.d = u2.connect(self.device_serial)
            else:
                self.d = u2.connect()  # 自动检测
        except Exception as e:
            print(f"\n❌ 设备连接失败：{e}")
            print("\n请确认：")
            print("  1. 手机已开启 USB 调试（设置 → 开发者选项 → USB 调试）")
            print("  2. 手机已通过 USB 连接电脑")
            print("  3. 已安装 adb 并在 PATH 中")
            print("  4. 运行 adb devices 确认设备可见")
            print("  5. 如使用无线连接：adb connect <IP:端口>")
            sys.exit(1)

        device_info = self.d.info
        print(f"✅ 已连接设备：{self.d.serial}")
        self._log(f"设备信息：{device_info}")

        # 初始化 ATX agent
        # 注意：set_fastinput_ime(True) 会切换到 ATX 输入法，
        # 这是自动化检测的重要指纹！风控系统可以通过 InputMethodManager
        # 检测到非标准输入法。仅在 low 隐身级别下启用。
        self._log("初始化 ATX agent…")
        if self._human_cfg.level == "low":
            try:
                self.d.set_fastinput_ime(True)
            except Exception as e:
                self._warn(f"设置输入法失败（非致命）：{e}")
        else:
            self._log("跳过 set_fastinput_ime（避免输入法指纹）")

        # 降低 ATX agent 指纹可见性
        self._hide_atx_fingerprint()

    # ── ATX 指纹隐藏 ──────────────────────────────────────────────────
    def _hide_atx_fingerprint(self) -> None:
        """尽量降低 ATX agent 指纹可见性。

        uiautomator2 会在设备上安装 ATX Agent（com.github.uiautomator），
        这是风控系统检测自动化的头号指纹：
          - /data/local/tmp/atx-agent 进程持续运行
          - com.github.uiautomator APK 安装记录
          - ATX 输入法切换到非标准输入法
        此方法在保留 u2 连接功能的前提下，尽量降低这些指纹的可见性。
        """
        if not self.d:
            return

        self._log("正在降低 ATX agent 指纹可见性…")

        # 1. 确保输入法切回系统默认（不使用 ATX IME）
        #    ATX 输入法是风控检测的重要特征：InputMethodManager 中出现非标准输入法
        try:
            # 获取当前输入法
            cur_ime = self.d.shell("settings get secure default_input_method 2>/dev/null")[0].strip()
            if "uiautomator" in cur_ime.lower() or "atx" in cur_ime.lower():
                self._log(f"当前输入法为 ATX IME：{cur_ime}，切回系统默认")
                self.d.shell(
                    "settings put secure default_input_method "
                    "com.android.inputmethod.latin/.LatinIME 2>/dev/null"
                )
                # 也尝试其他常见系统输入法
                for ime in [
                    "com.sohu.inputmethod.sogou/.SogouIME",          # 搜狗
                    "com.iflytek.inputmethod/.FlyIME",               # 讯飞
                    "com.baidu.input/.ImeService",                   # 百度
                ]:
                    try:
                        # 检查输入法是否安装
                        check = self.d.shell(f"pm list packages | grep '{ime.split('/')[0].split('.')[-1]}'")[0]
                        if check.strip():
                            self.d.shell(f"settings put secure default_input_method {ime} 2>/dev/null")
                            self._log(f"切换到已安装输入法：{ime}")
                            break
                    except Exception:
                        continue
            else:
                self._log(f"当前输入法非 ATX：{cur_ime}，无需切换")
        except Exception as e:
            self._log(f"输入法切换失败（非致命）：{e}")

        # 2. 停止 ATX agent 的 UI 界面（保留后台 HTTP 服务，u2 连接需要）
        try:
            self.d.shell("am force-stop com.github.uiautomator 2>/dev/null")
            self._log("已 force-stop com.github.uiautomator（UI 界面）")
        except Exception as e:
            self._log(f"force-stop ATX 失败（非致命）：{e}")

        # 3. 如果隐身级别 >= medium，尝试隐藏 ATX APK 的 launcher activity
        #    这样 ATX 不会出现在最近任务列表中
        if self._human_cfg.level in ("medium", "high"):
            try:
                self.d.shell(
                    "pm disable com.github.uiautomator/.MainActivity 2>/dev/null"
                )
                self._log("已 disable ATX launcher activity")
            except Exception as e:
                self._log(f"disable ATX activity 失败（需 root）：{e}")

    # ── Root 检测 ──────────────────────────────────────────────────────
    def check_root(self) -> bool:
        """检测设备是否已 root。

        策略（由快到慢，任一成功即返回 True）：
          1. `su -c id` → 返回 uid=0(root)
          2. `which su` → su 二进制存在
          3. 检查 /system/xbin/su 或 /sbin/su 是否存在
          4. 检查 Build.TAGS 是否含 "test-keys"

        Returns:
            是否已 root
        """
        # 使用缓存
        if self._has_root is not None:
            return self._has_root

        if not self.d:
            self._has_root = False
            return False

        print("🔍 正在检测设备 root 状态…")

        # 策略 1：su -c id
        try:
            output = self.d.shell("su -c id 2>/dev/null")[0]
            if "uid=0" in output or "root" in output:
                self._log("su -c id → root 权限确认")
                print("✅ 设备已 root（su 可用）")
                self._has_root = True
                return True
        except Exception as e:
            self._log(f"su -c id 失败：{e}")

        # 策略 2：which su
        try:
            output = self.d.shell("which su 2>/dev/null")[0].strip()
            if output and "not found" not in output:
                self._log(f"which su → {output}")
                print("✅ 设备已 root（su 二进制存在）")
                self._has_root = True
                return True
        except Exception as e:
            self._log(f"which su 失败：{e}")

        # 策略 3：检查常见 su 路径
        su_paths = ["/system/xbin/su", "/sbin/su", "/system/bin/su",
                     "/vendor/bin/su", "/su/bin/su"]
        for path in su_paths:
            try:
                output = self.d.shell(f"ls -l {path} 2>/dev/null")[0].strip()
                if output and "No such file" not in output:
                    self._log(f"找到 su：{output}")
                    print(f"✅ 设备已 root（su 位于 {path}）")
                    self._has_root = True
                    return True
            except Exception:
                continue

        # 策略 4：Build.TAGS 含 test-keys
        try:
            output = self.d.shell("getprop ro.build.tags 2>/dev/null")[0].strip()
            if "test-keys" in output:
                self._log("Build.TAGS 含 test-keys")
                print("✅ 设备已 root（test-keys build）")
                self._has_root = True
                return True
        except Exception as e:
            self._log(f"getprop ro.build.tags 失败：{e}")

        # 所有策略均未检测到 root
        print("⚠️ 设备未 root（Frida/注入类方案不可用，将使用 Intent/OCR/Native 方案）")
        self._has_root = False
        return False

    # ── APP 启动 ──────────────────────────────────────────────────────
    def launch_damai_app(self) -> None:
        """启动大麦 APP。"""
        print("🚀 正在启动大麦 APP…")
        try:
            self.d.app_start(DAMAI_PACKAGE, DAMAI_ACTIVITY, wait=True)
            human_delay(3.0, config=self._human_cfg)  # 等待 APP 启动
            self._log("大麦 APP 已启动")
            # 预热行为：模拟用户刚打开 APP 的浏览
            human_warmup(self.d, config=self._human_cfg)
        except Exception as e:
            self._warn(f"启动大麦 APP 失败：{e}")
            # 尝试只用包名启动
            try:
                self.d.app_start(DAMAI_PACKAGE, wait=True)
                human_delay(3.0, config=self._human_cfg)
                self._log("大麦 APP 已启动（备用方式）")
                human_warmup(self.d, config=self._human_cfg)
            except Exception as e2:
                print(f"❌ 无法启动大麦 APP：{e2}")
                print("请确认已安装大麦 APP（包名：cn.damai）")
                sys.exit(1)

    def stop_damai_app(self) -> None:
        """停止大麦 APP。"""
        try:
            self.d.app_stop(DAMAI_PACKAGE)
            self._log("大麦 APP 已停止")
        except Exception:
            pass

    # ── WebView 上下文切换 ────────────────────────────────────────────
    def _find_devtools_port(self) -> Optional[int]:
        """通过 adb 查找设备上 WebView 的 Chrome DevTools 端口。

        核心原理：
          Android WebView 的 DevTools 监听在 Unix 抽象套接字上，名称形如：
            @webview_devtools_remote_<pid>       (Android System WebView)
            @chrome_devtools_remote_<pid>        (Chrome)
            @devtools_remote_<pid>               (旧版)
          必须用 adb forward tcp:<local> localabstract:<socket_name> 转发，
          而非 tcp:<port> → tcp:<port>（设备端没有 TCP 端口监听）。

        Returns:
            DevTools 本地转发端口号，或 None
        """
        if not self.d:
            return None

        if not requests:
            self._warn("缺少 requests 库，无法检测 DevTools 端口")
            return None

        # ── 方法 1：从 /proc/net/unix 提取抽象套接字名，正确转发 ──
        # 这是最可靠的方式
        try:
            # /proc/net/unix 第 6 列是套接字路径，抽象套接字以 @ 开头
            # 但在 /proc/net/unix 中 @ 会被显示为空格
            output = self.d.shell(
                "cat /proc/net/unix 2>/dev/null"
            )[0]
            if output:
                # 查找所有 devtools 相关的抽象套接字
                # 在 /proc/net/unix 中，抽象套接字的路径列以空格开头（@ 被替换为空格）
                devtools_sockets = []
                for line in output.splitlines():
                    parts = line.split()
                    if len(parts) >= 6:
                        sock_path = parts[-1]  # 最后一列是路径
                        # 匹配 devtools 抽象套接字（@ 被显示为前导空格或 @）
                        if any(kw in sock_path.lower() for kw in
                               ["devtools_remote", "webview_devtools"]):
                            # 还原 @ 前缀（抽象套接字在 /proc/net/unix 中 @ 显示为空格）
                            if sock_path.startswith(" "):
                                sock_path = "@" + sock_path[1:]
                            devtools_sockets.append(sock_path)

                # 去重
                devtools_sockets = list(dict.fromkeys(devtools_sockets))

                if devtools_sockets:
                    self._log(f"找到 {len(devtools_sockets)} 个 devtools 抽象套接字：{devtools_sockets}")

                    # 对每个套接字尝试转发
                    local_port = 9222
                    for sock_name in devtools_sockets:
                        # 去掉 @ 前缀得到 localabstract 的名称
                        abstract_name = sock_name.lstrip("@")
                        self._log(f"尝试 adb forward tcp:{local_port} localabstract:{abstract_name}")
                        try:
                            # 先移除可能存在的旧转发
                            try:
                                self.d.adb.forward_remove(f"tcp:{local_port}")
                            except Exception:
                                pass
                            # 设置正确的转发：本地 TCP → 设备抽象套接字
                            self.d.adb.forward(f"tcp:{local_port}", f"localabstract:{abstract_name}")
                            time.sleep(0.5)
                            # 验证端口可用
                            resp = requests.get(
                                f"http://127.0.0.1:{local_port}/json/version",
                                timeout=3,
                            )
                            if resp.status_code == 200:
                                self._log(f"DevTools 在端口 {local_port} 可用（via localabstract:{abstract_name}）：{resp.json()}")
                                return local_port
                            else:
                                self._log(f"端口 {local_port} 响应非 200：{resp.status_code}")
                        except Exception as e:
                            self._log(f"转发 localabstract:{abstract_name} 失败：{e}")
                        finally:
                            # 如果失败，移除转发
                            try:
                                self.d.adb.forward_remove(f"tcp:{local_port}")
                            except Exception:
                                pass
                        local_port += 1
                else:
                    self._log("/proc/net/unix 中未找到 devtools 抽象套接字")
        except Exception as e:
            self._log(f"解析 /proc/net/unix 失败：{e}")

        # ── 方法 2：用 adb shell cat /proc/net/unix + grep 精简版 ──
        # 有些设备 /proc/net/unix 权限受限，用 grep 直接搜
        try:
            for pattern in ["webview_devtools", "chrome_devtools", "devtools_remote"]:
                output = self.d.shell(
                    f"cat /proc/net/unix 2>/dev/null | grep -i '{pattern}'"
                )[0]
                if output:
                    self._log(f"grep '{pattern}' 结果：{output[:500]}")
                    # 解析套接字名
                    for line in output.strip().splitlines():
                        parts = line.split()
                        if parts:
                            sock_path = parts[-1]
                            abstract_name = sock_path.lstrip("@").lstrip()
                            if abstract_name and any(kw in abstract_name.lower() for kw in
                                                      ["devtools_remote", "webview_devtools"]):
                                self._log(f"尝试转发 localabstract:{abstract_name}")
                                try:
                                    try:
                                        self.d.adb.forward_remove("tcp:9222")
                                    except Exception:
                                        pass
                                    self.d.adb.forward("tcp:9222", f"localabstract:{abstract_name}")
                                    time.sleep(0.5)
                                    resp = requests.get(
                                        "http://127.0.0.1:9222/json/version",
                                        timeout=3,
                                    )
                                    if resp.status_code == 200:
                                        self._log(f"DevTools 在端口 9222 可用（via localabstract:{abstract_name}）")
                                        return 9222
                                except Exception as e:
                                    self._log(f"转发失败：{e}")
                                finally:
                                    try:
                                        self.d.adb.forward_remove("tcp:9222")
                                    except Exception:
                                        pass
        except Exception as e:
            self._log(f"方法 2 grep 搜索失败：{e}")

        # ── 方法 3：通过 pidof / ps 找 WebView 进程，构造套接字名 ──
        try:
            # 找 WebView 相关进程 PID
            for proc_name in ["webview", "chrome", "damai"]:
                pid_out = self.d.shell(
                    f"pidof {proc_name} 2>/dev/null || "
                    f"ps 2>/dev/null | grep -i '{proc_name}' | head -5"
                )[0]
                if pid_out:
                    self._log(f"进程 '{proc_name}' 输出：{pid_out[:300]}")
                    # 提取 PID（数字）
                    pids = re.findall(r'\b(\d+)\b', pid_out)
                    for pid in pids[:5]:  # 最多尝试 5 个 PID
                        for sock_prefix in ["webview_devtools_remote_", "chrome_devtools_remote_"]:
                            abstract_name = f"{sock_prefix}{pid}"
                            self._log(f"尝试 localabstract:{abstract_name}")
                            try:
                                try:
                                    self.d.adb.forward_remove("tcp:9222")
                                except Exception:
                                    pass
                                self.d.adb.forward("tcp:9222", f"localabstract:{abstract_name}")
                                time.sleep(0.5)
                                resp = requests.get(
                                    "http://127.0.0.1:9222/json/version",
                                    timeout=3,
                                )
                                if resp.status_code == 200:
                                    self._log(f"DevTools 在端口 9222 可用（via localabstract:{abstract_name}）")
                                    return 9222
                            except Exception:
                                pass
                            finally:
                                try:
                                    self.d.adb.forward_remove("tcp:9222")
                                except Exception:
                                    pass
        except Exception as e:
            self._log(f"方法 3 进程搜索失败：{e}")

        # ── 方法 4：直接尝试 tcp:9222 → tcp:9222（某些定制 ROM 可能用 TCP） ──
        for port in [9222, 9229, 9223]:
            try:
                self.d.adb.forward(f"tcp:{port}", f"tcp:{port}")
                time.sleep(0.5)
                resp = requests.get(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=2,
                )
                if resp.status_code == 200:
                    self._log(f"DevTools 在端口 {port} 可用（TCP 直连）")
                    return port
            except Exception:
                try:
                    self.d.adb.forward_remove(f"tcp:{port}")
                except Exception:
                    pass

        return None

    def _connect_cdp(self, port: int) -> bool:
        """通过 Chrome DevTools Protocol 连接 WebView 页面。

        Args:
            port: DevTools 本地转发端口

        Returns:
            是否成功连接
        """
        if not requests:
            self._warn("缺少 requests 库，无法连接 CDP。请执行：pip install requests")
            return False
        if not websocket:
            self._warn("缺少 websocket-client 库，无法连接 CDP。请执行：pip install websocket-client")
            return False

        try:
            # 获取可调试页面列表
            resp = requests.get(f"http://127.0.0.1:{port}/json", timeout=5)
            pages = resp.json()
            self._log(f"DevTools 可调试页面：{json.dumps(pages, ensure_ascii=False)[:1000]}")
        except Exception as e:
            self._warn(f"获取 DevTools 页面列表失败：{e}")
            return False

        if not pages:
            self._warn("DevTools 返回空页面列表")
            return False

        # 选择大麦相关的页面（优先选搜索页，其次任意页面）
        target_page = None
        for page in pages:
            url = page.get("url", "")
            title = page.get("title", "")
            self._log(f"  页面：url={url!r}  title={title!r}")
            # 优先选搜索结果页
            if "search" in url or "damai" in url:
                target_page = page
                break

        # 如果没找到大麦页面，选第一个非空白页
        if not target_page:
            for page in pages:
                url = page.get("url", "")
                if url and "about:blank" not in url:
                    target_page = page
                    break

        # 兜底：选第一个
        if not target_page:
            target_page = pages[0]

        ws_url = target_page.get("webSocketDebuggerUrl", "")
        page_url = target_page.get("url", "")
        page_title = target_page.get("title", "")
        self._log(f"选中页面：url={page_url!r}  title={page_title!r}")
        self._log(f"WebSocket URL：{ws_url}")

        if not ws_url:
            self._warn("页面无 webSocketDebuggerUrl")
            return False

        # 创建 CDP 连接对象
        self._wd = CDPWebView(ws_url, verbose=self.verbose)
        self._webview_context = page_url
        print(f"✅ 已通过 CDP 连接 WebView：{page_url}")
        return True

    def _get_webviews(self) -> List[str]:
        """获取当前所有 WebView 上下文。"""
        # 通过 adb + Chrome DevTools 获取
        port = self._find_devtools_port()
        if port and requests:
            try:
                resp = requests.get(f"http://127.0.0.1:{port}/json", timeout=5)
                pages = resp.json()
                return [p.get("url", "") for p in pages if p.get("url")]
            except Exception as e:
                self._log(f"获取 DevTools 页面列表失败：{e}")
        return []

    def switch_to_webview(self) -> bool:
        """切换到 WebView 上下文（通过 Chrome DevTools Protocol）。

        Returns:
            是否成功切换
        """
        print("🔄 正在切换到 WebView（CDP 方式）…")

        if not requests:
            self._warn("缺少 requests 库，请执行：pip install requests")
        if not websocket:
            self._warn("缺少 websocket-client 库，请执行：pip install websocket-client")
        if not requests or not websocket:
            print("⚠️ 缺少依赖，无法连接 WebView。")
            print("请执行：pip install requests websocket-client")
            return False

        # 查找 DevTools 端口
        port = self._find_devtools_port()
        if port is None:
            # 最后的兜底：尝试通过进程 PID 构造套接字名
            self._log("自动检测端口失败，尝试通过进程 PID 构造套接字名…")
            try:
                # 查找大麦 APP 的 WebView 进程
                ps_out = self.d.shell(
                    "ps 2>/dev/null | grep -E 'webview|chrome' | head -10"
                )[0]
                if ps_out:
                    self._log(f"WebView/Chrome 进程：\n{ps_out[:500]}")
                    pids = re.findall(r'\b(\d+)\b', ps_out)
                    for pid in pids[:5]:
                        for sock_prefix in ["webview_devtools_remote_", "chrome_devtools_remote_"]:
                            abstract_name = f"{sock_prefix}{pid}"
                            try:
                                try:
                                    self.d.adb.forward_remove("tcp:9222")
                                except Exception:
                                    pass
                                self.d.adb.forward("tcp:9222", f"localabstract:{abstract_name}")
                                time.sleep(0.5)
                                resp = requests.get("http://127.0.0.1:9222/json/version", timeout=3)
                                if resp.status_code == 200:
                                    port = 9222
                                    self._log(f"通过 PID {pid} 构造的 localabstract:{abstract_name} 成功")
                                    break
                            except Exception:
                                pass
                            finally:
                                if port is None:
                                    try:
                                        self.d.adb.forward_remove("tcp:9222")
                                    except Exception:
                                        pass
                        if port is not None:
                            break
            except Exception as e:
                self._log(f"兜底进程搜索失败：{e}")

        if port is None:
            print("⚠️ 未找到 WebView DevTools 端口。")
            print("请尝试：")
            print("  1. 确保大麦 APP 已打开并显示了 H5 页面")
            print("  2. 在手机开发者选项中开启「WebView 调试」")
            print("     (设置 → 开发者选项 → WebView 实现 → 选择含 'Debug' 的版本)")
            print("  3. 确认手机已开启「USB 调试」和「USB 调试（安全设置）」")
            print("  4. 手动执行：")
            print("     adb shell cat /proc/net/unix | grep devtools   # 查看套接字名")
            print("     adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>")
            print("  5. 重启 APP 后重试")
            return False

        # 通过 CDP 连接
        return self._connect_cdp(port)

    def switch_to_native(self) -> None:
        """切换回 Native 上下文。"""
        if self._wd and self._native_context:
            try:
                self._wd.switch_to(self._native_context)
                self._log("已切换回 Native 上下文")
            except Exception as e:
                self._warn(f"切换回 Native 失败：{e}")

    # ── H5 页面导航 ──────────────────────────────────────────────────
    def navigate_to_url(self, url: str) -> None:
        """在 WebView 中导航到指定 URL。"""
        if not self._wd:
            self._warn("未连接 WebView，无法导航")
            return

        try:
            self._wd.get(url)
            self._log(f"已导航到：{url}")
            human_delay(2.0, config=self._human_cfg)
        except Exception as e:
            self._warn(f"导航失败：{e}")

    def get_current_url(self) -> str:
        """获取 WebView 当前 URL。"""
        if not self._wd:
            return ""
        try:
            return self._wd.current_url
        except Exception:
            return ""

    # ── 登录流程 ──────────────────────────────────────────────────────
    def navigate_to_login(self) -> bool:
        """导航到登录页面。

        策略：
          1. 先尝试在 APP native 层点击「我的」→ 登录
          2. 备用：直接在 WebView 中打开登录页 URL

        Returns:
            是否成功到达登录页
        """
        print("🔑 正在导航到登录页面…")

        # 策略 1：在 native 层操作
        try:
            self.switch_to_native()
            human_delay(1.0, config=self._human_cfg)

            # 点击底部「我的」tab（多种匹配方式）
            my_tab = self.d(text="我的")
            if not my_tab.exists(timeout=3):
                my_tab = self.d(textContains="我的")
            if not my_tab.exists(timeout=3):
                my_tab = self.d(description="我的")
            if not my_tab.exists(timeout=3):
                my_tab = self.d(resourceIdMatches=".*tab.*mine.*|.*tab.*my.*|.*bottom.*my.*")
            if my_tab.exists(timeout=3):
                human_click(my_tab, config=self._human_cfg)
                self._log("已点击「我的」tab")
                human_delay(2.0, config=self._human_cfg)

                # 查找登录/注册按钮（多种匹配方式）
                login_btn = self.d(textContains="登录")
                if not login_btn.exists(timeout=3):
                    login_btn = self.d(textContains="登录/注册")
                if not login_btn.exists(timeout=3):
                    login_btn = self.d(textContains="Login")
                if login_btn.exists(timeout=3):
                    human_click(login_btn, config=self._human_cfg)
                    self._log("已点击登录按钮")
                    human_delay(2.0, config=self._human_cfg)
                    return True

                # 查找头像（未登录时点击头像进入登录）
                avatar = self.d(resourceIdMatches=".*avatar.*|.*user.*icon.*|.*head.*img.*")
                if avatar.exists(timeout=3):
                    human_click(avatar, config=self._human_cfg)
                    self._log("已点击头像进入登录")
                    human_delay(2.0, config=self._human_cfg)
                    return True

                # 查找「登录/注册」文字（可能直接是可点击文字）
                login_text = self.d(text="登录/注册")
                if login_text.exists(timeout=3):
                    human_click(login_text, config=self._human_cfg)
                    self._log("已点击「登录/注册」文字")
                    human_delay(2.0, config=self._human_cfg)
                    return True

        except Exception as e:
            self._log(f"Native 层导航登录失败：{e}")

        # 策略 2：在 WebView 中直接打开登录页
        print("  尝试通过 H5 页面登录…")
        if self.switch_to_webview():
            self.navigate_to_url(H5_LOGIN_URL)
            human_delay(3.0, config=self._human_cfg)
            current = self.get_current_url()
            if "passport" in current or "login" in current:
                print("✅ 已到达登录页面")
                return True

        print("⚠️ 无法导航到登录页面，请手动在 APP 中打开登录页后重试")
        return False

    def input_phone(self, phone: str) -> bool:
        """在登录页输入手机号（拟人化逐字输入）。

        Args:
            phone: 手机号码

        Returns:
            是否成功输入
        """
        print(f"📝 正在输入手机号：{phone}")

        # 先尝试 WebView 方式（通过 JS）
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    f"""
                    var el = document.querySelector('input[type="tel"], input[name="mobile"], '
                        + 'input[placeholder*="手机"], input[placeholder*="号码"]');
                    if (el) {{ el.value = '{phone}'; el.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }}
                    return false;
                    """
                )
                if ok:
                    self._log("已通过 WebView(JS) 输入手机号")
                    return True
            except Exception as e:
                self._log(f"WebView 输入手机号失败：{e}")

        # 备用：Native 层输入（拟人化逐字输入）
        try:
            self.switch_to_native()
            # 查找手机号输入框
            phone_field = self.d(
                resourceIdMatches=".*phone.*|.*mobile.*|.*account.*"
            )
            if phone_field.exists(timeout=3):
                human_type_text(self.d, phone_field, phone, config=self._human_cfg)
                self._log("已通过 Native 逐字输入手机号")
                return True

            # 通过 className 查找 EditText
            edit_fields = self.d(className="android.widget.EditText")
            if edit_fields.exists(timeout=3):
                human_type_text(self.d, edit_fields, phone, config=self._human_cfg)
                self._log("已通过 EditText 逐字输入手机号")
                return True

        except Exception as e:
            self._log(f"Native 输入手机号失败：{e}")

        self._warn("无法找到手机号输入框")
        return False

    def click_send_code(self) -> bool:
        """点击发送验证码按钮。

        Returns:
            是否成功点击
        """
        print("📤 正在点击发送验证码…")

        # WebView 方式（通过 JS）
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    """
                    var btns = document.querySelectorAll('.send-code, .get-code, [class*="send"], [class*="code"]');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].textContent || '').trim();
                        if (t.indexOf('获取验证码') >= 0 || t.indexOf('发送验证码') >= 0 || t.indexOf('获取短信') >= 0) {
                            btns[i].click(); return true;
                        }
                    }
                    // 尝试 button 标签
                    var buttons = document.querySelectorAll('button');
                    for (var j = 0; j < buttons.length; j++) {
                        var t2 = (buttons[j].textContent || '').trim();
                        if (t2.indexOf('获取验证码') >= 0 || t2.indexOf('发送验证码') >= 0) {
                            buttons[j].click(); return true;
                        }
                    }
                    return false;
                    """
                )
                if ok:
                    self._log("已通过 WebView(JS) 点击发送验证码")
                    return True
            except Exception as e:
                self._log(f"WebView 点击发送验证码失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            send_btn = self.d(textContains="获取验证码")
            if send_btn.exists(timeout=3):
                human_click(send_btn, config=self._human_cfg)
                self._log("已通过 Native 点击发送验证码")
                return True

            send_btn = self.d(textContains="发送验证码")
            if send_btn.exists(timeout=3):
                human_click(send_btn, config=self._human_cfg)
                self._log("已通过 Native 点击发送验证码")
                return True

            send_btn = self.d(textContains="获取短信")
            if send_btn.exists(timeout=3):
                human_click(send_btn, config=self._human_cfg)
                self._log("已通过 Native 点击发送验证码")
                return True

        except Exception as e:
            self._log(f"Native 点击发送验证码失败：{e}")

        self._warn("无法找到发送验证码按钮")
        return False

    def input_verify_code(self, code: str) -> bool:
        """输入短信验证码（拟人化逐字输入，比手机号稍快）。

        Args:
            code: 验证码

        Returns:
            是否成功输入
        """
        print(f"📝 正在输入验证码：{code}")

        # WebView 方式（通过 JS）
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    f"""
                    var el = document.querySelector('input[type="number"], input[name="code"], '
                        + 'input[name="verifyCode"], input[placeholder*="验证码"]');
                    if (el) {{ el.value = '{code}'; el.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }}
                    return false;
                    """
                )
                if ok:
                    self._log("已通过 WebView(JS) 输入验证码")
                    return True
            except Exception as e:
                self._log(f"WebView 输入验证码失败：{e}")

        # Native 方式（拟人化逐字输入，验证码输入稍快）
        try:
            self.switch_to_native()
            # 查找验证码输入框（通常是第二个 EditText 或有特定 resourceId）
            code_field = self.d(
                resourceIdMatches=".*code.*|.*verify.*|.*sms.*"
            )
            if code_field.exists(timeout=3):
                human_type_text(self.d, code_field, code, interval=(0.03, 0.10),
                                config=self._human_cfg)
                self._log("已通过 Native 逐字输入验证码")
                return True

            # 查找所有 EditText，取第二个（第一个是手机号）
            edit_fields = self.d(className="android.widget.EditText")
            count = edit_fields.count
            if count >= 2:
                human_type_text(self.d, edit_fields[count - 1], code, interval=(0.03, 0.10),
                                config=self._human_cfg)
                self._log("已通过第二个 EditText 逐字输入验证码")
                return True

        except Exception as e:
            self._log(f"Native 输入验证码失败：{e}")

        self._warn("无法找到验证码输入框")
        return False

    def click_login(self) -> bool:
        """点击登录按钮。

        Returns:
            是否成功点击
        """
        print("🔐 正在点击登录…")

        # WebView 方式（通过 JS）
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    """
                    var btn = document.querySelector('button[type="submit"], .login-btn, [class*="submit"]');
                    if (btn) { btn.click(); return true; }
                    var btns = document.querySelectorAll('button');
                    for (var i = 0; i < btns.length; i++) {
                        if ((btns[i].textContent || '').trim().indexOf('登录') >= 0) {
                            btns[i].click(); return true;
                        }
                    }
                    return false;
                    """
                )
                if ok:
                    self._log("已通过 WebView(JS) 点击登录")
                    return True
            except Exception as e:
                self._log(f"WebView 点击登录失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            login_btn = self.d(text="登录")
            if login_btn.exists(timeout=3):
                human_click(login_btn, config=self._human_cfg)
                self._log("已通过 Native 点击登录")
                return True

            login_btn = self.d(textContains="登录")
            if login_btn.exists(timeout=3):
                human_click(login_btn, config=self._human_cfg)
                self._log("已通过 Native 点击登录")
                return True

        except Exception as e:
            self._log(f"Native 点击登录失败：{e}")

        self._warn("无法找到登录按钮")
        return False

    def wait_login_success(self, timeout: int = LONG_TIMEOUT) -> bool:
        """等待登录成功。

        检测方式：
          1. 页面 URL 变化（离开登录页）
          2. 出现用户头像/昵称元素
          3. 获取到登录态 cookies

        Args:
            timeout: 最大等待秒数

        Returns:
            是否登录成功
        """
        print("⏳ 等待登录完成…")

        start = time.time()
        old_url = self.get_current_url()

        while time.time() - start < timeout:
            # 检查 URL 是否已离开登录页
            current_url = self.get_current_url()
            if current_url and "login" not in current_url and "passport" not in current_url:
                if current_url != old_url:
                    print("✅ 登录成功（页面已跳转）")
                    return True

            # 检查 native 层是否出现用户信息
            try:
                self.switch_to_native()
                if self.d(textContains="我的订单").exists(timeout=1):
                    print("✅ 登录成功（检测到用户信息）")
                    return True
                if self.d(textContains="退出").exists(timeout=1):
                    print("✅ 登录成功（检测到退出按钮）")
                    return True
            except Exception:
                pass

            human_delay(2.0, config=self._human_cfg)

        self._warn("登录超时")
        return False

    # ── Cookie 提取 ────────────────────────────────────────────────── ──────────────────────────────────────────────────
    def get_cookies(self) -> List[Dict[str, Any]]:
        """从 WebView 提取 cookies。

        Returns:
            Cookie 列表，每项为 dict（name, value, domain, path, …）
        """
        print("🍪 正在提取 cookies…")

        # 方式 1：通过 Chrome DevTools Protocol
        if self._wd:
            try:
                # CDPWebView 直接提供 get_cookies
                self._cookies = self._wd.get_cookies()
                if self._cookies:
                    print(f"✅ 已提取 {len(self._cookies)} 个 cookies（CDP 方式）")
                    return self._cookies
            except Exception as e:
                self._log(f"CDP 获取 cookies 失败：{e}")

            # 方式 2：通过 JS 获取
            try:
                cookie_str = self._wd.execute_script("return document.cookie;")
                if cookie_str:
                    self._cookies = self._parse_cookie_string(cookie_str)
                    print(f"✅ 已提取 {len(self._cookies)} 个 cookies（JS 方式）")
                    return self._cookies
            except Exception as e:
                self._log(f"JS 获取 cookies 失败：{e}")

        self._warn("未能提取 cookies")
        return []

    @staticmethod
    def _parse_cookie_string(cookie_str: str) -> List[Dict[str, Any]]:
        """解析 document.cookie 字符串为 cookie 列表。"""
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".damai.cn",
                "path": "/",
            })
        return cookies

    def get_cookie_string(self) -> str:
        """获取 cookie 字符串（key=value; key2=value2 格式）。

        可直接注入到 DamaiMonitor 使用。
        """
        if not self._cookies:
            self.get_cookies()

        parts = []
        for c in self._cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            if name and value:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    def save_cookies(self, path: str = "damai_cookies_u2.json") -> None:
        """保存 cookies 到文件。"""
        if not self._cookies:
            self.get_cookies()

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._cookies, f, ensure_ascii=False, indent=2)
            self._log(f"Cookies 已保存到 {path}（{len(self._cookies)} 个）")
        except Exception as e:
            self._warn(f"保存 cookies 失败：{e}")

    # ── 搜索演出 ──────────────────────────────────────────────────────
    def navigate_to_search(self) -> bool:
        """导航到搜索页面。

        Returns:
            是否成功到达搜索页
        """
        print("🔍 正在导航到搜索页面…")

        # 策略 1：Native 层点击搜索入口
        try:
            self.switch_to_native()

            # 查找搜索框/搜索按钮
            search_entry = self.d(textContains="搜索")
            if search_entry.exists(timeout=3):
                human_click(search_entry, config=self._human_cfg)
                self._log("已点击搜索入口")
                human_delay(2.0, config=self._human_cfg)
                return True

            # 查找搜索图标
            search_icon = self.d(
                resourceIdMatches=".*search.*|.*home_search.*"
            )
            if search_icon.exists(timeout=3):
                human_click(search_icon, config=self._human_cfg)
                self._log("已点击搜索图标")
                human_delay(2.0, config=self._human_cfg)
                return True

        except Exception as e:
            self._log(f"Native 搜索导航失败：{e}")

        # 策略 2：WebView 中打开搜索页
        if self.switch_to_webview():
            self.navigate_to_url(H5_SEARCH_URL)
            human_delay(3.0, config=self._human_cfg)
            return True

        return False

    def input_search_keyword(self, keyword: str) -> bool:
        """输入搜索关键词。

        Args:
            keyword: 搜索关键词

        Returns:
            是否成功输入并触发搜索
        """
        print(f"📝 正在搜索：{keyword}")

        # WebView 方式（通过 JS）
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    f"""
                    var el = document.querySelector('input[type="search"], input[name="keyword"], '
                        + 'input[placeholder*="搜索"], .search-input input');
                    if (el) {{
                        el.value = '{keyword}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        // 触发搜索：回车或提交表单
                        var form = el.closest('form');
                        if (form) {{ form.submit(); }}
                        else {{
                            var ev = new KeyboardEvent('keydown', {{key: 'Enter', keyCode: 13, bubbles: true}});
                            el.dispatchEvent(ev);
                        }}
                        return true;
                    }}
                    return false;
                    """
                )
                if ok:
                    self._log("已通过 WebView(JS) 输入搜索关键词")
                    human_delay(3.0, config=self._human_cfg)
                    return True
            except Exception as e:
                self._log(f"WebView 搜索失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            search_field = self.d(
                resourceIdMatches=".*search.*input.*|.*query.*"
            )
            if search_field.exists(timeout=3):
                human_type_text(self.d, search_field, keyword, config=self._human_cfg)
                # 点击搜索按钮
                search_btn = self.d(textContains="搜索")
                if search_btn.exists(timeout=2):
                    human_click(search_btn, config=self._human_cfg)
                else:
                    # 按键盘回车
                    self.d.press("enter")
                self._log("已通过 Native 逐字输入搜索关键词")
                human_delay(3.0, config=self._human_cfg)
                return True

            # 通用 EditText
            edit = self.d(className="android.widget.EditText")
            if edit.exists(timeout=3):
                human_type_text(self.d, edit, keyword, config=self._human_cfg)
                self.d.press("enter")
                self._log("已通过 EditText 逐字搜索")
                human_delay(3.0, config=self._human_cfg)
                return True

        except Exception as e:
            self._log(f"Native 搜索失败：{e}")

        self._warn("无法输入搜索关键词")
        return False

    @staticmethod
    def _extract_item_id_from_url(url: str) -> Optional[str]:
        """从各种大麦 URL 格式中提取演出 ID。

        支持的格式：
          - item.htm?id=12345
          - /item/12345
          - itemId=12345
          - showId=12345
          - /detail/12345
          - id=12345 (通用)
        """
        patterns = [
            r'item\.htm\?id=(\d+)',
            r'/item/(\d+)',
            r'itemId=(\d+)',
            r'showId=(\d+)',
            r'/detail/(\d+)',
            r'[?&]id=(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    def _ensure_webview_connected(self) -> bool:
        """确保 WebView 已连接，若未连接则自动尝试切换。

        在非 root 设备上，如果 WebView 调试未开启，会静默降级为纯 Native 模式。
        只在首次失败时输出提示，后续静默返回。

        Returns:
            是否已连接 WebView（self._wd 非 None）
        """
        if self._wd is not None:
            return True

        # 已提示过且已知不可用，静默返回
        if self._webview_warned:
            return False

        print("  ⚡ _wd 为 None，尝试自动连接 WebView…")

        # 先输出诊断信息
        try:
            unix_out = self.d.shell(
                "cat /proc/net/unix 2>/dev/null | grep -i devtools"
            )[0]
            if unix_out:
                print(f"  📋 设备 devtools 套接字：")
                for line in unix_out.strip().splitlines()[:10]:
                    print(f"     {line.strip()}")
            else:
                print("  ⚠️ 设备上未找到任何 devtools 套接字")
                print("     原因：APP 未调用 WebView.setWebContentsDebuggingEnabled(true)")
                print("     将降级为纯 Native 层操作模式（功能可能受限）")
        except Exception as e:
            self._log(f"诊断信息获取失败：{e}")

        if self.switch_to_webview():
            print(f"  ✅ 已自动连接 WebView（上下文：{self._webview_context}）")
            return True
        else:
            self._webview_warned = True
            self._log("WebView 连接失败，后续将使用纯 Native 模式")
            return False

    def _dump_webview_debug_info(self) -> None:
        """将 WebView 调试信息写入日志文件，供离线分析。

        输出文件：damai_webview_debug.log
        内容包括：_wd 对象信息、当前 URL、页面源码（前 5000 字符）、
                  所有 <a> 标签、所有 <div> class 列表、window.__INITIAL_DATA__ 等
        """
        # 先确保 WebView 已连接
        self._ensure_webview_connected()

        log_path = "damai_webview_debug.log"
        lines: List[str] = []
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"\n{'='*60}")
        lines.append(f"[{ts}] WebView 调试信息转储")
        lines.append(f"{'='*60}")

        # ── 1. _wd 对象基本信息 ──
        lines.append("\n--- [1] _wd 对象信息 ---")
        if self._wd is None:
            lines.append("_wd is None（未连接 WebView）")
        else:
            lines.append(f"_wd type: {type(self._wd)}")
            lines.append(f"_wd repr: {repr(self._wd)}")
            # 常用属性
            for attr in [
                "current_url", "title", "name", "session_id",
                "context", "window_handle",
            ]:
                try:
                    val = getattr(self._wd, attr)
                    lines.append(f"_wd.{attr} = {val!r}")
                except Exception as e:
                    lines.append(f"_wd.{attr} → 错误: {e}")
            # contexts 列表
            try:
                ctxs = self._wd.contexts
                lines.append(f"_wd.contexts = {ctxs}")
            except Exception as e:
                lines.append(f"_wd.contexts → 错误: {e}")
            # capabilities
            try:
                caps = self._wd.capabilities
                lines.append(f"_wd.capabilities = {json.dumps(caps, ensure_ascii=False, default=str)[:2000]}")
            except Exception as e:
                lines.append(f"_wd.capabilities → 错误: {e}")

        # ── 2. 当前页面 URL ──
        lines.append("\n--- [2] 当前页面 URL ---")
        try:
            url = self.get_current_url()
            lines.append(f"current_url: {url}")
        except Exception as e:
            lines.append(f"get_current_url() → 错误: {e}")

        # ── 3. 页面源码（前 5000 字符） ──
        lines.append("\n--- [3] 页面源码（前 5000 字符） ---")
        if self._wd:
            try:
                page_source = self._wd.page_source or ""
                lines.append(f"页面源码总长度: {len(page_source)}")
                lines.append(page_source[:5000])
            except Exception as e:
                lines.append(f"page_source → 错误: {e}")
        else:
            lines.append("_wd 为空，无法获取页面源码")

        # ── 4. 所有 <a> 标签 ──
        lines.append("\n--- [4] 页面所有 <a> 标签 ---")
        if self._wd:
            try:
                js_all_a = """
                (function() {
                    var links = document.querySelectorAll('a[href]');
                    var out = [];
                    for (var i = 0; i < links.length; i++) {
                        out.push({
                            index: i,
                            href: links[i].href || '',
                            text: (links[i].textContent || '').trim().substring(0, 80),
                            className: links[i].className || '',
                            id: links[i].id || '',
                            parentClassName: (links[i].parentElement && links[i].parentElement.className) || ''
                        });
                    }
                    return JSON.stringify({total: links.length, links: out});
                })();
                """
                raw = self._wd.execute_script(js_all_a)
                if raw:
                    info = json.loads(raw)
                    lines.append(f"共 {info.get('total', '?')} 个 <a> 标签")
                    for link in info.get("links", []):
                        lines.append(
                            f"  [{link.get('index')}] "
                            f"href={link.get('href')!r}  "
                            f"text={link.get('text')!r}  "
                            f"class={link.get('className')!r}  "
                            f"id={link.get('id')!r}  "
                            f"parentClass={link.get('parentClassName')!r}"
                        )
                else:
                    lines.append("JS 返回空，页面上无 <a> 标签")
            except Exception as e:
                lines.append(f"JS 获取 <a> 标签 → 错误: {e}")
        else:
            lines.append("_wd 为空，无法获取 <a> 标签")

        # ── 5. 页面顶层 <div> class 列表（帮助识别 DOM 结构） ──
        lines.append("\n--- [5] 页面主要 <div> class 列表 ---")
        if self._wd:
            try:
                js_divs = """
                (function() {
                    var divs = document.querySelectorAll('div[class]');
                    var classes = [];
                    for (var i = 0; i < divs.length; i++) {
                        var cls = divs[i].className;
                        if (cls && typeof cls === 'string' && cls.trim()) {
                            classes.push(cls.trim().substring(0, 100));
                        }
                    }
                    // 去重
                    var unique = [];
                    var seen = {};
                    for (var j = 0; j < classes.length; j++) {
                        if (!seen[classes[j]]) {
                            seen[classes[j]] = true;
                            unique.push(classes[j]);
                        }
                    }
                    return JSON.stringify(unique);
                })();
                """
                raw = self._wd.execute_script(js_divs)
                if raw:
                    class_list = json.loads(raw)
                    lines.append(f"共 {len(class_list)} 个不同的 div class：")
                    for cls in class_list[:100]:  # 最多记录 100 个
                        lines.append(f"  .{cls}")
                    if len(class_list) > 100:
                        lines.append(f"  … 还有 {len(class_list) - 100} 个省略")
                else:
                    lines.append("JS 返回空，无带 class 的 <div>")
            except Exception as e:
                lines.append(f"JS 获取 div class → 错误: {e}")
        else:
            lines.append("_wd 为空")

        # ── 6. window.__INITIAL_DATA__ / __NEXT_DATA__ 等全局变量 ──
        lines.append("\n--- [6] 页面全局 JS 数据 ---")
        if self._wd:
            for var_name in [
                "__INITIAL_DATA__", "__NEXT_DATA__", "__DATA__",
                "window.__INITIAL_DATA__", "window.__NEXT_DATA__",
                "__NUXT__", "__APP_DATA__",
            ]:
                try:
                    val = self._wd.execute_script(
                        f"try {{ return JSON.stringify(typeof {var_name} !== 'undefined' ? {var_name} : null); }} catch(e) {{ return null; }}"
                    )
                    if val and val != "null":
                        snippet = val[:3000] if len(val) > 3000 else val
                        lines.append(f"{var_name} (长度 {len(val)}): {snippet}")
                    else:
                        lines.append(f"{var_name}: 未定义或为 null")
                except Exception as e:
                    lines.append(f"{var_name} → 错误: {e}")
        else:
            lines.append("_wd 为空")

        # ── 7. document.querySelectorAll('a') 的 outerHTML（前 10 个） ──
        lines.append("\n--- [7] 前 10 个 <a> 标签 outerHTML ---")
        if self._wd:
            try:
                js_outer = """
                (function() {
                    var links = document.querySelectorAll('a');
                    var out = [];
                    for (var i = 0; i < Math.min(links.length, 10); i++) {
                        out.push(links[i].outerHTML.substring(0, 500));
                    }
                    return JSON.stringify(out);
                })();
                """
                raw = self._wd.execute_script(js_outer)
                if raw:
                    outer_list = json.loads(raw)
                    for i, html in enumerate(outer_list):
                        lines.append(f"  [{i}] {html}")
                else:
                    lines.append("无 <a> 标签")
            except Exception as e:
                lines.append(f"获取 outerHTML → 错误: {e}")
        else:
            lines.append("_wd 为空")

        # ── 写入文件 ──
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"📝 WebView 调试信息已写入 {log_path}")
        except Exception as e:
            self._warn(f"写入调试日志失败: {e}")

        # 同时在控制台输出关键摘要
        print(f"  [debug] _wd={type(self._wd).__name__ if self._wd else 'None'}")
        try:
            url = self.get_current_url()
            print(f"  [debug] current_url={url!r}")
        except Exception:
            pass
        if self._wd:
            try:
                a_count = self._wd.execute_script("return document.querySelectorAll('a').length;")
                print(f"  [debug] 页面 <a> 标签数量: {a_count}")
            except Exception:
                pass

    def get_first_result(self) -> Optional[Dict[str, str]]:
        """获取第一条搜索结果。

        Returns:
            dict with keys: name, item_id, url; or None
        """
        print("📋 正在获取第一条搜索结果…")

        # ── 确保 WebView 已连接 ──
        self._ensure_webview_connected()

        # ── 先转储 WebView 调试信息到日志文件 ──
        self._dump_webview_debug_info()

        result: Dict[str, str] = {}

        # ── 方式 1：通过 JS 在页面中直接查找所有 <a> 链接 ──
        # 这是最可靠的方式，不依赖特定 CSS 选择器
        if self._wd:
            try:
                # 用 JS 获取页面中所有指向演出详情的链接
                js_script = """
                (function() {
                    var links = document.querySelectorAll('a[href]');
                    var results = [];
                    for (var i = 0; i < links.length; i++) {
                        var href = links[i].href || '';
                        var text = (links[i].textContent || '').trim();
                        // 匹配大麦演出详情页的各种 URL 格式
                        if (/item\\.htm|\\/item\\/|itemId=|showId=|\\/detail\\//.test(href)) {
                            results.push({href: href, text: text});
                        }
                    }
                    return JSON.stringify(results);
                })();
                """
                raw = self._wd.execute_script(js_script)
                if raw:
                    items = json.loads(raw)
                    self._log(f"JS 找到 {len(items)} 个演出链接")
                    if items:
                        first = items[0]
                        result["name"] = first.get("text", "")
                        result["url"] = first.get("href", "")
                        item_id = self._extract_item_id_from_url(result["url"])
                        if item_id:
                            result["item_id"] = item_id
                        self._log(f"第一条结果（JS方式）：{result}")
                        if result.get("item_id") or result.get("url"):
                            return result
            except Exception as e:
                self._log(f"JS 查找演出链接失败：{e}")

        # ── 方式 2：WebView CSS 选择器（通过 JS 逐个尝试） ──
        if self._wd:
            try:
                # 扩展选择器，覆盖大麦搜索结果页多种可能的 DOM 结构
                selectors = [
                    # 大麦搜索结果常见结构
                    '.search-result a', '.result-list a',
                    '[class*="item"] a', '[class*="card"] a',
                    # 大麦移动端搜索结果
                    '.search-box__item a', '.search-item a',
                    '.result-item a', '.show-item a',
                    # 通用卡片/列表结构
                    '.list-item a', '.card a',
                    # 任何包含演出链接的 a 标签
                    'a[href*="item.htm"]', 'a[href*="/item/"]',
                    'a[href*="itemId="]', 'a[href*="showId="]',
                ]
                for sel in selectors:
                    try:
                        # 通过 JS 查找，避免 find_element_by_css_selector 不兼容
                        found = self._wd.execute_script(
                            f"""
                            var el = document.querySelector('{sel}');
                            if (el && el.href && el.href.startsWith('http')) {{
                                return JSON.stringify({{href: el.href, text: (el.textContent || '').trim()}});
                            }}
                            return null;
                            """
                        )
                        if found:
                            info = json.loads(found)
                            result["name"] = info.get("text", "")
                            result["url"] = info.get("href", "")
                            item_id = self._extract_item_id_from_url(result["url"])
                            if item_id:
                                result["item_id"] = item_id
                            self._log(f"第一条结果（CSS选择器 {sel}）：{result}")
                            if result.get("item_id") or result.get("url"):
                                return result
                    except Exception:
                        continue  # 该选择器未命中，尝试下一个

            except Exception as e:
                self._log(f"WebView CSS 选择器获取搜索结果失败：{e}")

        # ── 方式 3：Native 方式 ──
        try:
            self.switch_to_native()

            # 查找第一个搜索结果项
            first_item = self.d(
                resourceIdMatches=".*item.*name.*|.*title.*|.*result.*|.*show.*name.*"
            )
            if first_item.exists(timeout=5):
                result["name"] = first_item.get_text() or ""

                # 点击进入详情页获取 ID
                human_click(first_item, config=self._human_cfg)
                human_delay(3.0, config=self._human_cfg)

                # 从当前 URL 获取 ID
                current_url = self.get_current_url()
                item_id = self._extract_item_id_from_url(current_url)
                if item_id:
                    result["item_id"] = item_id
                    result["url"] = current_url

                self._log(f"第一条结果（Native方式）：{result}")
                return result if result.get("name") or result.get("item_id") else None

        except Exception as e:
            self._log(f"Native 获取搜索结果失败：{e}")

        # ── 方式 4：页面源码正则解析（扩展正则） ──
        if self._wd:
            try:
                page_source = self._wd.page_source
                self._log(f"页面源码长度：{len(page_source)}")

                # 多种 ID 提取正则，覆盖大麦各种 URL 格式
                id_patterns = [
                    r'item\.htm\?id=(\d+)',
                    r'/item/(\d+)',
                    r'itemId[=:]\s*["\']?(\d+)',
                    r'showId[=:]\s*["\']?(\d+)',
                    r'/detail/(\d+)',
                ]
                ids = []
                for pat in id_patterns:
                    found = re.findall(pat, page_source)
                    ids.extend(found)
                # 去重保序
                seen = set()
                unique_ids = []
                for i in ids:
                    if i not in seen:
                        seen.add(i)
                        unique_ids.append(i)

                # 提取演出名称
                name_patterns = [
                    r'"itemName"\s*:\s*"([^"]+)"',
                    r'"showName"\s*:\s*"([^"]+)"',
                    r'"title"\s*:\s*"([^"]+)"',
                    r'"name"\s*:\s*"([^"]+)"',
                ]
                names = []
                for pat in name_patterns:
                    found = re.findall(pat, page_source)
                    names.extend(found)

                if unique_ids:
                    result["item_id"] = unique_ids[0]
                    result["name"] = names[0] if names else ""
                    result["url"] = f"https://item.damai.cn/item.htm?id={unique_ids[0]}"
                    self._log(f"通过页面源码提取结果：{result}")
                    return result

                # 如果正则也没找到 ID，尝试从源码中找所有 <a href> 链接
                all_hrefs = re.findall(r'href=["\']([^"\']*(?:item|detail|show)[^"\']*)["\']', page_source)
                if all_hrefs:
                    href = all_hrefs[0]
                    if not href.startswith("http"):
                        href = "https://m.damai.cn" + href if href.startswith("/") else "https://m.damai.cn/" + href
                    result["url"] = href
                    item_id = self._extract_item_id_from_url(href)
                    if item_id:
                        result["item_id"] = item_id
                    self._log(f"通过源码 href 提取结果：{result}")
                    if result.get("item_id") or result.get("url"):
                        return result

            except Exception as e:
                self._log(f"页面源码解析失败：{e}")

        # ── 方式 5：获取页面所有链接的兜底日志 ──
        if self._wd:
            try:
                all_links_js = """
                (function() {
                    var links = document.querySelectorAll('a[href]');
                    var out = [];
                    for (var i = 0; i < Math.min(links.length, 20); i++) {
                        out.push({href: links[i].href, text: (links[i].textContent||'').trim().substring(0, 50)});
                    }
                    return JSON.stringify({total: links.length, sample: out});
                })();
                """
                raw = self._wd.execute_script(all_links_js)
                if raw:
                    info = json.loads(raw)
                    self._warn(
                        f"未能提取演出链接。页面上共有 {info.get('total', '?')} 个 <a> 标签，"
                        f"前 20 个：{json.dumps(info.get('sample', []), ensure_ascii=False)}"
                    )
                else:
                    self._warn("页面上未找到任何 <a> 标签")
            except Exception as e:
                self._log(f"兜底日志获取失败：{e}")

        self._warn("未能获取搜索结果")
        return None

    # ── 完整登录流程 ──────────────────────────────────────────────────
    def login(self, phone: str) -> bool:
        """执行完整的登录流程。

        1. 导航到登录页
        2. 输入手机号
        3. 点击发送验证码
        4. 提示用户输入验证码
        5. 输入验证码并点击登录
        6. 等待登录成功

        Args:
            phone: 手机号码

        Returns:
            是否登录成功
        """
        # Step 1: 导航到登录页
        if not self.navigate_to_login():
            print("❌ 无法到达登录页面")
            return False

        # Step 2: 输入手机号
        if not self.input_phone(phone):
            print("❌ 无法输入手机号")
            return False

        human_delay(1.0, config=self._human_cfg)

        # Step 3: 点击发送验证码
        if not self.click_send_code():
            print("❌ 无法发送验证码")
            return False

        print("✅ 验证码已发送，请查收短信。")

        # Step 4: 提示用户输入验证码
        max_retries = 3
        for i in range(max_retries):
            code = input(
                f"\n🔑 请输入短信验证码（剩余 {max_retries - i} 次机会）: "
            ).strip()
            if not code:
                print("验证码不能为空，请重新输入。")
                continue

            # Step 5: 输入验证码并登录
            if not self.input_verify_code(code):
                print("❌ 无法输入验证码")
                continue

            human_delay(1.0, config=self._human_cfg)

            if not self.click_login():
                print("❌ 无法点击登录按钮")
                continue

            # Step 6: 等待登录成功
            if self.wait_login_success():
                return True

            print("❌ 登录失败，验证码可能不正确。")

        print("❌ 验证码输入次数已用完，登录失败。")
        return False

    # ── 清理 ──────────────────────────────────────────────────────────
    def cleanup(self) -> None:
        """清理资源。"""
        try:
            self.switch_to_native()
        except Exception:
            pass
        self._log("已清理资源")
