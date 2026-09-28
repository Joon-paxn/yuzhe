"""vision/water.py
水域检测：OpenCV HSV 阈值分割 + 形态学清理 + 连通域分析。

HSV 使用 OpenCV 标度 (H:0-179, S/V:0-255)。
默认阈值针对青蓝色调水域，光照/资源包不同时需校准 config.vision。
"""
from __future__ import annotations

from typing import List, Tuple

import cv2
import numpy as np

from config import VisionConfig
from vision.models import WaterRegion


class WaterDetector:
    """水域检测器 (无状态，可被任意帧调用)"""

    def __init__(self, cfg: VisionConfig) -> None:
        self._cfg = cfg

    def detect(self, frame: np.ndarray) -> Tuple[List[WaterRegion], np.ndarray]:
        """
        检测画面中的水域连通域。
        返回 (水域列表, 水域二值掩码)。
        """
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        cfg = self._cfg

        # 色相区间可能跨过 180 边界，这里简单用单区间 (本服水色 hue 85-110 不跨界)
        lower = np.array([cfg.water_h_low, cfg.water_s_low, cfg.water_v_low], dtype=np.uint8)
        upper = np.array([cfg.water_h_high, cfg.water_s_high, cfg.water_v_high], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)

        # 形态学清理：开运算去噪点，闭运算填小洞
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

        regions = self._connected_components(mask, gray, w, h)
        return regions, mask

    def _connected_components(
        self, mask: np.ndarray, gray: np.ndarray, frame_w: int, frame_h: int
    ) -> List[WaterRegion]:
        """连通域分析，过滤小面积 + 平滑天空 (纹理标准差过低) 噪点"""
        num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        regions: List[WaterRegion] = []
        min_area = self._cfg.min_water_area
        min_std = self._cfg.min_texture_std
        for i in range(1, num):  # 0 是背景
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < min_area:
                continue
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            bw = int(stats[i, cv2.CC_STAT_WIDTH])
            bh = int(stats[i, cv2.CC_STAT_HEIGHT])
            # 纹理过滤：水面有方块网格纹理，标准差较高；平滑天空标准差低 → 拒绝
            comp_pixels = gray[labels == i]
            if comp_pixels.size > 0:
                std = float(comp_pixels.std())
            else:
                std = 0.0
            if std < min_std:
                continue
            # 置信度：面积占 bbox 比例 (越饱满越可信) + 纹理强度
            bbox_area = max(1, bw * bh)
            fill = area / bbox_area
            tex_score = min(1.0, std / 60.0)
            conf = min(1.0, 0.6 * fill + 0.4 * tex_score)
            regions.append(WaterRegion(
                x=x, y=y, width=bw, height=bh, area=area, confidence=conf,
                norm_x=x / frame_w, norm_y=y / frame_h,
                norm_w=bw / frame_w, norm_h=bh / frame_h,
            ))
        # 按面积降序
        regions.sort(key=lambda r: r.area, reverse=True)
        return regions
