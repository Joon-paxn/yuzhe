"""Typed, validated settings. Paths are relative to the application, not the shell."""
from __future__ import annotations
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


@dataclass
class WindowConfig:
    title_keyword: str = "WinIsland"
    process_names: tuple = ("WinIsland.exe", "javaw.exe", "java.exe", "Minecraft.exe")


@dataclass
class RoiConfig:
    x: float = 0.35
    y: float = 0.35
    width: float = 0.30
    height: float = 0.20
    configured: bool = False


@dataclass
class TitleConfig:
    target_text: str = "咬钩！"
    preprocess_scale: int = 3
    ocr_similarity_threshold: float = 0.80
    ocr_confidence_threshold: float = 0.65
    title_confirm_frames: int = 2
    title_confirm_window_ms: int = 900
    title_candidate_timeout: int = 1200
    bite_cooldown: int = 2000
    clear_frames: int = 2
    screenshot_interval_ms: int = 80
    min_wait_ms: int = 600


@dataclass
class FishingConfig:
    recast_delay_min_ms: int = 1000
    recast_delay_max_ms: int = 2500
    result_timeout: int = 6000
    stop_on_unknown: bool = True
    initial_cast: bool = False


@dataclass
class GameLogConfig:
    enabled: bool = True
    path: str = ""
    extra_errors: dict = field(default_factory=dict)


@dataclass
class AlertConfig:
    alert_enabled: bool = True
    sound_enabled: bool = True
    sound_frequency: int = 880
    sound_duration_ms: int = 350
    repeat_interval_ms: int = 3000


@dataclass
class BobberConfig:
    enabled: bool = False
    roi: RoiConfig = field(default_factory=RoiConfig)
    motion_threshold: float = 0.06


@dataclass
class HotkeyConfig:
    toggle: str = "f6"
    stop: str = "f7"
    select_region: str = "f8"
    test: str = "f9"
    capture_template: str = "f10"
    pick_color: str = "f11"


@dataclass
class UserActivityConfig:
    enabled: bool = True
    threshold_ms: int = 1500
    poll_interval_ms: int = 80


@dataclass
class UiConfig:
    window_geometry: str = "1060x790"


@dataclass
class AppConfig:
    window: WindowConfig = field(default_factory=WindowConfig)
    roi: RoiConfig = field(default_factory=RoiConfig)
    title: TitleConfig = field(default_factory=TitleConfig)
    fishing: FishingConfig = field(default_factory=FishingConfig)
    game_log: GameLogConfig = field(default_factory=GameLogConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    bobber: BobberConfig = field(default_factory=BobberConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    user_activity: UserActivityConfig = field(default_factory=UserActivityConfig)
    ui: UiConfig = field(default_factory=UiConfig)


def validate(config):
    limits = {
        "ocr_similarity_threshold": (0.5, 1.0), "ocr_confidence_threshold": (0.1, 1.0),
        "title_confirm_frames": (2, 10), "title_confirm_window_ms": (200, 5000),
        "title_candidate_timeout": (200, 10000), "bite_cooldown": (300, 15000),
        "clear_frames": (1, 10), "screenshot_interval_ms": (30, 1000), "min_wait_ms": (0, 5000),
        "preprocess_scale": (3, 4),
    }
    for name, (low, high) in limits.items():
        value = float(getattr(config.title, name))
        value = max(low, min(high, value))
        setattr(config.title, name, value if "threshold" in name else int(value))
    config.title.title_candidate_timeout = max(config.title.title_confirm_window_ms,
                                               config.title.title_candidate_timeout)
    if not config.title.target_text.strip():
        config.title.target_text = "咬钩！"
    for roi in (config.roi, config.bobber.roi):
        roi.x, roi.y = max(0, min(.99, float(roi.x))), max(0, min(.99, float(roi.y)))
        roi.width = max(.005, min(1-roi.x, float(roi.width)))
        roi.height = max(.005, min(1-roi.y, float(roi.height)))
    fish = config.fishing
    fish.recast_delay_min_ms = max(0, int(fish.recast_delay_min_ms))
    fish.recast_delay_max_ms = max(fish.recast_delay_min_ms, int(fish.recast_delay_max_ms))
    fish.result_timeout = max(500, min(30000, int(fish.result_timeout)))
    alert = config.alert
    alert.sound_frequency = max(37, min(32767, int(alert.sound_frequency)))
    alert.sound_duration_ms = max(50, min(2000, int(alert.sound_duration_ms)))
    alert.repeat_interval_ms = max(alert.sound_duration_ms + 100, int(alert.repeat_interval_ms))
    activity = config.user_activity
    activity.threshold_ms = max(300, min(30000, int(activity.threshold_ms)))
    activity.poll_interval_ms = max(30, min(1000, int(activity.poll_interval_ms)))
    if (not isinstance(config.ui.window_geometry, str)
            or not re.fullmatch(r"\d+x\d+(?:[+-]\d+){0,2}", config.ui.window_geometry)):
        config.ui.window_geometry = "1060x790"


def load_config(path=CONFIG_PATH):
    config = AppConfig()
    if not Path(path).exists():
        save_config(path, config)
        return config
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        for name in ("window", "roi", "title", "fishing", "game_log", "alert", "hotkeys", "user_activity", "ui"):
            section = getattr(config, name)
            for f in fields(section):
                if f.name in data.get(name, {}):
                    setattr(section, f.name, data[name][f.name])
        b = data.get("bobber", {})
        config.bobber.enabled = b.get("enabled", False)
        config.bobber.motion_threshold = float(b.get("motion_threshold", .06))
        config.bobber.roi = RoiConfig(**{k: v for k, v in b.get("roi", {}).items()
                                       if k in {f.name for f in fields(RoiConfig)}})
        validate(config)
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise ValueError(f"配置文件无效：{path}\n{exc}") from exc
    return config


def save_config(path, config):
    validate(config)
    temp = Path(str(path) + ".tmp")
    temp.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
