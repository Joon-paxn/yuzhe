"""
fishing.py
----------
自动钓鱼状态机核心模块。

状态流转：
    WAITING_BITE -> (检测到咬钩) -> PULLING(右键收竿) -> RECAST_DELAY -> RECAST(右键抛竿) -> WAITING_BITE

操作说明：
- 拉鱼阶段: 检测到咬钩后右键点击一次（收竿）。
- 重抛等待: 随机 recast_delay_min_ms ~ recast_delay_max_ms。
- 重抛: 一次独立右键点击（抛竿）。

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


class State(Enum):
    WAITING_BITE = auto()
    PULLING = auto()
    RECAST_DELAY = auto()
    RECAST = auto()


class FishingEngine:
    """自动钓鱼状态机引擎"""

    def __init__(
        self,
        config: AppConfig,
        mc_window: MinecraftWindow,
        detector: BiteDetector,
        input_ctrl: InputController,
    ) -> None:
        self._cfg = config
        self._mc = mc_window
        self._detector = detector
        self._input = input_ctrl
        self._log = get_logger()

        # 状态机状态
        self._state: State = State.WAITING_BITE
        self._running: bool = False

        # 并发控制事件
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停

        # 工作线程
        self._thread: Optional[threading.Thread] = None

    # ---------------- 公共控制接口 ----------------

    def start(self) -> bool:
        """
        启动自动钓鱼 (在新线程中运行)。

        Returns:
            是否成功启动 (已在运行则返回 False)
        """
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
        self._running = True

        self._thread = threading.Thread(target=self._run, name="FishingEngine", daemon=True)
        self._thread.start()
        self._log.info("自动钓鱼启动")
        return True

    def stop(self) -> None:
        """立即停止自动钓鱼 (F7，最高优先级)"""
        if not self._running:
            return
        self._log.info("正在停止自动钓鱼...")
        self._stop_event.set()
        # 如果暂停中，唤醒线程以便它能退出
        self._pause_event.clear()
        self._running = False

    def toggle_pause(self) -> bool:
        """
        切换暂停/恢复 (F6)。

        Returns:
            暂停状态 (True=已暂停, False=已恢复)
        """
        if not self._running:
            self._log.warn("自动钓鱼未运行，无法暂停")
            return False

        if self._pause_event.is_set():
            # 当前已暂停 -> 恢复
            self._pause_event.clear()
            self._log.info("已恢复自动钓鱼")
            return False
        else:
            # 当前运行中 -> 暂停
            self._pause_event.set()
            self._log.info("已暂停")
            return True

    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def wait(self) -> None:
        """等待工作线程结束"""
        if self._thread is not None:
            self._thread.join()

    # ---------------- 内部状态机 ----------------

    def _run(self) -> None:
        """工作线程主循环"""
        try:
            while not self._stop_event.is_set():
                # 暂停处理
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

                # 状态切换后短暂让步，避免空转
                if not self._stop_event.is_set():
                    time.sleep(0.01)
        except Exception as e:
            self._log.error(f"钓鱼线程异常: {e}")
        finally:
            self._running = False
            self._log.info("自动钓鱼已停止")

    def _wait_for_resume_or_stop(self) -> None:
        """暂停时阻塞等待恢复或停止"""
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.05)

    def _check_safety(self) -> bool:
        """
        安全检查：Minecraft 窗口是否仍有效。
        无效则触发停止并返回 False。
        """
        if not self._mc.is_valid():
            self._log.error("Minecraft 窗口已失效，立即停止自动钓鱼")
            self.stop()
            return False
        return True

    def _state_waiting_bite(self) -> None:
        """WAITING_BITE: 循环截图检测咬钩"""
        self._log.info("等待咬钩...")
        interval = self._cfg.detection.screenshot_interval_ms / 1000.0

        while not self._stop_event.is_set() and not self._pause_event.is_set():
            if not self._check_safety():
                return

            result = self._detector.detect_bite()
            if result.bite_detected:
                if result.dual_color_used:
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

            # 按检测间隔 sleep (可被停止/暂停中断)
            self._interruptible_sleep(interval)

    def _state_pulling(self) -> None:
        """
        PULLING: 检测到咬钩后右键点击一次（收竿拉鱼）。
        不再连点，只需一次右键即可收竿。
        """
        self._log.info("咬钩！右键收竿")
        if not self._check_safety():
            return
        self._input.right_click()
        self._state = State.RECAST_DELAY

    def _state_recast_delay(self) -> None:
        """RECAST_DELAY: 随机等待 3~4 秒"""
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
        """
        可中断的 sleep。

        在 stop 或 pause 时立即返回，避免长时间阻塞导致 F7 无法及时响应。
        每次最多 sleep 0.05 秒检查一次事件。
        """
        end = time.monotonic() + seconds
        while True:
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))
