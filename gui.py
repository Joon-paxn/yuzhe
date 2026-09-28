"""
gui.py
------
自动钓鱼程序的图形界面。

职责：
- 显示当前运行状态 (运行/暂停/停止/自动暂停)
- 提供操作按钮：开始/暂停、停止、选区域、测试、截模板、取色
- 用 tkinter 的 after() 驱动主循环，替代原来的 while+sleep

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


TICK_INTERVAL_MS = 50
WINDOW_CHECK_INTERVAL_MS = 2000
FOCUS_CHECK_INTERVAL_MS = 300
FOCUS_RESUME_DELAY_S = 3.0
# 自适应状态刷新间隔 (ms)
ADAPTIVE_REFRESH_MS = 500


class FishingGUI:
    """自动钓鱼图形界面"""

    def __init__(self, app: "AutoFishingApp") -> None:
        self._app = app
        self._root: Optional[tk.Tk] = None

        self._last_window_check = 0.0
        self._last_focus_check = 0.0
        self._last_adaptive_check = 0.0
        self._focus_regain_time: float = 0.0

        self._status_var: Optional[tk.StringVar] = None
        self._mode_var: Optional[tk.StringVar] = None
        self._roi_var: Optional[tk.StringVar] = None
        self._params_var: Optional[tk.StringVar] = None
        self._color_var: Optional[tk.StringVar] = None
        self._adaptive_var: Optional[tk.StringVar] = None  # 自适应模型状态
        self._ocr_var: Optional[tk.StringVar] = None        # OCR 识别层状态

        self._btn_toggle: Optional[ttk.Button] = None
        self._color_preview: Optional[tk.Canvas] = None

    # ---------------- 构建界面 ----------------

    def build(self) -> tk.Tk:
        root = tk.Tk()
        root.title("Minecraft 自动钓鱼")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        self._root = root

        self._status_var = tk.StringVar(value="就绪")
        self._mode_var = tk.StringVar(value="")
        self._roi_var = tk.StringVar(value="")
        self._params_var = tk.StringVar(value="")
        self._color_var = tk.StringVar(value="")
        self._adaptive_var = tk.StringVar(value="")
        self._ocr_var = tk.StringVar(value="")

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

        # 自适应模型状态行
        ttk.Label(
            info_frame, textvariable=self._adaptive_var, foreground="#0066cc",
        ).pack(anchor="w", pady=(2, 0))

        # OCR 识别层状态行 (阶段一)
        ttk.Label(
            info_frame, textvariable=self._ocr_var, foreground="#6a3a8c",
        ).pack(anchor="w", pady=(2, 0))

        # ---- 操作按钮区 ----
        btn_frame = ttk.LabelFrame(frm, text="操作", padding=8)
        btn_frame.pack(fill="x", pady=(0, 8))

        row1 = ttk.Frame(btn_frame)
        row1.pack(fill="x", pady=2)
        self._btn_toggle = ttk.Button(
            row1, text="开始 (F6)", command=self._on_toggle, width=18,
        )
        self._btn_toggle.pack(side="left", padx=2)
        ttk.Button(
            row1, text="停止 (F7)", command=self._on_stop, width=18,
        ).pack(side="left", padx=2)

        row2 = ttk.Frame(btn_frame)
        row2.pack(fill="x", pady=2)
        ttk.Button(
            row2, text="选区域 (F8)", command=self._on_select_region, width=18,
        ).pack(side="left", padx=2)
        ttk.Button(
            row2, text="测试 (F9)", command=self._on_test, width=18,
        ).pack(side="left", padx=2)

        row3 = ttk.Frame(btn_frame)
        row3.pack(fill="x", pady=2)
        ttk.Button(
            row3, text="截取咬钩 (F10)", command=self._on_capture_template, width=18,
        ).pack(side="left", padx=2)
        ttk.Label(
            row3, text="双重取色 A+B + 模板", foreground="gray",
        ).pack(side="left", padx=4)

        tip = (
            "快捷键: F6 开始/暂停 | F7 停止 | F8 选区域 | F9 测试 | F10 截取咬钩\n"
            "失焦保护: 切到其他窗口自动暂停，切回 Minecraft 等 3 秒自动恢复"
        )
        ttk.Label(frm, text=tip, foreground="gray", justify="left").pack(anchor="w")

        self._update_info_text()
        return root

    # ---------------- 按钮回调 ----------------

    def _on_toggle(self) -> None:
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

    def _on_close(self) -> None:
        self._app._shutdown()

    # ---------------- 主循环 (after 驱动) ----------------

    def tick(self) -> None:
        app = self._app
        root = self._root

        try:
            while True:
                task = app._task_queue.get_nowait()
                root.after(0, self._dispatch_task_safe, task)
        except queue.Empty:
            pass
        except Exception as exc:
            app._log.error(f"任务队列处理异常: {exc}")

        try:
            now = time.monotonic()
            if now - self._last_window_check >= WINDOW_CHECK_INTERVAL_MS / 1000.0:
                self._last_window_check = now
                self._check_window_valid()
            if now - self._last_focus_check >= FOCUS_CHECK_INTERVAL_MS / 1000.0:
                self._last_focus_check = now
                self._check_focus()
            # 自适应模型状态刷新
            if now - self._last_adaptive_check >= ADAPTIVE_REFRESH_MS / 1000.0:
                self._last_adaptive_check = now
                self._update_adaptive_status()
                self._update_ocr_status()
            self._update_status()
        except Exception as exc:
            app._log.error(f"tick 状态检查异常: {exc}")

        if root is not None and root.winfo_exists():
            root.after(TICK_INTERVAL_MS, self.tick)

    def _dispatch_task_safe(self, task: str) -> None:
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
        except Exception as exc:
            self._app._log.error(f"任务执行失败 [{task}]: {exc}")
        finally:
            self._refresh_after_overlay()

    def _refresh_after_overlay(self) -> None:
        root = self._root
        if root is None or not root.winfo_exists():
            return
        try:
            root.update_idletasks()
            root.update()
            root.lift()
        except Exception:
            pass

    def _check_window_valid(self) -> None:
        engine = self._app._engine
        if engine is not None and engine.is_running():
            if not self._app._mc.is_valid():
                self._app._log.error("检测到 Minecraft 窗口关闭，停止自动钓鱼")
                self._app._auto_paused = False
                engine.stop()

    def _check_focus(self) -> None:
        engine = self._app._engine
        if engine is None:
            return
        if not engine.is_running():
            self._app._auto_paused = False
            self._focus_regain_time = 0.0
            return
        mc = self._app._mc
        if mc.is_foreground():
            if self._app._auto_paused:
                if self._focus_regain_time == 0.0:
                    self._focus_regain_time = time.monotonic()
                    self._app._log.info(
                        f"Minecraft 回到前台，{FOCUS_RESUME_DELAY_S:.0f} 秒后自动恢复..."
                    )
                elapsed = time.monotonic() - self._focus_regain_time
                if elapsed >= FOCUS_RESUME_DELAY_S:
                    self._app._auto_paused = False
                    self._focus_regain_time = 0.0
                    engine.toggle_pause()
                    self._app._log.info("焦点已稳定，自动恢复自动钓鱼")
        else:
            self._focus_regain_time = 0.0
            if not engine.is_paused():
                self._app._auto_paused = True
                engine.toggle_pause()
                self._app._log.info("Minecraft 失去焦点，强制暂停")

    # ---------------- 状态显示 ----------------

    def _update_status(self) -> None:
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
                remaining = FOCUS_RESUME_DELAY_S - (time.monotonic() - self._focus_regain_time)
                self._status_var.set(f"● 自动暂停 (回焦 {remaining:.1f}s 后恢复)")
            else:
                self._status_var.set("● 自动暂停 (切回 Minecraft 恢复)")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="恢复 (F6)")
        elif engine.is_paused():
            self._status_var.set("● 已暂停")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="恢复 (F6)")
        else:
            state_name = engine.get_state_name()
            if engine.get_state() is not None and "枯竭" in state_name:
                self._status_var.set(f"● 钓点枯竭 (已停止钓鱼)")
            elif engine.get_state() is not None and "移动" in state_name:
                self._status_var.set(f"● 运行中 ({state_name})")
            else:
                self._status_var.set(f"● 运行中 ({state_name})")
            if self._btn_toggle is not None:
                self._btn_toggle.config(text="暂停 (F6)")

    def _update_info_text(self) -> None:
        """更新检测模式、ROI、参数、颜色等信息"""
        det = self._app._config.detection
        roi = self._app._config.roi
        fish = self._app._config.fishing
        ada = self._app._config.adaptive_rgb

        detector = self._app._detector
        mode = detector.get_mode_name() if detector is not None else "未知"
        self._mode_var.set(f"检测模式: {mode}")

        self._roi_var.set(
            f"ROI: x={roi.x:.3f} y={roi.y:.3f} w={roi.width:.3f} h={roi.height:.3f}"
        )
        self._params_var.set(
            f"咬钩: 右键单击收竿 | 抛竿延迟 {fish.recast_delay_min_ms}~{fish.recast_delay_max_ms}ms"
        )

        tc = det.target_color
        r, g, b = int(tc[0]), int(tc[1]), int(tc[2])
        if det.use_dual_color:
            tcb = det.target_color_b
            rb, gb, bb = int(tcb[0]), int(tcb[1]), int(tcb[2])
            self._color_var.set(
                f"A:RGB({r},{g},{b})  B:RGB({rb},{gb},{bb})  容差 ±{det.color_tolerance}  (双重取色已启用)"
            )
        elif det.use_color_detection:
            self._color_var.set(f"目标颜色: RGB({r}, {g}, {b})  容差 ±{det.color_tolerance}")
        else:
            self._color_var.set(f"目标颜色: RGB({r}, {g}, {b})  (颜色检测未启用)")

        if self._color_preview is not None:
            self._color_preview.delete("all")
            if det.use_dual_color:
                tcb = det.target_color_b
                rb, gb, bb = int(tcb[0]), int(tcb[1]), int(tcb[2])
                self._color_preview.create_rectangle(
                    0, 0, 10, 20, fill=f"#{r:02x}{g:02x}{b:02x}", outline="",
                )
                self._color_preview.create_rectangle(
                    10, 0, 20, 20, fill=f"#{rb:02x}{gb:02x}{bb:02x}", outline="",
                )
            else:
                self._color_preview.create_rectangle(
                    0, 0, 20, 20, fill=f"#{r:02x}{g:02x}{b:02x}", outline=""
                )

    def _update_adaptive_status(self) -> None:
        """实时刷新自适应模型状态 (Reference/Samples/Tolerance)"""
        ada = self._app._config.adaptive_rgb
        if not ada.enabled:
            self._adaptive_var.set("")
            return
        detector = self._app._detector
        if detector is None:
            self._adaptive_var.set("")
            return
        snap = detector.get_adaptive_snapshot()
        sa, sb = snap["a"], snap["b"]
        a_ref = sa["reference"]
        b_ref = sb["reference"]
        a_ref_s = f"RGB{a_ref}" if a_ref else "未就绪"
        b_ref_s = f"RGB{b_ref}" if b_ref else "未就绪"
        a_tol = sa["tolerance"]
        b_tol = sb["tolerance"]
        a_ready = "✓" if sa["ready"] else "冷启动"
        b_ready = "✓" if sb["ready"] else "冷启动"
        self._adaptive_var.set(
            f"自适应 A: Ref={a_ref_s} N={sa['samples']} ±{a_tol[0]} [{a_ready}] | "
            f"B: Ref={b_ref_s} N={sb['samples']} ±{b_tol[0]} [{b_ready}]"
        )

    def _update_ocr_status(self) -> None:
        """实时刷新 OCR 识别层状态 (阶段一：仅显示，不接入钓鱼)"""
        ocr = self._app._ocr
        ocr_cfg = self._app._config.ocr
        if ocr is None:
            self._ocr_var.set("")
            return
        if not ocr_cfg.enabled:
            self._ocr_var.set("OCR: 未启用 (ocr.enabled=false)")
            return
        if not ocr.engine_available:
            self._ocr_var.set(f"OCR: 引擎不可用 ({ocr.engine_name})，需安装 rapidocr")
            return
        state = ocr.get_latest_state()
        result = ocr.get_latest()
        parts = [f"OCR: {ocr.engine_name}"]
        if ocr.is_running:
            parts.append("运行中")
        if state.bite_detected:
            parts.append("咬钩✓")
        if state.depleted:
            parts.append("钓点枯竭✓")
        if state.xyz is not None:
            parts.append(f"XYZ={state.xyz}")
        if result.available:
            parts.append(f"文本={result.full_text!r}")
        elif result.error:
            parts.append(f"({result.error})")
        self._ocr_var.set(" | ".join(parts))

    def destroy(self) -> None:
        if self._root is not None and self._root.winfo_exists():
            self._root.destroy()
        self._root = None
