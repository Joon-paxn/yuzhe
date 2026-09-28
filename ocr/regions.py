"""
ocr/regions.py
--------------
OCR ROI 配置。

ROI 以相对于 Minecraft 窗口的归一化坐标保存 (x, y, width, height ∈ [0,1])，
窗口移动 / 缩放时自动重新计算屏幕坐标，适配不同分辨率与窗口位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from minecraft_window import WindowRect


@dataclass
class OcrRoiConfig:
    """OCR 检测区域 (相对 Minecraft 窗口比例 0~1)"""

    x: float = 0.25
    y: float = 0.55
    width: float = 0.50
    height: float = 0.15

    def to_screen(self, win_rect: WindowRect) -> Optional[Tuple[int, int, int, int]]:
        """将归一化 ROI 转为屏幕像素坐标 (x, y, w, h)。窗口无效返回 None。"""
        if win_rect.width <= 0 or win_rect.height <= 0:
            return None
        x = win_rect.left + int(win_rect.width * self.x)
        y = win_rect.top + int(win_rect.height * self.y)
        w = max(1, int(win_rect.width * self.width))
        h = max(1, int(win_rect.height * self.height))
        # clamp 到窗口范围内
        if x < win_rect.left:
            x = win_rect.left
        if y < win_rect.top:
            y = win_rect.top
        if x + w > win_rect.right:
            w = win_rect.right - x
        if y + h > win_rect.bottom:
            h = win_rect.bottom - y
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def clamp(self) -> None:
        """将各分量限制在 [0, 1]"""
        for attr in ("x", "y", "width", "height"):
            v = getattr(self, attr)
            if v < 0.0:
                setattr(self, attr, 0.0)
            elif v > 1.0:
                setattr(self, attr, 1.0)
