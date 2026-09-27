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
import time
from typing import NamedTuple, Optional, Tuple

# Win32 常量
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

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


def send_paste_keystrokes() -> bool:
    """通过 Win32 SendInput 模拟按下并释放 Ctrl+V"""
    inputs = (INPUT * 4)()

    # 1. Ctrl Down
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = VK_CONTROL
    inputs[0].union.ki.wScan = 0
    inputs[0].union.ki.dwFlags = 0
    inputs[0].union.ki.time = 0
    inputs[0].union.ki.dwExtraInfo = 0

    # 2. V Down
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = VK_V
    inputs[1].union.ki.wScan = 0
    inputs[1].union.ki.dwFlags = 0
    inputs[1].union.ki.time = 0
    inputs[1].union.ki.dwExtraInfo = 0

    # 3. V Up
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].union.ki.wVk = VK_V
    inputs[2].union.ki.wScan = 0
    inputs[2].union.ki.dwFlags = KEYEVENTF_KEYUP
    inputs[2].union.ki.time = 0
    inputs[2].union.ki.dwExtraInfo = 0

    # 4. Ctrl Up
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].union.ki.wVk = VK_CONTROL
    inputs[3].union.ki.wScan = 0
    inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP
    inputs[3].union.ki.time = 0
    inputs[3].union.ki.dwExtraInfo = 0

    sent = user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return sent == 4


class InjectionOutcome(enum.Enum):
    SUCCESS = "success"
    EMPTY_TEXT = "empty_text"
    FOCUS_CHANGED = "focus_changed"
    TARGET_CLOSED = "target_closed"
    CLIPBOARD_ERROR = "clipboard_error"
    SENDINPUT_ERROR = "sendinput_error"


class InjectionResult:
    def __init__(self, success: bool, outcome: InjectionOutcome, message: str = ""):
        self.success = success
        self.outcome = outcome
        self.message = message

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        return f"<InjectionResult success={self.success} outcome={self.outcome.value} msg='{self.message}'>"


class WindowsInjector:
    def __init__(self, restore_clipboard: bool = True, restore_delay: float = 0.15):
        self.restore_clipboard = restore_clipboard
        self.restore_delay = restore_delay

    def inject_text(
        self,
        text: str,
        target_hwnd: Optional[int] = None,
        target_pid: Optional[int] = None,
    ) -> InjectionResult:
        """安全注入文本到前台目标窗口

        1. 严格校验目标窗口 HWND 与 PID 是否仍处于前台活动焦点
        2. 写入 Unicode 剪贴板并模拟 Ctrl+V
        3. 延迟恢复用户先前剪贴板内容
        """
        if not text:
            return InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "待注入文本为空")

        # 焦点校验
        current_hwnd = get_foreground_window()
        current_pid = get_window_pid(current_hwnd)

        if target_hwnd is not None and target_hwnd != 0:
            if current_hwnd != target_hwnd:
                msg = f"窗口焦点已切换: 目标={target_hwnd} 当前={current_hwnd} ({get_window_title(current_hwnd)})"
                return InjectionResult(False, InjectionOutcome.FOCUS_CHANGED, msg)

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

        return InjectionResult(True, InjectionOutcome.SUCCESS, "成功注入到目标窗口")
