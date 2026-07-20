#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 OCR + 坐标点击辅助模块

当 WebView 和 Native UI 自动化都无法获取页面元素时（自研渲染引擎/Flutter/关闭调试的 WebView），
通过截图 → OCR 识别文字 → 计算坐标 → adb input tap 来交互。

核心功能：
  1. screenshot()       — 截取设备屏幕
  2. ocr_find_texts()   — OCR 识别文字并匹配关键词
  3. click_text()       — 找到文字并点击其中心坐标
  4. scroll_and_find()  — 滚动查找目标文字
  5. extract_all_text() — 提取当前屏所有文字
  6. smart_scroll_and_extract() — 滚动提取全页文字并分类

使用：
  # 作为独立模块测试
  python damai_ocr_click.py --test

  # 在自动化脚本中集成
  from damai_ocr_click import OcrClickHelper
  helper = OcrClickHelper(device=d)
  helper.click_text("已预约", scroll=True)
  info = helper.smart_scroll_and_extract()

依赖：
  pip install Pillow paddleocr paddlepaddle
  # 或轻量替代：
  pip install Pillow easyocr torch
"""

from __future__ import annotations

import io
import os
import random
import re
import sys
import time
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    sys.stderr.write("缺少依赖 Pillow，请先执行：pip install Pillow\n")
    Image = None  # type: ignore


# ─── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class OCRMatch:
    """OCR 识别结果中的单条匹配。"""
    text: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # (left, top, right, bottom)
    center_x: int = 0
    center_y: int = 0

    def __post_init__(self):
        if self.center_x == 0 and self.center_y == 0:
            self.center_x = (self.bbox[0] + self.bbox[2]) // 2
            self.center_y = (self.bbox[1] + self.bbox[3]) // 2


@dataclass
class OCRTextLine:
    """OCR 识别的一行文字。"""
    text: str
    confidence: float
    bbox: Tuple[int, int, int, int]
    center_x: int = 0
    center_y: int = 0

    def __post_init__(self):
        if self.center_x == 0 and self.center_y == 0:
            self.center_x = (self.bbox[0] + self.bbox[2]) // 2
            self.center_y = (self.bbox[1] + self.bbox[3]) // 2


# ─── OCR 引擎封装 ──────────────────────────────────────────────────────────

class OcrEngine:
    """OCR 引擎统一封装（支持 PaddleOCR / EasyOCR）。"""

    def __init__(self, engine: str = "auto", verbose: bool = False):
        """
        Args:
            engine: "paddleocr" / "easyocr" / "auto"（自动选择可用的）
            verbose: 是否输出详细日志
        """
        self.engine_name = engine
        self.verbose = verbose
        self._ocr = None
        self._init_engine(engine)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [ocr] {msg}")

    def _init_engine(self, engine: str) -> None:
        """初始化 OCR 引擎。"""
        if engine in ("auto", "paddleocr"):
            try:
                from paddleocr import PaddleOCR
                self._ocr = PaddleOCR(
                    use_angle_cls=True,
                    lang="ch",
                    use_gpu=False,
                    show_log=False,
                )
                self.engine_name = "paddleocr"
                self._log("已初始化 PaddleOCR")
                return
            except ImportError:
                if engine == "paddleocr":
                    sys.stderr.write(
                        "缺少依赖 paddleocr，请执行：pip install paddleocr paddlepaddle\n"
                    )
                    raise
                self._log("PaddleOCR 不可用，尝试 EasyOCR…")

        if engine in ("auto", "easyocr"):
            try:
                import easyocr
                self._ocr = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
                self.engine_name = "easyocr"
                self._log("已初始化 EasyOCR")
                return
            except ImportError:
                if engine == "easyocr":
                    sys.stderr.write(
                        "缺少依赖 easyocr，请执行：pip install easyocr torch\n"
                    )
                    raise
                self._log("EasyOCR 也不可用")

        raise RuntimeError(
            "无可用的 OCR 引擎。请安装其中一个：\n"
            "  pip install paddleocr paddlepaddle\n"
            "  pip install easyocr torch\n"
        )

    def recognize(self, image: "Image.Image") -> List[OCRTextLine]:
        """对图片执行 OCR 识别。

        Args:
            image: PIL.Image 对象

        Returns:
            识别到的文字行列表
        """
        if self.engine_name == "paddleocr":
            return self._recognize_paddle(image)
        elif self.engine_name == "easyocr":
            return self._recognize_easy(image)
        else:
            raise RuntimeError(f"未知引擎：{self.engine_name}")

    def _recognize_paddle(self, image: "Image.Image") -> List[OCRTextLine]:
        """PaddleOCR 识别。"""
        # PaddleOCR 需要文件路径或 numpy 数组
        import numpy as np
        img_array = np.array(image)

        results = self._ocr.ocr(img_array, cls=True)

        lines: List[OCRTextLine] = []
        if results and results[0]:
            for item in results[0]:
                bbox_points, (text, confidence) = item
                # bbox_points 是四个角点 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                xs = [p[0] for p in bbox_points]
                ys = [p[1] for p in bbox_points]
                left = int(min(xs))
                top = int(min(ys))
                right = int(max(xs))
                bottom = int(max(ys))
                lines.append(OCRTextLine(
                    text=text,
                    confidence=float(confidence),
                    bbox=(left, top, right, bottom),
                ))

        self._log(f"PaddleOCR 识别到 {len(lines)} 行文字")
        return lines

    def _recognize_easy(self, image: "Image.Image") -> List[OCRTextLine]:
        """EasyOCR 识别。"""
        import numpy as np
        img_array = np.array(image)

        results = self._ocr.readtext(img_array)

        lines: List[OCRTextLine] = []
        for item in results:
            # EasyOCR 返回 (bbox, text, confidence)
            bbox_points, text, confidence = item
            xs = [p[0] for p in bbox_points]
            ys = [p[1] for p in bbox_points]
            left = int(min(xs))
            top = int(min(ys))
            right = int(max(xs))
            bottom = int(max(ys))
            lines.append(OCRTextLine(
                text=text,
                confidence=float(confidence),
                bbox=(left, top, right, bottom),
            ))

        self._log(f"EasyOCR 识别到 {len(lines)} 行文字")
        return lines


# ─── 核心辅助类 ────────────────────────────────────────────────────────────

class OcrClickHelper:
    """OCR + 坐标点击辅助类。

    通过截图 → OCR → adb input tap 实现与 APP 页面的交互，
    不依赖 UI 元素树（Native/WebView），适用于自研渲染引擎/Flutter 等场景。
    """

    # 设备屏幕上的常见区域（用于滑动等操作）
    SWIPE_START_Y_RATIO = 0.8   # 滑动起始 Y（屏幕高度的 80%）
    SWIPE_END_Y_RATIO = 0.2     # 滑动结束 Y（屏幕高度的 20%）
    SWIPE_X_RATIO = 0.5         # 滑动 X 位置（屏幕宽度的 50%）
    SWIPE_DURATION = 300         # 滑动持续时间（ms）

    # OCR 置信度阈值
    MIN_CONFIDENCE = 0.5

    def __init__(
        self,
        device: Any,
        ocr_engine: str = "auto",
        verbose: bool = False,
    ):
        """
        Args:
            device: uiautomator2 Device 对象
            ocr_engine: OCR 引擎（"auto" / "paddleocr" / "easyocr"）
            verbose: 是否输出详细日志
        """
        self.d = device
        self.verbose = verbose
        self._ocr_engine: Optional[OcrEngine] = None
        self._ocr_engine_name = ocr_engine
        self._screen_width = 0
        self._screen_height = 0
        self._last_screenshot: Optional["Image.Image"] = None

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [ocr-click] {msg}")

    @property
    def ocr(self) -> OcrEngine:
        """延迟初始化 OCR 引擎（首次使用时才加载模型）。"""
        if self._ocr_engine is None:
            print("🔤 正在初始化 OCR 引擎（首次加载模型，请稍候）…")
            self._ocr_engine = OcrEngine(
                engine=self._ocr_engine_name,
                verbose=self.verbose,
            )
            print(f"✅ OCR 引擎已就绪（{self._ocr_engine.engine_name}）")
        return self._ocr_engine

    # ── 设备信息 ──────────────────────────────────────────────────────

    def _get_screen_size(self) -> Tuple[int, int]:
        """获取设备屏幕尺寸。"""
        if self._screen_width and self._screen_height:
            return self._screen_width, self._screen_height

        try:
            info = self.d.info
            self._screen_width = info.get("displayWidth", 1080)
            self._screen_height = info.get("displayHeight", 1920)
        except Exception:
            # 备用：通过 adb shell wm size
            try:
                output = self.d.shell("wm size")[0]
                m = re.search(r"(\d+)x(\d+)", output)
                if m:
                    self._screen_width = int(m.group(1))
                    self._screen_height = int(m.group(2))
            except Exception:
                self._screen_width = 1080
                self._screen_height = 1920

        self._log(f"屏幕尺寸：{self._screen_width}x{self._screen_height}")
        return self._screen_width, self._screen_height

    # ── 截图 ──────────────────────────────────────────────────────────

    def screenshot(self) -> "Image.Image":
        """截取设备当前屏幕。

        优先使用 uiautomator2 内置截图，备用 adb screencap。

        Returns:
            PIL.Image 对象
        """
        if Image is None:
            raise RuntimeError("缺少 Pillow 库，请执行：pip install Pillow")

        # 方式1：uiautomator2 内置截图（返回 PIL.Image）
        try:
            img = self.d.screenshot()
            if img is not None:
                self._last_screenshot = img
                self._log(f"截图成功（{img.size[0]}x{img.size[1]}）")
                return img
        except Exception as e:
            self._log(f"u2 截图失败：{e}，尝试 adb screencap…")

        # 方式2：adb screencap + pull
        try:
            remote_path = "/sdcard/_ocr_screenshot.png"
            self.d.shell(f"screencap -p {remote_path}")

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                local_path = f.name

            try:
                self.d.adb.pull(remote_path, local_path)
                img = Image.open(local_path)
                img.load()  # 确保数据加载
                self._last_screenshot = img
                self._log(f"adb 截图成功（{img.size[0]}x{img.size[1]}）")
                return img
            finally:
                try:
                    os.unlink(local_path)
                except Exception:
                    pass
                try:
                    self.d.shell(f"rm {remote_path}")
                except Exception:
                    pass
        except Exception as e:
            raise RuntimeError(f"截图失败：{e}")

    def save_screenshot(self, path: str = "debug_screenshot.png") -> str:
        """截图并保存到文件（用于调试）。

        Args:
            path: 保存路径

        Returns:
            保存的文件路径
        """
        img = self.screenshot()
        img.save(path)
        self._log(f"截图已保存到 {path}")
        return path

    # ── OCR 查找 ──────────────────────────────────────────────────────

    def ocr_find_texts(
        self,
        image: Optional["Image.Image"] = None,
        keywords: Optional[List[str]] = None,
        min_confidence: float = 0.5,
    ) -> List[OCRMatch]:
        """在截图中查找包含指定关键词的文字。

        Args:
            image: PIL.Image 对象，None 则自动截图
            keywords: 关键词列表，None 则返回所有文字
            min_confidence: 最低置信度阈值

        Returns:
            匹配的 OCRMatch 列表
        """
        if image is None:
            image = self.screenshot()

        all_lines = self.ocr.recognize(image)

        # 过滤低置信度
        all_lines = [l for l in all_lines if l.confidence >= min_confidence]

        if keywords is None:
            # 返回所有文字
            return [
                OCRMatch(
                    text=l.text,
                    confidence=l.confidence,
                    bbox=l.bbox,
                    center_x=l.center_x,
                    center_y=l.center_y,
                )
                for l in all_lines
            ]

        # 按关键词匹配
        matches: List[OCRMatch] = []
        for line in all_lines:
            for kw in keywords:
                if kw in line.text:
                    matches.append(OCRMatch(
                        text=line.text,
                        confidence=line.confidence,
                        bbox=line.bbox,
                        center_x=line.center_x,
                        center_y=line.center_y,
                    ))
                    break  # 一行只匹配一次

        self._log(f"OCR 查找关键词 {keywords}：找到 {len(matches)} 个匹配")
        return matches

    # ── 点击 ──────────────────────────────────────────────────────────

    def _tap(self, x: int, y: int) -> None:
        """通过 adb input tap 点击指定坐标（带拟人化抖动）。

        风控系统可通过 /proc/input/event 事件流检测到：
          - 每次点击坐标都是整数（真人会有亚像素抖动）
          - 点击位置总是精确命中 OCR 文字中心（真人会偏移 5-20px）
        此方法加入坐标偏移和前后延迟来模拟真人点击。

        Args:
            x: X 坐标
            y: Y 坐标
        """
        # 拟人化：坐标偏移 ±5~15px（真人不会精确点击中心）
        offset_x = random.randint(-12, 12)
        offset_y = random.randint(-12, 12)
        tap_x = x + offset_x
        tap_y = y + offset_y
        # 拟人化：点击前短暂停顿（模拟手指移动时间）
        time.sleep(random.uniform(0.05, 0.15))
        self.d.shell(f"input tap {tap_x} {tap_y}")
        # 拟人化：点击后短暂停顿
        time.sleep(random.uniform(0.08, 0.20))
        self._log(f"已点击坐标 ({tap_x}, {tap_y})，原始 ({x}, {y})")

    def click_text(
        self,
        keyword: str,
        image: Optional["Image.Image"] = None,
        scroll: bool = False,
        max_swipes: int = 8,
        swipe_direction: str = "up",
        min_confidence: float = 0.5,
        offset_y: int = 0,
    ) -> bool:
        """在屏幕上查找包含关键词的文字并点击其中心坐标。

        Args:
            keyword: 要查找的关键词
            image: PIL.Image 对象，None 则自动截图
            scroll: 如果当前屏没找到，是否自动滑动查找
            max_swipes: 最大滑动次数
            swipe_direction: 滑动方向 "up"（向上滑，即查看下方内容）或 "down"
            min_confidence: 最低置信度
            offset_y: Y 坐标偏移（微调点击位置）

        Returns:
            是否成功找到并点击
        """
        print(f"🔍 OCR 查找「{keyword}」…")

        # 先在当前屏查找
        matches = self.ocr_find_texts(
            image=image,
            keywords=[keyword],
            min_confidence=min_confidence,
        )
        if matches:
            # 取置信度最高的
            best = max(matches, key=lambda m: m.confidence)
            tap_x = best.center_x
            tap_y = best.center_y + offset_y
            self._tap(tap_x, tap_y)
            print(f"✅ 已点击「{best.text}」({tap_x}, {tap_y})")
            return True

        if not scroll:
            print(f"⚠️ 当前屏未找到「{keyword}」")
            return False

        # 滑动查找
        print(f"  当前屏未找到，开始滑动查找…")
        return self.scroll_and_find(
            keyword=keyword,
            max_swipes=max_swipes,
            swipe_direction=swipe_direction,
            min_confidence=min_confidence,
            offset_y=offset_y,
        ) is not None

    def scroll_and_find(
        self,
        keyword: str,
        max_swipes: int = 8,
        swipe_direction: str = "up",
        min_confidence: float = 0.5,
        offset_y: int = 0,
    ) -> Optional[OCRMatch]:
        """滑动屏幕查找包含关键词的文字。

        每次滑动后截图 OCR 查找，找到则点击并返回。

        Args:
            keyword: 关键词
            max_swipes: 最大滑动次数
            swipe_direction: "up" 或 "down"
            min_confidence: 最低置信度
            offset_y: Y 坐标偏移

        Returns:
            找到的 OCRMatch，或 None
        """
        w, h = self._get_screen_size()
        swipe_x = int(w * self.SWIPE_X_RATIO)

        for i in range(max_swipes):
            # 滑动
            if swipe_direction == "up":
                start_y = int(h * self.SWIPE_START_Y_RATIO)
                end_y = int(h * self.SWIPE_END_Y_RATIO)
            else:
                start_y = int(h * self.SWIPE_END_Y_RATIO)
                end_y = int(h * self.SWIPE_START_Y_RATIO)

            self.d.shell(
                f"input swipe {swipe_x} {start_y} {swipe_x} {end_y} {self.SWIPE_DURATION}"
            )
            time.sleep(1)  # 等待滑动完成和页面稳定

            # 截图 OCR
            matches = self.ocr_find_texts(
                keywords=[keyword],
                min_confidence=min_confidence,
            )
            if matches:
                best = max(matches, key=lambda m: m.confidence)
                tap_x = best.center_x
                tap_y = best.center_y + offset_y
                self._tap(tap_x, tap_y)
                print(f"✅ 滑动 {i+1} 次后找到并点击「{best.text}」({tap_x}, {tap_y})")
                return best

            self._log(f"滑动 {i+1}/{max_swipes}，未找到「{keyword}」")

        print(f"⚠️ 滑动 {max_swipes} 次后仍未找到「{keyword}」")
        return None

    # ── 提取文字 ──────────────────────────────────────────────────────

    def extract_all_text(
        self,
        image: Optional["Image.Image"] = None,
        min_confidence: float = 0.5,
    ) -> List[OCRTextLine]:
        """提取当前屏幕所有文字。

        Args:
            image: PIL.Image，None 则自动截图
            min_confidence: 最低置信度

        Returns:
            OCRTextLine 列表
        """
        if image is None:
            image = self.screenshot()

        lines = self.ocr.recognize(image)
        lines = [l for l in lines if l.confidence >= min_confidence]
        self._log(f"提取到 {len(lines)} 行文字")
        return lines

    def smart_scroll_and_extract(
        self,
        max_swipes: int = 15,
        swipe_direction: str = "up",
        min_confidence: float = 0.5,
        dedup: bool = True,
    ) -> Dict[str, Any]:
        """滚动页面逐屏 OCR 提取所有文字，并按关键词分类。

        适用于提取演出详情页的场次和票档信息。

        Args:
            max_swipes: 最大滑动次数
            swipe_direction: 滑动方向
            min_confidence: 最低置信度
            dedup: 是否去重

        Returns:
            dict with keys:
              all_lines: List[OCRTextLine] — 所有识别到的文字行
              sessions: List[Dict] — 场次信息（含日期/时间的行）
              tickets: List[Dict] — 票档信息（含价格/票价的行）
              raw_text: str — 所有文字拼接
        """
        print("📊 OCR 滚动提取页面文字…")

        w, h = self._get_screen_size()
        swipe_x = int(w * self.SWIPE_X_RATIO)

        all_lines: List[OCRTextLine] = []
        seen_texts: set = set()

        # 先提取当前屏
        current_lines = self.extract_all_text(min_confidence=min_confidence)
        for line in current_lines:
            if dedup and line.text in seen_texts:
                continue
            seen_texts.add(line.text)
            all_lines.append(line)

        # 逐屏滑动提取
        stable_count = 0  # 连续没有新内容的次数
        for i in range(max_swipes):
            # 滑动
            if swipe_direction == "up":
                start_y = int(h * self.SWIPE_START_Y_RATIO)
                end_y = int(h * self.SWIPE_END_Y_RATIO)
            else:
                start_y = int(h * self.SWIPE_END_Y_RATIO)
                end_y = int(h * self.SWIPE_START_Y_RATIO)

            self.d.shell(
                f"input swipe {swipe_x} {start_y} {swipe_x} {end_y} {self.SWIPE_DURATION}"
            )
            time.sleep(1)

            # 截图 OCR
            current_lines = self.extract_all_text(min_confidence=min_confidence)

            new_count = 0
            for line in current_lines:
                if dedup and line.text in seen_texts:
                    continue
                seen_texts.add(line.text)
                all_lines.append(line)
                new_count += 1

            self._log(f"滑动 {i+1}/{max_swipes}，新增 {new_count} 行")

            if new_count == 0:
                stable_count += 1
                if stable_count >= 3:
                    self._log("连续 3 次无新内容，停止滑动")
                    break
            else:
                stable_count = 0

        # 分类
        sessions: List[Dict] = []
        tickets: List[Dict] = []
        raw_texts: List[str] = []

        # 场次正则：日期/时间格式
        session_patterns = [
            r'\d{4}年\d{1,2}月\d{1,2}日',
            r'\d{4}[.-]\d{1,2}[.-]\d{1,2}',
            r'\d{1,2}月\d{1,2}日',
            r'\d{1,2}月\d{1,2}',
            r'周[一二三四五六日天]',
            r'\d{1,2}:\d{2}',  # 时间
            r'\d{1,2}:\d{2}[-~]\d{1,2}:\d{2}',  # 时间段
        ]
        session_re = re.compile('|'.join(session_patterns))

        # 票档正则：价格/票相关
        ticket_patterns = [
            r'[¥￥]\s*\d+',           # ¥价格
            r'\d+元',                   # X元
            r'票档|票价|座位|座席|档位',  # 票档关键词
        ]
        ticket_re = re.compile('|'.join(ticket_patterns))

        for line in all_lines:
            raw_texts.append(line.text)

            # 场次分类
            if session_re.search(line.text):
                sessions.append({
                    "text": line.text,
                    "confidence": round(line.confidence, 3),
                    "y_position": line.center_y,
                })

            # 票档分类
            if ticket_re.search(line.text):
                tickets.append({
                    "text": line.text,
                    "confidence": round(line.confidence, 3),
                    "y_position": line.center_y,
                })

        result = {
            "all_lines": all_lines,
            "sessions": sessions,
            "tickets": tickets,
            "raw_text": "\n".join(raw_texts),
        }

        self._log(
            f"提取完成：共 {len(all_lines)} 行文字，"
            f"{len(sessions)} 个场次，{len(tickets)} 个票档"
        )
        return result

    # ── 图像模板匹配 ──────────────────────────────────────────────────

    def click_by_template(
        self,
        template_name: str,
        threshold: float = 0.7,
        scroll: bool = False,
        max_swipes: int = 5,
    ) -> bool:
        """通过图像模板匹配查找并点击按钮/图标。

        补充 OCR 对纯图标按钮（返回箭头、关闭×、底部 tab 图标等）的盲区。
        使用 OpenCV matchTemplate 在当前屏幕截图中匹配预存模板。

        Args:
            template_name: 模板名称（不含路径和扩展名），如 "btn_reserved"
                           对应 templates/btn_reserved.png
            threshold: 匹配置信度阈值（0~1），越高越严格
            scroll: 未找到时是否自动滑动查找
            max_swipes: 最大滑动次数

        Returns:
            是否成功找到并点击
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            print("⚠️ 缺少 OpenCV 依赖，无法使用模板匹配。pip install opencv-python")
            return False

        if Image is None:
            print("⚠️ 缺少 Pillow 依赖")
            return False

        # 查找模板文件
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        template_path = os.path.join(template_dir, f"{template_name}.png")

        if not os.path.isfile(template_path):
            # 尝试其他扩展名
            for ext in [".jpg", ".jpeg", ".bmp"]:
                alt_path = os.path.join(template_dir, f"{template_name}{ext}")
                if os.path.isfile(alt_path):
                    template_path = alt_path
                    break
            else:
                print(f"⚠️ 模板文件不存在：{template_path}")
                return False

        # 读取模板
        template_cv = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template_cv is None:
            print(f"⚠️ 无法读取模板图片：{template_path}")
            return False

        th, tw = template_cv.shape[:2]
        print(f"🔍 模板匹配「{template_name}」({tw}x{th})…")

        # 截取当前屏幕
        img = self.screenshot()
        # PIL → OpenCV 格式
        screen_np = np.array(img)
        screen_cv = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)
        sh, sw = screen_cv.shape[:2]

        # 如果模板比屏幕大，自动缩放模板
        if tw > sw or th > sh:
            scale = min(sw * 0.8 / tw, sh * 0.8 / th)
            new_w, new_h = int(tw * scale), int(th * scale)
            template_cv = cv2.resize(template_cv, (new_w, new_h), interpolation=cv2.INTER_AREA)
            th, tw = template_cv.shape[:2]

        # 模板匹配
        result = cv2.matchTemplate(screen_cv, template_cv, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        if max_val >= threshold:
            # 计算匹配区域的中心坐标
            match_x = max_loc[0] + tw // 2
            match_y = max_loc[1] + th // 2
            print(f"✅ 模板匹配成功（置信度={max_val:.3f}，坐标=({match_x}, {match_y})）")
            self._tap(match_x, match_y)
            return True

        # 当前屏未找到，尝试滑动
        if scroll:
            print(f"  当前屏未找到，开始滑动查找…")
            for i in range(max_swipes):
                self._swipe_up()
                time.sleep(1)

                # 重新截图
                img = self.screenshot()
                screen_np = np.array(img)
                screen_cv = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)

                result = cv2.matchTemplate(screen_cv, template_cv, cv2.TM_CCOEFF_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

                if max_val >= threshold:
                    match_x = max_loc[0] + tw // 2
                    match_y = max_loc[1] + th // 2
                    print(f"✅ 滑动后模板匹配成功（第{i+1}次，置信度={max_val:.3f}）")
                    self._tap(match_x, match_y)
                    return True

        print(f"⚠️ 模板匹配未找到「{template_name}」（最高置信度={max_val:.3f}）")
        return False

    def _swipe_up(self) -> None:
        """向上滑动（查看下方内容）。"""
        w, h = self._get_screen_size()
        swipe_x = int(w * 0.5)
        y_start = int(h * 0.7)
        y_end = int(h * 0.3)
        self._device.swipe(swipe_x, y_start, swipe_x, y_end, duration=0.5)

    # ── 便捷方法 ──────────────────────────────────────────────────────

    def click_reserved_button(self) -> bool:
        """在演出详情页点击底部「已预约」按钮。

        策略：
          1. 先在当前屏 OCR 查找
          2. 没找到则滚动到页面底部查找
          3. 查找关键词：已预约、预约、立即预约

        Returns:
            是否成功点击
        """
        print("📌 OCR 查找「已预约」按钮…")

        # 先在当前屏查找
        for keyword in ["已预约", "预约", "立即预约"]:
            matches = self.ocr_find_texts(keywords=[keyword])
            if matches:
                # 优先选靠近屏幕底部的（「已预约」按钮通常在底部）
                w, h = self._get_screen_size()
                bottom_matches = [m for m in matches if m.center_y > h * 0.6]
                if bottom_matches:
                    best = max(bottom_matches, key=lambda m: m.confidence)
                else:
                    best = max(matches, key=lambda m: m.confidence)

                self._tap(best.center_x, best.center_y)
                print(f"✅ 已点击「{best.text}」({best.center_x}, {best.center_y})")
                return True

        # 滚动到页面底部查找
        print("  当前屏未找到，滚动到页面底部…")

        # 先快速滚到底部
        w, h = self._get_screen_size()
        swipe_x = int(w * self.SWIPE_X_RATIO)
        for _ in range(5):
            start_y = int(h * self.SWIPE_START_Y_RATIO)
            end_y = int(h * self.SWIPE_END_Y_RATIO)
            self.d.shell(f"input swipe {swipe_x} {start_y} {swipe_x} {end_y} {self.SWIPE_DURATION}")
            time.sleep(0.5)

        time.sleep(1)

        # 在底部区域查找
        for keyword in ["已预约", "预约", "立即预约"]:
            matches = self.ocr_find_texts(keywords=[keyword])
            if matches:
                best = max(matches, key=lambda m: m.confidence)
                self._tap(best.center_x, best.center_y)
                print(f"✅ 页面底部找到并点击「{best.text}」({best.center_x}, {best.center_y})")
                return True

        # 最后尝试：滚动查找
        for keyword in ["已预约", "预约"]:
            result = self.scroll_and_find(keyword=keyword, max_swipes=5, swipe_direction="down")
            if result:
                return True

        print("❌ OCR 未找到「已预约」按钮")
        return False

    def extract_sessions_and_tickets(self) -> Dict[str, Any]:
        """提取场次和票档信息（OCR 方式）。

        Returns:
            dict with keys: sessions, tickets, raw_text
        """
        print("📊 OCR 提取场次和票档信息…")

        result = self.smart_scroll_and_extract(max_swipes=15)

        info: Dict[str, Any] = {
            "sessions": result.get("sessions", []),
            "tickets": result.get("tickets", []),
            "raw_text": result.get("raw_text", ""),
        }

        self._log(
            f"OCR 提取到 {len(info['sessions'])} 个场次, {len(info['tickets'])} 个票档"
        )
        return info

    def debug_dump(self, save_dir: str = ".") -> str:
        """调试：截图 + OCR 结果保存到文件。

        Args:
            save_dir: 保存目录

        Returns:
            保存的截图路径
        """
        import json as json_mod

        # 截图
        img = self.screenshot()
        img_path = os.path.join(save_dir, "debug_ocr_screenshot.png")
        img.save(img_path)
        print(f"📸 截图已保存：{img_path}")

        # OCR
        lines = self.extract_all_text(image=img)

        # 保存 OCR 结果
        ocr_path = os.path.join(save_dir, "debug_ocr_result.json")
        ocr_data = [
            {
                "text": l.text,
                "confidence": round(l.confidence, 3),
                "bbox": list(l.bbox),
                "center": [l.center_x, l.center_y],
            }
            for l in lines
        ]
        with open(ocr_path, "w", encoding="utf-8") as f:
            json_mod.dump(ocr_data, f, ensure_ascii=False, indent=2)
        print(f"📝 OCR 结果已保存：{ocr_path}（{len(lines)} 行）")

        # 在截图上标注
        try:
            from PIL import ImageDraw, ImageFont
            draw = ImageDraw.Draw(img)
            for l in lines:
                left, top, right, bottom = l.bbox
                # 画框
                draw.rectangle([left, top, right, bottom], outline="red", width=2)
                # 标注文字
                draw.text((left, top - 12), f"{l.text} ({l.confidence:.2f})", fill="red")
            annotated_path = os.path.join(save_dir, "debug_ocr_annotated.png")
            img.save(annotated_path)
            print(f"🖼️ 标注截图已保存：{annotated_path}")
        except Exception as e:
            self._log(f"标注截图失败：{e}")

        return img_path


# ─── 独立测试 ──────────────────────────────────────────────────────────────

def _test() -> None:
    """独立测试：截图 + OCR + 点击。"""
    try:
        import uiautomator2 as u2
    except ImportError:
        sys.stderr.write("缺少 uiautomator2，请执行：pip install uiautomator2\n")
        sys.exit(1)

    print("╔════════════════════════════════════════════════╗")
    print("║  OCR + 坐标点击 模块测试                        ║")
    print("╚════════════════════════════════════════════════╝")
    print()

    # 连接设备
    print("📱 连接设备…")
    d = u2.connect()
    print(f"✅ 已连接：{d.serial}")

    # 创建 helper
    helper = OcrClickHelper(device=d, verbose=True)

    # 截图 + OCR
    print("\n--- 截图 + OCR ---")
    img = helper.screenshot()
    print(f"截图尺寸：{img.size}")

    lines = helper.extract_all_text()
    print(f"\n识别到 {len(lines)} 行文字：")
    for i, line in enumerate(lines[:30]):
        print(f"  [{i}] {line.text}  (置信度: {line.confidence:.3f}, 位置: {line.center_x},{line.center_y})")
    if len(lines) > 30:
        print(f"  … 还有 {len(lines) - 30} 行省略")

    # 调试转储
    print("\n--- 调试转储 ---")
    helper.debug_dump()

    # 查找关键词测试
    print("\n--- 关键词查找测试 ---")
    for keyword in ["我的", "首页", "预约", "搜索", "登录"]:
        matches = helper.ocr_find_texts(keywords=[keyword])
        if matches:
            best = max(matches, key=lambda m: m.confidence)
            print(f"  「{keyword}」→ 找到：{best.text} @ ({best.center_x}, {best.center_y})")
        else:
            print(f"  「{keyword}」→ 未找到")

    print("\n✅ 测试完成")


if __name__ == "__main__":
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="大麦 OCR + 坐标点击辅助模块")
    parser.add_argument("--test", action="store_true", help="运行独立测试")
    parser.add_argument("--debug", action="store_true", help="调试模式：截图+OCR+标注")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.test or args.debug:
        _test()
    else:
        parser.print_help()
