"""全局热键与鼠标侧键监听状态机：支持鼠标侧键 (X1/X2) 以及键盘热键 (F8/CapsLock)"""

import enum
import threading
import time
from typing import Callable, Optional

try:
    from pynput import keyboard, mouse
except ImportError:
    keyboard = None
    mouse = None


class HotkeyState(enum.Enum):
    IDLE = "IDLE"
    PRESSED = "PRESSED"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"


# 鼠标按键别名映射
MOUSE_BUTTON_ALIASES = {
    "mouse_x1": "x1",
    "x1": "x1",
    "mouse4": "x1",
    "mouse_back": "x1",
    "side": "x1",
    "side_back": "x1",
    "mouse_x2": "x2",
    "x2": "x2",
    "mouse5": "x2",
    "mouse_forward": "x2",
    "side_forward": "x2",
    "middle": "middle",
    "mouse_middle": "middle",
}


class HotkeyController:
    def __init__(
        self,
        hotkey_name: str = "mouse_x1",
        threshold_seconds: float = 0.25,
        on_start_record: Optional[Callable[[], None]] = None,
        on_stop_record: Optional[Callable[[], None]] = None,
        on_cancel: Optional[Callable[[], None]] = None,
    ):
        self.hotkey_name = hotkey_name.lower().strip()
        self.threshold_seconds = threshold_seconds
        self.on_start_record = on_start_record
        self.on_stop_record = on_stop_record
        self.on_cancel = on_cancel

        self._state = HotkeyState.IDLE
        self._press_time: float = 0.0
        self._keyboard_listener = None
        self._mouse_listener = None
        self._lock = threading.Lock()
        self._threshold_timer: Optional[threading.Timer] = None

    @property
    def state(self) -> HotkeyState:
        return self._state

    def is_mouse_trigger(self) -> bool:
        """判断当前触发键是否为鼠标按键"""
        return self.hotkey_name in MOUSE_BUTTON_ALIASES

    def get_display_name(self) -> str:
        """获取按键的人类友好显示名称"""
        if self.is_mouse_trigger():
            m_key = MOUSE_BUTTON_ALIASES.get(self.hotkey_name)
            if m_key == "x1":
                return "鼠标后侧键 (X1 / 后退键，大拇指侧键)"
            elif m_key == "x2":
                return "鼠标前侧键 (X2 / 前进键)"
            elif m_key == "middle":
                return "鼠标滚轮中键"
        elif self.hotkey_name in ("caps_lock", "capslock"):
            return "键盘 CapsLock 键 (长按说话，短按切换大小写)"
        return f"键盘 {self.hotkey_name.upper()} 键"

    def set_processing(self) -> None:
        with self._lock:
            self._state = HotkeyState.PROCESSING

    def set_idle(self) -> None:
        with self._lock:
            self._state = HotkeyState.IDLE

    def _match_mouse_button(self, button) -> bool:
        """检查鼠标按键是否匹配目标侧键"""
        if mouse is None:
            return False
        m_alias = MOUSE_BUTTON_ALIASES.get(self.hotkey_name)
        if m_alias == "x1":
            return button == getattr(mouse.Button, "x1", None)
        elif m_alias == "x2":
            return button == getattr(mouse.Button, "x2", None)
        elif m_alias == "middle":
            return button == getattr(mouse.Button, "middle", None)
        return False

    def _match_keyboard_key(self, key) -> bool:
        """检查键盘按键是否匹配目标热键"""
        if keyboard is None:
            return False
        if self.hotkey_name in ("f8", "f9", "f10", "f7"):
            target_attr = getattr(keyboard.Key, self.hotkey_name, None)
            return key == target_attr
        elif self.hotkey_name in ("caps_lock", "capslock"):
            return key == keyboard.Key.caps_lock
        elif self.hotkey_name in ("ctrl_r", "right_ctrl"):
            return key == keyboard.Key.ctrl_r
        return False

    def _on_threshold_reached(self):
        """长按达到阈值，真正触发录音"""
        with self._lock:
            if self._state == HotkeyState.PRESSED:
                self._state = HotkeyState.RECORDING
                if self.on_start_record:
                    threading.Thread(target=self.on_start_record, daemon=True).start()

    # ── 鼠标事件回调 ──
    def _on_mouse_click(self, x, y, button, pressed):
        if not self._match_mouse_button(button):
            return

        with self._lock:
            if pressed:
                # 忽略多重触发
                if self._state != HotkeyState.IDLE:
                    return
                self._state = HotkeyState.RECORDING
                self._press_time = time.monotonic()
                if self.on_start_record:
                    threading.Thread(target=self.on_start_record, daemon=True).start()
            else:
                # 松开鼠标侧键
                if self._state == HotkeyState.RECORDING:
                    self._state = HotkeyState.PROCESSING
                    if self.on_stop_record:
                        threading.Thread(target=self.on_stop_record, daemon=True).start()
                else:
                    self._state = HotkeyState.IDLE

    # ── 键盘事件回调 ──
    def _on_keyboard_press(self, key):
        # 随时按 Esc 可紧急取消录音
        if keyboard is not None and key == keyboard.Key.esc:
            with self._lock:
                if self._state in (HotkeyState.PRESSED, HotkeyState.RECORDING):
                    self._state = HotkeyState.IDLE
                    if self._threshold_timer:
                        self._threshold_timer.cancel()
                    if self.on_cancel:
                        threading.Thread(target=self.on_cancel, daemon=True).start()
            return

        # 若是鼠标模式，则忽略其他键盘按键
        if self.is_mouse_trigger():
            return

        if not self._match_keyboard_key(key):
            return

        with self._lock:
            if self._state != HotkeyState.IDLE:
                return

            self._state = HotkeyState.PRESSED
            self._press_time = time.monotonic()

            if self.hotkey_name in ("caps_lock", "capslock"):
                self._threshold_timer = threading.Timer(
                    self.threshold_seconds, self._on_threshold_reached
                )
                self._threshold_timer.daemon = True
                self._threshold_timer.start()
            else:
                self._state = HotkeyState.RECORDING
                if self.on_start_record:
                    threading.Thread(target=self.on_start_record, daemon=True).start()

    def _on_keyboard_release(self, key):
        if self.is_mouse_trigger() or not self._match_keyboard_key(key):
            return

        with self._lock:
            if self._threshold_timer:
                self._threshold_timer.cancel()
                self._threshold_timer = None

            if self._state == HotkeyState.RECORDING:
                self._state = HotkeyState.PROCESSING
                if self.on_stop_record:
                    threading.Thread(target=self.on_stop_record, daemon=True).start()
            else:
                self._state = HotkeyState.IDLE

    def start(self):
        """启动监听器（根据触发键类型自动注册鼠标或键盘钩子，并常驻 Esc 键盘取消钩子）"""
        if keyboard is None or mouse is None:
            raise RuntimeError("pynput 未安装，请在专属虚拟环境中安装 requirements.txt")

        # 1. 始终启动键盘监听（用于 Esc 紧急取消及键盘热键）
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_keyboard_press,
            on_release=self._on_keyboard_release,
        )
        self._keyboard_listener.daemon = True
        self._keyboard_listener.start()

        # 2. 如果是鼠标触发，启动鼠标钩子
        if self.is_mouse_trigger():
            self._mouse_listener = mouse.Listener(
                on_click=self._on_mouse_click,
            )
            self._mouse_listener.daemon = True
            self._mouse_listener.start()

    def stop(self):
        """停止所有监听钩子"""
        if self._keyboard_listener is not None:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None
