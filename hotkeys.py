"""
hotkeys.py
----------
全局快捷键管理模块。

使用 keyboard 库注册系统级快捷键，即使 Minecraft 在前台也能响应。

快捷键：
- F6: 开始 / 暂停自动钓鱼
- F7: 立即停止 (最高优先级)
- F8: 重新选择检测区域
- F9: 测试当前检测区域
- F10: 截取「咬钩！」模板
- F11: 取色器 (放大镜选取目标像素颜色)

注意：keyboard 库的回调在监听线程中执行，
因此回调中应尽量只做状态切换，耗时/UI 操作交给主线程。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import keyboard

from config import HotkeyConfig
from logger import get_logger


class HotkeyManager:
    """全局快捷键管理器"""

    def __init__(self, config: HotkeyConfig) -> None:
        self._cfg = config
        self._log = get_logger()
        self._registered = False

        # 回调函数 (由外部设置)
        self.on_toggle: Optional[Callable[[], None]] = None
        self.on_stop: Optional[Callable[[], None]] = None
        self.on_select_region: Optional[Callable[[], None]] = None
        self.on_test: Optional[Callable[[], None]] = None
        self.on_capture_template: Optional[Callable[[], None]] = None
        self.on_pick_color: Optional[Callable[[], None]] = None

    def register(self) -> bool:
        """注册所有全局快捷键。失败返回 False。"""
        if self._registered:
            return True

        try:
            keyboard.add_hotkey(self._cfg.toggle, self._safe(self._on_toggle))
            keyboard.add_hotkey(self._cfg.stop, self._safe(self._on_stop))
            keyboard.add_hotkey(self._cfg.select_region, self._safe(self._on_select_region))
            keyboard.add_hotkey(self._cfg.test, self._safe(self._on_test))
            keyboard.add_hotkey(self._cfg.capture_template, self._safe(self._on_capture_template))
            keyboard.add_hotkey(self._cfg.pick_color, self._safe(self._on_pick_color))
            self._registered = True
            self._log.info(
                f"快捷键已注册: "
                f"F6(开始/暂停) F7(停止) F8(选区域) F9(测试) F10(截模板) F11(取色)"
            )
            return True
        except Exception as e:
            self._log.error(f"快捷键注册失败: {e}")
            self._log.error(
                "可能原因: 未以管理员权限运行，或快捷键被其他程序占用。"
                "请尝试以管理员身份运行本程序。"
            )
            return False

    def unregister(self) -> None:
        """注销所有快捷键"""
        if not self._registered:
            return
        try:
            keyboard.clear_all_hotkeys()
        except Exception:
            pass
        self._registered = False
        self._log.info("快捷键已注销")

    def _safe(self, cb: Callable[[], None]) -> Callable[[], None]:
        """包装回调，捕获异常避免监听线程崩溃"""
        def wrapper():
            try:
                cb()
            except Exception as e:
                self._log.error(f"快捷键回调异常: {e}")
        return wrapper

    def _on_toggle(self) -> None:
        if self.on_toggle:
            self.on_toggle()

    def _on_stop(self) -> None:
        if self.on_stop:
            self.on_stop()

    def _on_select_region(self) -> None:
        if self.on_select_region:
            self.on_select_region()

    def _on_test(self) -> None:
        if self.on_test:
            self.on_test()

    def _on_capture_template(self) -> None:
        if self.on_capture_template:
            self.on_capture_template()

    def _on_pick_color(self) -> None:
        if self.on_pick_color:
            self.on_pick_color()
