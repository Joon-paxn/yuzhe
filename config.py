"""配置。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FishingConfig:
    polling_rate: int = 50          # ms
    polling_jitter: int = 50        # ms
    color_tolerance: int = 10
    no_fish_timeout: int = 30       # s
    confirmation_time: float = 0.3  # s
    reel_wait_min: float = 4.0
    reel_wait_max: float = 6.0
    cast_delay_min: float = 0.1
    cast_delay_max: float = 0.4
    px_color: str = "#FFFFFF"
    m_pos: Optional[list] = None    # [x, y] 咬钩像素
    auto_throw: bool = True


@dataclass
class NavigationConfig:
    arrive_dist: float = 1.5
    min_stand_distance: float = 2.0
    max_stand_distance: float = 6.0
    soft_stuck_window: float = 3.0
    hard_stuck_window: float = 8.0
    min_displacement: float = 0.3
    exclude_spot_distance: float = 5.0


@dataclass
class AppConfig:
    map_data_path: str = "map_data.json"
    runtime_path: str = "runtime_state.json"
    route_memory_path: str = "runtime_route_memory.json"
    window_title_keyword: str = "我的世界"
    input_mode: str = "global"        # "global" | "window"
    fishing: FishingConfig = field(default_factory=FishingConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)


def load_config(path: str = "config.json") -> AppConfig:
    cfg = AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "fishing" in data:
            for k, v in data["fishing"].items():
                if hasattr(cfg.fishing, k):
                    setattr(cfg.fishing, k, v)
        if "navigation" in data:
            for k, v in data["navigation"].items():
                if hasattr(cfg.navigation, k):
                    setattr(cfg.navigation, k, v)
        for k in ("map_data_path", "runtime_path", "route_memory_path",
                  "window_title_keyword", "input_mode"):
            if k in data:
                setattr(cfg, k, data[k])
    except FileNotFoundError:
        pass
    return cfg
