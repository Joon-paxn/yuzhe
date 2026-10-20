"""Per-thread screenshot handles and window-relative regions."""
import threading
import mss
import numpy as np


class WindowCapture:
    def __init__(self, window):
        self.window = window
        self._local = threading.local()
        self._handles = set()
        self._lock = threading.RLock()
        self._generation = 0

    def capture(self, roi):
        rect = self.window.get_rect()
        if rect is None or rect.width <= 0 or rect.height <= 0:
            return None, (0, 0, 0, 0)
        x, y = rect.left + int(rect.width * roi.x), rect.top + int(rect.height * roi.y)
        w = min(max(1, int(rect.width * roi.width)), rect.right - x)
        h = min(max(1, int(rect.height * roi.height)), rect.bottom - y)
        if w <= 0 or h <= 0:
            return None, (0, 0, 0, 0)
        with self._lock:
            if (not hasattr(self._local, "sct")
                    or getattr(self._local, "generation", -1) != self._generation):
                old = getattr(self._local, "sct", None)
                if old is not None:
                    self._handles.discard(old)
                self._local.sct = mss.mss()
                self._local.generation = self._generation
                self._handles.add(self._local.sct)
            shot = self._local.sct.grab(dict(left=x, top=y, width=w, height=h))
        return np.asarray(shot)[:, :, :3].copy(), (x, y, w, h)

    def close(self):
        with self._lock:
            self._generation += 1
            handles = list(self._handles)
            self._handles.clear()
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass
        if hasattr(self._local, "sct"):
            del self._local.sct
