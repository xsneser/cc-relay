"""桌面轻量免激活悬浮胶囊客户端 (DesktopVoiceWidget)：
- 基于 Windows 原生 Tkinter + Win32 WS_EX_NOACTIVATE 扩展样式
- 零焦点抢夺：点击悬浮麦克风不会夺走目标窗口 (VS Code/终端/记事本) 的光标焦点！
- 实时流式因果出字预览 (Partial Streaming Text)
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
        self._current_audio_level = 0.0

        # 浮窗尺寸
        self.collapsed_width = 170
        self.expanded_width = 380
        self.height = 42

        self.initial_x = default_x
        self.initial_y = default_y

        # UI 元素引用
        self.canvas: Optional[tk.Canvas] = None
        self.status_label: Optional[tk.Label] = None
        self.partial_label: Optional[tk.Label] = None
        self.btn_cancel: Optional[tk.Label] = None

        # 动效计时器
        self._anim_timer = None
        self._pulse_phase = 0.0

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

    def create_window(self):
        """在主线程构建深色亚克力风格的悬浮胶囊界面"""
        self.root = tk.Tk()
        self.root.title("CC Relay Voice Capsule")

        # 无边框 + 保持置顶
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        # 默认停靠在屏幕顶部偏右位置
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        x = self.initial_x if self.initial_x >= 0 else screen_w - self.collapsed_width - 80
        y = self.initial_y if self.initial_y >= 0 else 60
        self.root.geometry(f"{self.collapsed_width}x{self.height}+{x}+{y}")

        # 背景深黑金属质感
        bg_color = "#13161c"
        border_color = "#30363d"
        self.root.configure(bg=bg_color)

        # 主胶囊框架 (圆润卡片)
        self.main_frame = tk.Frame(
            self.root,
            bg=bg_color,
            highlightbackground=border_color,
            highlightcolor="#00ffc4",
            highlightthickness=1,
            bd=0,
        )
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. 麦克风图标与动效画布 (Canvas)
        self.canvas = tk.Canvas(
            self.main_frame,
            width=36,
            height=36,
            bg=bg_color,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack(side=tk.LEFT, padx=(4, 2), pady=2)
        self._draw_mic_icon(color="#00ffc4", state="idle")

        # 2. 状态与提示文字
        self.info_container = tk.Frame(self.main_frame, bg=bg_color)
        self.info_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

        self.status_label = tk.Label(
            self.info_container,
            text="点击语音输入",
            font=("Segoe UI", 9, "bold"),
            fg="#e6edf3",
            bg=bg_color,
            anchor="w",
            cursor="hand2",
        )
        self.status_label.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))

        self.partial_label = tk.Label(
            self.info_container,
            text=f"快捷键: {self.coordinator.config.hotkey.upper()}",
            font=("Segoe UI", 8),
            fg="#8b949e",
            bg=bg_color,
            anchor="w",
            cursor="hand2",
        )
        self.partial_label.pack(side=tk.TOP, fill=tk.X)

        # 3. 取消按钮 (仅在录音态显现)
        self.btn_cancel = tk.Label(
            self.main_frame,
            text="✖",
            font=("Segoe UI", 10, "bold"),
            fg="#8b949e",
            bg=bg_color,
            cursor="hand2",
            padx=8,
        )
        self.btn_cancel.bind("<Button-1>", lambda e: self._on_cancel_click())
        self.btn_cancel.bind("<Enter>", lambda e: self.btn_cancel.configure(fg="#ff7b72"))
        self.btn_cancel.bind("<Leave>", lambda e: self.btn_cancel.configure(fg="#8b949e"))

        # 绑定点击与拖动手势
        for w in (self.main_frame, self.canvas, self.status_label, self.partial_label, self.info_container):
            w.bind("<Button-1>", self._on_left_click)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Button-3>", self._show_context_menu)

        # 右键上下文菜单
        self.context_menu = tk.Menu(self.root, tearoff=0, bg="#161b22", fg="#c9d1d9", activebackground="#1f6feb")
        self.context_menu.add_command(label="🎙️ 开始/停止录音", command=self._toggle_record)
        self.context_menu.add_command(label="📋 重新输入上次内容", command=self._retry_inject)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="✕ 隐藏胶囊", command=self.hide)

        # 窗口初次渲染完毕后，注入 Win32 非抢焦点样式
        self.root.update_idletasks()
        self._apply_win32_non_activating(self.root)

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
                # 依据位置与电平生成不同跳动高度
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
        self.root.after(50, self._start_animation_loop)

    def _on_left_click(self, event):
        """记录拖动起点，同时作为点击响应"""
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._dragged = False

    def _on_drag(self, event):
        """支持鼠标按住胶囊随意拖动吸附"""
        self._dragged = True
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        new_x = cur_x + dx
        new_y = cur_y + dy
        self.root.geometry(f"+{new_x}+{new_y}")

    def _toggle_record(self):
        """点击麦克风胶囊触发开始或停止录音"""
        if self.coordinator.is_recording:
            self.coordinator.stop_session(source="widget")
        else:
            self.coordinator.start_session(source="widget", mode="toggle", output_mode="inject")

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
            self.context_menu.post(event.x_root, event.y_root)

    def _set_window_width(self, width: int):
        """动态平滑调整胶囊宽度"""
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        self.root.geometry(f"{width}x{self.height}+{cur_x}+{cur_y}")

    # --- 协调器观察者异步安全调度 ---

    def _handle_state_change(self, state: SessionState, sess):
        if not self.root:
            return
        self.root.after(0, self._apply_state_change, state, sess)

    def _apply_state_change(self, state: SessionState, sess):
        if state == SessionState.RECORDING:
            self._set_window_width(self.expanded_width)
            self.main_frame.configure(highlightcolor="#ff4b4b", highlightbackground="#ff4b4b")
            self.status_label.configure(text="● 正在聆听 (请说话)", fg="#ff7b72")
            self.partial_label.configure(text="边说边出字中…", fg="#8b949e")
            self.btn_cancel.pack(side=tk.RIGHT, padx=4)

        elif state in (SessionState.DRAINING, SessionState.FINALIZING):
            self.status_label.configure(text="⌛ 2-Pass 纠错与标点中…", fg="#d29922")
            self.main_frame.configure(highlightcolor="#d29922", highlightbackground="#d29922")
            self.btn_cancel.pack_forget()

        elif state == SessionState.INJECTING:
            self.status_label.configure(text="⚡ 正在自动输入到目标窗口…", fg="#58a6ff")

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
        if self.partial_label:
            # 截断展示最新 25 个字符
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

        if res.success:
            self.set_feedback(f"✓ 已自动输入: {text[:15]}…", color="#00ffc4")
        else:
            if res.outcome == InjectionOutcome.FOCUS_CHANGED:
                self.set_feedback("⚠️ 目标窗口已切换 (点击补录)", color="#d29922", clickable=True)
            else:
                self.set_feedback(f"✕ {res.message}", color="#ff7b72")

    def _handle_audio_level(self, level: float):
        self._current_audio_level = level

    def set_feedback(self, text: str, color: str = "#00ffc4", clickable: bool = False):
        """展示完成或警告反馈，并在 2.0 秒后自动恢复待机微型胶囊"""
        self.status_label.configure(text=text, fg=color)
        self.partial_label.configure(text=f"快捷键: {self.coordinator.config.hotkey.upper()}", fg="#8b949e")
        self.main_frame.configure(highlightcolor=color, highlightbackground=color)

        if clickable:
            self.status_label.bind("<Button-1>", lambda e: self._retry_inject())
        else:
            self.status_label.bind("<Button-1>", self._on_left_click)

        # 2秒后复原胶囊
        self.root.after(2200, self._collapse_capsule)

    def _collapse_capsule(self):
        """平滑收缩恢复到小巧状态"""
        if not self.coordinator.is_recording and self.root:
            self._set_window_width(self.collapsed_width)
            self.main_frame.configure(highlightcolor="#30363d", highlightbackground="#30363d")
            self.status_label.configure(text="点击语音输入", fg="#e6edf3")
            self.status_label.bind("<Button-1>", self._on_left_click)

    def hide(self):
        """隐藏悬浮窗"""
        if self.root:
            self.root.withdraw()

    def show(self):
        """显示悬浮窗"""
        if self.root:
            self.root.deiconify()
            self._apply_win32_non_activating(self.root)

    def start(self):
        """在当前线程启动 Tk 消息循环"""
        self._is_running = True
        self.create_window()
        self.root.mainloop()

    def stop(self):
        """关闭悬浮窗"""
        self._is_running = False
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None
