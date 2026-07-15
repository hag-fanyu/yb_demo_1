#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网演出搜索模块

通过 mtop 网关搜索演出，返回演出列表（名称、ID、场馆等）。

依赖：requests（pip install requests）
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    sys.stderr.write("缺少依赖 requests，请先执行：pip install requests\n")
    sys.exit(1)


# ─── 常量 ────────────────────────────────────────────────────────────────

APP_KEY = "12574478"
MTOP_HOST = "https://mtop.damai.cn"

# 搜索 API 候选列表
SEARCH_APIS: List[Tuple[str, str]] = [
    # (api_name, version)
    ("mtop.damai.wireless.search.result.get", "1.0"),
    ("mtop.damai.wireless.h5.search", "1.0"),
    ("mtop.damai.wireless.search.get", "1.0"),
    ("mtop.damai.item.search.get", "1.0"),
]


# ─── 数据结构 ────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """单条搜索结果。"""
    item_id: str = ""           # 演出 ID
    name: str = ""              # 演出名称
    venue: str = ""             # 场馆
    city: str = ""              # 城市
    show_time: str = ""         # 演出时间
    price_range: str = ""       # 价格区间
    category: str = ""          # 分类
    status: str = ""            # 状态（在售/售罄等）
    url: str = ""               # 详情页 URL
    raw: Dict[str, Any] = field(default_factory=dict)


# ─── 搜索类 ──────────────────────────────────────────────────────────────

class DamaiSearch:
    """大麦网演出搜索（通过 mtop 网关）。"""

    def __init__(
        self,
        session: requests.Session,
        verbose: bool = False,
    ):
        self.session = session
        self.verbose = verbose
        self._working_api: Optional[Tuple[str, str]] = None

    # ── 日志 ──────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [search] {msg}")

    @staticmethod
    def _warn(msg: str) -> None:
        sys.stderr.write(f"[warn] {msg}\n")

    # ── mtop 签名 ────────────────────────────────────────────────────
    def _sign(self, token: str, t: str, data: str) -> str:
        return hashlib.md5(
            f"{token}&{t}&{APP_KEY}&{data}".encode("utf-8")
        ).hexdigest()

    def _get_token(self) -> str:
        tk = (
            self.session.cookies.get("_m_h5_tk", domain=".damai.cn")
            or self.session.cookies.get("_m_h5_tk")
        )
        if not tk or "_" not in tk:
            return ""
        return tk.split("_")[0]

    # ── mtop 请求 ────────────────────────────────────────────────────
    def _mtop_request(
        self,
        api: str,
        version: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """发起 mtop GET 请求。"""
        token = self._get_token()
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

        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            self._warn(f"搜索请求失败 ({api}): {e}")
            return {"ret": ["FAIL_NET"], "msg": str(e)}

    # ── 搜索 ────────────────────────────────────────────────────────
    def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 10,
    ) -> List[SearchResult]:
        """搜索演出。

        自动在候选 API 列表中探测可用的搜索接口。

        Args:
            keyword: 搜索关键词（演出名称）
            page: 页码（从 1 开始）
            page_size: 每页条数

        Returns:
            搜索结果列表
        """
        data = {
            "keyword": keyword,
            "currentPage": page,
            "pageSize": page_size,
        }

        # 优先使用已缓存的 API
        if self._working_api:
            api, ver = self._working_api
            resp = self._mtop_request(api, ver, data)
            ret = resp.get("ret", [""])
            if "SUCCESS" in str(ret[0]):
                return self._parse_response(resp, api)
            self._log(f"缓存搜索 API {api} 失败: {ret}")

        # 遍历候选列表
        for api, ver in SEARCH_APIS:
            self._log(f"尝试搜索 API: {api}/{ver}")
            resp = self._mtop_request(api, ver, data)
            ret = resp.get("ret", [""])
            ret_str = str(ret[0]) if ret else ""

            if "SUCCESS" in ret_str:
                self._working_api = (api, ver)
                results = self._parse_response(resp, api)
                self._log(f"搜索成功，使用 API: {api}，返回 {len(results)} 条结果")
                return results

            if "API_NOT_EXIST" in ret_str.upper() or "API_NOT_FOUND" in ret_str.upper():
                self._log(f"API 不存在: {api}")
                continue

            self._log(f"API {api} 返回: {ret_str}")

        # 如果 mtop API 都不可用，尝试 HTTP 搜索页
        self._log("mtop 搜索 API 均不可用，尝试 HTTP 搜索页…")
        return self._search_via_http(keyword)

    # ── HTTP 搜索页回退 ─────────────────────────────────────────────
    def _search_via_http(self, keyword: str) -> List[SearchResult]:
        """通过 HTTP 请求搜索页获取结果（回退方案）。"""
        try:
            url = "https://search.damai.cn/search.html"
            params = {"keyword": keyword}
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Referer": "https://m.damai.cn/",
            }
            resp = self.session.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()

            # 尝试从 HTML 中提取搜索结果
            return self._parse_html_search(resp.text, keyword)
        except Exception as e:
            self._warn(f"HTTP 搜索页请求失败: {e}")
            return []

    def _parse_html_search(self, html: str, keyword: str) -> List[SearchResult]:
        """从搜索页 HTML 中提取结果。"""
        import re
        results = []

        # 尝试提取 JSON 数据（大麦搜索页可能在 __INIT_DATA__ 或类似变量中）
        json_pattern = re.search(
            r'window\.__INIT_DATA__\s*=\s*({.*?});',
            html, re.DOTALL
        )
        if json_pattern:
            try:
                init_data = json.loads(json_pattern.group(1))
                # 尝试从不同路径提取搜索结果
                for path in [
                    ["data", "result", "list"],
                    ["data", "searchResult", "list"],
                    ["result", "list"],
                    ["data", "list"],
                ]:
                    node = init_data
                    for key in path:
                        if isinstance(node, dict):
                            node = node.get(key)
                        else:
                            node = None
                            break
                    if isinstance(node, list) and node:
                        for item in node:
                            sr = SearchResult(
                                item_id=str(item.get("itemId", item.get("id", ""))),
                                name=item.get("itemName", item.get("name", item.get("title", ""))),
                                venue=item.get("venueName", item.get("venue", "")),
                                city=item.get("cityName", item.get("city", "")),
                                show_time=str(item.get("showTime", item.get("showDateTime", ""))),
                                price_range=item.get("priceRange", str(item.get("price", ""))),
                                category=item.get("categoryName", ""),
                                status=item.get("statusDesc", ""),
                                raw=item,
                            )
                            if sr.item_id or sr.name:
                                results.append(sr)
                        break
            except json.JSONDecodeError:
                pass

        # 如果 JSON 提取失败，尝试正则提取
        if not results:
            # 匹配 item.htm?id=XXXXX 模式
            id_pattern = re.findall(r'item\.htm\?id=(\d+)', html)
            name_pattern = re.findall(r'"itemName"\s*:\s*"([^"]+)"', html)

            ids = list(dict.fromkeys(id_pattern))  # 去重保序
            names = name_pattern

            for i, item_id in enumerate(ids[:10]):
                sr = SearchResult(
                    item_id=item_id,
                    name=names[i] if i < len(names) else "",
                )
                results.append(sr)

        return results

    # ── 解析 mtop 搜索响应 ──────────────────────────────────────────
    def _parse_response(
        self,
        payload: Dict[str, Any],
        api: str,
    ) -> List[SearchResult]:
        """解析 mtop 搜索 API 响应。"""
        results: List[SearchResult] = []
        data = payload.get("data", {}) or {}

        # 尝试多种数据结构路径
        result_list = None
        for path in [
            ["result", "list"],
            ["searchResult", "list"],
            ["resultList"],
            ["list"],
            ["data", "list"],
            ["result", "data", "list"],
        ]:
            node = data
            for key in path:
                if isinstance(node, dict):
                    node = node.get(key)
                else:
                    node = None
                    break
            if isinstance(node, list) and node:
                result_list = node
                break

        if not result_list:
            # 尝试直接遍历 data 的值找 list
            for key, value in data.items():
                if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict):
                    result_list = value
                    break
                if isinstance(value, dict):
                    for k2, v2 in value.items():
                        if isinstance(v2, list) and len(v2) > 0 and isinstance(v2[0], dict):
                            result_list = v2
                            break
                if result_list:
                    break

        if not result_list:
            self._log(f"未能从响应中提取搜索结果，data keys: {list(data.keys())}")
            return results

        for item in result_list:
            if not isinstance(item, dict):
                continue
            sr = SearchResult(
                item_id=str(
                    item.get("itemId")
                    or item.get("id")
                    or item.get("projectId", "")
                ),
                name=item.get("itemName") or item.get("name") or item.get("title", ""),
                venue=item.get("venueName") or item.get("venue", ""),
                city=item.get("cityName") or item.get("city", ""),
                show_time=str(
                    item.get("showTime")
                    or item.get("showDateTime")
                    or item.get("showtime", "")
                ),
                price_range=item.get("priceRange") or str(item.get("price", "")),
                category=item.get("categoryName") or item.get("category", ""),
                status=item.get("statusDesc") or item.get("status", ""),
                url=item.get("url", ""),
                raw=item,
            )
            if sr.item_id or sr.name:
                results.append(sr)

        return results

    # ── 格式化输出 ──────────────────────────────────────────────────
    @staticmethod
    def format_results(results: List[SearchResult], keyword: str) -> str:
        """格式化搜索结果为可读字符串。"""
        if not results:
            return f"未找到与「{keyword}」相关的演出。"

        lines = [f"搜索「{keyword}」共找到 {len(results)} 条结果：\n"]
        for i, sr in enumerate(results, 1):
            lines.append(f"  {i}. {sr.name or '(未知演出)'}")
            if sr.item_id:
                lines.append(f"     ID: {sr.item_id}")
            if sr.venue:
                lines.append(f"     📍 {sr.venue}")
            if sr.show_time:
                lines.append(f"     🕐 {sr.show_time}")
            if sr.price_range:
                lines.append(f"     💰 {sr.price_range}")
            if sr.city:
                lines.append(f"     🏙️ {sr.city}")
            if sr.status:
                lines.append(f"     📌 {sr.status}")
            lines.append("")

        return "\n".join(lines)
