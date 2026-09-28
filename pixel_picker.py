"""
pixel_picker.py
---------------
屏幕取色器 + 放大镜工具。

采用「全屏覆盖层」方案 (与 F8 区域选择一致)：
- 截取全屏作为覆盖层背景，保证鼠标点击/按键一定落在本程序窗口上，
  不会被 Minecraft 等其他窗口抢走 (解决之前右键/Esc 无法取消的问题)。
- 鼠标移动时，在光标旁绘制放大镜，显示放大后的画面和中心像素 RGB。
- 左键点击：确认选取该像素颜色并返回。
- 右键点击 / Esc：取消并返回 None。

支持一次会话内连续取多个颜色 (F10 双重取色用)，
在同一个 Tk 根窗口内依次取色，避免多次创建/销毁 Tk 导致第二个窗口不显示。
"""

from __future__ import annotations

from typing import Optional, Tuple

import mss
import numpy as np
from PIL import Image, ImageTk

import tkinter as tk


# 放大镜参数
MAGNIFY_SIZE = 160       # 放大镜区域边长 (像素)
MAGNIFY_ZOOM = 5         # 放大倍数
CROSS_SIZE = 12          # 十字准星半长
MAGNIFY_OFFSET = 20      # 放大镜相对光标的偏移 (避免挡住光标)


def pick_pixel_colors(hints: list) -> list:
    """
    启动一次全屏取色会话，连续取多个颜色。

    在同一个 Tk 根窗口内依次取色，避免多次创建/销毁 Tk 导致的窗口不显示问题。
    每取完一个，顶部提示自动切换为下一个，取完所有或中途取消即退出。

    Args:
        hints: 每次取色的提示文字列表，长度即取色次数
               (如 ["取色 A 点...", "取色 B 点..."])

    Returns:
        与 hints 等长的列表，每项为 (R,G,B) 元组；中途取消时剩余项为 None。
    """
    n = len(hints)
    if n == 0:
        return []
    results = [None] * n
    state_idx = {"i": 0}  # 当前要取的是第几个

    # 1. 截取全屏 (整个会话复用同一张截图，保证 A/B 来自同一帧)
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            screen_w, screen_h = shot.size
            raw = np.array(shot)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception as e:
        print(f"[pixel_picker] 全屏截图失败: {e}")
        return results

    # 2. 创建全屏覆盖窗口 (整个会话只创建一次)
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(cursor="cross")
    root.focus_force()
    root.grab_set()

    photo = ImageTk.PhotoImage(img, master=root)
    canvas = tk.Canvas(
        root, width=screen_w, height=screen_h,
        highlightthickness=0, cursor="cross",
    )
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.image = photo  # 防止 GC

    # 提示文字 (随取色进度切换)
    hint_id = canvas.create_text(
        screen_w // 2, 30,
        text=hints[0],
        fill="yellow", font=("Microsoft YaHei", 18, "bold"),
    )
    # 进度文字
    prog_id = canvas.create_text(
        screen_w // 2, 58,
        text=f"第 1 / {n} 点",
        fill="lime", font=("Microsoft YaHei", 13, "bold"),
    )

    # 放大镜状态
    state = {
        "mag_img": None, "mag_rect_id": None, "mag_img_id": None,
        "cross_h": None, "cross_v": None, "info_id": None,
    }

    def get_cursor_xy(_event) -> Tuple[int, int]:
        """获取鼠标在屏幕上的坐标 (与截图坐标一致)"""
        x = root.winfo_pointerx()
        y = root.winfo_pointery()
        x = max(0, min(screen_w - 1, x))
        y = max(0, min(screen_h - 1, y))
        return x, y

    def pixel_rgb(x: int, y: int) -> Tuple[int, int, int]:
        """从截图数组中读取 (x,y) 像素的 RGB 值"""
        # raw 是 BGRA 格式
        b, g, r = int(raw[y, x, 0]), int(raw[y, x, 1]), int(raw[y, x, 2])
        return (r, g, b)

    def draw_magnifier(x: int, y: int) -> None:
        """在光标旁绘制放大镜 + RGB 信息"""
        mx = x + MAGNIFY_OFFSET
        my = y + MAGNIFY_OFFSET
        if mx + MAGNIFY_SIZE > screen_w:
            mx = x - MAGNIFY_SIZE - MAGNIFY_OFFSET
        if my + MAGNIFY_SIZE > screen_h:
            my = y - MAGNIFY_SIZE - MAGNIFY_OFFSET
        mx = max(0, mx)
        my = max(0, my)

        half = MAGNIFY_SIZE // (2 * MAGNIFY_ZOOM)
        crop_x1 = max(0, x - half)
        crop_y1 = max(0, y - half)
        crop_x2 = min(screen_w, x + half)
        crop_y2 = min(screen_h, y + half)
        crop = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
        mag = crop.resize((MAGNIFY_SIZE, MAGNIFY_SIZE), Image.NEAREST)
        state["mag_img"] = ImageTk.PhotoImage(mag, master=root)

        # 清除旧放大镜
        for item in (state["mag_rect_id"], state["mag_img_id"],
                     state["cross_h"], state["cross_v"], state["info_id"]):
            if item is not None:
                canvas.delete(item)

        state["mag_rect_id"] = canvas.create_rectangle(
            mx, my, mx + MAGNIFY_SIZE, my + MAGNIFY_SIZE,
            outline="yellow", width=2,
        )
        state["mag_img_id"] = canvas.create_image(
            mx, my, image=state["mag_img"], anchor="nw",
        )

        cx = mx + MAGNIFY_SIZE // 2
        cy = my + MAGNIFY_SIZE // 2
        state["cross_h"] = canvas.create_line(
            cx - CROSS_SIZE, cy, cx + CROSS_SIZE, cy, fill="red", width=1,
        )
        state["cross_v"] = canvas.create_line(
            cx, cy - CROSS_SIZE, cx, cy + CROSS_SIZE, fill="red", width=1,
        )

        r, g, b = pixel_rgb(x, y)
        state["info_id"] = canvas.create_text(
            mx + MAGNIFY_SIZE // 2, my + MAGNIFY_SIZE + 14,
            text=f"RGB({r},{g},{b})  位置({x},{y})",
            fill="yellow", font=("Consolas", 11, "bold"),
        )

    def on_motion(event) -> None:
        x, y = get_cursor_xy(event)
        draw_magnifier(x, y)

    def on_pick(event) -> None:
        """左键确认当前点；取完最后一个才关闭，否则切到下一个提示"""
        x, y = get_cursor_xy(event)
        results[state_idx["i"]] = pixel_rgb(x, y)
        state_idx["i"] += 1
        if state_idx["i"] >= n:
            try:
                root.grab_release()
            except Exception:
                pass
            root.destroy()
        else:
            canvas.itemconfig(hint_id, text=hints[state_idx["i"]])
            canvas.itemconfig(prog_id, text=f"第 {state_idx['i'] + 1} / {n} 点")

    def on_cancel(_event=None) -> None:
        """右键 / Esc / 关闭：结束会话，剩余项保持 None"""
        try:
            root.grab_release()
        except Exception:
            pass
        root.destroy()

    # 事件绑定 (绑定到全屏 canvas，确保一定能收到)
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Button-1>", on_pick)
    canvas.bind("<Button-3>", on_cancel)
    root.bind("<Escape>", on_cancel)
    canvas.bind("<Escape>", on_cancel)
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    root.mainloop()
    return results


def pick_pixel_color(hint: str = "左键取色  |  右键 / Esc 取消") -> Optional[Tuple[int, int, int]]:
    """单次取色 (F11 用)。等价于 pick_pixel_colors([hint])[0]。"""
    return pick_pixel_colors([hint])[0]
