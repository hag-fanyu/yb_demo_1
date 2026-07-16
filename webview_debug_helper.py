#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
非 root 设备 WebView 调试方案

在无 root 的设备上，无法通过 Frida/Xposed 强制开启 WebView 调试。
但可以通过以下方式绕过：

方案 A：使用 Chrome DevTools 远程调试（需 USB 连接）
  - 手机开启「USB 调试」+「WebView 调试」
  - 电脑 Chrome 打开 chrome://inspect
  - 直接在 Chrome 中调试 WebView

方案 B：使用 adb + chrome devtools protocol 直接连接
  - 通过 adb forward 将 devtools 端口映射到本地
  - 关键：需要 APP 自身开启 WebView 调试

方案 C（本脚本核心）：利用 Android Backup + 重打包 开启调试
  - 不现实，太复杂

方案 D（推荐）：使用「WebViewDebugProxy」APP
  - 安装一个开启调试的 WebView Provider
  - 在开发者选项中选择该 Provider
  - 无需 root

方案 E（本脚本实现）：通过 uiautomator2 + Chrome DevTools Protocol
  - 利用 u2 的 app_wait + adb forward 直接探测
  - 不依赖 Frida，纯 adb 方式

关键发现：
  Android 10+ 的「WebView 实现」选项只是选择 WebView 渲染引擎，
  并不自动开启调试端口。调试端口需要 APP 代码显式调用
  WebView.setWebContentsDebuggingEnabled(true)。

  但有一个例外：如果设备上安装了 Chrome 或 Chromium，
  Chrome 自身的 DevTools 端口可以通过以下方式暴露：
    adb forward tcp:9222 localabstract:chrome_devtools_remote

本脚本：
  1. 检测设备上所有 devtools 套接字
  2. 尝试所有可能的 adb forward 方式
  3. 如果都失败，提供手动操作指引

使用：
  python webview_debug_helper.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Optional, List, Dict


# ─── 配置 ────────────────────────────────────────────────────────────────────

ADB_PATH = r"D:\platform-tools\adb.exe"
DAMAI_PACKAGE = "cn.damai"


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def adb(cmd: str, timeout: int = 10) -> str:
    """执行 adb 命令并返回输出。"""
    try:
        r = subprocess.run(
            [ADB_PATH, "shell", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def adb_forward(local: str, remote: str) -> bool:
    """设置 adb forward。"""
    try:
        r = subprocess.run(
            [ADB_PATH, "forward", local, remote],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def adb_forward_remove(local: str) -> bool:
    """移除 adb forward。"""
    try:
        subprocess.run(
            [ADB_PATH, "forward", "--remove", local],
            capture_output=True, text=True, timeout=5
        )
        return True
    except Exception:
        return False


def test_devtools_port(port: int) -> Optional[dict]:
    """测试本地端口是否是有效的 DevTools 端口。"""
    try:
        import requests
        resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def test_devtools_pages(port: int) -> Optional[List[dict]]:
    """获取 DevTools 可调试页面列表。"""
    try:
        import requests
        resp = requests.get(f"http://127.0.0.1:{port}/json", timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ─── 诊断函数 ────────────────────────────────────────────────────────────────

def check_device_info() -> Dict[str, str]:
    """获取设备基本信息。"""
    info = {}
    info["model"] = adb("getprop ro.product.model")
    info["android"] = adb("getprop ro.build.version.release")
    info["sdk"] = adb("getprop ro.build.version.sdk")
    info["build_type"] = adb("getprop ro.build.type")
    info["webview_provider"] = adb("settings get global webview_provider 2>/dev/null || getprop persist.sys.webview.provider")
    info["has_root"] = "yes" if adb("id") != "ERROR" and "root" in adb("id") else "no"
    return info


def scan_devtools_sockets() -> List[str]:
    """扫描设备上所有 devtools 相关的 Unix 抽象套接字。"""
    sockets = []

    # 方法 1: /proc/net/unix
    output = adb("cat /proc/net/unix 2>/dev/null")
    if output and "ERROR" not in output:
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 6:
                sock_path = parts[-1]
                # 抽象套接字在 /proc/net/unix 中 @ 显示为空格或 @
                if any(kw in sock_path.lower() for kw in
                       ["devtools_remote", "webview_devtools", "chrome_devtools"]):
                    # 还原 @ 前缀
                    if sock_path.startswith(" "):
                        sock_path = "@" + sock_path[1:]
                    sockets.append(sock_path)

    # 方法 2: grep 精简搜索
    for pattern in ["webview_devtools", "chrome_devtools", "devtools_remote"]:
        output2 = adb(f"cat /proc/net/unix 2>/dev/null | grep -i '{pattern}'")
        if output2 and "ERROR" not in output2:
            for line in output2.strip().splitlines():
                parts = line.split()
                if parts:
                    sock_path = parts[-1]
                    if sock_path.startswith(" "):
                        sock_path = "@" + sock_path[1:]
                    if sock_path not in sockets:
                        sockets.append(sock_path)

    # 去重
    return list(dict.fromkeys(sockets))


def scan_webview_processes() -> List[Dict[str, str]]:
    """查找 WebView 相关进程。"""
    processes = []
    for proc_name in ["webview", "chrome", "damai"]:
        output = adb(f"ps -A 2>/dev/null | grep -i '{proc_name}'")
        if output and "ERROR" not in output:
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = parts[1] if parts[1].isdigit() else parts[0]
                        name = parts[-1] if not parts[-1].isdigit() else ""
                        processes.append({"pid": pid, "name": name, "raw": line.strip()})
                    except Exception:
                        pass
    return processes


def try_connect_devtools() -> Optional[int]:
    """尝试所有可能的方式连接 DevTools，返回可用端口。"""
    import requests

    # 清理旧的 forward
    for port in range(9222, 9230):
        adb_forward_remove(f"tcp:{port}")

    # Step 1: 通过抽象套接字转发
    sockets = scan_devtools_sockets()
    if sockets:
        print(f"\n  找到 {len(sockets)} 个 devtools 套接字：")
        for s in sockets:
            print(f"    {s}")

        local_port = 9222
        for sock in sockets:
            abstract_name = sock.lstrip("@").lstrip()
            if not abstract_name:
                continue
            print(f"\n  尝试: adb forward tcp:{local_port} localabstract:{abstract_name}")
            if adb_forward(f"tcp:{local_port}", f"localabstract:{abstract_name}"):
                time.sleep(0.5)
                result = test_devtools_port(local_port)
                if result:
                    print(f"  >>> 成功! DevTools 在端口 {local_port}")
                    print(f"  >>> {result}")
                    return local_port
                else:
                    print(f"  端口无响应，移除 forward")
                    adb_forward_remove(f"tcp:{local_port}")
            local_port += 1

    # Step 2: 通过进程 PID 构造套接字名
    processes = scan_webview_processes()
    if processes:
        print(f"\n  找到 WebView 相关进程：")
        for p in processes:
            print(f"    pid={p['pid']}  name={p['name']}")

        for proc in processes:
            pid = proc["pid"]
            for prefix in ["webview_devtools_remote_", "chrome_devtools_remote_", "devtools_remote_"]:
                abstract_name = f"{prefix}{pid}"
                print(f"\n  尝试: adb forward tcp:9222 localabstract:{abstract_name}")
                adb_forward_remove("tcp:9222")
                if adb_forward("tcp:9222", f"localabstract:{abstract_name}"):
                    time.sleep(0.5)
                    result = test_devtools_port(9222)
                    if result:
                        print(f"  >>> 成功! DevTools 在端口 9222")
                        return 9222
                    else:
                        adb_forward_remove("tcp:9222")

    # Step 3: TCP 直连（某些 ROM）
    for port in [9222, 9229, 9223]:
        print(f"\n  尝试: adb forward tcp:{port} tcp:{port}")
        if adb_forward(f"tcp:{port}", f"tcp:{port}"):
            time.sleep(0.5)
            result = test_devtools_port(port)
            if result:
                print(f"  >>> 成功! DevTools 在端口 {port}")
                return port
            else:
                adb_forward_remove(f"tcp:{port}")

    return None


# ─── 主函数 ──────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("  WebView DevTools 诊断工具（非 root 设备）")
    print("=" * 60)

    # Step 1: 设备信息
    print("\n[1] 设备信息")
    info = check_device_info()
    for k, v in info.items():
        print(f"  {k}: {v}")

    # Step 2: 检查大麦 APP
    print("\n[2] 大麦 APP 进程")
    damai_procs = adb(f"ps -A 2>/dev/null | grep {DAMAI_PACKAGE}")
    if damai_procs and "ERROR" not in damai_procs:
        print(f"  {damai_procs}")
    else:
        print("  未运行")

    # Step 3: 扫描 devtools 套接字
    print("\n[3] DevTools 套接字扫描")
    sockets = scan_devtools_sockets()
    if sockets:
        print(f"  找到 {len(sockets)} 个：")
        for s in sockets:
            print(f"    {s}")
    else:
        print("  未找到任何 devtools 套接字!")
        print()
        print("  >>> 这是 _wd is None 的根本原因 <<<")
        print()
        print("  原因：大麦 APP 未调用 WebView.setWebContentsDebuggingEnabled(true)")
        print("  因此 WebView 不会暴露 DevTools 端口")
        print()

    # Step 4: WebView 进程
    print("\n[4] WebView 相关进程")
    procs = scan_webview_processes()
    if procs:
        for p in procs:
            print(f"  pid={p['pid']}  name={p['name']}")
    else:
        print("  未找到")

    # Step 5: 尝试连接
    print("\n[5] 尝试连接 DevTools")
    port = try_connect_devtools()
    if port:
        print(f"\n  DevTools 可用！端口: {port}")
        pages = test_devtools_pages(port)
        if pages:
            print(f"  可调试页面 ({len(pages)} 个)：")
            for p in pages:
                print(f"    - {p.get('title', '?')} | {p.get('url', '?')}")
        print()
        print("  现在可以运行: python damai_reserve_u2.py --verbose")
    else:
        print("\n  所有方式均失败！")
        print()
        print("=" * 60)
        print("  解决方案（按推荐顺序）：")
        print("=" * 60)
        print()
        print("  方案 1 [推荐]：安装 Chrome 浏览器 + USB 调试")
        print("  ─────────────────────────────────────────────")
        print("    1. 在手机上安装 Chrome 浏览器（从应用商店）")
        print("    2. 开启 USB 调试（已开启）")
        print("    3. 在电脑 Chrome 打开 chrome://inspect")
        print("    4. 应该能看到大麦 APP 的 WebView 页面")
        print("    5. 点击 inspect 即可调试")
        print()
        print("  方案 2：使用 adb + Chrome 远程调试")
        print("  ─────────────────────────────────────────────")
        print("    1. 手机安装 Chrome 后，Chrome 会自动开启 DevTools")
        print("    2. 运行以下命令：")
        print(f"       {ADB_PATH} forward tcp:9222 localabstract:chrome_devtools_remote")
        print("    3. 浏览器打开 http://127.0.0.1:9222/json")
        print()
        print("  方案 3：安装 WebView Debug Provider APP")
        print("  ─────────────────────────────────────────────")
        print("    1. 下载 Crosswalk 或其他带调试的 WebView 实现")
        print("    2. 安装后在开发者选项中选择该 WebView 实现")
        print()
        print("  方案 4 [需 root]：Frida/Xposed 强制开启")
        print("  ─────────────────────────────────────────────")
        print("    1. 需要 Magisk/Xposed/Frida + root 权限")
        print("    2. 当前设备未 root，此方案不可用")
        print()
        print("  方案 5 [备选]：纯 Native 层操作")
        print("  ─────────────────────────────────────────────")
        print("    1. 不使用 WebView，完全通过 uiautomator2 Native 层操作")
        print("    2. 修改脚本，跳过所有 WebView 相关步骤")
        print("    3. 功能可能受限，但不需要调试端口")
        print()


if __name__ == "__main__":
    main()
