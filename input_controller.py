"""
input_controller.py
-------------------
鼠标 + 键盘输入控制模块。

封装独立的左键 / 右键点击操作、键盘按键 (按住/释放)、鼠标相对移动。
使用 pyautogui 发送事件，确保每次点击都是独立的
mouseDown -> mouseUp，而非持续按住。

借鉴 Tau-main：
- 支持 window 输入模式 (PostMessageW 向游戏窗口直接注入键鼠消息)，
  MC 窗口被遮挡/非前台时也能操作，不影响用户真实键鼠；失败自动回退 global 模式。
- global 模式为原有 pyautogui 全局注入 (需窗口前台)。
- 视角旋转 (mouse_event) 必须用全局，因为 GLFW 光标锁定模式下 PostMessage
  WM_MOUSEMOVE 无法驱动视角 (Tau 已验证)。

阶段六新增：
- 键盘按键: press_key / release_key / release_all_keys (W/A/S/D/Space 等)
- 鼠标相对移动: move_mouse_rel (用于 Minecraft 视角旋转)
- 当前按下的键集合 _held_keys，stop/focus-loss 时可一键全部释放

安全约束：
- 所有操作前必须确认 Minecraft 窗口有效。
- 点击位置固定在 Minecraft 窗口中心 (避免点击到其他窗口)。
- global 模式键盘按键前校验窗口为前台 (避免向其他程序发送按键)。
- window 模式无需前台 (PostMessage 直达窗口)。
- release_all_keys 由上层 (F7 停止 / 窗口失焦) 调用，确保不卡键。
"""

from __future__ import annotations

import ctypes
from typing import Optional, Set

import pyautogui
import win32gui

from minecraft_window import MinecraftWindow
from logger import get_logger


# pyautogui 全局安全设置：移动到屏幕角落不会触发 FailSafeException，
# 因为自动钓鱼时鼠标可能移动到任何位置。把这个保护关闭，
# 真正的安全依赖于 Minecraft 窗口有效性检查。
pyautogui.FAILSAFE = False
# 关闭 pyautogui 内置的点击间 pause，由我们自己用 monotonic 精确控制节奏
pyautogui.PAUSE = 0


# Windows 消息常量 (借鉴 Tau-main)
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_MK_RBUTTON = 0x0002
_MK_LBUTTON = 0x0001
_KEYUP_LPARAM = 0xC0000000  # prev=1, transition=1

# 键名 -> 虚拟键码
_VK_MAP = {
    'w': 0x57, 'a': 0x41, 's': 0x53, 'd': 0x44,
    'space': 0x20, 'f3': 0x72, 'c': 0x43,
    'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12,
}


class InputController:
    """鼠标 + 键盘输入控制器，所有操作均校验 Minecraft 窗口有效性"""

    def __init__(self, mc_window: MinecraftWindow, input_mode: str = "global") -> None:
        self._mc = mc_window
        self._log = get_logger()
        # 当前按下的键集合 (用于安全释放)，线程安全由 GIL 保证简单 set 操作
        self._held_keys: Set[str] = set()
        # 输入模式："global" (pyautogui 全局) 或 "window" (PostMessageW 窗口消息)
        self._input_mode = input_mode if input_mode in ("global", "window") else "global"
        self._user32 = ctypes.windll.user32

    @property
    def input_mode(self) -> str:
        return self._input_mode

    def set_input_mode(self, mode: str) -> None:
        """切换输入模式。window 模式需窗口有效；切换失败回退 global。"""
        mode = mode if mode in ("global", "window") else "global"
        if mode == "window":
            if not self._mc.is_valid():
                self._log.warn("Minecraft 窗口无效，无法切换到 window 输入模式，回退 global")
                self._input_mode = "global"
                return
        # 切换前释放当前所有按键 (避免卡在旧模式)
        self.release_all_keys()
        self._input_mode = mode
        self._log.info(f"输入模式已切换为: {mode}")

    def _get_hwnd(self) -> Optional[int]:
        """window 模式返回游戏窗口句柄；global 模式或无窗口返回 None"""
        if self._input_mode != "window":
            return None
        if not self._mc.is_valid():
            return None
        return self._mc.hwnd

    def _fallback_global(self, why: str) -> None:
        """window 模式操作失败时回退到 global 模式"""
        self._log.warn(f"{why}，已回退到 global 输入模式")
        self._input_mode = "global"

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

        # window 模式：PostMessageW 注入点击到窗口客户区中心 (被遮挡也可用)
        hwnd = self._get_hwnd()
        if hwnd is not None:
            try:
                l, t, r, b = win32gui.GetClientRect(hwnd)
                cx, cy = (r - l) // 2, (b - t) // 2
                lparam = (cy << 16) | (cx & 0xFFFF)
                if button == "right":
                    self._user32.PostMessageW(hwnd, _WM_RBUTTONDOWN, _MK_RBUTTON, lparam)
                    self._user32.PostMessageW(hwnd, _WM_RBUTTONUP, 0, lparam)
                else:
                    self._user32.PostMessageW(hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON, lparam)
                    self._user32.PostMessageW(hwnd, _WM_LBUTTONUP, 0, lparam)
                return True
            except Exception as e:
                self._fallback_global(f"窗口模式点击失败: {e}")
                # 回退后 fall through 到 global 点击

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
        键盘操作前的安全检查。
        - window 模式：只需窗口有效 (PostMessage 直达窗口，无需前台)
        - global 模式：窗口有效且为前台 (避免向其他程序发送按键)
        """
        if not self._mc.is_valid():
            self._log.warn("Minecraft 窗口无效，取消键盘操作")
            return False
        if self._input_mode == "global" and not self._mc.is_foreground():
            self._log.warn("Minecraft 窗口未在前台，取消键盘操作 (防止误操作其他程序)")
            return False
        return True

    def _key_down_window(self, key: str, hwnd: int) -> None:
        """window 模式：PostMessage 按键按下"""
        vk = _VK_MAP.get(key.lower())
        if vk is not None:
            self._user32.PostMessageW(hwnd, _WM_KEYDOWN, vk, 0)
        else:
            pyautogui.keyDown(key)

    def _key_up_window(self, key: str, hwnd: int) -> None:
        """window 模式：PostMessage 按键释放"""
        vk = _VK_MAP.get(key.lower())
        if vk is not None:
            self._user32.PostMessageW(hwnd, _WM_KEYUP, vk, _KEYUP_LPARAM)
        else:
            pyautogui.keyUp(key)

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
        hwnd = self._get_hwnd()
        try:
            if hwnd is not None:
                self._key_down_window(key, hwnd)
            else:
                pyautogui.keyDown(key)
            self._held_keys.add(key)
            return True
        except Exception as e:
            self._log.error(f"按键按下异常 key={key!r}: {e}")
            if hwnd is not None:
                self._fallback_global(f"窗口模式按键按下失败: {e}")
            return False

    def release_key(self, key: str) -> None:
        """
        释放一个键。无论是否在 _held_keys 中都尝试释放 (容错)。
        不会抛异常，用于 stop / focus-loss 安全释放。
        """
        hwnd = self._get_hwnd()
        try:
            if hwnd is not None:
                self._key_up_window(key, hwnd)
            else:
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
        hwnd = self._get_hwnd()
        for key in keys:
            try:
                if hwnd is not None:
                    self._key_up_window(key, hwnd)
                else:
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
        hwnd = self._get_hwnd()
        try:
            if hwnd is not None:
                self._key_down_window(key, hwnd)
            else:
                pyautogui.keyDown(key)
            self._held_keys.add(key)
            # 短暂停顿 (不可中断，但 duration 很短)
            import time as _t
            _t.sleep(max(0.0, min(duration_s, 0.2)))
            if hwnd is not None:
                self._key_up_window(key, hwnd)
            else:
                pyautogui.keyUp(key)
            self._held_keys.discard(key)
            return True
        except Exception as e:
            self._log.error(f"按键点击异常 key={key!r}: {e}")
            if hwnd is not None:
                self._fallback_global(f"窗口模式按键点击失败: {e}")
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
