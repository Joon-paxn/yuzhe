"""
detector.py
-----------
咬钩检测模块。

支持两种检测模式：
1. 模板匹配 (推荐): 用户先截取「咬钩！」的图像作为模板，
   使用 cv2.matchTemplate 在 ROI 中查找匹配。能精确区分「咬钩！」
   与其他白色文字 (如「钓点」「鱼群: 丰富」)。
2. 白色像素 + 连通域 (备用): 统计大连通域白色像素数量，
   适用于未截取模板时的降级方案。

不使用 OCR。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import mss
import numpy as np

from config import DetectionConfig, RoiConfig
from minecraft_window import MinecraftWindow, WindowRect
from logger import get_logger


TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bite_template.png")


@dataclass
class DetectionResult:
    """单次检测结果"""
    white_pixels: int                          # 总白色像素数
    large_white_pixels: int                    # 大连通域内的白色像素数
    component_count: int                       # 白色连通域数量
    match_score: float                         # 模板匹配置信度 (0~1)，无模板时为 0
    bite_detected: bool
    roi_screen: Tuple[int, int, int, int]      # (x, y, w, h) 屏幕坐标
    # ---- 阶段一：双重取色混合判断 ----
    color_a_passed: bool = False               # 颜色 A 是否通过
    color_b_passed: bool = False               # 颜色 B 是否通过
    dual_color_used: bool = False              # 本次检测是否启用了双取色
    color_a_large: int = 0                      # 颜色 A 大连通域像素数
    color_b_large: int = 0                      # 颜色 B 大连通域像素数


class BiteDetector:
    """咬钩检测器"""

    def __init__(
        self,
        mc_window: MinecraftWindow,
        detection_cfg: DetectionConfig,
        roi_cfg: RoiConfig,
    ) -> None:
        self._mc = mc_window
        self._det_cfg = detection_cfg
        self._roi_cfg = roi_cfg
        self._log = get_logger()
        self._sct = mss.mss()

        # 模板图像 (灰度图)，None 表示未加载
        self._template: Optional[np.ndarray] = None
        self._load_template()

        # 连续帧确认状态
        self._confirm_count: int = 0
        # 冷却结束时间 (monotonic)
        self._cooldown_until: float = 0.0

    def update_config(self, detection_cfg: DetectionConfig, roi_cfg: RoiConfig) -> None:
        """运行时更新检测配置"""
        self._det_cfg = detection_cfg
        self._roi_cfg = roi_cfg

    # ---------------- 模板管理 ----------------

    def _load_template(self) -> None:
        """从磁盘加载模板图像"""
        if os.path.exists(TEMPLATE_PATH):
            try:
                tpl = cv2.imread(TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
                if tpl is not None and tpl.size > 0:
                    self._template = tpl
                    self._log.info(f"咬钩模板已加载: {tpl.shape[1]}x{tpl.shape[0]}")
                    return
            except Exception as e:
                self._log.error(f"加载模板失败: {e}")
        self._template = None
        self._log.warn("未找到咬钩模板 (bite_template.png)，将使用白色像素检测")

    def capture_template(self) -> bool:
        """
        截取当前 ROI 作为「咬钩！」模板并保存。

        调用时机: 屏幕上正在显示「咬钩！」时按 F10。

        Returns:
            是否成功保存
        """
        frame, roi = self.capture_roi()
        if frame is None:
            self._log.error("截图失败，无法截取模板")
            return False

        # 转为灰度图保存
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        try:
            cv2.imwrite(TEMPLATE_PATH, gray)
            self._template = gray
            self._log.success(
                f"咬钩模板已保存: {gray.shape[1]}x{gray.shape[0]} -> {TEMPLATE_PATH}"
            )
            return True
        except Exception as e:
            self._log.error(f"保存模板失败: {e}")
            return False

    def has_template(self) -> bool:
        return self._template is not None

    def get_mode_name(self) -> str:
        """
        返回当前实际生效的检测模式名称。
        与 detect_once() 中的优先级一致：模板匹配 > 颜色检测 > 白色像素。
        """
        if self._det_cfg.use_template_matching and self._template is not None:
            return "模板匹配"
        if self._det_cfg.use_color_detection:
            return "双重取色" if self._det_cfg.use_dual_color else "颜色检测"
        return "白像素"

    # ---------------- ROI 计算与截图 ----------------

    def _calc_roi_screen(self, win_rect: WindowRect) -> Optional[Tuple[int, int, int, int]]:
        """根据窗口矩形和 ROI 比例计算屏幕 ROI 坐标"""
        if win_rect.width <= 0 or win_rect.height <= 0:
            return None
        x = win_rect.left + int(win_rect.width * self._roi_cfg.x)
        y = win_rect.top + int(win_rect.height * self._roi_cfg.y)
        w = max(1, int(win_rect.width * self._roi_cfg.width))
        h = max(1, int(win_rect.height * self._roi_cfg.height))
        if x < win_rect.left:
            x = win_rect.left
        if y < win_rect.top:
            y = win_rect.top
        if x + w > win_rect.right:
            w = win_rect.right - x
        if y + h > win_rect.bottom:
            h = win_rect.bottom - y
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def capture_roi(self) -> Tuple[Optional[np.ndarray], Optional[Tuple[int, int, int, int]]]:
        """截取 ROI 区域图像 (BGR)"""
        win_rect = self._mc.get_rect()
        if win_rect is None:
            return None, None
        roi = self._calc_roi_screen(win_rect)
        if roi is None:
            return None, None

        x, y, w, h = roi
        monitor = {"left": x, "top": y, "width": w, "height": h}
        try:
            shot = self._sct.grab(monitor)
            frame = np.array(shot)
            frame = frame[:, :, :3]  # BGRA -> BGR
            return frame, roi
        except Exception as e:
            self._log.error(f"截图失败: {e}")
            return None, None

    # ---------------- 白色像素分析 (备用) ----------------

    def _build_white_mask(self, frame: np.ndarray) -> np.ndarray:
        """构建白色像素二值掩码"""
        th = self._det_cfg.white_threshold
        return (
            (frame[:, :, 0] > th)
            & (frame[:, :, 1] > th)
            & (frame[:, :, 2] > th)
        ).astype(np.uint8) * 255

    def analyze_white(self, frame: np.ndarray) -> Tuple[int, int, int]:
        """返回 (总白色像素, 大连通域白色像素, 连通域数量)"""
        mask = self._build_white_mask(frame)
        total_white = int(np.count_nonzero(mask))

        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_size = self._det_cfg.min_component_size
        large_white = 0
        large_components = 0
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= min_size:
                large_white += area
                large_components += 1

        return total_white, large_white, large_components

    # ---------------- 目标颜色检测 ----------------

    def _build_color_mask(self, frame: np.ndarray, color: Tuple[int, int, int]) -> np.ndarray:
        """
        构建目标颜色匹配掩码。

        判定条件: 像素 RGB 三通道均在 color ± color_tolerance 范围内。
        frame 为 BGR 格式，color 为 RGB。
        """
        tol = self._det_cfg.color_tolerance
        tr, tg, tb = color  # RGB
        # frame 是 BGR: [B, G, R]
        b_match = np.abs(frame[:, :, 0].astype(int) - tb) <= tol
        g_match = np.abs(frame[:, :, 1].astype(int) - tg) <= tol
        r_match = np.abs(frame[:, :, 2].astype(int) - tr) <= tol
        return (b_match & g_match & r_match).astype(np.uint8) * 255

    def analyze_color(self, frame: np.ndarray, color: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """返回 (总匹配像素, 大连通域匹配像素, 连通域数量)"""
        mask = self._build_color_mask(frame, color)
        total = int(np.count_nonzero(mask))

        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_size = self._det_cfg.min_component_size
        large = 0
        comps = 0
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= min_size:
                large += area
                comps += 1

        return total, large, comps

    # ---------------- 模板匹配 ----------------

    def match_template(self, frame: np.ndarray) -> float:
        """
        在 frame 中匹配模板，返回最高置信度 (0~1)。

        使用 TM_CCOEFF_NORMED，对亮度变化有一定鲁棒性。
        """
        if self._template is None:
            return 0.0
        # 模板尺寸必须小于等于 frame 尺寸
        th, tw = self._template.shape[:2]
        fh, fw = frame.shape[:2]
        if tw > fw or th > fh:
            return 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        try:
            result = cv2.matchTemplate(gray, self._template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            return float(max_val)
        except Exception:
            return 0.0

    # ---------------- 检测入口 ----------------

    def detect_once(self) -> DetectionResult:
        """
        执行一次检测。

        优先级:
        1. 模板匹配 (use_template_matching=True 且模板存在)
        2. 目标颜色检测 (use_color_detection=True)
        3. 白色像素 + 连通域 (降级)
        """
        frame, roi = self.capture_roi()
        if frame is None or roi is None:
            return DetectionResult(
                white_pixels=0, large_white_pixels=0, component_count=0,
                match_score=0.0, bite_detected=False, roi_screen=(0, 0, 0, 0),
            )

        cfg = self._det_cfg
        total_white, large_white, comp_count = self.analyze_white(frame)

        # 1. 模板匹配模式
        if cfg.use_template_matching and self._template is not None:
            score = self.match_template(frame)
            bite = score >= cfg.template_match_threshold
            return DetectionResult(
                white_pixels=total_white,
                large_white_pixels=large_white,
                component_count=comp_count,
                match_score=score,
                bite_detected=bite,
                roi_screen=roi,
            )

        # 2. 目标颜色检测模式
        if cfg.use_color_detection:
            roi_area = roi[2] * roi[3]
            max_allowed = int(roi_area * cfg.max_white_ratio)

            # 阶段一：双重取色混合判断 (A 且 B 同时通过才触发)
            if cfg.use_dual_color:
                a_total, a_large, a_comps = self.analyze_color(frame, cfg.target_color)
                b_total, b_large, b_comps = self.analyze_color(frame, cfg.target_color_b)
                a_passed = a_large >= cfg.white_pixel_threshold and a_large <= max_allowed
                b_passed = b_large >= cfg.white_pixel_threshold and b_large <= max_allowed
                bite = a_passed and b_passed
                return DetectionResult(
                    white_pixels=a_total,          # 复用字段显示 A 总匹配数
                    large_white_pixels=a_large,    # 复用字段显示 A 大连通域数
                    component_count=a_comps,
                    match_score=0.0,
                    bite_detected=bite,
                    roi_screen=roi,
                    color_a_passed=a_passed,
                    color_b_passed=b_passed,
                    dual_color_used=True,
                    color_a_large=a_large,
                    color_b_large=b_large,
                )

            # 单取色模式 (原有逻辑)
            total_color, large_color, color_comps = self.analyze_color(frame, cfg.target_color)
            bite = large_color >= cfg.white_pixel_threshold and large_color <= max_allowed
            return DetectionResult(
                white_pixels=total_color,       # 复用字段显示匹配像素数
                large_white_pixels=large_color,  # 复用字段显示大连通域匹配数
                component_count=color_comps,
                match_score=0.0,
                bite_detected=bite,
                roi_screen=roi,
            )

        # 3. 降级: 白色像素 + 连通域
        roi_area = roi[2] * roi[3]
        max_allowed = int(roi_area * cfg.max_white_ratio)
        bite = large_white >= cfg.white_pixel_threshold and large_white <= max_allowed
        return DetectionResult(
            white_pixels=total_white,
            large_white_pixels=large_white,
            component_count=comp_count,
            match_score=0.0,
            bite_detected=bite,
            roi_screen=roi,
        )

    def detect_bite(self) -> DetectionResult:
        """带连续帧确认 + 冷却的咬钩检测"""
        result = self.detect_once()

        now = time.monotonic()

        if now < self._cooldown_until:
            result.bite_detected = False
            return result

        if result.bite_detected:
            self._confirm_count += 1
            if self._confirm_count >= self._det_cfg.bite_confirm_frames:
                self._confirm_count = 0
                self._cooldown_until = now + self._det_cfg.bite_cooldown_ms / 1000.0
                result.bite_detected = True
                return result
        else:
            self._confirm_count = 0

        result.bite_detected = False
        return result

    def reset(self) -> None:
        """重置确认计数和冷却"""
        self._confirm_count = 0
        self._cooldown_until = 0.0

    def close(self) -> None:
        """释放 mss 资源"""
        try:
            self._sct.close()
        except Exception:
            pass
