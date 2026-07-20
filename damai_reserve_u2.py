#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 H5 页面自动化：登录 → 抢票预约 → 查看场次票档（uiautomator2 版）

完整流程：
  1. 通过 uiautomator2 连接 Android 设备，启动大麦 APP
  2. 检测是否已登录 → 已登录则跳过，未登录则：
     a. 勾选同意条款/协议
     b. 输入手机号 + 短信验证码完成登录
  3. 登录后进入「我的」→「抢票预约」
  4. 找到第一条已预约的演出
  5. 进入演出详情，点击底部「已预约」查看场次和票档

使用：
  python damai_reserve_u2.py                                    # 交互式
  python damai_reserve_u2.py --phone 1********5               # 指定手机号
  python damai_reserve_u2.py --device abc123                   # 指定设备序列号
  python damai_reserve_u2.py --skip-login                      # 跳过登录（使用已保存的 cookies）
  python damai_reserve_u2.py --verbose                          # 详细日志

依赖：
  pip install uiautomator2 requests websocket-client
  Android 设备需开启 USB 调试并通过 adb 连接
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from damai_u2 import DamaiU2Automation, DAMAI_PACKAGE

from human_sim import (
    dismiss_dialogs,
    human_browse,
    human_click,
    human_delay,
    human_idle_swipe,
    human_navigate_pause,
    human_scroll,
    human_type_text,
    retry_with_backoff,
)

try:
    from damai_ocr_click import OcrClickHelper
except ImportError:
    OcrClickHelper = None  # type: ignore

try:
    from damai_intent import DamaiIntentHelper
except ImportError:
    DamaiIntentHelper = None  # type: ignore

try:
    from frida_damai_rpc import FridaDamaiRpc
except ImportError:
    FridaDamaiRpc = None  # type: ignore


# ─── 常量 ────────────────────────────────────────────────────────────────

DEFAULT_PHONE = ""  # 留空，必须通过 --phone 参数传入，避免硬编码手机号泄露
DEFAULT_COOKIE_FILE = "damai_cookies_reserve.json"


# ─── 扩展自动化类 ────────────────────────────────────────────────────────

class DamaiReserveAutomation(DamaiU2Automation):
    """大麦网抢票预约自动化（继承 DamaiU2Automation，扩展预约相关操作）。"""

    # ── 登录状态检测 ──────────────────────────────────────────────────
    def check_login_status(self) -> bool:
        """检测当前是否已登录。

        策略：
          1. 切换到 Native 层，点击底部「我的」tab
          2. 检查是否出现用户昵称/订单/退出等已登录标志
          3. 如果检测到已登录标志 → 返回 True
          4. 否则 → 返回 False

        Returns:
            是否已登录
        """
        print("🔍 正在检测登录状态…")

        try:
            # 先回到 Native 层
            self.switch_to_native()
            human_delay(1.0, config=self._human_cfg)

            # 拟人化：先关闭可能遮挡的弹窗（升级提示、广告等）
            dismiss_dialogs(self.d, config=self._human_cfg)

            # 点击底部「我的」tab（多种匹配方式）
            # 拟人化：连续 .exists() 间加微小延迟，避免快速 UI 树扫描特征
            my_tab = self.d(text="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(textContains="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(description="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(resourceIdMatches=".*tab.*mine.*|.*tab.*my.*|.*bottom.*my.*")
            if my_tab.exists(timeout=3):
                human_click(my_tab, config=self._human_cfg)
                self._log("已点击「我的」tab")
                human_delay(2.0, config=self._human_cfg)
            else:
                self._warn("未找到「我的」tab")

            # 检测已登录标志
            # 标志 1：用户昵称（通常在「我的」页面顶部显示）
            nickname = self.d(resourceIdMatches=".*nickname.*|.*user.*name.*|.*nick.*")
            if nickname.exists(timeout=3):
                name_text = nickname.get_text()
                if name_text and name_text not in ("登录/注册", "登录", "注册", ""):
                    print(f"✅ 已登录（昵称：{name_text}）")
                    return True

            # 标志 2：「我的订单」入口
            if self.d(textContains="我的订单").exists(timeout=2):
                print("✅ 已登录（检测到「我的订单」）")
                return True

            # 标志 3：头像区域（已登录时头像可点击进入个人中心）
            avatar = self.d(resourceIdMatches=".*avatar.*|.*user.*icon.*|.*head.*img.*")
            if avatar.exists(timeout=2):
                # 头像存在不代表已登录，还需排除「登录/注册」文字
                login_text = self.d(textContains="登录/注册")
                if not login_text.exists(timeout=1):
                    print("✅ 已登录（检测到头像且无登录入口）")
                    return True

            # 标志 4：通过 WebView 检测 cookie
            if self._wd:
                try:
                    has_login = self._wd.execute_script(
                        """
                        var c = document.cookie;
                        return !!(c && (c.indexOf('cookie2') >= 0 || c.indexOf('sgcookie') >= 0 || c.indexOf('login2') >= 0));
                        """
                    )
                    if has_login:
                        print("✅ 已登录（检测到登录态 cookie）")
                        return True
                except Exception as e:
                    self._log(f"WebView cookie 检测失败：{e}")

            # 未检测到登录标志
            print("⚠️ 未检测到登录状态，需要登录")
            return False

        except Exception as e:
            self._warn(f"登录状态检测异常：{e}")
            return False

    # ── 同意条款/协议 ────────────────────────────────────────────────
    def agree_terms(self) -> bool:
        """检测并勾选同意条款/协议的 checkbox。

        策略：
          1. Native 层：查找未勾选的 CheckBox 或含「同意」文字的勾选框
          2. WebView 层：通过 JS 查找 checkbox 并勾选

        Returns:
            是否成功勾选（或本身已勾选/无需勾选）
        """
        print("📋 正在检测是否需要同意条款/协议…")

        # ── Native 层 ──
        try:
            # 查找含「同意」文字附近的 CheckBox
            agree_text = self.d(textContains="同意")
            if agree_text.exists(timeout=3):
                self._log("找到含「同意」文字的元素")
                # 尝试直接点击（有些是文字本身可点击切换 checkbox）
                # 先查找同级的 CheckBox
                checkbox = self.d(className="android.widget.CheckBox")
                if checkbox.exists(timeout=2):
                    # 检查是否已勾选
                    checked = checkbox.info.get("checked", False)
                    if not checked:
                        human_click(checkbox, config=self._human_cfg)
                        self._log("已勾选同意条款（Native CheckBox）")
                        print("✅ 已勾选同意条款")
                    else:
                        self._log("条款已勾选，无需操作")
                        print("✅ 条款已勾选")
                    return True
                else:
                    # 没有 CheckBox 但有「同意」文字，尝试点击文字
                    human_click(agree_text, config=self._human_cfg)
                    self._log("已点击「同意」文字")
                    print("✅ 已点击同意条款")
                    return True

            # 查找 CheckBox（可能没有文字关联）
            checkbox = self.d(className="android.widget.CheckBox")
            if checkbox.exists(timeout=2):
                checked = checkbox.info.get("checked", False)
                if not checked:
                    human_click(checkbox, config=self._human_cfg)
                    self._log("已勾选 CheckBox（Native）")
                    print("✅ 已勾选同意条款")
                else:
                    self._log("CheckBox 已勾选")
                    print("✅ 条款已勾选")
                return True

        except Exception as e:
            self._log(f"Native 层同意条款检测失败：{e}")

        # ── WebView 层 ──
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    """
                    (function() {
                        // 查找含"同意"文字的 checkbox
                        var labels = document.querySelectorAll('label');
                        for (var i = 0; i < labels.length; i++) {
                            var text = (labels[i].textContent || '').trim();
                            if (text.indexOf('同意') >= 0 || text.indexOf('协议') >= 0 || text.indexOf('条款') >= 0) {
                                var cb = labels[i].querySelector('input[type="checkbox"]');
                                if (cb && !cb.checked) {
                                    cb.click();
                                    return 'checked_via_label';
                                }
                                if (cb && cb.checked) {
                                    return 'already_checked';
                                }
                                // label 本身可能可点击
                                labels[i].click();
                                return 'clicked_label';
                            }
                        }
                        // 查找所有未勾选的 checkbox
                        var checkboxes = document.querySelectorAll('input[type="checkbox"]');
                        for (var j = 0; j < checkboxes.length; j++) {
                            if (!checkboxes[j].checked) {
                                checkboxes[j].click();
                                return 'checked_generic';
                            }
                        }
                        // 查找含"同意"的可点击元素
                        var spans = document.querySelectorAll('span, div, a');
                        for (var k = 0; k < spans.length; k++) {
                            var t = (spans[k].textContent || '').trim();
                            if ((t.indexOf('同意') >= 0 || t.indexOf('阅读并同意') >= 0) && t.length < 50) {
                                spans[k].click();
                                return 'clicked_agree_text';
                            }
                        }
                        return 'no_checkbox_found';
                    })();
                    """
                )
                if ok and ok != "no_checkbox_found":
                    self._log(f"WebView 同意条款结果：{ok}")
                    if ok == "already_checked":
                        print("✅ 条款已勾选")
                    else:
                        print("✅ 已勾选同意条款")
                    return True
                elif ok == "no_checkbox_found":
                    self._log("WebView 未找到条款勾选框，可能不需要勾选")
                    print("ℹ️ 未发现条款勾选框，可能无需勾选")
                    return True  # 无勾选框视为正常
            except Exception as e:
                self._log(f"WebView 同意条款失败：{e}")

        # 没有找到也不报错，可能页面本身不需要
        print("ℹ️ 未发现条款勾选框，继续登录流程")
        return True

    # ── 完整登录流程（含同意条款） ────────────────────────────────────
    def login_with_terms(self, phone: str) -> bool:
        """执行完整的登录流程（含同意条款检测）。

        1. 导航到登录页
        2. 勾选同意条款/协议
        3. 输入手机号
        4. 点击发送验证码
        5. 提示用户输入验证码
        6. 输入验证码并点击登录
        7. 等待登录成功

        Args:
            phone: 手机号码

        Returns:
            是否登录成功
        """
        # Step 1: 导航到登录页
        if not self.navigate_to_login():
            print("❌ 无法到达登录页面")
            return False

        human_navigate_pause(config=self._human_cfg)

        # Step 2: 勾选同意条款
        # 拟人化：先看一眼页面再勾选
        human_delay(0.7, config=self._human_cfg)
        self.agree_terms()

        human_delay(1.0, config=self._human_cfg)

        # Step 3: 输入手机号
        # 拟人化：输入前先停顿一下（模拟看页面）
        human_delay(0.8, config=self._human_cfg)
        if not self.input_phone(phone):
            print("❌ 无法输入手机号")
            return False

        human_delay(1.0, config=self._human_cfg)

        # Step 4: 点击发送验证码
        if not self.click_send_code():
            print("❌ 无法发送验证码")
            return False

        print("✅ 验证码已发送，请查收短信。")

        # 拟人化：发送验证码后做随机行为（模拟用户切出去看短信）
        print("  💡 提示：如出现滑块验证码，请在手机上手动完成验证后继续")
        human_delay(2.0, config=self._human_cfg)
        human_idle_swipe(self.d, config=self._human_cfg)

        # Step 5: 提示用户输入验证码
        max_retries = 5
        for i in range(max_retries):
            # 拟人化：等待输入期间做随机浏览（模拟切出 APP 看短信再回来）
            if i > 0:
                human_browse(self.d, duration=(1.0, 3.0), config=self._human_cfg)

            code = input(
                f"\n🔑 请输入短信验证码（剩余 {max_retries - i} 次机会）: "
            ).strip()
            if not code:
                print("验证码不能为空，请重新输入。")
                continue

            # Step 6: 输入验证码并登录
            if not self.input_verify_code(code):
                print("❌ 无法输入验证码")
                continue

            human_delay(1.0, config=self._human_cfg)

            if not self.click_login():
                print("❌ 无法点击登录按钮")
                continue

            # Step 7: 等待登录成功
            if self.wait_login_success():
                # 拟人化：登录成功后额外等待（模拟用户看到登录成功的反应）
                human_delay(2.0, config=self._human_cfg)
                human_idle_swipe(self.d, config=self._human_cfg)
                return True

            print("❌ 登录失败，验证码可能不正确。")

        print("❌ 验证码输入次数已用完，登录失败。")
        return False

    # ── 导航到抢票预约 ────────────────────────────────────────────────
    def navigate_to_reserve(self) -> bool:
        """导航到「我的」→「抢票预约」页面。

        Returns:
            是否成功到达抢票预约页面
        """
        print("📋 正在导航到「抢票预约」…")

        try:
            self.switch_to_native()
            human_delay(1.0, config=self._human_cfg)

            # 拟人化：先关闭可能遮挡的弹窗
            dismiss_dialogs(self.d, config=self._human_cfg)

            # 确保在「我的」页面（多种匹配方式）
            # 拟人化：连续 .exists() 间加微小延迟
            my_tab = self.d(text="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(textContains="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(description="我的")
            if not my_tab.exists(timeout=3):
                human_delay(0.15, config=self._human_cfg)
                my_tab = self.d(resourceIdMatches=".*tab.*mine.*|.*tab.*my.*|.*bottom.*my.*")
            if my_tab.exists(timeout=3):
                human_click(my_tab, config=self._human_cfg)
                self._log("已点击「我的」tab")
                # 拟人化：在「我的」页面先浏览一下再找入口
                human_browse(self.d, duration=(2.0, 4.0), config=self._human_cfg)

            # 查找「抢票预约」入口
            # 策略 1：精确匹配
            reserve_entry = self.d(text="抢票预约")
            if reserve_entry.exists(timeout=5):
                human_click(reserve_entry, config=self._human_cfg)
                self._log("已点击「抢票预约」")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已进入「抢票预约」页面")
                return True

            # 策略 2：包含匹配
            reserve_entry = self.d(textContains="抢票预约")
            if reserve_entry.exists(timeout=3):
                human_click(reserve_entry, config=self._human_cfg)
                self._log("已点击「抢票预约」（包含匹配）")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已进入「抢票预约」页面")
                return True

            # 策略 3：更宽泛的匹配
            reserve_entry = self.d(textContains="预约")
            if reserve_entry.exists(timeout=3):
                human_click(reserve_entry, config=self._human_cfg)
                self._log("已点击含「预约」的入口")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已进入预约相关页面")
                return True

            # 策略 4：resourceId 匹配
            reserve_entry = self.d(
                resourceIdMatches=".*reserve.*|.*booking.*|.*appointment.*|.*subscribe.*"
            )
            if reserve_entry.exists(timeout=3):
                human_click(reserve_entry, config=self._human_cfg)
                self._log("已点击预约入口（resourceId）")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已进入预约相关页面")
                return True

            # 策略 5：尝试滚动页面查找
            self._log("尝试滚动查找「抢票预约」…")
            for _ in range(5):
                human_scroll(self.d, direction="down", config=self._human_cfg)
                human_delay(1.0, config=self._human_cfg)
                reserve_entry = self.d(textContains="预约")
                if reserve_entry.exists(timeout=2):
                    human_click(reserve_entry, config=self._human_cfg)
                    self._log("滚动后找到预约入口")
                    human_navigate_pause(config=self._human_cfg)
                    print("✅ 已进入预约相关页面")
                    return True

        except Exception as e:
            self._log(f"Native 层导航抢票预约失败：{e}")

        # 策略 6：WebView 方式
        if self.switch_to_webview():
            try:
                ok = self._wd.execute_script(
                    """
                    (function() {
                        // 查找含"预约"的可点击元素
                        var els = document.querySelectorAll('a, button, span, div');
                        for (var i = 0; i < els.length; i++) {
                            var t = (els[i].textContent || '').trim();
                            if ((t.indexOf('抢票预约') >= 0 || t.indexOf('预约') >= 0) && t.length < 20) {
                                els[i].click();
                                return true;
                            }
                        }
                        return false;
                    })();
                    """
                )
                if ok:
                    self._log("WebView 点击预约入口成功")
                    human_navigate_pause(config=self._human_cfg)
                    print("✅ 已进入预约相关页面")
                    return True
            except Exception as e:
                self._log(f"WebView 导航预约失败：{e}")

        print("❌ 无法找到「抢票预约」入口")
        print("提示：")
        print("  1. 确认已登录")
        print("  2. 确认「我的」页面中有「抢票预约」入口")
        print("  3. 可能需要手动点击进入后重试")
        return False

    # ── 获取第一条已预约演出 ──────────────────────────────────────────
    def get_first_reserved_show(self) -> Optional[Dict[str, str]]:
        """获取第一条已预约的演出。

        Returns:
            dict with keys: name, url, item_id; or None
        """
        print("🎭 正在查找第一条已预约演出…")

        result: Dict[str, str] = {}

        # ── Native 层 ──
        try:
            self.switch_to_native()
            human_delay(1.0, config=self._human_cfg)

            # 拟人化：列表页加载后先浏览一下
            human_browse(self.d, duration=(1.0, 3.0), config=self._human_cfg)

            # 查找第一个列表项（演出卡片）
            # 策略 1：通过 resourceId 查找
            first_item = self.d(
                resourceIdMatches=".*item.*name.*|.*show.*name.*|.*title.*|.*card.*title.*"
            )
            if first_item.exists(timeout=5):
                result["name"] = first_item.get_text() or ""
                self._log(f"找到第一条预约演出（Native resourceId）：{result['name']}")

                # 拟人化：点击前短暂停顿
                human_delay(0.8, config=self._human_cfg)
                # 点击进入详情
                human_click(first_item, config=self._human_cfg)
                human_navigate_pause(config=self._human_cfg)
                return result if result.get("name") else result

            # 策略 2：通过 className 查找第一个可点击项
            # 大麦列表通常是 RecyclerView/ListView 中的项
            first_item = self.d(className="android.widget.RelativeLayout")
            if first_item.exists(timeout=3):
                # 尝试获取项中的文字
                child_text = first_item.child(className="android.widget.TextView")
                if child_text.exists(timeout=2):
                    result["name"] = child_text.get_text() or ""
                    self._log(f"找到第一条预约演出（Native TextView）：{result['name']}")

                human_delay(0.8, config=self._human_cfg)
                human_click(first_item, config=self._human_cfg)
                human_navigate_pause(config=self._human_cfg)
                return result

            # 策略 3：直接查找所有 TextView，取第一个看起来像演出名的
            text_views = self.d(className="android.widget.TextView")
            if text_views.exists(timeout=3):
                count = text_views.count
                self._log(f"页面上共 {count} 个 TextView")
                # 遍历前几个，找看起来像演出名的（长度 > 4 且不含常见非演出文字）
                skip_texts = {"抢票预约", "我的", "搜索", "首页", "更多", "返回", "设置"}
                for idx in range(min(count, 15)):
                    try:
                        tv = text_views[idx]
                        txt = tv.get_text() or ""
                        if len(txt) > 4 and txt not in skip_texts:
                            result["name"] = txt
                            self._log(f"疑似演出名：{txt}")
                            human_delay(0.8, config=self._human_cfg)
                            human_click(tv, config=self._human_cfg)
                            human_navigate_pause(config=self._human_cfg)
                            return result
                    except Exception:
                        continue

        except Exception as e:
            self._log(f"Native 获取预约演出失败：{e}")

        # ── WebView 层 ──
        self._ensure_webview_connected()
        if self._wd:
            try:
                # 通过 JS 查找第一个演出卡片/链接
                raw = self._wd.execute_script(
                    """
                    (function() {
                        // 查找演出卡片
                        var selectors = [
                            '.reserve-item a', '.booking-item a',
                            '[class*="reserve"] a', '[class*="booking"] a',
                            '[class*="item"] a', '[class*="card"] a',
                            '.list-item a', 'a[href*="item"]', 'a[href*="detail"]'
                        ];
                        for (var s = 0; s < selectors.length; s++) {
                            var el = document.querySelector(selectors[s]);
                            if (el) {
                                return JSON.stringify({
                                    href: el.href || '',
                                    text: (el.textContent || '').trim().substring(0, 100)
                                });
                            }
                        }
                        // 备用：找所有 a 标签中含演出链接的
                        var links = document.querySelectorAll('a[href]');
                        for (var i = 0; i < links.length; i++) {
                            var href = links[i].href || '';
                            if (/item\\.htm|\\/item\\/|itemId=|\\/detail\\//.test(href)) {
                                return JSON.stringify({
                                    href: href,
                                    text: (links[i].textContent || '').trim().substring(0, 100)
                                });
                            }
                        }
                        return null;
                    })();
                    """
                )
                if raw:
                    info = json.loads(raw)
                    result["name"] = info.get("text", "")
                    result["url"] = info.get("href", "")
                    if result.get("url"):
                        item_id = self._extract_item_id_from_url(result["url"])
                        if item_id:
                            result["item_id"] = item_id
                    self._log(f"WebView 找到第一条预约演出：{result}")

                    # 点击进入详情
                    if result.get("url"):
                        self.navigate_to_url(result["url"])
                        human_navigate_pause(config=self._human_cfg)
                    else:
                        # 通过 JS 点击
                        self._wd.execute_script(
                            """
                            var el = document.querySelector('.reserve-item a, .booking-item a, [class*="item"] a, [class*="card"] a');
                            if (el) { el.click(); }
                            """
                        )
                        human_navigate_pause(config=self._human_cfg)

                    return result

            except Exception as e:
                self._log(f"WebView 获取预约演出失败：{e}")

        print("❌ 未找到已预约的演出")
        return None

    # ── 点击底部「已预约」按钮 ────────────────────────────────────────
    def click_reserved_button(self) -> bool:
        """在演出详情页点击底部「已预约」按钮，查看场次和票档。

        Returns:
            是否成功点击
        """
        print("📌 正在查找「已预约」按钮…")

        # ── Native 层 ──
        try:
            self.switch_to_native()
            human_delay(1.0, config=self._human_cfg)

            # 拟人化：先关闭可能遮挡的弹窗
            dismiss_dialogs(self.d, config=self._human_cfg)

            # 先尝试直接查找
            reserved_btn = self.d(textContains="已预约")
            if reserved_btn.exists(timeout=5):
                # 拟人化：找到按钮后不立即点击，先短暂停顿
                human_delay(0.5, config=self._human_cfg)
                human_click(reserved_btn, config=self._human_cfg)
                self._log("已点击「已预约」按钮")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已点击「已预约」")
                return True

            # 滚动到页面底部查找
            self._log("滚动到页面底部查找…")
            for _ in range(8):
                human_scroll(self.d, direction="down", config=self._human_cfg)
                human_delay(0.5, config=self._human_cfg)
                reserved_btn = self.d(textContains="已预约")
                if reserved_btn.exists(timeout=2):
                    human_delay(0.5, config=self._human_cfg)
                    human_click(reserved_btn, config=self._human_cfg)
                    self._log("滚动后找到并点击「已预约」")
                    human_navigate_pause(config=self._human_cfg)
                    print("✅ 已点击「已预约」")
                    return True

            # 备用：查找含「预约」的按钮
            reserved_btn = self.d(textContains="预约")
            if reserved_btn.exists(timeout=3):
                human_delay(0.5, config=self._human_cfg)
                human_click(reserved_btn, config=self._human_cfg)
                self._log("已点击含「预约」的按钮")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已点击预约相关按钮")
                return True

            # resourceId 匹配
            reserved_btn = self.d(
                resourceIdMatches=".*reserve.*btn.*|.*booking.*btn.*|.*subscribe.*btn.*"
            )
            if reserved_btn.exists(timeout=3):
                human_delay(0.5, config=self._human_cfg)
                human_click(reserved_btn, config=self._human_cfg)
                self._log("已点击预约按钮（resourceId）")
                human_navigate_pause(config=self._human_cfg)
                print("✅ 已点击预约按钮")
                return True

        except Exception as e:
            self._log(f"Native 查找「已预约」按钮失败：{e}")

        # ── WebView 层 ──
        self._ensure_webview_connected()
        if self._wd:
            try:
                ok = self._wd.execute_script(
                    """
                    (function() {
                        // 查找含"已预约"的按钮/链接
                        var els = document.querySelectorAll('button, a, span, div');
                        for (var i = 0; i < els.length; i++) {
                            var t = (els[i].textContent || '').trim();
                            if (t.indexOf('已预约') >= 0 || t === '预约') {
                                els[i].click();
                                return true;
                            }
                        }
                        // 查找底部固定栏中的按钮
                        var footer = document.querySelector('.footer, .bottom-bar, [class*="fixed-bottom"], [class*="action-bar"]');
                        if (footer) {
                            var btns = footer.querySelectorAll('button, a, span');
                            for (var j = 0; j < btns.length; j++) {
                                var t2 = (btns[j].textContent || '').trim();
                                if (t2.indexOf('预约') >= 0) {
                                    btns[j].click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    })();
                    """
                )
                if ok:
                    self._log("WebView 点击「已预约」成功")
                    human_navigate_pause(config=self._human_cfg)
                    print("✅ 已点击「已预约」")
                    return True
            except Exception as e:
                self._log(f"WebView 查找「已预约」按钮失败：{e}")

        print("❌ 未找到「已预约」按钮")
        print("提示：")
        print("  1. 确认已进入演出详情页")
        print("  2. 确认该演出确实已预约")
        print("  3. 可能需要手动滚动到页面底部")
        return False

    # ── OCR 优先：获取第一条已预约演出 ──────────────────────────────
    def get_first_reserved_show_ocr(self) -> Optional[Dict[str, str]]:
        """OCR 方式获取第一条已预约演出（优先方案，不依赖 UI 元素树）。

        通过截图 → OCR 识别文字 → 找到第一个看起来像演出名的文字 → 点击坐标，
        适用于自研渲染引擎/Flutter 等场景。

        Returns:
            dict with keys: name; or None
        """
        if OcrClickHelper is None:
            self._warn("damai_ocr_click 模块不可用，无法使用 OCR 方案")
            print("  安装方法：pip install Pillow paddleocr paddlepaddle")
            return None

        print("🎭 [OCR 优先] 正在通过截图+OCR 查找第一条已预约演出…")

        try:
            helper = OcrClickHelper(device=self.d, verbose=self.verbose)

            # 先浏览一下页面
            human_browse(self.d, duration=(1.0, 2.5), config=self._human_cfg)

            # OCR 提取当前屏所有文字
            all_texts = helper.extract_all_text()
            if not all_texts:
                self._log("OCR 未识别到任何文字")
                return None

            # 找第一个看起来像演出名的文字（长度>4，排除常见非演出文字）
            skip_texts = {"抢票预约", "我的", "搜索", "首页", "更多", "返回", "设置",
                          "登录", "注册", "消息", "推荐", "热门"}
            for item in all_texts:
                text = item.text if hasattr(item, 'text') else str(item)
                if len(text) > 4 and text not in skip_texts:
                    result = {"name": text}
                    self._log(f"OCR 找到疑似演出名：{text}")

                    # 点击该文字进入详情
                    if hasattr(item, 'center_x') and hasattr(item, 'center_y'):
                        human_delay(0.8, config=self._human_cfg)
                        helper._tap(item.center_x, item.center_y)
                        human_navigate_pause(config=self._human_cfg)
                        return result
                    else:
                        # 降级：用 click_text 点击
                        if helper.click_text(text):
                            human_navigate_pause(config=self._human_cfg)
                            return result

            # 当前屏没找到，尝试滑动查找
            self._log("当前屏未找到演出名，尝试滑动查找…")
            for _ in range(5):
                human_scroll(self.d, direction="down", config=self._human_cfg)
                human_delay(1.0, config=self._human_cfg)
                all_texts = helper.extract_all_text()
                for item in all_texts:
                    text = item.text if hasattr(item, 'text') else str(item)
                    if len(text) > 4 and text not in skip_texts:
                        result = {"name": text}
                        self._log(f"OCR 滑动后找到疑似演出名：{text}")
                        if hasattr(item, 'center_x') and hasattr(item, 'center_y'):
                            human_delay(0.8, config=self._human_cfg)
                            helper._tap(item.center_x, item.center_y)
                            human_navigate_pause(config=self._human_cfg)
                            return result

        except Exception as e:
            self._warn(f"OCR 获取预约演出失败：{e}")

        return None

    # ── OCR 优先：点击「已预约」按钮 ─────────────────────────────────
    def click_reserved_button_ocr(self) -> bool:
        """OCR 方式点击底部「已预约」按钮（优先方案，不依赖 UI 元素树）。

        通过截图 → OCR 识别文字 → adb input tap 点击坐标，
        不依赖 UI 元素树，适用于自研渲染引擎/Flutter 等场景。

        Returns:
            是否成功点击
        """
        if OcrClickHelper is None:
            self._warn("damai_ocr_click 模块不可用，无法使用 OCR 方案")
            print("  安装方法：pip install Pillow paddleocr paddlepaddle")
            return False

        print("📌 [OCR 优先] 正在通过截图+OCR 查找「已预约」按钮…")

        try:
            helper = OcrClickHelper(device=self.d, verbose=self.verbose)
            return helper.click_reserved_button()
        except Exception as e:
            self._warn(f"OCR 点击「已预约」失败：{e}")
            return False

    # ── OCR 优先：提取场次和票档信息 ─────────────────────────────────
    def extract_sessions_and_tickets_ocr(self) -> Dict[str, Any]:
        """OCR 方式提取场次和票档信息（优先方案，不依赖 UI 元素树）。

        通过滚动页面 → 逐屏截图 OCR → 按关键词分类，
        不依赖 UI 元素树。

        Returns:
            dict with keys: sessions, tickets, raw_text
        """
        if OcrClickHelper is None:
            self._warn("damai_ocr_click 模块不可用，无法使用 OCR 方案")
            print("  安装方法：pip install Pillow paddleocr paddlepaddle")
            return {"sessions": [], "tickets": [], "raw_text": ""}

        print("📊 [OCR 优先] 正在通过截图+OCR 提取场次和票档信息…")

        try:
            helper = OcrClickHelper(device=self.d, verbose=self.verbose)
            return helper.extract_sessions_and_tickets()
        except Exception as e:
            self._warn(f"OCR 提取场次票档失败：{e}")
            return {"sessions": [], "tickets": [], "raw_text": ""}

    # ── OCR 调试：截图+OCR+标注 ──────────────────────────────────────
    def debug_ocr_dump(self, save_dir: str = ".") -> None:
        """调试：截图 + OCR + 标注保存到文件。

        Args:
            save_dir: 保存目录
        """
        if OcrClickHelper is None:
            self._warn("damai_ocr_click 模块不可用")
            return

        try:
            helper = OcrClickHelper(device=self.d, verbose=self.verbose)
            helper.debug_dump(save_dir=save_dir)
        except Exception as e:
            self._warn(f"OCR 调试转储失败：{e}")

    # ── 提取场次和票档信息 ────────────────────────────────────────────
    def extract_sessions_and_tickets(self) -> Dict[str, Any]:
        """提取场次和票档信息。

        Returns:
            dict with keys:
              sessions: List[Dict] — 场次列表（date, time, venue）
              tickets: List[Dict] — 票档列表（name, price, status）
              raw_text: str — 页面原始文本（兜底）
        """
        print("📊 正在提取场次和票档信息…")

        info: Dict[str, Any] = {
            "sessions": [],
            "tickets": [],
            "raw_text": "",
        }

        # ── WebView 层（优先，信息更结构化） ──
        self._ensure_webview_connected()
        if self._wd:
            try:
                raw = self._wd.execute_script(
                    """
                    (function() {
                        var result = {sessions: [], tickets: [], rawText: ''};

                        // 提取场次信息
                        // 查找含日期/时间的元素
                        var dateEls = document.querySelectorAll(
                            '[class*="session"], [class*="schedule"], [class*="date"], ' +
                            '[class*="perform"], [class*="show-time"], [class*="time"]'
                        );
                        for (var i = 0; i < dateEls.length; i++) {
                            var text = (dateEls[i].textContent || '').trim();
                            if (text && text.length < 100 && /\\d/.test(text)) {
                                result.sessions.push({text: text});
                            }
                        }

                        // 提取票档信息
                        // 查找含价格/票档的元素
                        var priceEls = document.querySelectorAll(
                            '[class*="price"], [class*="ticket"], [class*="sku"], ' +
                            '[class*="seat"], [class*="tier"]'
                        );
                        for (var j = 0; j < priceEls.length; j++) {
                            var text2 = (priceEls[j].textContent || '').trim();
                            if (text2 && text2.length < 100) {
                                result.tickets.push({text: text2});
                            }
                        }

                        // 备用：遍历所有文本节点，按关键词分类
                        if (result.sessions.length === 0 && result.tickets.length === 0) {
                            var allText = [];
                            var walker = document.createTreeWalker(
                                document.body,
                                NodeFilter.SHOW_TEXT,
                                null,
                                false
                            );
                            var node;
                            while (node = walker.nextNode()) {
                                var t = (node.textContent || '').trim();
                                if (t && t.length > 2 && t.length < 100) {
                                    allText.push(t);
                                }
                            }
                            // 去重
                            var seen = {};
                            var unique = [];
                            for (var k = 0; k < allText.length; k++) {
                                if (!seen[allText[k]]) {
                                    seen[allText[k]] = true;
                                    unique.push(allText[k]);
                                }
                            }
                            result.rawText = unique.join('\\n');

                            // 尝试按关键词分类
                            for (var m = 0; m < unique.length; m++) {
                                var line = unique[m];
                                // 场次：含日期格式
                                if (/\\d{4}[.-]\\d{1,2}[.-]\\d{1,2}|\\d{1,2}月\\d{1,2}日|周[一二三四五六日]/.test(line)) {
                                    result.sessions.push({text: line});
                                }
                                // 票档：含价格格式
                                if (/¥|元|价格|票档|\\d+\\.\\d+/.test(line) && /票|价|座|档/.test(line)) {
                                    result.tickets.push({text: line});
                                }
                            }
                        }

                        // 如果还是没提取到，直接获取页面所有文本
                        if (result.sessions.length === 0 && result.tickets.length === 0 && !result.rawText) {
                            result.rawText = (document.body.innerText || '').substring(0, 5000);
                        }

                        return JSON.stringify(result);
                    })();
                    """
                )
                if raw:
                    data = json.loads(raw)
                    info["sessions"] = data.get("sessions", [])
                    info["tickets"] = data.get("tickets", [])
                    info["raw_text"] = data.get("rawText", "")
                    self._log(f"WebView 提取到 {len(info['sessions'])} 个场次, {len(info['tickets'])} 个票档")
            except Exception as e:
                self._log(f"WebView 提取场次票档失败：{e}")

        # ── Native 层（兜底） ──
        if not info["sessions"] and not info["tickets"]:
            try:
                self.switch_to_native()
                human_delay(1.0, config=self._human_cfg)
                # 拟人化：页面加载后先浏览一下
                human_browse(self.d, duration=(2.0, 4.0), config=self._human_cfg)

                # 获取当前页面的所有文本
                all_text = []
                text_views = self.d(className="android.widget.TextView")
                if text_views.exists(timeout=3):
                    count = text_views.count
                    for idx in range(min(count, 100)):
                        try:
                            txt = text_views[idx].get_text() or ""
                            if txt and txt.strip():
                                all_text.append(txt.strip())
                        except Exception:
                            continue

                if all_text:
                    info["raw_text"] = "\n".join(all_text)
                    self._log(f"Native 获取到 {len(all_text)} 个文本节点")

                    # 尝试分类
                    import re
                    for txt in all_text:
                        # 场次
                        if re.search(r'\d{4}[.-]\d{1,2}[.-]\d{1,2}|\d{1,2}月\d{1,2}日|周[一二三四五六日]', txt):
                            info["sessions"].append({"text": txt})
                        # 票档
                        if re.search(r'¥|元|价格|票档', txt) and len(txt) < 100:
                            info["tickets"].append({"text": txt})

            except Exception as e:
                self._log(f"Native 提取场次票档失败：{e}")

        return info

    # ── 格式化输出场次和票档 ──────────────────────────────────────────
    @staticmethod
    def format_sessions_and_tickets(info: Dict[str, Any]) -> str:
        """格式化场次和票档信息为可读字符串。"""
        lines = []
        lines.append(f"{'='*50}")
        lines.append("🎭 场次与票档信息")
        lines.append(f"{'='*50}")

        sessions = info.get("sessions", [])
        tickets = info.get("tickets", [])

        if sessions:
            lines.append(f"\n📅 场次（共 {len(sessions)} 个）：")
            for i, s in enumerate(sessions, 1):
                text = s.get("text", "")
                date = s.get("date", "")
                time_str = s.get("time", "")
                venue = s.get("venue", "")
                if text:
                    lines.append(f"   {i}. {text}")
                else:
                    parts = []
                    if date:
                        parts.append(date)
                    if time_str:
                        parts.append(time_str)
                    if venue:
                        parts.append(f"📍 {venue}")
                    if parts:
                        lines.append(f"   {i}. {' | '.join(parts)}")
        else:
            lines.append(f"\n📅 未提取到场次信息")

        if tickets:
            lines.append(f"\n🎫 票档（共 {len(tickets)} 个）：")
            for i, t in enumerate(tickets, 1):
                text = t.get("text", "")
                name = t.get("name", "")
                price = t.get("price", "")
                status = t.get("status", "")
                if text:
                    lines.append(f"   {i}. {text}")
                else:
                    parts = []
                    if name:
                        parts.append(name)
                    if price:
                        parts.append(f"¥{price}")
                    if status:
                        parts.append(f"[{status}]")
                    if parts:
                        lines.append(f"   {i}. {' | '.join(parts)}")
        else:
            lines.append(f"\n🎫 未提取到票档信息")

        # 如果结构化信息都为空，输出原始文本
        if not sessions and not tickets:
            raw = info.get("raw_text", "")
            if raw:
                lines.append(f"\n📄 页面原始文本（供参考）：")
                # 限制输出长度
                display = raw[:3000] if len(raw) > 3000 else raw
                for line in display.split("\n"):
                    line = line.strip()
                    if line:
                        lines.append(f"   {line}")
                if len(raw) > 3000:
                    lines.append(f"   … (共 {len(raw)} 字符，已截断)")

        return "\n".join(lines)


# ─── Cookie 加载辅助 ────────────────────────────────────────────────────

def try_load_cookies(automation: DamaiReserveAutomation, cookie_file: str) -> bool:
    """尝试从文件加载已保存的 cookies 并验证。

    Returns:
        是否成功加载
    """
    from pathlib import Path
    p = Path(cookie_file)
    if not p.is_file():
        return False

    try:
        with p.open("r", encoding="utf-8") as f:
            cookies = json.load(f)
        if isinstance(cookies, list) and cookies:
            automation._cookies = cookies
            print(f"✅ 已从 {cookie_file} 加载 {len(cookies)} 个 cookies")
            return True
    except Exception:
        pass
    return False


# ─── 主流程 ──────────────────────────────────────────────────────────────

def main() -> None:
    # Windows 控制台 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="大麦网 H5 自动化：登录 → 抢票预约 → 查看场次票档（uiautomator2，OCR 优先）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python damai_reserve_u2.py --phone 15757176315              # OCR 优先模式（默认）\n"
            "  python damai_reserve_u2.py --no-ocr --phone 15757176315     # Native/WebView 优先\n"
            "  python damai_reserve_u2.py --device abc123 --phone 15757176315\n"
            "  python damai_reserve_u2.py --skip-login\n"
            "  python damai_reserve_u2.py --stealth high --phone 15757176315\n"
            "  python damai_reserve_u2.py --verbose --phone 15757176315\n"
            "\n"
            "策略说明:\n"
            "  优先级链（有 root）：Frida RPC → ADB Intent → OCR → Native/WebView → 图像模板\n"
            "  优先级链（无 root）：ADB Intent → OCR → Native/WebView → 图像模板\n"
            "  默认自动检测 root，无 root 时跳过 Frida，优先使用 Intent → OCR\n"
            "  --frida-rpc：Frida RPC 调内部方法，零 UI 交互（需 root + frida-server）\n"
            "  --intent：ADB Intent 直调，跳过 UI 导航（无需 root）\n"
            "  --no-ocr：反转 OCR/Native 优先级\n"
            "  --no-root：跳过 root 检测，直接按无 root 模式运行\n"
            "\n"
            "设备要求:\n"
            "  Frida RPC: 需要 root + frida-server（零 UI 交互，最难被检测）\n"
            "  ADB Intent: 仅需 USB 调试（无需 root，跳过 UI 导航）\n"
            "  OCR: 仅需 USB 调试 + adb screenshot（无需 root，不依赖 UI 元素树）\n"
            "  Native/WebView: 仅需 USB 调试（无需 root，依赖 UI 元素树）\n"
            "\n"
            "设备准备:\n"
            "  1. 手机开启 USB 调试（设置 → 开发者选项 → USB 调试）\n"
            "  2. USB 连接电脑，运行 adb devices 确认设备可见\n"
            "  3. 安装依赖：pip install uiautomator2 requests websocket-client\n"
            "  4. OCR 依赖：pip install Pillow paddleocr paddlepaddle\n"
        ),
    )
    parser.add_argument("--device", type=str, default=None,
                        help="设备序列号（默认自动检测）")
    parser.add_argument("--phone", type=str, default=None,
                        help="手机号（必填，或运行时交互输入）")
    parser.add_argument("--cookie-file", type=str, default=DEFAULT_COOKIE_FILE,
                        help=f"Cookie 保存文件（默认 {DEFAULT_COOKIE_FILE}）")
    parser.add_argument("--skip-login", action="store_true",
                        help="跳过登录（使用已保存的 cookies）")
    parser.add_argument("--frida", action="store_true",
                        help="使用 Frida 注入开启 WebView 调试（需 root + frida-server）")
    parser.add_argument("--frida-rpc", action="store_true",
                        help="使用 Frida RPC 调 APP 内部方法（最高优先级，需 root + frida-server）")
    parser.add_argument("--intent", action="store_true",
                        help="使用 ADB Intent 直调跳转页面（跳过 UI 导航，减少操作痕迹）")
    parser.add_argument("--no-ocr", action="store_true",
                        help="禁用 OCR 优先方案，改用 Native/WebView 优先（OCR 作为降级）")
    parser.add_argument("--no-root", action="store_true",
                        help="声明设备无 root，跳过 root 检测，直接按无 root 模式运行（Intent → OCR → Native/WebView）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出详细日志")
    parser.add_argument("--stealth", type=str, default="medium",
                        choices=["low", "medium", "high"],
                        help="拟人化等级：low(仅延迟抖动) / medium(默认) / high(更多随机行为)")
    args = parser.parse_args()

    phone = args.phone or DEFAULT_PHONE
    if not phone:
        phone = input("📱 请输入手机号: ").strip()
        if not phone:
            print("❌ 手机号不能为空")
            sys.exit(1)
    cookie_file = args.cookie_file
    verbose = args.verbose
    stealth = args.stealth

    # 确定策略标签（先占位，连接设备后再根据 root 状态更新）
    strategy = "(待检测 root 后确定)"

    print("╔════════════════════════════════════════════════╗")
    print("║  大麦网 抢票预约自动化 (u2)                    ║")
    print("║  登录 → 抢票预约 → 场次票档                    ║")
    print(f"║  拟人化等级: {stealth:<33}║")
    print("╚════════════════════════════════════════════════╝")
    print()

    # ── Step 0: 连接设备 ─────────────────────────────────────────────
    automation = DamaiReserveAutomation(
        device_serial=args.device,
        verbose=verbose,
        stealth_level=args.stealth,
    )
    automation.connect_device()

    # ── Step 0.1: 检测 root 状态 ─────────────────────────────────────
    if args.no_root:
        has_root = False
        print("🔓 --no-root 已指定，跳过 root 检测，按无 root 模式运行")
    else:
        has_root = automation.check_root()

    # ── Step 0.2: 根据root状态 + 命令行开关确定策略 ──────────────────
    if args.frida_rpc:
        if has_root:
            strategy = "Frida RPC → Intent → OCR → NW"
        else:
            print("⚠️ --frida-rpc 需要 root，但设备未 root，自动降级到 Intent → OCR → NW")
            args.frida_rpc = False  # 自动降级
            strategy = "Intent → OCR → Native/WebView"
    elif args.intent:
        strategy = "Intent → OCR → Native/WebView"
    elif args.no_ocr:
        strategy = "Native/WebView → OCR"
    else:
        if has_root:
            strategy = "Frida RPC → Intent → OCR → NW"
        else:
            strategy = "Intent → OCR → Native/WebView → Template"

    print(f"\n📋 策略: {strategy}")
    print(f"   Root: {'✅ 已 root' if has_root else '❌ 未 root'}")
    print()

    # ── Step -1: Frida 注入（可选，开启 WebView 调试） ────────────────
    frida_hook = None
    if args.frida and not has_root:
        print("⚠️ --frida 需要 root 权限，但设备未 root，跳过 Frida 注入")
    elif args.frida:
        try:
            from frida_webview_debug import FridaWebViewDebugHook
            print("💉 正在通过 Frida 注入 WebView 调试 hook…")
            frida_hook = FridaWebViewDebugHook(verbose=verbose)
            # attach 模式：APP 可能已启动
            if not frida_hook.attach(DAMAI_PACKAGE):
                print("⚠️ Frida attach 失败，将尝试继续（WebView 可能无法连接）")
            else:
                print("✅ Frida hook 已注入，WebView 调试已开启")
                time.sleep(2)  # 等待 hook 生效
        except ImportError:
            print("⚠️ 未安装 frida，跳过 Frida 注入。")
            print("  安装方法：pip install frida frida-tools")
        except Exception as e:
            print(f"⚠️ Frida 注入失败：{e}")
            print("  将尝试继续，但 WebView 可能无法连接。")

    # ── Step -0.5: Frida RPC 注入（可选，调 APP 内部方法） ──────────
    frida_rpc = None
    if args.frida_rpc and not has_root:
        print("⚠️ Frida RPC 需要 root 权限，但设备未 root，跳过 Frida RPC")
    elif args.frida_rpc:
        if FridaDamaiRpc is None:
            print("⚠️ frida_damai_rpc 模块不可用。pip install frida frida-tools")
        else:
            try:
                print("💉 正在通过 Frida RPC 注入…")
                frida_rpc = FridaDamaiRpc(verbose=verbose)
                if not frida_rpc.attach(DAMAI_PACKAGE):
                    print("⚠️ Frida RPC attach 失败，将降级到其他方案")
                    frida_rpc = None
                else:
                    print("✅ Frida RPC 已注入，可调 APP 内部方法")
            except Exception as e:
                print(f"⚠️ Frida RPC 注入失败：{e}")
                frida_rpc = None

    # ── Step 1: 启动 APP ─────────────────────────────────────────────
    automation.launch_damai_app()

    # 拟人化：启动后到登录之间的间隔
    human_delay(2.0, config=automation._human_cfg)
    human_idle_swipe(automation.d, config=automation._human_cfg)

    # ── Step 2: 登录 ─────────────────────────────────────────────────
    if args.skip_login:
        # 尝试加载已保存的 cookies
        if try_load_cookies(automation, cookie_file):
            print("✅ 跳过登录，使用已保存的 cookies。")
        else:
            print("⚠️ 未找到已保存的 cookies，需要重新登录。")
            args.skip_login = False

    if not args.skip_login:
        # 检测是否已登录
        if automation.check_login_status():
            print("✅ 已登录，跳过登录流程。")
        else:
            # 需要登录
            print(f"📱 手机号: {phone}")
            print()

            if not automation.login_with_terms(phone):
                print("\n❌ 登录失败，无法继续。")
                print("提示：")
                print("  1. 确认手机号正确且能接收短信")
                print("  2. 确认大麦 APP 中登录页面正常显示")
                print("  3. 如触发图形验证码，请在手机上手动完成验证")
                print("  4. 确认设备 WebView 调试已开启")
                automation.cleanup()
                sys.exit(1)

            # 登录成功，提取并保存 cookies
            cookies = automation.get_cookies()
            if cookies:
                # 校验关键登录态 cookie 是否存在
                login_cookie_names = {"cookie2", "sgcookie", "login2", "_m_h5_tk", "_m_h5_tk_enc"}
                has_login_cookie = any(
                    c.get("name") in login_cookie_names
                    for c in cookies
                )
                if not has_login_cookie:
                    print(f"⚠️ 已提取 {len(cookies)} 个 cookies，但未检测到关键登录态（cookie2/sgcookie/login2）")
                    print("  保存可能不完整，下次 --skip-login 可能需要重新登录")
                else:
                    print(f"✅ 登录态 cookie 校验通过（{len(cookies)} 个 cookies）")
                automation.save_cookies(cookie_file)
            else:
                print("⚠️ 未能提取 cookies，将尝试继续…")

    # ── Step 3: 导航到抢票预约 ───────────────────────────────────────
    print()
    # 拟人化：登录后到导航之间的间隔
    human_delay(1.5, config=automation._human_cfg)
    human_idle_swipe(automation.d, config=automation._human_cfg)

    reserve_ok = False

    # 优先级 1：Frida RPC 导航（仅 root 设备且 frida_rpc 已注入时可用）
    if frida_rpc and frida_rpc.is_attached:
        print("🔗 [Frida RPC] 尝试通过内部方法导航到预约页…")
        reserve_ok = frida_rpc.navigate_to("https://m.damai.cn/app/dmfe/h5-ultron-my/reserve.html")
        if reserve_ok:
            human_navigate_pause(config=automation._human_cfg)
        else:
            print("  Frida RPC 导航失败，降级到下一方案")
            # 降级前做拟人化行为（掩盖失败尝试的痕迹，看起来像用户在页面上犹豫）
            human_browse(automation.d, duration=(1.0, 3.0), config=automation._human_cfg)

    # 优先级 2：ADB Intent 直调
    if not reserve_ok and args.intent:
        if DamaiIntentHelper is not None:
            print("🔗 [Intent] 尝试通过 ADB Intent 打开预约页…")
            try:
                intent_helper = DamaiIntentHelper(device=automation.d, verbose=verbose)
                reserve_ok = intent_helper.open_reserve_list()
                if reserve_ok:
                    human_navigate_pause(config=automation._human_cfg)
                else:
                    print("  Intent 导航失败，降级到下一方案")
                    # 降级前做拟人化行为
                    human_browse(automation.d, duration=(1.5, 3.5), config=automation._human_cfg)
            except Exception as e:
                print(f"  Intent 导航异常：{e}")

    # 优先级 3：UI 导航（OCR/Native/WebView）
    if not reserve_ok:
        if not automation.navigate_to_reserve():
            print("\n❌ 无法进入「抢票预约」页面，无法继续。")
            automation.cleanup()
            sys.exit(1)

    # ── Step 4: 找到第一条已预约演出 ─────────────────────────────────
    print()
    # 拟人化：进入预约页后先浏览一下
    human_delay(1.0, config=automation._human_cfg)
    if args.no_ocr:
        # Native/WebView 优先，OCR 降级
        first_show = automation.get_first_reserved_show()
        if not first_show:
            print("\n⚠️ Native/WebView 未找到预约演出，尝试 OCR 方案…")
            first_show = automation.get_first_reserved_show_ocr()
    else:
        # OCR 优先（默认），Native/WebView 降级
        first_show = automation.get_first_reserved_show_ocr()
        if not first_show:
            print("\n⚠️ OCR 未找到预约演出，尝试 Native/WebView 方案…")
            first_show = automation.get_first_reserved_show()
    if not first_show:
        print("\n❌ 未找到已预约的演出。")
        print("提示：")
        print("  1. 确认「抢票预约」中有预约记录")
        print("  2. 确认预约未过期/取消")
        automation.cleanup()
        sys.exit(1)

    show_name = first_show.get("name", "(未知)")
    print(f"\n📌 第一条预约演出：")
    print(f"   🎭 {show_name}")
    if first_show.get("item_id"):
        print(f"   🆔 ID: {first_show['item_id']}")
    if first_show.get("url"):
        print(f"   🔗 {first_show['url']}")

    # ── Step 5: 点击「已预约」查看场次和票档 ─────────────────────────
    print()
    # 拟人化：查看演出详情后到点击预约之间的间隔
    human_delay(1.0, config=automation._human_cfg)
    human_idle_swipe(automation.d, config=automation._human_cfg)
    clicked = False

    if args.no_ocr:
        # Native/WebView 优先，OCR 降级
        clicked = automation.click_reserved_button()
        if not clicked:
            print("\n⚠️ Native/WebView 方式未找到「已预约」按钮，尝试 OCR 方案…")
            clicked = automation.click_reserved_button_ocr()
    else:
        # OCR 优先（默认），Native/WebView 降级
        clicked = automation.click_reserved_button_ocr()
        if not clicked:
            print("\n⚠️ OCR 方式未找到「已预约」按钮，尝试 Native/WebView 方案…")
            clicked = automation.click_reserved_button()

    if not clicked:
        print("\n❌ 无法点击「已预约」按钮。")
        print("将尝试提取当前页面信息…")

    # 提取场次和票档信息
    human_delay(2.0, config=automation._human_cfg)  # 等待页面加载

    if args.no_ocr:
        # Native/WebView 优先，OCR 降级
        info = automation.extract_sessions_and_tickets()
        if not info.get("sessions") and not info.get("tickets"):
            print("\n⚠️ Native/WebView 提取为空，尝试 OCR 方案…")
            ocr_info = automation.extract_sessions_and_tickets_ocr()
            if ocr_info.get("sessions") or ocr_info.get("tickets"):
                info = ocr_info
            elif ocr_info.get("raw_text"):
                if not info.get("raw_text"):
                    info["raw_text"] = ocr_info["raw_text"]
    else:
        # OCR 优先（默认），Native/WebView 降级
        info = automation.extract_sessions_and_tickets_ocr()
        if not info.get("sessions") and not info.get("tickets"):
            print("\n⚠️ OCR 提取为空，尝试 Native/WebView 方案…")
            nw_info = automation.extract_sessions_and_tickets()
            if nw_info.get("sessions") or nw_info.get("tickets"):
                info = nw_info
            elif nw_info.get("raw_text"):
                if not info.get("raw_text"):
                    info["raw_text"] = nw_info["raw_text"]

    # 输出结果
    print()
    print(DamaiReserveAutomation.format_sessions_and_tickets(info))

    # ── 清理 ─────────────────────────────────────────────────────────
    automation.cleanup()
    if frida_rpc and frida_rpc.is_attached:
        frida_rpc.detach()
        print("✅ Frida RPC 已断开")
    if frida_hook and frida_hook.is_attached():
        frida_hook.detach()
        print("✅ Frida hook 已断开")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
