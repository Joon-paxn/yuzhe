"""vision/detector.py
视觉识别统一入口。上层 navigation 只调用 VisionDetector.detect(frame)。

组装水域检测 + 地形分析，输出 VisionResult。
钓点候选 = 足够大的水域连通域；置信度随面积/饱满度提升，
若关联 OCR「钓点」文字标记则 has_marker=True (由上层关联，阶段四仅留接口)。
"""
from __future__ import annotations

import time
from typing import List, Optional

import numpy as np

from config import VisionConfig
from vision.models import CandidateSpot, VisionResult, WaterRegion
from vision.water import WaterDetector
from vision.terrain import TerrainAnalyzer


class VisionDetector:
    """视觉识别层统一入口 (无状态，处理单帧)"""

    def __init__(self, cfg: VisionConfig) -> None:
        self._cfg = cfg
        self._water = WaterDetector(cfg)
        self._terrain = TerrainAnalyzer(cfg) if cfg.analyze_terrain else None

    def detect(self, frame: np.ndarray) -> VisionResult:
        """对一帧画面进行视觉识别，返回 VisionResult"""
        t0 = time.monotonic()
        h, w = frame.shape[:2]
        water_regions, water_mask = self._water.detect(frame)

        walkable: List = []
        obstacles: List = []
        elevation: List = []
        if self._terrain is not None:
            walkable, obstacles, elevation = self._terrain.analyze(frame, water_mask)

        candidates = self._candidate_spots(water_regions, w, h)

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return VisionResult(
            water=water_regions,
            walkable=walkable,
            obstacles=obstacles,
            elevation=elevation,
            candidate_fishing_spots=candidates,
            frame_size=(w, h),
            elapsed_ms=elapsed_ms,
        )

    def _candidate_spots(
        self, water: List[WaterRegion], frame_w: int, frame_h: int
    ) -> List[CandidateSpot]:
        """从水域连通域生成候选钓点 (面积越大、越饱满越可信)"""
        candidates: List[CandidateSpot] = []
        for r in water:
            # 置信度：面积归一化 (相对画面) 的对数缩放 + 饱满度
            area_ratio = r.area / max(1, frame_w * frame_h)
            size_score = min(1.0, area_ratio * 20.0)        # 占画面 5% 即满分
            conf = 0.5 * size_score + 0.5 * r.confidence
            candidates.append(CandidateSpot(region=r, confidence=conf, has_marker=False))
        return candidates
