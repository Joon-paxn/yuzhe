"""Exercise real FlashWindowEx/winsound calls without taking focus or fishing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ctypes
import tempfile
import time
import tkinter as tk
from alert_manager import AlertManager, WindowsAlertBackend, AlertState
from config import AlertConfig
from events import EventStore
from game_log import LogEvent


class TracedBackend(WindowsAlertBackend):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.beeps = 0
        self.closed = False
        self.focus_mode = False
    def foreground(self):
        # Inject focus states; FlashWindowEx and Beep below remain native calls.
        return self.focus_mode
    def flash(self, active):
        super().flash(active)
        self.calls.append(active)
    def beep(self, frequency, duration, cancel):
        self.beeps += 1
        super().beep(frequency, duration, cancel)
    def close(self):
        super().close()
        self.closed = True


def run():
    temp = tempfile.TemporaryDirectory()
    store = EventStore(Path(temp.name)/"events.jsonl")
    root = tk.Tk()
    root.title("AutoFishing alarm verification")
    root.geometry("380x120")
    tk.Label(root, text="Windows 报警验证：随后自动关闭").pack(pady=25)
    root.update()
    backend = TracedBackend()
    u32 = ctypes.WinDLL("user32")
    u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u32.GetAncestor.restype = ctypes.c_void_p
    backend.hwnd = u32.GetAncestor(root.winfo_id(), 2)
    config = AlertConfig(sound_frequency=660, sound_duration_ms=100, repeat_interval_ms=400)
    alerts = AlertManager(config, store, backend)
    root.withdraw()
    failures = []
    def callback_failed(exc_type, exc, tb):
        failures.append(exc)
        root.destroy()
    root.report_callback_exception = callback_failed
    event = LogEvent("native-1", "SPOT_DEPLETED", "钓点已枯竭", time.monotonic(), "")
    root.after(150, lambda: alerts.trigger(event))
    def check():
        assert backend.beeps >= 2, backend.beeps
        assert True in backend.calls, backend.calls
        backend.focus_mode = True
        def foreground_checked():
            assert backend.calls[-1] is False
            backend.focus_mode = False
            root.after(300, acknowledge)
        root.after(300, foreground_checked)
    def acknowledge():
        assert backend.calls[-1] is True
        alerts.acknowledge()
        assert backend.calls[-1] is False
        count = backend.beeps
        def stopped():
            assert backend.beeps == count
            alerts.trigger(LogEvent("native-2", "NO_FISH_ACTIVITY", "没有鱼群", time.monotonic(), ""))
            root.after(300, restarted)
        root.after(800, stopped)
    def restarted():
        assert backend.calls[-1] is True
        root.destroy()
    root.after(1500, check)
    try: root.mainloop()
    finally:
        alerts.stop()
        store.close()
        temp.cleanup()
    if failures: raise failures[0]
    assert not alerts._thread.is_alive()
    assert backend.closed
    assert backend.calls[-1] is False
    print("Native Beep/FlashWindowEx invocation, injected foreground/background, repeat, acknowledge, reactivation, exit PASS")

if __name__ == "__main__": run()
