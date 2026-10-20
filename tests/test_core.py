import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np

from alert_manager import AlertManager, AlertState
from config import AppConfig, AlertConfig, GameLogConfig, RoiConfig, TitleConfig
from events import EventStore
from fishing import FishingEngine, State
from game_log import GameLogMonitor, LogEvent
from title_detector import TitleDetector, TitleResult, normalize_text, preprocess_title, match_text
from capture import WindowCapture
from minecraft_window import WindowRect
from events import BiteEvent, timestamp


def until(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition(): return True
        threading.Event().wait(.005)
    return condition()


class Window:
    focused = True
    def is_valid(self): return True
    def is_foreground(self): return self.focused


class Backend:
    def __init__(self):
        self.focused = False
        self.flashes = []
        self.sounds = []
        self.closed = False
    def foreground(self): return self.focused
    def flash(self, active): self.flashes.append(active)
    def beep(self, frequency, duration, cancel):
        if not cancel.is_set(): self.sounds.append(time.monotonic())
    def close(self): self.flash(False); self.closed = True


class Input:
    def __init__(self): self.clicks = 0
    def right_click(self): self.clicks += 1; return True


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.temp.name) / "events.jsonl")
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def detector(self):
        d = TitleDetector(Window(), TitleConfig(bite_cooldown=0), RoiConfig())
        d.detect = lambda: TitleResult("上钩!", .95, 1, True, (0,0,200,100))
        d._armed = True
        return d

    def test_normalization_and_similar_text(self):
        for text in ("上钩！", "上 钩!\n", "上钩", "★上钩！★"):
            self.assertEqual(normalize_text(text), "上钩")
        d = TitleDetector(Window(), TitleConfig(), RoiConfig())
        d.capture.capture = lambda roi: (np.zeros((100,200,3),np.uint8), (0,0,200,100))
        for text in ("上钩", "上 钩！", "上钩!", "咬钩", "咬钩!", "咬 钩！\n", "★咬钩！★"):
            d._ocr = lambda frame, text=text: (text, .95)
            self.assertTrue(d.detect().matched)
        for text in ("没有上钩", "未上钩", "不上钩", "没上钩", "未咬钩", "没有咬钩", "钓点", "上钩成功", "鱼群活动"):
            d._ocr = lambda frame, text=text: (text, .95)
            self.assertFalse(d.detect().matched, text)
        d._ocr = lambda _: ("上钩", .3)
        self.assertFalse(d.detect().matched)

    def test_preprocessing_and_fusion(self):
        frame = np.zeros((40, 100, 3), np.uint8)
        frame[10:30, 20:40] = 240
        versions = preprocess_title(frame, 4)
        self.assertEqual(len(versions), 7)
        self.assertEqual(versions["enlarged"].shape, (160, 400, 3))
        self.assertEqual(versions["white_text"][80, 120], 0)
        self.assertEqual(versions["white_text"][0, 0], 255)
        d = TitleDetector(Window(), TitleConfig(), RoiConfig())
        # A very confident unrelated version must not overrule valid variants.
        answers = [("鱼群活动", .99), ("", 0), ("咬钩!", .9), ("咬 钩！", .92), ("", 0), ("", 0), ("上钩", .94)]
        d._ocr = lambda _: answers.pop(0)
        r = d.detect_frame(frame, (0,0,100,40))
        self.assertTrue(r.matched)
        self.assertEqual(r.normalized, "咬钩")
        self.assertEqual(r.best_variant, "sharpened")
        self.assertEqual(r.support_count, 3)
        self.assertEqual(r.detection_state, "BITE_CANDIDATE")
        d._armed = True
        self.assertIsNone(d.confirm_result(r)[1])
        self.assertIsNotNone(d.confirm_result(r)[1])
        self.assertEqual(r.detection_state, "BITE_CONFIRMED")

    def test_default_aliases_ignore_legacy_target(self):
        cfg = TitleConfig(target_text="上钩！")
        self.assertTrue(match_text("咬钩！", .9, cfg)[3])
        cfg.ocr_similarity_threshold = .65
        self.assertTrue(match_text("咬钩1", .9, cfg)[3])
        self.assertFalse(match_text("没咬钩", .99, cfg)[3])

    def test_consecutive_frames_and_dedup(self):
        d = self.detector()
        self.assertIsNone(d.confirm()[1])
        self.assertIsNotNone(d.confirm()[1])
        for _ in range(8): self.assertIsNone(d.confirm()[1])
        d.detect = lambda: TitleResult(roi=(0,0,200,100))
        d.confirm(); d.confirm()
        d.detect = lambda: TitleResult("上钩", .95, 1, True, (0,0,200,100))
        self.assertIsNone(d.confirm()[1])
        self.assertIsNotNone(d.confirm()[1])

    def test_negative_frame_breaks_confirmation(self):
        d = self.detector()
        positive = d.detect
        d.confirm()
        d.detect = lambda: TitleResult(roi=(0,0,200,100))
        d.confirm()
        d.detect = positive
        self.assertIsNone(d.confirm()[1])

    def test_window_expiry(self):
        d = self.detector()
        with patch("title_detector.time.monotonic", side_effect=[100.0, 102.0, 102.1]):
            self.assertIsNone(d.confirm()[1])
            self.assertIsNone(d.confirm()[1])
            self.assertIsNotNone(d.confirm()[1])

    def test_log_tail_results_offsets_and_new_errors(self):
        path = Path(self.temp.name) / "latest.log"
        old = "[12:00:00] [Render thread/INFO]: [CHAT] 上钩! 您钓到了 [旧鱼]\n"
        path.write_text(old, encoding="utf-8")
        emitted = []
        m = GameLogMonitor(GameLogConfig(path=str(path)), emitted.append)
        m.poll(); self.assertEqual(emitted, [])
        with path.open("a", encoding="utf-8") as f:
            f.write("[12:01:00] [Render thread/INFO]: [CHAT] 上钩! 您钓到了 [鲈鱼]\n")
            f.write("[12:01:01] [Render thread/INFO]: [CHAT] 不行! 这个钓点已经枯竭，换一个地方吧。\n")
            f.write("[12:01:02] [Render thread/INFO]: [CHAT] 不行! 您只能在有鱼群活动的钓点钓鱼。\n")
        m.poll(); m.poll()
        self.assertEqual([e.kind for e in emitted], ["SUCCESS", "SPOT_DEPLETED", "NO_FISH_ACTIVITY"])
        self.assertEqual(emitted[0].fish, "[鲈鱼]")
        m._classify(emitted[1].raw, emitted[1].offset)
        self.assertEqual(len(emitted), 3)
        with path.open("a", encoding="utf-8") as f: f.write(emitted[1].raw + "\n")
        m.poll(); self.assertEqual(len(emitted), 4)
        self.assertNotEqual(emitted[1].id, emitted[3].id)
        with path.open("a", encoding="utf-8") as f: f.write("[CHAT] 上钩！\n")
        m.poll(); self.assertEqual(len(emitted), 4)  # title-like chat has no trigger

    def test_split_utf8_and_rotation(self):
        path = Path(self.temp.name) / "latest.log"
        path.write_bytes(b"")
        emitted = []
        m = GameLogMonitor(GameLogConfig(path=str(path)), emitted.append)
        m.poll()
        data = "[CHAT] 上钩! 您钓到了 [鱼]\n".encode()
        path.write_bytes(data[:-3]); m.poll(); self.assertEqual(len(emitted), 0)
        with path.open("ab") as f: f.write(data[-3:])
        m.poll(); self.assertEqual(len(emitted), 1)
        path.write_text("[CHAT] 钓点已枯竭\n", encoding="utf-8")
        m.poll(); m.poll(); self.assertEqual(emitted[-1].kind, "SPOT_DEPLETED")

    def test_fullwidth_results_and_player_chat(self):
        emitted = []
        m = GameLogMonitor(GameLogConfig(), emitted.append)
        m._classify("[CHAT] 上钩！ 您钓到了 [鲈鱼]", 1)
        m._classify("[CHAT] 不行！ 这个钓点已经枯竭，换一个地方吧。", 2)
        m._classify("[CHAT] <玩家> 上钩! 您钓到了 [鲈鱼]", 3)
        m._classify("[CHAT] 未钓到鱼", 4)
        self.assertEqual([e.kind for e in emitted], ["SUCCESS", "SPOT_DEPLETED"])

    def test_pass_card_chat_is_saved_as_event(self):
        emitted = []
        monitor = GameLogMonitor(GameLogConfig(), emitted.append)
        line = ("[1310月2026 03:03:31.631] [Render thread/INFO] "
                "[net.minecraft.client.gui.components.ChatComponent/]: "
                "[CHAT] 您取得了 通行证-沉船遗迹 ! 可通过船员前往对应区域进行垂钓。")
        monitor._classify(line, 42)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, "PASS_CARD")
        self.assertIn("通行证-沉船遗迹", emitted[0].text)
        self.assertEqual(emitted[0].clock, "03:03:31")

    def test_delayed_old_log_cannot_resolve_new_reel(self):
        path = Path(self.temp.name) / "latest.log"
        path.write_bytes(b"")
        events = []
        m = GameLogMonitor(GameLogConfig(path=str(path)), events.append)
        m.poll()
        with path.open("a", encoding="utf-8") as f: f.write("[CHAT] 您钓到了 [旧鱼]\n")
        source, offset = m.result_boundary()
        e = FishingEngine(AppConfig(), Window(), self.detector(), Input(), self.store)
        e._pending = BiteEvent(timestamp(), "上钩", .95, (0,0,200,100), 0,
                               reel_monotonic=time.monotonic(), log_source=source, log_start_offset=offset)
        e.state = State.WAITING_RESULT
        e._result_deadline = time.monotonic() + 6
        m.poll()
        e.on_log_event(events[-1])
        self.assertEqual(e.state, State.WAITING_RESULT)
        with path.open("a", encoding="utf-8") as f: f.write("[CHAT] 您钓到了 [新鱼]\n")
        m.poll(); e.on_log_event(events[-1])
        self.assertEqual(e.state, State.SUCCESS)
        self.assertEqual(e._pending.fish, "[新鱼]")

    def test_roi_scales_with_client_window(self):
        window = Window()
        window.get_rect = lambda: WindowRect(100, 200, 1100, 1000)
        capture = WindowCapture(window)
        class Screenshot:
            def grab(self, region):
                return np.zeros((region["height"], region["width"], 4), np.uint8)
        capture._local.sct = Screenshot()
        roi = RoiConfig(x=.2, y=.3, width=.4, height=.2)
        self.assertEqual(capture.capture(roi)[1], (300, 440, 400, 160))
        window.get_rect = lambda: WindowRect(-100, 0, 1900, 1600)
        self.assertEqual(capture.capture(roi)[1], (300, 480, 800, 320))

    def test_focus_blocks_clicks(self):
        window = Window(); window.focused = False
        input_ctrl = Input()
        e = FishingEngine(AppConfig(), window, self.detector(), input_ctrl, self.store)
        self.assertFalse(e._click())
        self.assertEqual(input_ctrl.clicks, 0)

    def test_alert_background_foreground_loop_ack_repeat_exit(self):
        backend = Backend()
        config = AlertConfig(sound_duration_ms=10, repeat_interval_ms=30)
        a = AlertManager(config, self.store, backend)
        try:
            e = LogEvent("1", "SPOT_DEPLETED", "钓点已枯竭", time.monotonic(), "12:00:00")
            self.assertTrue(a.trigger(e))
            self.assertTrue(until(lambda: len(backend.sounds) >= 2))
            self.assertIn(True, backend.flashes)
            self.assertFalse(a.trigger(e))
            backend.focused = True
            self.assertTrue(until(lambda: backend.flashes[-1] is False))
            before = len(backend.sounds)
            self.assertTrue(until(lambda: len(backend.sounds) > before))
            backend.focused = False
            self.assertTrue(until(lambda: backend.flashes[-1] is True))
            a.acknowledge()
            after = len(backend.sounds)
            threading.Event().wait(.25)
            self.assertEqual(len(backend.sounds), after)
            self.assertIs(backend.flashes[-1], False)
            self.assertEqual(a.state, AlertState.ACKNOWLEDGED)
            self.assertTrue(a.trigger(LogEvent("2", "NO_FISH_ACTIVITY", "没有鱼群", time.monotonic(), "12:01:00")))
            self.assertTrue(until(lambda: len(backend.sounds) > after))
            self.assertTrue(until(lambda: backend.flashes[-1] is True))
        finally: a.stop()
        self.assertTrue(backend.closed)
        self.assertFalse(a._thread.is_alive())
        self.assertIs(backend.flashes[-1], False)
        self.assertEqual(sum(r["type"]=="error" for r in self.store.history()), 2)

    def test_alarm_disabled_preserves_error(self):
        b = Backend()
        a = AlertManager(AlertConfig(alert_enabled=False), self.store, b)
        a.trigger(LogEvent("1", "SPOT_DEPLETED", "枯竭", time.monotonic(), ""))
        threading.Event().wait(.05)
        self.assertEqual(a.state, AlertState.WARNING)
        self.assertEqual(b.sounds, [])
        a.stop()

    def test_title_reels_without_log_and_log_never_reels(self):
        cfg = AppConfig(roi=RoiConfig(configured=True))
        cfg.title.min_wait_ms = 0
        cfg.title.screenshot_interval_ms = 10
        cfg.fishing.recast_delay_min_ms = cfg.fishing.recast_delay_max_ms = 500
        d = self.detector()
        input_ctrl = Input()
        e = FishingEngine(cfg, Window(), d, input_ctrl, self.store)
        e.on_log_event(LogEvent("historical", "SUCCESS", "旧鱼", time.monotonic(), "", "旧鱼"))
        self.assertEqual(input_ctrl.clicks, 0)
        self.assertTrue(e.start())
        d._armed = True
        self.assertTrue(until(lambda: e.state == State.WAITING_RESULT))
        self.assertEqual(input_ctrl.clicks, 1)
        threading.Event().wait(.08)
        self.assertEqual(input_ctrl.clicks, 1)
        e.on_log_event(LogEvent("stale", "SUCCESS", "旧鱼", e._pending.reel_monotonic-1, "", "旧鱼"))
        self.assertEqual(e.state, State.WAITING_RESULT)
        e.on_log_event(LogEvent("new", "SUCCESS", "新鱼", time.monotonic(), "", "[鲈鱼]"))
        self.assertEqual(e.state, State.SUCCESS)
        e.stop(); e.wait()
        self.assertEqual(self.store.stats()["success"], 1)
        bite = self.store._bites[e._pending.id]
        for name in ("timestamp", "title_text", "ocr_confidence", "title_roi", "bobber_confidence", "state", "reel_timestamp"):
            self.assertIn(name, bite)

    def test_timeout_unknown(self):
        cfg = AppConfig(roi=RoiConfig(configured=True))
        cfg.title.min_wait_ms = 0
        cfg.title.screenshot_interval_ms = 10
        cfg.fishing.result_timeout = 60
        d = self.detector()
        e = FishingEngine(cfg, Window(), d, Input(), self.store)
        e.start(); d._armed = True
        self.assertTrue(until(lambda: not e.is_running()))
        self.assertEqual(e.state, State.UNKNOWN)
        self.assertEqual(self.store.stats()["unknown"], 1)


if __name__ == "__main__": unittest.main()
