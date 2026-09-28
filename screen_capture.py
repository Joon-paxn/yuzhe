"""
screen_capture.py
-----------------
窗口截图模块。

借鉴 Tau-main：优先使用 PrintWindow (PW_RENDERFULLCONTENT=2) 抓取游戏窗口，
即使窗口被其他窗口遮挡/非前台也能获取游戏自身画面 (通过 DWM 合成)。
失败时回退到 mss 屏幕截图 (窗口必须可见不被遮挡)。

接口：
- capture_window(mc_window, use_print_window=True) -> np.ndarray (RGB) 或 None
  截取整个 Minecraft 窗口画面。
- capture_rect(left, top, width, height, use_print_window=True) -> np.ndarray (RGB) 或 None
  截取屏幕指定矩形。

注意：
- PrintWindow 返回 BGRA，需转 RGB。
- 全黑画面视为抓取失败 (回退)。
- 仅支持 Windows。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

import numpy as np

import mss
from minecraft_window import MinecraftWindow, WindowRect


# BITMAPINFOHEADER 结构
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


_PW_RENDERFULLCONTENT = 2  # DWM 合成完整内容，OpenGL/DirectX 后台也能抓


def _print_window(hwnd: int, rect: WindowRect) -> Optional[np.ndarray]:
    """
    用 PrintWindow 抓取窗口画面 (后台/被遮挡也可用)。
    返回 RGB ndarray，失败返回 None。
    """
    w, h = rect.width, rect.height
    if w <= 0 or h <= 0:
        return None
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd_dc = user32.GetDC(hwnd)
    if not hwnd_dc:
        return None
    try:
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        if not mem_dc:
            return None
        try:
            bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
            if not bitmap:
                return None
            try:
                old = gdi32.SelectObject(mem_dc, bitmap)
                # PW_RENDERFULLCONTENT=2：走 DWM 合成内容
                if user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT) == 0:
                    return None
                bmi = _BITMAPINFOHEADER()
                bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                bmi.biWidth = w
                bmi.biHeight = -h   # 自顶向下
                bmi.biPlanes = 1
                bmi.biBitCount = 32
                buf = ctypes.create_string_buffer(w * h * 4)
                if gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(bmi), 0) == 0:
                    return None
                # BGRA -> RGB (丢弃 alpha 通道)
                img = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
                img_rgb = img[:, :, :3][:, :, ::-1].copy()
                # 全黑视为抓取失败
                if img_rgb.max() == 0:
                    return None
                return img_rgb
            finally:
                gdi32.SelectObject(mem_dc, old)
                gdi32.DeleteObject(bitmap)
        finally:
            gdi32.DeleteDC(mem_dc)
    finally:
        user32.ReleaseDC(hwnd, hwnd_dc)


def _mss_grab(left: int, top: int, width: int, height: int) -> Optional[np.ndarray]:
    """用 mss 截取屏幕指定矩形 (RGB)，失败返回 None"""
    if width <= 0 or height <= 0:
        return None
    try:
        with mss.mss() as sct:
            monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
            return np.array(shot)[:, :, :3]
    except Exception:
        return None


def capture_window(
    mc_window: MinecraftWindow, use_print_window: bool = True,
) -> Optional[np.ndarray]:
    """
    截取整个 Minecraft 窗口画面 (RGB ndarray)。

    Args:
        mc_window: Minecraft 窗口对象
        use_print_window: 是否优先用 PrintWindow (后台抓图)

    Returns:
        RGB ndarray (H, W, 3) 或 None (窗口无效/抓取失败)
    """
    if not mc_window.is_valid():
        return None
    rect = mc_window.get_rect()
    if rect is None:
        return None
    hwnd = mc_window.hwnd
    if use_print_window and hwnd is not None:
        img = _print_window(hwnd, rect)
        if img is not None:
            return img
    # 回退 mss 屏幕截图
    return _mss_grab(rect.left, rect.top, rect.width, rect.height)


def capture_rect(
    left: int, top: int, width: int, height: int,
    mc_window: Optional[MinecraftWindow] = None, use_print_window: bool = True,
) -> Optional[np.ndarray]:
    """
    截取屏幕/窗口指定矩形 (RGB ndarray)。

    若提供 mc_window 且 use_print_window，则用 PrintWindow 抓整窗后裁剪 ROI；
    否则用 mss 截屏幕矩形。

    Args:
        left, top, width, height: 矩形 (屏幕坐标)
        mc_window: Minecraft 窗口对象 (用于 PrintWindow 抓整窗后裁剪)
        use_print_window: 是否优先用 PrintWindow

    Returns:
        RGB ndarray 或 None
    """
    if mc_window is not None and use_print_window and mc_window.is_valid():
        full = capture_window(mc_window, use_print_window=True)
        if full is not None:
            rect = mc_window.get_rect()
            if rect is not None:
                # 相对窗口坐标裁剪
                rx = left - rect.left
                ry = top - rect.top
                h, w = full.shape[:2]
                x0 = max(0, rx)
                y0 = max(0, ry)
                x1 = min(w, rx + width)
                y1 = min(h, ry + height)
                if x1 > x0 and y1 > y0:
                    return full[y0:y1, x0:x1].copy()
    # 回退 mss
    return _mss_grab(left, top, width, height)
