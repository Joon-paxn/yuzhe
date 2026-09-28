"""钓鱼引擎。

忠实保留 Tau 的钓鱼逻辑：
- 像素颜色检测咬钩
- 确认时间内持续匹配
- 右键收竿 → 等待 3~4 秒 → 右键抛竿
- 超时无鱼 → 枯竭 → 触发换池

不修改原有的咬钩检测/拉杆/抛竿逻辑。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional, Tuple

from .state import FishingState

logger = logging.getLogger("fishing.engine")


class FishingEngine:
    def __init__(
        self,
        grab_pixel: Callable[[int, int], Optional[Tuple[int, int, int]]],
        right_click: Callable[[], None],
        on_depleted: Optional[Callable[[], None]] = None,
        config: Optional[dict] = None,
    ):
        self._grab_pixel = grab_pixel
        self._right_click = right_click
        self._on_depleted = on_depleted
        cfg = config or {}
        self._poll_rate = cfg.get("polling_rate", 50) / 1000.0
        self._jitter = cfg.get("polling_jitter", 50) / 1000.0
        self._color_tolerance = cfg.get("color_tolerance", 10)
        self._no_fish_timeout = cfg.get("no_fish_timeout", 30)
        self._confirmation_time = cfg.get("confirmation_time", 0.3)
        self._reel_wait_min = cfg.get("reel_wait_min", 4.0)
        self._reel_wait_max = cfg.get("reel_wait_max", 6.0)
        self._cast_delay_min = cfg.get("cast_delay_min", 0.1)
        self._cast_delay_max = cfg.get("cast_delay_max", 0.4)
        self._px_color = cfg.get("px_color", "#FFFFFF")
        self._m_pos: Optional[Tuple[int, int]] = None
        self._target_rgb = self._hex_to_rgb(self._px_color)

        self._state = FishingState.IDLE
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._catch_count = 0
        self._fishing_active = False
        self._depleted_alerted = False
        self._last_throw = 0.0
        self._auto_throw = True
        self._skip_initial_throw = False

    @property
    def state(self) -> FishingState:
        return self._state

    @property
    def catch_count(self) -> int:
        return self._catch_count

    @property
    def is_active(self) -> bool:
        return self._fishing_active

    def set_target_pixel(self, pos: Tuple[int, int], color: str) -> None:
        self._m_pos = pos
        self._px_color = color
        self._target_rgb = self._hex_to_rgb(color)

    def start(self, auto_throw: bool = True, skip_initial: bool = False) -> None:
        if self._fishing_active:
            return
        self._stop_event.clear()
        self._auto_throw = auto_throw
        self._skip_initial_throw = skip_initial
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._fishing_active = False
        self._state = FishingState.STOPPED
        logger.info("钓鱼循环已停止")

    def _loop(self) -> None:
        if self._m_pos is None:
            logger.error("未设置钓鱼像素位置 M")
            self._state = FishingState.ERROR
            return
        mx, my = int(self._m_pos[0]), int(self._m_pos[1])
        tol = self._color_tolerance
        try:
            # 初始抛竿
            if self._skip_initial_throw:
                self._last_throw = time.time()
                self._depleted_alerted = False
                logger.info("直接进入自动钓鱼循环")
            else:
                logger.info("请先进行一次手动抛竿")
                # 等待手动抛竿或自动抛竿
                wait_start = time.time()
                while not self._stop_event.is_set():
                    if self._auto_throw:
                        self._right_click()
                        if self._stop_event.wait(2.0):
                            return
                        break
                    if time.time() - wait_start > 30:
                        logger.warning("长时间未检测到抛竿")
                        if self._stop_event.wait(1.0):
                            return
                self._last_throw = time.time()
                self._depleted_alerted = False

            self._state = FishingState.FISHING
            self._fishing_active = True
            logger.info("开始自动钓鱼循环")

            while not self._stop_event.is_set():
                # 超时枯竭检测
                if time.time() - self._last_throw > self._no_fish_timeout:
                    if not self._depleted_alerted:
                        logger.warning("该地区鱼群枯竭，请换个地方")
                        self._depleted_alerted = True
                        self._state = FishingState.DEPLETED
                        if self._on_depleted:
                            self._on_depleted()
                            return  # 换池由外部接管
                    if self._auto_throw:
                        self._right_click()
                        self._last_throw = time.time()
                        self._depleted_alerted = False
                        if self._stop_event.wait(2.0):
                            break
                        continue
                    else:
                        if self._stop_event.wait(self._poll_rate):
                            break
                        continue

                # 咬钩检测
                pixel = self._grab_pixel(mx, my)
                match = (pixel is not None and
                         sum(abs(a - b) for a, b in zip(pixel, self._target_rgb)) <= tol)
                if match:
                    cstart = time.time()
                    confirmed = True
                    while time.time() - cstart < self._confirmation_time:
                        if self._stop_event.wait(0.1):
                            return
                        pix = self._grab_pixel(mx, my)
                        if pix is None or sum(abs(a - b) for a, b in zip(pix, self._target_rgb)) > tol:
                            confirmed = False
                            break
                    if confirmed:
                        self._state = FishingState.BITE
                        logger.info("检测到上钩，收竿！")
                        self._catch_count += 1
                        self._state = FishingState.PULLING
                        self._right_click()
                        wait = random.uniform(self._reel_wait_min, self._reel_wait_max)
                        logger.info(f"等待 {wait:.1f} 秒后重新抛竿...")
                        if self._stop_event.wait(wait):
                            break
                        self._right_click()
                        if self._stop_event.wait(random.uniform(self._cast_delay_min, self._cast_delay_max)):
                            break
                        self._last_throw = time.time()
                        self._depleted_alerted = False
                        self._state = FishingState.FISHING
                        continue
                interval = max(0.05, self._poll_rate + random.uniform(-self._jitter, self._jitter))
                if self._stop_event.wait(interval):
                    break
        finally:
            self._fishing_active = False
            self._state = FishingState.STOPPED
            logger.info("钓鱼循环已停止")

    @staticmethod
    def _hex_to_rgb(hex_str: str) -> Tuple[int, int, int]:
        h = hex_str.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
