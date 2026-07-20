#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 Frida RPC 调内部方法模块

通过 Frida 注入后，用 rpc.exports 暴露 APP 内部 Java 方法给 Python 调用。
可以直接读取页面数据、调用内部导航方法，完全绕过 UI 交互。

优势：
  - 完全绕过 UI，零交互痕迹
  - 可读取 APP 内部状态（是否已登录、当前页面等）
  - 最难被风控检测（没有 UI 操作）

劣势：
  - 需要 root + frida-server
  - 需要逆向找到方法签名（每个 APP 版本可能不同）
  - Frida 本身可能被检测

使用：
  from frida_damai_rpc import FridaDamaiRpc

  rpc = FridaDamaiRpc()
  rpc.attach("cn.damai")

  # 读取内部状态
  logged_in = rpc.is_logged_in()
  url = rpc.get_current_url()

  # 导航到指定页面
  rpc.navigate_to("https://m.damai.cn/item.htm?id=825173765577")

  # 获取预约列表
  reserves = rpc.get_reserve_list()

依赖：
  pip install frida frida-tools
  手机需运行 frida-server（版本与本地 frida 一致）
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

try:
    import frida
except ImportError:
    frida = None  # type: ignore


# ─── Frida JS 脚本（RPC exports）────────────────────────────────────────

FRIDA_RPC_JS = r"""
"use strict";

rpc.exports = {
    // ── 检查是否已登录 ────────────────────────────────────────
    isLoggedIn: function () {
        var result = false;
        Java.perform(function () {
            try {
                // 方式 1：读取 SharedPreferences 中的登录态
                var ActivityThread = Java.use("android.app.ActivityThread");
                var app = ActivityThread.currentApplication();
                var ctx = app.getApplicationContext();
                var sp = ctx.getSharedPreferences("damai_prefs", 0);  // 常见 SP 名
                var token = sp.getString("cookie2", "");
                if (token && token.length > 10) {
                    result = true;
                    return;
                }
            } catch (e) { }

            try {
                // 方式 2：尝试其他常见 SP 名
                var ActivityThread = Java.use("android.app.ActivityThread");
                var app = ActivityThread.currentApplication();
                var ctx = app.getApplicationContext();

                var spNames = ["user_info", "login_info", "account", "auth", "session"];
                for (var i = 0; i < spNames.length; i++) {
                    try {
                        var sp = ctx.getSharedPreferences(spNames[i], 0);
                        var all = sp.getAll();
                        var keys = all.keySet().toArray();
                        for (var j = 0; j < keys.length; j++) {
                            var val = String(sp.getString(keys[j], ""));
                            if (val.indexOf("cookie2") >= 0 || val.indexOf("sgcookie") >= 0 || val.indexOf("login2") >= 0) {
                                result = true;
                                return;
                            }
                        }
                    } catch (e) { }
                }
            } catch (e) { }

            try {
                // 方式 3：检查 WebView cookie
                var CookieManager = Java.use("android.webkit.CookieManager");
                var cm = CookieManager.getInstance();
                var cookie = cm.getCookie(".damai.cn");
                if (cookie && (cookie.indexOf("cookie2") >= 0 || cookie.indexOf("sgcookie") >= 0)) {
                    result = true;
                    return;
                }
            } catch (e) { }
        });
        return result;
    },

    // ── 获取当前 WebView URL ──────────────────────────────────
    getCurrentUrl: function () {
        var url = "";
        Java.perform(function () {
            try {
                Java.choose("android.webkit.WebView", {
                    onMatch: function (instance) {
                        try {
                            var u = instance.getUrl();
                            if (u && u.length > 0) {
                                url = u;
                            }
                        } catch (e) { }
                    },
                    onComplete: function () { }
                });
            } catch (e) { }
        });
        return url;
    },

    // ── 导航到指定 URL ────────────────────────────────────────
    navigateTo: function (targetUrl) {
        var success = false;
        Java.perform(function () {
            try {
                Java.choose("android.webkit.WebView", {
                    onMatch: function (instance) {
                        try {
                            instance.loadUrl(targetUrl);
                            success = true;
                        } catch (e) { }
                    },
                    onComplete: function () { }
                });
            } catch (e) { }

            // 如果 WebView 方式失败，尝试通过 Intent
            if (!success) {
                try {
                    var Intent = Java.use("android.content.Intent");
                    var ActivityThread = Java.use("android.app.ActivityThread");
                    var app = ActivityThread.currentApplication();
                    var ctx = app.getApplicationContext();

                    var intent = Intent.$new();
                    intent.setAction("android.intent.action.VIEW");
                    intent.setData(android.net.Uri.parse(targetUrl));
                    intent.setPackage("cn.damai");
                    intent.addFlags(0x10000000);  // FLAG_ACTIVITY_NEW_TASK
                    ctx.startActivity(intent);
                    success = true;
                } catch (e) { }
            }
        });
        return success;
    },

    // ── 获取预约列表数据 ──────────────────────────────────────
    getReserveList: function () {
        var result = [];
        Java.perform(function () {
            try {
                // 尝试从 WebView 中提取预约数据
                Java.choose("android.webkit.WebView", {
                    onMatch: function (instance) {
                        try {
                            // 通过 JS 提取页面中的预约信息
                            instance.evaluateJavascript(
                                "(function(){" +
                                "  var items = [];" +
                                "  var els = document.querySelectorAll('[class*=reserve] a, [class*=booking] a, [class*=item] a');" +
                                "  for (var i = 0; i < els.length; i++) {" +
                                "    items.push({name: (els[i].textContent||'').trim().substring(0,100), href: els[i].href||''});" +
                                "  }" +
                                "  return JSON.stringify(items);" +
                                "})()",
                                null  // callback (同步模式下忽略)
                            );
                        } catch (e) { }
                    },
                    onComplete: function () { }
                });
            } catch (e) { }
        });
        return JSON.stringify(result);
    },

    // ── 读取 SharedPreferences ────────────────────────────────
    readSharedPreferences: function (spName) {
        var result = {};
        Java.perform(function () {
            try {
                var ActivityThread = Java.use("android.app.ActivityThread");
                var app = ActivityThread.currentApplication();
                var ctx = app.getApplicationContext();
                var sp = ctx.getSharedPreferences(spName, 0);
                var all = sp.getAll();
                var keys = all.keySet().toArray();
                for (var i = 0; i < keys.length; i++) {
                    try {
                        result[keys[i]] = String(sp.getString(keys[i], ""));
                    } catch (e) {
                        try { result[keys[i]] = String(sp.getInt(keys[i], 0)); } catch (e2) { }
                    }
                }
            } catch (e) { }
        });
        return JSON.stringify(result);
    },

    // ── 列出所有 SharedPreferences 文件 ────────────────────────
    listSharedPreferences: function () {
        var result = [];
        Java.perform(function () {
            try {
                var ActivityThread = Java.use("android.app.ActivityThread");
                var app = ActivityThread.currentApplication();
                var ctx = app.getApplicationContext();
                var prefsDir = ctx.getSharedPrefsFile("").getParentFile();
                if (prefsDir) {
                    var files = prefsDir.listFiles();
                    if (files) {
                        for (var i = 0; i < files.length; i++) {
                            result.push(files[i].getName());
                        }
                    }
                }
            } catch (e) { }
        });
        return JSON.stringify(result);
    }
};
"""


# ─── Frida RPC 封装类 ────────────────────────────────────────────────────

class FridaDamaiRpc:
    """通过 Frida RPC 调用大麦 APP 内部方法。"""

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: 是否输出详细日志
        """
        if frida is None:
            raise ImportError("缺少 frida，请执行：pip install frida frida-tools")
        self._verbose = verbose
        self._session = None
        self._script = None
        self._api = None

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"  [frida-rpc] {msg}")

    def attach(self, package: str = "cn.damai") -> bool:
        """Attach 到已运行的大麦 APP 并注入 RPC 脚本。

        Args:
            package: APP 包名

        Returns:
            是否成功注入
        """
        try:
            device = frida.get_usb_device()
            self._session = device.attach(package)
            self._script = self._session.create_script(FRIDA_RPC_JS)

            # 注册消息处理器
            self._script.on("message", self._on_message)

            # 加载脚本
            self._script.load()

            # 获取 RPC exports
            self._api = self._script.exports

            self._log(f"已 attach 到 {package}，RPC 脚本已加载")
            return True

        except Exception as e:
            self._log(f"attach 失败：{e}")
            return False

    def spawn(self, package: str = "cn.damai") -> bool:
        """Spawn 模式注入（APP 启动前注入）。

        Args:
            package: APP 包名

        Returns:
            是否成功注入
        """
        try:
            device = frida.get_usb_device()
            pid = device.spawn([package])
            self._session = device.attach(pid)
            self._script = self._session.create_script(FRIDA_RPC_JS)
            self._script.on("message", self._on_message)
            self._script.load()
            self._api = self._script.exports
            device.resume(pid)
            self._log(f"已 spawn {package}（pid={pid}），RPC 脚本已加载")
            return True
        except Exception as e:
            self._log(f"spawn 失败：{e}")
            return False

    def _on_message(self, message: Dict, data: Any) -> None:
        """Frida 消息回调。"""
        if message.get("type") == "send":
            self._log(f"JS: {message.get('payload', '')}")
        elif message.get("type") == "error":
            self._log(f"JS Error: {message.get('description', '')}")

    @property
    def is_attached(self) -> bool:
        """检查是否已 attach。"""
        return self._session is not None and self._api is not None

    # ── RPC 方法封装 ────────────────────────────────────────────────

    def is_logged_in(self) -> bool:
        """检查 APP 内部是否已登录。

        通过读取 SharedPreferences 和 WebView Cookie 判断。

        Returns:
            是否已登录
        """
        if not self.is_attached:
            return False
        try:
            return bool(self._api.is_logged_in())
        except Exception as e:
            self._log(f"is_logged_in 失败：{e}")
            return False

    def get_current_url(self) -> str:
        """获取当前 WebView 的 URL。

        Returns:
            当前 URL，或空字符串
        """
        if not self.is_attached:
            return ""
        try:
            return str(self._api.get_current_url())
        except Exception as e:
            self._log(f"get_current_url 失败：{e}")
            return ""

    def navigate_to(self, url: str) -> bool:
        """通过 Frida 导航到指定 URL。

        优先通过 WebView.loadUrl()，失败则通过 Intent。

        Args:
            url: 目标 URL

        Returns:
            是否成功导航
        """
        if not self.is_attached:
            return False
        try:
            return bool(self._api.navigate_to(url))
        except Exception as e:
            self._log(f"navigate_to 失败：{e}")
            return False

    def get_reserve_list(self) -> List[Dict[str, str]]:
        """获取预约列表数据。

        Returns:
            预约列表，每项含 name 和 href
        """
        if not self.is_attached:
            return []
        try:
            raw = self._api.get_reserve_list()
            if raw:
                return json.loads(raw)
            return []
        except Exception as e:
            self._log(f"get_reserve_list 失败：{e}")
            return []

    def read_shared_preferences(self, sp_name: str) -> Dict[str, str]:
        """读取指定的 SharedPreferences。

        Args:
            sp_name: SP 文件名

        Returns:
            key-value 字典
        """
        if not self.is_attached:
            return {}
        try:
            raw = self._api.read_shared_preferences(sp_name)
            if raw:
                return json.loads(raw)
            return {}
        except Exception as e:
            self._log(f"read_shared_preferences 失败：{e}")
            return {}

    def list_shared_preferences(self) -> List[str]:
        """列出所有 SharedPreferences 文件名。

        Returns:
            SP 文件名列表
        """
        if not self.is_attached:
            return []
        try:
            raw = self._api.list_shared_preferences()
            if raw:
                return json.loads(raw)
            return []
        except Exception as e:
            self._log(f"list_shared_preferences 失败：{e}")
            return []

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
        self._api = None
        self._log("已 detach")
