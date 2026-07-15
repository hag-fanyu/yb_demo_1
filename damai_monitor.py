#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网余票实时监控工具

功能：
  - 通过大麦 mtop 网关接口查询演出/项目的实时上架/开售状态
  - 自动获取并刷新 `_m_h5_tk` token，自动签名
  - 支持多 API 端点回退链（v2.0 → v1.2 → wireless），自动选择可用端点
  - 支持登录 cookie 注入（配置文件 / --cookie / 环境变量），绕过风控拦截
  - 智能检测风控拦截并给出操作指引
  - 按设定间隔轮询，状态变化时在控制台高亮提示（可选系统通知）
  - 展示票档价格区间、购买按钮状态（有票/缺货/即将开售/已售罄）

使用：
  python damai_monitor.py                          # 使用默认配置文件 damai_config.json
  python damai_monitor.py --config my_config.json  # 指定配置文件
  python damai_monitor.py 825173765577             # 命令行覆盖配置文件中的 item_id
  python damai_monitor.py --cookie "cookie2=xxx"   # 命令行覆盖 cookie

配置文件（damai_config.json）：
  {
    "item_id": "1061170881710",
    "cookie": "cookie2=xxx; sgcookie=yyy",
    "interval": 3,
    "notify": false,
    "verbose": false
  }

  字段均可省略，省略时使用默认值。命令行参数优先级高于配置文件。

如何获取演出ID：
  打开大麦演出详情页，URL 形如 https://item.damai.cn/item.htm?id=825173765577
  其中 id= 后面的数字即为本工具需要的 <演出ID>。

如何获取登录 cookie：
  1. 浏览器登录大麦网 (damai.cn)
  2. 打开 F12 开发者工具 → Network → 任意请求 → Headers
  3. 复制 Cookie 请求头的完整值
  4. 填入配置文件的 cookie 字段，或通过 --cookie 参数传入

注意：
  本工具仅用于"查看"余票/开售状态，方便个人及时了解放票情况，不做任何自动下单、
  抢票、绕过风控等操作。大麦网对自动化访问有风控（滑块、行为校验等），频繁请求可能
  被限流或要求验证码，请合理设置轮询间隔（建议 >= 3 秒），并仅用于合法合规目的。

依赖：requests（pip install requests）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

try:
    import requests
except ImportError:
    sys.stderr.write("缺少依赖 requests，请先执行：pip install requests\n")
    sys.exit(1)


# ─── 常量 ────────────────────────────────────────────────────────────────

APP_KEY = "12574478"            # 大麦 H5 端通用 appKey
MTOP_HOST = "https://mtop.damai.cn"
SEED_URL = "https://m.damai.cn/"   # 用于初次访问获取 _m_h5_tk cookie
DEFAULT_CONFIG_FILE = "damai_config.json"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 大麦购买按钮/项目状态语义映射
BUY_BTN_TEXT: Dict[str, str] = {
    "1": "立即购买",
    "2": "即将开抢",
    "3": "即将开抢",
    "4": "缺货登记",
    "5": "缺货登记",
    "9": "立即购买",
    "10": "选座购买",
    "12": "预售",
    "13": "提交失败",
}
AVAILABLE_CODES: frozenset = frozenset({"1", "9", "10"})


# ─── API 端点回退链 ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ApiEndpoint:
    """mtop API 端点描述"""
    api: str       # e.g. "mtop.damai.item.detail.getdetail"
    version: str   # e.g. "2.0"
    host: str      # e.g. "https://mtop.damai.cn"
    label: str     # e.g. "getdetail-v2.0"（日志用）


API_FALLBACK_CHAIN: List[ApiEndpoint] = [
    ApiEndpoint(
        api="mtop.damai.item.detail.getdetail",
        version="2.0",
        host=MTOP_HOST,
        label="getdetail-v2.0",
    ),
    ApiEndpoint(
        api="mtop.damai.item.detail.getdetail",
        version="1.2",
        host=MTOP_HOST,
        label="getdetail-v1.2",
    ),
    ApiEndpoint(
        api="mtop.damai.wireless.item.detail.get",
        version="1.0",
        host=MTOP_HOST,
        label="wireless-v1.0",
    ),
]


# ─── 数据结构 ────────────────────────────────────────────────────────────

@dataclass
class MonitorConfig:
    """监控配置（来自配置文件 + CLI + 环境变量的合并结果）"""
    item_id: str = ""
    cookie: str = ""
    interval: float = 3.0
    notify: bool = False
    verbose: bool = False


@dataclass
class TicketStatus:
    """单次查询解析出的可读状态"""
    item_id: str
    raw_ok: bool = False                       # 接口是否成功返回
    name: str = ""                             # 演出名称
    perform_id: str = ""                       # 场次
    buy_btn: str = ""                          # 原始按钮状态码
    buy_btn_text: str = ""                     # 按钮文案
    available: bool = False                    # 是否处于"可购买"状态
    price_list: list = field(default_factory=list)   # 票档价格信息
    venue: str = ""                            # 场馆名称
    show_time: str = ""                        # 演出时间
    error_msg: str = ""                        # 查询失败时的可读错误
    endpoint_label: str = ""                   # 使用的 API 端点
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskControlResult:
    """风控检测结果"""
    hit: bool                                  # 是否被风控拦截
    kind: str                                  # "none" | "no_login" | "rate_limited" | "item_not_found" | "generic_block"
    message: str                               # 中文操作指引
    should_try_next: bool                      # 是否值得尝试下一个端点


# ─── 配置加载 ────────────────────────────────────────────────────────────

def load_config_file(path: str) -> Dict[str, Any]:
    """从 JSON 配置文件加载配置，文件不存在或格式错误时返回空 dict。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            sys.stderr.write(f"[warn] 配置文件 {path} 顶层不是 JSON 对象，已忽略\n")
            return {}
        return data
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[warn] 配置文件 {path} JSON 格式错误：{e}，已忽略\n")
        return {}
    except Exception as e:
        sys.stderr.write(f"[warn] 无法读取配置文件 {path}：{e}，已忽略\n")
        return {}


def merge_config(
    file_cfg: Dict[str, Any],
    cli_item_id: Optional[str],
    cli_cookie: Optional[str],
    cli_interval: Optional[float],
    cli_notify: Optional[bool],
    cli_verbose: Optional[bool],
) -> MonitorConfig:
    """合并配置：CLI 参数 > 环境变量 > 配置文件 > 默认值。

    CLI 参数中，只有用户显式传入的才覆盖配置文件（None 表示未传入）。
    """
    # 环境变量
    env_cookie = os.environ.get("DAMAI_COOKIE", "")

    # item_id: CLI > 配置文件
    item_id = cli_item_id or str(file_cfg.get("item_id", "") or "")

    # cookie: CLI > 环境变量 > 配置文件
    cookie = cli_cookie or env_cookie or str(file_cfg.get("cookie", "") or "")

    # interval: CLI > 配置文件 > 默认 3.0
    interval = cli_interval if cli_interval is not None else float(file_cfg.get("interval", 3.0))

    # notify: CLI > 配置文件 > 默认 False
    notify = cli_notify if cli_notify is not None else bool(file_cfg.get("notify", False))

    # verbose: CLI > 配置文件 > 默认 False
    verbose = cli_verbose if cli_verbose is not None else bool(file_cfg.get("verbose", False))

    return MonitorConfig(
        item_id=item_id,
        cookie=cookie,
        interval=interval,
        notify=notify,
        verbose=verbose,
    )


# ─── 风控检测 ────────────────────────────────────────────────────────────

def detect_risk_control(
    payload: Dict[str, Any],
    has_login_cookie: bool = False,
) -> RiskControlResult:
    """分析 mtop 响应，判断是否被风控拦截，返回分类结果和操作指引。"""
    ret = payload.get("ret", [""])
    ret_str = str(ret[0]) if ret else ""

    # 成功
    if "SUCCESS" in ret_str:
        data = payload.get("data", {}) or {}
        # SUCCESS 但 data 只有 errorMsg → 风控拦截
        if "errorMsg" in data and len([k for k in data if k != "errorMsg"]) == 0:
            error_msg = data.get("errorMsg", "")
            return _classify_error_msg(error_msg, has_login_cookie)
        return RiskControlResult(hit=False, kind="none", message="", should_try_next=False)

    # Token 相关 — 不是风控，不需要换端点
    if "TOKEN" in ret_str.upper():
        return RiskControlResult(
            hit=False, kind="token_expired",
            message="token 过期，将自动刷新重试",
            should_try_next=False,
        )

    # 网络失败 — 不是风控
    if "FAIL_NET" in ret_str:
        return RiskControlResult(
            hit=False, kind="network_error",
            message="网络请求失败",
            should_try_next=False,
        )

    # API 版本不存在 — 继续尝试下一个端点（不同 API/版本可能存在）
    if "API_NOT_EXIST" in ret_str.upper() or "API_NOT_FOUND" in ret_str.upper() or "API_NOT_FOUNDED" in ret_str.upper():
        return RiskControlResult(
            hit=True, kind="api_not_found",
            message=f"API 版本不存在：{ret_str}",
            should_try_next=True,
        )

    # 限流 / 频率控制
    if "RISK" in ret_str.upper() or "FREQ" in ret_str.upper():
        return RiskControlResult(
            hit=True, kind="rate_limited",
            message="被限流或风控拦截，请增大轮询间隔。",
            should_try_next=True,
        )

    # data 中的 errorMsg
    data = payload.get("data", {}) or {}
    error_msg = data.get("errorMsg", "")
    if error_msg:
        return _classify_error_msg(error_msg, has_login_cookie)

    # 未知错误
    return RiskControlResult(
        hit=True, kind="generic_block",
        message=f"未知错误：{ret_str}",
        should_try_next=True,
    )


def _classify_error_msg(
    error_msg: str, has_login_cookie: bool,
) -> RiskControlResult:
    """根据 errorMsg 内容分类风控类型。"""
    if "小二很忙" in error_msg or "稍后再试" in error_msg:
        if not has_login_cookie:
            return RiskControlResult(
                hit=True, kind="no_login",
                message="风控拦截（未登录）：请在配置文件中设置 cookie，或通过 --cookie / DAMAI_COOKIE 提供。\n"
                        "  获取方法：浏览器登录大麦 → F12 → Network → 任意请求 → 复制 Cookie 头",
                should_try_next=True,
            )
        return RiskControlResult(
            hit=True, kind="rate_limited",
            message="风控拦截（已登录但仍被限流）：请增大轮询间隔，或稍后重试。",
            should_try_next=True,
        )
    if "不存在" in error_msg or "已下架" in error_msg:
        return RiskControlResult(
            hit=True, kind="item_not_found",
            message=f"演出不存在或已下架：{error_msg}",
            should_try_next=False,
        )
    return RiskControlResult(
        hit=True, kind="generic_block",
        message=f"接口返回错误：{error_msg}",
        should_try_next=True,
    )


# ─── 响应解析 ────────────────────────────────────────────────────────────

def parse_response(
    item_id: str,
    payload: Dict[str, Any],
    endpoint_label: str = "",
) -> TicketStatus:
    """将 mtop 原始响应解析为 TicketStatus。

    兼容多种 API 响应结构：
      Schema A (getdetail): data.itemInfoResult.itemInfo / data.staticData.itemInfo
      Schema B (较新):      data.buyButton / data.item / data.price / data.venue
      Schema C (wireless):  data.resultData.itemInfo / data.resultData.skuList
    """
    st = TicketStatus(item_id=item_id, raw=payload, endpoint_label=endpoint_label)
    ret = payload.get("ret", [])
    if not ret or "SUCCESS" not in str(ret[0]):
        st.error_msg = ret[0] if ret else "UNKNOWN"
        return st

    data = payload.get("data", {}) or {}

    # SUCCESS 但 data 只有 errorMsg → 风控拦截，视为失败
    if "errorMsg" in data and len([k for k in data if k != "errorMsg"]) == 0:
        st.error_msg = data.get("errorMsg", "")
        return st

    st.raw_ok = True

    # ── Schema B: 较新的扁平结构 ──
    if "buyButton" in data or ("item" in data and isinstance(data.get("item"), dict)):
        _parse_schema_b(st, data)
    else:
        # ── Schema A / C: 嵌套结构 ──
        _parse_schema_a(st, data)

    return st


def _parse_schema_a(st: TicketStatus, data: Dict) -> None:
    """解析 getdetail API 的嵌套结构（v1.2 / v2.0 / wireless）。"""
    # itemInfo 路径：itemInfoResult → staticData → resultData
    item_info = (data.get("itemInfoResult") or {}).get("itemInfo") or {}
    static = (data.get("staticData") or {}).get("itemInfo") or {}
    result_data = data.get("resultData") or {}
    result_info = result_data.get("itemInfo") or {}

    info = item_info or static or result_info or data.get("itemInfo") or {}

    st.name = info.get("itemName") or info.get("name") or ""
    st.perform_id = str(info.get("performId") or "")
    st.buy_btn = str(info.get("buyBtn") or info.get("buybtn") or "")
    st.buy_btn_text = BUY_BTN_TEXT.get(st.buy_btn, info.get("buyBtnText", ""))
    st.available = st.buy_btn in AVAILABLE_CODES
    st.venue = info.get("venueName") or info.get("venue") or ""
    st.show_time = str(info.get("showTime") or info.get("showDateTime") or "")

    # SKU / 价格列表
    skus = (data.get("skuResult") or {}).get("skuList") or \
          info.get("skuList") or \
          result_data.get("skuList") or []
    _parse_skus(st, skus)


def _parse_schema_b(st: TicketStatus, data: Dict) -> None:
    """解析较新的扁平结构（buyButton / item / price / venue 顶层键）。"""
    # buyButton
    btn = data.get("buyButton") or {}
    if isinstance(btn, dict):
        st.buy_btn = str(btn.get("btnStatusCode") or btn.get("status") or "")
        st.buy_btn_text = btn.get("btnText") or BUY_BTN_TEXT.get(st.buy_btn, "")
    else:
        st.buy_btn = str(btn)
        st.buy_btn_text = BUY_BTN_TEXT.get(st.buy_btn, "")
    st.available = st.buy_btn in AVAILABLE_CODES

    # item
    item = data.get("item") or {}
    if isinstance(item, dict):
        st.name = item.get("itemName") or item.get("name") or ""
        st.perform_id = str(item.get("performId") or "")
        st.show_time = str(item.get("showTime") or item.get("showDateTime") or "")

    # price
    price_data = data.get("price") or {}
    if isinstance(price_data, dict):
        skus = price_data.get("skuList") or price_data.get("list") or []
    elif isinstance(price_data, list):
        skus = price_data
    else:
        skus = []
    _parse_skus(st, skus)

    # venue
    venue = data.get("venue") or {}
    if isinstance(venue, dict):
        st.venue = venue.get("venueName") or venue.get("name") or ""


def _parse_skus(st: TicketStatus, skus: list) -> None:
    """解析 SKU 价格列表。"""
    for sku in skus:
        if not isinstance(sku, dict):
            continue
        price = sku.get("price") or sku.get("discountPrice")
        if price:
            st.price_list.append({
                "name": sku.get("skuName") or sku.get("priceName") or "",
                "price": price,
                "status": sku.get("statusDesc") or sku.get("skuStatusDesc") or "",
            })


# ─── 核心监控类 ──────────────────────────────────────────────────────────

class DamaiMonitor:
    def __init__(self, config: MonitorConfig):
        self.item_id = str(config.item_id).strip()
        self.interval = max(config.interval, 1.0)
        self.notify = config.notify
        self.verbose = config.verbose
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": SEED_URL,
            "Accept": "application/json, text/plain, */*",
        })

        # 注入登录 cookie
        self._has_login_cookie = False
        if config.cookie:
            self._inject_cookies(config.cookie)

        # API 端点缓存：记录上次成功的端点，避免每次遍历
        self._working_endpoint: Optional[ApiEndpoint] = None

    # ── Cookie 注入 ──────────────────────────────────────────────────
    def _inject_cookies(self, cookie_str: str) -> None:
        """解析 cookie 字符串并注入 session。

        支持格式：
          - "key1=val1; key2=val2"  （浏览器 DevTools 复制格式）
          - "key1=val1;key2=val2"   （无空格）
        """
        count = 0
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            key, val = key.strip(), val.strip()
            if not key:
                continue
            self.session.cookies.set(key, val, domain=".damai.cn")
            count += 1
        if count > 0:
            self._has_login_cookie = True
            # 如果注入了 _m_h5_tk，无需再 seed
            if self._get_token():
                self._info(f"已注入 {count} 个 cookie（含 _m_h5_tk，跳过 token 获取）")
            else:
                self._info(f"已注入 {count} 个 cookie")

    # ── mtop 签名 ────────────────────────────────────────────────────
    def _sign(self, token: str, t: str, data: str) -> str:
        return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data}".encode("utf-8")).hexdigest()

    def _get_token(self) -> str:
        """从 cookie 取 _m_h5_tk，取下划线前半部分作为签名 token"""
        tk = self.session.cookies.get("_m_h5_tk", domain=".damai.cn") \
            or self.session.cookies.get("_m_h5_tk")
        if not tk or "_" not in tk:
            return ""
        return tk.split("_")[0]

    def _seed_token(self) -> None:
        """请求一次 mtop 接口以触发服务端 Set-Cookie 下发 _m_h5_tk。

        m.damai.cn 首页不会下发该 cookie；必须由 mtop 网关在响应头里下发。
        第一次请求会返回 FAIL_SYS_TOKEN_EMPTY，但同时 Set-Cookie，下次即可签名。
        遍历回退链，用第一个能成功下发 _m_h5_tk 的端点做 seed。
        """
        data_str = json.dumps({"itemId": self.item_id}, separators=(",", ":"))
        for ep in API_FALLBACK_CHAIN:
            t = str(int(time.time() * 1000))
            params = {
                "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": "",
                "api": ep.api, "v": ep.version,
                "type": "originaljson", "dataType": "json",
                "data": data_str,
            }
            url = f"{ep.host}/h5/{ep.api}/{ep.version}/"
            try:
                self.session.get(url, params=params, timeout=10)
            except requests.RequestException as e:
                self._warn(f"端点 {ep.label} 获取 token 失败：{e}")
                continue
            # 检查是否成功拿到了 _m_h5_tk
            if self._get_token():
                return
        self._warn("所有端点均未能获取 _m_h5_tk token")

    def _ensure_token(self) -> str:
        """确保拿到 _m_h5_tk cookie，返回签名用 token"""
        token = self._get_token()
        if token:
            return token
        self._seed_token()
        return self._get_token()

    def _refresh_token(self) -> None:
        """强制清除 token cookie 并重新 seed。"""
        self.session.cookies.clear(domain=".damai.cn")
        self._seed_token()

    # ── 单端点请求 ───────────────────────────────────────────────────
    def _build_request_params(
        self, endpoint: ApiEndpoint, token: str, t: str, data: str, sign: str,
    ) -> tuple:
        """构造指定端点的请求 URL 和参数。"""
        url = f"{endpoint.host}/h5/{endpoint.api}/{endpoint.version}/"
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": sign,
            "api": endpoint.api,
            "v": endpoint.version,
            "type": "originaljson",
            "dataType": "json",
            "data": data,
        }
        return url, params

    def _query_single(self, endpoint: ApiEndpoint) -> Dict[str, Any]:
        """对单个端点发起请求，含 token 过期自动重试。"""
        last_payload: Dict[str, Any] = {"ret": ["FAIL_NET"]}
        for _ in range(2):  # 最多两轮：首轮请求，二轮在 token 失效后重试
            token = self._ensure_token()
            if not token:
                return {"ret": ["FAIL_SYS_TOKEN_EMPTY"]}

            t = str(int(time.time() * 1000))
            data = json.dumps({"itemId": self.item_id}, separators=(",", ":"))
            sign = self._sign(token, t, data)

            url, params = self._build_request_params(endpoint, token, t, data, sign)
            try:
                resp = self.session.get(url, params=params, timeout=10)
                resp.raise_for_status()
                last_payload = resp.json()
            except (requests.RequestException, ValueError) as e:
                return {"ret": ["FAIL_NET"], "msg": str(e)}

            ret = last_payload.get("ret", [""])
            if ret and "TOKEN" in str(ret[0]).upper():
                continue
            return last_payload
        return last_payload

    # ── 多端点回退查询 ───────────────────────────────────────────────
    def query(self) -> Dict[str, Any]:
        """查询一次，自动在 API 端点回退链中选择可用端点。

        策略：
          1. 有缓存端点 → 先用它请求
          2. 缓存端点被风控 → 直接返回（不再遍历，避免无谓请求）
          3. 首次查询 → 遍历回退链，缓存最接近成功的端点
        """
        # 快速路径：尝试缓存端点
        if self._working_endpoint:
            payload = self._query_single(self._working_endpoint)
            rc = detect_risk_control(payload, self._has_login_cookie)
            if not rc.hit:
                return payload
            # 缓存端点被风控，直接返回（不再遍历链，避免无谓请求）
            if self.verbose:
                self._warn(f"缓存端点 {self._working_endpoint.label} 被风控拦截")
            return payload

        # 遍历回退链
        last_payload: Dict[str, Any] = {"ret": ["FAIL_NET"]}
        best_payload: Optional[Dict[str, Any]] = None  # 最接近成功的响应
        best_endpoint: Optional[ApiEndpoint] = None     # 最接近成功的端点
        for endpoint in API_FALLBACK_CHAIN:
            payload = self._query_single(endpoint)
            rc = detect_risk_control(payload, self._has_login_cookie)
            if not rc.hit:
                self._working_endpoint = endpoint
                if self.verbose:
                    self._info(f"使用端点: {endpoint.label}")
                return payload
            if self.verbose:
                self._warn(f"端点 {endpoint.label} 返回风控: {rc.kind}")
            last_payload = payload
            # 记录 API 存在但被风控的端点（优先级高于 api_not_found）
            if rc.kind in ("no_login", "rate_limited"):
                best_endpoint = endpoint
                best_payload = payload
            if not rc.should_try_next:
                break

        # 缓存最接近成功的端点，下次直接用它（风控可能已解除）
        if best_endpoint:
            self._working_endpoint = best_endpoint

        # 返回最接近成功的响应（而非最后一个端点的响应）
        return best_payload or last_payload

    # ── 输出 ─────────────────────────────────────────────────────────
    @staticmethod
    def _warn(msg: str) -> None:
        sys.stderr.write(f"[warn] {msg}\n")

    @staticmethod
    def _info(msg: str) -> None:
        print(f"  [info] {msg}")

    def _format(self, st: TicketStatus) -> str:
        ts = time.strftime("%H:%M:%S")
        if not st.raw_ok:
            data = st.raw.get("data", {}) or {}
            err = data.get("errorMsg", "") or st.error_msg
            ret = st.raw.get("ret", ["?"])
            detail = f" {err}" if err else f" ret={ret[0] if ret else '?'}"
            ep = f" [{st.endpoint_label}]" if st.endpoint_label else ""
            return f"[{ts}] ⚠️ 查询失败{detail}{ep}"

        flag = "✅ 有票" if st.available else "❌ 暂无"
        ep = f" [{st.endpoint_label}]" if (self.verbose and st.endpoint_label) else ""
        lines = [f"[{ts}] {flag} | {st.name or '(未取到名称)'} "
                 f"| btn={st.buy_btn}({st.buy_btn_text}){ep}"]

        if st.venue:
            lines.append(f"        📍 {st.venue}")
        if st.show_time:
            lines.append(f"        🕐 {st.show_time}")

        for p in st.price_list[:8]:
            lines.append(f"        ¥{p['price']} {p['name']} {p['status']}")

        if self.verbose and st.raw:
            lines.append(f"        [raw keys] {list(st.raw.get('data', {}).keys())}")
        return "\n".join(lines)

    def _maybe_notify(self, st: TicketStatus, prev: Optional[TicketStatus]) -> None:
        if not self.notify:
            return
        became_avail = st.available and (prev is None or not prev.available)
        if not became_avail:
            return
        msg = f"大麦余票提醒：{st.name or self.item_id} 现在可购买！"
        try:
            if sys.platform.startswith("win"):
                # Windows 通知：优先尝试 win10toast，fallback 到 PowerShell
                try:
                    from win10toast import ToastNotifier
                    ToastNotifier().show_toast("大麦余票", msg, duration=5)
                except ImportError:
                    os.system(
                        f'powershell -NoProfile -Command '
                        f'New-BurntToastNotification -Text "大麦余票", "{msg}"'
                    )
            elif sys.platform == "darwin":
                os.system(f'osascript -e \'display notification "{msg}" with title "大麦余票"\'')
            else:
                os.system(f'notify-send "大麦余票" "{msg}"')
        except Exception as e:
            self._warn(f"系统通知失败：{e}")

    # ── 主循环 ───────────────────────────────────────────────────────
    def run(self) -> None:
        print(f"开始监控演出 {self.item_id}，间隔 {self.interval}s，Ctrl+C 退出。")

        # 未设置 cookie 时给出提示
        if not self._has_login_cookie:
            print("提示：未设置登录 cookie，部分演出可能被风控拦截。")
            print("  可在配置文件中设置 cookie，或通过 --cookie / DAMAI_COOKIE 提供。")

        if self._working_endpoint:
            print(f"使用端点: {self._working_endpoint.label}")

        prev: Optional[TicketStatus] = None
        consecutive_risk_hits = 0

        while True:
            payload = self.query()

            # token 失效时刷新重试一次
            ret = payload.get("ret", [""])
            if ret and "TOKEN" in str(ret[0]).upper():
                self._warn("token 失效，正在刷新…")
                self._refresh_token()
                payload = self.query()

            ep_label = self._working_endpoint.label if self._working_endpoint else ""
            st = parse_response(self.item_id, payload, endpoint_label=ep_label)
            print(self._format(st))

            # 风控拦截时输出操作指引
            if not st.raw_ok:
                rc = detect_risk_control(payload, self._has_login_cookie)
                if rc.hit and rc.message:
                    print(f"  ⚠️ {rc.message}")
                    consecutive_risk_hits += 1
                else:
                    consecutive_risk_hits = 0

                # 连续多次风控 → 输出详细帮助
                if consecutive_risk_hits >= 3:
                    print()
                    print("  ─── 风控拦截频繁，请尝试以下操作 ───")
                    print("  1. 在配置文件中设置 cookie：浏览器登录大麦 → F12 → Network → 复制 Cookie 头")
                    print("     或命令行：python damai_monitor.py --cookie 'cookie2=xxx; sgcookie=yyy'")
                    print("     或环境变量：set DAMAI_COOKIE=cookie2=xxx; sgcookie=yyy")
                    print("  2. 增大轮询间隔：配置文件 interval 字段 或 --interval 10")
                    print("  3. 检查配置文件中 item_id 是否正确")
                    print()
                    consecutive_risk_hits = 0  # 避免重复输出
            else:
                consecutive_risk_hits = 0

            self._maybe_notify(st, prev)
            prev = st

            try:
                time.sleep(self.interval)
            except KeyboardInterrupt:
                break
        print("已停止监控。")


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> None:
    # Windows 控制台默认 GBK，强制 UTF-8 以正常显示中文
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="大麦网余票实时监控工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python damai_monitor.py                          # 使用默认配置文件\n"
            "  python damai_monitor.py --config my.json        # 指定配置文件\n"
            "  python damai_monitor.py 825173765577            # 覆盖配置中的 item_id\n"
            "  python damai_monitor.py --cookie 'cookie2=xxx'  # 覆盖 cookie\n"
        ),
    )
    parser.add_argument("item_id", nargs="?", default=None,
                        help="演出ID（覆盖配置文件中的 item_id）")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE,
                        help=f"配置文件路径（默认 {DEFAULT_CONFIG_FILE}）")
    parser.add_argument("-i", "--interval", type=float, default=None,
                        help="轮询间隔秒数，最小 1（覆盖配置文件）")
    parser.add_argument("-n", "--notify", action="store_true", default=None,
                        help='状态变为"可购买"时弹出系统通知')
    parser.add_argument("-v", "--verbose", action="store_true", default=None,
                        help="输出更多调试信息")
    parser.add_argument("-c", "--cookie", type=str, default=None,
                        help="登录 cookie 字符串（覆盖配置文件和环境变量）")
    args = parser.parse_args()

    # 加载配置文件
    file_cfg = load_config_file(args.config)
    if file_cfg:
        print(f"已加载配置文件: {args.config}")

    # 合并配置：CLI > 环境变量 > 配置文件 > 默认值
    config = merge_config(
        file_cfg=file_cfg,
        cli_item_id=args.item_id,
        cli_cookie=args.cookie,
        cli_interval=args.interval,
        cli_notify=args.notify if args.notify is not None else None,
        cli_verbose=args.verbose if args.verbose is not None else None,
    )

    if not config.item_id:
        parser.error("未指定演出ID。请在配置文件中设置 item_id，或通过命令行参数传入。")

    DamaiMonitor(config).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
