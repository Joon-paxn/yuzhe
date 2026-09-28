"""动态站位 (Dynamic Positioning)。

对于目标钓点 T=(x,y,z)，在 T 周围生成多个候选站位，
逐个检查合法性并评分，选出最适合钓鱼的站位。

不修改 map_data.json，候选站位纯运行时计算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from client.world_query import WorldQuery
from spots.models import ForbiddenZone


@dataclass
class StandCandidate:
    x: float
    y: float
    z: float
    distance_to_target: float = 0.0
    height_diff: float = 0.0
    is_standable: bool = True
    has_los: bool = True
    obstacle_count: int = 0
    in_forbidden: bool = False
    path_cost: float = 0.0
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return (self.is_standable and self.has_los
                and not self.in_forbidden and self.obstacle_count <= 2)


class DynamicPositioning:
    """动态站位生成与评分。"""

    def __init__(
        self,
        world_query: WorldQuery,
        forbidden_zones: Optional[List[ForbiddenZone]] = None,
        min_distance: float = 2.0,
        max_distance: float = 6.0,
        height_tolerance: float = 2.0,
        angle_step_deg: float = 30.0,
        heights: Optional[Tuple[float, ...]] = None,
    ):
        self._wq = world_query
        self._forbidden = forbidden_zones or []
        self._min_d = min_distance
        self._max_d = max_distance
        self._height_tol = height_tolerance
        self._angle_step = angle_step_deg
        self._heights = heights or (-1.0, 0.0, 1.0)

    def generate_candidates(
        self,
        target_x: float, target_y: float, target_z: float,
        path_cost_fn=None,
    ) -> List[StandCandidate]:
        """在目标周围生成候选站位。"""
        candidates: List[StandCandidate] = []
        angle = 0.0
        while angle < 360.0:
            rad = math.radians(angle)
            for d in self._frange(self._min_d, self._max_d + 0.01, 1.0):
                cx = target_x + math.cos(rad) * d
                cz = target_z + math.sin(rad) * d
                for dh in self._heights:
                    cy = target_y + dh
                    cand = self._evaluate(
                        cx, cy, cz, target_x, target_y, target_z, path_cost_fn)
                    candidates.append(cand)
            angle += self._angle_step
        valid = [c for c in candidates if c.valid]
        valid.sort(key=lambda c: c.score, reverse=True)
        return valid

    def _evaluate(
        self,
        cx: float, cy: float, cz: float,
        tx: float, ty: float, tz: float,
        path_cost_fn=None,
    ) -> StandCandidate:
        dist = math.hypot(cx - tx, cz - tz)
        hdiff = abs(cy - ty)
        cand = StandCandidate(
            x=cx, y=cy, z=cz,
            distance_to_target=dist,
            height_diff=hdiff,
        )
        # 1. 脚下可站立
        cand.is_standable = self._wq.is_standable(cx, cy, cz)
        if not cand.is_standable:
            cand.reasons.append("不可站立")
        # 2. 视线
        cand.has_los = self._wq.has_line_of_sight(cx, cy + 1.6, cz, tx, ty, tz)
        if not cand.has_los:
            cand.reasons.append("视线阻挡")
        # 3. 投竿障碍
        cand.obstacle_count = self._wq.count_obstacles(cx, cy + 1.6, cz, tx, ty, tz)
        # 4. 禁区
        cand.in_forbidden = any(fz.contains(cx, cz) for fz in self._forbidden)
        if cand.in_forbidden:
            cand.reasons.append("禁区内")
        # 5. 高度差
        if hdiff > self._height_tol:
            cand.reasons.append("高度差过大")
        # 6. 路径成本
        cand.path_cost = path_cost_fn(cx, cy, cz) if path_cost_fn else dist

        # 评分 (越低越好的因素取负)
        score = 0.0
        score -= cand.path_cost * 0.4           # 路径成本
        score -= abs(dist - 4.0) * 0.2          # 偏好 4 格距离
        score -= hdiff * 0.5                     # 高度差惩罚
        score -= cand.obstacle_count * 1.0       # 障碍惩罚
        cand.score = score
        return cand

    def pick_best(
        self,
        target_x: float, target_y: float, target_z: float,
        path_cost_fn=None,
    ) -> Optional[StandCandidate]:
        cands = self.generate_candidates(target_x, target_y, target_z, path_cost_fn)
        if not cands:
            return None
        best = cands[0]
        return best

    @staticmethod
    def _frange(start: float, stop: float, step: float):
        x = start
        while x <= stop + 1e-6:
            yield x
            x += step
