"""
region_selector.py
------------------
检测区域 (ROI) 框选模块。

流程：
1. 使用 mss 截取整个屏幕。
2. 创建一个全屏 tkinter 窗口，显示截图作为背景。
3. 用户按住鼠标左键拖动，框出矩形区域。
4. 松开鼠标后返回选中的屏幕坐标 (x, y, w, h)。
5. 调用方根据 Minecraft 窗口矩形将其转换为相对比例并保存。

使用 tkinter (Python 内置)，无需额外 GUI 依赖。
"""

from __future__ import annotations

from typing import Optional, Tuple

import mss
import numpy as np
from PIL import Image, ImageTk

import tkinter as tk

from minecraft_window import MinecraftWindow, WindowRect
from logger import get_logger


def select_region_screen() -> Optional[Tuple[int, int, int, int]]:
    """
    全屏框选一个矩形区域。

    Returns:
        (x, y, w, h) 屏幕坐标。用户取消 (按 Esc 或关闭窗口) 时返回 None。
    """
    log = get_logger()

    # 1. 截取全屏
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # 所有显示器组合的虚拟屏幕
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception as e:
        log.error(f"全屏截图失败: {e}")
        return None

    screen_w = img.width
    screen_h = img.height

    # 2. 创建全屏窗口
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(cursor="cross")

    # 将截图显示为背景
    photo = ImageTk.PhotoImage(img, master=root)
    canvas = tk.Canvas(root, width=screen_w, height=screen_h, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    # 保持 photo 引用，防止被 GC
    canvas.image = photo  # type: ignore[attr-defined]

    # 提示文字
    canvas.create_text(
        screen_w // 2, 30,
        text="按住鼠标左键拖动框选「咬钩！」区域，松开确认；按 Esc 取消",
        fill="yellow", font=("Microsoft YaHei", 16, "bold"),
    )

    state: dict = {
        "start_x": 0,
        "start_y": 0,
        "rect_id": None,
        "result": None,
    }

    def on_press(event):
        state["start_x"] = event.x
        state["start_y"] = event.y
        state["rect_id"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="red", width=3,
        )

    def on_drag(event):
        if state["rect_id"] is not None:
            canvas.coords(
                state["rect_id"],
                state["start_x"], state["start_y"],
                event.x, event.y,
            )

    def on_release(event):
        x1 = min(state["start_x"], event.x)
        y1 = min(state["start_y"], event.y)
        x2 = max(state["start_x"], event.x)
        y2 = max(state["start_y"], event.y)
        w = x2 - x1
        h = y2 - y1
        if w >= 5 and h >= 5:
            state["result"] = (x1, y1, w, h)
        root.destroy()

    def on_cancel(_event=None):
        state["result"] = None
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_cancel)

    root.mainloop()
    return state["result"]


def select_roi_relative(mc_window: MinecraftWindow) -> Optional[Tuple[float, float, float, float]]:
    """
    框选检测区域并转换为相对于 Minecraft 窗口的比例。

    Returns:
        (x_ratio, y_ratio, w_ratio, h_ratio) 范围 0~1。
        取消或窗口无效时返回 None。
    """
    log = get_logger()

    win_rect = mc_window.get_rect()
    if win_rect is None:
        log.error("Minecraft 窗口无效，无法选择检测区域")
        return None

    screen_rect = select_region_screen()
    if screen_rect is None:
        log.info("已取消区域选择")
        return None

    sx, sy, sw, sh = screen_rect
    # 转换为相对于 Minecraft 窗口的比例
    if win_rect.width <= 0 or win_rect.height <= 0:
        log.error("Minecraft 窗口尺寸异常")
        return None

    x_ratio = (sx - win_rect.left) / win_rect.width
    y_ratio = (sy - win_rect.top) / win_rect.height
    w_ratio = sw / win_rect.width
    h_ratio = sh / win_rect.height

    # 限制在 0~1
    x_ratio = max(0.0, min(1.0, x_ratio))
    y_ratio = max(0.0, min(1.0, y_ratio))
    w_ratio = max(0.0, min(1.0 - x_ratio, w_ratio))
    h_ratio = max(0.0, min(1.0 - y_ratio, h_ratio))

    log.info(
        f"已选择检测区域: "
        f"屏幕=({sx},{sy},{sw}x{sh}), "
        f"相对比例=({x_ratio:.4f},{y_ratio:.4f},{w_ratio:.4f},{h_ratio:.4f})"
    )
    return (x_ratio, y_ratio, w_ratio, h_ratio)
