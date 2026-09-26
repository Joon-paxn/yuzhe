"""
config.py
---------
配置文件加载与保存模块。

所有可调参数集中在 config.json 中，避免硬编码。
ROI 以相对于 Minecraft 窗口的比例保存，使检测区域随窗口缩放自适应。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Tuple


@dataclass
class WindowConfig:
    """Minecraft 窗口相关配置"""
    title_keyword: str = "Minecraft"
    check_interval_ms: int = 2000


@dataclass
class FishingConfig:
    """钓鱼时间参数配置"""
    # 拉鱼阶段已改为「咬钩后右键单击一次收竿」，
    # 以下两个参数保留仅为向后兼容，当前不再使用。
    pull_interval_ms: int = 100        # (已弃用) 原左键连点间隔
    pull_duration_ms: int = 3500       # (已弃用) 原拉鱼持续时间
    recast_delay_min_ms: int = 1000    # 重新抛竿等待下限
    recast_delay_max_ms: int = 5000    # 重新抛竿等待上限


@dataclass
class DetectionConfig:
    """咬钩检测配置"""
    use_template_matching: bool = False  # 是否使用模板匹配
    template_match_threshold: float = 0.80  # 模板匹配置信度阈值 (0~1)
    use_color_detection: bool = False    # 是否使用目标颜色检测 (取色器选取)
    target_color: Tuple[int, int, int] = (255, 255, 255)  # 目标颜色 RGB
    color_tolerance: int = 30            # 颜色匹配容差 (每个通道)
    white_threshold: int = 235           # 单通道白色阈值 (R/G/B 均需大于此值)
    white_pixel_threshold: int = 800     # 大连通域白色像素数量阈值
    min_component_size: int = 50         # 最小连通域面积 (小于此值的白色区域视为噪点)
    max_white_ratio: float = 0.6         # 白色像素占 ROI 面积的最大比例 (防止白屏误触发)
    bite_confirm_frames: int = 3         # 连续确认帧数
    bite_cooldown_ms: int = 1500         # 咬钩冷却时间，防止重复触发
    screenshot_interval_ms: int = 50     # 截图检测间隔


@dataclass
class HotkeyConfig:
    """全局快捷键配置"""
    toggle: str = "f6"
    stop: str = "f7"
    select_region: str = "f8"
    test: str = "f9"
    capture_template: str = "f10"
    pick_color: str = "f11"


@dataclass
class RoiConfig:
    """检测区域 (相对于 Minecraft 窗口的比例 0~1)"""
    x: float = 0.25
    y: float = 0.63
    width: float = 0.32
    height: float = 0.16


@dataclass
class AppConfig:
    """整体配置聚合"""
    window: WindowConfig = field(default_factory=WindowConfig)
    fishing: FishingConfig = field(default_factory=FishingConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    roi: RoiConfig = field(default_factory=RoiConfig)


_DEFAULT_CONFIG = AppConfig()


def _default_config_dict() -> Dict[str, Any]:
    """返回默认配置字典"""
    return asdict(_DEFAULT_CONFIG)


def load_config(config_path: str) -> AppConfig:
    """
    从 JSON 文件加载配置。

    若文件不存在或解析失败，则使用默认配置并保存一份默认配置。
    部分字段缺失时，使用默认值填充，保证健壮性。
    """
    default_dict = _default_config_dict()

    if not os.path.exists(config_path):
        print(f"[config] 配置文件不存在，创建默认配置: {config_path}")
        save_config(config_path, _DEFAULT_CONFIG)
        return _DEFAULT_CONFIG

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_dict: Dict[str, Any] = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] 配置文件损坏，使用默认配置: {e}")
        # 备份损坏的配置
        backup_path = config_path + ".broken"
        try:
            os.rename(config_path, backup_path)
            print(f"[config] 损坏的配置已备份为: {backup_path}")
        except OSError:
            pass
        save_config(config_path, _DEFAULT_CONFIG)
        return _DEFAULT_CONFIG

    # 深度合并：用户配置覆盖默认配置，缺失字段使用默认值
    merged_dict = _deep_merge(default_dict, user_dict)

    # 规范化 target_color：支持对象 {r,g,b} 和数组 [r,g,b] 两种格式
    det_dict = merged_dict.get("detection", {})
    det_dict["target_color"] = _normalize_color(det_dict.get("target_color"))

    try:
        config = AppConfig(
            window=WindowConfig(**merged_dict.get("window", {})),
            fishing=FishingConfig(**merged_dict.get("fishing", {})),
            detection=DetectionConfig(**det_dict),
            hotkeys=HotkeyConfig(**merged_dict.get("hotkeys", {})),
            roi=RoiConfig(**merged_dict.get("roi", {})),
        )
    except TypeError as e:
        print(f"[config] 配置字段错误，使用默认配置: {e}")
        return _DEFAULT_CONFIG

    _validate_config(config)
    return config


def _normalize_color(value: Any) -> Tuple[int, int, int]:
    """
    将配置中的颜色值规范化为 (r, g, b) 元组。
    支持两种输入格式：
      - 对象: {"r": 252, "g": 252, "b": 252}
      - 数组: [252, 252, 252]
    格式错误时返回默认白色。
    """
    if isinstance(value, dict):
        r = value.get("r")
        g = value.get("g")
        b = value.get("b")
        if all(isinstance(v, int) and 0 <= v <= 255 for v in (r, g, b)):
            return (int(r), int(g), int(b))
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        if all(isinstance(v, int) and 0 <= v <= 255 for v in value):
            return (int(value[0]), int(value[1]), int(value[2]))
    return (255, 255, 255)


def save_config(config_path: str, config: AppConfig) -> None:
    """保存配置到 JSON 文件，颜色以 RGB 对象格式保存"""
    data = asdict(config)
    # 将 target_color 从元组转为 RGB 对象格式 {r, g, b}
    tc = data.get("detection", {}).get("target_color")
    if isinstance(tc, (list, tuple)) and len(tc) == 3:
        data["detection"]["target_color"] = {
            "r": int(tc[0]),
            "g": int(tc[1]),
            "b": int(tc[2]),
        }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并两个字典，override 中的值覆盖 base"""
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_config(config: AppConfig) -> None:
    """校验配置合理性，不合规则修正并提示"""
    det = config.detection
    fish = config.fishing

    if fish.pull_interval_ms < 10:
        fish.pull_interval_ms = 10
        print("[config] pull_interval_ms 过小，已修正为 10ms")
    if fish.pull_duration_ms < fish.pull_interval_ms:
        fish.pull_duration_ms = fish.pull_interval_ms
        print("[config] pull_duration_ms 小于间隔，已修正")
    if fish.recast_delay_min_ms < 0:
        fish.recast_delay_min_ms = 0
    if fish.recast_delay_max_ms < fish.recast_delay_min_ms:
        fish.recast_delay_max_ms = fish.recast_delay_min_ms
        print("[config] recast_delay_max 小于 min，已修正")

    if det.white_threshold < 0 or det.white_threshold > 255:
        det.white_threshold = max(0, min(255, det.white_threshold))
    if det.white_pixel_threshold < 1:
        det.white_pixel_threshold = 1
    if det.min_component_size < 1:
        det.min_component_size = 1
    if det.max_white_ratio <= 0 or det.max_white_ratio > 1:
        det.max_white_ratio = 0.6
        print("[config] max_white_ratio 超出范围，已修正为 0.6")
    if det.template_match_threshold < 0.5 or det.template_match_threshold > 1.0:
        det.template_match_threshold = max(0.5, min(1.0, det.template_match_threshold))
        print("[config] template_match_threshold 超出范围，已修正")
    if det.color_tolerance < 0 or det.color_tolerance > 255:
        det.color_tolerance = max(0, min(255, det.color_tolerance))
    # 校验 target_color 是 3 个 0~255 的整数
    tc = det.target_color
    if (
        not isinstance(tc, (list, tuple))
        or len(tc) != 3
        or any(not isinstance(v, int) or v < 0 or v > 255 for v in tc)
    ):
        det.target_color = (255, 255, 255)
        print("[config] target_color 格式错误，已重置为白色")
    if det.bite_confirm_frames < 1:
        det.bite_confirm_frames = 1
    if det.bite_cooldown_ms < 0:
        det.bite_cooldown_ms = 0
    if det.screenshot_interval_ms < 1:
        det.screenshot_interval_ms = 1

    # ROI 范围限制在 0~1
    roi = config.roi
    for attr in ("x", "y", "width", "height"):
        val = getattr(roi, attr)
        if val < 0:
            setattr(roi, attr, 0.0)
        elif val > 1:
            setattr(roi, attr, 1.0)
