"""Persistent event history and a thread-safe read model for the UI."""
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class BiteEvent:
    timestamp: str
    title_text: str
    ocr_confidence: float
    title_roi: tuple
    bobber_confidence: float
    state: str = "BITE_CONFIRMED"
    reel_timestamp: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    reel_monotonic: float = 0.0
    result: str = "PENDING"
    result_event_id: str = ""
    fish: str = ""
    reason: str = ""
    latency_ms: float = 0.0
    log_source: str = ""
    log_start_offset: int = 0


class EventStore:
    def __init__(self, path):
        self.path = Path(path)
        self.snapshot_path = self.path.with_suffix(".snapshot.json")
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._history = deque(maxlen=500)
        self._bites = {}
        self._pending = deque()
        self._wake = threading.Event()
        self._flushed = threading.Event()
        self._flushed.set()
        self._closed = False
        self.write_error = ""
        # Replay every bite revision for lifetime totals; bound only UI history.
        if self.path.exists():
            try:
                with self.path.open("rb") as f:
                    for raw in f:
                        try:
                            record = json.loads(raw)
                            if not isinstance(record, dict):
                                continue
                            self._history.append(record)
                            if record.get("type") == "bite" and record.get("id"):
                                self._bites[record["id"]] = record
                        except (ValueError, UnicodeDecodeError):
                            pass
            except OSError as exc:
                self.write_error = str(exc)
        self._worker = threading.Thread(target=self._write, name="EventHistory", daemon=True)
        self._worker.start()

    def add(self, kind, payload):
        record = dict(payload, type=kind)
        with self._lock:
            if self._closed:
                raise RuntimeError("Event history is already closed")
            self._history.append(record)
            if kind == "bite":
                self._bites[record["id"]] = record
            self._pending.append(record)
            self._flushed.clear()
        self._wake.set()

    def bite(self, event):
        self.add("bite", asdict(event))

    def history(self):
        with self._lock:
            return list(self._history)

    def stats(self):
        with self._lock:
            values = list(self._bites.values())
        success = [b for b in values if b.get("result") == "SUCCESS"]
        return dict(bites=len(values), success=len(success),
                    failure=sum(b.get("result") == "FAILURE" for b in values),
                    unknown=sum(b.get("result") == "UNKNOWN" for b in values),
                    last_fish=success[-1].get("fish", "") if success else "暂无鱼获",
                    rate=len(success) / max(1, sum(b.get("result") != "PENDING" for b in values)))

    def _write(self):
        while True:
            self._wake.wait(.25)
            self._wake.clear()
            with self._lock:
                pending = list(self._pending)
                closed = self._closed
            if pending:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self._io_lock:
                        with self.path.open("a", encoding="utf-8") as f:
                            for record in pending:
                                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                            f.flush()
                            os.fsync(f.fileno())
                    with self._lock:
                        for _ in pending:
                            self._pending.popleft()
                        if not self._pending:
                            self._flushed.set()
                    self.write_error = ""
                except OSError as exc:
                    self.write_error = str(exc)
            if closed:
                return

    def flush(self, timeout=5.0):
        """Synchronously persist queued records before the UI exits."""
        self._wake.set()
        if not self._flushed.wait(max(0.0, float(timeout))):
            raise OSError("历史保存超时")
        if self.write_error:
            raise OSError(f"历史保存失败：{self.write_error}")

    def close(self):
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
            self.flush()
            # Producers have stopped: drain all final results before the snapshot.
            self._worker.join()
            if self.write_error:
                raise OSError(f"历史保存失败：{self.write_error}")
            self._save_snapshot()

    def _save_snapshot(self):
        temporary = self.snapshot_path.with_suffix(".json.tmp")
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = dict(version=1, saved_at=timestamp(), stats=self.stats(),
                            history=self.history())
            with temporary.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.snapshot_path)
        except OSError as exc:
            self.write_error = str(exc)
            raise

    def reset(self):
        """Clear persisted history and counters while keeping the store usable."""
        with self._io_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Event history is already closed")
                self._pending.clear()
                self._history.clear()
                self._bites.clear()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("w", encoding="utf-8") as f:
                    f.flush()
                    os.fsync(f.fileno())
                self.write_error = ""
                self._save_snapshot()
