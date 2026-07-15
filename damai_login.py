#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 APP 模拟登录模块

通过 mtop 网关模拟 APP 端短信验证码登录流程：
  1. 初始化 session，获取 _m_h5_tk token
  2. 发送短信验证码（mtop.taobao.h5.mlogin.sendcode）
  3. 验证短信验证码完成登录（mtop.taobao.h5.mlogin.verifycode）
  4. 登录成功后 session 持有登录态 cookies

依赖：requests（pip install requests）
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    sys.stderr.write("缺少依赖 requests，请先执行：pip install requests\n")
    sys.exit(1)


# ─── 常量 ────────────────────────────────────────────────────────────────

APP_KEY = "12574478"
MTOP_HOST = "https://mtop.damai.cn"
SEED_URL = "https://m.damai.cn/"

# APP 端 User-Agent（模拟大麦 Android APP）
APP_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 12; M2102J2SC Build/SKQ1.211006.001) "
    "DamaiApp/8.5.2 (damai;android;12)"
)

# H5 端 User-Agent（备用）
H5_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# ── 登录 API 候选列表 ──
# 大麦/淘宝系 mtop 登录 API 名称可能随版本变化，按优先级尝试
SEND_CODE_APIS: List[Tuple[str, str]] = [
    # (api_name, version)
    ("mtop.taobao.h5.mlogin.sendcode", "1.0"),
    ("mtop.taobao.h5.mlogin.sendVerifyCode", "1.0"),
    ("mtop.damai.wireless.login.sendcode", "1.0"),
    ("mtop.damai.wireless.h5login.sendcode", "1.0"),
]

VERIFY_CODE_APIS: List[Tuple[str, str]] = [
    # (api_name, version)
    ("mtop.taobao.h5.mlogin.verifycode", "1.0"),
    ("mtop.taobao.h5.mlogin.login", "1.0"),
    ("mtop.damai.wireless.login.verifycode", "1.0"),
    ("mtop.damai.wireless.h5login.verifycode", "1.0"),
]

# 用于 seed token 的 API（不需要登录即可调用）
SEED_API = "mtop.damai.item.detail.getdetail"
SEED_VERSION = "2.0"


# ─── 核心登录类 ──────────────────────────────────────────────────────────

class DamaiAppLogin:
    """大麦网 APP 模拟登录（纯 requests，无浏览器依赖）。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": APP_UA,
            "Referer": SEED_URL,
            "Accept": "application/json, text/plain, */*",
            "x-features": "1",
        })
        self._has_login_cookie = False
        self._working_send_api: Optional[Tuple[str, str]] = None
        self._working_verify_api: Optional[Tuple[str, str]] = None

    # ── 日志 ──────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [login] {msg}")

    @staticmethod
    def _warn(msg: str) -> None:
        sys.stderr.write(f"[warn] {msg}\n")

    # ── mtop 签名 ────────────────────────────────────────────────────
    def _sign(self, token: str, t: str, data: str) -> str:
        """mtop 网关签名：MD5(token & t & appKey & data)"""
        return hashlib.md5(
            f"{token}&{t}&{APP_KEY}&{data}".encode("utf-8")
        ).hexdigest()

    def _get_token(self) -> str:
        """从 cookie 中取 _m_h5_tk，取下划线前半部分作为签名 token。"""
        tk = (
            self.session.cookies.get("_m_h5_tk", domain=".damai.cn")
            or self.session.cookies.get("_m_h5_tk")
        )
        if not tk or "_" not in tk:
            return ""
        return tk.split("_")[0]

    def _seed_token(self) -> None:
        """请求 mtop 网关以触发服务端 Set-Cookie 下发 _m_h5_tk。"""
        data_str = json.dumps({"itemId": "1"}, separators=(",", ":"))
        t = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": "",
            "api": SEED_API,
            "v": SEED_VERSION,
            "type": "originaljson",
            "dataType": "json",
            "data": data_str,
        }
        url = f"{MTOP_HOST}/h5/{SEED_API}/{SEED_VERSION}/"
        try:
            self.session.get(url, params=params, timeout=10)
        except requests.RequestException as e:
            self._warn(f"获取 token 失败：{e}")
            return

        if self._get_token():
            self._log("成功获取 _m_h5_tk token")
        else:
            self._warn("未能获取 _m_h5_tk token，部分请求可能失败")

    def _ensure_token(self) -> str:
        """确保拿到 _m_h5_tk cookie，返回签名用 token。"""
        token = self._get_token()
        if token:
            return token
        self._seed_token()
        return self._get_token()

    def _refresh_token(self) -> None:
        """强制清除 token cookie 并重新 seed。"""
        self.session.cookies.clear(domain=".damai.cn")
        self._seed_token()

    # ── 通用 mtop 请求 ──────────────────────────────────────────────
    def _mtop_request(
        self,
        api: str,
        version: str,
        data: Dict[str, Any],
        method: str = "GET",
        extra_params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """通用 mtop 网关请求。

        Args:
            api: mtop API 名称，如 "mtop.taobao.h5.mlogin.sendcode"
            version: API 版本，如 "1.0"
            data: 请求 data 字典
            method: "GET" 或 "POST"
            extra_params: 额外 URL 参数

        Returns:
            mtop 响应 JSON dict
        """
        for attempt in range(2):  # 最多两轮：首轮 + token 失效重试
            token = self._ensure_token()
            if not token:
                return {"ret": ["FAIL_SYS_TOKEN_EMPTY"]}

            t = str(int(time.time() * 1000))
            data_str = json.dumps(data, separators=(",", ":"))
            sign = self._sign(token, t, data_str)

            url = f"{MTOP_HOST}/h5/{api}/{version}/"
            params = {
                "jsv": "2.7.2",
                "appKey": APP_KEY,
                "t": t,
                "sign": sign,
                "api": api,
                "v": version,
                "type": "originaljson",
                "dataType": "json",
                "data": data_str,
            }
            if method == "POST":
                params["method"] = "POST"
            if extra_params:
                params.update(extra_params)

            try:
                if method == "POST":
                    resp = self.session.post(
                        url, params=params, data={"data": data_str}, timeout=15
                    )
                else:
                    resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as e:
                self._warn(f"mtop 请求失败 ({api}): {e}")
                return {"ret": ["FAIL_NET"], "msg": str(e)}

            # token 过期 → 刷新重试
            ret = payload.get("ret", [""])
            if ret and "TOKEN" in str(ret[0]).upper():
                self._log("token 过期，刷新重试…")
                self._refresh_token()
                continue

            return payload

        return {"ret": ["FAIL_SYS_TOKEN_EXHAUSTED"]}

    # ── 发送短信验证码 ──────────────────────────────────────────────
    def send_sms_code(self, mobile: str) -> Dict[str, Any]:
        """发送短信验证码到指定手机号。

        自动在候选 API 列表中探测可用的发送接口。

        Args:
            mobile: 手机号码

        Returns:
            成功时返回 mtop 响应（含 ret=["SUCCESS::调用成功"]），
            全部失败时返回最后一个错误响应。
        """
        data = {"mobile": mobile}
        last_resp: Dict[str, Any] = {"ret": ["FAIL_NET"]}

        # 优先使用已缓存的 API
        if self._working_send_api:
            api, ver = self._working_send_api
            resp = self._mtop_request(api, ver, data, method="POST")
            ret = resp.get("ret", [""])
            if "SUCCESS" in str(ret[0]):
                return resp
            self._log(f"缓存发送 API {api} 失败: {ret}")
            # 不立即放弃，继续遍历候选列表

        for api, ver in SEND_CODE_APIS:
            self._log(f"尝试发送验证码 API: {api}/{ver}")
            resp = self._mtop_request(api, ver, data, method="POST")
            ret = resp.get("ret", [""])
            ret_str = str(ret[0]) if ret else ""

            if "SUCCESS" in ret_str:
                self._working_send_api = (api, ver)
                self._log(f"发送验证码成功，使用 API: {api}")
                return resp

            # API 不存在 → 尝试下一个
            if "API_NOT_EXIST" in ret_str.upper() or "API_NOT_FOUND" in ret_str.upper():
                self._log(f"API 不存在: {api}")
                last_resp = resp
                continue

            # 其他错误（风控、限流等）→ 记录但不继续尝试
            self._log(f"API {api} 返回错误: {ret_str}")
            last_resp = resp

            # 如果不是 API 不存在，可能是可用的 API 但被风控了，缓存它
            if "API_NOT_EXIST" not in ret_str.upper():
                self._working_send_api = (api, ver)
                break

        return last_resp

    # ── 验证短信验证码 ──────────────────────────────────────────────
    def verify_sms_code(self, mobile: str, code: str) -> Dict[str, Any]:
        """验证短信验证码完成登录。

        自动在候选 API 列表中探测可用的验证接口。

        Args:
            mobile: 手机号码
            code: 短信验证码

        Returns:
            成功时返回 mtop 响应，session 中将包含登录态 cookies。
        """
        data = {"mobile": mobile, "code": code}
        last_resp: Dict[str, Any] = {"ret": ["FAIL_NET"]}

        # 优先使用已缓存的 API
        if self._working_verify_api:
            api, ver = self._working_verify_api
            resp = self._mtop_request(api, ver, data, method="POST")
            ret = resp.get("ret", [""])
            if "SUCCESS" in str(ret[0]):
                self._on_login_success(resp)
                return resp

        for api, ver in VERIFY_CODE_APIS:
            self._log(f"尝试验证登录 API: {api}/{ver}")
            resp = self._mtop_request(api, ver, data, method="POST")
            ret = resp.get("ret", [""])
            ret_str = str(ret[0]) if ret else ""

            if "SUCCESS" in ret_str:
                self._working_verify_api = (api, ver)
                self._on_login_success(resp)
                return resp

            if "API_NOT_EXIST" in ret_str.upper() or "API_NOT_FOUND" in ret_str.upper():
                self._log(f"API 不存在: {api}")
                last_resp = resp
                continue

            self._log(f"API {api} 返回错误: {ret_str}")
            last_resp = resp

            if "API_NOT_EXIST" not in ret_str.upper():
                self._working_verify_api = (api, ver)
                break

        return last_resp

    def _on_login_success(self, resp: Dict[str, Any]) -> None:
        """登录成功后的处理：检查 cookies。"""
        self._has_login_cookie = True
        cookie_names = [c.name for c in self.session.cookies]
        self._log(f"登录成功，当前 cookies: {cookie_names}")

        # 检查关键登录态 cookie
        important_cookies = ["cookie2", "sgcookie", "_m_h5_tk", "login2"]
        found = [c for c in important_cookies if c in cookie_names]
        if found:
            self._log(f"关键登录态 cookies: {found}")
        else:
            self._warn("未检测到常见登录态 cookie，登录可能未完全生效")

    # ── 完整登录流程 ────────────────────────────────────────────────
    def login(self, mobile: str) -> bool:
        """执行完整的短信验证码登录流程。

        1. 发送短信验证码
        2. 提示用户输入验证码
        3. 验证登录

        Args:
            mobile: 手机号码

        Returns:
            登录是否成功
        """
        print(f"\n📱 正在向 {mobile} 发送短信验证码…")

        # 发送验证码
        send_resp = self.send_sms_code(mobile)
        ret = send_resp.get("ret", [""])
        ret_str = str(ret[0]) if ret else ""

        if "SUCCESS" not in ret_str:
            # 输出详细错误信息
            data = send_resp.get("data", {}) or {}
            error_msg = data.get("errorMsg", "") or data.get("msg", "")
            print(f"❌ 发送验证码失败: {ret_str}")
            if error_msg:
                print(f"   错误详情: {error_msg}")

            # 检查是否需要图形验证码
            if "验证" in error_msg or "check" in ret_str.lower() or "RISK" in ret_str.upper():
                print("\n⚠️  可能触发了图形验证码/滑块验证。")
                print("   请尝试以下方式：")
                print("   1. 在浏览器中登录 m.damai.cn，完成验证后复制 Cookie")
                print("   2. 稍后重试（验证码触发有冷却时间）")

            return False

        print("✅ 验证码已发送，请查收短信。")

        # 提示用户输入验证码
        max_retries = 3
        for i in range(max_retries):
            code = input(f"\n🔑 请输入短信验证码（剩余 {max_retries - i} 次机会）: ").strip()
            if not code:
                print("验证码不能为空，请重新输入。")
                continue

            # 验证登录
            verify_resp = self.verify_sms_code(mobile, code)
            v_ret = verify_resp.get("ret", [""])
            v_ret_str = str(v_ret[0]) if v_ret else ""

            if "SUCCESS" in v_ret_str:
                print("✅ 登录成功！")
                return True

            v_data = verify_resp.get("data", {}) or {}
            v_error = v_data.get("errorMsg", "") or v_data.get("msg", "")
            print(f"❌ 验证失败: {v_ret_str}")
            if v_error:
                print(f"   错误详情: {v_error}")

            if "验证码" in v_error and "错误" in v_error:
                print("   验证码不正确，请重新输入。")
            elif "过期" in v_error or "失效" in v_error:
                print("   验证码已过期，需要重新发送。")
                # 重新发送
                send_resp = self.send_sms_code(mobile)
                s_ret = send_resp.get("ret", [""])
                if "SUCCESS" in str(s_ret[0]):
                    print("✅ 验证码已重新发送。")
                else:
                    print("❌ 重新发送失败，请稍后重试。")
                    return False

        print("❌ 验证码输入次数已用完，登录失败。")
        return False

    # ── Cookie 持久化 ───────────────────────────────────────────────
    def save_cookies(self, path: str = "damai_cookies.json") -> None:
        """将当前 session cookies 保存到文件。"""
        cookies = []
        for c in self.session.cookies:
            cookies.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
            })
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self._log(f"Cookies 已保存到 {path}（{len(cookies)} 个）")
        except Exception as e:
            self._warn(f"保存 cookies 失败: {e}")

    def load_cookies(self, path: str = "damai_cookies.json") -> bool:
        """从文件加载 cookies 到 session。

        Returns:
            是否成功加载了 cookies
        """
        p = Path(path)
        if not p.is_file():
            return False
        try:
            with p.open("r", encoding="utf-8") as f:
                cookies = json.load(f)
            if not isinstance(cookies, list):
                return False
            count = 0
            for c in cookies:
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ".damai.cn"),
                    path=c.get("path", "/"),
                )
                count += 1
            if count > 0:
                self._has_login_cookie = True
                self._log(f"已从 {path} 加载 {count} 个 cookies")
                return True
        except Exception as e:
            self._warn(f"加载 cookies 失败: {e}")
        return False

    def get_cookie_string(self) -> str:
        """获取当前 session 的 cookie 字符串（用于注入 DamaiMonitor 等）。"""
        parts = []
        for c in self.session.cookies:
            parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)

    @property
    def is_logged_in(self) -> bool:
        """检查是否已登录（通过关键 cookie 判断）。"""
        cookie_names = {c.name for c in self.session.cookies}
        # 有 cookie2 或 sgcookie 或 login2 即视为已登录
        return bool(cookie_names & {"cookie2", "sgcookie", "login2"}) or self._has_login_cookie


# ─── 便捷函数 ────────────────────────────────────────────────────────────

def quick_login(mobile: str, verbose: bool = False) -> DamaiAppLogin:
    """快捷登录：创建登录实例并执行登录流程。

    Args:
        mobile: 手机号码
        verbose: 是否输出详细日志

    Returns:
        登录成功的 DamaiAppLogin 实例
    """
    login = DamaiAppLogin(verbose=verbose)
    login.login(mobile)
    return login
