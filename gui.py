"""
gui.py
------
自动钓鱼程序的图形界面。

职责：
- 显示当前运行状态 (运行/暂停/停止/自动暂停)
- 提供操作按钮：开始/暂停、停止、选区域、测试、截模板、取色
- 用 tkinter 的 after() 驱动主循环，替代原来的 while+sleep：
    * 处理任务队列 (F8/F9/F10/F11)
    * 检查 Minecraft 窗口有效性 (窗口关闭则停止)
    * 检查窗口焦点 (失焦自动暂停，回焦自动恢复)
    * 更新状态显示

运行在主线程 (tkinter 要求)；FishingEngine 运行在独立线程。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import AutoFishingApp


# 主循环 tick 间隔 (ms)
TICK_INTERVAL_MS = 50
# 窗口有效性检查间隔 (ms)
WINDOW_CHECK_INTERVAL_MS = 2000
# 焦点检查间隔 (ms)
FOCUS_CHECK_INTERVAL_MS = 300
# 回焦后等待多久才恢复 (秒)：避免焦点抖动误恢复
FOCUS_RESUME_DELAY_S = 3.0


class FishingGUI:
    """自动钓鱼图形界面"""

    def __init__(self, app: "AutoFishingApp") -> None:
        self._app = app
        self._root: Optional[tk.Tk] = None

        # 计时器
        self._last_window_check = 0.0
        self._last_focus_check = 0.0
        # 回焦恢复计时：失焦时为 0；回焦后记录时刻，持续焦点达到阈值才恢复
        self._focus_regain_time: float = 0.0

        # 状态变量 (在 build() 中创建，因为需要先有 Tk 根窗口)
        self._status_var: Optional[tk.StringVar] = None
        self._mode_var: Optional[tk.StringVar] = None
        self._roi_var: Optional[tk.StringVar] = None
        self._params_var: Optional[tk.StringVar] = None
        self._color_var: Optional[tk.StringVar] = None   # 当前目标颜色 RGB

        # 按钮引用 (用于更新文字/状态)
        self._btn_toggle: Optional[ttk.Button] = None
        # 颜色预览画布 (显示当前 target_color 的色块)
        self._color_preview: Optional[tk.Canvas] = None

    # ---------------- 构建界面 ----------------

    def build(self) -> tk.Tk:
        """构建并返回主窗口"""
        root = tk.Tk()
        root.title("Minecraft 自动钓鱼")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        self._root = root

        # 状态变量必须在 Tk 根窗口创建后实例化
        self._status_var = tk.StringVar(value="就绪")
        self._mode_var = tk.StringVar(value="")
        self._roi_var = tk.StringVar(value="")
        self._params_var = tk.StringVar(value="")
        self._color_var = tk.StringVar(value="")

        # 窗口关闭按钮 -> 退出程序
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        # ---- 状态区 ----
        status_frame = ttk.LabelFrame(frm, text="状态", padding=8)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(
            status_frame, textvariable=self._status_var,
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(anchor="w")

        info_frame = ttk.Frame(status_frame)
        info_frame.pack(fill="x", pady=(4, 0))
        ttk.Label(info_frame, textvariable=self._mode_var, foreground="gray").pack(anchor="w")
        ttk.Label(info_frame, textvariable=self._roi_var, foreground="gray").pack(anchor="w")
        ttk.Label(info_frame, textvariable=self._params_var, foreground="gray").pack(anchor="w")

        # 颜色显示行：色块预览 + RGB 数值
        color_frame = ttk.Frame(info_frame)
        color_frame.pack(anchor="w", pady=(2, 0))
        self._color_preview = tk.Canvas(
            color_frame, width=20, height=20, highlightthickness=1,
            highlightbackground="gray",
        )
        self._color_preview.pack(side="left")
        ttk.Label(
            color_frame, textvariable=self._color_var, foreground="gray",
        ).pack(side="left", padx=(6, 0))

        # ---- 操作按钮区 ----
        btn_frame = ttk.LabelFrame(frm, text="操作", padding=8)
        btn_frame.pack(fill="x", pady=(0, 8))

        # 第一行：开始/暂停 + 停止
        row1 = ttk.Frame(btn_frame)
        row1.pack(fill="x", pady=2)
        self._btn_toggle = ttk.Button(
            row1, text="开始 (F6)", command=self._on_toggle, width=18,
        )
        self._btn_toggle.pack(side="left", padx=2)
        ttk.Button(
            row1, text="停止 (F7)", command=self._on_stop, width=18,
        ).pack(side="left", padx=2)

        # 第二行：选区域 + 测试
        row2 = ttk.Frame(btn_frame)
        row2.pack(fill="x", pady=2)
        ttk.Button(
            row2, text="选区域 (F8)", command=self._on_select_region, width=18,
        ).pack(side="left", padx=2)
        ttk.Button(
            row2, text="测试 (F9)", command=self._on_test, width=18,
        ).pack(side="left", padx=2)

        # 第三行：截取咬钩（合并取色+模板）
        row3 = ttk.Frame(btn_frame)
        row3.pack(fill="x", pady=2)
        ttk.Button(
            row3, text="截取咬钩 (F10)", command=self._on_capture_template, width=18,
        ).pack(side="left", padx=2)
        ttk.Label(
            row3, text="取色+模板一次完成", foreground="gray",
        ).pack(side="left", padx=4)

        # 第四行：选择检测点 B (阶段一双检测点)
        row4 = ttk.Frame(btn_frame)
        row4.pack(fill="x", pady=2)
        ttk.Button(
            row4, text="检测点B (F12)", command=self._on_select_region_b, width=18,
        ).pack(side="left", padx=2)
        ttk.Label(
            row4, text="启用双检测点联合确认", foreground="gray",
        ).pack(side="left", padx=4)

        # ---- 快捷键提示 ----
        tip = (
            "快捷键: F6 开始/暂停 | F7 停止 | F8 选区域 | F9 测试 | F10 截取咬钩 | F12 检测点B\n"
            "失焦保护: 切到其他窗口自动暂停，切回 Minecraft 等 3 秒自动恢复"
        )
        ttk.Label(frm, text=tip, foreground="gray", justify="left").pack(anchor="w")

        self._update_info_text()
        return root

    # ---------------- 按钮回调 ----------------

    def _on_toggle(self) -> None:
        """开始/暂停按钮：手动操作时清除自动暂停标记"""
        self._app._auto_paused = False
        self._app._on_toggle()

    def _on_stop(self) -> None:
        self._app._auto_paused = False
        self._app._on_stop()

    def _on_select_region(self) -> None:
        self._app._task_queue.put("select_region")

    def _on_test(self) -> None:
        self._app._task_queue.put("test")

    def _on_capture_template(self) -> None:
        self._app._task_queue.put("capture_template")

    def _on_select_region_b(self) -> None:
        self._app._task_queue.put("select_region_b")

    def _on_close(self) -> None:
        self._app._shutdown()

    # ---------------- 主循环 (after 驱动) ----------------

    def tick(self) -> None:
        """每次 after 回调执行的主循环逻辑"""
        app = self._app
        root = self._root

        # 1. 处理任务队列：每个任务用 after(0, ...) 投递到事件队列，
        #    避免阻塞 tick (框选/取色是同步阻塞操作)。
        #    任意任务异常都不能打断 after 调度链。
        try:
            while True:
                task = app._task_queue.get_nowait()
                # 投递到事件队列，由 mainloop 在 tick 之外调度
                root.after(0, self._dispatch_task_safe, task)
        except queue.Empty:
            pass
        except Exception as exc:
            app._log.error(f"任务队列处理异常: {exc}")

        # 2. 定期检查 Minecraft 窗口有效性
        try:
            now = time.monotonic()
            if now - self._last_window_check >= WINDOW_CHECK_INTERVAL_MS / 1000.0:
                self._last_window_check = now
                self._check_window_valid()

            # 3. 定期检查窗口焦点 (失焦自动暂停)
            if now - self._last_focus_check >= FOCUS_CHECK_INTERVAL_MS / 1000.0:
                self._last_focus_check = now
                self._check_focus()

            # 4. 更新状态显示
            self._update_status()
        except Exception as exc:
            app._log.error(f"tick 状态检查异常: {exc}")

        # 5. 安排下一次 tick (必须执行，即使上面出错)
        if root is not None and root.winfo_exists():
            root.after(TICK_INTERVAL_MS, self.tick)

    def _dispatch_task_safe(self, task: str) -> None:
        """在事件队列中执行任务，单任务异常不影响其他任务和 tick 链"""
        try:
            if task == "select_region":
                self._app._do_select_region()
                self._update_info_text()
            elif task == "test":
                self._app._do_test()
            elif task == "capture_template":
                self._app._do_capture_template()
                self._update_info_text()
            elif task == "pick_color":
                self._app._do_pick_color()
                self._update_info_text()
            elif task == "select_region_b":
                self._app._do_select_region_b()
                self._update_info_text()
        except Exception as exc:
            self._app._log.error(f"任务执行失败 [{task}]: {exc}")

    def _check_window_valid(self) -> None:
        """Minecraft 窗口关闭则停止"""
        engine = self._app._engine
        if engine is not None and engine.is_running():
            if not self._app._mc.is_valid():
                self._app._log.error("检测到 Minecraft 窗口关闭，停止自动钓鱼")
                self._app._auto_paused = False
                engine.stop()

    def _check_focus(self) -> None:
        """
        焦点检查：Minecraft 失焦时强制暂停（无论处于哪个步骤），
        回焦后等待 FOCUS_RESUME_DELAY_S 秒（焦点稳定确认）才自动恢复暂停前的操作。
        仅对「自动暂停」的状态自动恢复；用户手动暂停 (F6/按钮) 不会自动恢复。
        """
        engine = self._app._engine
        if engine is None:
            return

        if not engine.is_running():
            # 引擎未运行，重置自动暂停标记和计时
            self._app._auto_paused = False
            self._focus_regain_time = 0.0
            return

        mc = self._app._mc
        if mc.is_foreground():
            # Minecraft 在前台
            if self._app._auto_paused:
                # 之前因失焦自动暂停了，开始/继续回焦计时
                if self._focus_regain_time == 0.0:
                    self._focus_regain_time = time.monotonic()
                    self._app._log.info(
                        f"Minecraft 回到前台，{FOCUS_RESUME_DELAY_S:.0f} 秒后自动恢复..."
                    )
                # 检查是否已稳定达到阈值
                elapsed = time.monotonic() - self._focus_regain_time
                if elapsed >= FOCUS_RESUME_DELAY_S:
                    # 焦点稳定足够久，恢复暂停前的操作
                    self._app._auto_paused = False
                    self._focus_regain_time = 0.0
                    engine.toggle_pause()
                    self._app._log.info("焦点已稳定，自动恢复自动钓鱼")
        else:
            # Minecraft 不在前台：强制暂停（无论当前在哪个步骤）
            self._focus_regain_time = 0.0  # 重置回焦计时
            if not engine.is_paused():
                # 正在运行但失焦 -> 立即强制暂停
                self._app._auto_paused = True
                engine.toggle_pause()
                self._app._log.info("Minecraft 失去焦点，强制暂停")

    # ---------------- 状态显示 ----------------

    def _update_status(self) -> None:
        """更新状态文字和按钮文字"""
        engine = self._app._engine
        if engine is None:
            self._status_var.set("未初始化")
            return

        if not engine.is_running():
            self._status_var.set("● 已停止")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="开始 (F6)")
        elif self._app._auto_paused:
            if self._focus_regain_time > 0.0:
                # 已回焦，正在倒计时恢复
                remaining = FOCUS_RESUME_DELAY_S - (time.monotonic() - self._focus_regain_time)
                self._status_var.set(
                    f"● 自动暂停 (回焦 {remaining:.1f}s 后恢复)"
                )
            else:
                self._status_var.set("● 自动暂停 (切回 Minecraft 恢复)")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="恢复 (F6)")
        elif engine.is_paused():
            self._status_var.set("● 已暂停")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="恢复 (F6)")
        else:
            self._status_var.set("● 运行中")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="暂停 (F6)")

    def _update_info_text(self) -> None:
        """更新检测模式、ROI、参数等信息"""
        det = self._app._config.detection
        roi = self._app._config.roi
        fish = self._app._config.fishing

        # 模式
        detector = self._app._detector
        if detector is not None:
            mode = detector.get_mode_name()
        else:
            mode = "未知"
        self._mode_var.set(f"检测模式: {mode}")

        # ROI
        self._roi_var.set(
            f"ROI: x={roi.x:.3f} y={roi.y:.3f} "
            f"w={roi.width:.3f} h={roi.height:.3f}"
        )

        # 双检测点状态 (阶段一)
        if det.use_dual_detection:
            roi_b = self._app._config.roi_b
            self._roi_var.set(
                self._roi_var.get()
                + f"  | 检测点B: x={roi_b.x:.3f} y={roi_b.y:.3f} "
                f"w={roi_b.width:.3f} h={roi_b.height:.3f} (双检测已启用)"
            )

        # 参数：拉鱼改为右键单击一次，不再需要持续时间/间隔参数
        self._params_var.set(
            f"咬钩: 右键单击收竿 | 抛竿延迟 {fish.recast_delay_min_ms}~{fish.recast_delay_max_ms}ms"
        )

        # 颜色：显示当前 target_color 的 RGB 值 + 色块预览
        tc = det.target_color  # (r, g, b)
        r, g, b = int(tc[0]), int(tc[1]), int(tc[2])
        if det.use_color_detection:
            self._color_var.set(f"目标颜色: RGB({r}, {g}, {b})  容差 ±{det.color_tolerance}")
        else:
            self._color_var.set(f"目标颜色: RGB({r}, {g}, {b})  (颜色检测未启用)")
        # 更新色块
        if self._color_preview is not None:
            color_hex = f"#{r:02x}{g:02x}{b:02x}"
            self._color_preview.delete("all")
            self._color_preview.create_rectangle(
                0, 0, 20, 20, fill=color_hex, outline=""
            )

    def destroy(self) -> None:
        """销毁窗口"""
        if self._root is not None and self._root.winfo_exists():
            self._root.destroy()
        self._root = None
