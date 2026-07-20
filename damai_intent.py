#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 ADB Intent 直调模块

通过 adb shell am start 直接启动大麦 APP 的目标页面，跳过所有 UI 导航步骤。
相比 OCR/WebView/Native 方案，Intent 直调：
  - 完全跳过 UI 导航（点 tab → 滚动 → 找入口），减少被检测的操作步骤
  - 不依赖 UI 元素树 / OCR / WebView，最稳定
  - 速度最快

支持的操作：
  - open_item_detail()  — 打开演出详情页
  - open_search()       — 打开搜索页
  - open_my_page()      — 打开「我的」页面
  - open_reserve_list() — 打开抢票预约列表（通过 URL scheme）

使用：
  from damai_intent import DamaiIntentHelper

  helper = DamaiIntentHelper(device=d)
  helper.open_item_detail("825173765577")

依赖：
  uiautomator2（设备已连接）
"""

from __future__ import annotations

import random
import re
import sys
import time
from typing import Any, Dict, Optional

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None  # type: ignore


# ─── 常量 ────────────────────────────────────────────────────────────────

DAMAI_PACKAGE = "cn.damai"
DAMAI_ACTIVITY = "cn.damai.homepage.ui.MainActivity"

# 大麦 deep link URL scheme
# 演出详情页（buyParam 格式：itemId_数量）
ITEM_DETAIL_URL = "https://m.damai.cn/app/dmfe/h5-ultron-buy/?buyParam={item_id}_1"
# 演出详情页（item.htm 格式）
ITEM_DETAIL_URL_ALT = "https://m.damai.cn/app/dmfe/h5-ultron-buy/buy?buyParam={item_id}"
# 搜索页
SEARCH_URL = "https://search.damai.cn/search.html"
# 「我的」页面
MY_PAGE_URL = "https://m.damai.cn/app/dmfe/h5-ultron-my/index.html"
# 抢票预约页面
RESERVE_URL = "https://m.damai.cn/app/dmfe/h5-ultron-my/reserve.html"


# ─── Intent 直调类 ────────────────────────────────────────────────────────

class DamaiIntentHelper:
    """通过 ADB Intent 直调大麦 APP 页面。"""

    def __init__(self, device: Any = None, device_serial: Optional[str] = None,
                 verbose: bool = False):
        """
        Args:
            device: u2.Device 对象（优先使用）
            device_serial: 设备序列号（device 为 None 时使用）
            verbose: 是否输出详细日志
        """
        if device is not None:
            self.d = device
        elif device_serial:
            if u2 is None:
                raise ImportError("缺少 uiautomator2")
            self.d = u2.connect(device_serial)
        else:
            if u2 is None:
                raise ImportError("缺少 uiautomator2")
            self.d = u2.connect()
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [intent] {msg}")

    def _adb_shell(self, cmd: str) -> str:
        """执行 adb shell 命令并返回输出。"""
        output = self.d.shell(cmd)[0]
        self._log(f"adb shell {cmd} → {output.strip()[:200]}")
        return output

    def _am_start(self, args: str) -> bool:
        """执行 am start 命令。

        Args:
            args: am start 的参数部分

        Returns:
            是否成功启动
        """
        # 拟人化：Intent 发送前短暂停顿（模拟用户思考/操作间隔）
        time.sleep(random.uniform(0.3, 0.8))

        cmd = f"am start {args}"
        output = self._adb_shell(cmd)

        # 检查启动结果
        success_markers = ["Starting", "Status: ok"]
        error_markers = ["Error", "Exception", "not exist", "Permission Denial"]

        for marker in success_markers:
            if marker in output:
                self._log(f"Intent 启动成功：{args[:100]}")
                return True

        for marker in error_markers:
            if marker in output:
                self._log(f"Intent 启动失败：{output.strip()[:200]}")
                return False

        # 不确定结果，假设成功
        self._log(f"Intent 启动结果不确定：{output.strip()[:200]}")
        return True

    # ── 打开演出详情页 ────────────────────────────────────────────────
    def open_item_detail(self, item_id: str) -> bool:
        """通过 Intent 打开演出详情页。

        尝试多种 URL 格式，优先使用 buyParam 格式。

        Args:
            item_id: 演出 ID

        Returns:
            是否成功打开
        """
        print(f"🔗 [Intent] 打开演出详情页：{item_id}")

        # 方式 1：buyParam 格式（最常用）
        url = ITEM_DETAIL_URL.format(item_id=item_id)
        if self._am_start(f"-a android.intent.action.VIEW -d '{url}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            return True

        # 方式 2：buyParam 格式（备选）
        url = ITEM_DETAIL_URL_ALT.format(item_id=item_id)
        if self._am_start(f"-a android.intent.action.VIEW -d '{url}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            return True

        # 方式 3：item.htm 格式
        url = f"https://m.damai.cn/item.htm?id={item_id}"
        if self._am_start(f"-a android.intent.action.VIEW -d '{url}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            return True

        print("❌ Intent 打开演出详情页失败（所有 URL 格式均失败）")
        return False

    # ── 打开搜索页 ────────────────────────────────────────────────────
    def open_search(self) -> bool:
        """通过 Intent 打开搜索页。

        Returns:
            是否成功打开
        """
        print("🔗 [Intent] 打开搜索页")

        if self._am_start(f"-a android.intent.action.VIEW -d '{SEARCH_URL}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            return True

        print("❌ Intent 打开搜索页失败")
        return False

    # ── 打开「我的」页面 ──────────────────────────────────────────────
    def open_my_page(self) -> bool:
        """通过 Intent 打开「我的」页面。

        先尝试 URL scheme，失败则启动 APP 后通过 u2 点击「我的」tab。

        Returns:
            是否成功打开
        """
        print("🔗 [Intent] 打开「我的」页面")

        # 方式 1：URL scheme
        if self._am_start(f"-a android.intent.action.VIEW -d '{MY_PAGE_URL}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            return True

        # 方式 2：启动 APP + 点击 tab
        self._log("URL scheme 失败，尝试启动 APP + 点击 tab")
        self._am_start(f"-n {DAMAI_PACKAGE}/{DAMAI_ACTIVITY}")
        time.sleep(random.uniform(2.0, 4.0))

        try:
            my_tab = self.d(text="我的")
            if not my_tab.exists(timeout=3):
                my_tab = self.d(textContains="我的")
            if my_tab.exists(timeout=3):
                my_tab.click()
                time.sleep(random.uniform(1.5, 3.0))
                return True
        except Exception as e:
            self._log(f"点击「我的」tab 失败：{e}")

        print("❌ Intent 打开「我的」页面失败")
        return False

    # ── 打开抢票预约页面 ──────────────────────────────────────────────
    def open_reserve_list(self) -> bool:
        """通过 Intent 打开抢票预约页面。

        先尝试 URL scheme，失败则打开「我的」页面后点击入口。

        Returns:
            是否成功打开
        """
        print("🔗 [Intent] 打开抢票预约页面")

        # 方式 1：URL scheme（如果大麦支持）
        if self._am_start(f"-a android.intent.action.VIEW -d '{RESERVE_URL}' {DAMAI_PACKAGE}"):
            time.sleep(random.uniform(1.5, 3.5))
            # 验证是否真的到了预约页面
            try:
                if self.d(textContains="预约").exists(timeout=5):
                    return True
                # 可能跳转到了「我的」页面，需要再点
                reserve_entry = self.d(textContains="抢票预约")
                if reserve_entry.exists(timeout=3):
                    reserve_entry.click()
                    time.sleep(random.uniform(2.0, 4.0))
                    return True
            except Exception:
                pass

        # 方式 2：打开「我的」→ 点击「抢票预约」
        self._log("URL scheme 失败，尝试「我的」→ 点击入口")
        if self.open_my_page():
            try:
                reserve_entry = self.d(textContains="抢票预约")
                if reserve_entry.exists(timeout=5):
                    reserve_entry.click()
                    time.sleep(random.uniform(2.0, 4.0))
                    return True
                # 滚动查找
                for _ in range(5):
                    self.d.swipe(0.5, 0.8, 0.5, 0.2)
                    time.sleep(random.uniform(0.8, 1.5))
                    if reserve_entry.exists(timeout=2):
                        reserve_entry.click()
                        time.sleep(random.uniform(2.0, 4.0))
                        return True
            except Exception as e:
                self._log(f"点击预约入口失败：{e}")

        print("❌ Intent 打开抢票预约页面失败")
        return False

    # ── 检查当前页面 ──────────────────────────────────────────────────
    def get_current_activity(self) -> str:
        """获取当前顶层 Activity 名。"""
        output = self._adb_shell("dumpsys activity activities | grep mResumedActivity")
        # 解析 Activity 名
        m = re.search(r'([a-zA-Z0-9._]+)/([a-zA-Z0-9._]+)', output)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        return ""

    def is_damai_foreground(self) -> bool:
        """检查大麦 APP 是否在前台。"""
        activity = self.get_current_activity()
        return DAMAI_PACKAGE in activity


# ─── 独立测试 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="大麦 ADB Intent 直调测试")
    parser.add_argument("--device", type=str, default=None, help="设备序列号")
    parser.add_argument("--item-id", type=str, default=None, help="演出 ID（用于测试详情页）")
    parser.add_argument("--test", action="store_true", help="运行基本测试")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    helper = DamaiIntentHelper(device_serial=args.device, verbose=args.verbose)

    if args.test:
        print("=== ADB Intent 直调测试 ===\n")

        # 测试 1：检查大麦是否在前台
        print(f"1. 大麦是否在前台：{helper.is_damai_foreground()}")
        print(f"   当前 Activity：{helper.get_current_activity()}")
        print()

        # 测试 2：打开搜索页
        print("2. 打开搜索页…")
        ok = helper.open_search()
        print(f"   结果：{'✅ 成功' if ok else '❌ 失败'}")
        print()

        # 测试 3：打开「我的」页面
        print("3. 打开「我的」页面…")
        ok = helper.open_my_page()
        print(f"   结果：{'✅ 成功' if ok else '❌ 失败'}")
        print()

        # 测试 4：打开演出详情页（如果提供了 item_id）
        if args.item_id:
            print(f"4. 打开演出详情页（{args.item_id}）…")
            ok = helper.open_item_detail(args.item_id)
            print(f"   结果：{'✅ 成功' if ok else '❌ 失败'}")
            print()

    elif args.item_id:
        ok = helper.open_item_detail(args.item_id)
        print(f"结果：{'✅ 成功' if ok else '❌ 失败'}")
    else:
        parser.print_help()
