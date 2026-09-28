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

用于让用户精确指定「咬钩！」文字中某个像素的颜色，
之后检测器就以该颜色 (含容差) 为目标进行检测。
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


def pick_pixel_color(hint: str = "左键取色  |  右键 / Esc 取消") -> Optional[Tuple[int, int, int]]:
    """
    启动全屏取色器，返回用户点击位置的像素 RGB 值。

    Args:
        hint: 顶部提示文字 (用于区分多重取色，如 "取色 A 点" / "取色 B 点")。

    Returns:
        (R, G, B) 元组；右键 / Esc / 关闭窗口时返回 None。
    """
    result: dict = {"color": None, "cancelled": False}

    # 1. 截取全屏
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # 所有显示器组合的虚拟屏幕
            shot = sct.grab(monitor)
            screen_w, screen_h = shot.size
            # 保存原始像素数组用于取色 (BGRA)
            raw = np.array(shot)
            # RGB 图像用于背景显示
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception as e:
        print(f"[pixel_picker] 全屏截图失败: {e}")
        return None

    # 2. 创建全屏覆盖窗口
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(cursor="cross")
    # 确保窗口获得焦点，接收键盘事件
    root.focus_force()
    # 强制接管输入，防止焦点被 Minecraft 等其他窗口抢走
    root.grab_set()

    photo = ImageTk.PhotoImage(img, master=root)
    canvas = tk.Canvas(
        root, width=screen_w, height=screen_h,
        highlightthickness=0, cursor="cross",
    )
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.image = photo  # 防止 GC

    # 提示文字
    hint_id = canvas.create_text(
        screen_w // 2, 30,
        text=hint,
        fill="yellow", font=("Microsoft YaHei", 18, "bold"),
    )

    # 状态
    state = {
        "mag_img": None,       # 放大镜 PhotoImage 引用
        "mag_rect_id": None,   # 放大镜背景矩形 id
        "mag_img_id": None,    # 放大镜图像 id
        "cross_h": None,       # 十字准星横线 id
        "cross_v": None,       # 十字准星竖线 id
        "info_id": None,       # RGB 文字 id
    }

    def get_cursor_xy(_event) -> Tuple[int, int]:
        """获取鼠标在屏幕上的坐标 (与截图坐标一致)"""
        x = root.winfo_pointerx()
        y = root.winfo_pointery()
        # 限制在屏幕范围内
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
        # 放大镜位置：尽量放在光标右下方，超出屏幕则翻到另一侧
        mx = x + MAGNIFY_OFFSET
        my = y + MAGNIFY_OFFSET
        if mx + MAGNIFY_SIZE > screen_w:
            mx = x - MAGNIFY_SIZE - MAGNIFY_OFFSET
        if my + MAGNIFY_SIZE > screen_h:
            my = y - MAGNIFY_SIZE - MAGNIFY_OFFSET
        mx = max(0, mx)
        my = max(0, my)

        # 截取光标周围的小区域并放大
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

        # 放大镜背景框 (黄色边框)
        state["mag_rect_id"] = canvas.create_rectangle(
            mx, my, mx + MAGNIFY_SIZE, my + MAGNIFY_SIZE,
            outline="yellow", width=2,
        )
        state["mag_img_id"] = canvas.create_image(
            mx, my, image=state["mag_img"], anchor="nw",
        )

        # 十字准星 (放大镜中心)
        cx = mx + MAGNIFY_SIZE // 2
        cy = my + MAGNIFY_SIZE // 2
        state["cross_h"] = canvas.create_line(
            cx - CROSS_SIZE, cy, cx + CROSS_SIZE, cy,
            fill="red", width=1,
        )
        state["cross_v"] = canvas.create_line(
            cx, cy - CROSS_SIZE, cx, cy + CROSS_SIZE,
            fill="red", width=1,
        )

        # RGB 信息
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
        """左键确认取色"""
        x, y = get_cursor_xy(event)
        result["color"] = pixel_rgb(x, y)
        root.destroy()

    def on_cancel(_event=None) -> None:
        """右键 / Esc 取消"""
        result["color"] = None
        result["cancelled"] = True
        root.destroy()

    # 事件绑定 (绑定到全屏 canvas，确保一定能收到)
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Button-1>", on_pick)
    canvas.bind("<Button-3>", on_cancel)
    # Esc 同时绑定到 root 和 canvas，避免焦点落在 canvas 时 root 收不到
    root.bind("<Escape>", on_cancel)
    canvas.bind("<Escape>", on_cancel)
    # 窗口关闭按钮也作为取消
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    root.mainloop()
    return result["color"]
