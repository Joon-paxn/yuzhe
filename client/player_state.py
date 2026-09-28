"""玩家状态数据模型。统一通过 get_player_state() 获取，上层不直接操作 OCR/剪贴板。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class NavState(str, Enum):
    """客户端导航状态 (由 Pathfinder 上报)。"""

    IDLE = "idle"
    PATHFINDING = "pathfinding"
    WALKING = "walking"
    PARKOURING = "parkouring"
    JUMPING = "jumping"
    SWIMMING = "swimming"
    ASCENDING = "ascending"
    DESCENDING = "descending"
    STUCK = "stuck"
    ARRIVED = "arrived"
    FAILED = "failed"


@dataclass
class PlayerState:
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    dimension: str = "overworld"
    on_ground: bool = True
    in_water: bool = False
    navigation_state: NavState = NavState.IDLE
    health: Optional[float] = None
    food: Optional[float] = None
    timestamp: float = 0.0

    def distance_xz_to(self, x: float, z: float) -> float:
        dx = self.x - x
        dz = self.z - z
        return (dx * dx + dz * dz) ** 0.5

    def distance_xyz_to(self, x: float, y: float, z: float) -> float:
        dx = self.x - x
        dy = self.y - y
        dz = self.z - z
        return (dx * dx + dy * dy + dz * dz) ** 0.5
