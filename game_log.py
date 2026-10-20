"""latest.log is a result/error source, never a bite source."""
from __future__ import annotations
import hashlib
import os
import re
import threading
import time
import uuid
import unicodedata
from collections import deque
from pathlib import Path
from dataclasses import dataclass
from config import GameLogConfig


@dataclass(frozen=True)
class LogEvent:
    id: str
    kind: str
    text: str
    at: float
    clock: str
    fish: str = ""
    raw: str = ""
    offset: int = 0
    source: str = ""


def file_identity(path, stat):
    return (path, stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime", 0))


def discover_logs():
    roots = [Path(os.environ.get("APPDATA", "")) / ".minecraft", Path.home() / ".minecraft"]
    paths = []
    for root in roots:
        for pattern in ("logs/latest.log", "versions/*/logs/latest.log", "instances/*/.minecraft/logs/latest.log"):
            paths.extend(root.glob(pattern))
    return sorted(set(paths), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


class GameLogMonitor:
    def __init__(self, config, on_event=None):
        self.config = config
        self.on_event = on_event
        self.path = ""
        self.status = "未连接日志"
        self._offset = 0
        self._identity = None
        self._session = uuid.uuid4().hex
        self._anchor = b""
        self._pending = b""
        self._events = deque(maxlen=250)
        self._seen = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="LatestLog", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:
                self.status = f"日志监听异常：{exc}"
            self._stop.wait(.15)

    def poll(self):
        with self._lock:
            self._poll_locked()

    def _poll_locked(self):
        if not self.config.enabled:
            self.status = "日志监控已关闭"
            return
        path = self.config.path.strip() or self.path
        if not path:
            candidates = discover_logs()
            path = str(candidates[0]) if candidates else ""
        if not path or not os.path.isfile(path):
            self.status = "未找到 latest.log"
            return
        try:
            with open(path, "rb") as f:
                stat = os.fstat(f.fileno())
                identity = file_identity(path, stat)
                f.seek(max(0, self._offset - len(self._anchor)))
                anchor = f.read(len(self._anchor))
                if identity != self._identity or stat.st_size < self._offset or (self._anchor and anchor != self._anchor):
                    first = self._identity is None
                    self.path, self._identity = path, identity
                    self._offset = stat.st_size if first else 0
                    self._session = uuid.uuid4().hex
                    self._pending = b""
                    f.seek(max(0, self._offset - 64))
                    self._anchor = f.read(64) if self._offset else b""
                    self.status = "已连接 · 忽略历史内容" if first else "日志轮转 · 监听新文件"
                    return
                start_offset = self._offset - len(self._pending)
                f.seek(self._offset)
                data = f.read(512 * 1024)
                self._offset = f.tell()
                f.seek(max(0, self._offset - 64))
                self._anchor = f.read(64)
            self.path, self.status = path, "监听中"
            combined = self._pending + data
            lines = combined.split(b"\n")
            self._pending = lines.pop()[-65536:]
            for raw in lines:
                try: line = raw.decode("utf-8").strip()
                except UnicodeDecodeError: line = raw.decode("gb18030", errors="replace").strip()
                self._classify(line, start_offset)
                start_offset += len(raw) + 1
        except OSError as exc:
            self.status = f"日志不可读：{exc}"

    def reconnect(self):
        # Called while idle; preserve the event history and reset the attachment only.
        with self._lock:
            self.path = ""
            self._identity = None
            self._anchor = b""

    def result_boundary(self):
        """Snapshot before OCR, so already-written results cannot resolve a later reel."""
        path = self.config.path.strip() or self.path
        if not self.config.enabled or not path: return None
        try:
            stat = os.stat(path)
            return repr(file_identity(path, stat)), stat.st_size
        except OSError:
            return None

    def _classify(self, line, offset=0):
        clean = unicodedata.normalize("NFKC", re.sub(r"\u00a7[0-9a-fk-or]", "", line, flags=re.I))
        if "[chat]" not in clean.lower():
            if re.search(r"lost connection:|disconnected from|disconnecting from", clean, re.I):
                self._emit("DISCONNECTED", clean, line, offset)
            return
        text = re.split(r"\[CHAT\]\s*", clean, flags=re.I)[-1].strip()
        lowered = text.lower()
        if re.search(r"^<[^>]+>|^[^:：]{1,40}[:：]\s|未(?:钓到|上钩)|not caught", text, re.I):
            kind = "CHAT"
        elif re.search(r"^(?:不行!?\s*.*枯竭|钓点.*枯竭|spot.*deplet)", lowered, re.I):
            kind = "SPOT_DEPLETED"
        elif re.search(r"^(?:不行!?\s*.*鱼群|没有鱼群|no fish.*activ)", lowered, re.I):
            kind = "NO_FISH_ACTIVITY"
        elif re.search(r"鱼竿.*(?:损坏|断)|rod broke|broken.*rod", lowered, re.I):
            kind = "ROD_BROKEN"
        elif re.search(r"(?:您|你)取得了\s*通行证\s*[-—–]\s*[^!！。]+", text):
            kind = "PASS_CARD"
        elif re.search(r"^(?:上钩!?\s*)?(?:(?:您|你)钓到了|成功钓到|you caught)", text, re.I):
            kind = "SUCCESS"
        else:
            kind = "CHAT"
        if kind == "CHAT":
            for code, phrase in self.config.extra_errors.items():
                if phrase and phrase in text:
                    kind = "ERROR_" + str(code)
                    break
        if kind == "CHAT": return
        self._emit(kind, text, line, offset)

    def _emit(self, kind, text, raw, offset):
        # Position + file generation distinguishes two genuine identical lines.
        event_id = hashlib.sha256(f"{self._session}:{offset}:{raw}".encode("utf-8")).hexdigest()
        fish = ""
        if kind == "SUCCESS":
            match = re.search(r"(?:您|你)钓到了\s*(.+)|you caught\s*(.+)", text, re.I)
            fish = (next((g for g in match.groups() if g), "").strip() if match else text)
        with self._lock:
            if event_id in self._seen: return
            self._seen.add(event_id)
            stamp = re.match(r"\[\d{1,2}\d{1,2}月\d{4}\s+(\d{2}:\d{2}:\d{2})", raw)
            if not stamp:
                stamp = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", raw)
            event = LogEvent(event_id, kind, text, time.monotonic(),
                             stamp[1] if stamp else time.strftime("%H:%M:%S"), fish, raw, offset,
                             repr(self._identity))
            self._events.append(event)
        if self.on_event: self.on_event(event)

    def events(self):
        with self._lock: return list(self._events)
