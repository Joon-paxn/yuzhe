"""Nonblocking persistent alarms. No UI calls or focus stealing from workers."""
from __future__ import annotations
import ctypes
import threading
import time
from enum import Enum
from dataclasses import asdict

from events import timestamp


class AlertState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    ALERT_ACTIVE = "ALERT_ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


class WindowsAlertBackend:
    def __init__(self):
        self.hwnd = 0
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
        self._user32.FlashWindowEx.restype = ctypes.c_int
        self._user32.GetForegroundWindow.restype = ctypes.c_void_p

    def foreground(self):
        return self._user32.GetForegroundWindow() == self.hwnd

    def flash(self, active):
        if not self.hwnd: return
        # FLASHW_TRAY | FLASHW_TIMER: continuously flash until explicitly stopped.
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), self.hwnd,
                           2 | 4 if active else 0, 0, 0)
        self._user32.FlashWindowEx(ctypes.byref(info))

    def beep(self, frequency, duration, cancel):
        import winsound
        # Short chunks bound acknowledgement/exit latency to 50 ms.
        remaining = duration
        while remaining > 0 and not cancel.is_set():
            chunk = min(50, remaining)
            winsound.Beep(frequency, chunk)
            remaining -= chunk

    def close(self):
        self.flash(False)


class AlertManager:
    def __init__(self, config, store, backend=None):
        self.config, self.store = config, store
        self.backend = backend or WindowsAlertBackend()
        self.state = AlertState.NORMAL
        self.current = None
        self._seen = set()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel_sound = threading.Event()
        self._shutdown = threading.Event()
        self._flashing = False
        self._thread = threading.Thread(target=self._run, name="AlertManager", daemon=True)
        self._thread.start()

    def trigger(self, event):
        with self._lock:
            if event.id in self._seen: return False
            self._seen.add(event.id)
            self.current = dict(asdict(event), timestamp=timestamp(),
                                last_fish=self.store.stats()["last_fish"])
            self.state = AlertState.ALERT_ACTIVE if self.config.alert_enabled else AlertState.WARNING
            self._cancel_sound.clear()
            self.store.add("error", self.current)
        self._wake.set()
        return True

    def acknowledge(self):
        with self._lock:
            self._cancel_sound.set()
            self.state = AlertState.ACKNOWLEDGED
            self.backend.flash(False)
            self._flashing = False
            self.store.add("alert_ack", dict(timestamp=timestamp(),
                                             id=self.current["id"] if self.current else ""))
        self._wake.set()

    def snapshot(self):
        with self._lock:
            return self.state, dict(self.current) if self.current else None

    def _run(self):
        next_sound = 0.0
        sound_event_id = ""
        try:
            while not self._shutdown.is_set():
                with self._lock:
                    active = self.state == AlertState.ALERT_ACTIVE and self.config.alert_enabled
                    if active and self.current["id"] != sound_event_id:
                        sound_event_id = self.current["id"]
                        next_sound = 0.0
                    should_flash = active and not self.backend.foreground()
                    if should_flash != self._flashing:
                        self.backend.flash(should_flash)
                        self._flashing = should_flash
                    play = active and self.config.sound_enabled and time.monotonic() >= next_sound
                    if play:
                        next_sound = time.monotonic() + self.config.repeat_interval_ms / 1000
                        frequency, duration = self.config.sound_frequency, self.config.sound_duration_ms
                if play:
                    try:
                        self.backend.beep(frequency, duration, self._cancel_sound)
                    except (RuntimeError, OSError):
                        pass
                self._wake.wait(.15)
                self._wake.clear()
                if not active: next_sound = 0.0
        finally:
            self.backend.close()

    def stop(self):
        self._cancel_sound.set()
        self._shutdown.set()
        self._wake.set()
        self._thread.join(3)
        self.backend.flash(False)
