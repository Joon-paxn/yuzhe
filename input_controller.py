"""
input_controller.py
-------------------
鼠标 + 键盘输入控制模块。

封装独立的左键 / 右键点击操作、键盘按键 (按住/释放)、鼠标相对移动。
使用 pyautogui 发送事件，确保每次点击都是独立的
mouseDown -> mouseUp，而非持续按住。

阶段六新增：
- 键盘按键: press_key / release_key / release_all_keys (W/A/S/D/Space 等)
- 鼠标相对移动: move_mouse_rel (用于 Minecraft 视角旋转)
- 当前按下的键集合 _held_keys，stop/focus-loss 时可一键全部释放

安全约束：
- 所有操作前必须确认 Minecraft 窗口有效。
- 点击位置固定在 Minecraft 窗口中心 (避免点击到其他窗口)。
- 键盘按键前校验窗口为前台 (避免向其他程序发送按键)。
- release_all_keys 由上层 (F7 停止 / 窗口失焦) 调用，确保不卡键。
"""

from __future__ import annotations

from typing import Optional, Set

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
    """鼠标 + 键盘输入控制器，所有操作均校验 Minecraft 窗口有效性"""

    def __init__(self, mc_window: MinecraftWindow) -> None:
        self._mc = mc_window
        self._log = get_logger()
        # 当前按下的键集合 (用于安全释放)，线程安全由 GIL 保证简单 set 操作
        self._held_keys: Set[str] = set()

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

    # ---------------- 阶段六：键盘按键 ----------------

    def _check_key_safety(self) -> bool:
        """
        键盘操作前的安全检查：窗口有效且为前台。
        键盘按键会直接发送到前台窗口，若 Minecraft 不在前台则不能发送，
        避免向其他程序发送 WASD 等按键。
        """
        if not self._mc.is_valid():
            self._log.warn("Minecraft 窗口无效，取消键盘操作")
            return False
        if not self._mc.is_foreground():
            self._log.warn("Minecraft 窗口未在前台，取消键盘操作 (防止误操作其他程序)")
            return False
        return True

    def press_key(self, key: str) -> bool:
        """
        按下一个键 (不释放，持续按住状态)。
        配合 release_key 实现按住移动。重复按同一键视为幂等。

        Args:
            key: pyautogui 键名 (如 "w", "a", "s", "d", "space", "shift")

        Returns:
            是否成功执行
        """
        if not self._check_key_safety():
            return False
        if key in self._held_keys:
            return True
        try:
            pyautogui.keyDown(key)
            self._held_keys.add(key)
            return True
        except Exception as e:
            self._log.error(f"按键按下异常 key={key!r}: {e}")
            return False

    def release_key(self, key: str) -> None:
        """
        释放一个键。无论是否在 _held_keys 中都尝试释放 (容错)。
        不会抛异常，用于 stop / focus-loss 安全释放。
        """
        try:
            pyautogui.keyUp(key)
        except Exception as e:
            self._log.error(f"按键释放异常 key={key!r}: {e}")
        finally:
            self._held_keys.discard(key)

    def release_all_keys(self) -> None:
        """
        释放所有当前按下的键 (安全释放)。
        F7 停止 / 窗口失焦 / 异常时调用，确保不卡键。
        """
        if not self._held_keys:
            return
        keys = list(self._held_keys)
        self._log.info(f"释放所有按键: {keys}")
        for key in keys:
            try:
                pyautogui.keyUp(key)
            except Exception as e:
                self._log.error(f"释放按键异常 key={key!r}: {e}")
        self._held_keys.clear()

    def tap_key(self, key: str, duration_s: float = 0.05) -> bool:
        """
        独立点击一次按键 (按下 + 短暂等待 + 释放)。
        用于跳跃 (space) 等单次操作。duration_s 由调用方控制，
        但本方法内的等待不可中断 (短促操作)；长按移动请用
        MovementPlanner.hold_key 配合 stop_event。

        Returns:
            是否成功执行按下 (释放失败仅记录日志)
        """
        if not self._check_key_safety():
            return False
        try:
            pyautogui.keyDown(key)
            self._held_keys.add(key)
            # 短暂停顿 (不可中断，但 duration 很短)
            import time as _t
            _t.sleep(max(0.0, min(duration_s, 0.2)))
            pyautogui.keyUp(key)
            self._held_keys.discard(key)
            return True
        except Exception as e:
            self._log.error(f"按键点击异常 key={key!r}: {e}")
            self._held_keys.discard(key)
            return False

    def jump(self) -> bool:
        """按一次空格键 (跳跃)"""
        return self.tap_key("space", duration_s=0.05)

    @property
    def held_keys(self) -> Set[str]:
        """当前按下的键集合 (只读视图)"""
        return set(self._held_keys)

    # ---------------- 阶段六：鼠标相对移动 (视角旋转) ----------------

    def move_mouse_rel(self, dx: int, dy: int = 0) -> bool:
        """
        鼠标相对移动 (dx, dy) 像素。用于 Minecraft 视角旋转。
        Minecraft 默认鼠标控制视角，相对移动即旋转视角。
        不校验前台 (视角旋转可在窗口内进行)，但仍校验窗口有效。

        Args:
            dx: 水平相对位移 (正=向右转视角，负=向左)
            dy: 垂直相对位移 (正=向下，负=向上，一般保持 0 避免视角上下偏)

        Returns:
            是否成功执行
        """
        if not self._mc.is_valid():
            self._log.warn("Minecraft 窗口无效，取消鼠标相对移动")
            return False
        try:
            pyautogui.moveRel(dx, dy, duration=0)
            return True
        except Exception as e:
            self._log.error(f"鼠标相对移动异常 dx={dx} dy={dy}: {e}")
            return False
