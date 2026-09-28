"""输入与截图工具 (钓鱼用)。

保留原有像素检测 + 右键方案。
input_mode: global (pyautogui) / window (PostMessageW 后台注入)
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

import mss

_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_VK_RBUTTON = 0x02
_MK_RBUTTON = 0x0002


def find_window(keyword: str) -> Optional[int]:
    user32 = ctypes.windll.user32
    hwnd = None

    def _cb(h, _):
        nonlocal hwnd
        length = user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            if keyword in buf.value:
                hwnd = h
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_cb), 0)
    return hwnd


def grab_pixel(x: int, y: int, hwnd: Optional[int] = None) -> Optional[Tuple[int, int, int]]:
    """截取屏幕像素 (x,y) 的 RGB。"""
    try:
        with mss.mss() as sct:
            img = sct.grab({"left": x, "top": y, "width": 1, "height": 1})
            return (img.pixel(0, 0)[0], img.pixel(0, 0)[1], img.pixel(0, 0)[2])
    except Exception:
        return None


def right_click(hwnd: Optional[int] = None, input_mode: str = "global") -> bool:
    """右键点击。window 模式用 PostMessageW 后台注入，global 用 pyautogui。"""
    if hwnd and input_mode == "window":
        try:
            ctypes.windll.user32.PostMessageW(hwnd, _WM_RBUTTONDOWN, _MK_RBUTTON, 0)
            ctypes.windll.user32.PostMessageW(hwnd, _WM_RBUTTONUP, 0, 0)
            return True
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.rightClick()
        return True
    except Exception:
        return False
