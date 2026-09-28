"""vision/models.py
视觉识别层统一数据接口 (阶段四)

设计目标：上层寻路/导航逻辑只依赖本模块的数据结构，
底层实现可自由替换 (OpenCV 传统图像处理 / 目标检测 / 分割模型)，
无需修改 navigation 层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class TerrainClass(Enum):
    """地形分类"""
    WATER = "water"          # 水域
    GRASS = "grass"          # 草地 (可通行)
    SAND = "sand"            # 沙/路径 (可通行)
    STONE = "stone"          # 石头/石砖 (障碍或可踩)
    WOOD = "wood"            # 木头/树干
    UNKNOWN = "unknown"      # 未知


@dataclass
class Region:
    """屏幕坐标区域 (像素)，用于水域/障碍/可通行区域"""
    x: int
    y: int
    width: int
    height: int
    area: int = 0                       # 连通域像素面积
    confidence: float = 1.0             # 置信度 [0,1]

    @property
    def cx(self) -> float:
        return self.x + self.width / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.height / 2.0

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class WaterRegion(Region):
    """水域连通域，附带归一化坐标 (相对画面，便于跨分辨率复用)"""
    norm_x: float = 0.0                 # x/画面宽
    norm_y: float = 0.0                 # y/画面高
    norm_w: float = 0.0
    norm_h: float = 0.0


@dataclass
class CandidateSpot:
    """候选钓点：足够大的水域连通域"""
    region: WaterRegion
    confidence: float = 0.5             # 综合置信度
    has_marker: bool = False            # 是否检测到「钓点」文字标记 (OCR 关联)


@dataclass
class VisionResult:
    """
    视觉识别统一输出。上层 navigation 只读此结构。
    所有坐标默认为像素坐标 (相对传入 frame)。
    """
    water: List[WaterRegion] = field(default_factory=list)
    walkable: List[Region] = field(default_factory=list)         # 可通行区域 (草地/沙)
    obstacles: List[Region] = field(default_factory=list)        # 障碍 (石头/墙/树干)
    elevation: List[Region] = field(default_factory=list)        # 高低差/台阶 (阶段四暂留接口)
    candidate_fishing_spots: List[CandidateSpot] = field(default_factory=list)
    frame_size: Tuple[int, int] = (0, 0)                          # (宽,高)
    elapsed_ms: float = 0.0

    def summary(self) -> str:
        return (
            f"水域={len(self.water)} 可通行={len(self.walkable)} "
            f"障碍={len(self.obstacles)} 候选钓点={len(self.candidate_fishing_spots)} "
            f"耗时={self.elapsed_ms:.1f}ms"
        )
