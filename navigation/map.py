"""navigation/map.py
世界地图模型 (阶段五)

边探索、边建图。记录：
- 玩家当前位置 (来源可为 OCR XYZ / 相对定位 / 手动，架构允许未来扩展)
- 已知钓点 (used 已用过 / depleted 已枯竭 / confidence 置信度)
- 当前钓点、候选钓点
- 已探索区域、已知水域、障碍

钓点按位置去重 (小于 spot_dedup_distance 视为同一钓点)；
同一钓点枯竭后不再作为候选，避免无限重复使用。
支持 JSON 持久化，跨会话保留探索成果。
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------- 玩家位置 ----------------

@dataclass
class PlayerPosition:
    """玩家世界位置。坐标可能缺失 (无 XYZ 文字时用相对定位)。"""
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    source: str = "unknown"        # "ocr" | "relative" | "manual"
    timestamp: float = 0.0

    @property
    def valid(self) -> bool:
        return self.x is not None and self.z is not None

    @classmethod
    def from_ocr(cls, x: Optional[float], y: Optional[float], z: Optional[float]) -> "PlayerPosition":
        return cls(x=x, y=y, z=z, source="ocr", timestamp=time.monotonic())

    def distance_xz(self, other: "PlayerPosition") -> Optional[float]:
        """XZ 平面距离 (Minecraft 高度 Y 不计入水平距离)"""
        if self.x is None or self.z is None or other.x is None or other.z is None:
            return None
        return math.hypot(self.x - other.x, self.z - other.z)


# ---------------- 钓点 ----------------

@dataclass
class FishingSpot:
    """一个钓点"""
    id: str
    x: float
    y: float
    z: float
    used: bool = False               # 是否已被使用 (到访/作钓过)
    depleted: bool = False           # 是否已枯竭 (OCR 识别「鱼群：枯竭」)
    confidence: float = 0.5          # 置信度 [0,1]
    has_marker: bool = False         # 是否检测到「钓点」文字标记
    last_visited: float = 0.0        # 上次到访时间戳
    visits: int = 0                  # 累计到访次数

    def distance_xz_to(self, x: float, z: float) -> float:
        return math.hypot(self.x - x, self.z - z)


# ---------------- 已探索区域 / 水域 / 障碍 ----------------

@dataclass
class ExploredRegion:
    """XZ 平面已探索矩形区域 (世界坐标)"""
    x1: float
    z1: float
    x2: float
    z2: float
    timestamp: float = 0.0

    def contains(self, x: float, z: float) -> bool:
        return self.x1 <= x <= self.x2 and self.z1 <= z <= self.z2


@dataclass
class WaterArea:
    """已知水域 (世界坐标矩形，来源视觉/OCR)"""
    x1: float
    z1: float
    x2: float
    z2: float
    confidence: float = 0.5
    timestamp: float = 0.0


@dataclass
class Obstacle:
    """已知障碍 (世界坐标)"""
    x: float
    y: float
    z: float
    kind: str = "unknown"            # "stone" | "wall" | "tree" | ...
    timestamp: float = 0.0


# ---------------- 世界地图 ----------------

@dataclass
class WorldMap:
    """
    世界地图容器 + 操作。纯数据层，不依赖截图/输入，供后续阶段寻路/导航使用。
    """
    player: Optional[PlayerPosition] = None
    spots: Dict[str, FishingSpot] = field(default_factory=dict)
    explored: List[ExploredRegion] = field(default_factory=list)
    water_areas: List[WaterArea] = field(default_factory=list)
    obstacles: List[Obstacle] = field(default_factory=list)
    current_spot_id: Optional[str] = None
    # 配置 (运行时注入，不持久化)
    spot_dedup_distance: float = 3.0
    auto_save: bool = True
    persistence_path: str = "world_map.json"

    # ---------- 玩家位置 ----------
    def update_player(self, pos: PlayerPosition) -> None:
        self.player = pos
        self._maybe_save()

    # ---------- 钓点管理 ----------
    def add_spot(
        self, x: float, y: float, z: float,
        confidence: float = 0.5, has_marker: bool = False,
    ) -> str:
        """
        添加钓点。若与已有钓点距离 < spot_dedup_distance 则视为同一钓点，
        合并 (取较高置信度，保留 used/depleted 标记)，返回钓点 id。
        """
        existing = self._find_nearby_spot(x, z, self.spot_dedup_distance)
        if existing is not None:
            # 合并：置信度取大，标记 OR，marker 取真
            if confidence > existing.confidence:
                existing.confidence = confidence
            existing.has_marker = existing.has_marker or has_marker
            self._maybe_save()
            return existing.id
        spot = FishingSpot(
            id=uuid.uuid4().hex[:8], x=x, y=y, z=z,
            confidence=confidence, has_marker=has_marker,
        )
        self.spots[spot.id] = spot
        self._maybe_save()
        return spot.id

    def _find_nearby_spot(self, x: float, z: float, max_dist: float) -> Optional[FishingSpot]:
        for spot in self.spots.values():
            if spot.distance_xz_to(x, z) < max_dist:
                return spot
        return None

    def get_spot(self, spot_id: str) -> Optional[FishingSpot]:
        return self.spots.get(spot_id)

    def mark_used(self, spot_id: str) -> None:
        spot = self.spots.get(spot_id)
        if spot:
            spot.used = True
            spot.visits += 1
            spot.last_visited = time.time()
            self._maybe_save()

    def mark_depleted(self, spot_id: str) -> None:
        spot = self.spots.get(spot_id)
        if spot:
            spot.depleted = True
            self._maybe_save()

    def set_current(self, spot_id: Optional[str]) -> None:
        self.current_spot_id = spot_id
        self._maybe_save()

    def get_current(self) -> Optional[FishingSpot]:
        if self.current_spot_id is None:
            return None
        return self.spots.get(self.current_spot_id)

    def all_spots(self) -> List[FishingSpot]:
        return list(self.spots.values())

    def candidate_spots(self) -> List[FishingSpot]:
        """候选钓点：未枯竭 (used 不排除，可重复到访未枯竭的钓点)"""
        return [s for s in self.spots.values() if not s.depleted]

    def unused_spots(self) -> List[FishingSpot]:
        """未使用且未枯竭的钓点"""
        return [s for s in self.spots.values() if not s.used and not s.depleted]

    def nearest_candidate(
        self, from_pos: Optional[PlayerPosition] = None, max_distance: float = float("inf"),
    ) -> Optional[FishingSpot]:
        """
        从 from_pos (默认玩家位置) 出发，找最近的未枯竭钓点。
        优先未使用过的；无玩家位置时返回置信度最高的候选。
        """
        cands = self.candidate_spots()
        if not cands:
            return None
        pos = from_pos or self.player
        if pos is None or not pos.valid:
            # 无位置信息：优先未使用 + 高置信度
            cands.sort(key=lambda s: (s.used, -s.confidence))
            return cands[0]
        # 按距离排序，过滤超距
        in_range = [s for s in cands if s.distance_xz_to(pos.x, pos.z) <= max_distance]
        if not in_range:
            return None
        in_range.sort(key=lambda s: (s.used, s.distance_xz_to(pos.x, pos.z)))
        return in_range[0]

    # ---------- 探索区域 ----------
    def add_explored(self, x1: float, z1: float, x2: float, z2: float) -> None:
        x1, x2 = min(x1, x2), max(x1, x2)
        z1, z2 = min(z1, z2), max(z1, z2)
        self.explored.append(ExploredRegion(x1, z1, x2, z2, time.time()))
        self._maybe_save()

    def is_explored(self, x: float, z: float) -> bool:
        return any(r.contains(x, z) for r in self.explored)

    # ---------- 水域 / 障碍 ----------
    def add_water(self, x1: float, z1: float, x2: float, z2: float, confidence: float = 0.5) -> None:
        self.water_areas.append(WaterArea(x1, z1, x2, z2, confidence, time.time()))
        self._maybe_save()

    def add_obstacle(self, x: float, y: float, z: float, kind: str = "unknown") -> None:
        self.obstacles.append(Obstacle(x, y, z, kind, time.time()))
        self._maybe_save()

    # ---------- 持久化 ----------
    def _maybe_save(self) -> None:
        if self.auto_save and self.persistence_path:
            try:
                self.save(self.persistence_path)
            except Exception:
                pass  # 持久化失败不阻断主流程

    def to_dict(self) -> dict:
        return {
            "player": asdict(self.player) if self.player else None,
            "spots": {k: asdict(v) for k, v in self.spots.items()},
            "explored": [asdict(r) for r in self.explored],
            "water_areas": [asdict(w) for w in self.water_areas],
            "obstacles": [asdict(o) for o in self.obstacles],
            "current_spot_id": self.current_spot_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorldMap":
        m = cls()
        if d.get("player"):
            m.player = PlayerPosition(**d["player"])
        for sid, sd in (d.get("spots") or {}).items():
            m.spots[sid] = FishingSpot(**sd)
        for rd in (d.get("explored") or []):
            m.explored.append(ExploredRegion(**rd))
        for wd in (d.get("water_areas") or []):
            m.water_areas.append(WaterArea(**wd))
        for od in (d.get("obstacles") or []):
            m.obstacles.append(Obstacle(**od))
        m.current_spot_id = d.get("current_spot_id")
        return m

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "WorldMap":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def summary(self) -> str:
        return (
            f"钓点={len(self.spots)} 候选={len(self.candidate_spots())} "
            f"未用={len(self.unused_spots())} 已探索区域={len(self.explored)} "
            f"水域={len(self.water_areas)} 障碍={len(self.obstacles)}"
        )
