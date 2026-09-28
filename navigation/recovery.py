"""卡死恢复引擎 (Recovery Engine)。

持续记录玩家位置/目标/速度，定义 SOFT_STUCK / HARD_STUCK / PATH_FAILED，
分级恢复策略 (Level 1~6)，禁止无限循环。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from client.player_state import PlayerState


class StuckLevel(str, Enum):
    NONE = "none"
    SOFT_STUCK = "soft_stuck"
    HARD_STUCK = "hard_stuck"
    PATH_FAILED = "path_failed"


class RecoveryAction(str, Enum):
    NONE = "none"
    REPLAN_PATH = "replan_path"           # Level 1: 重新规划路径
    RECALC_POSITION = "recalc_position"   # Level 2: 重新计算站位
    REPLAN_TO_WAYPOINT = "replan_waypoint"  # Level 3: 规划到中间节点
    REPLAN_FULL = "replan_full"           # Level 4: 完整重新规划
    SWITCH_STAND = "switch_stand"         # Level 5: 切换备用站位
    MARK_UNREACHABLE = "mark_unreachable" # Level 6: 标记不可达


@dataclass
class NavSample:
    timestamp: float
    x: float
    y: float
    z: float
    target_x: float
    target_y: float
    target_z: float
    distance: float
    velocity: float
    nav_state: str


@dataclass
class RecoveryResult:
    action: RecoveryAction
    reason: str
    new_stand: Optional[Tuple[float, float, float]] = None


class RecoveryEngine:
    def __init__(
        self,
        soft_window: float = 3.0,
        hard_window: float = 8.0,
        min_displacement: float = 0.3,
        max_level: int = 6,
    ):
        self._soft_window = soft_window
        self._hard_window = hard_window
        self._min_disp = min_displacement
        self._max_level = max_level
        self._samples: List[NavSample] = []
        self._current_level = 0
        self._last_target: Optional[Tuple[float, float, float]] = None

    def reset(self) -> None:
        self._samples.clear()
        self._current_level = 0

    def record(self, state: PlayerState, target: Tuple[float, float, float]) -> None:
        dist = state.distance_xyz_to(*target)
        vel = 0.0
        if self._samples:
            prev = self._samples[-1]
            dt = max(1e-6, state.timestamp - prev.timestamp)
            moved = ((state.x - prev.x) ** 2 + (state.z - prev.z) ** 2) ** 0.5
            vel = moved / dt
        self._samples.append(NavSample(
            timestamp=state.timestamp, x=state.x, y=state.y, z=state.z,
            target_x=target[0], target_y=target[1], target_z=target[2],
            distance=dist, velocity=vel, nav_state=state.navigation_state.value,
        ))
        # 只保留最近 hard_window 秒的样本
        cutoff = state.timestamp - self._hard_window - 1
        self._samples = [s for s in self._samples if s.timestamp >= cutoff]

    def evaluate(self) -> StuckLevel:
        if len(self._samples) < 3:
            return StuckLevel.NONE
        now = self._samples[-1].timestamp
        # Soft stuck: soft_window 内位移 < min_displacement
        soft_samples = [s for s in self._samples if s.timestamp >= now - self._soft_window]
        if len(soft_samples) >= 2:
            disp = ((soft_samples[-1].x - soft_samples[0].x) ** 2
                    + (soft_samples[-1].z - soft_samples[0].z) ** 2) ** 0.5
            if disp < self._min_disp:
                return StuckLevel.SOFT_STUCK
        # Hard stuck: hard_window 内无进展且距离未减小
        hard_samples = [s for s in self._samples if s.timestamp >= now - self._hard_window]
        if len(hard_samples) >= 2:
            disp = ((hard_samples[-1].x - hard_samples[0].x) ** 2
                    + (hard_samples[-1].z - hard_samples[0].z) ** 2) ** 0.5
            dist_reduced = hard_samples[0].distance - hard_samples[-1].distance
            if disp < self._min_disp * 2 and dist_reduced < 0.5:
                return StuckLevel.HARD_STUCK
        return StuckLevel.NONE

    def next_action(self, level: StuckLevel) -> RecoveryResult:
        if level == StuckLevel.NONE:
            return RecoveryResult(RecoveryAction.NONE, "正常")
        if level == StuckLevel.PATH_FAILED:
            self._current_level = self._max_level
        else:
            self._current_level += 1
        actions = [
            RecoveryAction.REPLAN_PATH,
            RecoveryAction.RECALC_POSITION,
            RecoveryAction.REPLAN_TO_WAYPOINT,
            RecoveryAction.REPLAN_FULL,
            RecoveryAction.SWITCH_STAND,
            RecoveryAction.MARK_UNREACHABLE,
        ]
        idx = min(self._current_level - 1, len(actions) - 1)
        action = actions[idx]
        reason = f"Level {self._current_level}: {action.value}"
        return RecoveryResult(action, reason)

    @property
    def current_level(self) -> int:
        return self._current_level
