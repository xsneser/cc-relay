"""Win32 文本注入与剪贴板安全操作模块：
- 严格的 64 位 ctypes API 签名与异常保护
- UTF-16 字节精确分配与内存所有权安全流转
- 前台目标窗口 HWND 与 PID 双重一致性校验
- Win32 SendInput 模拟 Ctrl+V 注入与现场剪贴板无感恢复
- 结构化注入结果返回 (支持向后兼容布尔判断)
"""

import ctypes
import ctypes.wintypes
import enum
import threading
import time
import unicodedata
from typing import NamedTuple, Optional, Tuple

# Win32 常量
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

VK_CONTROL = 0x11
VK_V = 0x56
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
SW_RESTORE = 9
SW_SHOW = 5

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Win32 结构体定义 (严格对齐 64 位平台)
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


VK_BACK = 0x08
INJECTED_INPUT_MARKER = 0x4343524C59564F49
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
LLMHF_INJECTED = 0x01
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E
WM_QUIT = 0x0012


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
)


# 显式声明 Win32 API 签名，彻底杜绝指针截断
try:
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.wintypes.HWND

    user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.OpenClipboard.restype = ctypes.wintypes.BOOL

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.wintypes.BOOL

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.wintypes.BOOL

    user32.IsClipboardFormatAvailable.argtypes = [ctypes.wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = ctypes.wintypes.BOOL

    user32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.wintypes.HANDLE

    user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
    user32.SetClipboardData.restype = ctypes.wintypes.HANDLE

    user32.SendInput.argtypes = [ctypes.wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = ctypes.wintypes.UINT

    kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL

    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p

    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL

    kernel32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL

    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL

    user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
    user32.IsIconic.restype = ctypes.wintypes.BOOL

    user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.wintypes.BOOL

    user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
    user32.BringWindowToTop.restype = ctypes.wintypes.BOOL

    user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
    user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL

    user32.AttachThreadInput.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL]
    user32.AttachThreadInput.restype = ctypes.wintypes.BOOL

    user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.wintypes.DWORD, ULONG_PTR]
    user32.keybd_event.restype = None

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = ctypes.wintypes.HWND

    user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
    user32.GetAncestor.restype = ctypes.wintypes.HWND
except Exception:
    pass


class TargetWindowSnapshot(NamedTuple):
    hwnd: int
    pid: int
    title: str


def get_foreground_window() -> int:
    """获取当前前台激活窗口句柄 (HWND)"""
    try:
        return user32.GetForegroundWindow() or 0
    except Exception:
        return 0


def get_window_pid(hwnd: int) -> int:
    """获取窗口归属的进程 PID"""
    if not hwnd:
        return 0
    pid = ctypes.wintypes.DWORD()
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def get_window_title(hwnd: int) -> str:
    """获取窗口标题"""
    if not hwnd:
        return ""
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value
    except Exception:
        pass
    return ""


def capture_target_snapshot() -> TargetWindowSnapshot:
    """捕获当前焦点窗口的快照 (HWND, PID, Title)"""
    hwnd = get_foreground_window()
    pid = get_window_pid(hwnd)
    title = get_window_title(hwnd)
    return TargetWindowSnapshot(hwnd=hwnd, pid=pid, title=title)


def is_window_valid(hwnd: int) -> bool:
    """检查窗口句柄在 Win32 系统中是否仍有效且存在"""
    if not hwnd or hwnd == 0:
        return False
    try:
        is_win = getattr(user32, "IsWindow", None)
        if callable(is_win):
            return bool(is_win(hwnd))
        return True
    except Exception:
        return False


def restore_foreground_window(target_hwnd: int, timeout: float = 0.25) -> bool:
    """安全将目标窗口置顶并激活为前台活动焦点

    使用 AttachThreadInput + keybd_event(VK_MENU) 绕过 Windows Foreground Lock 限制
    """
    if not is_window_valid(target_hwnd):
        return False

    try:
        cur_fg = get_foreground_window()
        if cur_fg == target_hwnd:
            return True

        # 1. 若窗口最小化则恢复，否则正常展现
        try:
            if hasattr(user32, "IsIconic") and user32.IsIconic(target_hwnd):
                user32.ShowWindow(target_hwnd, SW_RESTORE)
            elif hasattr(user32, "ShowWindow"):
                user32.ShowWindow(target_hwnd, SW_SHOW)
        except Exception:
            pass

        # 2. 线程输入队列挂载，绕过 Windows 前台锁定
        cur_pid = ctypes.wintypes.DWORD()
        fg_thread = user32.GetWindowThreadProcessId(cur_fg, ctypes.byref(cur_pid)) if cur_fg else 0
        my_thread = kernel32.GetCurrentThreadId() if hasattr(kernel32, "GetCurrentThreadId") else 0

        attached = False
        if fg_thread and my_thread and fg_thread != my_thread and hasattr(user32, "AttachThreadInput"):
            try:
                attached = bool(user32.AttachThreadInput(my_thread, fg_thread, True))
            except Exception:
                attached = False

        try:
            if hasattr(user32, "BringWindowToTop"):
                user32.BringWindowToTop(target_hwnd)
            if hasattr(user32, "SetForegroundWindow"):
                user32.SetForegroundWindow(target_hwnd)
            if get_foreground_window() != target_hwnd and hasattr(user32, "keybd_event"):
                # 模拟轻触 Alt (VK_MENU) 触发系统前台转移授权 (附带本程序注入标记免受活动监控)
                user32.keybd_event(VK_MENU, 0, 0, INJECTED_INPUT_MARKER)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, INJECTED_INPUT_MARKER)
                if hasattr(user32, "SetForegroundWindow"):
                    user32.SetForegroundWindow(target_hwnd)
        except Exception:
            pass
        finally:
            if attached and fg_thread and my_thread and hasattr(user32, "AttachThreadInput"):
                try:
                    user32.AttachThreadInput(my_thread, fg_thread, False)
                except Exception:
                    pass

        # 3. 轮询等待窗口成为前台活动焦点
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if get_foreground_window() == target_hwnd:
                return True
            time.sleep(0.015)
        return get_foreground_window() == target_hwnd
    except Exception:
        return False


def set_clipboard_text(text: str, owner_hwnd: Optional[int] = None) -> bool:
    """将 Unicode 文本写入系统剪贴板 (CF_UNICODETEXT)，确保 UTF-16LE 字节正确与内存所有权安全"""
    if not text:
        return False

    # 尝试重试打开剪贴板 (处理并发冲突)
    opened = False
    for _ in range(5):
        if user32.OpenClipboard(owner_hwnd or 0):
            opened = True
            break
        time.sleep(0.01)

    if not opened:
        return False

    h_mem = None
    try:
        user32.EmptyClipboard()
        # 精确转换为 UTF-16LE 字节并追加两字节空终止符 \x00\x00
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        size = len(encoded)

        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, size)
        if not h_mem:
            return False

        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            h_mem = None
            return False

        ctypes.memmove(ptr, encoded, size)
        kernel32.GlobalUnlock(h_mem)

        # 移交内存所有权给 Windows 系统剪贴板
        res = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        if res:
            # 成功后操作系统接管 h_mem，禁止自主释放
            h_mem = None
            return True
        return False
    finally:
        if h_mem:
            kernel32.GlobalFree(h_mem)
        user32.CloseClipboard()


def get_clipboard_text() -> Optional[str]:
    """读取当前剪贴板的纯文本内容 (用于注入后恢复现场)"""
    opened = False
    for _ in range(3):
        if user32.OpenClipboard(0):
            opened = True
            break
        time.sleep(0.01)

    if not opened:
        return None

    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h_mem = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_mem:
            return None
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h_mem)
    finally:
        user32.CloseClipboard()


def _key_input(vk_code: int, key_up: bool = False) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.union.ki.wVk = vk_code
    item.union.ki.wScan = 0
    item.union.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    item.union.ki.time = 0
    item.union.ki.dwExtraInfo = INJECTED_INPUT_MARKER
    return item


def send_paste_keystrokes() -> bool:
    """通过 Win32 SendInput 模拟按下并释放 Ctrl+V。"""
    inputs = (INPUT * 4)(
        _key_input(VK_CONTROL),
        _key_input(VK_V),
        _key_input(VK_V, key_up=True),
        _key_input(VK_CONTROL, key_up=True),
    )
    sent = user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return sent == 4


def send_backspace_and_paste_keystrokes(backspace_count: int) -> bool:
    """Delete the previously pasted plain-text span, then paste its replacement."""
    if backspace_count <= 0:
        return False
    keys = []
    for _ in range(backspace_count):
        keys.extend((_key_input(VK_BACK), _key_input(VK_BACK, key_up=True)))
    keys.extend((
        _key_input(VK_CONTROL),
        _key_input(VK_V),
        _key_input(VK_V, key_up=True),
        _key_input(VK_CONTROL, key_up=True),
    ))
    inputs = (INPUT * len(keys))(*keys)
    sent = user32.SendInput(len(inputs), ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return sent == len(inputs)


def safe_backspace_count(text: str) -> Optional[int]:
    """Return a conservative key count for simple one-line text, else None."""
    if not isinstance(text, str) or not text or len(text) > 512:
        return None
    for char in text:
        codepoint = ord(char)
        if codepoint < 0x20 or codepoint == 0x7F or codepoint > 0xFFFF:
            return None
        if char in ("\r", "\n", "\t", "‍", "︎", "️"):
            return None
        if unicodedata.category(char).startswith("M"):
            return None
    return len(text)


class InjectionOutcome(enum.Enum):
    SUCCESS = "success"
    EMPTY_TEXT = "empty_text"
    FOCUS_CHANGED = "focus_changed"
    TARGET_CLOSED = "target_closed"
    CLIPBOARD_ERROR = "clipboard_error"
    SENDINPUT_ERROR = "sendinput_error"
    ACTIVITY_CHANGED = "activity_changed"
    UNSAFE_TEXT = "unsafe_text"


class InjectionResult:
    def __init__(self, success: bool, outcome: InjectionOutcome, message: str = ""):
        self.success = success
        self.outcome = outcome
        self.message = message

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        return f"<InjectionResult success={self.success} outcome={self.outcome.value} msg='{self.message}'>"


class InputActivityMonitor:
    """Observe non-self keyboard/mouse activity and foreground changes during a paste lease."""

    def __init__(self):
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._watch_stop = threading.Event()
        self._hook_stop = threading.Event()
        self._thread = None
        self._watch_thread = None
        self._thread_id = 0
        self._keyboard_hook = None
        self._mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None
        self._startup_ok = False
        self._generation = 0
        self._lease_id = 0
        self._active_lease = None
        self._invalid = False

    def _mark_activity(self):
        with self._lock:
            if self._active_lease is not None:
                self._generation += 1
                self._invalid = True

    def _on_keyboard(self, code, message, lparam):
        if code == HC_ACTION and message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                self_generated = False
                if lparam:
                    event = ctypes.cast(ctypes.c_void_p(lparam), ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self_generated = (
                        event.dwExtraInfo == INJECTED_INPUT_MARKER
                        and bool(event.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                    )
                if not self_generated:
                    with self._lock:
                        lease = self._active_lease
                    if lease:
                        _, _, target_hwnd, target_pid = lease
                        # 仅当按键发生在目标窗口前台时，才标记目标输入被干扰
                        if get_foreground_window() == target_hwnd:
                            self._mark_activity()
            except Exception:
                pass
        return user32.CallNextHookEx(self._keyboard_hook, code, message, lparam)

    def _on_mouse(self, code, message, lparam):
        # 仅关注会产生实际交互的按下与滚轮事件，忽略普通松开事件（如松开录音侧键）和移动
        tracked = message in (
            WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN,
            WM_MOUSEWHEEL, WM_MOUSEHWHEEL,
        )
        if code == HC_ACTION and tracked:
            try:
                self_generated = False
                event = None
                if lparam:
                    event = ctypes.cast(ctypes.c_void_p(lparam), ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    self_generated = (
                        event.dwExtraInfo == INJECTED_INPUT_MARKER
                        and bool(event.flags & LLMHF_INJECTED)
                    )
                if not self_generated:
                    with self._lock:
                        lease = self._active_lease
                    if lease:
                        _, _, target_hwnd, target_pid = lease
                        click_in_target = False
                        try:
                            if event and hasattr(user32, "WindowFromPoint") and hasattr(user32, "GetAncestor"):
                                pt = POINT(event.pt.x, event.pt.y)
                                clicked_hwnd = user32.WindowFromPoint(pt)
                                root_hwnd = user32.GetAncestor(clicked_hwnd, 2) or clicked_hwnd
                                click_in_target = bool(root_hwnd == target_hwnd or clicked_hwnd == target_hwnd)
                            else:
                                click_in_target = bool(get_foreground_window() == target_hwnd)
                        except Exception:
                            click_in_target = bool(get_foreground_window() == target_hwnd)

                        if click_in_target:
                            self._mark_activity()
            except Exception:
                pass
        return user32.CallNextHookEx(self._mouse_hook, code, message, lparam)

    def _hook_loop(self):
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, _HOOKPROC, ctypes.c_void_p, ctypes.wintypes.DWORD
            ]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
            ]
            self._keyboard_proc = _HOOKPROC(self._on_keyboard)
            self._mouse_proc = _HOOKPROC(self._on_mouse)
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
            module = kernel32.GetModuleHandleW(None)
            self._keyboard_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._keyboard_proc, module, 0
            )
            self._mouse_hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._mouse_proc, module, 0
            )
            self._startup_ok = bool(self._keyboard_hook and self._mouse_hook)
            if not self._startup_ok:
                self._ready.set()
                return
            msg = ctypes.wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(ctypes.wintypes.MSG), ctypes.wintypes.HWND,
                ctypes.wintypes.UINT, ctypes.wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int
            self._ready.set()
            while not self._hook_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._startup_ok = False
        finally:
            try:
                if self._keyboard_hook:
                    user32.UnhookWindowsHookEx(self._keyboard_hook)
            except Exception:
                pass
            try:
                if self._mouse_hook:
                    user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._keyboard_hook = None
            self._mouse_hook = None
            self._ready.set()

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._hook_stop.is_set():
                    return False
                return self._ready.wait(0.1) and self._startup_ok
            self._ready.clear()
            self._hook_stop.clear()
            self._startup_ok = False
            self._thread = threading.Thread(target=self._hook_loop, name="VoiceInputActivityHooks", daemon=True)
            self._thread.start()
        return self._ready.wait(0.75) and self._startup_ok

    def prewarm(self) -> None:
        """Pre-warm the hook thread in background so begin_lease doesn't incur startup delay."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
        threading.Thread(target=self.start, name="VoicePrewarmHooks", daemon=True).start()

    def begin_lease(self, hwnd: int, pid: int):
        if not self.start() or not hwnd or not pid:
            return None
        with self._lock:
            before = self._generation
        if not is_window_valid(hwnd) or get_window_pid(hwnd) != pid:
            self.end_lease()
            return None
        with self._lock:
            generation_changed = before != self._generation
            if not generation_changed:
                self._lease_id += 1
                token = (self._lease_id, self._generation, int(hwnd), int(pid))
                self._active_lease = token
                self._invalid = False
                self._watch_stop.clear()
                self._watch_thread = threading.Thread(
                    target=self._watch_target, args=(token,), name="VoiceInputTargetGuard", daemon=True
                )
                self._watch_thread.start()
        if generation_changed:
            self.end_lease()
            return None
        return token

    def _watch_target(self, token):
        while not self._watch_stop.wait(0.05):
            with self._lock:
                if self._active_lease != token or self._invalid:
                    return
            _, _, hwnd, pid = token
            if not is_window_valid(hwnd) or get_window_pid(hwnd) != pid:
                self._mark_activity()
                return

    def is_valid(self, token) -> bool:
        with self._lock:
            valid = bool(
                token and self._active_lease == token and not self._invalid
                and self._generation == token[1]
            )
        if not valid:
            return False
        _, _, hwnd, pid = token
        if not is_window_valid(hwnd) or get_window_pid(hwnd) != pid:
            self._mark_activity()
            return False
        return True

    def end_lease(self, token=None):
        with self._lock:
            if token is not None and self._active_lease != token:
                return
            self._active_lease = None
            self._invalid = False
            self._watch_stop.set()
            watch_thread = self._watch_thread
            self._watch_thread = None
        if watch_thread and watch_thread is not threading.current_thread():
            watch_thread.join(timeout=0.1)
        self._stop_hooks()

    def _stop_hooks(self):
        with self._lock:
            hook_thread = self._thread
            thread_id = self._thread_id
            if hook_thread:
                self._hook_stop.set()
        if thread_id:
            try:
                user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if hook_thread and hook_thread is not threading.current_thread():
            hook_thread.join(timeout=0.75)
        with self._lock:
            if not hook_thread or not hook_thread.is_alive():
                self._thread = None
                self._thread_id = 0
                self._ready.clear()

    def stop(self):
        self.end_lease()


class WindowsInjector:
    def __init__(
        self,
        restore_clipboard: bool = True,
        restore_delay: float = 0.15,
        auto_reactivate_target: bool = True,
        restore_switched_focus: bool = True,
    ):
        self.restore_clipboard = restore_clipboard
        self.restore_delay = restore_delay
        self.auto_reactivate_target = auto_reactivate_target
        self.restore_switched_focus = restore_switched_focus
        self.activity_monitor = InputActivityMonitor()
        self.activity_monitor.prewarm()

    def close(self):
        self.activity_monitor.stop()

    def prewarm(self):
        if hasattr(self, "activity_monitor") and hasattr(self.activity_monitor, "prewarm"):
            self.activity_monitor.prewarm()

    def begin_input_lease(self, target_hwnd: int, target_pid: int):
        return self.activity_monitor.begin_lease(target_hwnd, target_pid)

    def end_input_lease(self, lease=None):
        self.activity_monitor.end_lease(lease)

    def inject_text(
        self,
        text: str,
        target_hwnd: Optional[int] = None,
        target_pid: Optional[int] = None,
    ) -> InjectionResult:
        """安全注入文本到前台目标窗口

        1. 严格校验目标窗口 HWND 与 PID 是否仍处于前台活动焦点；若焦点切换且开启自动切回，则自动恢复目标窗口焦点
        2. 写入 Unicode 剪贴板并模拟 Ctrl+V
        3. 延迟恢复用户先前剪贴板内容
        4. 若此前用户已切换至其他窗口，且开启了归还焦点选项，则在粘贴完成后平滑归还焦点
        """
        if not text:
            return InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "待注入文本为空")

        # 焦点校验与自动切回
        current_hwnd = get_foreground_window()
        current_pid = get_window_pid(current_hwnd)
        switched_from_hwnd = 0

        if target_hwnd is not None and target_hwnd != 0:
            if current_hwnd != target_hwnd:
                if not is_window_valid(target_hwnd):
                    return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, f"目标窗口已关闭 (HWND: {target_hwnd})")

                if not self.auto_reactivate_target:
                    msg = f"窗口焦点已切换: 目标={target_hwnd} 当前={current_hwnd} ({get_window_title(current_hwnd)})"
                    return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, msg)

                # 记录用户已切换至的窗口，用于注入后归还焦点
                switched_from_hwnd = current_hwnd
                if not restore_foreground_window(target_hwnd):
                    msg = f"无法恢复目标窗口焦点: 目标={target_hwnd} 当前={current_hwnd} ({get_window_title(current_hwnd)})"
                    return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, msg)

                # 刷新切回后的前台窗口及进程信息
                current_hwnd = get_foreground_window()
                current_pid = get_window_pid(current_hwnd)

            if target_pid is not None and target_pid != 0 and current_pid != target_pid:
                msg = f"目标进程已变更: 目标PID={target_pid} 当前PID={current_pid}"
                return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, msg)

        # 备份当前剪贴板
        old_clipboard = get_clipboard_text() if self.restore_clipboard else None

        # 写入新内容
        if not set_clipboard_text(text):
            return InjectionResult(False, InjectionOutcome.CLIPBOARD_ERROR, "写入系统剪贴板失败")

        # 触发模拟粘贴
        if not send_paste_keystrokes():
            return InjectionResult(False, InjectionOutcome.SENDINPUT_ERROR, "模拟 Ctrl+V 按键失败")

        # 延迟恢复剪贴板
        if self.restore_clipboard and old_clipboard is not None:
            time.sleep(self.restore_delay)
            # 仅当剪贴板仍是刚注入的内容时恢复，防止覆盖用户正在复制的新数据
            current_clip = get_clipboard_text()
            if current_clip == text:
                set_clipboard_text(old_clipboard)

        # 若此前用户已切换至其他窗口，且开启了归还焦点选项，则在粘贴完成后平滑归还焦点
        if (
            self.restore_switched_focus
            and switched_from_hwnd != 0
            and switched_from_hwnd != target_hwnd
            and is_window_valid(switched_from_hwnd)
        ):
            restore_foreground_window(switched_from_hwnd)

        return InjectionResult(True, InjectionOutcome.SUCCESS, "成功注入到目标窗口")

    def replace_pasted_text(
        self,
        original_text: str,
        corrected_text: str,
        target_hwnd: int,
        target_pid: int,
        activity_lease,
    ) -> InjectionResult:
        """Replace a recent paste only while the monitored target has stayed untouched."""
        # 1. 基础校验
        backspace_count = safe_backspace_count(original_text)
        if backspace_count is None:
            return InjectionResult(False, InjectionOutcome.UNSAFE_TEXT, "原文含有不支持安全计数的字符")
        if not corrected_text or safe_backspace_count(corrected_text) is None:
            return InjectionResult(False, InjectionOutcome.UNSAFE_TEXT, "校正文本含有不支持的字符")
        if not self.activity_monitor.is_valid(activity_lease):
            return InjectionResult(False, InjectionOutcome.ACTIVITY_CHANGED, "目标输入状态已变化")

        # 2. 焦点校验与自动重新激活目标窗口
        current_hwnd = get_foreground_window()
        current_pid = get_window_pid(current_hwnd)
        switched_from_hwnd = 0

        if current_hwnd != target_hwnd:
            if not is_window_valid(target_hwnd):
                return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, f"目标窗口已失效 (HWND: {target_hwnd})")

            # 暂存用户当前操作的新窗口，用于替换后归还焦点
            switched_from_hwnd = current_hwnd
            if not restore_foreground_window(target_hwnd):
                return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, f"无法恢复目标窗口焦点 (HWND: {target_hwnd})")

            # 刷新切回后的前台窗口及进程信息
            current_hwnd = get_foreground_window()
            current_pid = get_window_pid(current_hwnd)

        # 确认当前前台已成为目标窗口
        if current_hwnd != target_hwnd:
            return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, "目标窗口未能成为前台活动焦点")
        if target_pid is not None and target_pid != 0 and current_pid != target_pid:
            return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, f"目标进程已变更 (PID: {current_pid})")

        # 3. 准备写入剪贴板
        old_clipboard = get_clipboard_text() if self.restore_clipboard else None
        if not set_clipboard_text(corrected_text):
            if switched_from_hwnd != 0 and self.restore_switched_focus and is_window_valid(switched_from_hwnd):
                restore_foreground_window(switched_from_hwnd)
            return InjectionResult(False, InjectionOutcome.CLIPBOARD_ERROR, "写入校正文本到剪贴板失败")

        # 4. 执行原子替换：退格删除原文 + 粘贴新文本
        if not send_backspace_and_paste_keystrokes(backspace_count):
            if self.restore_clipboard and old_clipboard is not None and get_clipboard_text() == corrected_text:
                set_clipboard_text(old_clipboard)
            if switched_from_hwnd != 0 and self.restore_switched_focus and is_window_valid(switched_from_hwnd):
                restore_foreground_window(switched_from_hwnd)
            return InjectionResult(False, InjectionOutcome.SENDINPUT_ERROR, "替换按键发送不完整，原输入状态未知")

        # 5. 延迟恢复剪贴板
        if self.restore_clipboard and old_clipboard is not None:
            time.sleep(self.restore_delay)
            if get_clipboard_text() == corrected_text:
                set_clipboard_text(old_clipboard)

        # 6. 平滑归还焦点给用户当前操作的窗口
        if (
            self.restore_switched_focus
            and switched_from_hwnd != 0
            and switched_from_hwnd != target_hwnd
            and is_window_valid(switched_from_hwnd)
        ):
            restore_foreground_window(switched_from_hwnd)

        return InjectionResult(True, InjectionOutcome.SUCCESS, "已替换为语义校正文本")
