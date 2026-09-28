"""全局热键与鼠标侧键监听状态机：支持鼠标侧键 (X1/X2) 以及键盘热键 (F8/CapsLock)"""

import enum
import sys
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

KEY_ALIASES = {
    "capslock": "caps_lock",
    "right_ctrl": "ctrl_r",
    "right_shift": "shift_r",
    "right_alt": "alt_r",
    "left_ctrl": "ctrl_l",
    "left_shift": "shift_l",
    "left_alt": "alt_l",
}

# Windows 虚拟键码常量 (Win32)
VK_MOUSE_BUTTONS = {
    "x1": 0x05,      # VK_XBUTTON1 (鼠标后侧键 / 后退键)
    "x2": 0x06,      # VK_XBUTTON2 (鼠标前侧键 / 前进键)
    "middle": 0x04,  # VK_MBUTTON (鼠标滚轮中键)
}
VK_ESCAPE = 0x1B     # VK_ESCAPE (Esc 紧急取消)


class WindowsMousePoller:
    """Windows 原生物理按键轻量异步轮询器：
    - 使用 user32.GetAsyncKeyState 轮询鼠标侧键 (X1/X2) 与滚轮中键
    - 绝不安装全局鼠标钩子 (WH_MOUSE_LL)，彻底避免拦截 WM_MOUSEMOVE 导致的光标卡顿与漂移
    - 零 GIL 竞争干扰操作系统光标流水线，即使 Python CPU 满载鼠标依旧绝对平滑
    - 内置 VK_ESCAPE 紧急取消监听，无需启动全局键盘钩子
    """

    def __init__(
        self,
        vk_code: int,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
        poll_interval: float = 0.015,
    ):
        self.vk_code = vk_code
        self.on_press = on_press
        self.on_release = on_release
        self.on_cancel = on_cancel
        self.poll_interval = poll_interval

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="WindowsMousePoller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        self._thread = None

    def _run(self):
        import ctypes
        try:
            get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
        except Exception:
            return

        def _is_down(vk: int) -> bool:
            try:
                return bool(get_async_key_state(vk) & 0x8000)
            except Exception:
                return False

        # 初始化当前物理按键状态（若启动时键已被按住，则不误触发，需松开后再次按下才生效）
        was_pressed = _is_down(self.vk_code)
        was_esc_pressed = _is_down(VK_ESCAPE)

        while not self._stop_event.is_set():
            # 1. 检测 Esc 紧急取消
            if self.on_cancel:
                esc_down = _is_down(VK_ESCAPE)
                if esc_down and not was_esc_pressed:
                    was_esc_pressed = True
                    try:
                        self.on_cancel()
                    except Exception:
                        pass
                elif not esc_down:
                    was_esc_pressed = False

            # 2. 检测目标鼠标按键物理边缘触发
            is_down = _is_down(self.vk_code)
            if is_down != was_pressed:
                was_pressed = is_down
                if is_down:
                    try:
                        self.on_press()
                    except Exception:
                        pass
                else:
                    try:
                        self.on_release()
                    except Exception:
                        pass

            self._stop_event.wait(self.poll_interval)


def key_to_name(key) -> Optional[str]:
    """将 pynput 按键对象规范化为小写字符串名称"""
    if key is None:
        return None
    if hasattr(key, "name") and key.name:
        name = str(key.name).lower().strip()
        return KEY_ALIASES.get(name, name)
    if hasattr(key, "char") and key.char:
        return str(key.char).lower().strip()
    if hasattr(key, "vk") and key.vk:
        if 0x70 <= key.vk <= 0x87:
            return f"f{key.vk - 0x70 + 1}"
    return str(key).lower().strip().replace("key.", "")


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
        self._mouse_poller: Optional[WindowsMousePoller] = None
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
            return f"鼠标按键 [{self.hotkey_name}]"

        target = KEY_ALIASES.get(self.hotkey_name, self.hotkey_name)
        if target in ("caps_lock", "capslock"):
            return "键盘 CapsLock 键 (长按说话，短按切换大小写)"
        elif target == "ctrl_r":
            return "键盘 右Ctrl 键"
        elif target == "shift_r":
            return "键盘 右Shift 键"
        elif target == "alt_r":
            return "键盘 右Alt 键"
        elif target.startswith("f") and target[1:].isdigit():
            return f"键盘 {target.upper()} 键"
        elif target == "space":
            return "键盘 空格 (Space) 键"
        elif target == "tab":
            return "键盘 Tab 键"
        return f"键盘 [{target.upper()}] 键"

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
        if key is None:
            return False
        k_name = key_to_name(key)
        target = KEY_ALIASES.get(self.hotkey_name, self.hotkey_name)
        if k_name == target:
            return True
        if keyboard is not None:
            target_attr = getattr(keyboard.Key, target, None)
            if target_attr is not None and key == target_attr:
                return True
        return False

    def _on_threshold_reached(self):
        """长按达到阈值，真正触发录音"""
        with self._lock:
            if self._state == HotkeyState.PRESSED:
                self._state = HotkeyState.RECORDING
                if self.on_start_record:
                    threading.Thread(target=self.on_start_record, daemon=True).start()

    # ── 触发事件核心调度 ──
    def _on_mouse_trigger_down(self):
        """鼠标按键按下"""
        with self._lock:
            if self._state != HotkeyState.IDLE:
                return
            self._state = HotkeyState.RECORDING
            self._press_time = time.monotonic()
            if self.on_start_record:
                threading.Thread(target=self.on_start_record, daemon=True).start()

    def _on_mouse_trigger_up(self):
        """鼠标按键松开"""
        with self._lock:
            if self._state == HotkeyState.RECORDING:
                self._state = HotkeyState.PROCESSING
                if self.on_stop_record:
                    threading.Thread(target=self.on_stop_record, daemon=True).start()
            else:
                self._state = HotkeyState.IDLE

    def _on_esc_cancel(self):
        """Esc 键紧急取消"""
        with self._lock:
            if self._state in (HotkeyState.PRESSED, HotkeyState.RECORDING):
                self._state = HotkeyState.IDLE
                if self._threshold_timer:
                    self._threshold_timer.cancel()
                    self._threshold_timer = None
                if self.on_cancel:
                    threading.Thread(target=self.on_cancel, daemon=True).start()

    # ── 鼠标事件回调 (用于 pynput 回退) ──
    def _on_mouse_click(self, x, y, button, pressed):
        if not self._match_mouse_button(button):
            return
        if pressed:
            self._on_mouse_trigger_down()
        else:
            self._on_mouse_trigger_up()

    # ── 键盘事件回调 ──
    def _on_keyboard_press(self, key):
        # 随时按 Esc 可紧急取消录音
        if keyboard is not None and key == keyboard.Key.esc:
            self._on_esc_cancel()
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
        """启动监听器（Windows 鼠标热键默认启用零钩子异步轮询，彻底避免 WH_MOUSE_LL 鼠标卡顿）"""
        self.stop()

        # 1. 鼠标按键触发
        if self.is_mouse_trigger():
            m_alias = MOUSE_BUTTON_ALIASES.get(self.hotkey_name)
            vk = VK_MOUSE_BUTTONS.get(m_alias)
            # 在 Windows 平台下且匹配已知按键，使用 GetAsyncKeyState 零钩子轮询
            if sys.platform == "win32" and vk is not None:
                self._mouse_poller = WindowsMousePoller(
                    vk_code=vk,
                    on_press=self._on_mouse_trigger_down,
                    on_release=self._on_mouse_trigger_up,
                    on_cancel=self._on_esc_cancel,
                )
                self._mouse_poller.start()
                return

            # 非 Windows 平台或不支持轮询：回退到 pynput
            if keyboard is None or mouse is None:
                raise RuntimeError("pynput 未安装，请在专属虚拟环境中安装 requirements.txt")

            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_keyboard_press,
                on_release=self._on_keyboard_release,
            )
            self._keyboard_listener.daemon = True
            self._keyboard_listener.start()

            self._mouse_listener = mouse.Listener(
                on_click=self._on_mouse_click,
            )
            self._mouse_listener.daemon = True
            self._mouse_listener.start()
            return

        # 2. 键盘按键触发 (CapsLock, F8, Ctrl_R 等)
        if keyboard is None:
            raise RuntimeError("pynput 未安装，请在专属虚拟环境中安装 requirements.txt")

        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_keyboard_press,
            on_release=self._on_keyboard_release,
        )
        self._keyboard_listener.daemon = True
        self._keyboard_listener.start()

    def stop(self):
        """停止所有监听钩子与轮询线程"""
        with self._lock:
            if self._threshold_timer:
                self._threshold_timer.cancel()
                self._threshold_timer = None
            self._state = HotkeyState.IDLE

        if self._mouse_poller is not None:
            self._mouse_poller.stop()
            self._mouse_poller = None
        if self._keyboard_listener is not None:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None
