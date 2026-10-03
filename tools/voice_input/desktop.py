"""桌面轻量免激活悬浮胶囊客户端 (DesktopVoiceWidget)：
- 基于 Windows 原生 Tkinter + Win32 WS_EX_NOACTIVATE 扩展样式
- 零焦点抢夺：点击悬浮麦克风不会夺走目标窗口 (VS Code/终端/记事本) 的光标焦点！
- Win32 GDI 圆角剪裁 (CreateRoundRectRgn)：消除生硬方框，呈现现代圆角胶囊 (Pill) 造型
- 动态 ASR 模型徽章 (Badge)：实时显示 SenseVoice / Qwen 1.7B 当前激活引擎
- 竖向展开流式抽屉 (Vertical Streaming Drawer)：出字时向下自然展开多行文本气泡
- 动态音量波形动画 (基于实时音频 RMS 电平)
- 自由拖动吸附与记忆屏幕坐标
- 异常焦点保护与一键补贴重试
"""

import ctypes
import math
import os
import sys
import threading
import time
import tkinter as tk
from typing import Callable, Optional

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    Image = None
    ImageDraw = None
    ImageTk = None
    HAS_PIL = False

from .inject import InjectionOutcome, InjectionResult, set_clipboard_text
from .session import SessionCoordinator, SessionState

# Win32 常量定义
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3

user32 = ctypes.windll.user32


class DesktopVoiceWidget:
    def __init__(
        self,
        coordinator: SessionCoordinator,
        default_x: int = -1,
        default_y: int = -1,
        on_close: Optional[Callable[[], None]] = None,
    ):
        self.coordinator = coordinator
        self.root: Optional[tk.Tk] = None
        self._is_running = False
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._dragged = False
        self._current_audio_level = 0.0
        self._on_close = on_close
        self._feedback_until = 0.0
        self._correction_suggestion = ""
        self._resource_ui_key = None

        # 浮窗尺寸 (固定胶囊宽度，竖向伸缩展示文字)
        self.capsule_width = 276
        self.collapsed_height = 44
        self.expanded_height = 96
        self.border_width = 2
        self._current_border_color = "#10b981"

        # 保持属性兼容
        self.collapsed_width = self.capsule_width
        self.expanded_width = self.capsule_width
        self.height = self.collapsed_height

        self.initial_x = default_x
        self.initial_y = default_y

        # UI 元素引用
        self.bg_canvas: Optional[tk.Canvas] = None
        self.main_frame: Optional[tk.Frame] = None
        self.header_frame: Optional[tk.Frame] = None
        self.info_container: Optional[tk.Frame] = None
        self.title_row: Optional[tk.Frame] = None
        self.canvas: Optional[tk.Canvas] = None
        self.status_label: Optional[tk.Label] = None
        self.model_badge: Optional[tk.Label] = None
        self.partial_label: Optional[tk.Label] = None
        self.btn_cancel: Optional[tk.Label] = None
        self.btn_close: Optional[tk.Label] = None

        # 下方竖向展开抽屉
        self.drawer_frame: Optional[tk.Frame] = None
        self.divider: Optional[tk.Frame] = None
        self.stream_label: Optional[tk.Label] = None

        # 动效计时器
        self._anim_timer = None
        self._pulse_phase = 0.0

        # 抗锯齿超采样底图缓存
        self._pill_photo = None
        self._pill_img_id = None
        self._frame_win_id = None

    def _get_model_display_name(self) -> str:
        """动态获取当前实际生效的 ASR 引擎名称与运行平台 (GPU / CPU)"""
        engine = getattr(self.coordinator, "engine", None)
        eng_name = "SenseVoice"
        device_label = "CPU"

        if engine is not None and hasattr(engine, "get_capabilities"):
            try:
                cap = engine.get_capabilities()
                eng_type = str(cap.get("engine", "")).lower()
                if "qwen" in eng_type:
                    eng_name = "Qwen 1.7B"
                elif "sherpa" in eng_type:
                    eng_name = "Sherpa 2Pass"
                elif "sensevoice" in eng_type:
                    eng_name = "SenseVoice"

                dev = str(cap.get("device", "")).lower()
                if dev:
                    device_label = "GPU" if "cuda" in dev else "CPU"
            except Exception:
                pass
        else:
            cfg_eng = str(getattr(self.coordinator.config, "engine", "")).lower()
            if "qwen" in cfg_eng or "paraformer" in cfg_eng:
                eng_name = "Qwen 1.7B"
            else:
                eng_name = "SenseVoice"

        # 若是 Qwen 引擎，且设备尚未识别为 GPU 时，进行显式环境预检/探测
        if "qwen" in eng_name.lower():
            if not device_label.startswith("GPU"):
                cfg_dev = str(
                    getattr(self.coordinator.config, "device", "")
                    or getattr(self.coordinator.config, "qwen_device", "")
                    or "auto"
                ).strip().lower()
                if cfg_dev == "cpu":
                    device_label = "CPU"
                elif cfg_dev.startswith("cuda"):
                    device_label = "GPU"
                else:
                    # "auto": 检查当前环境是否有可用 CUDA 显卡 (如 RTX 3060)
                    try:
                        import torch
                        if torch.cuda.is_available():
                            device_label = "GPU"
                    except Exception:
                        pass
        else:
            device_label = "CPU"

        return f"{eng_name} · {device_label}"

    def _apply_win32_non_activating(self, root: tk.Tk):
        """将当前 Tkinter 顶级窗口配置为绝不抢夺前台焦点的扩展样式"""
        try:
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            old_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # 叠加 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            new_style = old_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        except Exception as e:
            print(f"[Widget] 应用 Win32 非激活样式异常: {e}")

    def _render_pill_image(
        self, width: int, height: int, border_color: str, radius: Optional[int] = None
    ) -> Optional[object]:
        """使用 Pillow 4x 超采样生成完全抗锯齿、纯净无黑边的圆角胶囊背景底图 (缺少 Pillow 时返回 None)"""
        if not HAS_PIL:
            return None
        try:
            scale = 4
            W = width * scale
            H = height * scale
            # 统一绘制状态边框 (待机绿框、录音红框、纠错黄框)
            has_border = bool(
                border_color and border_color.lower() not in ("none", "transparent")
            )
            BW = (self.border_width * scale) if has_border else 0
            R = (radius * scale) if radius else (H // 2)

            # 4x 超采样 RGBA 画布
            im_large = Image.new("RGBA", (W, H), (1, 2, 3, 0))
            draw = ImageDraw.Draw(im_large)
            inset = BW / 2.0 if BW > 0 else 0
            draw.rounded_rectangle(
                [inset, inset, W - inset, H - inset],
                radius=R,
                fill=(255, 255, 255, 255),
                outline=border_color if has_border else None,
                width=int(BW) if has_border else 0,
            )
            # 高质量 Lanczos 下采样至物理尺寸
            im = im_large.resize((width, height), Image.Resampling.LANCZOS)

            # 二值透明切分：彻底消除 Lanczos 下采样与黑色色键混合产生的黑边光晕 (Black Fringe)
            pixels = im.load()
            clean_im = Image.new("RGB", (width, height), (1, 2, 3))
            clean_pixels = clean_im.load()
            for y in range(height):
                for x in range(width):
                    r, g, b, a = pixels[x, y]
                    if a >= 128:
                        clean_pixels[x, y] = (r, g, b)
                    else:
                        clean_pixels[x, y] = (1, 2, 3)

            return ImageTk.PhotoImage(clean_im, master=self.root)
        except Exception as e:
            print(f"[Widget] 渲染 Pillow 胶囊底图异常: {e}")
            return None

    def _apply_rounded_shape(self, width: int, height: int, radius: int = 38):
        """通过 Win32 GDI 为窗口设置平滑圆角 (缺少 Pillow 时的原生回退)"""
        if HAS_PIL or sys.platform != "win32" or not self.root:
            return
        try:
            hwnd = int(self.root.winfo_id())
            gdi32 = ctypes.windll.gdi32
            rgn = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
            if rgn:
                user32.SetWindowRgn(hwnd, rgn, True)
        except Exception as e:
            print(f"[Widget] 应用 Win32 圆角异常: {e}")

    def _set_border_color(self, color: str):
        """更新四周胶囊完全抗锯齿边框颜色"""
        self._current_border_color = color
        if not self.bg_canvas:
            return
        h = self.expanded_height if self.coordinator.is_recording else self.collapsed_height
        rad = 20 if h > 50 else None
        if HAS_PIL and self._pill_img_id:
            try:
                self.bg_canvas.configure(height=h)
                self._pill_photo = self._render_pill_image(self.capsule_width, h, color, radius=rad)
                if self._pill_photo:
                    self.bg_canvas.itemconfig(self._pill_img_id, image=self._pill_photo)
                if self._frame_win_id:
                    self.bg_canvas.itemconfigure(
                        self._frame_win_id,
                        width=self.capsule_width - 32,
                        height=h - 6,
                    )
            except Exception:
                pass
        else:
            try:
                self.bg_canvas.configure(height=h, highlightbackground=color, highlightcolor=color)
                self._apply_rounded_shape(self.capsule_width, h, radius=rad or 38)
            except Exception:
                pass

    def close_system(self):
        """关闭悬浮窗并彻底退出整个语音伴侣守护进程"""
        if self._on_close:
            try:
                self._on_close()
            except Exception as e:
                print(f"[Widget] 执行退出回调异常: {e}")
        self.stop()

        def _force_exit():
            time.sleep(0.15)
            import os
            os._exit(0)

        # 仅在非单元测试环境触发强行退出进程
        if "unittest" not in sys.modules:
            threading.Thread(target=_force_exit, daemon=True).start()

    def create_window(self):
        """在主线程构建现代白底黑字抗锯齿圆角胶囊界面"""
        try:
            from .tk_runtime import setup_tk_environment
            setup_tk_environment()
        except Exception:
            pass
        self.root = tk.Tk()
        # 实例化后立即隐藏窗口，杜绝初始默认白色方框在屏幕上闪烁
        self.root.withdraw()
        self.root.title("CC Relay Voice Capsule")

        # 无边框 + 保持置顶
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        TRANS_COLOR = "#010203"
        try:
            self.root.wm_attributes("-transparentcolor", TRANS_COLOR)
        except Exception:
            pass
        self.root.configure(bg=TRANS_COLOR)

        # 默认停靠在屏幕顶部偏右位置
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        x = self.initial_x if self.initial_x >= 0 else screen_w - self.capsule_width - 80
        y = self.initial_y if self.initial_y >= 0 else 60
        self.root.geometry(f"{self.capsule_width}x{self.collapsed_height}+{x}+{y}")

        # 1. 宿主画布 (背景承载 4x 超采样抗锯齿高清胶囊图片)
        self.bg_canvas = tk.Canvas(
            self.root,
            width=self.capsule_width,
            height=self.collapsed_height,
            bg=TRANS_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self.bg_canvas.pack(fill=tk.BOTH, expand=True)

        if HAS_PIL:
            self._pill_photo = self._render_pill_image(
                self.capsule_width, self.collapsed_height, self._current_border_color
            )
            if self._pill_photo:
                self._pill_img_id = self.bg_canvas.create_image(0, 0, image=self._pill_photo, anchor="nw")
        if not self._pill_img_id:
            try:
                self.root.configure(bg="#ffffff")
                self.bg_canvas.configure(bg="#ffffff", highlightthickness=self.border_width, highlightbackground=self._current_border_color)
                self._apply_rounded_shape(self.capsule_width, self.collapsed_height, radius=38)
            except Exception:
                pass

        # 2. 主内容容器 (置于胶囊中央安全矩形内，避免覆盖平滑圆弧)
        self.main_frame = tk.Frame(self.bg_canvas, bg="#ffffff", bd=0, highlightthickness=0)
        self._frame_win_id = self.bg_canvas.create_window(
            16,
            3,
            window=self.main_frame,
            anchor="nw",
            width=self.capsule_width - 32,
            height=self.collapsed_height - 6,
        )

        # 顶部栏 (Header)
        self.header_frame = tk.Frame(self.main_frame, bg="#ffffff", height=38)
        self.header_frame.pack(side=tk.TOP, fill=tk.X)

        # 麦克风图标画布
        self.canvas = tk.Canvas(
            self.header_frame,
            width=24,
            height=24,
            bg="#ffffff",
            bd=0,
            highlightthickness=0,
        )
        self.canvas.pack(side=tk.LEFT, padx=(2, 4), pady=0)
        engine = getattr(self.coordinator, "engine", None)
        is_ready = getattr(engine, "is_loaded", True) if engine is not None else True
        init_mic_color = "#059669" if is_ready else "#d97706"
        self._draw_mic_icon(color=init_mic_color, state="idle")

        # 3. 关闭按钮 (常驻 Header 最右侧，点击退出整个语音系统)
        self.btn_close = tk.Label(
            self.header_frame,
            text="✕",
            font=("Segoe UI", -11),
            fg="#8c959f",
            bg="#ffffff",
            cursor="hand2",
            padx=4,
            pady=0,
        )
        self.btn_close.pack(side=tk.RIGHT, padx=(0, 2))
        self.btn_close.bind("<Button-1>", lambda e: self.close_system())
        self.btn_close.bind("<Enter>", lambda e: self.btn_close.configure(fg="#cf222e"))
        self.btn_close.bind("<Leave>", lambda e: self.btn_close.configure(fg="#8c959f"))

        # 取消按钮 (仅在录音态显现，置于关闭按钮左侧)
        self.btn_cancel = tk.Label(
            self.header_frame,
            text="↺ 取消",
            font=("Segoe UI", -10, "bold"),
            fg="#8c959f",
            bg="#ffffff",
            cursor="hand2",
            padx=4,
            pady=0,
        )
        self.btn_cancel.bind("<Button-1>", lambda e: self._on_cancel_click())
        self.btn_cancel.bind("<Enter>", lambda e: self.btn_cancel.configure(fg="#cf222e"))
        self.btn_cancel.bind("<Leave>", lambda e: self.btn_cancel.configure(fg="#8c959f"))

        # 4. 状态与提示文字容器
        self.info_container = tk.Frame(self.header_frame, bg="#ffffff")
        self.info_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        init_title = "按快捷键说话" if is_ready else "⏳ 模型加载中"
        init_sub = f"快捷键: {self.coordinator.config.hotkey.upper()}" if is_ready else "正在预载模型，请稍候"
        model_name = self._get_model_display_name()
        badge_fg = "#24292f" if is_ready else "#9a6700"

        # 标题行 (主标题 + 模型徽章 Badge)
        self.title_row = tk.Frame(self.info_container, bg="#ffffff")
        self.title_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 0))

        self.status_label = tk.Label(
            self.title_row,
            text=init_title,
            font=("Segoe UI", -13, "bold"),
            fg="#1f2328" if is_ready else "#9a6700",
            bg="#ffffff",
            anchor="w",
            pady=0,
        )
        self.status_label.pack(side=tk.LEFT)

        self.model_badge = tk.Label(
            self.title_row,
            text=model_name,
            font=("Segoe UI", -10, "bold"),
            fg=badge_fg,
            bg="#f3f4f6",
            padx=4,
            pady=0,
            relief="flat",
            bd=0,
        )
        self.model_badge.pack(side=tk.LEFT, padx=5)

        self.partial_label = tk.Label(
            self.info_container,
            text=init_sub,
            font=("Segoe UI", -11),
            fg="#656d76",
            bg="#ffffff",
            anchor="w",
            pady=0,
        )
        self.partial_label.pack(side=tk.TOP, fill=tk.X)

        # 5. 竖向展开识别抽屉 (默认收起 pack_forget)
        self.drawer_frame = tk.Frame(self.main_frame, bg="#ffffff")
        self.divider = tk.Frame(self.drawer_frame, bg="#e1e4e8", height=1)
        self.divider.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 3))
        self.stream_label = tk.Label(
            self.drawer_frame,
            text="",
            font=("Segoe UI", -12),
            fg="#0969da",
            bg="#ffffff",
            wraplength=240,
            justify=tk.LEFT,
            anchor="nw",
        )
        self.stream_label.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        # 绑定点击与拖动手势
        bind_widgets = (
            self.bg_canvas,
            self.main_frame,
            self.header_frame,
            self.drawer_frame,
            self.canvas,
            self.title_row,
            self.status_label,
            self.model_badge,
            self.partial_label,
            self.stream_label,
            self.info_container,
        )
        for w in bind_widgets:
            w.bind("<Button-1>", self._on_left_click)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._on_left_release)
            w.bind("<Button-3>", self._show_context_menu)

        # 右键上下文菜单 (白底黑字风格)
        self.context_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#ffffff",
            fg="#1f2328",
            activebackground="#f3f4f6",
            activeforeground="#1f2328",
        )
        self.context_menu.add_command(label=f"🏷️ 引擎: {model_name}", state=tk.DISABLED)
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label=f"🎙️ 按快捷键录音: {self.coordinator.config.hotkey.upper()}", state=tk.DISABLED
        )
        self.context_menu.add_command(label="📋 重新输入上次内容", command=self._retry_inject)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="— 隐藏悬浮窗", command=self.hide)
        self.context_menu.add_command(label="⏻ 退出语音系统", command=self.close_system)

        # 窗口初次渲染完毕后，注入 Win32 非抢焦点样式并平滑呈现
        self.root.update_idletasks()
        self._apply_win32_non_activating(self.root)
        self.root.deiconify()

        # 注册协调器观察者回调
        self.coordinator.add_state_listener(self._handle_state_change)
        self.coordinator.add_partial_listener(self._handle_partial_text)
        self.coordinator.add_final_listener(self._handle_final_result)
        add_correction_listener = getattr(self.coordinator, "add_correction_listener", None)
        if callable(add_correction_listener):
            add_correction_listener(self._handle_correction_result)
        self.coordinator.add_audio_level_listener(self._handle_audio_level)

        self._start_animation_loop()

    def _draw_mic_icon(self, color: str = "#059669", state: str = "idle"):
        """使用 Canvas 绘制矢量麦克风与声波图标 (高对比白底配色)"""
        if not self.canvas:
            return
        self.canvas.delete("all")
        cx, cy = 12, 12

        if state == "recording":
            # 动态音量波形动画：绘制 5 根根据音频电平跳动的柱形
            lvl = self._current_audio_level
            bar_count = 5
            spacing = 4
            start_x = cx - (bar_count * spacing) // 2 + 1

            for i in range(bar_count):
                factor = math.sin(self._pulse_phase + i * 0.8) * 0.4 + 0.6
                h = max(3.0, lvl * 16.0 * factor)
                bx = start_x + i * spacing
                self.canvas.create_line(
                    bx, cy - h / 2, bx, cy + h / 2,
                    fill="#cf222e", width=2.2, capstyle=tk.ROUND
                )
        else:
            # 绘制静态/呼吸质感麦克风 (白底深色高对比)
            # 麦克风头部胶囊
            self.canvas.create_rectangle(
                cx - 3, cy - 6, cx + 3, cy + 2,
                fill=color, outline=color, width=1
            )
            # 底部弧形托架
            self.canvas.create_arc(
                cx - 6, cy - 4, cx + 6, cy + 5,
                start=180, extent=180,
                outline=color, width=1.5, style=tk.ARC
            )
            # 支柱与底座
            self.canvas.create_line(cx, cy + 5, cx, cy + 9, fill=color, width=1.5)
            self.canvas.create_line(cx - 4, cy + 9, cx + 4, cy + 9, fill=color, width=1.5)

    def _start_animation_loop(self):
        """动画驱动定时器：更新脉冲相位并重绘 Canvas"""
        if not self.root:
            return
        self._pulse_phase += 0.3
        if self.coordinator.is_recording:
            self._draw_mic_icon(state="recording")
            self._refresh_residency_ui()
        elif time.monotonic() >= self._feedback_until:
            self._refresh_residency_ui()
        try:
            self._anim_timer = self.root.after(50, self._start_animation_loop)
        except Exception:
            pass

    def _on_left_click(self, event):
        """记录拖动起点，同时作为点击响应"""
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._dragged = False

    def _on_drag(self, event):
        """支持鼠标按住胶囊随意拖动吸附"""
        dx = abs(event.x - self._drag_start_x)
        dy = abs(event.y - self._drag_start_y)
        if dx > 3 or dy > 3:
            self._dragged = True
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        new_x = cur_x + (event.x - self._drag_start_x)
        new_y = cur_y + (event.y - self._drag_start_y)
        self.root.geometry(f"+{new_x}+{new_y}")

    def _on_left_release(self, event):
        """鼠标松开时重置拖动标记 (已取消点击触发录音功能，仅允许快捷键输入)"""
        self._dragged = False

    def _toggle_record(self):
        """点击麦克风胶囊触发开始或停止录音"""
        engine = getattr(self.coordinator, "engine", None)
        if engine is not None and not getattr(engine, "is_loaded", True):
            self.set_feedback("⏳ 模型仍在加载中…", color="#9a6700")
            return

        if self.coordinator.is_recording:
            self.coordinator.stop_session(source="widget")
        else:
            self.coordinator.start_session(source="widget", mode="toggle", output_mode="inject")

    def _update_model_badge(self):
        """更新模型徽章文字与颜色"""
        if not self.model_badge:
            return
        engine = getattr(self.coordinator, "engine", None)
        is_ready = getattr(engine, "is_loaded", True) if engine is not None else True
        model_name = self._get_model_display_name()
        fg_col = "#24292f" if is_ready else "#9a6700"
        self.model_badge.configure(text=model_name, fg=fg_col, bg="#f3f4f6")

    def _refresh_residency_ui(self):
        """在 Tk 主线程刷新空闲时的 GPU 驻留提示。"""
        engine = getattr(self.coordinator, "engine", None)
        if engine is None or not hasattr(engine, "get_capabilities"):
            return
        try:
            cap = engine.get_capabilities()
            if cap.get("vram_mode") != "on_demand_offload" or not str(cap.get("device", "")).startswith("cuda"):
                return
            state = cap.get("residency_state", "")
            recording = bool(getattr(self.coordinator, "is_recording", False))
            if recording:
                text_by_state = {
                    "activating_gpu": "● 录音中 · GPU 准备中",
                    "gpu_ready": "● 正在聆听",
                    "error": "✕ 显存加载失败 · 音频已缓存",
                }
                fallback_text = "● 正在聆听"
            else:
                text_by_state = {
                    "cpu_ready": "已释放显存",
                    "activating_gpu": "按键后加载显存…",
                    "gpu_ready": "模型已载入 GPU",
                    "offloading_cpu": "正在释放显存…",
                    "error": "✕ 显存迁移失败",
                }
                fallback_text = "等待按键"
            text = text_by_state.get(state, fallback_text)
            key = (recording, state, cap.get("gpu_resident"), cap.get("residency_error"))
            if key == self._resource_ui_key:
                return
            self._resource_ui_key = key
            if self.status_label and self.status_label.cget("text") != text:
                self.status_label.configure(text=text, fg="#cf222e" if state == "error" else "#1f2328")
            self._update_model_badge()
        except Exception:
            pass

    def set_ready(self):
        """引擎载入成功，更新胶囊为就绪状态 (绿框 + 按快捷键说话)"""
        if not self.root:
            return
        def _update():
            if self.status_label:
                self.status_label.configure(text="● 按快捷键说话", fg="#1f2328")
            if self.partial_label:
                self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#656d76")
            if self.model_badge and not self.model_badge.winfo_ismapped():
                self.model_badge.pack(side=tk.LEFT, padx=5)
            self._update_model_badge()
            self._draw_mic_icon(color="#059669", state="idle")
            self._set_border_color("#10b981")
        self.root.after(0, _update)

    def set_error(self, err_msg: str = ""):
        """引擎载入失败，更新胶囊为异常状态"""
        if not self.root:
            return
        def _update():
            is_model_missing = any(
                k in err_msg for k in ("模型不存在", "未在本地检测到", "未找到离线模型", "未在本地找到", "无法启动")
            )
            if self.status_label:
                main_txt = "✕ 无法启动 (缺少模型)" if is_model_missing else "✕ 模型加载失败"
                self.status_label.configure(text=main_txt, fg="#cf222e")
            if self.model_badge:
                self.model_badge.configure(fg="#cf222e")
            if self.partial_label:
                if is_model_missing:
                    disp = "请在 Web 控制台点击自动下载"
                elif "依赖缺失" in err_msg or "No module" in err_msg:
                    disp = "核心依赖缺失 (请运行 setup)"
                elif err_msg:
                    disp = err_msg.splitlines()[0][:26]
                else:
                    disp = "请检查依赖与模型目录"
                self.partial_label.configure(text=disp, fg="#cf222e")
            self._draw_mic_icon(color="#cf222e", state="idle")
            self._set_border_color("#ef4444")
        self.root.after(0, _update)

    def _on_cancel_click(self):
        """点击取消按钮"""
        self.coordinator.cancel_session()

    def _retry_inject(self):
        """重试向当前激活窗口注入上次的识别文本"""
        res = self.coordinator.retry_last_injection()
        if res.success:
            self.set_feedback("✓ 已补录到光标处", color="#1a7f37")
        else:
            self.set_feedback(f"✕ 补录失败: {res.message}", color="#cf222e")

    def _show_context_menu(self, event):
        if self.context_menu:
            # 刷新引擎标签
            model_name = self._get_model_display_name()
            try:
                self.context_menu.entryconfigure(0, label=f"🏷️ 引擎: {model_name}")
            except Exception:
                pass
            self.context_menu.post(event.x_root, event.y_root)

    def _expand_vertically(self):
        """向下竖向展开抽屉气泡"""
        if not self.root:
            return
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        self.root.geometry(f"{self.capsule_width}x{self.expanded_height}+{cur_x}+{cur_y}")
        self.root.update_idletasks()
        if self.bg_canvas:
            try:
                self.bg_canvas.configure(height=self.expanded_height)
                if HAS_PIL and self._pill_img_id:
                    self._pill_photo = self._render_pill_image(
                        self.capsule_width, self.expanded_height, self._current_border_color, radius=20
                    )
                    if self._pill_photo:
                        self.bg_canvas.itemconfig(self._pill_img_id, image=self._pill_photo)
                else:
                    self._apply_rounded_shape(self.capsule_width, self.expanded_height, radius=32)
                if self._frame_win_id:
                    self.bg_canvas.itemconfigure(
                        self._frame_win_id,
                        width=self.capsule_width - 32,
                        height=self.expanded_height - 6,
                    )
            except Exception:
                pass
        if self.partial_label:
            self.partial_label.pack_forget()
        if self.drawer_frame:
            self.drawer_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _collapse_capsule(self):
        """平滑收缩恢复到小巧圆角胶囊"""
        if not self.coordinator.is_recording and self.root:
            if self.drawer_frame:
                self.drawer_frame.pack_forget()
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
            self.root.geometry(f"{self.capsule_width}x{self.collapsed_height}+{cur_x}+{cur_y}")
            self.root.update_idletasks()
            if self.bg_canvas:
                try:
                    self.bg_canvas.configure(height=self.collapsed_height)
                    if HAS_PIL and self._pill_img_id:
                        self._pill_photo = self._render_pill_image(
                            self.capsule_width, self.collapsed_height, "#10b981", radius=None
                        )
                        if self._pill_photo:
                            self.bg_canvas.itemconfig(self._pill_img_id, image=self._pill_photo)
                    else:
                        self._apply_rounded_shape(self.capsule_width, self.collapsed_height, radius=38)
                    if self._frame_win_id:
                        self.bg_canvas.itemconfigure(
                            self._frame_win_id,
                            width=self.capsule_width - 32,
                            height=self.collapsed_height - 6,
                        )
                except Exception:
                    pass
            if self.partial_label:
                self.partial_label.pack(side=tk.TOP, fill=tk.X)
                self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#656d76")
            if self.model_badge and not self.model_badge.winfo_ismapped():
                self.model_badge.pack(side=tk.LEFT, padx=5)
            self._current_border_color = "#10b981"
            self.status_label.configure(text="按快捷键说话", fg="#1f2328")
            self.status_label.bind("<Button-1>", self._on_left_click)
            self._update_model_badge()
            self._draw_mic_icon(color="#059669", state="idle")

    def hide(self):
        """隐藏悬浮窗"""
        if self.root:
            self.root.withdraw()

    def show(self):
        """显示悬浮窗"""
        if self.root:
            self.root.deiconify()
            self.root.update_idletasks()
            self._apply_win32_non_activating(self.root)

    def start(self):
        """在当前线程启动 Tk 消息循环"""
        self._is_running = True
        self.create_window()
        print("[*] 桌面悬浮胶囊已启动 (Win32 WS_EX_NOACTIVATE 零抢焦点)。")
        self.root.mainloop()

    def stop(self):
        """关闭悬浮窗"""
        self._is_running = False
        if self.root:
            if self._anim_timer:
                try:
                    self.root.after_cancel(self._anim_timer)
                except Exception:
                    pass
                self._anim_timer = None
            # Explicitly release the Tk image on the Tk owner thread before destroying its interpreter.
            photo, self._pill_photo = self._pill_photo, None
            if photo is not None:
                try:
                    photo.__del__()
                except Exception:
                    pass
            root = self.root
            try:
                root.destroy()
            except Exception:
                pass
            self.root = None
            # Drop every Python wrapper that holds this Tcl interpreter on the Tk owner thread.
            for name in (
                "bg_canvas", "main_frame", "header_frame", "info_container", "title_row", "canvas",
                "status_label", "model_badge", "partial_label", "btn_cancel", "btn_close",
                "drawer_frame", "divider", "stream_label", "context_menu",
            ):
                setattr(self, name, None)
            self._pill_img_id = None
            self._frame_win_id = None

    # --- 协调器观察者异步安全调度 ---

    def _handle_state_change(self, state: SessionState, sess):
        if not self.root:
            return
        self.root.after(0, self._apply_state_change, state, sess)

    def _apply_state_change(self, state: SessionState, sess):
        if state == SessionState.RECORDING:
            self._expand_vertically()
            self._set_border_color("#ef4444")
            self.status_label.configure(text="● 正在聆听", fg="#cf222e")
            if self.model_badge:
                self.model_badge.pack_forget()
            if self.stream_label:
                self.stream_label.configure(text="请说话，实时出字中…", fg="#656d76")
            if self.partial_label:
                self.partial_label.configure(text="边说边出字中…", fg="#656d76")
            self.btn_cancel.pack(side=tk.RIGHT, padx=(0, 4))

        elif state in (SessionState.DRAINING, SessionState.FINALIZING):
            self.status_label.configure(text="⌛ 正在输出…", fg="#9a6700")
            self._set_border_color("#d97706")
            self.btn_cancel.pack_forget()
            if self.model_badge:
                self.model_badge.pack_forget()
            self._draw_mic_icon(color="#d97706", state="idle")

        elif state == SessionState.INJECTING:
            self.status_label.configure(text="⚡ 正在自动输入…", fg="#0969da")

        elif state == SessionState.ERROR:
            self.btn_cancel.pack_forget()
            err_msg = getattr(self.coordinator, "last_error", "") or "录音启动失败"
            self._set_border_color("#ef4444")
            self.set_feedback(f"✕ {err_msg}", color="#cf222e")

        elif state == SessionState.IDLE:
            self._resource_ui_key = None
            self.btn_cancel.pack_forget()
            if self.model_badge and not self.model_badge.winfo_ismapped():
                self.model_badge.pack(side=tk.LEFT, padx=5)
            self._draw_mic_icon(color="#059669", state="idle")
            self._set_border_color("#10b981")

    def _handle_partial_text(self, confirmed: str, partial: str):
        if not self.root:
            return
        display_text = f"{confirmed} {partial}".strip()
        if display_text:
            self.root.after(0, self._apply_partial_text, display_text)

    def _apply_partial_text(self, text: str):
        if self.stream_label:
            self.stream_label.configure(text=f"“{text}”", fg="#0969da")
        if self.partial_label:
            shown = text[-25:] if len(text) > 25 else text
            self.partial_label.configure(text=f"“{shown}”", fg="#0969da")

    def _handle_final_result(self, text: str, res: InjectionResult):
        if not self.root:
            return
        self.root.after(0, self._apply_final_result, text, res)

    def _apply_final_result(self, text: str, res: InjectionResult):
        if not text:
            last_error = getattr(self.coordinator, "last_error", "")
            if last_error.startswith("ASR识别失败:"):
                self.set_feedback(f"✕ {last_error[:42]}", color="#cf222e")
            else:
                self.set_feedback("未识别到有效语音", color="#656d76")
            return

        disp = text[:22] + "…" if len(text) > 22 else text
        if res.success:
            self.set_feedback(f"✓ 已自动输入: {disp}", color="#1a7f37")
        else:
            if res.outcome == InjectionOutcome.FOCUS_CHANGED:
                self.set_feedback("⚠️ 目标窗口已切换 (点击补录)", color="#9a6700", clickable=True)
            else:
                self.set_feedback(f"✕ {res.message}", color="#cf222e")

    def _handle_correction_result(self, status: str, text: str, message: str):
        if not self.root:
            return
        self.root.after(0, self._apply_correction_result, status, text, message)

    def _apply_correction_result(self, status: str, text: str, message: str):
        if status == "pending":
            self._feedback_until = time.monotonic() + 12.0
            if self.status_label:
                self.status_label.configure(text="语义校正中…", fg="#0969da")
            if self.partial_label:
                self.partial_label.configure(text="原文已输入，请稍候", fg="#0969da")
            self._set_border_color("#0969da")
            return
        if status == "suggestion" and text:
            self._correction_suggestion = text
            self._feedback_until = time.monotonic() + 15.0
            self._expand_vertically()
            shown = text[:180] + ("…" if len(text) > 180 else "")
            if self.status_label:
                self.status_label.configure(text="校正建议 · 未自动替换", fg="#9a6700")
            if self.stream_label:
                self.stream_label.configure(
                    text=f"{shown}\n\n{message}\n点击此处复制建议文本",
                    fg="#9a6700",
                )
                self.stream_label.bind("<Button-1>", self._copy_correction_suggestion, add="+")
            self._set_border_color("#d97706")
            return
        self._correction_suggestion = ""
        if status == "applied":
            self.set_feedback("✓ 已语义校正", color="#1a7f37")
        elif status == "unchanged":
            self.set_feedback("✓ 无需语义修正", color="#1a7f37")
        elif status == "skipped":
            clean_msg = str(message or "").strip()
            if clean_msg.startswith("语义校正") or clean_msg.startswith("校正"):
                self.set_feedback(f"⚠️ {clean_msg[:40]}", color="#9a6700")
            else:
                self.set_feedback(f"⚠️ 语义校正已跳过: {clean_msg[:40]}", color="#9a6700")
        else:
            clean_msg = str(message or "").strip()
            if clean_msg.startswith("语义校正") or clean_msg.startswith("校正"):
                self.set_feedback(f"⚠️ {clean_msg[:40]}", color="#9a6700")
            else:
                self.set_feedback(f"⚠️ 语义校正失败: {clean_msg[:40]}", color="#9a6700")

    def _copy_correction_suggestion(self, _event=None):
        if self._correction_suggestion and set_clipboard_text(self._correction_suggestion):
            self.set_feedback("✓ 建议已复制，请手动替换原文", color="#1a7f37")

    def _handle_audio_level(self, level: float):
        self._current_audio_level = level

    def set_feedback(self, text: str, color: str = "#1a7f37", clickable: bool = False):
        """展示完成或警告反馈，并在 2.2 秒后自动恢复待机微型胶囊"""
        self._feedback_until = time.monotonic() + 2.2
        self.status_label.configure(text=text, fg=color)
        if self.stream_label:
            self.stream_label.configure(text=text, fg=color)
        if self.partial_label:
            self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#656d76")

        # 隐藏录音态取消按钮
        if self.btn_cancel:
            self.btn_cancel.pack_forget()

        # 反馈文本较长，临时隐去徽章避免重叠
        if self.model_badge:
            self.model_badge.pack_forget()

        # 还原麦克风就绪状态
        self._draw_mic_icon(color="#059669", state="idle")

        # 对应设置边框颜色
        border_col = (
            "#10b981" if color in ("#1a7f37", "#059669", "#00ffc4")
            else ("#ef4444" if color in ("#cf222e", "#ff7b72")
            else ("#d97706" if color in ("#9a6700", "#d29922")
            else color))
        )
        self._set_border_color(border_col)

        if clickable:
            self.status_label.bind("<Button-1>", lambda e: self._retry_inject())
        else:
            self.status_label.bind("<Button-1>", self._on_left_click)

        # 2.2秒后复原胶囊
        self.root.after(2200, self._collapse_capsule)
