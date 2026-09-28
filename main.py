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
from pixel_picker import pick_pixel_color, pick_pixel_colors, pick_pixel_colors_with_pos
from gui import FishingGUI
from logger import get_logger
from ocr import OcrService
from navigation.map import WorldMap
from navigation.movement import MovementPlanner
from navigation.navigator import Navigator
from vision import VisionDetector
from log_watcher import LogWatcher


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
        self._ocr: Optional[OcrService] = None
        self._vision: Optional[VisionDetector] = None
        self._world_map: Optional[WorldMap] = None
        self._movement: Optional[MovementPlanner] = None
        self._navigator: Optional[Navigator] = None
        self._log_watcher: Optional[LogWatcher] = None

        self._task_queue: "queue.Queue[str]" = queue.Queue()
        self._exit_event = threading.Event()
        self._auto_paused = False

    def run(self) -> int:
        self._log.info("===== Minecraft 自动钓鱼程序启动 =====")

        if not self._mc.find():
            self._log.error(
                "未找到 Minecraft 窗口。请先启动 Minecraft 并进入游戏，然后重新运行本程序。"
            )
            return 1
        self._log.success("Minecraft 窗口已找到")

        self._detector = BiteDetector(
            self._mc, self._config.detection, self._config.roi, self._config.adaptive_rgb
        )
        # 借鉴 Tau-main：后台抓图开关
        self._detector._grab_from_window = self._config.window.grab_from_window
        # 借鉴 Tau-main：输入模式 global/window
        self._input = InputController(self._mc, input_mode=self._config.window.input_mode)

        # OCR 识别层 (阶段二/三)：后台独立线程，供 hybrid/ocr 确认咬钩 + 枯竭检测
        self._ocr = OcrService(self._mc, self._config.ocr)
        self._ocr._grab_from_window = self._config.window.grab_from_window
        self._detector.set_ocr_service(self._ocr)

        # 日志监听 (借鉴 Tau-main)：增量 tail latest.log 检测钓点枯竭，比 OCR 更可靠
        if self._config.window.log_watch_enabled and self._config.window.log_watch_path:
            self._log_watcher = LogWatcher(
                log_path=self._config.window.log_watch_path,
                interval_s=self._config.window.log_watch_interval_ms / 1000.0,
            )
            started = self._log_watcher.start()
            if started:
                self._log.success(f"日志监听已启动: {self._config.window.log_watch_path}")
            else:
                self._log_watcher = None
        else:
            self._log.info("日志监听未启用 (window.log_watch_enabled=false 或路径为空)")

        self._engine = FishingEngine(
            self._config, self._mc, self._detector, self._input, self._ocr,
            log_watcher=self._log_watcher,
        )

        # ocr.enabled=true 或 detection.mode ∈ {hybrid, ocr} 时启动 OCR 后台线程
        need_ocr = self._config.ocr.enabled or self._config.detection.mode in ("hybrid", "ocr")
        if need_ocr:
            started = self._ocr.start()
            if not started:
                self._log.warn(
                    f"检测模式={self._config.detection.mode!r} 需要 OCR，但 OCR 引擎不可用，"
                    f"将回退到 RGB 检测。请安装 rapidocr onnxruntime。"
                )
        else:
            self._log.info("OCR 层未启用 (ocr.enabled=false 且 mode=rgb)，现有钓鱼检测不受影响")

        # 阶段六~九：视觉层 + 世界地图 + 移动规划 + 导航器
        if self._config.vision.enabled:
            self._vision = VisionDetector(self._config.vision)
            self._log.info("视觉识别层已启用 (阶段四)")
        else:
            self._log.info("视觉识别层未启用 (vision.enabled=false)，自动寻路将不可用")

        # 加载持久化世界地图
        self._world_map = WorldMap.load(self._config.map.persistence_path)
        self._world_map.spot_dedup_distance = self._config.map.spot_dedup_distance
        self._world_map.auto_save = self._config.map.auto_save
        self._world_map.persistence_path = self._config.map.persistence_path
        self._log.info(f"世界地图: {self._world_map.summary()}")

        # MovementPlanner + Navigator (共享 FishingEngine 的 stop/pause 事件)
        self._movement = MovementPlanner(
            self._input, self._engine._stop_event, self._engine._pause_event,
            nav_cfg=self._config.navigation,
        )
        self._navigator = Navigator(
            self._config, self._mc, self._input, self._movement,
            vision=self._vision, world_map=self._world_map, ocr_service=self._ocr,
        )
        self._navigator._grab_from_window = self._config.window.grab_from_window
        # 注入 Navigator 到 FishingEngine (DEPLETED 后自动寻路)
        self._engine._navigator = self._navigator
        if self._config.navigation.enabled:
            if self._vision is None:
                self._log.warn(
                    "navigation.enabled=true 但 vision.enabled=false，"
                    "自动寻路需要视觉层识别水域，已回退到 DEPLETED 停止行为"
                )
            else:
                self._log.success("自动寻找新钓点已启用 (阶段六~九完整自动循环)")
        else:
            self._log.info("自动寻路未启用 (navigation.enabled=false)，DEPLETED 时停止钓鱼")

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

        signal.signal(signal.SIGINT, lambda *_: self._shutdown())

        self._print_help()

        self._gui = FishingGUI(self)
        root = self._gui.build()
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
        if self._engine is None:
            return
        self._auto_paused = False
        if not self._engine.is_running():
            if not self._mc.is_valid():
                if not self._mc.find():
                    self._log.error("Minecraft 窗口无效，无法启动")
                    return
            self._engine.start()
        else:
            self._engine.toggle_pause()

    def _on_stop(self) -> None:
        self._auto_paused = False
        if self._engine is not None and self._engine.is_running():
            self._engine.stop()
        else:
            self._log.info("自动钓鱼未在运行")

    # ---------------- F8 / F9 操作 ----------------

    def _do_select_region(self) -> None:
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
            self._detector.update_config(
                self._config.detection, self._config.roi, self._config.adaptive_rgb
            )
        self._log.success(
            f"检测区域已保存: x={x:.4f}, y={y:.4f}, w={w:.4f}, h={h:.4f}"
        )

    def _do_test(self) -> None:
        """F9: 测试当前检测区域，自适应模式输出模型状态"""
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
            now = time.monotonic()
            if now - last_log >= 0.2:
                last_log = now
                status = "检测到咬钩！" if result.bite_detected else "未检测到咬钩"
                mode = self._detector.get_mode_name()
                if result.adaptive_used:
                    self._log_adaptive_state(result, status)
                elif result.dual_color_used:
                    a_status = "通过" if result.color_a_passed else "未通过"
                    b_status = "通过" if result.color_b_passed else "未通过"
                    self._log.test(
                        f"[{mode}] A:{a_status}({result.color_a_large}px) | "
                        f"B:{b_status}({result.color_b_large}px) | 状态={status}"
                    )
                else:
                    self._log.test(
                        f"[{mode}] 匹配度={result.match_score:.3f} | "
                        f"总白={result.white_pixels} | 大连通域白={result.large_white_pixels} | "
                        f"连通域数={result.component_count} | 状态={status}"
                    )
            try:
                task = self._task_queue.get_nowait()
                if task in ("test", "select_region", "capture_template"):
                    self._task_queue.put(task)
                    break
            except queue.Empty:
                pass
            time.sleep(0.1)

        self._log.info("===== 测试结束 =====")
        self._log_test_summary()

    def _log_adaptive_state(self, result, status: str) -> None:
        """输出自适应模型状态 (规格点 9 格式)"""
        a_ref = result.a_reference
        b_ref = result.b_reference
        a_ref_s = f"RGB{a_ref}" if a_ref else "未就绪"
        b_ref_s = f"RGB{b_ref}" if b_ref else "未就绪"
        a_mark = "✓" if result.a_in_range else "✗"
        b_mark = "✓" if result.b_in_range else "✗"
        a_tol = result.a_tolerance
        b_tol = result.b_tolerance
        self._log.test(
            f"[自适应] Point A  Reference={a_ref_s}  Samples={result.a_samples}  "
            f"Tolerance=±({a_tol[0]},{a_tol[1]},{a_tol[2]})  当前={result.a_current_rgb} {a_mark}"
        )
        self._log.test(
            f"[自适应] Point B  Reference={b_ref_s}  Samples={result.b_samples}  "
            f"Tolerance=±({b_tol[0]},{b_tol[1]},{b_tol[2]})  当前={result.b_current_rgb} {b_mark}"
        )
        self._log.test(f"[自适应] 当前: A {a_mark}  B {b_mark}  |  结果: {status}")

    def _log_test_summary(self) -> None:
        mode = self._detector.get_mode_name()
        det = self._config.detection
        ada = self._config.adaptive_rgb
        if mode == "模板匹配":
            self._log.info(
                f"当前使用模板匹配。匹配度 >= {det.template_match_threshold:.2f} 判定为咬钩。"
            )
        elif mode == "自适应双重取色":
            self._log.info(
                f"当前使用自适应 RGB 模型 (Beta)。"
                f"A/B 各自维护 {ada.max_samples} 历史样本 (中位数 Reference + 动态容差)。"
                f"冷启动需 {ada.min_samples} 样本，容差范围 {ada.min_tolerance}~{ada.max_tolerance}，"
                f"异常过滤倍数 {ada.outlier_threshold}。"
            )
            snap = self._detector.get_adaptive_snapshot()
            sa, sb = snap["a"], snap["b"]
            self._log.info(
                f"模型状态: A 样本={sa['samples']} Reference={sa['reference']} "
                f"Tolerance=±{sa['tolerance']} ready={sa['ready']} | "
                f"B 样本={sb['samples']} Reference={sb['reference']} "
                f"Tolerance=±{sb['tolerance']} ready={sb['ready']}"
            )
        elif mode == "双重取色":
            self._log.info(
                f"当前使用双重取色混合判断。A 色 RGB{det.target_color}，B 色 RGB{det.target_color_b}，"
                f"容差±{det.color_tolerance}，两色大连通域像素均 >= {det.white_pixel_threshold} 才判定咬钩。"
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
        F10: 截取「咬钩！」—— 双重取色 (含坐标) + 截模板。
        阶段二: 同时记录 A、B 屏幕坐标 (相对窗口比例)，供自适应模型每帧采样。
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
        roi = self._config.roi
        if roi.width <= 0 or roi.height <= 0:
            self._log.error("请先按 F8 选择检测区域，再截取咬钩")
            return

        self._log.info("=== 截取咬钩（双重取色 A+B + 模板）===")
        self._log.info("即将弹出取色器：先取 A 点，再取 B 点 (右键/Esc 取消)")

        # 取色同时获取屏幕坐标
        picks = pick_pixel_colors_with_pos([
            "取色 A 点：移动到「咬钩！」文字主体，左键确认  |  右键/Esc 取消",
            "取色 B 点：移动到「咬钩！」描边/另一像素，左键确认  |  右键/Esc 取消",
        ])
        color_a, pos_a = picks[0]
        color_b, pos_b = picks[1]
        if color_a is None or color_b is None:
            self._log.info("已取消取色，未做任何更改")
            return

        ra, ga, ba = color_a
        rb, gb, bb = color_b
        self._config.detection.target_color = (ra, ga, ba)
        self._config.detection.target_color_b = (rb, gb, bb)
        self._config.detection.use_color_detection = True
        self._config.detection.use_dual_color = True

        # 阶段二：记录 A、B 相对窗口比例坐标
        win_rect = self._mc.get_rect()
        if win_rect is not None and win_rect.width > 0 and win_rect.height > 0:
            ax, ay = pos_a
            bx, by = pos_b
            self._config.detection.point_a_ratio = (
                (ax - win_rect.left) / win_rect.width,
                (ay - win_rect.top) / win_rect.height,
            )
            self._config.detection.point_b_ratio = (
                (bx - win_rect.left) / win_rect.width,
                (by - win_rect.top) / win_rect.height,
            )
            self._config.detection.points_ratio_set = True
            self._log.info(
                f"A 点比例=({self._config.detection.point_a_ratio[0]:.4f},"
                f"{self._config.detection.point_a_ratio[1]:.4f})  "
                f"B 点比例=({self._config.detection.point_b_ratio[0]:.4f},"
                f"{self._config.detection.point_b_ratio[1]:.4f})"
            )
        else:
            self._log.warn("窗口矩形无效，未记录 A/B 坐标，自适应模型将不可用")

        save_config(CONFIG_PATH, self._config)
        if self._detector is not None:
            self._detector.update_config(
                self._config.detection, self._config.roi, self._config.adaptive_rgb
            )
            # 重新取色后重置模型，让新颜色重新冷启动
            self._detector.reset_adaptive_models()
        self._log.success(
            f"双重取色完成：A=RGB({ra},{ga},{ba})@{pos_a}  B=RGB({rb},{gb},{bb})@{pos_b}"
        )

        self._log.info("正在截取模板...")
        success = self._detector.capture_template()
        if success:
            self._log.success("模板已保存！双重取色 + 模板均已完成。可按 F9 测试效果。")
        else:
            self._log.warn("模板截取失败（双重取色已保存）。可稍后再按 F10 补截模板。")

    def _do_pick_color(self) -> None:
        """F11: 已合并到 F10。保留入口避免旧配置报错。"""
        self._do_capture_template()

    # ---------------- 退出与清理 ----------------

    def _shutdown(self) -> None:
        self._exit_event.set()
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
        if self._ocr is not None:
            self._ocr.close()
        if self._log_watcher is not None:
            self._log_watcher.close()
        if self._hotkeys is not None:
            self._hotkeys.unregister()
        if self._gui is not None:
            self._gui.destroy()
        if self._detector is not None:
            self._detector.close()
        self._log.success("程序已退出")

    def _print_help(self) -> None:
        ada = self._config.adaptive_rgb
        self._log.info("快捷键说明:")
        self._log.info("  F6  - 开始 / 暂停自动钓鱼")
        self._log.info("  F7  - 立即停止自动钓鱼")
        self._log.info("  F8  - 重新选择「咬钩！」检测区域")
        self._log.info("  F9  - 测试当前检测区域")
        self._log.info("  F10 - 截取咬钩 (双重取色 A+B + 模板 + 坐标，出现咬钩时按)")
        self._log.info("  Ctrl+C - 退出程序")
        self._log.info(
            "使用流程: F8选区域 -> 抛竿等「咬钩！」出现 -> F10双重取色+模板 -> F9测试 -> F6开始"
        )
        self._log.info("双重取色: 咬钩需 A 色、B 色同时达标才触发，降低误触发")
        if ada.enabled:
            self._log.info(
                f"自适应模型 (Beta): 已启用。中位数 Reference + 动态容差，"
                f"冷启动 {ada.min_samples} 样本，历史上限 {ada.max_samples}。"
            )
        else:
            self._log.info("自适应模型 (Beta): 未启用，走阶段一色差掩膜计数")
        self._log.info("失焦保护: 切换到其他窗口会自动暂停，切回 Minecraft 等 3 秒自动恢复")


def main() -> int:
    app = AutoFishingApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
