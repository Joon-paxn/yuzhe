"""
log_watcher.py
--------------
游戏日志监听模块 (借鉴 Tau-main)。

增量读取 Minecraft `latest.log` 新增行，匹配关键字触发动作 (如钓点枯竭)。
比 OCR 更可靠、更省 CPU，作为枯竭检测的首选来源，OCR 作为回退。

设计：
- 后台线程增量 tail：记录字节游标，只读新增内容；文件被重写时游标归零。
- 编码自动检测：优先 UTF-8，失败用 GB18030 (中文 Windows Minecraft 日志)。
- 归一化匹配：去 Minecraft 颜色代码 (§x)、全角转半角、转小写，子串匹配。
- 规则化：每条规则 {text, action}，支持 '|' 多片段任一命中。
- 线程安全：最新匹配状态存于 self._latest_match，主线程读取。

默认规则：
- 文本含 "枯竭" → action="depleted" (钓点枯竭，触发换点)
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from logger import get_logger


# 默认枯竭检测规则 (匹配「鱼群：枯竭」「钓点枯竭」等所有变体)
_DEFAULT_RULES: List[dict] = [
    {"text": "枯竭", "action": "depleted"},
]


@dataclass
class LogMatch:
    """一次日志匹配结果"""
    action: str = ""
    line: str = ""
    timestamp: float = 0.0


class LogWatcher:
    """游戏日志增量监听器"""

    def __init__(
        self,
        log_path: str,
        rules: Optional[List[dict]] = None,
        interval_s: float = 0.3,
        on_match: Optional[Callable[[LogMatch], None]] = None,
    ) -> None:
        self._log_path = log_path
        self._rules = rules if rules is not None else list(_DEFAULT_RULES)
        self._interval = max(0.1, interval_s)
        self._on_match = on_match
        self._log = get_logger()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False

        self._lock = threading.Lock()
        self._latest_match: Optional[LogMatch] = None
        self._last_read_ts: float = 0.0

    # ---------------- 生命周期 ----------------

    def start(self) -> bool:
        """启动日志监听线程。日志路径不存在时返回 False。"""
        if not self._log_path or not os.path.exists(self._log_path):
            self._log.warn(f"日志文件不存在，日志监听未启动: {self._log_path}")
            return False
        if self._running:
            return True
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="LogWatcher", daemon=True)
        self._thread.start()
        self._log.info(f"日志监听已启动: {self._log_path} (规则 {len(self._rules)} 条)")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._log.info("日志监听已停止")

    def close(self) -> None:
        self.stop()

    # ---------------- 主线程循环 ----------------

    def _loop(self) -> None:
        # 启动时跳到文件末尾：只监听启动后新增的行，
        # 避免历史旧消息 (如昨天的"枯竭") 误触发动作
        try:
            offset = os.path.getsize(self._log_path)
        except OSError:
            offset = 0
        while not self._stop_event.is_set():
            try:
                size = os.path.getsize(self._log_path)
                if size < offset:
                    offset = 0  # 日志被重写/滚动
                if size > offset:
                    with open(self._log_path, "rb") as f:
                        f.seek(offset)
                        raw = f.read()
                        offset = f.tell()
                    text = self._decode(raw)
                    self._check_lines(text)
            except FileNotFoundError:
                offset = 0
            except Exception:
                pass
            if self._stop_event.wait(self._interval):
                break

    # ---------------- 编码与匹配 ----------------

    @staticmethod
    def _decode(raw: bytes) -> str:
        """自动检测编码：优先 UTF-8，失败用 GB18030 (中文 Windows Minecraft 日志)。"""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("gb18030", errors="ignore")
            except Exception:
                return raw.decode("utf-8", errors="ignore")

    @staticmethod
    def _normalize(s: str) -> str:
        """归一化：去 Minecraft 颜色代码 (§x)、全角转半角、转小写。"""
        try:
            s = re.sub(r"\u00a7[0-9a-fk-or]", "", s, flags=re.IGNORECASE)
        except Exception:
            pass
        out = []
        for ch in s:
            o = ord(ch)
            if o == 0x3000:
                out.append(" ")
            elif 0xFF01 <= o <= 0xFF5E:
                out.append(chr(o - 0xFEE0))  # 全角 → 半角
            else:
                out.append(ch)
        return "".join(out).lower()

    def _check_lines(self, data: str) -> None:
        """对新增日志逐行匹配规则 (归一化后子串匹配，支持 '|' 多片段任一命中)。"""
        if not data:
            return
        for line in data.splitlines():
            line_norm = self._normalize(line)
            for rule in self._rules:
                text = (rule.get("text", "") or "").strip()
                if not text:
                    continue
                matched = False
                for frag in text.split("|"):
                    frag_norm = self._normalize(frag.strip())
                    if frag_norm and frag_norm in line_norm:
                        matched = True
                        break
                if matched:
                    action = rule.get("action", "")
                    self._log.info(f"日志命中 [{action}]: {line.strip()[:80]}")
                    match = LogMatch(action=action, line=line.strip(), timestamp=time.monotonic())
                    with self._lock:
                        self._latest_match = match
                    if self._on_match is not None:
                        try:
                            self._on_match(match)
                        except Exception as e:
                            self._log.error(f"日志匹配回调异常: {e}")
                    break  # 一行只触发一条规则

    # ---------------- 查询 (线程安全) ----------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def log_path(self) -> str:
        return self._log_path

    def get_latest_match(self) -> Optional[LogMatch]:
        """获取最近一次匹配结果 (线程安全)"""
        with self._lock:
            return self._latest_match

    def pop_latest_match(self) -> Optional[LogMatch]:
        """取出并清除最近一次匹配结果 (避免重复消费)"""
        with self._lock:
            m = self._latest_match
            self._latest_match = None
            return m

    @property
    def last_read_ts(self) -> float:
        with self._lock:
            return self._last_read_ts
