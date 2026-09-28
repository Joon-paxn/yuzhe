"""
fishing.py
----------
自动钓鱼状态机核心模块。

状态流转：
    WAITING_BITE -> (检测到咬钩) -> PULLING(右键收竿) -> RECAST_DELAY -> RECAST(右键抛竿) -> WAITING_BITE
    WAITING_BITE -> (检测到钓点枯竭) -> DEPLETED (停止钓鱼，等待后续阶段自动寻找新钓点)

操作说明：
- 拉鱼阶段: 检测到咬钩后右键点击一次（收竿）。
- 重抛等待: 随机 recast_delay_min_ms ~ recast_delay_max_ms。
- 重抛: 一次独立右键点击（抛竿）。
- 钓点枯竭: OCR 识别到「鱼群：枯竭」并持续确认后进入 DEPLETED，停止抛竿与咬钩检测。

并发控制：
- stop_event: F7 立即停止，最高优先级，所有循环都必须检查。
- pause_event: F6 暂停/恢复，暂停时不执行任何鼠标操作。
- 运行在独立线程，主线程负责快捷键与 UI。

安全：
- 每次鼠标操作前检查 Minecraft 窗口有效性，无效则立即 STOP。
"""

from __future__ import annotations

import random
import threading
import time
from enum import Enum, auto
from typing import Optional

from config import AppConfig
from detector import BiteDetector
from input_controller import InputController
from minecraft_window import MinecraftWindow
from logger import get_logger
from ocr import OcrService


class State(Enum):
    WAITING_BITE = auto()
    PULLING = auto()
    RECAST_DELAY = auto()
    RECAST = auto()
    DEPLETED = auto()      # 阶段三：钓点枯竭，停止钓鱼


class FishingEngine:
    """自动钓鱼状态机引擎"""

    def __init__(
        self,
        config: AppConfig,
        mc_window: MinecraftWindow,
        detector: BiteDetector,
        input_ctrl: InputController,
        ocr_service: Optional[OcrService] = None,
    ) -> None:
        self._cfg = config
        self._mc = mc_window
        self._detector = detector
        self._input = input_ctrl
        self._ocr = ocr_service
        self._log = get_logger()

        self._state: State = State.WAITING_BITE
        self._running: bool = False

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        # 阶段三：钓点枯竭检测 (时间窗确认，避免 OCR 闪烁误判)
        self._depleted_since: float = 0.0       # 首次检测到枯竭的时间戳
        self._depleted_warned: bool = False     # 枯竭检测不可用警告 (只提示一次)

        self._thread: Optional[threading.Thread] = None

    # ---------------- 公共控制接口 ----------------

    def start(self) -> bool:
        if self._running:
            self._log.warn("自动钓鱼已在运行中")
            return False
        if not self._mc.is_valid():
            self._log.error("Minecraft 窗口无效，无法启动自动钓鱼")
            return False
        self._stop_event.clear()
        self._pause_event.clear()
        self._state = State.WAITING_BITE
        self._detector.reset()
        self._depleted_since = 0.0
        self._depleted_warned = False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="FishingEngine", daemon=True)
        self._thread.start()
        self._log.info("自动钓鱼启动")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._log.info("正在停止自动钓鱼...")
        self._stop_event.set()
        self._pause_event.clear()
        self._running = False

    def toggle_pause(self) -> bool:
        if not self._running:
            self._log.warn("自动钓鱼未运行，无法暂停")
            return False
        if self._pause_event.is_set():
            self._pause_event.clear()
            self._log.info("已恢复自动钓鱼")
            return False
        else:
            self._pause_event.set()
            self._log.info("已暂停")
            return True

    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def get_state(self) -> State:
        """当前状态机状态 (供 GUI 显示)"""
        return self._state

    def get_state_name(self) -> str:
        """当前状态名 (中文，供 GUI 显示)"""
        return {
            State.WAITING_BITE: "等待咬钩",
            State.PULLING: "收竿中",
            State.RECAST_DELAY: "抛竿等待",
            State.RECAST: "抛竿",
            State.DEPLETED: "钓点枯竭",
        }.get(self._state, str(self._state))

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    # ---------------- 内部状态机 ----------------

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._wait_for_resume_or_stop()
                    if self._stop_event.is_set():
                        break
                    continue
                if self._state == State.WAITING_BITE:
                    self._state_waiting_bite()
                elif self._state == State.PULLING:
                    self._state_pulling()
                elif self._state == State.RECAST_DELAY:
                    self._state_recast_delay()
                elif self._state == State.RECAST:
                    self._state_recast()
                elif self._state == State.DEPLETED:
                    self._state_depleted()
                if not self._stop_event.is_set():
                    time.sleep(0.01)
        except Exception as e:
            self._log.error(f"钓鱼线程异常: {e}")
        finally:
            self._running = False
            self._log.info("自动钓鱼已停止")

    def _wait_for_resume_or_stop(self) -> None:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.05)

    def _check_safety(self) -> bool:
        if not self._mc.is_valid():
            self._log.error("Minecraft 窗口已失效，立即停止自动钓鱼")
            self.stop()
            return False
        return True

    # ---------------- 阶段三：钓点枯竭检测 ----------------

    def _ocr_ready(self) -> bool:
        """OCR 服务是否就绪 (可用于枯竭检测)"""
        return (
            self._ocr is not None
            and self._ocr.engine_available
            and self._ocr.is_running
        )

    def _check_depleted(self) -> bool:
        """
        检查钓点是否枯竭。OCR 在独立后台线程持续识别「鱼群：枯竭」面板，
        本方法只读最新 GameState，不阻塞。
        要求枯竭持续 depleted_confirm_ms 才确认，避免 OCR 闪烁误判。
        """
        if not self._cfg.ocr.depleted_enabled:
            return False
        if not self._ocr_ready():
            if not self._depleted_warned:
                self._depleted_warned = True
                self._log.warn(
                    "钓点枯竭检测需要 OCR 服务运行 (ocr.enabled=true 或检测模式为 "
                    "hybrid/ocr)，当前 OCR 不可用，枯竭检测关闭。"
                )
            return False
        state = self._ocr.get_latest_state()
        depleted = bool(state and state.depleted)
        now = time.monotonic()
        if depleted:
            if self._depleted_since == 0.0:
                self._depleted_since = now
                self._log.info("检测到疑似钓点枯竭，正在确认...")
                return False
            confirm_s = self._cfg.ocr.depleted_confirm_ms / 1000.0
            if now - self._depleted_since >= confirm_s:
                return True
            return False
        # 未检测到 → 重置
        self._depleted_since = 0.0
        return False

    def _state_depleted(self) -> None:
        """DEPLETED: 钓点枯竭，停止钓鱼，等待 (后续阶段自动寻找新钓点)"""
        # 进入此状态时已记录日志；此处阻塞线程等待用户停止或后续阶段接管
        self._interruptible_sleep(0.5)

    def _state_waiting_bite(self) -> None:
        """WAITING_BITE: 循环截图检测咬钩"""
        self._log.info("等待咬钩...")
        interval = self._cfg.detection.screenshot_interval_ms / 1000.0

        while not self._stop_event.is_set() and not self._pause_event.is_set():
            if not self._check_safety():
                return
            # 阶段三：优先检测钓点枯竭 (OCR 后台线程识别「鱼群：枯竭」)
            if self._check_depleted():
                self._log.warn(
                    "钓点枯竭！停止钓鱼，进入 DEPLETED 状态 "
                    "(等待后续阶段自动寻找新钓点)"
                )
                self._state = State.DEPLETED
                return
            result = self._detector.detect_bite()
            if result.bite_detected:
                if result.adaptive_used:
                    a_ref = result.a_reference
                    b_ref = result.b_reference
                    a_ref_s = f"RGB{a_ref}" if a_ref else "?"
                    b_ref_s = f"RGB{b_ref}" if b_ref else "?"
                    self._log.info(
                        f"检测到咬钩！ [自适应] "
                        f"A:通过 {result.a_current_rgb}≈{a_ref_s} "
                        f"(±{result.a_tolerance[0]},N={result.a_samples}) "
                        f"B:通过 {result.b_current_rgb}≈{b_ref_s} "
                        f"(±{result.b_tolerance[0]},N={result.b_samples})"
                    )
                elif result.dual_color_used:
                    self._log.info(
                        f"检测到咬钩！ [双重取色] "
                        f"A:通过({result.color_a_large}px) "
                        f"B:通过({result.color_b_large}px)"
                    )
                else:
                    self._log.info(
                        f"检测到咬钩！ (匹配度={result.match_score:.3f}, "
                        f"大连通域白像素={result.large_white_pixels})"
                    )
                self._state = State.PULLING
                return
            self._interruptible_sleep(interval)

    def _state_pulling(self) -> None:
        """PULLING: 检测到咬钩后右键点击一次（收竿拉鱼）"""
        self._log.info("咬钩！右键收竿")
        if not self._check_safety():
            return
        self._input.right_click()
        self._state = State.RECAST_DELAY

    def _state_recast_delay(self) -> None:
        """RECAST_DELAY: 随机等待"""
        fish = self._cfg.fishing
        delay_ms = random.randint(fish.recast_delay_min_ms, fish.recast_delay_max_ms)
        delay_s = delay_ms / 1000.0
        self._log.info(f"等待重新抛竿：{delay_s:.2f} 秒")
        self._interruptible_sleep(delay_s)
        if not self._stop_event.is_set():
            self._state = State.RECAST

    def _state_recast(self) -> None:
        """RECAST: 一次独立右键点击重新抛竿"""
        if not self._check_safety():
            return
        self._log.info("右键抛竿")
        self._input.right_click()
        self._detector.reset()
        self._state = State.WAITING_BITE

    def _interruptible_sleep(self, seconds: float) -> None:
        """可中断的 sleep"""
        end = time.monotonic() + seconds
        while True:
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))
