"""
main.py
-------
程序入口。

职责：
- 加载配置
- 查找 Minecraft 窗口
- 初始化检测器、输入控制器、钓鱼引擎、快捷键
- 主线程事件循环：处理 F8(选区域) / F9(测试) 等需要主线程的操作
- 优雅退出
"""

from __future__ import annotations

import queue
import signal
import sys
import threading
import time
from typing import Optional

from config import AppConfig, load_config, save_config, RoiConfig
from detector import BiteDetector
from fishing import FishingEngine
from hotkeys import HotkeyManager
from input_controller import InputController
from minecraft_window import MinecraftWindow
from region_selector import select_roi_relative
from pixel_picker import pick_pixel_color
from gui import FishingGUI
from logger import get_logger


CONFIG_PATH = "config.json"


class AutoFishingApp:
    """自动钓鱼应用主类"""

    def __init__(self) -> None:
        self._log = get_logger()
        self._config: AppConfig = load_config(CONFIG_PATH)

        self._mc = MinecraftWindow(self._config.window.title_keyword)
        self._detector: Optional[BiteDetector] = None
        self._input: Optional[InputController] = None
        self._engine: Optional[FishingEngine] = None
        self._hotkeys: Optional[HotkeyManager] = None
        self._gui: Optional[FishingGUI] = None

        # 主线程任务队列 (F8/F9 等需要主线程执行的操作)
        self._task_queue: "queue.Queue[str]" = queue.Queue()
        self._exit_event = threading.Event()

        # 失焦自动暂停标记：True 表示因 Minecraft 失焦而自动暂停，
        # 回焦时自动恢复；用户手动暂停 (F6/按钮) 不会设置此标记。
        self._auto_paused = False

    def run(self) -> int:
        """主程序入口，返回退出码"""
        self._log.info("===== Minecraft 自动钓鱼程序启动 =====")

        # 1. 查找 Minecraft 窗口
        if not self._mc.find():
            self._log.error(
                "未找到 Minecraft 窗口。请先启动 Minecraft 并进入游戏，"
                "然后重新运行本程序。"
            )
            return 1
        self._log.success("Minecraft 窗口已找到")

        # 2. 初始化各模块
        self._detector = BiteDetector(
            self._mc, self._config.detection, self._config.roi
        )
        self._input = InputController(self._mc)
        self._engine = FishingEngine(
            self._config, self._mc, self._detector, self._input
        )

        # 3. 注册快捷键
        self._hotkeys = HotkeyManager(self._config.hotkeys)
        self._hotkeys.on_toggle = self._on_toggle
        self._hotkeys.on_stop = self._on_stop
        self._hotkeys.on_select_region = lambda: self._task_queue.put("select_region")
        self._hotkeys.on_test = lambda: self._task_queue.put("test")
        self._hotkeys.on_capture_template = lambda: self._task_queue.put("capture_template")
        self._hotkeys.on_pick_color = lambda: self._task_queue.put("pick_color")

        if not self._hotkeys.register():
            self._log.error("快捷键注册失败，程序退出")
            return 1

        # 4. 信号处理 (Ctrl+C)
        signal.signal(signal.SIGINT, lambda *_: self._shutdown())

        # 5. 提示
        self._print_help()

        # 6. 启动 GUI (GUI 的 after() 驱动主循环，替代 while+sleep)
        self._gui = FishingGUI(self)
        root = self._gui.build()
        # 启动第一次 tick，之后由 tick 自己用 after 调度
        root.after(50, self._gui.tick)

        try:
            root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

        return 0

    # ---------------- 快捷键回调 ----------------

    def _on_toggle(self) -> None:
        """F6: 开始/暂停。手动操作时清除自动暂停标记，避免被自动恢复覆盖。"""
        if self._engine is None:
            return
        # 用户手动操作，不再是「自动暂停」状态
        self._auto_paused = False
        if not self._engine.is_running():
            # 启动前重新查找窗口 (可能用户重新打开了 Minecraft)
            if not self._mc.is_valid():
                if not self._mc.find():
                    self._log.error("Minecraft 窗口无效，无法启动")
                    return
            self._engine.start()
        else:
            self._engine.toggle_pause()

    def _on_stop(self) -> None:
        """F7: 停止"""
        self._auto_paused = False
        if self._engine is not None and self._engine.is_running():
            self._engine.stop()
        else:
            self._log.info("自动钓鱼未在运行")

    # ---------------- F8 / F9 操作 (主线程执行) ----------------

    def _do_select_region(self) -> None:
        """F8: 重新选择检测区域"""
        if self._engine is not None and self._engine.is_running():
            self._log.warn("请先停止自动钓鱼 (F7) 再选择检测区域")
            return

        self._log.info("进入区域选择模式...")
        self._log.info("请在弹出的全屏截图中框住「咬钩！」文字区域")

        if not self._mc.is_valid():
            if not self._mc.find():
                self._log.error("Minecraft 窗口无效，无法选择区域")
                return

        result = select_roi_relative(self._mc)
        if result is None:
            return

        x, y, w, h = result
        self._config.roi = RoiConfig(x=x, y=y, width=w, height=h)
        save_config(CONFIG_PATH, self._config)

        if self._detector is not None:
            self._detector.update_config(self._config.detection, self._config.roi)

        self._log.success(
            f"检测区域已保存: x={x:.4f}, y={y:.4f}, "
            f"w={w:.4f}, h={h:.4f}"
        )

    def _do_test(self) -> None:
        """
        F9: 测试当前检测区域。

        持续检测约 8 秒，实时输出白色像素数量和检测状态，
        方便用户调节阈值。期间按任意 F 键可提前结束 (用队列机制简化)。
        """
        if self._engine is not None and self._engine.is_running():
            self._log.warn("请先停止自动钓鱼 (F7) 再进行测试")
            return

        if self._detector is None:
            return

        if not self._mc.is_valid():
            if not self._mc.find():
                self._log.error("Minecraft 窗口无效，无法测试")
                return

        self._log.info("===== 检测测试模式 (约 8 秒) =====")
        self._detector.reset()

        duration_s = 8.0
        start = time.monotonic()
        last_log = 0.0

        while time.monotonic() - start < duration_s:
            result = self._detector.detect_once()
            x, y, w, h = result.roi_screen
            now = time.monotonic()

            # 每 ~200ms 输出一次，避免刷屏
            if now - last_log >= 0.2:
                last_log = now
                status = "检测到咬钩！" if result.bite_detected else "未检测到咬钩"
                mode = self._detector.get_mode_name()
                self._log.test(
                    f"[{mode}] "
                    f"匹配度={result.match_score:.3f} | "
                    f"总白={result.white_pixels} | "
                    f"大连通域白={result.large_white_pixels} | "
                    f"连通域数={result.component_count} | "
                    f"状态={status}"
                )

            # 检查是否有新任务 (例如用户再次按 F9/F8/F10)
            try:
                task = self._task_queue.get_nowait()
                if task in ("test", "select_region", "capture_template"):
                    self._task_queue.put(task)
                    break
            except queue.Empty:
                pass

            time.sleep(0.1)

        self._log.info("===== 测试结束 =====")
        mode = self._detector.get_mode_name()
        det = self._config.detection
        if mode == "模板匹配":
            self._log.info(
                f"当前使用模板匹配。匹配度 >= {det.template_match_threshold:.2f} 判定为咬钩。"
            )
        elif mode == "颜色检测":
            self._log.info(
                f"当前使用颜色检测。目标色 RGB{det.target_color}，"
                f"容差±{det.color_tolerance}，大连通域像素 >= {det.white_pixel_threshold} 判定为咬钩。"
            )
        else:
            self._log.info(
                f"当前使用白色像素检测。白色阈值={det.white_threshold}，"
                f"大连通域像素 >= {det.white_pixel_threshold} 判定为咬钩。"
            )

    def _do_capture_template(self) -> None:
        """
        F10: 截取「咬钩！」—— 一次操作同时完成取色 + 截模板。

        流程：
        1. 弹出全屏取色器，用户左键点击「咬钩！」文字上的像素
        2. 取该像素颜色 → 保存为 target_color，启用颜色检测
        3. 取色后立即截取当前 ROI 区域 → 保存为 bite_template.png

        这样用户只需在咬钩出现时按一次 F10、点一下文字，
        颜色和模板都搞定，不用分别操作 F10+F11。
        """
        if self._engine is not None and self._engine.is_running():
            self._log.warn("请先停止自动钓鱼 (F7) 再截取咬钩")
            return

        if self._detector is None:
            return

        if not self._mc.is_valid():
            if not self._mc.find():
                self._log.error("Minecraft 窗口无效，无法截取咬钩")
                return

        # 检查 ROI 是否已设置
        roi = self._config.roi
        if roi.width <= 0 or roi.height <= 0:
            self._log.error("请先按 F8 选择检测区域，再截取咬钩")
            return

        self._log.info("=== 截取咬钩（取色 + 模板）===")
        self._log.info(
            "即将弹出取色器：移动鼠标到「咬钩！」文字上，左键确认，右键/Esc 取消"
        )

        # 第一步：取色
        color = pick_pixel_color()
        if color is None:
            self._log.info("已取消，未做任何更改")
            return

        r, g, b = color
        self._config.detection.target_color = (r, g, b)
        self._config.detection.use_color_detection = True
        save_config(CONFIG_PATH, self._config)
        if self._detector is not None:
            self._detector.update_config(self._config.detection, self._config.roi)
        self._log.success(f"已取色 RGB({r},{g},{b})，颜色检测已启用")

        # 第二步：截取 ROI 为模板（此时咬钩仍在屏幕上）
        self._log.info("正在截取模板...")
        success = self._detector.capture_template()
        if success:
            self._log.success(
                "模板已保存！取色 + 模板均已完成。可按 F9 测试效果。"
            )
        else:
            self._log.warn(
                "模板截取失败（颜色已保存）。可稍后再按 F10 补截模板。"
            )

    def _do_pick_color(self) -> None:
        """F11: 已合并到 F10（取色 + 截模板一次完成）。保留入口避免旧配置报错。"""
        self._do_capture_template()

    # ---------------- 退出与清理 ----------------

    def _shutdown(self) -> None:
        """请求退出：设置退出标志并结束 tkinter 主循环"""
        self._exit_event.set()
        # 结束 GUI 主循环 (从 mainloop 返回后进入 _cleanup)
        if self._gui is not None and self._gui._root is not None:
            try:
                self._gui._root.quit()
            except Exception:
                pass

    def _cleanup(self) -> None:
        self._log.info("正在退出...")
        if self._engine is not None:
            self._engine.stop()
            self._engine.wait()
        if self._hotkeys is not None:
            self._hotkeys.unregister()
        if self._gui is not None:
            self._gui.destroy()
        if self._detector is not None:
            self._detector.close()
        self._log.success("程序已退出")

    def _print_help(self) -> None:
        self._log.info("快捷键说明:")
        self._log.info("  F6  - 开始 / 暂停自动钓鱼")
        self._log.info("  F7  - 立即停止自动钓鱼")
        self._log.info("  F8  - 重新选择「咬钩！」检测区域")
        self._log.info("  F9  - 测试当前检测区域")
        self._log.info("  F10 - 截取咬钩 (取色 + 模板一次完成，出现咬钩时按)")
        self._log.info("  Ctrl+C - 退出程序")
        self._log.info(
            "使用流程: F8选区域 -> 抛竿等「咬钩！」出现 -> "
            "F10截取咬钩 -> F9测试 -> F6开始"
        )
        self._log.info(
            "失焦保护: 切换到其他窗口会自动暂停，切回 Minecraft 等 3 秒自动恢复"
        )


def main() -> int:
    app = AutoFishingApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
