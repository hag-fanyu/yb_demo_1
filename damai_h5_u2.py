#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 H5 页面自动化登录 + 演出搜索 + 余票查询（uiautomator2 版）

完整流程：
  1. 通过 uiautomator2 连接 Android 设备
  2. 启动大麦 APP，在其 WebView 中完成登录
  3. 登录后搜索演出，取第一条结果
  4. 用登录态 cookies 查询余票信息

使用：
  python damai_h5_u2.py                                    # 交互式登录+搜索+查询
  python damai_h5_u2.py --phone 15757176315               # 指定手机号
  python damai_h5_u2.py --search "周杰伦演唱会"            # 直接搜索
  python damai_h5_u2.py --device abc123                   # 指定设备序列号
  python damai_h5_u2.py --verbose                          # 详细日志

依赖：
  pip install uiautomator2 requests
  Android 设备需开启 USB 调试并通过 adb 连接
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from damai_u2 import DamaiU2Automation
from damai_monitor import DamaiMonitor, MonitorConfig, parse_response, TicketStatus


# ─── 常量 ────────────────────────────────────────────────────────────────

DEFAULT_PHONE = "15757176315"
DEFAULT_COOKIE_FILE = "damai_cookies_u2.json"


# ─── 辅助函数 ────────────────────────────────────────────────────────────

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


def try_load_cookies(automation: DamaiU2Automation, cookie_file: str) -> bool:
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
        description="大麦网 H5 自动化登录 + 演出搜索 + 余票查询（uiautomator2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python damai_h5_u2.py\n"
            "  python damai_h5_u2.py --phone 15757176315\n"
            "  python damai_h5_u2.py --search '周杰伦演唱会'\n"
            "  python damai_h5_u2.py --device abc123\n"
            "  python damai_h5_u2.py --verbose\n"
            "\n"
            "设备准备:\n"
            "  1. 手机开启 USB 调试（设置 → 开发者选项 → USB 调试）\n"
            "  2. USB 连接电脑，运行 adb devices 确认设备可见\n"
            "  3. 安装依赖：pip install uiautomator2 requests\n"
        ),
    )
    parser.add_argument("--device", type=str, default=None,
                        help="设备序列号（默认自动检测）")
    parser.add_argument("--phone", type=str, default=None,
                        help=f"手机号（默认 {DEFAULT_PHONE}）")
    parser.add_argument("--search", type=str, default=None,
                        help="演出名称（跳过交互输入）")
    parser.add_argument("--cookie-file", type=str, default=DEFAULT_COOKIE_FILE,
                        help=f"Cookie 保存文件（默认 {DEFAULT_COOKIE_FILE}）")
    parser.add_argument("--skip-login", action="store_true",
                        help="跳过登录（使用已保存的 cookies）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出详细日志")
    args = parser.parse_args()

    phone = args.phone or DEFAULT_PHONE
    cookie_file = args.cookie_file
    verbose = args.verbose

    print("╔════════════════════════════════════════════════╗")
    print("║   大麦网 H5 自动化登录 + 余票查询 (u2)        ║")
    print("╚════════════════════════════════════════════════╝")
    print()

    # ── Step 0: 连接设备 ─────────────────────────────────────────────
    automation = DamaiU2Automation(
        device_serial=args.device,
        verbose=verbose,
    )
    automation.connect_device()

    # ── Step 1: 启动 APP ─────────────────────────────────────────────
    automation.launch_damai_app()

    # ── Step 2: 登录 ─────────────────────────────────────────────────
    if args.skip_login:
        # 尝试加载已保存的 cookies
        if try_load_cookies(automation, cookie_file):
            print("✅ 跳过登录，使用已保存的 cookies。")
        else:
            print("⚠️ 未找到已保存的 cookies，需要重新登录。")
            args.skip_login = False

    if not args.skip_login:
        print(f"📱 手机号: {phone}")
        print()

        if not automation.login(phone):
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
            automation.save_cookies(cookie_file)
        else:
            print("⚠️ 未能提取 cookies，将尝试继续…")

    # ── Step 3: 搜索演出 ─────────────────────────────────────────────
    search_keyword = args.search
    if not search_keyword:
        search_keyword = input("\n🔍 请输入演出名称: ").strip()
        if not search_keyword:
            print("演出名称不能为空。")
            automation.cleanup()
            sys.exit(1)

    print(f"\n🔍 正在搜索「{search_keyword}」…")

    # 导航到搜索页并搜索
    if not automation.navigate_to_search():
        print("❌ 无法到达搜索页面")
        automation.cleanup()
        sys.exit(1)

    if not automation.input_search_keyword(search_keyword):
        print("❌ 无法输入搜索关键词")
        automation.cleanup()
        sys.exit(1)

    # 获取第一条搜索结果
    first_result = automation.get_first_result()
    if not first_result:
        print(f"\n❌ 未找到与「{search_keyword}」相关的演出。")
        print("提示：")
        print("  1. 检查演出名称是否正确")
        print("  2. 尝试使用更简短的关键词")
        print("  3. 确认登录态有效")
        automation.cleanup()
        sys.exit(1)

    item_id = first_result.get("item_id", "")
    item_name = first_result.get("name", "(未知)")

    print(f"\n📌 第一条搜索结果：")
    print(f"   🎭 {item_name}")
    if item_id:
        print(f"   🆔 ID: {item_id}")
    if first_result.get("url"):
        print(f"   🔗 {first_result['url']}")

    if not item_id:
        print("❌ 第一条搜索结果缺少演出 ID，无法查询余票。")
        automation.cleanup()
        sys.exit(1)

    # ── Step 4: 查询余票 ─────────────────────────────────────────────
    print(f"\n🎫 正在查询余票信息…")

    # 用提取的 cookies 创建 DamaiMonitor 查询余票
    cookie_str = automation.get_cookie_string()
    monitor_config = MonitorConfig(
        item_id=item_id,
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
    st = parse_response(item_id, payload, endpoint_label=ep_label)

    # 输出结果
    print()
    print(format_ticket_status(st))

    # 如果查询失败，输出风控指引
    if not st.raw_ok:
        from damai_monitor import detect_risk_control
        rc = detect_risk_control(payload, monitor._has_login_cookie)
        if rc.hit and rc.message:
            print(f"\n⚠️ {rc.message}")

    # ── 清理 ─────────────────────────────────────────────────────────
    automation.cleanup()
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
