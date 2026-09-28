"""钓点管理器。

职责：
- 加载 map_data.json 中的 fishing_spots 为 FishingSpot
- 管理钓点状态 (AVAILABLE/DEPLETED/UNREACHABLE/...)
- 根据路径成本 + 历史成功率选择目标钓点 (不按直线距离)
- 不修改 map_data.json，运行时状态独立存储
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from .models import (
    FishingSpot,
    ForbiddenZone,
    NavigationHint,
    SeaLevel,
    SpotStatus,
)

logger = logging.getLogger("spots.manager")


class SpotManager:
    def __init__(self, map_data_path: str, runtime_path: str = "runtime_state.json"):
        self._map_data_path = map_data_path
        self._runtime_path = runtime_path
        self._spots: Dict[str, FishingSpot] = {}
        self._hints: List[NavigationHint] = []
        self._forbidden_zones: List[ForbiddenZone] = []
        self._sea_levels: List[SeaLevel] = []
        self._water_jump_threshold: Optional[float] = None
        self._current_spot_id: Optional[str] = None
        self._load_map_data()
        self._load_runtime()

    # ---------- 数据加载 ----------
    def _load_map_data(self) -> None:
        with open(self._map_data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 当前只用第一张非空地图 (可扩展为按选择加载)
        maps = data.get("maps", {})
        for map_name, m in maps.items():
            spots = m.get("fishing_spots", [])
            if not spots:
                continue
            self._water_jump_threshold = m.get("water_jump_threshold")
            for i, s in enumerate(spots):
                sid = f"{map_name}_{i}"
                self._spots[sid] = FishingSpot(
                    id=sid,
                    target_x=float(s["x"]),
                    target_y=float(s["y"]),
                    target_z=float(s["z"]),
                )
            # 中转点 / 特殊点 -> NavigationHint
            for idx, v in enumerate(m.get("via_stations", []) or []):
                self._hints.append(NavigationHint(
                    id=f"{map_name}_via_{idx}", x=v["x"], y=v["y"], z=v["z"], hint_type="via"))
            for idx, sp in enumerate(m.get("special_spots", []) or []):
                self._hints.append(NavigationHint(
                    id=f"{map_name}_sp_{idx}", x=sp[0], y=sp[1], z=sp[2], hint_type="special"))
            for sv in m.get("sub_via_stations", []) or []:
                self._hints.append(NavigationHint(
                    id=sv["id"], x=sv["x"], y=sv["y"], z=sv["z"], hint_type="sub_via",
                    linked_spot_ids=tuple(str(x) for x in sv.get("spots", []))))
            via_rule = m.get("via_rule")
            if via_rule and via_rule.get("mode") == "chain":
                for node in via_rule.get("chain", []):
                    self._hints.append(NavigationHint(
                        id=node["id"], x=node["x"], y=node["y"], z=node["z"], hint_type="chain"))
            # 禁区
            for fz in m.get("forbidden_zones", []) or []:
                self._forbidden_zones.append(ForbiddenZone(
                    id=fz.get("id", f"fz_{len(self._forbidden_zones)}"),
                    x1=fz["x1"], z1=fz["z1"], x2=fz["x2"], z2=fz["z2"],
                    min_y=fz.get("min_y"), max_y=fz.get("max_y")))
            # 海平面
            for sl in m.get("sea_levels", []) or []:
                self._sea_levels.append(SeaLevel(
                    id=sl["id"], y=sl["y"],
                    rects=tuple(tuple(r) for r in sl.get("rects", []))))
            break  # 只加载第一张有钓点的地图

        logger.info(f"加载钓点 {len(self._spots)} 个, 中转点 {len(self._hints)} 个, "
                    f"禁区 {len(self._forbidden_zones)} 个")

    def _load_runtime(self) -> None:
        """加载运行时状态 (枯竭/不可达/历史)，不污染 map_data.json。"""
        if not os.path.exists(self._runtime_path):
            return
        try:
            with open(self._runtime_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            for sid, meta in rt.get("spots", {}).items():
                spot = self._spots.get(sid)
                if spot is None:
                    continue
                spot.status = SpotStatus(meta.get("status", "unknown"))
                spot.last_visited = meta.get("last_visited")
                spot.visit_count = meta.get("visit_count", 0)
                spot.fail_count = meta.get("fail_count", 0)
                spot.history_success_rate = meta.get("success_rate", 0.0)
        except Exception as e:
            logger.warning(f"运行时状态加载失败: {e}")

    def save_runtime(self) -> None:
        rt = {"spots": {}}
        for sid, spot in self._spots.items():
            rt["spots"][sid] = {
                "status": spot.status.value,
                "last_visited": spot.last_visited,
                "visit_count": spot.visit_count,
                "fail_count": spot.fail_count,
                "success_rate": spot.history_success_rate,
            }
        with open(self._runtime_path, "w", encoding="utf-8") as f:
            json.dump(rt, f, ensure_ascii=False, indent=2)

    # ---------- 查询 ----------
    @property
    def spots(self) -> Dict[str, FishingSpot]:
        return self._spots

    @property
    def current_spot(self) -> Optional[FishingSpot]:
        return self._spots.get(self._current_spot_id) if self._current_spot_id else None

    @property
    def hints(self) -> List[NavigationHint]:
        return self._hints

    @property
    def forbidden_zones(self) -> List[ForbiddenZone]:
        return self._forbidden_zones

    @property
    def sea_levels(self) -> List[SeaLevel]:
        return self._sea_levels

    @property
    def water_jump_threshold(self) -> Optional[float]:
        return self._water_jump_threshold

    def get_spot(self, sid: str) -> Optional[FishingSpot]:
        return self._spots.get(sid)

    # ---------- 状态变更 ----------
    def set_current(self, sid: str) -> None:
        old = self.current_spot
        if old is not None and old.status == SpotStatus.CURRENT:
            old.status = SpotStatus.AVAILABLE
        self._current_spot_id = sid
        spot = self._spots.get(sid)
        if spot is not None:
            spot.status = SpotStatus.CURRENT
            spot.last_visited = time.time()
            spot.visit_count += 1
            logger.info(f"[SPOT] 当前钓点: {sid} ({spot.target_x:.1f},{spot.target_y:.1f},{spot.target_z:.1f})")

    def mark_depleted(self, sid: str) -> None:
        spot = self._spots.get(sid)
        if spot:
            spot.status = SpotStatus.DEPLETED
            logger.info(f"[SPOT] {sid} 标记为 DEPLETED")

    def mark_unreachable(self, sid: str) -> None:
        spot = self._spots.get(sid)
        if spot:
            spot.status = SpotStatus.UNREACHABLE
            spot.fail_count += 1
            logger.warning(f"[SPOT] {sid} 标记为 UNREACHABLE")

    def mark_cooldown(self, sid: str) -> None:
        spot = self._spots.get(sid)
        if spot:
            spot.status = SpotStatus.COOLDOWN

    def reset_status(self, sid: str) -> None:
        spot = self._spots.get(sid)
        if spot:
            spot.status = SpotStatus.AVAILABLE

    # ---------- 钓点选择 ----------
    def select_target(
        self,
        player_x: float,
        player_z: float,
        path_cost_fn=None,
        exclude_distance: float = 5.0,
    ) -> Optional[FishingSpot]:
        """选择目标钓点。

        不按欧氏距离。综合考虑：
        - 状态过滤 (AVAILABLE)
        - 排除当前枯竭点 (exclude_distance)
        - 路径成本 (path_cost_fn 提供，无则用直线距离*惩罚系数)
        - 历史成功率
        - 失败次数惩罚
        """
        cur = self.current_spot
        candidates: List[Tuple[float, FishingSpot]] = []
        for spot in self._spots.values():
            if spot.status not in (SpotStatus.AVAILABLE, SpotStatus.UNKNOWN):
                continue
            if cur is not None and spot.id == cur.id:
                continue
            dist = spot.distance_xz_to(player_x, player_z)
            if dist < exclude_distance:
                continue
            # 路径成本: 优先用 Pathfinder 实际评估，否则直线距离 * 1.3 (估计绕路)
            if path_cost_fn is not None:
                cost = path_cost_fn(spot)
                if cost is None:
                    continue  # 不可达
            else:
                cost = dist * 1.3
            # 失败惩罚: 每次失败 *20% 成本
            fail_penalty = 1.0 + spot.fail_count * 0.2
            # 历史成功率加成: 成功率高 -> 成本低
            success_bonus = 1.0 - spot.history_success_rate * 0.3
            score = cost * fail_penalty * success_bonus
            candidates.append((score, spot))

        if not candidates:
            logger.warning("[SPOT] 无可用候选钓点")
            return None
        candidates.sort(key=lambda x: x[0])
        best_score, best = candidates[0]
        logger.info(
            f"[SPOT] 选中 {best.id} 位置=({best.target_x:.1f},{best.target_y:.1f},{best.target_z:.1f}) "
            f"综合成本={best_score:.1f} (直线距离={best.distance_xz_to(player_x, player_z):.1f})"
        )
        return best
