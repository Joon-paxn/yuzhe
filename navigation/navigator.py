"""navigation/navigator.py
自动寻找新钓点编排 (阶段八)

职责：
- DEPLETED 时触发：探索附近 → 发现水域 (vision) 生成候选钓点 → 寻路/移动 → 到达 → 交接回钓鱼
- 结合 WorldMap (已知钓点/去重) + VisionDetector (水域视觉识别) + MovementPlanner (移动)
- 不直接调 pyautogui，通过 MovementPlanner + InputController 分层

状态流转 (由 FishingEngine 状态机驱动)：
    SEARCHING  - 旋转视角扫描水域 (vision.detect)，找到候选水域
    PLANNING   - 生成到水域的路径 (简化：方向按键序列，A* 网格在无真实坐标时退化为方向移动)
    MOVING     - 执行移动 (向前走 + 朝向水域旋转)
    ARRIVED    - 到达水域 (水域占画面 >= arrival_water_area_ratio)，添加钓点到 WorldMap

简化策略 (无精确世界坐标时)：
- SEARCHING：原地旋转视角 (move_mouse_rel)，每步跑 vision.detect 找水域连通域
- 找到水域后，计算水域中心相对屏幕中心的水平偏移 → 转视角对齐
- MOVING：向前走 move_step_s，定期重新检测水域，调整朝向
- 水域足够大 (占画面 >= arrival_water_area_ratio) → ARRIVED

真实坐标 (OCR XYZ + 视觉深度估计) 接入后可升级为 A* 真实寻路。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from config import AppConfig
from input_controller import InputController
from logger import get_logger
from minecraft_window import MinecraftWindow
from navigation.coordinate_provider import CoordinateProvider
from navigation.map import PlayerPosition, WorldMap
from navigation.movement import MovementPlanner
from ocr import OcrService
from vision import VisionDetector
from vision.models import VisionResult, WaterRegion


class NavigatorState:
    """导航子状态 (供 GUI 显示)"""
    SEARCHING = "探索水域"
    PLANNING = "规划路径"
    MOVING = "移动中"
    ARRIVED = "到达钓点"
    FAILED = "寻路失败"


@dataclass
class NavigationOutcome:
    """一次寻路结果"""
    success: bool
    spot_id: Optional[str] = None     # 新钓点 ID (成功时)
    reason: str = ""                  # 失败原因
    state: str = NavigatorState.FAILED


class Navigator:
    """自动寻找新钓点编排器"""

    def __init__(
        self,
        config: AppConfig,
        mc_window: MinecraftWindow,
        input_ctrl: InputController,
        movement: MovementPlanner,
        vision: Optional[VisionDetector] = None,
        world_map: Optional[WorldMap] = None,
        ocr_service: Optional[OcrService] = None,
    ) -> None:
        self._cfg = config
        self._mc = mc_window
        self._input = input_ctrl
        self._move = movement
        self._vision = vision
        self._map = world_map
        self._ocr = ocr_service
        self._log = get_logger()

        self._stop_event = movement._stop    # 共享 FishingEngine 的 stop_event
        self._pause_event = movement._pause
        self._current_state = NavigatorState.SEARCHING
        # 借鉴 Tau-main：是否用 PrintWindow 后台抓图 (由 main.py 设置)
        self._grab_from_window: bool = False
        # 移植 Tau-main：坐标提供者 (OCR 读 F3，替代 F3+C)
        self._coords = CoordinateProvider(ocr_service)
        # 卡死状态跨循环保留
        self._stuck_count: int = 0
        self._last_stuck_coord: Optional[Tuple[float, float]] = None

    @property
    def state(self) -> str:
        return self._current_state

    # ---------------- 截图辅助 ----------------

    def _capture_frame(self) -> Optional[np.ndarray]:
        """截取 Minecraft 窗口画面 (借鉴 Tau-main：优先 PrintWindow 后台抓图)"""
        if not self._mc.is_valid():
            return None
        rect = self._mc.get_rect()
        if rect is None:
            return None
        try:
            import screen_capture as _sc
            frame = _sc.capture_window(self._mc, use_print_window=self._grab_from_window)
            if frame is not None:
                return frame
            # 回退 mss
            import mss
            sct = mss.mss()
            monitor = {"left": rect.left, "top": rect.top,
                       "width": rect.width, "height": rect.height}
            shot = sct.grab(monitor)
            sct.close()
            return np.array(shot)[:, :, :3]
        except Exception as e:
            self._log.error(f"导航截图失败: {e}")
            return None

    def _detect_vision(self) -> Optional[VisionResult]:
        """运行视觉识别"""
        if self._vision is None:
            return None
        frame = self._capture_frame()
        if frame is None:
            return None
        try:
            return self._vision.detect(frame)
        except Exception as e:
            self._log.error(f"视觉识别失败: {e}")
            return None

    def _interrupted(self) -> bool:
        """是否被停止/暂停打断"""
        return self._stop_event.is_set() or self._pause_event.is_set()

    # ---------------- 主入口 ----------------

    def find_new_spot(self) -> NavigationOutcome:
        """
        寻找新钓点主流程 (借鉴 Tau-main 的预录钓点 + 坐标导航)。

        策略优先级：
        1. 坐标导航：若 OCR 能读到坐标且地图有已录钓点，排除当前枯竭点附近
           (min_distance) 的钓点后，导航到最近的可用钓点 (T/I 循环)。
        2. 视觉兜底：无坐标/无已录钓点时，旋转扫描水域 + 朝水移动。

        被 FishingEngine 在 DEPLETED 后调用。
        任何阶段被打断 (F7/失焦) 返回 success=False。
        """
        nav_cfg = self._cfg.navigation
        if not nav_cfg.enabled:
            return NavigationOutcome(
                success=False,
                reason="navigation.enabled=false，自动寻路未启用",
                state=NavigatorState.FAILED,
            )
        if self._map is None:
            return NavigationOutcome(
                success=False, reason="世界地图未初始化",
                state=NavigatorState.FAILED,
            )

        self._log.info("===== 开始自动寻找新钓点 =====")
        self._move.stop()  # 清理可能残留的按键

        # 标记当前钓点为枯竭 (若有)
        cur = self._map.get_current()
        if cur is not None:
            self._map.mark_depleted(cur.id)
            self._log.info(f"标记当前钓点 {cur.id} 为枯竭")

        # ---- 策略 1：坐标导航到已录钓点 (Tau 方式) ----
        coords = self._get_current_coords()
        if coords is not None:
            cx, cy, cz = coords[0], coords[1], coords[2]
            excl_dist = getattr(nav_cfg, "stuck_threshold", 0.15)
            # 排除距离用 5.0 格 (Tau exclude_spot_distance)
            min_dist = 5.0
            target = self._map.find_nearest_available(cx, cz, min_distance=min_dist)
            if target is not None:
                self._log.info(
                    f"坐标导航到钓点 ({target.x:.1f}, {target.y:.1f}, {target.z:.1f}) "
                    f"距离={target.distance_xz_to(cx, cz):.1f}格"
                )
                self._map.set_current(target.id)
                outcome = self.navigate_to_coords(target.x, target.y, target.z)
                if outcome.success:
                    self._map.mark_used(target.id)
                    self._log.success(f"坐标导航到达钓点 {target.id}")
                    return outcome
                self._log.warn(f"坐标导航失败: {outcome.reason}，回退视觉搜索")
            else:
                self._log.info("无可用已录钓点 (都在 5 格内或已枯竭)，回退视觉搜索")
        else:
            self._log.info("OCR 坐标不可用，回退视觉搜索")

        # ---- 策略 2：视觉搜索兜底 ----
        if self._vision is None:
            return NavigationOutcome(
                success=False,
                reason="无可用钓点且视觉识别层未初始化",
                state=NavigatorState.FAILED,
            )

        # 离开当前枯竭钓点：后退 + 转向，避免原地看到水就判定到达
        self._current_state = NavigatorState.MOVING
        retreat_s = max(1.5, nav_cfg.move_step_s * 3)
        self._log.info(f"离开枯竭钓点：后退 {retreat_s:.1f}s")
        if not self._move.move_backward(retreat_s):
            return NavigationOutcome(success=False, reason="后退被打断", state=self._current_state)
        time.sleep(0.2)
        self._move.turn(nav_cfg.turn_max_px // 4, 0)
        time.sleep(0.3)

        # SEARCHING: 旋转扫描水域
        water = self._search_water()
        if self._interrupted():
            self._move.stop()
            return NavigationOutcome(success=False, reason="被打断", state=self._current_state)
        if water is None:
            self._log.warn("未找到水域，寻路失败")
            self._current_state = NavigatorState.FAILED
            return NavigationOutcome(success=False, reason="未发现水域", state=self._current_state)

        self._log.info(f"发现水域：{water.area}px 中心=({water.cx:.0f},{water.cy:.0f})")

        # PLANNING + MOVING: 朝水域移动
        arrived = self._approach_water(water)
        if self._interrupted():
            self._move.stop()
            return NavigationOutcome(success=False, reason="被打断", state=self._current_state)
        if not arrived:
            self._log.warn("移动阶段未能到达水域")
            self._current_state = NavigatorState.FAILED
            return NavigationOutcome(success=False, reason="移动未到达水域", state=self._current_state)

        # ARRIVED: 添加钓点到地图
        self._current_state = NavigatorState.ARRIVED
        spot_id = self._register_arrived_spot()
        self._log.success(f"到达新钓点 spot_id={spot_id}")
        return NavigationOutcome(success=True, spot_id=spot_id, state=NavigatorState.ARRIVED)

    # ---------------- SEARCHING ----------------

    def _search_water(self) -> Optional[WaterRegion]:
        """
        旋转视角扫描水域。每步转 turn_step_px，最多 search_max_turns 步。
        返回最大的水域连通域 (置信度 >= arrival_min_confidence)。
        """
        self._current_state = NavigatorState.SEARCHING
        nav_cfg = self._cfg.navigation
        best: Optional[WaterRegion] = None
        for i in range(nav_cfg.search_max_turns):
            if self._interrupted():
                return None
            result = self._detect_vision()
            if result is not None and result.water:
                # 选最大的水域连通域
                cand = max(result.water, key=lambda w: w.area)
                if cand.confidence >= nav_cfg.arrival_min_confidence:
                    self._log.info(
                        f"扫描第 {i+1} 步发现水域: area={cand.area} conf={cand.confidence:.2f}"
                    )
                    if best is None or cand.area > best.area:
                        best = cand
                    # 找到足够大的水域即可停止扫描
                    frame_area = result.frame_size[0] * result.frame_size[1]
                    if cand.area / max(1, frame_area) >= nav_cfg.arrival_water_area_ratio * 0.5:
                        return best
            # 旋转一步
            self._move.turn(nav_cfg.turn_step_px, 0)
            time.sleep(0.15)
        return best

    # ---------------- PLANNING + MOVING ----------------

    def _approach_water(self, initial_water: WaterRegion) -> bool:
        """
        朝水域移动。每步：检测水域 → 旋转对齐 → 向前走。
        水域占画面 >= arrival_water_area_ratio 视为到达。
        卡死检测 (借鉴 Tau-main)：水域占比连续 N 步无明显增长 → 触发避障
        (后退 + 转向)，避免卡墙。
        """
        self._current_state = NavigatorState.PLANNING
        nav_cfg = self._cfg.navigation
        step_s = nav_cfg.move_step_s
        last_ratio: float = 0.0
        stuck_count: int = 0
        min_arrival_steps = 3  # 至少前进 3 步才允许判定到达 (确保离开枯竭钓点)
        moved_steps: int = 0
        for step in range(nav_cfg.search_max_steps):
            if self._interrupted():
                return False
            result = self._detect_vision()
            if result is None or not result.water:
                # 暂时丢失水域，向前走一步试探
                self._current_state = NavigatorState.MOVING
                self._log.info(f"步骤 {step+1}: 水域丢失，向前试探")
                if not self._move.move_forward(step_s):
                    return False
                moved_steps += 1
                # 丢失水域也可能是卡死，累计
                stuck_count += 1
                if stuck_count >= nav_cfg.stuck_trigger_count:
                    if not self._evade():
                        return False
                    stuck_count = 0
                continue
            # 选最大水域
            water = max(result.water, key=lambda w: w.area)
            frame_w, frame_h = result.frame_size
            frame_area = frame_w * frame_h
            ratio = water.area / max(1, frame_area)
            # 到达判定：必须至少前进 min_arrival_steps 步，避免原地看到水就判定到达
            if moved_steps >= min_arrival_steps and ratio >= nav_cfg.arrival_water_area_ratio and \
               water.confidence >= nav_cfg.arrival_min_confidence:
                self._log.info(f"步骤 {step+1}: 水域占比 {ratio:.2f} 达到阈值，已到达")
                return True
            # 卡死检测：水域占比无明显增长 → 可能撞墙
            ratio_change = abs(ratio - last_ratio)
            if ratio_change < nav_cfg.stuck_ratio_threshold:
                stuck_count += 1
                self._log.info(
                    f"步骤 {step+1}: 卡死计数 {stuck_count}/{nav_cfg.stuck_trigger_count} "
                    f"(水域占比变化 {ratio_change:.3f} < {nav_cfg.stuck_ratio_threshold})"
                )
            else:
                stuck_count = 0
            last_ratio = ratio
            if stuck_count >= nav_cfg.stuck_trigger_count:
                self._log.warn("连续未接近水域，触发避障")
                if not self._evade():
                    return False
                stuck_count = 0
                continue
            # 旋转对齐：水域中心相对屏幕中心的水平偏移
            screen_cx = frame_w / 2.0
            offset_x = water.cx - screen_cx
            turn_dx = int((offset_x / max(1, frame_w)) * nav_cfg.turn_max_px)
            turn_dx = max(-nav_cfg.turn_step_px, min(nav_cfg.turn_step_px, turn_dx))
            self._current_state = NavigatorState.MOVING
            self._log.info(
                f"步骤 {step+1}: 水域占比={ratio:.2f} 偏移={offset_x:.0f} 转向dx={turn_dx}"
            )
            if turn_dx != 0:
                self._move.turn(turn_dx, 0)
                if self._interrupted():
                    return False
                time.sleep(0.1)
            # 向前走
            if not self._move.move_forward(step_s):
                return False
            moved_steps += 1
        return False

    def _evade(self) -> bool:
        """
        卡死避障 (借鉴 Tau-main)：后退 + 转向，尝试脱离卡墙。
        顺序：松开 W → 按 S 后退 evasion_back_time → 转向 evasion_turn_px。
        被打断返回 False。
        """
        nav_cfg = self._cfg.navigation
        self._log.info("执行避障: 后退 + 转向")
        # 后退
        if not self._move.move_backward(nav_cfg.evasion_back_time):
            return False
        if self._interrupted():
            return False
        # 转向 (随机左右，避免总往一个方向)
        import random
        sign = 1 if random.random() > 0.5 else -1
        if not self._move.turn(sign * nav_cfg.evasion_turn_px, 0):
            return False
        time.sleep(0.1)
        self._log.info("避障完成，继续前进")
        return True

    # ---------------- ARRIVED ----------------

    def _register_arrived_spot(self) -> Optional[str]:
        """到达水域后，将当前位置作为新钓点添加到 WorldMap"""
        if self._map is None:
            return None
        # 尝试从 OCR 获取玩家坐标
        x, y, z = 0.0, 64.0, 0.0
        if self._ocr is not None:
            state = self._ocr.get_latest_state()
            if state and state.xyz is not None:
                x, y, z = float(state.xyz[0]), float(state.xyz[1]), float(state.xyz[2])
                self._map.update_player(PlayerPosition.from_ocr(x, y, z))
            else:
                self._map.update_player(PlayerPosition(
                    x=0.0, y=64.0, z=0.0, source="relative",
                ))
        spot_id = self._map.add_spot(x, y, z, confidence=0.6, has_marker=False)
        self._map.mark_used(spot_id)
        self._map.set_current(spot_id)
        return spot_id

    # ================= 移植 Tau-main：T/I 双循环坐标导航 =================

    def navigate_to_coords(self, tx: float, ty: float, tz: float) -> NavigationOutcome:
        """
        坐标导航主入口 (移植 Tau _navigate_to_target)。
        使用 T 循环 (远距) + I 循环 (近距) 导航到目标坐标。
        需要 OCR 坐标可用 (coord_enabled=true)。
        """
        nav_cfg = self._cfg.navigation
        if not nav_cfg.enabled:
            return NavigationOutcome(success=False, reason="navigation.enabled=false",
                                     state=NavigatorState.FAILED)
        self._log.info(f"===== 坐标导航到 ({tx:.1f}, {ty:.1f}, {tz:.1f}) =====")
        self._move.stop()
        self._stuck_count = 0
        self._last_stuck_coord = None

        for _ in range(5):  # 最多重试 5 次 (避障后重试)
            if self._interrupted():
                self._move.stop()
                return NavigationOutcome(success=False, reason="被打断",
                                         state=self._current_state)
            # T 循环：远距接近
            t_result = self._t_loop(tx, ty, tz)
            if t_result == "timeout":
                return NavigationOutcome(success=False, reason="寻路超时",
                                         state=NavigatorState.FAILED)
            if t_result is False:
                return NavigationOutcome(success=False, reason="T 循环失败",
                                         state=NavigatorState.FAILED)
            if t_result == "retry":
                continue
            # t_result == True → 进入 I 循环
            i_result = self._i_loop(tx, ty, tz)
            if i_result == "timeout":
                return NavigationOutcome(success=False, reason="寻路超时",
                                         state=NavigatorState.FAILED)
            if i_result is False:
                return NavigationOutcome(success=False, reason="I 循环失败",
                                         state=NavigatorState.FAILED)
            if i_result == "retry":
                continue
            # i_result == True → 到达
            self._current_state = NavigatorState.ARRIVED
            spot_id = self._register_arrived_spot()
            self._log.success(f"坐标导航到达 spot_id={spot_id}")
            return NavigationOutcome(success=True, spot_id=spot_id,
                                     state=NavigatorState.ARRIVED)
        return NavigationOutcome(success=False, reason="重试次数耗尽",
                                 state=NavigatorState.FAILED)

    def _get_current_coords(self):
        """获取当前坐标 (x,y,z,yaw,pitch)，OCR 不可用时返回 None"""
        return self._coords.get_coords()

    def _get_sea_level_y(self, cx: float, cz: float) -> float:
        """海平面 Y 高度 (简化：统一用配置默认值)"""
        return getattr(self._cfg.navigation, "water_jump_threshold", 63.0)

    def _is_in_forbidden(self, cx: float, cz: float) -> bool:
        """是否在禁区内"""
        zones = getattr(self._cfg.navigation, "forbidden_zones", [])
        for fz in zones:
            if (fz.get("x_min", 0) <= cx <= fz.get("x_max", 0) and
                    fz.get("z_min", 0) <= cz <= fz.get("z_max", 0)):
                return True
        return False

    def _stuck_check(self, cx: float, cz: float) -> bool:
        """
        卡死检测 (移植 Tau)。位置变化 < stuck_threshold 累积计数。
        达到 stuck_trigger_count 返回 True (需避障)。
        """
        nav_cfg = self._cfg.navigation
        if self._last_stuck_coord is not None:
            d = math.hypot(cx - self._last_stuck_coord[0], cz - self._last_stuck_coord[1])
            if d < nav_cfg.stuck_threshold:
                self._stuck_count += 1
            else:
                self._stuck_count = 0
        self._last_stuck_coord = (cx, cz)
        return self._stuck_count >= nav_cfg.stuck_trigger_count

    def _t_loop(self, tx: float, ty: float, tz: float) -> object:
        """
        T 循环 (移植 Tau)：远距离持续接近目标。
        每步：取坐标 → 禁区检测 → 卡死检测 → 落水检测 → 算角度 → 旋转 → 持续按 W。
        距离 < t_to_i_distance 时返回 True (进入 I 循环)。
        返回: True=进入I循环, 'retry'=需重试, False=失败, 'timeout'=超时
        """
        nav_cfg = self._cfg.navigation
        w_down = False
        try:
            while not self._interrupted():
                coords = self._get_current_coords()
                if not coords:
                    time.sleep(0.3)
                    continue
                cx, cy, cz, cyaw, cpitch = coords
                water_th = self._get_sea_level_y(cx, cz)

                # 禁区检测
                if self._is_in_forbidden(cx, cz):
                    self._log.info("(T) 进入禁区，绕行")
                    if w_down:
                        self._move.key_up("w")
                        w_down = False
                    if not self._exit_forbidden_zone(cx, cz):
                        return False
                    return "retry"

                # 卡死检测
                if self._stuck_check(cx, cz):
                    self._log.info("(T) 连续坐标不变，触发避障")
                    if w_down:
                        self._move.key_up("w")
                        w_down = False
                    self._move.stuck_evasion()
                    self._stuck_count = 0
                    self._last_stuck_coord = None
                    return "retry"

                hor_dist = math.hypot(tx - cx, tz - cz)
                self._log.info(f"(T) 距目标 {hor_dist:.1f} 格")
                if hor_dist < nav_cfg.t_to_i_distance:
                    self._log.info("进入精确调整 (I 循环)")
                    if w_down:
                        self._move.key_up("w")
                    return True

                # 落水检测
                if cy < water_th:
                    self._log.info("(T) 落水，上浮")
                    if w_down:
                        self._move.key_up("w")
                        w_down = False
                    tyaw, _ = MovementPlanner.calc_target_angles(
                        cx, cy, cz, tx, ty, tz, nav_cfg.eye_height)
                    self._move.rotate_to_angle(tyaw, cpitch or 0, cyaw or 0, cpitch or 0,
                                               tolerance=nav_cfg.water_turn_tolerance)
                    if not self._move.float_to_surface(
                            self._get_current_coords, target_yaw=tyaw, sea_level_y=water_th):
                        return "retry"
                    return "retry"

                # 计算目标角度并旋转
                tyaw, tpitch = MovementPlanner.calc_target_angles(
                    cx, cy, cz, tx, ty, tz, nav_cfg.eye_height)
                if w_down:
                    self._move.key_up("w")
                    w_down = False
                cur_yaw = cyaw if cyaw is not None else 0
                cur_pitch = cpitch if cpitch is not None else 0
                self._move.rotate_to_angle(tyaw, tpitch, cur_yaw, cur_pitch,
                                           get_coords=self._get_current_coords)
                if not w_down:
                    self._move.key_down("w")
                    w_down = True
                time.sleep(nav_cfg.per_check)
            return False
        finally:
            if w_down:
                self._move.key_up("w")

    def _i_loop(self, tx: float, ty: float, tz: float) -> object:
        """
        I 循环 (移植 Tau)：近距离精确定位。
        每步：取坐标 → 算角度 → 旋转 → 按 W 走自适应时长 → 松 W。
        距离 <= arrival_dist 时返回 True。
        返回: True=到达, 'retry'=需重试, False=失败, 'timeout'=超时
        """
        nav_cfg = self._cfg.navigation
        iter_count = 0
        while not self._interrupted() and iter_count < nav_cfg.i_loop_max_iter:
            coords = self._get_current_coords()
            if not coords:
                time.sleep(0.2)
                continue
            cx, cy, cz, cyaw, cpitch = coords
            water_th = self._get_sea_level_y(cx, cz)

            # 禁区
            if self._is_in_forbidden(cx, cz):
                if not self._exit_forbidden_zone(cx, cz):
                    return False
                return "retry"

            # 卡死
            if self._stuck_check(cx, cz):
                self._log.info("(I) 卡死，触发避障")
                self._move.stuck_evasion()
                self._stuck_count = 0
                self._last_stuck_coord = None
                return "retry"

            # 落水
            if cy < water_th:
                tyaw, _ = MovementPlanner.calc_target_angles(
                    cx, cy, cz, tx, ty, tz, nav_cfg.eye_height)
                cur_yaw = cyaw if cyaw is not None else 0
                cur_pitch = cpitch if cpitch is not None else 0
                self._move.rotate_to_angle(tyaw, cur_pitch, cur_yaw, cur_pitch,
                                           tolerance=nav_cfg.water_turn_tolerance)
                if not self._move.float_to_surface(
                        self._get_current_coords, target_yaw=tyaw, sea_level_y=water_th):
                    return "retry"
                return "retry"

            hor_dist = math.hypot(tx - cx, tz - cz)
            self._log.info(f"(I) 距目标 {hor_dist:.1f} 格")
            if hor_dist <= nav_cfg.arrival_dist and cy >= water_th:
                return True

            tyaw, tpitch = MovementPlanner.calc_target_angles(
                cx, cy, cz, tx, ty, tz, nav_cfg.eye_height)
            cur_yaw = cyaw if cyaw is not None else 0
            cur_pitch = cpitch if cpitch is not None else 0
            self._move.rotate_to_angle(tyaw, tpitch, cur_yaw, cur_pitch,
                                       get_coords=self._get_current_coords)

            # 自适应步行时间
            walk_time = (hor_dist / nav_cfg.player_speed) * nav_cfg.walk_time_factor
            adaptive_max = max(
                nav_cfg.i_loop_max_walk_time,
                min(hor_dist / nav_cfg.player_speed * nav_cfg.i_loop_adaptive_ratio,
                    nav_cfg.i_loop_adaptive_max_walk))
            walk_time = max(nav_cfg.i_loop_min_walk_time, min(walk_time, adaptive_max))

            self._move.key_down("w")
            try:
                time.sleep(walk_time)
            finally:
                self._move.key_up("w")
            time.sleep(nav_cfg.i_loop_post_walk_delay)
            iter_count += 1
        if iter_count >= nav_cfg.i_loop_max_iter:
            self._log.warn("I 循环达到最大迭代次数")
            return False
        return False

    def _exit_forbidden_zone(self, cx: float, cz: float) -> bool:
        """离开禁区 (移植 Tau 简化版)：朝最近边界方向走"""
        nav_cfg = self._cfg.navigation
        zones = getattr(nav_cfg, "forbidden_zones", [])
        fz = None
        for z in zones:
            if (z.get("x_min", 0) <= cx <= z.get("x_max", 0) and
                    z.get("z_min", 0) <= cz <= z.get("z_max", 0)):
                fz = z
                break
        if fz is None:
            return True
        # 选最近边界
        distances = {
            "east": fz["x_max"] - cx, "west": cx - fz["x_min"],
            "south": fz["z_max"] - cz, "north": cz - fz["z_min"],
        }
        dir_key = min(distances, key=lambda k: abs(distances[k]))
        target_yaw = {"east": -90.0, "west": 90.0, "south": 0.0, "north": 180.0}[dir_key]
        self._log.info(f"离开禁区方向: {dir_key}")
        coords = self._get_current_coords()
        if coords:
            cyaw, cpitch = coords[3], coords[4]
            self._move.rotate_to_angle(target_yaw, cpitch or 0, cyaw or 0, cpitch or 0)
        for _ in range(50):
            if self._interrupted():
                return False
            coords = self._get_current_coords()
            if not coords:
                time.sleep(0.2)
                continue
            cx2, cz2 = coords[0], coords[2]
            if not self._is_in_forbidden(cx2, cz2):
                return True
            self._move.key_down("w")
            try:
                time.sleep(0.2)
            finally:
                self._move.key_up("w")
            time.sleep(0.1)
        self._log.warn("离开禁区超时")
        return False

    # ---------------- 安全 ----------------

    def emergency_stop(self) -> None:
        """紧急停止：释放所有按键"""
        self._move.stop()
