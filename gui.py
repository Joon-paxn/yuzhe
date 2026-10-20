"""Restrained native desktop UI, with separate live, settings and history views."""
from __future__ import annotations
import os
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import cv2

from config import CONFIG_PATH, save_config, validate
from alert_manager import AlertState
from game_log import discover_logs

BG = "#f4f6f7"
WHITE = "#ffffff"
TEXT = "#263239"
MUTED = "#6b7981"
TEAL = "#187d70"
RED = "#b23d3d"
STATES = {"IDLE": "就绪", "CASTING": "抛竿中", "WAITING_BITE": "等待上钩",
          "BITE_CANDIDATE": "Title 确认中", "BITE_CONFIRMED": "已确认上钩", "REELING": "收杆中",
          "WAITING_RESULT": "等待鱼获确认", "SUCCESS": "鱼获成功", "FAILURE": "钓鱼异常", "UNKNOWN": "未确认鱼获"}
ERRORS = {"SPOT_DEPLETED": "钓点已枯竭", "NO_FISH_ACTIVITY": "钓点没有鱼群活动",
          "ROD_BROKEN": "鱼竿已损坏", "DISCONNECTED": "游戏连接中断"}
EVENT_LABELS = {
    "bite": "上钩记录", "log": "游戏日志", "error": "错误", "alert": "报警",
    "SUCCESS": "钓获成功", "SPOT_DEPLETED": "钓点枯竭", "NO_FISH_ACTIVITY": "无鱼群活动",
    "ROD_BROKEN": "鱼竿损坏", "DISCONNECTED": "连接中断",
    "PASS_CARD": "获得通行证",
}
RESULT_LABELS = {
    "PENDING": "等待确认", "SUCCESS": "成功", "FAILURE": "失败", "UNKNOWN": "未确认",
    "": "—", None: "—",
}


def event_label(record):
    kind = record.get("kind") or record.get("type", "")
    return EVENT_LABELS.get(kind, ERRORS.get(kind, kind or "其他事件"))


def result_label(value):
    return RESULT_LABELS.get(value, value or "—")


class AlertBubble:
    """Persistent, non-modal alert shown at the primary monitor's top-right."""

    BG = "#FFF0E9"

    def __init__(self, root, acknowledge):
        self.root = root
        self.acknowledge = acknowledge
        self.window = None
        self._alert_id = None

    def _work_area(self):
        # SPI_GETWORKAREA returns the primary monitor work area and avoids the taskbar.
        try:
            import ctypes
            class Rect(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = Rect()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError):
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def show(self, alert):
        if self.window is None or not self.window.winfo_exists():
            self.window = tk.Toplevel(self.root)
            self.window.title("AutoFishing 异常")
            self.window.overrideredirect(True)
            self.window.configure(bg=self.BG, highlightthickness=1,
                                  highlightbackground="#e6a58f")
            self.window.protocol("WM_DELETE_WINDOW", self.close)
            self.window.wm_attributes("-topmost", True)
            self.window.columnconfigure(0, weight=1)
            self.window.rowconfigure(1, weight=1)
            header = tk.Frame(self.window, bg=self.BG, padx=18, pady=14)
            header.grid(row=0, column=0, sticky="ew")
            self.title = tk.Label(header, bg=self.BG, fg=RED,
                                  font=("Microsoft YaHei UI", 15, "bold"), anchor="w")
            self.title.pack(side="left", fill="x", expand=True)
            tk.Button(header, text="×", command=self.close, relief="flat", borderwidth=0,
                      bg=self.BG, fg=MUTED, activebackground=self.BG,
                      font=("Segoe UI", 16), cursor="hand2").pack(side="right")
            # Tk Label's internal padding accepts one distance; tuple padding is
            # only valid for geometry managers such as grid/pack. Passing
            # ``(0, 14)`` here is interpreted as the invalid screen distance
            # "0 14" and causes intermittent UI update errors.
            self.body = tk.Label(self.window, bg=self.BG, fg=TEXT, justify="left", anchor="nw",
                                 padx=18, pady=14, wraplength=350)
            self.body.grid(row=1, column=0, sticky="nsew")
            tk.Button(self.window, text="确认并停止报警", command=self.close,
                      bg="#d96f55", fg="white", activebackground="#bd5940",
                      activeforeground="white", relief="flat", padx=12, pady=7,
                      cursor="hand2").grid(row=2, column=0, sticky="e", padx=18, pady=(0, 14))
        self._alert_id = alert.get("id")
        self.title.configure(text=ERRORS.get(alert.get("kind"), alert.get("kind", "异常")))
        self.body.configure(text=(f"类型：{alert.get('kind', '')}\n"
                                  f"检测时间：{alert.get('clock', alert.get('timestamp', ''))}\n"
                                  f"原始日志：{alert.get('raw', '')[:260]}\n"
                                  f"最近一次鱼获：{alert.get('last_fish', '暂无鱼获')}"))
        left, top, right, bottom = self._work_area()
        width, height, margin = 410, 250, 20
        self.window.geometry(f"{width}x{height}+{max(left, right - width - margin)}+{top + margin}")
        self.window.deiconify()
        self.window.lift()

    def close(self):
        try:
            if self.window is not None and self.window.winfo_exists():
                self.acknowledge()
                self.window.withdraw()
        except tk.TclError:
            self.window = None

    def hide(self):
        try:
            if self.window is not None and self.window.winfo_exists():
                self.window.withdraw()
        except tk.TclError:
            self.window = None

    def destroy(self):
        try:
            if self.window is not None and self.window.winfo_exists():
                self.window.destroy()
        except tk.TclError:
            pass
        self.window = None


class FishingGUI:
    def __init__(self, app):
        self.app = app
        self.root = None
        self._preview_image = None
        self._last_frame = None
        self._last_history = None
        self._last_history_query = None
        self._last_alert = None
        self._vars = {}
        self._debug_photos = {}
        self._debug_revision = None
        self._debug_visible = False
        self._dev_clicks = 0
        self._dev_click_started = 0.0
        self.alert_bubble = None

    def build(self):
        root = tk.Tk()
        self.root = root
        root.title("AutoFishing · Title 识别")
        root.geometry(self.app.config.ui.window_geometry or "1060x790")
        root.minsize(900, 680)
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("White.TFrame", background=WHITE)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("White.TLabel", background=WHITE, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TButton", padding=(13, 7), background=WHITE, borderwidth=1)
        style.map("TButton", background=[("active", "#e6ecee")])
        style.configure("Accent.TButton", background=TEAL, foreground=WHITE)
        style.map("Accent.TButton", background=[("active", "#12675d")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 10), background=BG)
        style.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", TEAL)])
        style.configure("Treeview", rowheight=32, background=WHITE, fieldbackground=WHITE, foreground=TEXT)
        style.configure("Treeview.Heading", padding=7, background="#e8edef")
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        self.notice = tk.StringVar(value="就绪")
        self.status = tk.StringVar(value="就绪")
        self.detail = tk.StringVar(value="")
        self.window_status = tk.StringVar(value="")
        self.ocr_status = tk.StringVar(value="")
        self.log_status = tk.StringVar(value="")
        self.metrics = [tk.StringVar(value="0") for _ in range(4)]

        header = ttk.Frame(root, padding=(24, 18))
        header.grid(row=0, column=0, sticky="ew")
        self.brand_label = ttk.Label(header, text="AutoFishing", font=("Segoe UI", 23, "bold"), cursor="hand2")
        self.brand_label.pack(side="left")
        self.brand_label.bind("<Button-1>", self._developer_click)
        ttk.Label(header, text="TITLE / LOG", foreground=TEAL, font=("Segoe UI", 10, "bold")).pack(side="left", padx=16)
        ttk.Button(header, text="刷新游戏窗口", command=self.refresh_window).pack(side="right")
        ttk.Label(header, textvariable=self.window_status, style="Muted.TLabel").pack(side="right", padx=12)

        self.alert_band = tk.Frame(root, bg="#fff0e9", padx=24, pady=12)
        self.alert_title = tk.Label(self.alert_band, bg="#fff0e9", fg=RED,
                                    font=("Microsoft YaHei UI", 13, "bold"), anchor="w")
        self.alert_title.pack(fill="x")
        self.alert_text = tk.Label(self.alert_band, bg="#fff0e9", fg=TEXT, anchor="w",
                                   justify="left", wraplength=840)
        self.alert_text.pack(side="left", fill="x", expand=True, pady=4)
        self.alert_text.bind("<Configure>", lambda e: self.alert_text.configure(wraplength=max(100, e.width)))
        ttk.Button(self.alert_band, text="停止报警", command=self.app.alerts.acknowledge).pack(side="right", padx=(12, 0))
        self.alert_bubble = AlertBubble(root, self.app.alerts.acknowledge)

        self.tabs = ttk.Notebook(root)
        self.tabs.grid(row=2, column=0, sticky="nsew", padx=24)
        live = ttk.Frame(self.tabs, padding=(0, 16))
        debug = ttk.Frame(self.tabs, padding=(8, 10))
        settings = ttk.Frame(self.tabs)
        history = ttk.Frame(self.tabs, padding=(0, 16))
        self.debug_page = debug
        self.live_page = live
        self.tabs.add(live, text="实时监控")
        self.tabs.add(settings, text="识别与设置")
        self.tabs.add(history, text="鱼获与事件")
        self._build_live(live)
        self._build_debug(debug)
        self._build_settings(settings)
        self._build_history(history)
        footer = ttk.Frame(root, padding=(24, 10))
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.notice, style="Muted.TLabel").pack(side="left")
        return root

    def _developer_click(self, _event=None):
        now = time.monotonic()
        if now - self._dev_click_started > 1.5:
            self._dev_clicks = 0
        self._dev_click_started = now
        self._dev_clicks += 1
        if self._dev_clicks < 5 or self._debug_visible:
            return
        self._debug_visible = True
        self.tabs.insert(1, self.debug_page, text="OCR 调试")
        self.notice.set("开发者模式已开启：OCR 调试页可用")

    def _build_debug(self, parent):
        outer = parent
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        scroll_canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=bar.set)
        scroll_canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        parent = ttk.Frame(scroll_canvas)
        window_id = scroll_canvas.create_window((0, 0), window=parent, anchor="nw")
        parent.bind("<Configure>", lambda _: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind("<Configure>", lambda e: scroll_canvas.itemconfigure(window_id, width=e.width))
        self.debug_canvas = scroll_canvas
        parent.columnconfigure(0, weight=1)
        images = ttk.Frame(parent)
        images.grid(row=0, column=0, sticky="ew")
        self.debug_canvases = {}
        for col, (name, label) in enumerate((("raw", "原始 Title ROI"), ("enlarged", "放大 OCR 图像"),
                                            ("best", "最佳预处理结果"))):
            images.columnconfigure(col, weight=1, uniform="debug")
            host = ttk.Frame(images, padding=(4, 0))
            host.grid(row=0, column=col, sticky="ew")
            ttk.Label(host, text=label).pack(anchor="w", pady=(0, 5))
            canvas = tk.Canvas(host, width=240, height=150, bg="#23292b", highlightthickness=0)
            canvas.pack(fill="x")
            canvas.bind("<Configure>", lambda _: self.draw_debug())
            self.debug_canvases[name] = canvas
        evidence = ttk.Frame(parent, padding=(4, 12))
        evidence.grid(row=1, column=0, sticky="ew")
        self.debug_vars = {}
        for i, label in enumerate(("OCR 原始结果", "标准化结果", "匹配目标", "OCR 置信度", "最终匹配相似度", "当前检测状态")):
            row, col = divmod(i, 3)
            evidence.columnconfigure(col, weight=1, uniform="evidence")
            host = ttk.Frame(evidence, padding=(0, 5))
            host.grid(row=row, column=col, sticky="ew")
            ttk.Label(host, text=label, style="Muted.TLabel").pack(anchor="w")
            var = tk.StringVar(value="等待检测")
            self.debug_vars[label] = var
            label_widget = ttk.Label(host, textvariable=var, wraplength=240)
            label_widget.pack(anchor="w", fill="x")
            label_widget.bind("<Configure>", lambda e, w=label_widget: w.configure(wraplength=max(80, e.width)))
        self.variant_table = ttk.Treeview(parent, columns=("name", "raw", "normal", "confidence", "similarity", "match"),
                                          show="headings", height=7)
        for name, label, width in (("name", "预处理", 125), ("raw", "原始文字", 160), ("normal", "标准化", 130),
                                   ("confidence", "置信度", 90), ("similarity", "相似度", 90), ("match", "匹配", 75)):
            self.variant_table.heading(name, text=label)
            self.variant_table.column(name, width=width, minwidth=55, stretch=name in ("raw", "normal"))
        self.variant_table.grid(row=2, column=0, sticky="nsew")
        tools = ttk.Frame(outer, padding=(0, 10))
        tools.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(tools, text="测试 Title", command=self.app.test).pack(side="left")
        ttk.Button(tools, text="导入截图测试", command=self.import_sample).pack(side="left", padx=8)
        ttk.Button(tools, text="保存调试样本", command=self.app.save_debug_sample).pack(side="left")
        self.sample_fullscreen = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="导入图使用已保存 ROI", variable=self.sample_fullscreen).pack(side="right")

    def import_sample(self):
        path = filedialog.askopenfilename(title="选择 Title ROI 或游戏截图", filetypes=[("截图", "*.png *.jpg *.jpeg *.bmp")])
        if path: self.app.test_image(path, self.sample_fullscreen.get())

    def draw_debug(self):
        _, images = self.app.detector.debug_snapshot()
        for name, canvas in self.debug_canvases.items():
            canvas.delete("all")
            frame = images.get(name)
            w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
            if frame is None:
                canvas.create_text(w/2, h/2, text="暂无图像", fill="#b7c4c9")
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB if frame.ndim == 2 else cv2.COLOR_BGR2RGB)
            preview = Image.fromarray(rgb)
            preview.thumbnail((max(1, w-12), max(1, h-12)), Image.Resampling.NEAREST)
            self._debug_photos[name] = ImageTk.PhotoImage(preview, master=self.root)
            canvas.create_image(w/2, h/2, image=self._debug_photos[name])

    def update_debug(self):
        result, _ = self.app.detector.debug_snapshot()
        values = (result["text"] or "未识别到文字", result["normalized"] or "空", result["target"] or "咬钩 / 上钩",
                  f'{result["confidence"]:.1%}', f'{result["similarity"]:.1%}',
                  f'{result["detection_state"]} · {result["reason"]}')
        for var, value in zip(self.debug_vars.values(), values): var.set(value)
        revision = (result["variants"], result["reason"])
        if revision != self._debug_revision:
            self._debug_revision = revision
            self.draw_debug()
            self.variant_table.delete(*self.variant_table.get_children())
            for row in result["variants"]:
                self.variant_table.insert("", "end", values=(row["name"], row["text"] or "空", row["normalized"],
                    f'{row["confidence"]:.1%}', f'{row["similarity"]:.1%}', "是" if row["matched"] else "否"))

    def _build_live(self, parent):
        tools = ttk.Frame(parent, padding=(0, 10))
        tools.pack(side="bottom", fill="x")
        ttk.Button(tools, text="选择 Title 区域", command=lambda: self.app.select_roi()).pack(side="left")
        ttk.Button(tools, text="测试 Title", command=self.app.test).pack(side="left", padx=8)
        ttk.Button(tools, text="刷新预览", command=lambda: self.app.test(True)).pack(side="left")
        run = ttk.Frame(parent, style="White.TFrame", padding=20)
        run.pack(fill="x")
        left = ttk.Frame(run, style="White.TFrame")
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, textvariable=self.status, style="White.TLabel", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(left, textvariable=self.detail, style="White.TLabel", wraplength=580).pack(anchor="w", pady=(8, 0))
        self.start_button = ttk.Button(run, text="开始钓鱼", style="Accent.TButton", command=self.app.toggle)
        self.start_button.pack(side="right", padx=(10, 0))
        ttk.Button(run, text="停止", command=self.app.engine.stop).pack(side="right")
        scroll_host = ttk.Frame(parent)
        scroll_host.pack(fill="both", expand=True)
        live_canvas = tk.Canvas(scroll_host, background=BG, highlightthickness=0)
        live_bar = ttk.Scrollbar(scroll_host, orient="vertical", command=live_canvas.yview)
        live_canvas.configure(yscrollcommand=live_bar.set)
        live_bar.pack(side="right", fill="y")
        live_canvas.pack(side="left", fill="both", expand=True)
        parent = ttk.Frame(live_canvas)
        live_id = live_canvas.create_window((0, 0), window=parent, anchor="nw")
        parent.bind("<Configure>", lambda _: live_canvas.configure(scrollregion=live_canvas.bbox("all")))
        live_canvas.bind("<Configure>", lambda e: live_canvas.itemconfigure(live_id, width=e.width))
        self.live_canvas = live_canvas
        metrics = ttk.Frame(parent, padding=(0, 18))
        metrics.pack(fill="x")
        for i, label in enumerate(("Title 触发", "确认鱼获", "未确认", "结果确认率")):
            col = ttk.Frame(metrics)
            col.pack(side="left", fill="x", expand=True)
            ttk.Label(col, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(col, textvariable=self.metrics[i], font=("Segoe UI", 22, "bold")).pack(anchor="w")
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text="Title 区域", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(body, text="识别证据", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=1, sticky="w", padx=20, pady=(0, 10))
        self.preview = tk.Canvas(body, bg="#23292b", highlightthickness=0, height=280, width=480)
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda _: self.draw_preview())
        evidence = ttk.Frame(body, padding=(20, 0))
        evidence.grid(row=1, column=1, sticky="nsew")
        self.title_text = tk.StringVar(value="等待 OCR")
        self.confidence = tk.StringVar(value="")
        self.roi_text = tk.StringVar(value="")
        for label, var in (("识别文字", self.title_text), ("置信度 / 相似度", self.confidence),
                           ("ROI", self.roi_text), ("OCR", self.ocr_status), ("latest.log", self.log_status)):
            ttk.Label(evidence, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 2))
            ttk.Label(evidence, textvariable=var, wraplength=330).pack(anchor="w", pady=(0, 12))

    def _field(self, parent, row, label, section, name, kind=float):
        value = getattr(getattr(self.app.config, section), name)
        var = tk.StringVar(value=str(value))
        self._vars[(section, name)] = (var, kind)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 24), pady=7)
        ttk.Entry(parent, textvariable=var, width=28).grid(row=row, column=1, sticky="ew", pady=7)

    def _build_settings(self, parent):
        settings_page = parent
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=(8, 18))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        def scroll(e):
            selected = self.tabs.nametowidget(self.tabs.select())
            if selected is settings_page:
                canvas.yview_scroll(-int(e.delta / 120), "units")
            elif self._debug_visible and selected is self.debug_page:
                self.debug_canvas.yview_scroll(-int(e.delta / 120), "units")
            elif selected is self.live_page:
                self.live_canvas.yview_scroll(-int(e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", scroll)
        inner.columnconfigure(1, weight=1)
        row = 0
        for heading, entries in (
            ("Title OCR", [("附加目标 Title", "title", "target_text", str), ("预处理放大倍数", "title", "preprocess_scale", int), ("文字相似度阈值", "title", "ocr_similarity_threshold", float),
             ("OCR 置信度阈值", "title", "ocr_confidence_threshold", float), ("连续确认帧数", "title", "title_confirm_frames", int),
             ("确认时间窗 (ms)", "title", "title_confirm_window_ms", int), ("候选超时 (ms)", "title", "title_candidate_timeout", int),
             ("上钩冷却 (ms)", "title", "bite_cooldown", int), ("消失确认帧数", "title", "clear_frames", int),
             ("截图间隔 (ms)", "title", "screenshot_interval_ms", int)]),
            ("钓鱼与结果", [("重新抛竿最短延迟 (ms)", "fishing", "recast_delay_min_ms", int),
             ("重新抛竿最长延迟 (ms)", "fishing", "recast_delay_max_ms", int), ("结果等待超时 (ms)", "fishing", "result_timeout", int),
             ("游戏窗口标题", "window", "title_keyword", str)]),
            ("持续报警", [("声音频率 (Hz)", "alert", "sound_frequency", int), ("声音时长 (ms)", "alert", "sound_duration_ms", int),
             ("重复提醒间隔 (ms)", "alert", "repeat_interval_ms", int)]),
            ("用户操作保护", [("WASD 触发阈值 (ms)", "user_activity", "threshold_ms", int),
             ("检测轮询间隔 (ms)", "user_activity", "poll_interval_ms", int)])):
            ttk.Label(inner, text=heading, font=("Microsoft YaHei UI", 12, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(15, 7))
            row += 1
            for label, section, name, kind in entries:
                self._field(inner, row, label, section, name, kind)
                row += 1
        self.sound_enabled = tk.BooleanVar(value=self.app.config.alert.sound_enabled)
        self.alert_enabled = tk.BooleanVar(value=self.app.config.alert.alert_enabled)
        self.stop_unknown = tk.BooleanVar(value=self.app.config.fishing.stop_on_unknown)
        self.initial_cast = tk.BooleanVar(value=self.app.config.fishing.initial_cast)
        self.bobber_enabled = tk.BooleanVar(value=self.app.config.bobber.enabled)
        self.log_enabled = tk.BooleanVar(value=self.app.config.game_log.enabled)
        self.activity_enabled = tk.BooleanVar(value=self.app.config.user_activity.enabled)
        for label, var in (("开启持续报警", self.alert_enabled), ("开启声音提醒", self.sound_enabled),
                           ("未确认鱼获时停止钓鱼", self.stop_unknown), ("开始时自动抛竿", self.initial_cast),
                           ("鱼漂辅助统计", self.bobber_enabled), ("监听 latest.log", self.log_enabled)):
            ttk.Checkbutton(inner, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
            row += 1
        ttk.Checkbutton(inner, text="检测用户 WASD 操作并自动暂停", variable=self.activity_enabled).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=5)
        row += 1
        ttk.Button(inner, text="选择鱼漂区域", command=lambda: self.app.select_roi(True)).grid(row=row, column=0, sticky="w", pady=10)
        row += 1
        ttk.Label(inner, text="latest.log", font=("Microsoft YaHei UI", 12, "bold")).grid(row=row, column=0, sticky="w", pady=(18, 8))
        row += 1
        self.log_path = tk.StringVar(value=self.app.config.game_log.path)
        path_row = ttk.Frame(inner)
        path_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Entry(path_row, textvariable=self.log_path).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="选择文件", command=self.choose_log).pack(side="left", padx=8)
        ttk.Button(path_row, text="自动查找", command=self.auto_log).pack(side="left")
        row += 1
        actions = ttk.Frame(inner)
        actions.grid(row=row, column=0, columnspan=2, sticky="e", pady=20)
        ttk.Button(actions, text="重置设置", command=self.reset_settings).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存设置", style="Accent.TButton", command=self.save_settings).pack(side="left")

    def _build_history(self, parent):
        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", pady=(0, 10))
        ttk.Label(search_row, text="查询鱼获：").pack(side="left")
        self.history_query = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self.history_query)
        search_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        search_entry.bind("<Escape>", lambda _event: self.history_query.set(""))
        ttk.Button(search_row, text="清空查询", command=lambda: self.history_query.set("")).pack(side="left")
        ttk.Label(search_row, text="支持鱼名、时间、事件、结果和日志内容模糊匹配", style="Muted.TLabel").pack(side="left", padx=(10, 0))
        self.history = ttk.Treeview(parent, columns=("time", "type", "text", "result"), show="headings")
        for name, label, width in (("time", "时间", 150), ("type", "事件类型", 135), ("text", "鱼获 / 内容", 410), ("result", "处理结果", 150)):
            self.history.heading(name, text=label)
            self.history.column(name, width=width, minwidth=90, stretch=name=="text")
        bar = ttk.Scrollbar(parent, orient="vertical", command=self.history.yview)
        self.history.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.history.pack(fill="both", expand=True)
        self.history.bind("<Double-1>", self.show_record)
        self._records = {}
        history_actions = ttk.Frame(parent)
        history_actions.pack(fill="x", pady=(10, 0))
        ttk.Button(history_actions, text="重置数据", command=self.reset_data).pack(side="right")

    def reset_data(self):
        if not messagebox.askyesno("重置数据", "确定清空累计钓鱼数据和全部历史记录吗？此操作无法撤销。",
                                   parent=self.root):
            return
        try:
            self.app.store.reset()
            self._last_history = None
            self._last_history_query = None
            self.notice.set("钓鱼数据和历史记录已重置")
        except (OSError, RuntimeError) as exc:
            self.notice.set(f"重置数据失败：{exc}")

    def refresh_window(self):
        self.notice.set("游戏窗口已连接" if self.app.find_window() else "未找到 Minecraft 窗口")

    def choose_log(self):
        path = filedialog.askopenfilename(title="选择游戏 latest.log", filetypes=[("游戏日志", "*.log"), ("全部文件", "*.*")])
        if path: self.log_path.set(path)

    def auto_log(self):
        candidates = discover_logs()
        if candidates: self.log_path.set(str(candidates[0]))
        else: self.notice.set("常见游戏目录没有找到 latest.log，请选择文件")

    def _collect_settings(self, allow_running=False):
        if ((not allow_running and self.app.engine.is_running())
                or (not allow_running and self.app._test_thread and self.app._test_thread.is_alive())):
            self.notice.set("请先停止钓鱼和检测测试再保存设置")
            return False
        try:
            parsed = {(section, name): kind(var.get()) for (section, name), (var, kind) in self._vars.items()}
        except ValueError:
            self.notice.set("设置包含无效数值")
            return False
        for (section, name), value in parsed.items(): setattr(getattr(self.app.config, section), name, value)
        cfg = self.app.config
        cfg.alert.alert_enabled, cfg.alert.sound_enabled = self.alert_enabled.get(), self.sound_enabled.get()
        cfg.fishing.stop_on_unknown, cfg.fishing.initial_cast = self.stop_unknown.get(), self.initial_cast.get()
        cfg.bobber.enabled, cfg.game_log.enabled = self.bobber_enabled.get(), self.log_enabled.get()
        cfg.user_activity.enabled = self.activity_enabled.get()
        cfg.game_log.path = self.log_path.get().strip()
        return True

    def _persist(self, allow_running=False, message=True):
        if not self._collect_settings(allow_running=allow_running):
            return False
        try:
            self.app.config.ui.window_geometry = self.root.geometry()
            save_config(CONFIG_PATH, self.app.config)
            self.app.game_log.reconnect()
            for (section, name), (var, _) in self._vars.items():
                var.set(str(getattr(getattr(self.app.config, section), name)))
            if message:
                self.notice.set("设置已保存")
            return True
        except (OSError, ValueError) as exc:
            self.notice.set(f"保存失败：{exc}")
            return False

    def save_settings(self):
        self._persist()

    def reset_settings(self):
        if messagebox.askyesno("重置设置", "将恢复所有设置为默认值并保存，是否继续？", parent=self.root):
            self.app.reset_config()

    def refresh_settings_from_config(self):
        cfg = self.app.config
        for (section, name), (var, _) in self._vars.items():
            var.set(str(getattr(getattr(cfg, section), name)))
        self.sound_enabled.set(cfg.alert.sound_enabled)
        self.alert_enabled.set(cfg.alert.alert_enabled)
        self.stop_unknown.set(cfg.fishing.stop_on_unknown)
        self.initial_cast.set(cfg.fishing.initial_cast)
        self.bobber_enabled.set(cfg.bobber.enabled)
        self.log_enabled.set(cfg.game_log.enabled)
        self.activity_enabled.set(cfg.user_activity.enabled)
        self.log_path.set(cfg.game_log.path)

    def draw_preview(self):
        canvas = self.preview
        canvas.delete("all")
        w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        if self._last_frame is None:
            canvas.create_text(w/2, h/2, text="暂无 Title 截图", fill="#b7c4c9", font=("Microsoft YaHei UI", 12))
            return
        image = Image.fromarray(cv2.cvtColor(self._last_frame, cv2.COLOR_BGR2RGB))
        image.thumbnail((max(1,w-24), max(1,h-24)), Image.Resampling.NEAREST)
        self._preview_image = ImageTk.PhotoImage(image, master=self.root)
        canvas.create_image(w/2, h/2, image=self._preview_image)

    def show_record(self, _):
        selection = self.history.selection()
        if not selection: return
        import json
        record = self._records[selection[0]]
        window = tk.Toplevel(self.root)
        window.title("事件详情")
        window.geometry("680x460")
        text = tk.Text(window, wrap="word", bg=WHITE, fg=TEXT, padx=18, pady=18)
        text.pack(fill="both", expand=True)
        translated = dict(record)
        translated["事件类型"] = event_label(record)
        if "result" in record:
            translated["处理结果"] = result_label(record.get("result"))
        text.insert("1.0", json.dumps(translated, ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def tick(self):
        try:
            while True:
                task = self.app.tasks.get_nowait()
                handlers = {"toggle": self.app.toggle, "roi": lambda: self.app.select_roi(), "test": self.app.test,
                            "snapshot": lambda: self.app.test(True), "bobber_roi": lambda: self.app.select_roi(True),
                            "test_done": lambda: self.notice.set("检测测试结束"),
                            "user_active": self.app.on_user_activity}
                if task in handlers: self.root.after(0, handlers[task])
        except queue.Empty: pass
        try:
            engine, detector = self.app.engine, self.app.detector
            self.status.set("已暂停" if engine.is_running() and engine.is_paused() else STATES.get(engine.state.value, engine.state.value))
            self.detail.set(engine.message)
            self.start_button.configure(text="恢复钓鱼" if engine.is_paused() else "暂停钓鱼" if engine.is_running() else "开始钓鱼")
            self.window_status.set("Minecraft 已连接" if self.app.window.is_valid() else "未连接游戏")
            result = detector.last
            self.title_text.set(result.text or "未识别到文字")
            self.confidence.set(f"{result.confidence:.1%} / {result.similarity:.1%} · {result.reason}")
            roi = self.app.config.roi
            self.roi_text.set(f"{roi.x:.3f}, {roi.y:.3f} · {roi.width:.3f} × {roi.height:.3f}")
            self.ocr_status.set(f"{detector.status} · {detector.latency_ms:.0f} ms")
            self.log_status.set(self.app.game_log.status)
            self.update_debug()
            frame = detector.preview()
            if frame is not None:
                self._last_frame = frame
                self.draw_preview()
            stats = self.app.store.stats()
            for var, value in zip(self.metrics, (stats["bites"], stats["success"], stats["unknown"], f'{stats["rate"]:.0%}')): var.set(value)
            state, alert = self.app.alerts.snapshot()
            alert_key = (state, alert["id"] if alert else None)
            if alert_key != self._last_alert:
                self._last_alert = alert_key
                if alert:
                    self.alert_band.grid(row=1, column=0, sticky="ew")
                    self.alert_title.configure(text=ERRORS.get(alert["kind"], alert["kind"]) + (" · 已确认" if state == AlertState.ACKNOWLEDGED else " · 异常报警"))
                    self.alert_text.configure(text=f'{alert["clock"]}  {alert["kind"]}\n{alert["raw"][:200]}\n最近鱼获：{alert["last_fish"]}')
                if state == AlertState.ALERT_ACTIVE and alert:
                    self.alert_bubble.show(alert)
                elif state != AlertState.ALERT_ACTIVE:
                    self.alert_bubble.hide()
            records = self.app.store.history()
            query = self.history_query.get().strip().casefold()
            if records != self._last_history or query != self._last_history_query:
                self._last_history = records
                self._last_history_query = query
                self.history.delete(*self.history.get_children())
                self._records.clear()
                matches = []
                for record in records:
                    text = record.get("fish") or record.get("text") or record.get("title_text") or record.get("reason", "")
                    searchable = " ".join(str(value) for value in (
                        record.get("timestamp", ""), event_label(record), result_label(record.get("result")),
                        record.get("fish", ""), record.get("text", ""), record.get("title_text", ""),
                        record.get("reason", ""), record.get("kind", ""), record.get("type", "")))
                    if not query or query in searchable.casefold():
                        matches.append((record, text))
                for record, text in reversed(matches[-250:]):
                    item = self.history.insert("", "end", values=(
                        record.get("timestamp", "")[11:23], event_label(record), text,
                        result_label(record.get("result"))))
                    self._records[item] = record
            if self.app.store.write_error: self.notice.set(f"历史保存失败：{self.app.store.write_error}")
        except Exception as exc:
            self.notice.set(f"界面更新异常：{exc}")
        if not self.app._closing and self.root.winfo_exists(): self.root.after(150, self.tick)

    def close(self):
        # Stop signals are immediate; join workers after mainloop returns.
        self._persist(allow_running=True, message=False)
        # Make the fish history durable before destroying the Tk root. The
        # application cleanup still closes the store and writes the exit
        # snapshot after worker threads have stopped.
        try:
            self.app.store.flush()
        except OSError as exc:
            self.notice.set(f"鱼获记录保存失败：{exc}")
        if self.alert_bubble:
            self.alert_bubble.destroy()
        self.app.engine.stop()
        self.app._test_stop.set()
        self.root.destroy()
