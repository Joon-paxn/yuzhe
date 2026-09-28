"""
navigation/coordinate_provider.py
---------------------------------
坐标提供者：从 OCR 读取玩家坐标 (x, y, z, yaw, pitch)。

替代 Tau 的 F3+C 剪贴板方案。OCR 读取 F3 调试屏左下角的
"X: ... Y: ... Z: ..." 行和 "Facing: ..." 行。

返回格式: (x, y, z, yaw, pitch)，任一缺失则为 None。
yaw 从 Facing 文本解析 (8 方向精度)，pitch 通常无法从 F3 文本获取 (返回 None)。
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from ocr import OcrService


class CoordinateProvider:
    """从 OCR 缓存读取玩家坐标"""

    def __init__(self, ocr_service: Optional[OcrService]) -> None:
        self._ocr = ocr_service
        self._last_coords: Optional[Tuple] = None
        self._last_ts: float = 0.0

    def get_coords(self) -> Optional[Tuple[float, float, float, Optional[float], Optional[float]]]:
        """
        返回 (x, y, z, yaw, pitch)。
        OCR 不可用或未解析到坐标时返回 None。
        """
        if self._ocr is None:
            return None
        state = self._ocr.get_latest_state()
        if state is None or state.xyz is None:
            return None
        x, y, z = state.xyz
        yaw = state.yaw
        pitch = state.pitch
        result = (x, y, z, yaw, pitch)
        self._last_coords = result
        self._last_ts = time.monotonic()
        return result

    @property
    def last_coords(self) -> Optional[Tuple]:
        return self._last_coords

    def get_xyz(self) -> Optional[Tuple[float, float, float]]:
        """只返回 (x, y, z)"""
        c = self.get_coords()
        return c[:3] if c else None
