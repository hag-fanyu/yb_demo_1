#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拟人化行为模拟模块

提供一系列工具函数，让自动化操作更像真人，降低被风控系统识别的概率：

  - human_delay()       — 替代 time.sleep()，带随机抖动
  - human_type_text()   — 逐字符输入文本，模拟打字节奏
  - human_click()       — 点击时加坐标偏移和前置延迟
  - human_scroll()      — 模拟人类滑动（不均匀速度、偶尔回弹）
  - human_browse()      — 在页面随机浏览（滑动 + 停顿）
  - human_navigate_pause() — 页面切换后的认知延迟
  - HumanBehaviorConfig — 集中管理拟人化参数

使用：
  from human_sim import HumanBehaviorConfig, human_delay, human_click

  cfg = HumanBehaviorConfig(level="medium")
  human_delay(2, config=cfg)          # 等待 1.2~2.8 秒
  human_click(element, config=cfg)    # 拟人化点击
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, Tuple, TypeVar


# ─── 拟人化配置 ────────────────────────────────────────────────────────────

class HumanBehaviorConfig:
    """拟人化行为配置。

    通过 level 快速设定预设参数，也可手动微调各参数。

    Levels:
      - low:    仅延迟抖动，不做额外浏览/停顿行为
      - medium: 延迟抖动 + 随机浏览 + 逐字输入 + 点击偏移
      - high:   medium + 更多随机停顿 + 偶尔"走错再回来"
    """

    # 预设参数表
    _PRESETS = {
        "low": dict(
            jitter_ratio=0.3,
            click_offset=0.05,
            click_pre_delay=(0.0, 0.1),
            click_post_delay=(0.05, 0.2),
            type_interval=(0.02, 0.06),
            type_hesitation_prob=0.05,
            type_hesitation_delay=(0.1, 0.3),
            browse_enabled=False,
            browse_duration=(0.5, 1.5),
            navigate_extra_prob=0.1,
            navigate_extra_delay=(0.3, 0.8),
            scroll_overshoot_prob=0.0,
            scroll_overshoot_ratio=0.0,
            warmup_enabled=False,
            idle_swipe_prob=0.0,
            go_wrong_prob=0.0,
        ),
        "medium": dict(
            jitter_ratio=0.4,
            click_offset=0.10,
            click_pre_delay=(0.1, 0.3),
            click_post_delay=(0.1, 0.5),
            type_interval=(0.05, 0.15),
            type_hesitation_prob=0.10,
            type_hesitation_delay=(0.3, 0.8),
            browse_enabled=True,
            browse_duration=(2.0, 5.0),
            navigate_extra_prob=0.30,
            navigate_extra_delay=(1.0, 2.0),
            scroll_overshoot_prob=0.15,
            scroll_overshoot_ratio=0.15,
            warmup_enabled=True,
            idle_swipe_prob=0.20,
            go_wrong_prob=0.0,
        ),
        "high": dict(
            jitter_ratio=0.5,
            click_offset=0.12,
            click_pre_delay=(0.15, 0.5),
            click_post_delay=(0.15, 0.7),
            type_interval=(0.06, 0.20),
            type_hesitation_prob=0.15,
            type_hesitation_delay=(0.4, 1.2),
            browse_enabled=True,
            browse_duration=(3.0, 7.0),
            navigate_extra_prob=0.40,
            navigate_extra_delay=(1.5, 3.0),
            scroll_overshoot_prob=0.25,
            scroll_overshoot_ratio=0.20,
            warmup_enabled=True,
            idle_swipe_prob=0.30,
            go_wrong_prob=0.05,
        ),
    }

    def __init__(self, level: str = "medium"):
        """初始化配置。

        Args:
            level: 拟人化等级 "low" / "medium" / "high"
        """
        level = level.lower()
        if level not in self._PRESETS:
            raise ValueError(f"Unknown stealth level: {level!r}, expected one of {list(self._PRESETS)}")

        preset = self._PRESETS[level]
        self.level = level

        # 延迟抖动
        self.jitter_ratio: float = preset["jitter_ratio"]

        # 点击
        self.click_offset: float = preset["click_offset"]
        self.click_pre_delay: Tuple[float, float] = preset["click_pre_delay"]
        self.click_post_delay: Tuple[float, float] = preset["click_post_delay"]

        # 输入
        self.type_interval: Tuple[float, float] = preset["type_interval"]
        self.type_hesitation_prob: float = preset["type_hesitation_prob"]
        self.type_hesitation_delay: Tuple[float, float] = preset["type_hesitation_delay"]

        # 浏览
        self.browse_enabled: bool = preset["browse_enabled"]
        self.browse_duration: Tuple[float, float] = preset["browse_duration"]

        # 导航停顿
        self.navigate_extra_prob: float = preset["navigate_extra_prob"]
        self.navigate_extra_delay: Tuple[float, float] = preset["navigate_extra_delay"]

        # 滚动
        self.scroll_overshoot_prob: float = preset["scroll_overshoot_prob"]
        self.scroll_overshoot_ratio: float = preset["scroll_overshoot_ratio"]

        # 预热
        self.warmup_enabled: bool = preset["warmup_enabled"]

        # 空闲滑动
        self.idle_swipe_prob: float = preset["idle_swipe_prob"]

        # "走错"概率（high 级别偶尔点错再回来）
        self.go_wrong_prob: float = preset["go_wrong_prob"]

    def __repr__(self) -> str:
        return f"HumanBehaviorConfig(level={self.level!r})"


# ─── 全局默认配置（懒初始化） ──────────────────────────────────────────────

_default_config: Optional[HumanBehaviorConfig] = None


def get_default_config() -> HumanBehaviorConfig:
    """获取全局默认配置（首次调用时初始化为 medium）。"""
    global _default_config
    if _default_config is None:
        _default_config = HumanBehaviorConfig("medium")
    return _default_config


def set_default_config(config: HumanBehaviorConfig) -> None:
    """设置全局默认配置。"""
    global _default_config
    _default_config = config


# ─── 核心工具函数 ──────────────────────────────────────────────────────────

def _jittered(base: float, ratio: float, min_val: float = 0.1) -> float:
    """生成带抖动的值，使用偏正态分布避免纯均匀分布的统计特征。

    内部用 Beta(2,5) 做偏移，让结果更集中在 base 附近，
    但仍有合理的长尾概率到达边界。

    Args:
        base: 基准值
        ratio: 抖动比例（0~1）
        min_val: 最小值

    Returns:
        抖动后的值
    """
    if base <= 0:
        return max(base, min_val)

    delta = base * ratio
    lo = base - delta
    hi = base + delta

    # 用 Beta 分布产生偏正态效果：Beta(2,5) 偏向 0，Beta(5,2) 偏向 1
    # 随机选择方向，让分布更自然
    if random.random() < 0.5:
        offset = random.betavariate(2, 5) * delta  # 偏向 0（即偏向 base）
    else:
        offset = -random.betavariate(2, 5) * delta  # 偏向 0（即偏向 base）

    result = base + offset
    return max(result, min_val)


def human_delay(
    base: float,
    jitter_ratio: Optional[float] = None,
    min_val: float = 0.1,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """替代 time.sleep() 的拟人化延迟。

    在 base ± base*jitter_ratio 范围内随机等待，
    使用偏正态分布避免纯均匀分布的统计特征。

    Args:
        base: 基准等待秒数
        jitter_ratio: 抖动比例，None 则使用 config 中的值
        min_val: 最小等待秒数
        config: 拟人化配置，None 则使用全局默认
    """
    cfg = config or get_default_config()
    ratio = jitter_ratio if jitter_ratio is not None else cfg.jitter_ratio
    delay = _jittered(base, ratio, min_val)
    time.sleep(delay)


def human_type_text(
    device: Any,
    element: Any,
    text: str,
    interval: Optional[Tuple[float, float]] = None,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """逐字符输入文本，模拟真人打字节奏。

    优先使用 u2 元素的 send_keys() 逐字输入；
    如果元素不支持 send_keys()，降级为逐字 set_text()。

    Args:
        device: u2.Device 对象（用于 send_keys 降级）
        element: u2 UI 元素（需已定位到输入框）
        text: 要输入的文本
        interval: 每字间隔范围 (min, max) 秒，None 则使用 config
        config: 拟人化配置
    """
    cfg = config or get_default_config()
    if interval is None:
        interval = cfg.type_interval

    # 注意：不清空输入框！真人不会先清空再输入，而是直接覆盖。
    # clear_text() 会产生一个 "select all → delete" 的操作序列，
    # 这是自动化检测的重要特征。

    # 逐字符输入
    for i, ch in enumerate(text):
        # 随机间隔
        gap = random.uniform(*interval)
        time.sleep(gap)

        # 偶尔犹豫（多停顿一会）
        if random.random() < cfg.type_hesitation_prob:
            hesitation = random.uniform(*cfg.type_hesitation_delay)
            time.sleep(hesitation)

        # 逐字输入：使用 send_keys 追加模式
        try:
            element.send_keys(ch, clear=False)
        except (AttributeError, TypeError):
            # 降级：用 set_text 设置已输入的部分
            # 注意：set_text 会产生 "select all → delete → paste" 的操作序列，
            # 是自动化检测的重要特征，仅在 send_keys 不可用时降级使用。
            try:
                element.set_text(text[: i + 1])
            except Exception:
                # 最后降级：一次性填入剩余
                try:
                    element.set_text(text)
                except Exception:
                    pass
                break


def human_click(
    element: Any,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """拟人化点击：前置延迟 + 坐标偏移 + 后置延迟。

    优先使用坐标点击（adb input tap），在元素中心加随机偏移，
    避免每次精确命中元素中心点（自动化检测的重要特征）。
    坐标点击失败时降级为 element.click()。

    Args:
        element: u2 UI 元素
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    # 前置延迟（模拟手指移向目标的时间）
    pre = random.uniform(*cfg.click_pre_delay)
    time.sleep(pre)

    # 尝试带偏移的坐标点击
    clicked = False
    try:
        info = element.info
        bounds = info.get("bounds", {})
        left = bounds.get("left", 0)
        top = bounds.get("top", 0)
        right = bounds.get("right", 0)
        bottom = bounds.get("bottom", 0)

        if right > left and bottom > top:
            width = right - left
            height = bottom - top
            # 中心点 + 随机偏移
            cx = (left + right) / 2
            cy = (top + bottom) / 2
            offset_x = random.uniform(-cfg.click_offset, cfg.click_offset) * width
            offset_y = random.uniform(-cfg.click_offset, cfg.click_offset) * height
            click_x = int(cx + offset_x)
            click_y = int(cy + offset_y)

            # 通过 u2 元素的 device 属性执行坐标点击
            # u2 UiObject 内部持有 device 引用
            try:
                d = element._device  # type: ignore
                d.click(click_x, click_y)
                clicked = True
            except (AttributeError, Exception):
                pass
    except Exception:
        pass

    # 坐标点击失败，降级为 element.click()
    if not clicked:
        try:
            # 注意：element.click() 会精确命中元素中心，无坐标偏移，
            # 是自动化检测的潜在特征。仅在坐标点击失败时降级使用。
            element.click()
        except Exception:
            pass

    # 后置延迟
    post = random.uniform(*cfg.click_post_delay)
    time.sleep(post)


def human_scroll(
    device: Any,
    direction: str = "down",
    distance: Optional[Tuple[float, float]] = None,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """拟人化滑动：不均匀速度 + 偶尔回弹/过冲。

    Args:
        device: u2.Device 对象
        direction: 滑动方向 "down" / "up" / "left" / "right"
        distance: 滑动距离比例范围 (min, max)，如 (0.3, 0.6)
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    if distance is None:
        distance = (0.3, 0.6)

    dist = random.uniform(*distance)

    # 起始/结束坐标加随机偏移
    x_center = 0.5 + random.uniform(-0.05, 0.05)

    if direction == "down":
        y_start = 0.7 + random.uniform(-0.05, 0.05)
        y_end = y_start - dist
    elif direction == "up":
        y_start = 0.3 + random.uniform(-0.05, 0.05)
        y_end = y_start + dist
    elif direction == "left":
        y_start = 0.5 + random.uniform(-0.05, 0.05)
        y_end = y_start
        x_center_start = 0.7 + random.uniform(-0.05, 0.05)
        x_center_end = x_center_start - dist
        device.swipe(x_center_start, y_start, x_center_end, y_end, duration=random.uniform(0.3, 0.8))
        return
    elif direction == "right":
        y_start = 0.5 + random.uniform(-0.05, 0.05)
        y_end = y_start
        x_center_start = 0.3 + random.uniform(-0.05, 0.05)
        x_center_end = x_center_start + dist
        device.swipe(x_center_start, y_start, x_center_end, y_end, duration=random.uniform(0.3, 0.8))
        return
    else:
        direction = "down"
        y_start = 0.7 + random.uniform(-0.05, 0.05)
        y_end = y_start - dist

    # 主滑动
    y_end = max(0.1, min(0.9, y_end))
    duration = random.uniform(0.3, 0.8)
    device.swipe(x_center, y_start, x_center, y_end, duration=duration)

    # 偶尔过冲（滑过头再回拉一点）
    if random.random() < cfg.scroll_overshoot_prob and cfg.scroll_overshoot_ratio > 0:
        overshoot = dist * cfg.scroll_overshoot_ratio
        time.sleep(random.uniform(0.1, 0.3))
        if direction == "down":
            device.swipe(x_center, y_end, x_center, y_end + overshoot * 0.5,
                         duration=random.uniform(0.15, 0.3))
        elif direction == "up":
            device.swipe(x_center, y_end, x_center, y_end - overshoot * 0.5,
                         duration=random.uniform(0.15, 0.3))


def human_browse(
    device: Any,
    duration: Optional[Tuple[float, float]] = None,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """在当前页面随机浏览，模拟真人看页面的行为。

    随机做几次小幅上下滑动 + 偶尔停顿。

    Args:
        device: u2.Device 对象
        duration: 浏览总时长范围 (min, max) 秒
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    if not cfg.browse_enabled:
        # low 级别不做浏览，仅做短延迟
        human_delay(0.5, config=cfg)
        return

    if duration is None:
        duration = cfg.browse_duration

    total = random.uniform(*duration)
    deadline = time.time() + total

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0.3:
            break

        # 随机选择动作
        action = random.random()

        if action < 0.5:
            # 小幅下滑
            human_scroll(device, direction="down", distance=(0.15, 0.35), config=cfg)
        elif action < 0.75:
            # 小幅上滑（回看）
            human_scroll(device, direction="up", distance=(0.10, 0.25), config=cfg)
        else:
            # 纯停顿（阅读）
            time.sleep(random.uniform(0.5, min(1.5, remaining)))

        # 动作间短暂间隔
        time.sleep(random.uniform(0.2, 0.6))


def human_navigate_pause(
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """页面切换后的认知延迟。

    基础等待 1~3 秒，偶尔（概率由 config 控制）额外多等 1~2 秒，
    模拟用户看到新页面后需要反应时间。

    Args:
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    # 基础认知延迟
    human_delay(2.0, config=cfg)

    # 偶尔额外停顿
    if random.random() < cfg.navigate_extra_prob:
        extra = random.uniform(*cfg.navigate_extra_delay)
        time.sleep(extra)


def human_idle_swipe(
    device: Any,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """偶尔做一次无意义的小幅滑动，增加行为随机性。

    只有在配置的 idle_swipe_prob 概率下才实际执行。

    Args:
        device: u2.Device 对象
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    if random.random() < cfg.idle_swipe_prob:
        # 随机方向小幅滑动
        direction = random.choice(["down", "up"])
        human_scroll(device, direction=direction, distance=(0.1, 0.2), config=cfg)


def human_warmup(
    device: Any,
    config: Optional[HumanBehaviorConfig] = None,
) -> None:
    """APP 启动后的预热行为：随机等待 + 偶尔在首页小幅滑动。

    Args:
        device: u2.Device 对象
        config: 拟人化配置
    """
    cfg = config or get_default_config()

    if not cfg.warmup_enabled:
        human_delay(3.0, config=cfg)
        return

    # 随机等待 2~5 秒（模拟用户刚打开 APP 的反应时间）
    human_delay(3.5, config=cfg)

    # 50% 概率做 1~2 次随机小幅滑动
    if random.random() < 0.5:
        n_swipes = random.randint(1, 2)
        for _ in range(n_swipes):
            direction = random.choice(["down", "up"])
            human_scroll(device, direction=direction, distance=(0.15, 0.3), config=cfg)
            time.sleep(random.uniform(0.3, 0.8))


# ─── 重试辅助 ──────────────────────────────────────────────────────────────

T = TypeVar("T")


def retry_with_backoff(
    fn: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
    config: Optional[HumanBehaviorConfig] = None,
    retry_on: Tuple[type, ...] = (Exception,),
) -> T:
    """带指数退避的重试，每次重试前做拟人化延迟。

    适用于网络波动、临时弹窗遮挡等场景。
    退避延迟 = base_delay * backoff_factor^attempt + 随机抖动。

    Args:
        fn: 要执行的函数（无参数）
        max_retries: 最大重试次数
        base_delay: 首次重试前的等待秒数
        backoff_factor: 退避倍数
        config: 拟人化配置
        retry_on: 触发重试的异常类型

    Returns:
        fn 的返回值

    Raises:
        最后一次异常（如果所有重试都失败）
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retry_on as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (backoff_factor ** attempt)
                human_delay(delay, config=config)
            else:
                raise
    # 不应到达这里，但类型检查需要
    raise last_exc  # type: ignore


def dismiss_dialogs(
    device: Any,
    config: Optional[HumanBehaviorConfig] = None,
) -> bool:
    """尝试关闭可能遮挡页面的弹窗/对话框。

    常见弹窗：升级提示、广告弹窗、权限请求、通知等。
    在关键操作前调用，避免弹窗导致操作失败。

    Args:
        device: u2.Device 对象
        config: 拟人化配置

    Returns:
        是否关闭了弹窗
    """
    dismissed = False

    # 常见关闭按钮文字（随机打乱顺序，避免每次遍历顺序相同产生可检测模式）
    close_texts = ["关闭", "取消", "以后再说", "暂不升级", "忽略", "跳过",
                   "不再提醒", "知道了", "确定", "Not now", "Later", "Cancel"]
    random.shuffle(close_texts)

    for text in close_texts:
        try:
            btn = device(text=text)
            if btn.exists(timeout=0.5):
                human_click(btn, config=config)
                dismissed = True
                human_delay(0.5, config=config)
                break
        except Exception:
            continue

    # 尝试按返回键关闭弹窗
    if not dismissed:
        try:
            # 检查是否有对话框存在
            dialog = device(className="android.app.Dialog")
            if dialog.exists(timeout=0.5):
                device.press("back")
                dismissed = True
                human_delay(0.5, config=config)
        except Exception:
            pass

    return dismissed
