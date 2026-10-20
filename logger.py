"""
logger.py
---------
轻量级日志模块，带时间戳输出到控制台。

不引入 logging 模块的复杂配置，保持简单清晰。
日志格式: [HH:MM:SS] message
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Optional


def _configure_utf8_streams() -> None:
    """Use UTF-8 for console logs when Windows exposes a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Logging must never prevent the application from starting.
            pass


class Logger:
    """带时间戳的控制台日志输出"""

    def __init__(self, name: str = "FishBot") -> None:
        _configure_utf8_streams()
        self._name = name
        self._enabled = True

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _print(self, level: str, msg: str) -> None:
        if not self._enabled:
            return
        line = f"[{self._ts()}] [{level}] {msg}"
        print(line, flush=True)

    def info(self, msg: str) -> None:
        self._print("INFO", msg)

    def warn(self, msg: str) -> None:
        self._print("WARN", msg)

    def error(self, msg: str) -> None:
        self._print("ERROR", msg)
        print(msg, file=sys.stderr, flush=True)

    def success(self, msg: str) -> None:
        self._print("OK  ", msg)

    def test(self, msg: str) -> None:
        """测试模式专用输出"""
        self._print("TEST", msg)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled


# 全局单例
_logger: Optional[Logger] = None


def get_logger() -> Logger:
    global _logger
    if _logger is None:
        _logger = Logger()
    return _logger
