"""Detect sustained manual WASD input without touching the UI thread."""
from __future__ import annotations

import ctypes
import threading
import time


class UserActivityMonitor:
    _KEYS = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44}

    def __init__(self, window, config, on_sustained):
        self.window = window
        self.config = config
        self.on_sustained = on_sustained
        self._stop = threading.Event()
        self._thread = None
        self._reported = False
        self._since = 0.0
        self.last_active = False
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="UserActivity", daemon=True)
        self._thread.start()

    def _keys_down(self):
        return any(self._user32.GetAsyncKeyState(vk) & 0x8000 for vk in self._KEYS.values())

    def _run(self):
        while not self._stop.is_set():
            active = False
            try:
                active = bool(self.config.enabled and self.window.is_foreground() and self._keys_down())
            except Exception:
                active = False
            now = time.monotonic()
            self.last_active = active
            if active:
                if not self._since:
                    self._since = now
                if not self._reported and now - self._since >= self.config.threshold_ms / 1000:
                    self._reported = True
                    try:
                        self.on_sustained()
                    except Exception:
                        pass
            else:
                self._since = 0.0
                self._reported = False
            self._stop.wait(self.config.poll_interval_ms / 1000)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
