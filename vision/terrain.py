"""vision/terrain.py
地形分类：基于 HSV 区分草地/沙/石头/木头，识别可通行区域与障碍。

阶段四为基础实现，仅做颜色分类 + 连通域。
高低差/悬崖/台阶 (elevation) 暂留接口，待后续阶段用深度/边缘分析填充。
"""
from __future__ import annotations

from typing import List, Tuple

import cv2
import numpy as np

from config import VisionConfig
from vision.models import Region, TerrainClass


class TerrainAnalyzer:
    """地形分析器 (无状态)"""

    def __init__(self, cfg: VisionConfig) -> None:
        self._cfg = cfg

    def analyze(
        self, frame: np.ndarray, water_mask: np.ndarray
    ) -> Tuple[List[Region], List[Region], List[Region]]:
        """
        返回 (walkable, obstacles, elevation)。
        walkable: 草地/沙等可通行区域；obstacles: 石头/树干等障碍；
        elevation: 暂为空 (接口预留)。
        water_mask 用于排除已被识别为水域的像素。
        """
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 草地：绿色 hue 35-85 (OpenCV 标度)，中等饱和
        grass_mask = cv2.inRange(
            hsv, np.array([35, 40, 30]), np.array([85, 255, 220])
        )
        # 沙/路径：低饱和黄白
        sand_mask = cv2.inRange(
            hsv, np.array([15, 30, 120]), np.array([35, 130, 230])
        )
        # 石头：低饱和灰
        stone_mask = cv2.inRange(
            hsv, np.array([0, 0, 60]), np.array([180, 60, 180])
        )

        # 排除水域像素
        non_water = cv2.bitwise_not(water_mask)
        grass_mask = cv2.bitwise_and(grass_mask, non_water)
        sand_mask = cv2.bitwise_and(sand_mask, non_water)
        stone_mask = cv2.bitwise_and(stone_mask, non_water)

        walkable = self._components(grass_mask, w, h) + self._components(sand_mask, w, h)
        obstacles = self._components(stone_mask, w, h, min_area=self._cfg.min_obstacle_area)
        elevation: List[Region] = []  # 阶段四预留
        return walkable, obstacles, elevation

    def _components(
        self, mask: np.ndarray, frame_w: int, frame_h: int, min_area: int = 200
    ) -> List[Region]:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out: List[Region] = []
        for i in range(1, num):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < min_area:
                continue
            out.append(Region(
                x=int(stats[i, cv2.CC_STAT_LEFT]),
                y=int(stats[i, cv2.CC_STAT_TOP]),
                width=int(stats[i, cv2.CC_STAT_WIDTH]),
                height=int(stats[i, cv2.CC_STAT_HEIGHT]),
                area=area,
            ))
        return out
