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

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from config import AppConfig
from input_controller import InputController
from logger import get_logger
from minecraft_window import MinecraftWindow
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

    @property
    def state(self) -> str:
        return self._current_state

    # ---------------- 截图辅助 ----------------

    def _capture_frame(self) -> Optional[np.ndarray]:
        """截取 Minecraft 窗口画面"""
        if not self._mc.is_valid():
            return None
        rect = self._mc.get_rect()
        if rect is None:
            return None
        try:
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
        寻找新钓点主流程：SEARCHING → PLANNING → MOVING → ARRIVED
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
        if self._vision is None:
            return NavigationOutcome(
                success=False,
                reason="视觉识别层未初始化 (vision.enabled=false)",
                state=NavigatorState.FAILED,
            )
        if self._map is None:
            return NavigationOutcome(
                success=False, reason="世界地图未初始化",
                state=NavigatorState.FAILED,
            )

        self._log.info("===== 开始自动寻找新钓点 =====")
        self._move.stop()  # 清理可能残留的按键

        # 1. SEARCHING: 旋转扫描水域
        water = self._search_water()
        if self._interrupted():
            self._move.stop()
            return NavigationOutcome(success=False, reason="被打断", state=self._current_state)
        if water is None:
            self._log.warn("未找到水域，寻路失败")
            self._current_state = NavigatorState.FAILED
            return NavigationOutcome(success=False, reason="未发现水域", state=self._current_state)

        self._log.info(f"发现水域：{water.area}px 中心=({water.cx:.0f},{water.cy:.0f})")

        # 2. PLANNING + MOVING: 朝水域移动，定期重新检测
        arrived = self._approach_water(water)
        if self._interrupted():
            self._move.stop()
            return NavigationOutcome(success=False, reason="被打断", state=self._current_state)
        if not arrived:
            self._log.warn("移动阶段未能到达水域")
            self._current_state = NavigatorState.FAILED
            return NavigationOutcome(success=False, reason="移动未到达水域", state=self._current_state)

        # 3. ARRIVED: 添加钓点到地图
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
        """
        self._current_state = NavigatorState.PLANNING
        nav_cfg = self._cfg.navigation
        step_s = nav_cfg.move_step_s
        frame_w = 1
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
                continue
            # 选最大水域
            water = max(result.water, key=lambda w: w.area)
            frame_w, frame_h = result.frame_size
            frame_area = frame_w * frame_h
            ratio = water.area / max(1, frame_area)
            # 到达判定
            if ratio >= nav_cfg.arrival_water_area_ratio and \
               water.confidence >= nav_cfg.arrival_min_confidence:
                self._log.info(f"步骤 {step+1}: 水域占比 {ratio:.2f} 达到阈值，已到达")
                return True
            # 旋转对齐：水域中心相对屏幕中心的水平偏移
            screen_cx = frame_w / 2.0
            offset_x = water.cx - screen_cx
            # 偏移转旋转像素 (简化：偏移占画面宽度的比例 × turn_max_px)
            turn_dx = int((offset_x / max(1, frame_w)) * nav_cfg.turn_max_px)
            # 限制单步旋转
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
        return False

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

    # ---------------- 安全 ----------------

    def emergency_stop(self) -> None:
        """紧急停止：释放所有按键"""
        self._move.stop()
