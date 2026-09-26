"""
minecraft_window.py
-------------------
Minecraft 窗口识别模块。

负责：
- 根据窗口标题关键词查找 Minecraft 窗口句柄
- 获取窗口矩形 (left, top, right, bottom)
- 校验窗口是否仍然有效 / 存在
- 区分 Minecraft 游戏窗口与 Launcher 等无关窗口

仅支持 Windows。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List

import win32gui
import win32con

from logger import get_logger


@dataclass
class WindowRect:
    """窗口矩形坐标 (屏幕坐标)"""
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return self.width * self.height


# 容易被误识别为 Minecraft 的窗口标题关键词 (黑名单)
_TITLE_BLACKLIST: List[str] = [
    "launcher",
    "minecraft launcher",
    "updater",
    "installer",
    "crash",
    "log",
    "error",
    "启动器",       # 中文：启动器
    "更新",         # 中文：更新
]


class MinecraftWindow:
    """Minecraft 窗口管理器"""

    def __init__(self, title_keyword: str = "Minecraft") -> None:
        self._keyword = title_keyword
        self._hwnd: Optional[int] = None
        self._log = get_logger()

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    def find(self) -> bool:
        """
        查找 Minecraft 窗口。

        遍历所有顶层窗口，匹配标题包含关键词的窗口，
        排除黑名单窗口，并选择面积最大的候选 (通常是游戏主窗口)。

        Returns:
            是否找到有效窗口
        """
        candidates: List[tuple[int, str, WindowRect]] = []

        # Minecraft 使用的窗口类名 (GLFW30=新版, LWJGL=旧版)
        mc_class_names = ("GLFW30", "LWJGL")

        def _enum_callback(hwnd: int, _) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            title_lower = title.lower()
            cls_name = win32gui.GetClassName(hwnd)

            # 匹配条件: 标题包含关键词，或窗口类名是 Minecraft 专用类名
            match_by_title = bool(title) and (self._keyword.lower() in title_lower)
            match_by_class = cls_name in mc_class_names
            if not (match_by_title or match_by_class):
                return
            # 排除启动器、日志等无关窗口 (标题匹配时才检查黑名单)
            if match_by_title:
                for bad in _TITLE_BLACKLIST:
                    if bad in title_lower:
                        return
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            # 过滤过小的窗口 (可能是通知图标之类)
            if w < 400 or h < 300:
                return
            candidates.append((hwnd, title or cls_name, WindowRect(*rect)))

        try:
            win32gui.EnumWindows(_enum_callback, None)
        except Exception as e:
            self._log.error(f"枚举窗口失败: {e}")
            return False

        if not candidates:
            self._log.warn(
                f"未找到 Minecraft 窗口 (标题包含 '{self._keyword}' "
                f"或类名为 GLFW30/LWJGL 的窗口)"
            )
            self._hwnd = None
            return False

        # 按面积降序，选最大的作为 Minecraft 主窗口
        candidates.sort(key=lambda c: c[2].area, reverse=True)
        best_hwnd, best_title, best_rect = candidates[0]
        self._hwnd = best_hwnd
        self._log.info(
            f"Minecraft 窗口已找到: "
            f"标题='{best_title}', "
            f"位置=({best_rect.left},{best_rect.top}), "
            f"尺寸={best_rect.width}x{best_rect.height}"
        )
        return True

    def get_rect(self) -> Optional[WindowRect]:
        """获取当前 Minecraft 窗口矩形。若窗口失效返回 None。"""
        if self._hwnd is None:
            return None
        if not win32gui.IsWindow(self._hwnd):
            self._log.warn("Minecraft 窗口已失效 (句柄无效)")
            self._hwnd = None
            return None
        try:
            rect = win32gui.GetWindowRect(self._hwnd)
        except Exception as e:
            self._log.error(f"获取窗口矩形失败: {e}")
            self._hwnd = None
            return None
        return WindowRect(*rect)

    def is_valid(self) -> bool:
        """检查 Minecraft 窗口是否仍然存在且可见"""
        if self._hwnd is None:
            return False
        if not win32gui.IsWindow(self._hwnd):
            self._hwnd = None
            return False
        if not win32gui.IsWindowVisible(self._hwnd):
            return False
        # 窗口最小化也视为不可用
        if win32gui.IsIconic(self._hwnd):
            return False
        return True

    def is_foreground(self) -> bool:
        """
        检查 Minecraft 窗口是否为当前前台活动窗口。
        用于失焦自动暂停：用户切到其他窗口时自动暂停，切回来时自动恢复。
        """
        if self._hwnd is None:
            return False
        try:
            fg = win32gui.GetForegroundWindow()
            return fg == self._hwnd
        except Exception:
            return False

    def ensure_foreground(self) -> bool:
        """尝试将 Minecraft 窗口置于前台 (非强制，仅用于截图坐标对齐)"""
        if self._hwnd is None or not self.is_valid():
            return False
        try:
            if win32gui.IsIconic(self._hwnd):
                win32gui.ShowWindow(self._hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self._hwnd)
            return True
        except Exception:
            return False

    def invalidate(self) -> None:
        """手动标记窗口失效"""
        self._hwnd = None
