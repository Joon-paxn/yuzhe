"""主入口。

阶段 1: 装配各模块，启动钓鱼循环。
导航引擎已接入，但实际 Pathfinder 寻路需阶段 2+ 的 ClientBridge 实现。
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from functools import partial

from config import load_config
from client.bridge import OcrFallbackBridge
from client.world_query import DummyWorldQuery
from fishing.engine import FishingEngine
from navigation.engine import NavigationEngine
from navigation.route_memory import RouteMemory
from spots.manager import SpotManager
import input as game_input

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


class AutoFishingApp:
    def __init__(self):
        self.config = load_config()
        self.hwnd = game_input.find_window(self.config.window_title_keyword)
        if self.hwnd:
            logger.info(f"找到游戏窗口 hwnd={self.hwnd}")
        else:
            logger.warning(f"未找到窗口 (关键词: {self.config.window_title_keyword})，将使用 global 模式")

        # 地图 & 钓点
        self.spots = SpotManager(self.config.map_data_path, self.config.runtime_path)

        # 客户端桥接 (阶段 1: OCR 兜底占位)
        self.bridge = OcrFallbackBridge()
        self.world_query = DummyWorldQuery()

        # 路线记忆
        self.route_memory = RouteMemory(self.config.route_memory_path)

        # 导航引擎
        self.navigator = NavigationEngine(
            self.bridge, self.world_query, self.spots, self.route_memory)

        # 钓鱼引擎
        self.fishing = FishingEngine(
            grab_pixel=partial(game_input.grab_pixel, hwnd=self.hwnd),
            right_click=partial(game_input.right_click, hwnd=self.hwnd,
                                input_mode=self.config.input_mode),
            on_depleted=self._on_depleted,
            config=self.config.fishing.__dict__,
        )
        if self.config.fishing.m_pos:
            self.fishing.set_target_pixel(
                tuple(self.config.fishing.m_pos), self.config.fishing.px_color)

        self._running = True

    def _on_depleted(self) -> None:
        """枯竭回调：启动换池导航。"""
        logger.info("[MAIN] 钓点枯竭，启动换池...")
        result = self.navigator.relocate()
        if result.success:
            logger.info(f"[MAIN] 换池成功，到达钓点 {result.spot_id}")
            # 换池后继续钓鱼
            self.fishing.start(auto_throw=self.config.fishing.auto_throw, skip_initial=True)
        else:
            logger.error(f"[MAIN] 换池失败: {result.reason}")

    def start(self) -> None:
        if not self.config.fishing.m_pos:
            logger.warning("未配置钓鱼像素位置 m_pos，钓鱼检测将无法工作")
        logger.info("启动自动钓鱼 (Ctrl+C 停止)...")
        self.fishing.start(auto_throw=self.config.fishing.auto_throw)
        try:
            while self._running:
                time.sleep(0.5)
                if not self.fishing.is_active and self.fishing.state.value != "depleted":
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._running = False
        self.fishing.stop()
        self.navigator.emergency_stop()
        self.spots.save_runtime()
        self.route_memory.save()
        logger.info("已停止")


def main():
    app = AutoFishingApp()
    signal.signal(signal.SIGINT, lambda *_: app.stop())
    app.start()


if __name__ == "__main__":
    main()
