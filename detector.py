"""
detector.py
-----------
咬钩检测模块。

支持三种检测模式：
1. 模板匹配 (推荐): 用户先截取「咬钩！」的图像作为模板，
   使用 cv2.matchTemplate 在 ROI 中查找匹配。
2. 目标颜色检测 / 双重取色 (阶段一): 统计目标颜色的大连通域像素数。
3. 自适应 RGB 模型 (阶段二 Beta): 在双重取色基础上，为 A/B 检测点
   各自维护历史样本，用中位数 + 动态容差判断当前是否咬钩，
   解决固定阈值误判/漏判问题。adaptive_rgb.enabled=false 时退回阶段一逻辑。

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

from config import DetectionConfig, RoiConfig, AdaptiveRGBConfig
from minecraft_window import MinecraftWindow, WindowRect
from logger import get_logger
from adaptive_rgb import AdaptiveColorModel
from ocr import OcrService, GameState


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
    # ---- 阶段二：自适应 RGB 模型 ----
    adaptive_used: bool = False                 # 本次检测是否走了自适应路径
    a_reference: Optional[Tuple[int, int, int]] = None
    b_reference: Optional[Tuple[int, int, int]] = None
    a_samples: int = 0
    b_samples: int = 0
    a_tolerance: Tuple[int, int, int] = (0, 0, 0)
    b_tolerance: Tuple[int, int, int] = (0, 0, 0)
    a_in_range: bool = False                   # A 点当前 RGB 是否在动态范围
    b_in_range: bool = False
    a_current_rgb: Tuple[int, int, int] = (0, 0, 0)
    b_current_rgb: Tuple[int, int, int] = (0, 0, 0)
    # ---- 阶段二：OCR 混合检测 ----
    ocr_used: bool = False                     # 本次检测是否启用 OCR 层
    ocr_confirmed: bool = False                # OCR 是否确认咬钩
    ocr_pending: bool = False                  # hybrid: 正在等待 OCR 确认


class BiteDetector:
    """咬钩检测器"""

    def __init__(
        self,
        mc_window: MinecraftWindow,
        detection_cfg: DetectionConfig,
        roi_cfg: RoiConfig,
        adaptive_cfg: Optional[AdaptiveRGBConfig] = None,
    ) -> None:
        self._mc = mc_window
        self._det_cfg = detection_cfg
        self._roi_cfg = roi_cfg
        self._ada_cfg = adaptive_cfg or AdaptiveRGBConfig()
        self._log = get_logger()
        self._sct = mss.mss()

        self._template: Optional[np.ndarray] = None
        self._load_template()

        self._confirm_count: int = 0
        self._cooldown_until: float = 0.0

        # 阶段二：OCR 混合检测
        self._ocr_service: Optional[OcrService] = None
        self._pending_ocr: bool = False          # hybrid: RGB 候选已通过，等待 OCR 确认
        self._pending_ocr_since: float = 0.0
        self._ocr_fallback_warned: bool = False  # OCR 不可用回退警告 (只提示一次)

        # 阶段二：A、B 各自一个自适应模型实例，持久化在检测器上
        self._model_a = AdaptiveColorModel(
            max_samples=self._ada_cfg.max_samples,
            min_samples=self._ada_cfg.min_samples,
            min_tolerance=self._ada_cfg.min_tolerance,
            max_tolerance=self._ada_cfg.max_tolerance,
            outlier_threshold=self._ada_cfg.outlier_threshold,
        )
        self._model_b = AdaptiveColorModel(
            max_samples=self._ada_cfg.max_samples,
            min_samples=self._ada_cfg.min_samples,
            min_tolerance=self._ada_cfg.min_tolerance,
            max_tolerance=self._ada_cfg.max_tolerance,
            outlier_threshold=self._ada_cfg.outlier_threshold,
        )

    def update_config(
        self,
        detection_cfg: DetectionConfig,
        roi_cfg: RoiConfig,
        adaptive_cfg: Optional[AdaptiveRGBConfig] = None,
    ) -> None:
        """运行时更新检测配置。adaptive_cfg 变动时不重置模型历史。"""
        self._det_cfg = detection_cfg
        self._roi_cfg = roi_cfg
        if adaptive_cfg is not None:
            self._ada_cfg = adaptive_cfg
            # 同步参数到模型实例 (不重置历史，保留已学到的 Reference)
            for m in (self._model_a, self._model_b):
                m._max_samples = adaptive_cfg.max_samples
                m._min_samples = adaptive_cfg.min_samples
                m._min_tolerance = adaptive_cfg.min_tolerance
                m._max_tolerance = adaptive_cfg.max_tolerance
                m._outlier_threshold = adaptive_cfg.outlier_threshold

    def reset_adaptive_models(self) -> None:
        """重置 A/B 自适应模型 (重新冷启动)"""
        self._model_a.reset()
        self._model_b.reset()

    # ---------------- OCR 接入 (阶段二) ----------------

    def set_ocr_service(self, ocr_service: Optional[OcrService]) -> None:
        """注入 OCR 服务，供 hybrid / ocr 模式确认咬钩。为 None 则回退 RGB。"""
        self._ocr_service = ocr_service

    def _ocr_available(self) -> bool:
        """OCR 服务是否就绪 (已注入、引擎可用、正在运行)"""
        return (
            self._ocr_service is not None
            and self._ocr_service.engine_available
            and self._ocr_service.is_running
        )

    def _get_ocr_state(self) -> Optional[GameState]:
        if not self._ocr_available():
            return None
        return self._ocr_service.get_latest_state()

    def _warn_ocr_fallback(self) -> None:
        if not self._ocr_fallback_warned:
            self._ocr_fallback_warned = True
            self._log.warn(
                f"检测模式={self._det_cfg.mode!r} 需要 OCR，但 OCR 服务不可用，"
                f"临时回退到 RGB 检测。请启用 ocr.enabled 并安装 rapidocr。"
            )

    def get_adaptive_snapshot(self) -> dict:
        """返回 A/B 模型状态快照，供外部显示"""
        return {"a": self._model_a.snapshot(), "b": self._model_b.snapshot()}

    # ---------------- 模板管理 ----------------

    def _load_template(self) -> None:
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
        frame, roi = self.capture_roi()
        if frame is None:
            self._log.error("截图失败，无法截取模板")
            return False
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
        mode = self._det_cfg.mode
        if mode == "ocr":
            return "OCR 文字识别"
        base = self._rgb_mode_name()
        if mode == "hybrid":
            return f"混合({base})"
        return base

    def _rgb_mode_name(self) -> str:
        """RGB 层检测模式名 (不含 hybrid/ocr 前缀)"""
        if self._det_cfg.use_template_matching and self._template is not None:
            return "模板匹配"
        if self._det_cfg.use_color_detection:
            if self._ada_cfg.enabled and self._det_cfg.use_dual_color:
                return "自适应双重取色"
            return "双重取色" if self._det_cfg.use_dual_color else "颜色检测"
        return "白像素"

    # ---------------- ROI 计算与截图 ----------------

    def _calc_roi_screen(self, win_rect: WindowRect) -> Optional[Tuple[int, int, int, int]]:
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

    def _capture_frame(self) -> Tuple[Optional[np.ndarray], Optional[Tuple[int, int, int, int]], Optional[WindowRect]]:
        """截取 ROI，同时返回窗口矩形 (供 A/B 点坐标计算)"""
        win_rect = self._mc.get_rect()
        if win_rect is None:
            return None, None, None
        roi = self._calc_roi_screen(win_rect)
        if roi is None:
            return None, None, win_rect
        x, y, w, h = roi
        monitor = {"left": x, "top": y, "width": w, "height": h}
        try:
            shot = self._sct.grab(monitor)
            frame = np.array(shot)[:, :, :3]
            return frame, roi, win_rect
        except Exception as e:
            self._log.error(f"截图失败: {e}")
            return None, None, win_rect

    def capture_roi(self) -> Tuple[Optional[np.ndarray], Optional[Tuple[int, int, int, int]]]:
        """截取 ROI 区域图像 (BGR)，对外保留阶段一接口"""
        frame, roi, _ = self._capture_frame()
        return frame, roi

    def _sample_point_rgb(
        self, frame: np.ndarray, roi: Tuple[int, int, int, int],
        win_rect: WindowRect, point_ratio: Tuple[float, float],
    ) -> Tuple[int, int, int]:
        """
        根据检测点相对窗口的比例，计算其在 ROI frame 中的像素坐标并读取 RGB。
        frame 为 BGR，返回 RGB 元组。坐标越界自动 clamp。
        """
        rx, ry = point_ratio
        screen_x = win_rect.left + int(win_rect.width * rx)
        screen_y = win_rect.top + int(win_rect.height * ry)
        roi_x, roi_y, roi_w, roi_h = roi
        fx = screen_x - roi_x
        fy = screen_y - roi_y
        fx = max(0, min(roi_w - 1, fx))
        fy = max(0, min(roi_h - 1, fy))
        bgr = frame[fy, fx]
        return (int(bgr[2]), int(bgr[1]), int(bgr[0]))  # BGR -> RGB

    # ---------------- 白色像素分析 (备用) ----------------

    def _build_white_mask(self, frame: np.ndarray) -> np.ndarray:
        th = self._det_cfg.white_threshold
        return (
            (frame[:, :, 0] > th)
            & (frame[:, :, 1] > th)
            & (frame[:, :, 2] > th)
        ).astype(np.uint8) * 255

    def analyze_white(self, frame: np.ndarray) -> Tuple[int, int, int]:
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
        tol = self._det_cfg.color_tolerance
        tr, tg, tb = color
        b_match = np.abs(frame[:, :, 0].astype(int) - tb) <= tol
        g_match = np.abs(frame[:, :, 1].astype(int) - tg) <= tol
        r_match = np.abs(frame[:, :, 2].astype(int) - tr) <= tol
        return (b_match & g_match & r_match).astype(np.uint8) * 255

    def analyze_color(self, frame: np.ndarray, color: Tuple[int, int, int]) -> Tuple[int, int, int]:
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
        if self._template is None:
            return 0.0
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
        1. 模板匹配
        2. 自适应 RGB 模型 (adaptive_rgb.enabled 且双重取色且 A/B 坐标已设置)
        3. 目标颜色检测 (双重取色 / 单取色)
        4. 白色像素 + 连通域 (降级)
        """
        frame, roi, win_rect = self._capture_frame()
        if frame is None or roi is None or win_rect is None:
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
                white_pixels=total_white, large_white_pixels=large_white,
                component_count=comp_count, match_score=score,
                bite_detected=bite, roi_screen=roi,
            )

        # 2. 目标颜色检测模式
        if cfg.use_color_detection:
            roi_area = roi[2] * roi[3]
            max_allowed = int(roi_area * cfg.max_white_ratio)

            # 阶段二：自适应 RGB 模型路径
            if self._ada_cfg.enabled and cfg.use_dual_color and cfg.points_ratio_set:
                return self._detect_adaptive(
                    frame, roi, win_rect, total_white, large_white, comp_count, max_allowed
                )

            # 阶段一：双重取色混合判断
            if cfg.use_dual_color:
                a_total, a_large, a_comps = self.analyze_color(frame, cfg.target_color)
                b_total, b_large, b_comps = self.analyze_color(frame, cfg.target_color_b)
                a_passed = a_large >= cfg.white_pixel_threshold and a_large <= max_allowed
                b_passed = b_large >= cfg.white_pixel_threshold and b_large <= max_allowed
                bite = a_passed and b_passed
                return DetectionResult(
                    white_pixels=a_total, large_white_pixels=a_large,
                    component_count=a_comps, match_score=0.0,
                    bite_detected=bite, roi_screen=roi,
                    color_a_passed=a_passed, color_b_passed=b_passed,
                    dual_color_used=True, color_a_large=a_large, color_b_large=b_large,
                )

            # 单取色模式
            total_color, large_color, color_comps = self.analyze_color(frame, cfg.target_color)
            bite = large_color >= cfg.white_pixel_threshold and large_color <= max_allowed
            return DetectionResult(
                white_pixels=total_color, large_white_pixels=large_color,
                component_count=color_comps, match_score=0.0,
                bite_detected=bite, roi_screen=roi,
            )

        # 3. 降级: 白色像素
        bite = large_white >= cfg.white_pixel_threshold and large_white <= max_allowed
        return DetectionResult(
            white_pixels=total_white, large_white_pixels=large_white,
            component_count=comp_count, match_score=0.0,
            bite_detected=bite, roi_screen=roi,
        )

    def _detect_adaptive(
        self, frame: np.ndarray, roi: Tuple[int, int, int, int],
        win_rect: WindowRect, total_white: int, large_white: int,
        comp_count: int, max_allowed: int,
    ) -> DetectionResult:
        """
        阶段二自适应检测路径。
        - 始终执行阶段一色差掩膜分析 (作为基础检测规则，用于冷启动采样)
        - 采样 A、B 当前 RGB，各自喂给独立模型
        - 冷启动期: 基础检测通过才采样；判定用基础检测
        - 已就绪期: is_in_range 判定；合理样本才加入历史 (防污染)
        """
        cfg = self._det_cfg

        # 基础检测 (阶段一色差掩膜计数)
        a_total, a_large, a_comps = self.analyze_color(frame, cfg.target_color)
        b_total, b_large, b_comps = self.analyze_color(frame, cfg.target_color_b)
        a_basic = a_large >= cfg.white_pixel_threshold and a_large <= max_allowed
        b_basic = b_large >= cfg.white_pixel_threshold and b_large <= max_allowed

        # 采样 A、B 当前 RGB
        a_rgb = self._sample_point_rgb(frame, roi, win_rect, cfg.point_a_ratio)
        b_rgb = self._sample_point_rgb(frame, roi, win_rect, cfg.point_b_ratio)

        ma, mb = self._model_a, self._model_b

        # A、B 独立处理
        if not ma.is_ready or not mb.is_ready:
            # 冷启动期: 基础检测通过才采样，判定也用基础检测
            if a_basic:
                ma.add_sample(a_rgb)
            if b_basic:
                mb.add_sample(b_rgb)
            a_in_range = ma.is_in_range(a_rgb) if ma.is_ready else a_basic
            b_in_range = mb.is_in_range(b_rgb) if mb.is_ready else b_basic
        else:
            # 已就绪: 自适应判定 + 合理样本加入历史
            a_in_range = ma.is_in_range(a_rgb)
            b_in_range = mb.is_in_range(b_rgb)
            if a_in_range:
                ma.add_sample(a_rgb)
            if b_in_range:
                mb.add_sample(b_rgb)

        bite = a_in_range and b_in_range

        return DetectionResult(
            white_pixels=a_total, large_white_pixels=a_large,
            component_count=a_comps, match_score=0.0,
            bite_detected=bite, roi_screen=roi,
            color_a_passed=a_in_range, color_b_passed=b_in_range,
            dual_color_used=True, color_a_large=a_large, color_b_large=b_large,
            adaptive_used=True,
            a_reference=ma.reference, b_reference=mb.reference,
            a_samples=ma.samples, b_samples=mb.samples,
            a_tolerance=ma.tolerance, b_tolerance=mb.tolerance,
            a_in_range=a_in_range, b_in_range=b_in_range,
            a_current_rgb=a_rgb, b_current_rgb=b_rgb,
        )

    def detect_bite(self) -> DetectionResult:
        """
        带连续帧确认 + 冷却的咬钩检测。

        阶段二按 detection.mode 分流：
        - "rgb":    仅 RGB 双检测点 (现有行为)
        - "ocr":    仅 OCR 文字识别「咬钩」
        - "hybrid": RGB 双检测点先发现候选，OCR 在时间窗口内确认
        OCR 不可用时 hybrid/ocr 自动回退到 rgb (只警告一次)。
        """
        mode = self._det_cfg.mode
        if mode == "ocr":
            if not self._ocr_available():
                self._warn_ocr_fallback()
                return self._detect_bite_rgb()
            return self._detect_bite_ocr()
        if mode == "hybrid":
            if not self._ocr_available():
                self._warn_ocr_fallback()
                return self._detect_bite_rgb()
            return self._detect_bite_hybrid()
        return self._detect_bite_rgb()

    # ---------------- rgb 模式 (现有逻辑) ----------------

    def _detect_bite_rgb(self) -> DetectionResult:
        """RGB 双检测点：连续帧确认 + 冷却"""
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

    # ---------------- ocr 模式 ----------------

    def _detect_bite_ocr(self) -> DetectionResult:
        """OCR 文字识别：连续帧确认 + 冷却"""
        result = self.detect_once()  # 仍采集 RGB 用于状态显示
        result.ocr_used = True
        now = time.monotonic()
        if now < self._cooldown_until:
            result.bite_detected = False
            return result
        state = self._get_ocr_state()
        ocr_bite = bool(state and state.bite_detected)
        result.ocr_confirmed = ocr_bite
        if ocr_bite:
            self._confirm_count += 1
            if self._confirm_count >= self._det_cfg.bite_confirm_frames:
                self._confirm_count = 0
                self._cooldown_until = now + self._det_cfg.bite_cooldown_ms / 1000.0
                result.bite_detected = True
                self._log.info("检测到咬钩！ [OCR] 文字层确认「咬钩」")
                return result
        else:
            self._confirm_count = 0
        result.bite_detected = False
        return result

    # ---------------- hybrid 模式 ----------------

    def _detect_bite_hybrid(self) -> DetectionResult:
        """
        RGB 双检测点先发现候选 → OCR 在时间窗口内确认。
        - RGB 连续帧确认通过后进入 _pending_ocr 等待态
        - 等待期内 OCR 识别到「咬钩」即确认；超时则丢弃候选
        """
        result = self.detect_once()
        result.ocr_used = True
        now = time.monotonic()
        if now < self._cooldown_until:
            result.bite_detected = False
            return result

        # 等待 OCR 确认
        if self._pending_ocr:
            result.ocr_pending = True
            state = self._get_ocr_state()
            ocr_ts = self._ocr_service.last_recog_ts if self._ocr_service else 0.0
            # OCR 已在等待期内刷新且识别到咬钩 → 确认
            if state and state.bite_detected and ocr_ts >= self._pending_ocr_since:
                self._pending_ocr = False
                self._confirm_count = 0
                self._cooldown_until = now + self._det_cfg.bite_cooldown_ms / 1000.0
                result.bite_detected = True
                result.ocr_confirmed = True
                self._log.info("检测到咬钩！ [hybrid] RGB 候选 + OCR 确认「咬钩」")
                return result
            # 超时未确认 → 丢弃
            window_s = self._det_cfg.ocr_confirm_window_ms / 1000.0
            if now - self._pending_ocr_since > window_s:
                self._pending_ocr = False
                self._confirm_count = 0
                result.bite_detected = False
                result.ocr_confirmed = False
                self._log.info(
                    f"咬钩候选未确认 [hybrid]: OCR 在 {window_s:.1f}s 内未确认，丢弃"
                )
                return result
            # 仍在等待
            result.bite_detected = False
            return result

        # 正常 RGB 检测：连续帧确认 → 进入 OCR 等待
        if result.bite_detected:
            self._confirm_count += 1
            if self._confirm_count >= self._det_cfg.bite_confirm_frames:
                self._pending_ocr = True
                self._pending_ocr_since = now
                self._confirm_count = 0
                self._log.info("咬钩候选 [hybrid]: RGB 双检测点通过，等待 OCR 确认...")
        else:
            self._confirm_count = 0
        result.bite_detected = False
        return result

    def reset(self) -> None:
        """重置确认计数、冷却与 OCR 等待态 (不重置自适应模型历史)"""
        self._confirm_count = 0
        self._cooldown_until = 0.0
        self._pending_ocr = False
        self._pending_ocr_since = 0.0

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass
