"""运行时路线记忆 (Runtime Route Memory)。

记录钓点间导航的成本/时间/成功/卡死次数，供后续钓点选择参考。
独立存储，不污染 map_data.json。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class RouteRecord:
    source_spot: str
    target_spot: str
    path_cost: float
    elapsed_time: float
    success: bool
    stuck_count: int = 0
    failure_reason: str = ""
    timestamp: float = 0.0


class RouteMemory:
    def __init__(self, path: str = "runtime_route_memory.json"):
        self._path = path
        self._records: List[RouteRecord] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    self._records.append(RouteRecord(**r))
        except Exception:
            self._records = []

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self._records[-500:]], f, ensure_ascii=False, indent=2)

    def add(self, record: RouteRecord) -> None:
        record.timestamp = record.timestamp or time.time()
        self._records.append(record)

    def query(self, source: str, target: str) -> Optional[RouteRecord]:
        for r in reversed(self._records):
            if r.source_spot == source and r.target_spot == target:
                return r
        return None

    def success_rate(self, target: str) -> float:
        rs = [r for r in self._records if r.target_spot == target]
        if not rs:
            return 0.0
        return sum(1 for r in rs if r.success) / len(rs)

    def avg_cost(self, target: str) -> Optional[float]:
        rs = [r for r in self._records if r.target_spot == target and r.success]
        if not rs:
            return None
        return sum(r.path_cost for r in rs) / len(rs)
