"""导航引擎 (Navigation Engine)。

编排：
SpotManager 选目标 → DynamicPositioning 算站位 → ClientBridge.goto 寻路
→ ArrivalVerifier 二次验证 → 失败时 RecoveryEngine 分级恢复

阶段 1: 实现编排骨架。实际 Pathfinder 寻路由 ClientBridge 负责 (阶段 2+)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from client.bridge import ClientBridge
from client.player_state import NavState, PlayerState
from client.world_query import WorldQuery
from navigation.position import DynamicPositioning, StandCandidate
from navigation.recovery import RecoveryEngine, RecoveryAction, StuckLevel
from navigation.route_memory import RouteMemory
from spots.manager import SpotManager
from spots.models import FishingSpot

logger = logging.getLogger("nav.engine")


class NavState2(str, Enum):
    IDLE = "idle"
    SELECTING = "selecting"
    POSITIONING = "positioning"
    NAVIGATING = "navigating"
    VERIFYING = "verifying"
    AIMING = "aiming"
    RECOVERY = "recovery"
    UNREACHABLE = "unreachable"
    ARRIVED = "arrived"
    FAILED = "failed"


@dataclass
class NavResult:
    success: bool
    state: NavState2
    spot_id: Optional[str] = None
    stand: Optional[Tuple[float, float, float]] = None
    reason: str = ""


class ArrivalVerifier:
    """到达验证：距离/Y/可站立/视线/视角。"""

    def __init__(self, wq: WorldQuery, arrive_dist: float = 1.5, height_tol: float = 1.0):
        self._wq = wq
        self._arrive_dist = arrive_dist
        self._height_tol = height_tol

    def verify(
        self,
        state: PlayerState,
        target_stand: Tuple[float, float, float],
        target_spot: Tuple[float, float, float],
    ) -> Tuple[bool, str]:
        sx, sy, sz = state.x, state.y, state.z
        tx, ty, tz = target_stand
        # 1. 距离站位
        dist = ((sx - tx) ** 2 + (sz - tz) ** 2) ** 0.5
        if dist > self._arrive_dist:
            return False, f"距离站位 {dist:.1f} > {self._arrive_dist}"
        # 2. Y 合理
        if abs(sy - ty) > self._height_tol:
            return False, f"Y 偏差 {abs(sy - ty):.1f}"
        # 3. 可站立
        if not self._wq.is_standable(sx, sy, sz):
            return False, "站位不可站立"
        # 4. 视线看钓点
        if not self._wq.has_line_of_sight(sx, sy + 1.6, sz, *target_spot):
            return False, "看不到钓点"
        return True, "OK"


class NavigationEngine:
    def __init__(
        self,
        bridge: ClientBridge,
        world_query: WorldQuery,
        spot_manager: SpotManager,
        route_memory: RouteMemory,
    ):
        self._bridge = bridge
        self._wq = world_query
        self._spots = spot_manager
        self._route_mem = route_memory
        self._positioning = DynamicPositioning(world_query, spot_manager.forbidden_zones)
        self._recovery = RecoveryEngine()
        self._verifier = ArrivalVerifier(world_query)
        self._state = NavState2.IDLE
        self._current_spot: Optional[FishingSpot] = None
        self._current_stand: Optional[StandCandidate] = None
        self._stand_candidates: list = []
        self._stand_idx = 0
        self._start_time = 0.0

    @property
    def state(self) -> NavState2:
        return self._state

    def emergency_stop(self) -> None:
        self._bridge.stop_navigation()
        self._recovery.reset()
        self._state = NavState2.IDLE
        logger.info("[NAV] 紧急停止")

    def relocate(self) -> NavResult:
        """换池主流程。被 FishingEngine 在 DEPLETED 后调用。"""
        self._state = NavState2.SELECTING
        player = self._bridge.get_player_state()
        if player is None:
            return NavResult(False, NavState2.FAILED, reason="无法获取玩家状态")

        # 1. 标记当前钓点枯竭
        cur = self._spots.current_spot
        if cur is not None:
            self._spots.mark_depleted(cur.id)

        # 2. 选目标钓点 (路径成本由 Pathfinder 评估，无则直线*1.3)
        path_cost_fn = lambda spot: self._estimate_path_cost(player, spot)
        target = self._spots.select_target(player.x, player.z, path_cost_fn)
        if target is None:
            self._state = NavState2.UNREACHABLE
            return NavResult(False, NavState2.UNREACHABLE, reason="无可用钓点")
        self._current_spot = target
        logger.info(f"[NAV] 目标钓点 {target.id}")

        # 3. 动态站位
        self._state = NavState2.POSITIONING
        stand = self._positioning.pick_best(*target.target_position, path_cost_fn=None)
        if stand is None:
            return NavResult(False, NavState2.FAILED, reason="无合法站位")
        self._current_stand = stand
        logger.info(f"[POSITION] 最终站位=({stand.x:.1f},{stand.y:.1f},{stand.z:.1f}) "
                    f"距离钓点={stand.distance_to_target:.1f}")

        # 4. 寻路
        self._spots.set_current(target.id)
        return self._navigate_to_stand(stand)

    def _navigate_to_stand(self, stand: StandCandidate) -> NavResult:
        self._state = NavState2.NAVIGATING
        self._recovery.reset()
        self._start_time = time.time()
        stand_pos = (stand.x, stand.y, stand.z)
        logger.info(f"[NAV] 开始导航到站位 {stand_pos}")

        # 阶段 1: 这里不执行实际寻路循环 (由阶段 3 实现)。
        # 仅通过 bridge.goto 发起，等待客户端 Pathfinder。
        ok = self._bridge.goto(stand.x, stand.y, stand.z)
        if not ok:
            return NavResult(False, NavState2.FAILED, reason="Pathfinder 不可用")

        # 占位: 实际的循环到达验证在阶段 3 实现
        self._state = NavState2.ARRIVED
        return NavResult(True, NavState2.ARRIVED, spot_id=self._current_spot.id,
                         stand=stand_pos, reason="导航完成 (阶段1占位)")

    def _estimate_path_cost(self, player: PlayerState, spot: FishingSpot) -> float:
        """估算路径成本。阶段 1 用直线*1.3，阶段 3 用 Pathfinder 实际评估。"""
        return spot.distance_xz_to(player.x, player.z) * 1.3
