"""
input_controller.py
-------------------
鼠标输入控制模块。

封装独立的左键 / 右键点击操作。
使用 pyautogui 发送鼠标事件，确保每次点击都是独立的
mouseDown -> mouseUp，而非持续按住。

安全约束：
- 所有点击操作前必须确认 Minecraft 窗口有效。
- 点击位置固定在 Minecraft 窗口中心 (避免点击到其他窗口)。
"""

from __future__ import annotations

from typing import Optional

import pyautogui

from minecraft_window import MinecraftWindow
from logger import get_logger


# pyautogui 全局安全设置：移动到屏幕角落不会触发 FailSafeException，
# 因为自动钓鱼时鼠标可能移动到任何位置。把这个保护关闭，
# 真正的安全依赖于 Minecraft 窗口有效性检查。
pyautogui.FAILSAFE = False
# 关闭 pyautogui 内置的点击间 pause，由我们自己用 monotonic 精确控制节奏
pyautogui.PAUSE = 0


class InputController:
    """鼠标输入控制器，所有操作均校验 Minecraft 窗口有效性"""

    def __init__(self, mc_window: MinecraftWindow) -> None:
        self._mc = mc_window
        self._log = get_logger()

    def _click_center(self, button: str) -> bool:
        """
        在 Minecraft 窗口中心执行一次独立点击。

        Args:
            button: "left" 或 "right"

        Returns:
            是否成功执行 (窗口无效则返回 False)
        """
        if not self._mc.is_valid():
            self._log.warn("Minecraft 窗口无效，取消鼠标操作")
            return False

        rect = self._mc.get_rect()
        if rect is None:
            return False

        cx = rect.left + rect.width // 2
        cy = rect.top + rect.height // 2

        try:
            # 使用 click 而非 mouseDown/mouseUp 长按时序，
            # pyautogui.click 内部是 down + 立即 up，即一次独立点击。
            pyautogui.click(x=cx, y=cy, button=button)
            return True
        except Exception as e:
            self._log.error(f"鼠标点击异常: {e}")
            return False

    def left_click(self) -> bool:
        """执行一次独立的左键点击"""
        return self._click_center("left")

    def right_click(self) -> bool:
        """执行一次独立的右键点击"""
        return self._click_center("right")

    def move_to_center(self) -> Optional[tuple[int, int]]:
        """将鼠标移动到 Minecraft 窗口中心，返回移动后坐标"""
        if not self._mc.is_valid():
            return None
        rect = self._mc.get_rect()
        if rect is None:
            return None
        cx = rect.left + rect.width // 2
        cy = rect.top + rect.height // 2
        try:
            pyautogui.moveTo(cx, cy, duration=0)
            return (cx, cy)
        except Exception as e:
            self._log.error(f"鼠标移动异常: {e}")
            return None
