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

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional, List

import win32gui
import win32con
import win32process

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

# ---- 进程枚举 (用于全屏游戏窗口检测) ----
_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _list_processes() -> List[tuple[int, str]]:
    """返回所有进程的 (pid, exe名称) 列表。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    procs: List[tuple[int, str]] = []
    try:
        if kernel32.Process32First(snap, ctypes.byref(pe)):
            procs.append((pe.th32ProcessID, pe.szExeFile.decode("gbk", errors="ignore")))
            while kernel32.Process32Next(snap, ctypes.byref(pe)):
                procs.append((pe.th32ProcessID, pe.szExeFile.decode("gbk", errors="ignore")))
    finally:
        kernel32.CloseHandle(snap)
    return procs


def _find_windows_by_pid(pid: int) -> List[tuple[int, str, str, WindowRect]]:
    """返回属于指定进程的所有顶层窗口 (hwnd, title, class, rect)。"""
    found: List[tuple[int, str, str, WindowRect]] = []

    def _cb(hwnd: int, _) -> None:
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid != pid:
            return
        try:
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            if w < 200 or h < 100:
                return
            found.append((hwnd, title or cls, cls, WindowRect(*rect)))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return found


class MinecraftWindow:
    """Minecraft 窗口管理器"""

    def __init__(
        self,
        title_keyword: str = "Minecraft",
        process_names: Optional[List[str]] = None,
    ) -> None:
        self._keyword = title_keyword
        # 进程名兜底：全屏游戏窗口可能无法被 EnumWindows 标题匹配到，
        # 此时通过进程名找到游戏进程，再获取其窗口句柄。
        self._process_names = [
            n.lower() for n in (
                process_names
                if process_names
                else ["WinIsland.exe", "javaw.exe", "java.exe", "Minecraft.exe"]
            )
        ]
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

        # Minecraft 使用的窗口类名 (GLFW30=新版, LWJGL=旧版, 兼容变体)
        mc_class_names = ("GLFW30", "GLFW", "GLFW30Window", "LWJGL", "LWJGL2")

        def _enum_callback(hwnd: int, _) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            title_lower = title.lower()
            cls_name = win32gui.GetClassName(hwnd)

            # 匹配条件:
            #   1. 标题包含配置关键词 (如 "我的世界")
            #   2. 标题包含 "Minecraft" (兜底，国际版/服务器标题不含中文时)
            #   3. 窗口类名是 Minecraft 专用类名
            match_by_title = bool(title) and (
                self._keyword.lower() in title_lower
                or "minecraft" in title_lower
            )
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
            # ---- 兜底：按进程名查找 (全屏游戏窗口可能无法被标题匹配) ----
            self._log.warn(
                f"标题/类名匹配未命中，尝试按进程名查找: {self._process_names}"
            )
            proc_windows: List[tuple[int, str, str, WindowRect]] = []
            try:
                all_procs = _list_processes()
            except Exception as e:
                self._log.error(f"枚举进程失败: {e}")
                all_procs = []
            for pid, exe_name in all_procs:
                if exe_name.lower() not in self._process_names:
                    continue
                wins = _find_windows_by_pid(pid)
                if wins:
                    self._log.info(
                        f"进程 {exe_name} (pid={pid}) 找到 {len(wins)} 个窗口"
                    )
                    proc_windows.extend(wins)
            if proc_windows:
                # 选面积最大的窗口
                proc_windows.sort(key=lambda w: w[3].area, reverse=True)
                best_hwnd, best_title, best_cls, best_rect = proc_windows[0]
                self._hwnd = best_hwnd
                self._log.info(
                    f"Minecraft 窗口已找到 (进程匹配): "
                    f"标题='{best_title}', 类名='{best_cls}', "
                    f"位置=({best_rect.left},{best_rect.top}), "
                    f"尺寸={best_rect.width}x{best_rect.height}"
                )
                return True

            # ---- 诊断：列出所有可见窗口，帮助定位 Minecraft 实际窗口 ----
            self._log.warn("===== 当前所有可见窗口 =====")
            all_windows: List[tuple[str, str, int, int]] = []

            def _diag_callback(hwnd: int, _) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                cls_name = win32gui.GetClassName(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w < 200 or h < 100:
                    return
                all_windows.append((title or "(无标题)", cls_name, w, h))

            try:
                win32gui.EnumWindows(_diag_callback, None)
            except Exception:
                pass
            for t, c, w, h in all_windows:
                self._log.warn(f"  [{c}] {w}x{h}  {t}")
            self._log.warn("==============================")

            # 诊断：列出匹配的进程
            if all_procs:
                matched = [
                    (pid, name)
                    for pid, name in all_procs
                    if name.lower() in self._process_names
                ]
                if matched:
                    self._log.warn("===== 匹配到的进程 (无可用窗口) =====")
                    for pid, name in matched:
                        self._log.warn(f"  pid={pid}  {name}")
                    self._log.warn("====================================")

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
        """Client-area coordinates keep saved ROI independent of window decorations."""
        if self._hwnd is None:
            return None
        if not win32gui.IsWindow(self._hwnd):
            self._log.warn("Minecraft 窗口已失效 (句柄无效)")
            self._hwnd = None
            return None
        try:
            client = win32gui.GetClientRect(self._hwnd)
            left, top = win32gui.ClientToScreen(self._hwnd, (client[0], client[1]))
            right, bottom = win32gui.ClientToScreen(self._hwnd, (client[2], client[3]))
            rect = (left, top, right, bottom)
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
