#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 APP 模拟登录 + 演出搜索 + 余票查询 主入口

完整流程：
  1. 检查已保存的 cookies → 有则直接加载，无则走登录流程
  2. 登录流程：发送短信验证码 → 提示输入 → 验证登录
  3. 输入演出名称搜索
  4. 取第一条搜索结果查询余票
  5. 格式化输出余票信息

使用：
  python damai_h5.py                              # 交互式登录+搜索+查询
  python damai_h5.py --phone 15757176315          # 指定手机号
  python damai_h5.py --search "周杰伦演唱会"       # 直接搜索
  python damai_h5.py --verbose                    # 详细日志

依赖：requests（pip install requests）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from damai_login import DamaiAppLogin
from damai_search import DamaiSearch, SearchResult
from damai_monitor import DamaiMonitor, MonitorConfig, parse_response, TicketStatus


# ─── 常量 ────────────────────────────────────────────────────────────────

DEFAULT_PHONE = "15757176315"
DEFAULT_COOKIE_FILE = "damai_cookies.json"
DEFAULT_CONFIG_FILE = "damai_config.json"


# ─── 辅助函数 ────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """加载配置文件。"""
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def format_ticket_status(st: TicketStatus) -> str:
    """格式化余票状态为可读字符串。"""
    lines = []

    if not st.raw_ok:
        data = st.raw.get("data", {}) or {}
        err = data.get("errorMsg", "") or st.error_msg
        ret = st.raw.get("ret", ["?"])
        detail = f" {err}" if err else f" ret={ret[0] if ret else '?'}"
        lines.append(f"⚠️ 查询失败{detail}")
        return "\n".join(lines)

    # 演出基本信息
    flag = "✅ 有票" if st.available else "❌ 暂无"
    lines.append(f"{'='*50}")
    lines.append(f"🎭 {st.name or '(未取到名称)'}")
    lines.append(f"{'='*50}")
    lines.append(f"📌 购买状态: {flag} | 按钮码={st.buy_btn}({st.buy_btn_text})")

    if st.venue:
        lines.append(f"📍 场馆: {st.venue}")
    if st.show_time:
        lines.append(f"🕐 时间: {st.show_time}")

    # 票档价格信息
    if st.price_list:
        lines.append(f"\n💰 票档价格:")
        for p in st.price_list:
            status_icon = "🟢" if "有票" in p.get("status", "") or "在售" in p.get("status", "") else "🔴"
            lines.append(
                f"   {status_icon} ¥{p['price']} — {p['name']} {p['status']}"
            )
    else:
        lines.append(f"\n💰 暂无票档价格信息")

    return "\n".join(lines)


def try_load_cookies(login: DamaiAppLogin, cookie_file: str) -> bool:
    """尝试从文件加载已保存的 cookies。

    Returns:
        是否成功加载并验证了登录态
    """
    if login.load_cookies(cookie_file):
        # 验证 cookies 是否仍然有效：尝试请求一个需要登录的接口
        login._log("已加载保存的 cookies，验证登录态…")
        if login.is_logged_in:
            print("✅ 已从保存的文件恢复登录态。")
            return True
        else:
            print("⚠️ 保存的 cookies 已失效，需要重新登录。")
            return False
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
        description="大麦网 APP 模拟登录 + 演出搜索 + 余票查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python damai_h5.py\n"
            "  python damai_h5.py --phone 15757176315\n"
            "  python damai_h5.py --search '周杰伦演唱会'\n"
            "  python damai_h5.py --verbose\n"
        ),
    )
    parser.add_argument("--phone", type=str, default=None,
                        help=f"手机号（默认 {DEFAULT_PHONE}）")
    parser.add_argument("--search", type=str, default=None,
                        help="演出名称（跳过交互输入）")
    parser.add_argument("--cookie-file", type=str, default=DEFAULT_COOKIE_FILE,
                        help=f"Cookie 保存文件（默认 {DEFAULT_COOKIE_FILE}）")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE,
                        help=f"配置文件（默认 {DEFAULT_CONFIG_FILE}）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出详细日志")
    args = parser.parse_args()

    # 加载配置文件
    config = load_config(args.config)
    phone = args.phone or config.get("phone", DEFAULT_PHONE)
    cookie_file = args.cookie_file or config.get("cookie_file", DEFAULT_COOKIE_FILE)
    verbose = args.verbose

    print("╔════════════════════════════════════════════════╗")
    print("║       大麦网 APP 模拟登录 + 余票查询           ║")
    print("╚════════════════════════════════════════════════╝")
    print()

    # ── Step 1: 登录 ─────────────────────────────────────────────────
    login = DamaiAppLogin(verbose=verbose)

    # 尝试加载已保存的 cookies
    if not try_load_cookies(login, cookie_file):
        # 需要重新登录
        print(f"📱 手机号: {phone}")
        print()

        if not login.login(phone):
            print("\n❌ 登录失败，无法继续。")
            print("提示：")
            print("  1. 确认手机号正确且能接收短信")
            print("  2. 如果触发图形验证码，请在浏览器中登录 m.damai.cn")
            print("     完成验证后复制 Cookie 到 damai_config.json")
            sys.exit(1)

        # 登录成功，保存 cookies
        login.save_cookies(cookie_file)

    # ── Step 2: 搜索演出 ─────────────────────────────────────────────
    search_keyword = args.search
    if not search_keyword:
        search_keyword = input("\n🔍 请输入演出名称: ").strip()
        if not search_keyword:
            print("演出名称不能为空。")
            sys.exit(1)

    print(f"\n🔍 正在搜索「{search_keyword}」…")

    searcher = DamaiSearch(session=login.session, verbose=verbose)
    results = searcher.search(search_keyword)

    if not results:
        print(f"\n❌ 未找到与「{search_keyword}」相关的演出。")
        print("提示：")
        print("  1. 检查演出名称是否正确")
        print("  2. 尝试使用更简短的关键词")
        print("  3. 确认登录态有效（cookies 未过期）")
        sys.exit(1)

    # 显示搜索结果
    print(DamaiSearch.format_results(results, search_keyword))

    # 取第一条结果
    first = results[0]
    if not first.item_id:
        print("❌ 第一条搜索结果缺少演出 ID，无法查询余票。")
        sys.exit(1)

    print(f"📌 取第一条结果: {first.name or '(未知)'} (ID: {first.item_id})")

    # ── Step 3: 查询余票 ─────────────────────────────────────────────
    print(f"\n🎫 正在查询余票信息…")

    # 使用已登录的 session 创建 DamaiMonitor
    cookie_str = login.get_cookie_string()
    monitor_config = MonitorConfig(
        item_id=first.item_id,
        cookie=cookie_str,
        interval=3.0,
        notify=False,
        verbose=verbose,
    )

    monitor = DamaiMonitor(monitor_config)

    # 查询一次
    payload = monitor.query()

    # token 失效时刷新重试
    ret = payload.get("ret", [""])
    if ret and "TOKEN" in str(ret[0]).upper():
        print("  token 失效，正在刷新…")
        monitor._refresh_token()
        payload = monitor.query()

    # 解析结果
    ep_label = monitor._working_endpoint.label if monitor._working_endpoint else ""
    st = parse_response(first.item_id, payload, endpoint_label=ep_label)

    # 输出结果
    print()
    print(format_ticket_status(st))

    # 如果查询失败，输出风控指引
    if not st.raw_ok:
        from damai_monitor import detect_risk_control
        rc = detect_risk_control(payload, monitor._has_login_cookie)
        if rc.hit and rc.message:
            print(f"\n⚠️ {rc.message}")

    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
