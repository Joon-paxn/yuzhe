"""
ocr/service.py
--------------
后台线程化 OCR 服务。

职责：
- 在独立线程中按固定间隔截取 OCR ROI 并识别
- 线程安全地缓存最新识别结果与解析出的游戏状态
- 不发送任何输入，不接入钓鱼状态机 (阶段一)

主线程通过 get_latest() 获取最新结果，用于显示 / 后续阶段接入检测。
OCR 阻塞只发生在本服务线程内，绝不影响截图检测与鼠标输入主循环。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import mss
import numpy as np

from config import OcrConfig
from logger import get_logger
from minecraft_window import MinecraftWindow
from .engine import OcrEngineBase, create_ocr_engine
from .models import OcrResult
from .parser import GameState, OcrParser
from .regions import OcrRoiConfig


class OcrService:
    """后台 OCR 识别服务"""

    def __init__(
        self,
        mc_window: MinecraftWindow,
        ocr_cfg: OcrConfig,
        engine: Optional[OcrEngineBase] = None,
    ) -> None:
        self._mc = mc_window
        self._cfg = ocr_cfg
        self._log = get_logger()
        self._engine = engine if engine is not None else create_ocr_engine()
        self._parser = OcrParser()

        # 阶段三：多 ROI 支持 (咬钩 + 枯竭面板位置不同，分别采集)
        self._rois = {
            "bite": OcrRoiConfig(
                x=ocr_cfg.roi_x, y=ocr_cfg.roi_y,
                width=ocr_cfg.roi_width, height=ocr_cfg.roi_height,
            ),
        }
        if getattr(ocr_cfg, "depleted_enabled", True):
            self._rois["depleted"] = OcrRoiConfig(
                x=ocr_cfg.depleted_roi_x, y=ocr_cfg.depleted_roi_y,
                width=ocr_cfg.depleted_roi_width, height=ocr_cfg.depleted_roi_height,
            )

        self._sct = mss.mss()

        self._lock = threading.Lock()
        self._latest_result: OcrResult = OcrResult(success=False, error="尚未运行")
        self._latest_state: GameState = GameState()
        self._last_recog_ts: float = 0.0

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running: bool = False

    # ---------------- 生命周期 ----------------

    @property
    def engine_available(self) -> bool:
        return self._engine.is_available()

    @property
    def engine_name(self) -> str:
        return self._engine.name

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """启动后台 OCR 线程。引擎不可用时返回 False。"""
        if self._running:
            return True
        if not self._engine.is_available():
            self._log.warn(
                f"OCR 引擎不可用，OCR 服务未启动: {self._engine.name}。"
                f"现有钓鱼检测不受影响。"
            )
            return False
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="OcrService", daemon=True)
        self._thread.start()
        self._log.info(
            f"OCR 服务已启动 (引擎={self._engine.name}, 间隔={self._cfg.interval_ms}ms)"
        )
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._log.info("OCR 服务已停止")

    def close(self) -> None:
        self.stop()
        try:
            self._sct.close()
        except Exception:
            pass

    # ---------------- 线程主循环 ----------------

    def _run(self) -> None:
        interval = max(50, self._cfg.interval_ms) / 1000.0
        try:
            while not self._stop_event.is_set():
                self._tick_once()
                # 可中断 sleep
                end = time.monotonic() + interval
                while True:
                    if self._stop_event.is_set():
                        return
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.05))
        except Exception as e:
            self._log.error(f"OCR 服务线程异常: {e}")
        finally:
            self._running = False

    def _tick_once(self) -> None:
        """采集所有 ROI + 识别 + 合并 + 缓存"""
        all_lines = []
        full_texts = []
        any_success = False
        errors = []
        total_ms = 0.0
        for name, roi in self._rois.items():
            frame = self._capture_roi(roi)
            if frame is None:
                errors.append(f"{name}:截图失败")
                continue
            result = self._engine.recognize(frame)
            if result.success:
                any_success = True
                all_lines.extend(result.lines)
                full_texts.append(result.full_text)
                total_ms += result.elapsed_ms
            elif result.error:
                errors.append(f"{name}:{result.error}")
        merged = OcrResult(
            lines=all_lines,
            full_text=" ".join(t for t in full_texts if t),
            success=any_success,
            error="; ".join(errors) if not any_success and errors else None,
            elapsed_ms=total_ms,
        )
        state = self._parser.parse(merged)
        with self._lock:
            self._latest_result = merged
            self._latest_state = state
            self._last_recog_ts = time.monotonic()

    # ---------------- 截图 ----------------

    def _capture_roi(self, roi: OcrRoiConfig) -> Optional[np.ndarray]:
        win_rect = self._mc.get_rect()
        if win_rect is None:
            return None
        screen = roi.to_screen(win_rect)
        if screen is None:
            return None
        x, y, w, h = screen
        monitor = {"left": x, "top": y, "width": w, "height": h}
        try:
            shot = self._sct.grab(monitor)
            return np.array(shot)[:, :, :3]
        except Exception as e:
            self._log.error(f"OCR 截图失败 ({roi}): {e}")
            return None

    def capture_roi_once(self, name: str = "bite") -> Optional[np.ndarray]:
        """对外暴露的单次 ROI 截图 (供测试脚本使用)，默认咬钩 ROI"""
        roi = self._rois.get(name)
        if roi is None:
            return None
        return self._capture_roi(roi)

    def capture_all_rois(self) -> dict:
        """采集所有 ROI，返回 {name: ndarray} (供测试可视化)"""
        out = {}
        for name, roi in self._rois.items():
            frame = self._capture_roi(roi)
            if frame is not None:
                out[name] = frame
        return out

    def get_roi_screen(self, name: str = "bite") -> Optional[tuple]:
        """返回指定 ROI 屏幕坐标 (x,y,w,h)，供测试可视化"""
        roi = self._rois.get(name)
        if roi is None:
            return None
        win_rect = self._mc.get_rect()
        if win_rect is None:
            return None
        return roi.to_screen(win_rect)

    def get_roi_names(self) -> list:
        """返回所有 ROI 名称 (供测试/GUI)"""
        return list(self._rois.keys())

    # ---------------- 结果查询 (线程安全) ----------------

    def get_latest(self) -> OcrResult:
        with self._lock:
            return self._latest_result

    def get_latest_state(self) -> GameState:
        with self._lock:
            return self._latest_state

    @property
    def last_recog_ts(self) -> float:
        with self._lock:
            return self._last_recog_ts

    def recognize_once(self, frame: Optional[np.ndarray] = None, name: str = "bite") -> OcrResult:
        """
        同步识别一次 (供测试脚本使用，不走后台线程)。
        frame 为 None 时自动截取指定 ROI (默认咬钩 ROI)。
        """
        if frame is None:
            roi = self._rois.get(name)
            if roi is None:
                return OcrResult(success=False, error=f"未知 ROI: {name}")
            frame = self._capture_roi(roi)
            if frame is None:
                return OcrResult(success=False, error="截图失败或窗口无效")
        return self._engine.recognize(frame)
