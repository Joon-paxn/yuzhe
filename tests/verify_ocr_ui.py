"""Manual smoke artifact: actual Chinese OCR + two native UI sizes, no game input."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ctypes
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from config import BASE_DIR
from main import AutoFishingApp
from gui import FishingGUI
from game_log import LogEvent
import time
import tempfile
import threading
import json
from dataclasses import asdict
from title_detector import TitleResult


def save_window(root, path):
    import win32gui
    import win32ui
    user32 = ctypes.WinDLL("user32")
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.PrintWindow.restype = ctypes.c_int
    hwnd = user32.GetAncestor(root.winfo_id(), 2)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right-left, bottom-top
    handle = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(handle)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    old = target.SelectObject(bitmap)
    try:
        assert user32.PrintWindow(hwnd, target.GetSafeHdc(), 2), "Window render capture failed"
        data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", 0, 1)
        assert np.asarray(image).std() > 5, "Window capture is blank"
        image.save(path)
    finally:
        target.SelectObject(old)
        win32gui.DeleteObject(bitmap.GetHandle())
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, handle)


def check_confirmation(app, canvas, label, out):
    frame = np.asarray(canvas)[:, :, ::-1].copy()
    roi = (0, 0, frame.shape[1], frame.shape[0])
    app.detector.reset()
    for _ in range(app.config.title.clear_frames):
        app.detector.confirm_result(TitleResult(roi=roi))
    states = []
    for index in range(app.config.title.title_confirm_frames):
        result, event = app.detector.confirm_result(app.detector.detect_frame(frame, roi))
        states.append(result.detection_state)
        assert result.matched, asdict(result)
        assert result.normalized == "咬钩", asdict(result)
        assert len(result.variants) == 7
        if index == 0:
            assert event is None and result.detection_state == "BITE_CANDIDATE"
        if index == app.config.title.title_confirm_frames - 1:
            assert event is not None and result.detection_state == "BITE_CONFIRMED"
    evidence, images = app.detector.debug_snapshot()
    evidence["confirmation_states"] = states
    evidence["latency_ms"] = app.detector.latency_ms
    (out / f"{label}_result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, image in images.items():
        rgb = image if image.ndim == 2 else image[:, :, ::-1]
        Image.fromarray(rgb).save(out / f"{label}_{name}.png")
    result, event = app.detector.confirm_result(app.detector.detect_frame(frame, roi))
    assert event is None, "Persistent Title must not trigger twice"
    print(label, states, round(app.detector.latency_ms), "ms", "dedup PASS")


def real_fixture():
    fixtures = BASE_DIR / "tests" / "fixtures"
    sample = fixtures / "bite_title_real.png"
    if not sample.exists():
        source = Path("C:/Users/Jiulin/Pictures/Snow Shot/SnowShot_2026-10-08_15-46-15.png")
        fixtures.mkdir(exist_ok=True)
        # This is an application screenshot: crop its embedded Minecraft preview.
        # Game-window normalized ROI coordinates cannot be applied to UI screenshots.
        box = (296, 538, 418, 600)
        with Image.open(source) as screenshot:
            screenshot.crop(box).save(sample)
        (fixtures / "bite_title_real.json").write_text(json.dumps({
            "source": str(source), "source_size": [1063, 819], "crop_box": box,
            "expected_normalized": "咬钩", "source_type": "application_preview",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return Image.open(sample).convert("RGB")

def run():
    temp = tempfile.TemporaryDirectory()
    app = AutoFishingApp(Path(temp.name) / "events.jsonl")
    out = BASE_DIR / "verification"
    out.mkdir(exist_ok=True)
    canvas = Image.new("RGB", (460, 150), (30, 60, 45))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 70)
    draw.text((110, 22), "咬钩！", font=font, fill="white", stroke_width=4, stroke_fill="black")
    canvas.save(out / "title_sample.png")
    app.detector.capture.capture = lambda roi: (np.asarray(canvas)[:, :, ::-1].copy(), (0,0,460,150))
    began = time.monotonic()
    result = app.detector.detect()
    print("OCR:", repr(result.text), "confidence:", result.confidence, "match:", result.matched,
          "seconds:", round(time.monotonic()-began, 3))
    if not result.matched: raise AssertionError("Chinese OCR did not recognize target")
    for _ in range(3):
        result = app.detector.detect()
        assert result.matched
        print("Warm OCR frame:", round(app.detector.latency_ms), "ms")
    check_confirmation(app, canvas, "synthetic", out)
    check_confirmation(app, real_fixture(), "real", out)
    app.gui = FishingGUI(app)
    root = app.gui.build()
    # Exercise the same background import path exposed by the debug UI.
    app.test_image(BASE_DIR / "tests" / "fixtures" / "bite_title_real.png")
    deadline = time.monotonic() + 10
    while app._test_thread.is_alive() and time.monotonic() < deadline:
        root.update()
        threading.Event().wait(.02)
    assert not app._test_thread.is_alive(), "Screenshot import did not finish"
    assert app.detector.last.normalized == "咬钩"
    assert app.detector.last.detection_state == "BITE_CONFIRMED"
    print("Background screenshot import: PASS")
    app.config.alert.sound_enabled = False
    app.alerts.trigger(LogEvent("ui-test", "SPOT_DEPLETED", "不行! 这个钓点已经枯竭，换一个地方吧。",
                                time.monotonic(), "15:00:00", raw="[CHAT] 不行! 这个钓点已经枯竭，换一个地方吧。"))
    root.update()
    app.gui.tick()
    app.gui.tabs.select(1)
    for width, height in ((1060,790), (900,680)):
        root.geometry(f"{width}x{height}+20+20")
        root.update()
        app.gui.update_debug()
        root.update()
        save_window(root, out / f"ui_{width}.png")
        app.gui.debug_canvas.yview_moveto(1)
        root.update()
        assert app.gui.debug_canvas.yview()[1] > .99
        if width == 900:
            assert app.gui.debug_canvas.yview()[0] > 0, "Debug page must scroll at minimum window size"
        save_window(root, out / f"ui_{width}_scrolled.png")
        app.gui.debug_canvas.yview_moveto(0)
    app.gui.tabs.select(2)
    root.update()
    save_window(root, out / "ui_settings.png")
    root.destroy()
    app.cleanup()
    temp.cleanup()

if __name__ == "__main__": run()
