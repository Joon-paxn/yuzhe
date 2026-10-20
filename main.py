"""AutoFishing desktop coordinator. Tk only runs on the main thread."""
from __future__ import annotations
import ctypes
import queue
import sys
import threading
import time
import traceback
from dataclasses import asdict

from config import BASE_DIR, CONFIG_PATH, AppConfig, RoiConfig, load_config, save_config
from minecraft_window import MinecraftWindow
from title_detector import TitleDetector
from bobber_detector import BobberMotionDetector
from input_controller import InputController
from fishing import FishingEngine
from events import EventStore, timestamp
from alert_manager import AlertManager, AlertState
from game_log import GameLogMonitor, discover_logs
from hotkeys import HotkeyManager
from logger import get_logger
from user_activity import UserActivityMonitor


class AutoFishingApp:
    def __init__(self, history_path=None):
        self.config = load_config()
        self.log = get_logger()
        self.tasks = queue.Queue()
        self.window = MinecraftWindow(self.config.window.title_keyword, list(self.config.window.process_names))
        self.store = EventStore(history_path or BASE_DIR / "data" / "events.jsonl")
        self.alerts = AlertManager(self.config.alert, self.store)
        self.detector = TitleDetector(self.window, self.config.title, self.config.roi, self.log)
        self.bobber = BobberMotionDetector(self.window, self.config.bobber)
        self.engine = FishingEngine(self.config, self.window, self.detector,
                                    InputController(self.window), self.store, self.bobber)
        self.game_log = GameLogMonitor(self.config.game_log, self._on_log)
        self.engine.log_boundary = self.game_log.result_boundary
        self.hotkeys = HotkeyManager(self.config.hotkeys)
        self.hotkeys.on_toggle = lambda: self.tasks.put("toggle")
        self.hotkeys.on_stop = lambda: self.engine.stop()
        self.hotkeys.on_select_region = lambda: self.tasks.put("roi")
        self.hotkeys.on_test = lambda: self.tasks.put("test")
        self.hotkeys.on_capture_template = lambda: self.tasks.put("snapshot")
        self.hotkeys.on_pick_color = lambda: self.tasks.put("bobber_roi")
        self.gui = None
        self._test_thread = None
        self._test_stop = threading.Event()
        self._closing = False
        self._warm_thread = None
        self.user_activity = UserActivityMonitor(self.window, self.config.user_activity,
                                                 lambda: self.tasks.put("user_active"))

    def _on_log(self, event):
        self.store.add("log", dict(asdict(event), timestamp=timestamp()))
        self.engine.on_log_event(event)
        if event.kind in ("SPOT_DEPLETED", "NO_FISH_ACTIVITY", "ROD_BROKEN", "DISCONNECTED") or event.kind.startswith("ERROR_"):
            self.engine.stop()
            self.alerts.trigger(event)

    def run(self):
        from gui import FishingGUI
        self.window.find()
        self.gui = FishingGUI(self)
        root = self.gui.build()
        root.update_idletasks()
        user32 = ctypes.WinDLL("user32")
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        self.alerts.backend.hwnd = user32.GetAncestor(root.winfo_id(), 2)
        self.game_log.start()
        self.user_activity.start()
        self._warm_thread = threading.Thread(target=self.detector.warmup, name="OCRWarmup", daemon=True)
        self._warm_thread.start()
        if not self.hotkeys.register(): self.gui.notice.set("快捷键不可用，仍可使用界面按钮")
        root.after(100, self.gui.tick)
        try: root.mainloop()
        finally: self.cleanup()
        return 0

    def find_window(self):
        self.window._keyword = self.config.window.title_keyword
        return self.window.find()

    def apply_config(self, config):
        """Swap configuration references after reset without restarting worker threads."""
        self.config = config
        self.window._keyword = config.window.title_keyword
        self.detector.config = config.title
        self.detector.roi = config.roi
        self.bobber.config = config.bobber
        self.engine.config = config
        self.alerts.config = config.alert
        self.game_log.config = config.game_log
        self.hotkeys._cfg = config.hotkeys
        self.user_activity.config = config.user_activity

    def reset_config(self):
        config = AppConfig()
        self.apply_config(config)
        self.game_log.reconnect()
        self.detector.reset_cycle()
        save_config(CONFIG_PATH, config)
        if self.gui:
            self.gui.refresh_settings_from_config()
            self.gui.root.geometry(config.ui.window_geometry)
            self.gui.notice.set("设置已重置为默认值并保存")

    def on_user_activity(self):
        """Handle the monitor callback on the Tk thread via the task queue."""
        if self.engine.pause_for_user() and self.gui:
            self.gui.notice.set("检测到 WASD 操作，已暂停并释放游戏资源；按 F6 恢复")

    def toggle(self):
        if self.engine.is_running():
            self.engine.toggle_pause()
            return
        if self._test_thread and self._test_thread.is_alive():
            self.gui.notice.set("检测测试正在运行")
            return
        if self._warm_thread and self._warm_thread.is_alive():
            self.gui.notice.set("中文 OCR 模型正在加载，请稍候")
            return
        if not self.detector.ready:
            self.gui.notice.set("OCR 尚未就绪，请测试 Title 并检查 OCR 状态")
            return
        state, _ = self.alerts.snapshot()
        if state == AlertState.ALERT_ACTIVE:
            self.gui.notice.set("请先确认异常并点击停止报警")
            return
        if not self.config.roi.configured:
            self.gui.notice.set("请先选择 Title 区域")
            return
        if not self.window.is_valid() and not self.find_window():
            self.gui.notice.set("未找到 Minecraft，请进入游戏后刷新窗口")
            return
        self.engine.start()

    def select_roi(self, bobber=False):
        if self.engine.is_running() or (self._test_thread and self._test_thread.is_alive()):
            self.gui.notice.set("请先停止自动钓鱼和检测测试")
            return
        if not self.window.is_valid() and not self.find_window():
            self.gui.notice.set("未找到 Minecraft 窗口")
            return
        from region_selector import select_roi_relative
        self.gui.root.withdraw()
        try:
            result = select_roi_relative(self.window)
            if result:
                roi = RoiConfig(*result, configured=True)
                if bobber:
                    self.config.bobber.roi = roi
                    self.config.bobber.enabled = True
                    self.gui.bobber_enabled.set(True)
                else:
                    self.config.roi = roi
                    self.detector.roi = roi
                    self.detector.reset_cycle()
                save_config(CONFIG_PATH, self.config)
                self.gui.notice.set("鱼漂区域已保存" if bobber else "Title 区域已保存")
        finally:
            self.gui.root.deiconify()

    def test(self, snapshot=False):
        if self.engine.is_running():
            self.gui.notice.set("请先停止自动钓鱼")
            return
        if self._test_thread and self._test_thread.is_alive():
            self._test_stop.set()
            return
        if not self.window.is_valid() and not self.find_window():
            self.gui.notice.set("未找到 Minecraft 窗口")
            return
        self._test_stop.clear()
        self.detector.reset()
        self.gui.notice.set("Title 检测测试中" if not snapshot else "正在刷新 ROI 预览")
        def work():
            deadline = time.monotonic() + (1 if snapshot else 12)
            try:
                while not self._test_stop.is_set() and time.monotonic() < deadline:
                    self.detector.confirm()
                    if snapshot: break
                    self._test_stop.wait(.08)
            except Exception as exc:
                self.log.error(f"Title 测试失败：{exc}")
            finally:
                self.detector.close()
                self.tasks.put("test_done")
        self._test_thread = threading.Thread(target=work, name="TitleTest", daemon=True)
        self._test_thread.start()

    def test_image(self, path, crop_roi=False):
        if self.engine.is_running() or (self._test_thread and self._test_thread.is_alive()):
            self.gui.notice.set("请先停止钓鱼和检测测试")
            return
        self._test_stop.clear()
        self.gui.notice.set("截图 OCR 测试中")
        def work():
            from PIL import Image
            import numpy as np
            from title_detector import TitleResult
            try:
                frame = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
                if crop_roi:
                    roi = self.config.roi
                    h, w = frame.shape[:2]
                    frame = frame[int(h*roi.y):int(h*(roi.y+roi.height)), int(w*roi.x):int(w*(roi.x+roi.width))]
                if not frame.size: raise ValueError("截图 ROI 为空")
                self.detector.reset()
                # An imported still is a simulation, never a game input source.
                for _ in range(self.config.title.clear_frames):
                    self.detector.confirm_result(TitleResult(roi=(0, 0, frame.shape[1], frame.shape[0])))
                for _ in range(self.config.title.title_confirm_frames):
                    if self._test_stop.is_set(): break
                    self.detector.confirm_result(self.detector.detect_frame(frame, (0, 0, frame.shape[1], frame.shape[0])))
            except Exception as exc:
                self.log.error(f"截图测试失败：{exc}")
            finally:
                self.tasks.put("test_done")
        self._test_thread = threading.Thread(target=work, name="ScreenshotTest", daemon=True)
        self._test_thread.start()

    def save_debug_sample(self):
        from tkinter import filedialog
        from PIL import Image
        from pathlib import Path
        import json
        import cv2
        result, images = self.detector.debug_snapshot()
        if not images:
            self.gui.notice.set("没有可保存的截图")
            return
        directory = filedialog.askdirectory(title="选择调试样本保存目录")
        if not directory: return
        target = Path(directory) / time.strftime("title-%Y%m%d-%H%M%S")
        try:
            target.mkdir()
            for name, frame in images.items():
                data = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                Image.fromarray(data).save(target / f"{name}.png")
            (target / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.gui.notice.set(f"调试样本已保存：{target}")
        except OSError as exc:
            self.gui.notice.set(f"保存失败：{exc}")

    def cleanup(self):
        if self._closing: return
        self._closing = True
        if self.gui and getattr(self.gui, "alert_bubble", None):
            self.gui.alert_bubble.destroy()
        self.hotkeys.unregister()
        self.engine.stop()
        self._test_stop.set()
        self.user_activity.stop()
        self.game_log.close()
        self.alerts.stop()
        self.engine.wait(timeout=None)
        if self._test_thread: self._test_thread.join(3)
        if self._warm_thread: self._warm_thread.join(3)
        self.store.close()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError): pass
    app = AutoFishingApp()
    try:
        return app.run()
    finally:
        app.cleanup()


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as exc:
        (BASE_DIR / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(0, f"{exc}\n详细错误见 error.log", "AutoFishing", 0x10)
        sys.exit(1)
