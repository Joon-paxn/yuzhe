"""ROI-scoped OCR. Visual title is the only bite trigger."""
from __future__ import annotations
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass, field, asdict
import cv2
import numpy as np
from capture import WindowCapture
from events import BiteEvent, timestamp

try:
    from rapidocr import RapidOCR
except ImportError:  # startup stays usable for ROI setup without OCR package
    RapidOCR = None


@dataclass
class TitleResult:
    text: str = ""
    confidence: float = 0.0
    similarity: float = 0.0
    matched: bool = False
    roi: tuple = (0, 0, 0, 0)
    reason: str = ""
    normalized: str = ""
    target: str = ""
    best_variant: str = ""
    support_count: int = 0
    detection_state: str = "WAITING_BITE"
    variants: list = field(default_factory=list)


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\s\r\n]+", "", value)
    # Punctuation and decoration are OCR noise; semantic characters remain intact.
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value, flags=re.UNICODE).lower()


def target_texts(config):
    return tuple(dict.fromkeys(normalize_text(t) for t in ("咬钩", "上钩", config.target_text)
                               if normalize_text(t)))


def match_text(text, confidence, config):
    actual = normalize_text(text)
    target = max(target_texts(config), key=lambda t: SequenceMatcher(None, actual, t).ratio())
    similarity = SequenceMatcher(None, actual, target).ratio() if actual else 0.0
    semantic = len(re.findall(r"[\u4e00-\u9fff]", actual)) <= len(re.findall(r"[\u4e00-\u9fff]", target))
    semantic = semantic and not re.search(r"[未没不无]", actual)
    matched = bool(actual) and semantic and similarity >= config.ocr_similarity_threshold
    matched = matched and confidence >= config.ocr_confidence_threshold
    return actual, target, similarity, bool(matched)


def preprocess_title(frame, scale=3):
    """Separate bright neutral glyphs from color backgrounds and dark outlines."""
    enlarged = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    contrast = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    sharp = cv2.addWeighted(contrast, 1.7, cv2.GaussianBlur(contrast, (0, 0), 1.2), -.7, 0)
    binary = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    low, high = enlarged.min(axis=2), enlarged.max(axis=2)
    # White cores exclude black drop shadows and colored scene pixels.
    mask = ((low >= 165) & ((high.astype(np.int16) - low) <= 55)).astype(np.uint8) * 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    clean = np.zeros_like(mask)
    minimum = max(6, scale * scale)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= minimum:
            clean[labels == i] = 255
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # Render the separated white fill as dark glyphs on a flat white background.
    white_text = 255 - clean
    versions = {"enlarged": enlarged, "grayscale": gray, "contrast": contrast,
                "sharpened": sharp, "binary": binary, "inverted": 255 - binary,
                "white_text": white_text}
    return versions


def fuse_results(rows, roi):
    positives = [row for row in rows if row["matched"]]
    pool = positives or rows
    # Votes combine equivalent aliases; confidence still belongs to actual OCR text.
    counts = {r["target"]: sum(x["target"] == r["target"] for x in positives) for r in positives}
    best = max(pool, key=lambda r: (counts.get(r["target"], 0), r["similarity"], r["confidence"]))
    return TitleResult(best["text"], best["confidence"], best["similarity"], bool(positives), roi,
                       "目标 Title" if positives else "未匹配", best["normalized"], best["target"],
                       best["name"], len(positives), "BITE_CANDIDATE" if positives else "WAITING_BITE", rows)


class TitleDetector:
    def __init__(self, window, config, roi, log=None):
        self.config = config
        self.roi = roi
        self.capture = WindowCapture(window)
        self._engine = None
        self._ocr_lock = threading.Lock()
        self._lock = threading.RLock()
        self._last = TitleResult(reason="等待 OCR")
        self._positive_times = []
        self._clear_count = 0
        self._armed = False
        self._last_confirmed = 0.0
        self._last_frame = None
        self._debug_images = {}
        self.status = "OCR 未初始化"
        self.ready = False
        self.latency_ms = 0.0
        self._log = log

    def _ocr(self, frame):
        with self._ocr_lock:
            return self._ocr_locked(frame)

    def _ocr_locked(self, frame):
        if RapidOCR is None:
            self.status = "未安装 RapidOCR，请运行 Run.bat"
            return "", 0.0
        try:
            if self._engine is None:
                self.status = "正在加载中文 OCR 模型"
                self._engine = RapidOCR(params={"EngineConfig.onnxruntime.intra_op_num_threads": 2,
                                                 "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                                                 "Det.limit_side_len": 160,
                                                 "Global.max_side_len": 640,
                                                 "Global.log_level": "error"})
                self.status = "中文 OCR 已就绪"
            # The selected ROI already contains one Title line. Running scene text
            # detection again for every variant adds latency and can discard glyphs.
            output = self._engine(frame, use_det=False, use_cls=False, return_word_box=False)
            self.ready = True
            self.status = "中文 OCR 已就绪"
            texts = getattr(output, "txts", None)
            scores = getattr(output, "scores", None)
            texts = list(texts) if texts is not None else []
            scores = list(scores) if scores is not None else []
            # Confidence must belong to the matching text, not a different high-score line.
            candidates = list(zip(texts, scores))
            if not candidates: return "", 0.0
            return max(candidates, key=lambda pair: (match_text(pair[0], float(pair[1]), self.config)[3],
                                                     match_text(pair[0], float(pair[1]), self.config)[2], pair[1]))
        except Exception as exc:
            self.ready = False
            if self._log:
                self._log.warn(f"Title OCR 暂时不可用：{exc}")
            self.status = f"OCR 错误：{exc}"
            return "", 0.0

    def detect(self):
        frame, screen_roi = self.capture.capture(self.roi)
        if frame is None:
            result = TitleResult(roi=screen_roi, reason="无法截图")
            with self._lock: self._last = result
            return result
        return self.detect_frame(frame, screen_roi)

    def detect_frame(self, frame, screen_roi=(0, 0, 0, 0)):
        began = time.monotonic()
        versions = preprocess_title(frame, self.config.preprocess_scale)
        rows = []
        for name, prepared in versions.items():
            text, confidence = self._ocr(prepared)
            confidence = float(confidence)
            actual, target, similarity, matched = match_text(text, confidence, self.config)
            rows.append(dict(name=name, text=text, confidence=confidence, normalized=actual,
                             target=target, similarity=similarity, matched=matched))
        result = fuse_results(rows, screen_roi)
        with self._lock:
            self._last_frame = frame.copy()
            self._debug_images = {"raw": frame.copy(), "enlarged": versions["enlarged"],
                                  "best": versions[result.best_variant]}
            self._last = result
        self.latency_ms = (time.monotonic() - began) * 1000
        return result

    def confirm(self):
        return self.confirm_result(self.detect())

    def confirm_result(self, result):
        now = time.monotonic()
        with self._lock:
            self._positive_times = [t for t in self._positive_times
                                    if now - t <= self.config.title_confirm_window_ms / 1000]
            if result.matched:
                if self._positive_times and now - self._positive_times[0] > self.config.title_candidate_timeout / 1000:
                    self._positive_times.clear()
                self._positive_times.append(now)
                self._clear_count = 0
            else:
                self._positive_times.clear()
                if result.roi != (0, 0, 0, 0):
                    self._clear_count += 1
                    if self._clear_count >= self.config.clear_frames:
                        self._armed = True
            if now - self._last_confirmed < self.config.bite_cooldown / 1000:
                result.reason = "Title 冷却中"
                result.detection_state = "WAITING_BITE"
                return result, None
            if not self._armed:
                result.reason = "等待 Title 消失后布防"
                result.detection_state = "WAITING_BITE"
                return result, None
            if (len(self._positive_times) >= self.config.title_confirm_frames
                    and result.matched):
                self._armed = False
                self._last_confirmed = now
                self._positive_times.clear()
                event = BiteEvent(timestamp(), result.text, result.confidence,
                                  result.roi, 0.0, reel_monotonic=now)
                result.reason = "BITE_CONFIRMED"
                result.detection_state = "BITE_CONFIRMED"
                return result, event
            result.reason = "Title 候选确认中" if result.matched else result.reason
            return result, None

    def debug_snapshot(self):
        with self._lock:
            return asdict(self._last), {key: value.copy() for key, value in self._debug_images.items()}

    def preview(self):
        with self._lock:
            return self._last_frame.copy() if self._last_frame is not None else None

    @property
    def last(self):
        return self._last

    def reset(self):
        with self._lock:
            self._positive_times.clear()
            self._clear_count = 0
            self._armed = False
            self._last_confirmed = 0.0

    def reset_candidate(self):
        with self._lock: self._positive_times.clear()

    def reset_cycle(self):
        with self._lock:
            self._positive_times.clear()
            self._clear_count = 0
            self._armed = False

    def close(self):
        self.capture.close()

    def release_resources(self):
        """Release OS screenshot handles while keeping OCR state for a later resume."""
        self.capture.close()

    def warmup(self):
        frame = np.zeros((32, 80, 3), dtype=np.uint8)
        self._ocr(preprocess_title(frame)["contrast"])
