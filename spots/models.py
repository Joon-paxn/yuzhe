"""钓点数据模型与状态机。

钓点 = 钓鱼目标坐标 (来自 map_data.json fishing_spots)。
玩家站位由 navigation.position 动态计算，不写回 map_data.json。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class SpotStatus(str, Enum):
    """钓点状态。DEPLETED 与 UNREACHABLE 必须严格分离。"""

    UNKNOWN = "unknown"          # 从未访问过
    AVAILABLE = "available"      # 可钓鱼
    CURRENT = "current"          # 当前所在钓点
    DEPLETED = "depleted"        # 服务器判定枯竭
    COOLDOWN = "cooldown"        # 冷却中 (短期不可用)
    UNREACHABLE = "unreachable"  # 导航无法到达
    INVALID = "invalid"          # 坐标无效


@dataclass
class FishingSpot:
    """钓鱼目标点。target_position 来自 map_data.json，不修改其含义。"""

    id: str
    target_x: float
    target_y: float
    target_z: float
    status: SpotStatus = SpotStatus.UNKNOWN

    # 运行时元数据 (不写入 map_data.json)
    last_visited: Optional[float] = None
    visit_count: int = 0
    fail_count: int = 0
    history_success_rate: float = 0.0

    @property
    def target_position(self) -> Tuple[float, float, float]:
        return (self.target_x, self.target_y, self.target_z)

    def distance_xz_to(self, x: float, z: float) -> float:
        dx = self.target_x - x
        dz = self.target_z - z
        return (dx * dx + dz * dz) ** 0.5

    def distance_xyz_to(self, x: float, y: float, z: float) -> float:
        dx = self.target_x - x
        dy = self.target_y - y
        dz = self.target_z - z
        return (dx * dx + dy * dy + dz * dz) ** 0.5


@dataclass
class NavigationHint:
    """中转/特殊点抽象 (来自 via_stations / special_spots / sub_via_stations)。
    不是强制导航逻辑，Pathfinder 能直达时优先直达。"""

    id: str
    x: float
    y: float
    z: float
    hint_type: str = "via"  # "via" | "special" | "sub_via" | "chain"
    linked_spot_ids: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ForbiddenZone:
    """禁止区域。来自 map_data.json forbidden_zones。"""

    id: str
    x1: float
    z1: float
    x2: float
    z2: float
    min_y: Optional[float] = None
    max_y: Optional[float] = None

    def contains(self, x: float, z: float) -> bool:
        return min(self.x1, self.x2) <= x <= max(self.x1, self.x2) and \
               min(self.z1, self.z2) <= z <= max(self.z1, self.z2)


@dataclass
class SeaLevel:
    """海平面信息。"""

    id: str
    y: float
    rects: Tuple[Tuple[float, float, float, float], ...] = field(default_factory=tuple)
