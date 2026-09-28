"""navigation/movement.py
移动规划层 (阶段六)

职责：
- 把路径节点 / 方向指令转成按键序列 (W/A/S/D + 鼠标视角旋转)
- 提供可中断的按住移动 (hold_key)：按住某键 duration 秒，期间检查 stop/pause
- 所有动作可被 F7 stop_event / F6 pause_event 立即中断，中断时释放按键
- 禁止在本层直接调 pyautogui，所有输入通过 InputController (分层约束)

架构：
    Pathfinding (纯数据寻路) → MovementPlanner (路径→按键动作) → InputController (底层输入)
    Navigator (编排) 调用 MovementPlanner 执行移动

由于 Minecraft 中玩家世界坐标→按键映射需要朝向信息，本层提供：
- hold_key(key, duration): 可中断按住一个键
- move_forward(duration) / move_backward / strafe_left / strafe_right
- turn(dx_pixels): 视角旋转 (鼠标相对移动)
- jump(): 单次跳跃
- stop(): 立即释放所有按键 (安全)
- execute_path(path): 接受 PathNode 列表，逐段执行 (简化：节点 delta→WASD)

真实地形/坐标接入后可扩展 execute_path 的映射策略。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from input_controller import InputController
from logger import get_logger


class MoveActionType(Enum):
    """移动动作类型"""
    HOLD_KEY = "hold_key"       # 按住某键一段时间
    TAP_KEY = "tap_key"         # 单次点击按键
    TURN = "turn"               # 视角旋转 (鼠标相对移动)
    STOP = "stop"               # 释放所有按键


@dataclass
class MoveAction:
    """一个移动动作"""
    type: MoveActionType
    key: str = ""               # HOLD_KEY/TAP_KEY 的键名
    duration_s: float = 0.0     # HOLD_KEY 持续时间
    dx: int = 0                 # TURN 的水平位移
    dy: int = 0                 # TURN 的垂直位移
    desc: str = ""              # 描述 (日志用)


class MovementPlanner:
    """
    移动规划器：把路径/方向指令转成可中断的按键序列。
    所有 hold 类动作在执行期间检查 stop/pause 事件，可立即中断并释放按键。
    """

    def __init__(
        self,
        input_ctrl: InputController,
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        self._input = input_ctrl
        self._stop = stop_event
        self._pause = pause_event
        self._log = get_logger()

    # ---------------- 基础可中断按住 ----------------

    def hold_key(self, key: str, duration_s: float) -> bool:
        """
        按住 key 持续 duration_s 秒，期间检查 stop/pause。
        中断或暂停时立即释放按键并返回 False。
        duration_s <= 0 时不执行。

        Returns:
            是否完整执行 (未被打断)
        """
        if duration_s <= 0:
            return True
        if self._stop.is_set() or self._pause.is_set():
            return False
        if not self._input.press_key(key):
            return False
        # 可中断等待
        ok = self._interruptible_wait(duration_s)
        # 无论是否完整执行都释放
        self._input.release_key(key)
        return ok

    def _interruptible_wait(self, seconds: float) -> bool:
        """
        可中断等待。stop/pause 时立即返回 False，让调用方释放按键。
        暂停时立即返回 False (而非阻塞)，确保移动按键被释放 (安全)。
        导航会在暂停时中止并回退到 WAITING_BITE，恢复后从钓鱼状态继续。
        """
        end = time.monotonic() + seconds
        while True:
            if self._stop.is_set() or self._pause.is_set():
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.05))

    # ---------------- 高级移动动作 ----------------

    def move_forward(self, duration_s: float) -> bool:
        """按住 W 向前移动"""
        return self.hold_key("w", duration_s)

    def move_backward(self, duration_s: float) -> bool:
        """按住 S 向后移动"""
        return self.hold_key("s", duration_s)

    def strafe_left(self, duration_s: float) -> bool:
        """按住 A 向左平移"""
        return self.hold_key("a", duration_s)

    def strafe_right(self, duration_s: float) -> bool:
        """按住 D 向右平移"""
        return self.hold_key("d", duration_s)

    def jump(self) -> bool:
        """单次跳跃 (space)"""
        if self._stop.is_set() or self._pause.is_set():
            return False
        return self._input.jump()

    def jump_while_forward(self, duration_s: float) -> bool:
        """跳跃同时向前移动 (跨越障碍/上坡)。先按 W 再按 space。"""
        if duration_s <= 0:
            return True
        if self._stop.is_set() or self._pause.is_set():
            return False
        if not self._input.press_key("w"):
            return False
        # 起步后跳跃
        time.sleep(0.1)
        if not self._stop.is_set() and not self._pause.is_set():
            self._input.jump()
        ok = self._interruptible_wait(duration_s)
        self._input.release_key("w")
        return ok

    def turn(self, dx_pixels: int, dy_pixels: int = 0) -> bool:
        """
        视角旋转 (鼠标相对移动)。
        正 dx=向右转，负 dx=向左转。

        Returns:
            是否成功执行 (stop/pause 时返回 False)
        """
        if dx_pixels == 0 and dy_pixels == 0:
            return True
        if self._stop.is_set() or self._pause.is_set():
            return False
        return self._input.move_mouse_rel(dx_pixels, dy_pixels)

    def stop(self) -> None:
        """立即释放所有按键 (安全停止)"""
        self._input.release_all_keys()

    # ---------------- 路径执行 ----------------

    def execute_actions(self, actions: List[MoveAction]) -> bool:
        """
        顺序执行一组移动动作。每个动作之间检查 stop/pause。
        任意动作被中断则立即停止并返回 False。

        Returns:
            是否完整执行全部动作
        """
        for act in actions:
            if self._stop.is_set() or self._pause.is_set():
                return False
            if act.desc:
                self._log.info(f"移动: {act.desc}")
            if act.type == MoveActionType.HOLD_KEY:
                if not self.hold_key(act.key, act.duration_s):
                    return False
            elif act.type == MoveActionType.TAP_KEY:
                if self._stop.is_set():
                    return False
                self._input.tap_key(act.key, act.duration_s if act.duration_s > 0 else 0.05)
            elif act.type == MoveActionType.TURN:
                if not self.turn(act.dx, act.dy):
                    return False
            elif act.type == MoveActionType.STOP:
                self.stop()
            # 动作间小间隔，避免按键冲突
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return True

    def execute_path(
        self, path: List, step_duration_s: float = 0.5,
    ) -> bool:
        """
        执行路径 (PathNode 列表)。
        简化策略：相邻节点 delta → W/A/S/D 按键。
        dx>0 → D (东), dx<0 → A (西)
        dz>0 → S (南), dz<0 → W (北)
        height_diff>0 → 跳跃 (jump_while_forward)
        注意：此映射假设玩家朝向正北 (z 负方向)，真实场景需 Navigator
        先用 turn 对齐朝向。真实地形接入后可扩展。

        Args:
            path: PathNode 列表 (含 x/z 坐标)
            step_duration_s: 每步按住时间

        Returns:
            是否完整执行
        """
        if len(path) < 2:
            return True
        actions: List[MoveAction] = []
        prev = path[0]
        for node in path[1:]:
            dx = getattr(node, "x", 0) - getattr(prev, "x", 0)
            dz = getattr(node, "z", 0) - getattr(prev, "z", 0)
            height_diff = 0
            if hasattr(node, "y") and hasattr(prev, "y"):
                height_diff = (node.y or 0) - (prev.y or 0)
            # 简化映射：主方向按键
            if dz < 0:
                key = "w"
                desc = f"向北 W ({dx},{dz})"
            elif dz > 0:
                key = "s"
                desc = f"向南 S ({dx},{dz})"
            elif dx > 0:
                key = "d"
                desc = f"向东 D ({dx},{dz})"
            elif dx < 0:
                key = "a"
                desc = f"向西 A ({dx},{dz})"
            else:
                prev = node
                continue
            if height_diff > 0:
                actions.append(MoveAction(
                    MoveActionType.HOLD_KEY, key=key,
                    duration_s=step_duration_s, desc=f"跳跃+{desc}",
                ))
                # 标记需要跳跃：用 TAP_KEY space 前置
                actions.insert(len(actions) - 1, MoveAction(
                    MoveActionType.TAP_KEY, key="space",
                    duration_s=0.05, desc="跳跃",
                ))
            else:
                actions.append(MoveAction(
                    MoveActionType.HOLD_KEY, key=key,
                    duration_s=step_duration_s, desc=desc,
                ))
            prev = node
        return self.execute_actions(actions)
