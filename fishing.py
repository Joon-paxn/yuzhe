"""Title -> immediate reel -> log result. All I/O runs outside the UI thread."""
from __future__ import annotations
import random
import threading
import time
from enum import Enum
from events import timestamp


class State(str, Enum):
    IDLE = "IDLE"
    CASTING = "CASTING"
    WAITING_BITE = "WAITING_BITE"
    BITE_CANDIDATE = "BITE_CANDIDATE"
    BITE_CONFIRMED = "BITE_CONFIRMED"
    REELING = "REELING"
    WAITING_RESULT = "WAITING_RESULT"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"


class FishingEngine:
    def __init__(self, config, window, detector, input_ctrl, store, bobber=None, log_boundary=None):
        self.config, self.window, self.detector = config, window, detector
        self.input, self.store, self.bobber = input_ctrl, store, bobber
        self.log_boundary = log_boundary
        self.state = State.IDLE
        self.message = "就绪"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = None
        self._running = False
        self._pending = None
        self._used_results = set()
        self._result_deadline = 0.0
        self._candidate_at = 0.0
        self._auto_paused = False
        self._regain_at = 0.0
        self._cycle_start = 0.0
        self._user_paused = False

    def is_running(self): return self._running
    def is_paused(self): return self._pause.is_set() or self._auto_paused

    def start(self):
        if self._thread and self._thread.is_alive(): return False
        if not self.window.is_valid() or not self.config.roi.configured: return False
        self._stop.clear()
        self._pause.clear()
        self._auto_paused = False
        self._pending = None
        self.detector.reset()
        self._cycle_start = time.monotonic()
        self.state = State.CASTING if self.config.fishing.initial_cast else State.WAITING_BITE
        self._running = True
        self._thread = threading.Thread(target=self._run, name="FishingEngine", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.message = "已停止"

    def toggle_pause(self):
        if self._pause.is_set():
            self._pause.clear()
            self._user_paused = False
            self.message = "已恢复自动钓鱼"
        else:
            self._pause.set()

    def pause_for_user(self):
        if not self._running:
            return False
        self._pause.set()
        self._user_paused = True
        self.detector.reset_candidate()
        self.detector.release_resources()
        if self.bobber:
            self.bobber.release_resources()
        self.message = "检测到用户正在操作，已释放游戏资源；按 F6 恢复"
        return True

    @property
    def user_paused(self):
        return self._user_paused

    def wait(self, timeout=3):
        if self._thread: self._thread.join(timeout)

    def on_log_event(self, event):
        # This entry point only resolves results. It cannot perform input or confirm Title.
        with self._lock:
            if event.id in self._used_results: return
            if self.state != State.WAITING_RESULT or self._pending is None: return
            if event.at < self._pending.reel_monotonic or event.at > self._result_deadline: return
            if self._pending.log_source and (event.source != self._pending.log_source
                                              or event.offset < self._pending.log_start_offset): return
            if event.kind == "SUCCESS":
                self._used_results.add(event.id)
                self._finish("SUCCESS", event)
            elif event.kind in ("SPOT_DEPLETED", "NO_FISH_ACTIVITY", "ROD_BROKEN", "DISCONNECTED") or event.kind.startswith("ERROR_"):
                self._used_results.add(event.id)
                self._finish("FAILURE", event)

    def _finish(self, result, event=None):
        if self._pending is None: return
        self._pending.result = result
        self._pending.result_event_id = event.id if event else ""
        self._pending.fish = event.fish if event else ""
        self._pending.reason = event.kind if event else "未确认鱼获"
        self._pending.latency_ms = (time.monotonic() - self._pending.reel_monotonic) * 1000
        self.store.bite(self._pending)
        self.state = State(result)
        self.message = event.text if event else "未确认鱼获"

    def _focus_ready(self):
        if not self.window.is_foreground():
            self._auto_paused = True
            self._regain_at = 0.0
            self.message = "游戏失焦，自动暂停"
            self.detector.reset_candidate()
            return False
        if self._auto_paused:
            if not self._regain_at: self._regain_at = time.monotonic()
            if time.monotonic() - self._regain_at < 3:
                self.message = "回焦等待中"
                return False
            self._auto_paused = False
        self._user_paused = False
        return not self._pause.is_set()

    def _click(self):
        if self._stop.is_set() or self._pause.is_set() or not self.window.is_foreground(): return False
        return self.input.right_click()

    def _run(self):
        try:
            while not self._stop.is_set():
                if not self.window.is_valid():
                    self.message = "Minecraft 窗口不可用"
                    break
                # Result timeouts are wall-time deadlines, also during manual/focus pauses.
                with self._lock:
                    if self.state == State.WAITING_RESULT and time.monotonic() >= self._result_deadline:
                        self._finish("UNKNOWN")
                    state = self.state
                if state in (State.FAILURE, State.UNKNOWN):
                    if state == State.FAILURE or self.config.fishing.stop_on_unknown: break
                if not self._focus_ready():
                    self.detector.reset_candidate()
                    self._stop.wait(.05)
                    continue
                if state == State.CASTING:
                    if not self._click():
                        self.message = "抛竿未执行"
                        break
                    self.detector.reset_cycle()
                    self._cycle_start = time.monotonic()
                    self.state = State.WAITING_BITE
                    self.message = "等待上钩 Title"
                elif state in (State.WAITING_BITE, State.BITE_CANDIDATE):
                    if time.monotonic() - self._cycle_start < self.config.title.min_wait_ms / 1000:
                        self._stop.wait(.03)
                        continue
                    boundary = self.log_boundary() if self.log_boundary else None
                    result, bite = self.detector.confirm()
                    if self._stop.is_set(): break
                    if bite:
                        # Once confirmed, reel immediately. No log polling or disk writes precede input.
                        with self._lock:
                            self.state = State.BITE_CONFIRMED
                            self.state = State.REELING
                            bite.bobber_confidence = self.bobber.confidence if self.bobber else 0.0
                            if boundary: bite.log_source, bite.log_start_offset = boundary
                            bite.reel_timestamp = timestamp()
                            bite.reel_monotonic = time.monotonic()
                            clicked = self._click()
                            self._pending = bite
                            if clicked:
                                self._result_deadline = bite.reel_monotonic + self.config.fishing.result_timeout / 1000
                                self.state = State.WAITING_RESULT
                                self.message = "已收杆，等待鱼获确认"
                            else:
                                bite.result = "FAILURE"
                                bite.reason = "鼠标操作被取消"
                                self.state = State.FAILURE
                            self.store.bite(bite)
                    elif result.matched:
                        if self.state != State.BITE_CANDIDATE: self._candidate_at = time.monotonic()
                        self.state = State.BITE_CANDIDATE
                        self.message = result.reason
                        if time.monotonic() - self._candidate_at > self.config.title.title_candidate_timeout / 1000:
                            self.detector.reset_candidate()
                            self.state = State.WAITING_BITE
                    else:
                        self.state = State.WAITING_BITE
                        self.message = result.reason
                    if self.bobber and not bite: self.bobber.detect()
                elif state == State.SUCCESS or (state == State.UNKNOWN and not self.config.fishing.stop_on_unknown):
                    delay = random.randint(self.config.fishing.recast_delay_min_ms,
                                           self.config.fishing.recast_delay_max_ms) / 1000
                    deadline = time.monotonic() + delay
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        self._stop.wait(.05)
                    if not self._stop.is_set(): self.state = State.CASTING
                self._stop.wait(self.config.title.screenshot_interval_ms / 1000)
        except Exception as exc:
            self.message = f"运行异常：{exc}"
            self.state = State.FAILURE
        finally:
            with self._lock:
                if self._pending and self._pending.result == "PENDING":
                    self._finish("UNKNOWN")
                self._running = False
                if self.state not in (State.FAILURE, State.UNKNOWN): self.state = State.IDLE
            self.detector.close()
            if self.bobber: self.bobber.close()
