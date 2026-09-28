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

import math
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

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
        nav_cfg=None,
    ) -> None:
        self._input = input_ctrl
        self._stop = stop_event
        self._pause = pause_event
        self._nav_cfg = nav_cfg
        self._log = get_logger()
        self._last_strafe_key: str = ""
        self._evasion_count: int = 0

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

    # ---------------- 移植 Tau-main：底层按键/鼠标 ----------------

    def key_down(self, key: str) -> bool:
        """按住键 (不自动释放)，用于 T/I 循环持续移动"""
        if self._stop.is_set() or self._pause.is_set():
            return False
        return self._input.press_key(key)

    def key_up(self, key: str) -> None:
        """释放键"""
        self._input.release_key(key)

    def mouse_move_stepped(self, dx: int, dy: int) -> bool:
        """
        步进式鼠标移动 (移植 Tau _mouse_move)。
        Minecraft(GLFW) 依赖真实光标位置轮询计算视角，PostMessage 的
        WM_MOUSEMOVE 无法驱动视角，因此使用全局 mouse_event 步进移动。
        每步 mouse_move_step 像素，间隔 mouse_move_delay 秒，可被 stop 中断。
        """
        cfg = self._nav_cfg
        mult = getattr(cfg, "mouse_move_multiplier", 1.0) if cfg else 1.0
        step = getattr(cfg, "mouse_move_step", 20) if cfg else 20
        delay = getattr(cfg, "mouse_move_delay", 0.005) if cfg else 0.005
        dx = int(dx * mult)
        dy = int(dy * mult)
        if dx == 0 and dy == 0:
            return True
        if self._stop.is_set() or self._pause.is_set():
            return False
        try:
            import ctypes
            MOUSEEVENTF_MOVE = 0x0001
            rem_x = abs(dx)
            sign_x = 1 if dx > 0 else -1
            while rem_x > 0:
                if self._stop.is_set() or self._pause.is_set():
                    return False
                cur = min(step, rem_x)
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_MOVE, int(cur * sign_x), 0, 0, 0)
                rem_x -= cur
                time.sleep(delay)
            rem_y = abs(dy)
            sign_y = 1 if dy > 0 else -1
            while rem_y > 0:
                if self._stop.is_set() or self._pause.is_set():
                    return False
                cur = min(step, rem_y)
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_MOVE, 0, int(cur * sign_y), 0, 0)
                rem_y -= cur
                time.sleep(delay)
            return True
        except Exception as e:
            self._log.error(f"步进鼠标移动失败: {e}")
            # 回退到 InputController 的 move_mouse_rel
            return self._input.move_mouse_rel(dx, dy)

    # ---------------- 移植 Tau-main：角度计算与旋转 ----------------

    def deg_per_pixel(self) -> float:
        """计算每像素对应的角度 (移植 Tau _get_deg_per_pixel)"""
        cfg = self._nav_cfg
        if cfg is None:
            return 0.15
        override = getattr(cfg, "deg_per_pixel_override", 0.0)
        if override and override != 0:
            return float(override)
        dpi = float(getattr(cfg, "dpi", 1320.0))
        sens = float(getattr(cfg, "sensitivity", 95.0))
        factor = float(getattr(cfg, "deg_per_pixel_factor", 0.15))
        return factor * (dpi / 800.0) * (sens / 100.0)

    @staticmethod
    def calc_target_angles(
        cx: float, cy: float, cz: float,
        tx: float, ty: float, tz: float,
        eye_height: float = 1.62,
    ) -> Tuple[float, float]:
        """
        计算从当前位置到目标位置的目标 yaw/pitch (移植 Tau _calc_target_angles)。
        Minecraft yaw: 南=0, 西=90, 北=±180, 东=-90。
        返回 (yaw, pitch)，yaw 范围 (-180, 180]。
        """
        dx, dy, dz = tx - cx, ty - (cy + eye_height), tz - cz
        yaw = math.degrees(-math.atan2(dx, dz)) % 360.0
        yaw = ((yaw + 180) % 360) - 180
        hor = math.hypot(dx, dz)
        pitch = math.degrees(-math.atan2(dy, hor)) if hor > 0 else 0.0
        return yaw, pitch

    def rotate_to_angle(
        self,
        target_yaw: float, target_pitch: float,
        cur_yaw: float, cur_pitch: float,
        tolerance: Optional[float] = None,
        get_coords=None,
    ) -> bool:
        """
        旋转视角到目标角度 (移植 Tau _rotate_to_angle)。
        通过 deg_per_pixel 把角度差转为像素移动，步进式鼠标执行。
        支持末次验证：若提供 get_coords 回调，旋转后重新读取朝向并补正。

        Args:
            target_yaw, target_pitch: 目标角度
            cur_yaw, cur_pitch: 当前角度
            tolerance: 容差 (度)，None 用配置默认值
            get_coords: 可选回调 () -> (x,y,z,yaw,pitch)，用于末次验证
        """
        cfg = self._nav_cfg
        if tolerance is None:
            tolerance = getattr(cfg, "angle_tolerance", 1.0) if cfg else 1.0
        max_attempts = getattr(cfg, "max_rotation_attempts", 3) if cfg else 3
        retry_delay = getattr(cfg, "rotation_retry_delay", 0.05) if cfg else 0.05

        total_dyaw = ((target_yaw - cur_yaw + 180) % 360) - 180
        total_dpitch = target_pitch - cur_pitch
        moved_yaw = 0.0
        moved_pitch = 0.0
        moved = False

        for attempt in range(max_attempts):
            if self._stop.is_set() or self._pause.is_set():
                return False
            dyaw = total_dyaw - moved_yaw
            dpitch = total_dpitch - moved_pitch
            if abs(dyaw) <= tolerance and abs(dpitch) <= tolerance:
                break
            deg_px = self.deg_per_pixel()
            if deg_px <= 0:
                break
            mdx = int(dyaw / deg_px)
            mdy = int(dpitch / deg_px)
            if mdx == 0 and mdy == 0:
                break
            moved = True
            self.mouse_move_stepped(mdx, mdy)
            moved_yaw += mdx * deg_px
            moved_pitch += mdy * deg_px
            if attempt < max_attempts - 1:
                time.sleep(retry_delay)

        if not moved:
            return True

        # 末次验证：若有坐标回调，重新读取朝向并补正
        if get_coords is not None:
            try:
                coords = get_coords()
                if coords and len(coords) >= 5:
                    _, _, _, c_yaw, c_pitch = coords[:5]
                    dyaw = ((target_yaw - c_yaw + 180) % 360) - 180
                    dpitch = target_pitch - c_pitch
                    if abs(dyaw) > tolerance or abs(dpitch) > tolerance:
                        deg_px = self.deg_per_pixel()
                        self.mouse_move_stepped(int(dyaw / deg_px), int(dpitch / deg_px))
            except Exception:
                pass
        return True

    # ---------------- 移植 Tau-main：卡死避障 ----------------

    def stuck_evasion(self) -> bool:
        """
        卡死避障 (移植 Tau _execute_stuck_evasion)。
        策略：后退 evasion_back_time 秒 → 随机侧移 a/d。
        侧移分短/长两种，每 evasion_cycle_interval 次强制长侧移，避免原地打转。
        """
        cfg = self._nav_cfg
        back_time = getattr(cfg, "evasion_back_time", 1.0) if cfg else 1.0
        short_max = getattr(cfg, "evasion_short_max", 3.0) if cfg else 3.0
        long_min = getattr(cfg, "evasion_long_min", 3.0) if cfg else 3.0
        long_max = getattr(cfg, "evasion_long_max", 5.0) if cfg else 5.0
        short_prob = getattr(cfg, "evasion_short_probability", 0.8) if cfg else 0.8
        cycle = getattr(cfg, "evasion_cycle_interval", 3) if cfg else 3

        self._log.info("卡点检测，执行避障：后退 + 随机侧移")
        # 后退
        if not self.key_down("s"):
            return False
        try:
            if not self._interruptible_wait(back_time):
                return False
        finally:
            self.key_up("s")
        if self._stop.is_set() or self._pause.is_set():
            return False
        time.sleep(0.3)

        # 随机侧移 (避免连续同侧)
        choices = ["a", "d"]
        if self._last_strafe_key in choices:
            choices.remove(self._last_strafe_key)
        move_key = random.choice(choices)
        self._evasion_count += 1
        if self._evasion_count % cycle == 0:
            strafe_time = random.uniform(long_min, long_max)
            self._log.info(f"强制长侧移 (每{cycle}次): {move_key} {strafe_time:.2f}s")
        else:
            if random.random() < short_prob:
                strafe_time = random.uniform(0.0, short_max)
            else:
                strafe_time = random.uniform(long_min, long_max)
        self._log.info(f"侧移方向: {move_key}, 时长: {strafe_time:.2f}s")
        if not self.key_down(move_key):
            return False
        try:
            if not self._interruptible_wait(strafe_time):
                return False
        finally:
            self.key_up(move_key)
        self._last_strafe_key = move_key
        return True

    # ---------------- 移植 Tau-main：水上浮 ----------------

    def float_to_surface(
        self,
        get_coords,
        target_yaw: Optional[float] = None,
        sea_level_y: Optional[float] = None,
    ) -> bool:
        """
        水中上浮到水面 (移植 Tau _float_to_surface，简化版)。
        按 W+Space 持续上浮，定期检查 Y 是否达到海平面。
        包含卡死检测：xz 连续不变触发避障。

        Args:
            get_coords: 回调 () -> (x,y,z,yaw,pitch)
            target_yaw: 上浮时朝向 (None 保持当前)
            sea_level_y: 海平面 Y (None 用配置默认)
        """
        cfg = self._nav_cfg
        timeout = getattr(cfg, "water_float_timeout", 2.0) if cfg else 2.0
        pitch_angle = getattr(cfg, "float_pitch_angle", 45.0) if cfg else 45.0
        check_interval = getattr(cfg, "float_check_interval", 0.3) if cfg else 0.3
        stuck_threshold = getattr(cfg, "stuck_threshold", 0.15) if cfg else 0.15
        stuck_trigger = getattr(cfg, "stuck_trigger_count", 2) if cfg else 2
        water_th = sea_level_y if sea_level_y is not None else (
            getattr(cfg, "water_jump_threshold", 63.0) if cfg else 63.0
        )

        coords = get_coords()
        if not coords:
            return False
        init_pitch = coords[4]
        cyaw = coords[3]
        # 上抬视角
        target_pitch = max(-90, init_pitch - pitch_angle)
        dpitch = target_pitch - init_pitch
        deg_px = self.deg_per_pixel()
        if deg_px > 0:
            dy_px = int(dpitch / deg_px)
            if dy_px != 0:
                self.mouse_move_stepped(0, dy_px)

        self.key_down("w")
        self.key_down("space")
        t0 = time.monotonic()
        stuck_count = 0
        last_coord = None
        try:
            while not self._stop.is_set() and not self._pause.is_set():
                coords = get_coords()
                if not coords:
                    time.sleep(check_interval)
                    continue
                cx, cy, cz = coords[0], coords[1], coords[2]
                # 卡死检测
                if last_coord is not None:
                    d = math.hypot(cx - last_coord[0], cz - last_coord[1])
                    if d < stuck_threshold:
                        stuck_count += 1
                        if stuck_count >= stuck_trigger:
                            self._log.info("上浮位置不变，触发避障")
                            self.stuck_evasion()
                            return False
                    else:
                        stuck_count = 0
                last_coord = (cx, cz)
                if cy >= water_th:
                    self._log.info("已浮出水面")
                    return True
                # 朝向目标
                if target_yaw is not None:
                    coords2 = get_coords()
                    if coords2:
                        self.rotate_to_angle(target_yaw, coords2[4], coords2[3], coords2[4],
                                              tolerance=5.0)
                if time.monotonic() - t0 > timeout:
                    self._log.warn("上浮超时")
                    return False
                time.sleep(check_interval)
            return False
        finally:
            self.key_up("space")
            self.key_up("w")
            # 恢复视角
            coords = get_coords()
            if coords:
                cur_pitch = coords[4]
                restore_dp = init_pitch - cur_pitch
                deg_px = self.deg_per_pixel()
                if deg_px > 0:
                    restore_dy = int(restore_dp / deg_px)
                    if restore_dy != 0:
                        self.mouse_move_stepped(0, restore_dy)

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
