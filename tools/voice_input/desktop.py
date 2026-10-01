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
from typing import Optional

from .inject import InjectionOutcome, InjectionResult
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
    def __init__(self, coordinator: SessionCoordinator, default_x: int = -1, default_y: int = -1):
        self.coordinator = coordinator
        self.root: Optional[tk.Tk] = None
        self._is_running = False
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._dragged = False
        self._current_audio_level = 0.0

        # 浮窗尺寸 (固定胶囊宽度，竖向伸缩展示文字)
        self.capsule_width = 240
        self.collapsed_height = 42
        self.expanded_height = 96

        # 保持属性兼容
        self.collapsed_width = self.capsule_width
        self.expanded_width = self.capsule_width
        self.height = self.collapsed_height

        self.initial_x = default_x
        self.initial_y = default_y

        # UI 元素引用
        self.main_frame: Optional[tk.Frame] = None
        self.header_frame: Optional[tk.Frame] = None
        self.info_container: Optional[tk.Frame] = None
        self.title_row: Optional[tk.Frame] = None
        self.canvas: Optional[tk.Canvas] = None
        self.status_label: Optional[tk.Label] = None
        self.model_badge: Optional[tk.Label] = None
        self.partial_label: Optional[tk.Label] = None
        self.btn_cancel: Optional[tk.Label] = None

        # 下方竖向展开抽屉
        self.drawer_frame: Optional[tk.Frame] = None
        self.divider: Optional[tk.Frame] = None
        self.stream_label: Optional[tk.Label] = None

        # 动效计时器
        self._anim_timer = None
        self._pulse_phase = 0.0

    def _get_model_display_name(self) -> str:
        """动态获取当前实际生效的 ASR 引擎名称 (具备 fallback 感知)"""
        engine = getattr(self.coordinator, "engine", None)
        if engine is not None and hasattr(engine, "get_capabilities"):
            try:
                cap = engine.get_capabilities()
                eng_type = str(cap.get("engine", "")).lower()
                if "qwen" in eng_type:
                    return "Qwen 1.7B"
                if "sherpa" in eng_type:
                    return "Sherpa 2Pass"
                if "sensevoice" in eng_type:
                    return "SenseVoice"
            except Exception:
                pass

        cfg_eng = str(getattr(self.coordinator.config, "engine", "")).lower()
        if "qwen" in cfg_eng or "paraformer" in cfg_eng:
            return "Qwen 1.7B"
        return "SenseVoice"

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

    def _apply_rounded_shape(self, width: int, height: int, radius: int = 38):
        """通过 Win32 GDI 为窗口设置平滑圆角/胶囊物理剪裁 (radius 为椭圆直径)"""
        if sys.platform != "win32" or not self.root:
            return
        try:
            hwnd = self.root.winfo_id()
            parent = user32.GetParent(hwnd) or hwnd
            if hasattr(ctypes.windll, "gdi32") and hasattr(ctypes.windll.gdi32, "CreateRoundRectRgn"):
                hrgn_p = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
                user32.SetWindowRgn(parent, hrgn_p, True)
                if parent != hwnd:
                    hrgn_w = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
                    user32.SetWindowRgn(hwnd, hrgn_w, True)
        except Exception as e:
            pass

    def create_window(self):
        """在主线程构建深色亚克力质感圆角胶囊界面"""
        self.root = tk.Tk()
        self.root.title("CC Relay Voice Capsule")

        # 无边框 + 保持置顶
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        # 默认停靠在屏幕顶部偏右位置
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        x = self.initial_x if self.initial_x >= 0 else screen_w - self.capsule_width - 80
        y = self.initial_y if self.initial_y >= 0 else 60
        self.root.geometry(f"{self.capsule_width}x{self.collapsed_height}+{x}+{y}")

        # 背景深黑金属质感
        bg_color = "#13161c"
        border_color = "#30363d"
        self.root.configure(bg=bg_color)

        # 主胶囊框架
        self.main_frame = tk.Frame(
            self.root,
            bg=bg_color,
            highlightbackground=border_color,
            highlightcolor="#00ffc4",
            highlightthickness=1,
            bd=0,
        )
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部栏 (Header)
        self.header_frame = tk.Frame(self.main_frame, bg=bg_color, height=40)
        self.header_frame.pack(side=tk.TOP, fill=tk.X)

        # 1. 麦克风图标与动效画布 (Canvas)
        self.canvas = tk.Canvas(
            self.header_frame,
            width=36,
            height=36,
            bg=bg_color,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack(side=tk.LEFT, padx=(4, 2), pady=2)
        engine = getattr(self.coordinator, "engine", None)
        is_ready = getattr(engine, "is_loaded", True) if engine is not None else True
        init_mic_color = "#00ffc4" if is_ready else "#d29922"
        self._draw_mic_icon(color=init_mic_color, state="idle")

        # 2. 状态与提示文字容器
        self.info_container = tk.Frame(self.header_frame, bg=bg_color)
        self.info_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)

        init_title = "点击语音输入" if is_ready else "⏳ 模型加载中"
        init_sub = f"快捷键: {self.coordinator.config.hotkey.upper()}" if is_ready else "正在预载模型，请稍候"
        model_name = self._get_model_display_name()
        badge_fg = "#00ffc4" if is_ready else "#d29922"

        # 标题行 (主标题 + 模型徽章 Badge)
        self.title_row = tk.Frame(self.info_container, bg=bg_color)
        self.title_row.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))

        self.status_label = tk.Label(
            self.title_row,
            text=init_title,
            font=("Segoe UI", 9, "bold"),
            fg="#e6edf3" if is_ready else "#d29922",
            bg=bg_color,
            anchor="w",
            cursor="hand2",
        )
        self.status_label.pack(side=tk.LEFT)

        self.model_badge = tk.Label(
            self.title_row,
            text=model_name,
            font=("Segoe UI", 7, "bold"),
            fg=badge_fg,
            bg="#21262d",
            padx=4,
            pady=0,
            relief="flat",
            bd=0,
            cursor="hand2",
        )
        self.model_badge.pack(side=tk.LEFT, padx=(5, 0))

        self.partial_label = tk.Label(
            self.info_container,
            text=init_sub,
            font=("Segoe UI", 8),
            fg="#8b949e",
            bg=bg_color,
            anchor="w",
            cursor="hand2",
        )
        self.partial_label.pack(side=tk.TOP, fill=tk.X)

        # 3. 取消按钮 (仅在录音态显现)
        self.btn_cancel = tk.Label(
            self.header_frame,
            text="✖",
            font=("Segoe UI", 10, "bold"),
            fg="#8b949e",
            bg=bg_color,
            cursor="hand2",
            padx=6,
        )
        self.btn_cancel.bind("<Button-1>", lambda e: self._on_cancel_click())
        self.btn_cancel.bind("<Enter>", lambda e: self.btn_cancel.configure(fg="#ff7b72"))
        self.btn_cancel.bind("<Leave>", lambda e: self.btn_cancel.configure(fg="#8b949e"))

        # 4. 竖向展开识别抽屉 (默认收起 pack_forget)
        self.drawer_frame = tk.Frame(self.main_frame, bg=bg_color)
        self.divider = tk.Frame(self.drawer_frame, bg="#262c36", height=1)
        self.divider.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(1, 3))
        self.stream_label = tk.Label(
            self.drawer_frame,
            text="",
            font=("Segoe UI", 8),
            fg="#58a6ff",
            bg=bg_color,
            wraplength=200,
            justify=tk.LEFT,
            anchor="nw",
        )
        self.stream_label.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # 绑定点击与拖动手势
        bind_widgets = (
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

        # 右键上下文菜单
        self.context_menu = tk.Menu(self.root, tearoff=0, bg="#161b22", fg="#c9d1d9", activebackground="#1f6feb")
        self.context_menu.add_command(label=f"🏷️ 引擎: {model_name}", state=tk.DISABLED)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🎙️ 开始/停止录音", command=self._toggle_record)
        self.context_menu.add_command(label="📋 重新输入上次内容", command=self._retry_inject)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="✕ 隐藏胶囊", command=self.hide)

        # 窗口初次渲染完毕后，注入 Win32 非抢焦点样式与圆角剪裁
        self.root.update_idletasks()
        self._apply_win32_non_activating(self.root)
        self._apply_rounded_shape(self.capsule_width, self.collapsed_height, radius=38)

        # 注册协调器观察者回调
        self.coordinator.add_state_listener(self._handle_state_change)
        self.coordinator.add_partial_listener(self._handle_partial_text)
        self.coordinator.add_final_listener(self._handle_final_result)
        self.coordinator.add_audio_level_listener(self._handle_audio_level)

        self._start_animation_loop()

    def _draw_mic_icon(self, color: str = "#00ffc4", state: str = "idle"):
        """使用 Canvas 绘制矢量麦克风与声波图标"""
        if not self.canvas:
            return
        self.canvas.delete("all")
        cx, cy = 18, 18

        if state == "recording":
            # 动态音量波形动画：绘制 5 根根据音频电平跳动的柱形
            lvl = self._current_audio_level
            bar_count = 5
            spacing = 5
            start_x = cx - (bar_count * spacing) // 2 + 2

            for i in range(bar_count):
                factor = math.sin(self._pulse_phase + i * 0.8) * 0.4 + 0.6
                h = max(3.0, lvl * 18.0 * factor)
                bx = start_x + i * spacing
                self.canvas.create_line(
                    bx, cy - h / 2, bx, cy + h / 2,
                    fill="#ff4b4b", width=2.5, capstyle=tk.ROUND
                )
        else:
            # 绘制静态/呼吸质感麦克风
            # 麦克风头部胶囊
            self.canvas.create_rectangle(
                cx - 4, cy - 8, cx + 4, cy + 2,
                fill=color, outline=color, width=1
            )
            # 底部弧形托架
            self.canvas.create_arc(
                cx - 7, cy - 6, cx + 7, cy + 6,
                start=180, extent=180,
                outline=color, width=1.5, style=tk.ARC
            )
            # 支柱与底座
            self.canvas.create_line(cx, cy + 6, cx, cy + 10, fill=color, width=1.5)
            self.canvas.create_line(cx - 5, cy + 10, cx + 5, cy + 10, fill=color, width=1.5)

    def _start_animation_loop(self):
        """动画驱动定时器：更新脉冲相位并重绘 Canvas"""
        if not self.root:
            return
        self._pulse_phase += 0.3
        if self.coordinator.is_recording:
            self._draw_mic_icon(state="recording")
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
        """鼠标松开时若未发生明显拖动，则触发点击切换录音"""
        if not self._dragged:
            self._toggle_record()

    def _toggle_record(self):
        """点击麦克风胶囊触发开始或停止录音"""
        engine = getattr(self.coordinator, "engine", None)
        if engine is not None and not getattr(engine, "is_loaded", True):
            self.set_feedback("⏳ 模型仍在加载中…", color="#d29922")
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
        fg_col = "#00ffc4" if is_ready else "#d29922"
        self.model_badge.configure(text=model_name, fg=fg_col)

    def set_ready(self):
        """引擎载入成功，更新胶囊为就绪状态"""
        if not self.root:
            return
        def _update():
            if self.status_label:
                self.status_label.configure(text="● 点击语音输入", fg="#e6edf3")
            if self.partial_label:
                self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#8b949e")
            self._update_model_badge()
            self._draw_mic_icon(color="#00ffc4", state="idle")
        self.root.after(0, _update)

    def set_error(self, err_msg: str = ""):
        """引擎载入失败，更新胶囊为异常状态"""
        if not self.root:
            return
        def _update():
            if self.status_label:
                self.status_label.configure(text="✕ 模型加载失败", fg="#ff7b72")
            if self.model_badge:
                self.model_badge.configure(fg="#ff7b72")
            if self.partial_label:
                disp = "请检查依赖与模型目录"
                if "依赖缺失" in err_msg or "No module" in err_msg:
                    disp = "核心依赖缺失 (请运行 setup)"
                elif "模型不存在" in err_msg or "未在本地找到" in err_msg:
                    disp = "未找到离线模型文件"
                elif err_msg:
                    disp = err_msg.splitlines()[0][:26]
                self.partial_label.configure(text=disp, fg="#ff7b72")
            self._draw_mic_icon(color="#ff7b72", state="idle")
        self.root.after(0, _update)

    def _on_cancel_click(self):
        """点击取消按钮"""
        self.coordinator.cancel_session()

    def _retry_inject(self):
        """重试向当前激活窗口注入上次的识别文本"""
        res = self.coordinator.retry_last_injection()
        if res.success:
            self.set_feedback("✓ 已补录到光标处", color="#00ffc4")
        else:
            self.set_feedback(f"✕ 补录失败: {res.message}", color="#ff7b72")

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
        self._apply_rounded_shape(self.capsule_width, self.expanded_height, radius=32)
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
            self._apply_rounded_shape(self.capsule_width, self.collapsed_height, radius=38)
            if self.partial_label:
                self.partial_label.pack(side=tk.TOP, fill=tk.X)
                self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#8b949e")
            self.main_frame.configure(highlightcolor="#30363d", highlightbackground="#30363d")
            self.status_label.configure(text="点击语音输入", fg="#e6edf3")
            self.status_label.bind("<Button-1>", self._on_left_click)
            self._update_model_badge()

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
            self._apply_rounded_shape(self.capsule_width, self.collapsed_height, radius=38)

    def start(self):
        """在当前线程启动 Tk 消息循环"""
        self._is_running = True
        self.create_window()
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
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

    # --- 协调器观察者异步安全调度 ---

    def _handle_state_change(self, state: SessionState, sess):
        if not self.root:
            return
        self.root.after(0, self._apply_state_change, state, sess)

    def _apply_state_change(self, state: SessionState, sess):
        if state == SessionState.RECORDING:
            self._expand_vertically()
            self.main_frame.configure(highlightcolor="#ff4b4b", highlightbackground="#ff4b4b")
            self.status_label.configure(text="● 正在聆听", fg="#ff7b72")
            if self.stream_label:
                self.stream_label.configure(text="请说话，实时出字中…", fg="#8b949e")
            if self.partial_label:
                self.partial_label.configure(text="边说边出字中…", fg="#8b949e")
            self.btn_cancel.pack(side=tk.RIGHT, padx=6)

        elif state in (SessionState.DRAINING, SessionState.FINALIZING):
            self.status_label.configure(text="⌛ 正在纠错与标点…", fg="#d29922")
            self.main_frame.configure(highlightcolor="#d29922", highlightbackground="#d29922")
            self.btn_cancel.pack_forget()

        elif state == SessionState.INJECTING:
            self.status_label.configure(text="⚡ 正在自动输入…", fg="#58a6ff")

        elif state == SessionState.ERROR:
            self.btn_cancel.pack_forget()
            err_msg = getattr(self.coordinator, "last_error", "") or "录音启动失败"
            self.set_feedback(f"✕ {err_msg}", color="#ff7b72")

        elif state == SessionState.IDLE:
            self.btn_cancel.pack_forget()
            self._draw_mic_icon(color="#00ffc4", state="idle")

    def _handle_partial_text(self, confirmed: str, partial: str):
        if not self.root:
            return
        display_text = f"{confirmed} {partial}".strip()
        if display_text:
            self.root.after(0, self._apply_partial_text, display_text)

    def _apply_partial_text(self, text: str):
        if self.stream_label:
            self.stream_label.configure(text=f"“{text}”", fg="#58a6ff")
        if self.partial_label:
            shown = text[-25:] if len(text) > 25 else text
            self.partial_label.configure(text=f"“{shown}”", fg="#58a6ff")

    def _handle_final_result(self, text: str, res: InjectionResult):
        if not self.root:
            return
        self.root.after(0, self._apply_final_result, text, res)

    def _apply_final_result(self, text: str, res: InjectionResult):
        if not text:
            self.set_feedback("未识别到有效语音", color="#8b949e")
            return

        disp = text[:22] + "…" if len(text) > 22 else text
        if res.success:
            self.set_feedback(f"✓ 已自动输入: {disp}", color="#00ffc4")
        else:
            if res.outcome == InjectionOutcome.FOCUS_CHANGED:
                self.set_feedback("⚠️ 目标窗口已切换 (点击补录)", color="#d29922", clickable=True)
            else:
                self.set_feedback(f"✕ {res.message}", color="#ff7b72")

    def _handle_audio_level(self, level: float):
        self._current_audio_level = level

    def set_feedback(self, text: str, color: str = "#00ffc4", clickable: bool = False):
        """展示完成或警告反馈，并在 2.2 秒后自动恢复待机微型胶囊"""
        self.status_label.configure(text=text, fg=color)
        if self.stream_label:
            self.stream_label.configure(text=text, fg=color)
        if self.partial_label:
            self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#8b949e")
        self.main_frame.configure(highlightcolor=color, highlightbackground=color)

        if clickable:
            self.status_label.bind("<Button-1>", lambda e: self._retry_inject())
        else:
            self.status_label.bind("<Button-1>", self._on_left_click)

        # 2.2秒后复原胶囊
        self.root.after(2200, self._collapse_capsule)
