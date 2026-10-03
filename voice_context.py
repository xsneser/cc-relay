"""Ephemeral, text-only context for the selected Claude CLI window."""

import ctypes
import ctypes.wintypes
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_CONTEXT_CHARS = 2500
DEFAULT_SESSION_TTL = 30 * 60
TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2
AF_INET6 = 23
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

TERM_BACKTICK_RE = re.compile(r"`([^`\n\r]{1,60})`")
TERM_IDENTIFIER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{2,40}\b")
TERM_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")

COMMON_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "have", "will", "your",
    "can", "not", "are", "but", "all", "any", "user", "assistant", "true", "false",
    "none", "null", "self", "text", "line", "code", "file", "error", "name",
    "type", "return", "import", "class", "def", "if", "else", "elif", "in", "or",
}


def extract_technical_terms(messages: Iterable[Tuple[str, str]], max_terms: int = 30) -> List[str]:
    """Extract code identifiers, backticked symbols, paths, and technical terms from recent dialogue."""
    seen = set()
    terms = []
    msg_list = list(messages)
    for _, text in reversed(msg_list):
        if not text:
            continue
        # 1. Backticked terms
        for m in TERM_BACKTICK_RE.finditer(text):
            val = m.group(1).strip()
            if val and len(val) >= 2 and val.lower() not in COMMON_STOPWORDS and val not in seen:
                seen.add(val)
                terms.append(val)
                if len(terms) >= max_terms:
                    return terms
        # 2. File paths
        for m in TERM_PATH_RE.finditer(text):
            val = m.group(0).strip()
            if val and val not in seen:
                seen.add(val)
                terms.append(val)
                if len(terms) >= max_terms:
                    return terms
        # 3. CamelCase or snake_case identifiers
        for m in TERM_IDENTIFIER_RE.finditer(text):
            val = m.group(0).strip()
            is_snake = "_" in val
            is_camel = any(c.isupper() for c in val[1:])
            is_all_caps = val.isupper() and len(val) >= 2
            if (is_snake or is_camel or is_all_caps) and val.lower() not in COMMON_STOPWORDS and val not in seen:
                seen.add(val)
                terms.append(val)
                if len(terms) >= max_terms:
                    return terms
    return terms


@dataclass(frozen=True)
class VoiceContextSnapshot:
    session_id: str
    model: str
    context: str
    revision: int


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts).strip()


def extract_conversation(messages: Sequence[dict]) -> List[Tuple[str, str]]:
    """Keep only top-level user/assistant text; never inspect nested tool content."""
    extracted = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _text_from_content(message.get("content"))
        if text:
            extracted.append((role, text))
    return extracted


def extract_response_text(response_body: bytes) -> str:
    """Extract completed assistant text from JSON or Anthropic SSE response bytes."""
    if not response_body:
        return ""
    raw = response_body.decode("utf-8", "replace")
    try:
        payload = __import__("json").loads(raw)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        if payload.get("type") == "error" or payload.get("stop_reason") not in ("end_turn", "stop_sequence"):
            return ""
        return _text_from_content(payload.get("content"))

    blocks = {}
    complete = False
    stop_reason = None
    try:
        import json
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                complete = True
                continue
            try:
                event = json.loads(data)
            except Exception:
                continue
            kind = event.get("type")
            if kind == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
            elif kind == "message_stop":
                complete = True
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                index = event.get("index")
                if block.get("type") == "text" and isinstance(index, int):
                    blocks[index] = str(block.get("text") or "")
            elif kind == "content_block_delta":
                index = event.get("index")
                delta = event.get("delta") or {}
                if isinstance(index, int) and delta.get("type") == "text_delta" and index in blocks:
                    blocks[index] += str(delta.get("text") or "")
    except Exception:
        return ""
    if not complete or stop_reason not in ("end_turn", "stop_sequence"):
        return ""
    return "\n".join(blocks[index].strip() for index in sorted(blocks) if blocks[index].strip())


def _ipv4(value: str) -> Optional[str]:
    try:
        return socket.inet_aton(value)
    except OSError:
        return None


def _tcp_owner_pid_v4(peer: tuple, local: tuple) -> Optional[int]:
    if not hasattr(ctypes, "windll"):
        return None

    class Row(ctypes.Structure):
        _fields_ = [
            ("state", ctypes.wintypes.DWORD),
            ("local_addr", ctypes.wintypes.DWORD),
            ("local_port", ctypes.wintypes.DWORD),
            ("remote_addr", ctypes.wintypes.DWORD),
            ("remote_port", ctypes.wintypes.DWORD),
            ("pid", ctypes.wintypes.DWORD),
        ]

    iphlpapi = ctypes.windll.iphlpapi
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.ULONG),
        ctypes.wintypes.BOOL,
        ctypes.wintypes.ULONG,
        ctypes.c_int,
        ctypes.wintypes.ULONG,
    ]
    get_table.restype = ctypes.wintypes.DWORD
    size = ctypes.wintypes.ULONG(0)
    result = get_table(None, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if result not in (0, 122) or not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    result = get_table(buffer, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if result != 0:
        return None

    peer_ip = _ipv4(str(peer[0]))
    local_ip = _ipv4(str(local[0]))
    if not peer_ip or not local_ip:
        return None
    peer_addr = int.from_bytes(peer_ip, "little")
    local_addr = int.from_bytes(local_ip, "little")
    peer_port = int(peer[1])
    local_port = int(local[1])
    count = ctypes.cast(buffer, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value
    base = ctypes.addressof(buffer) + ctypes.sizeof(ctypes.wintypes.DWORD)
    row_size = ctypes.sizeof(Row)
    for index in range(count):
        row = Row.from_address(base + index * row_size)
        row_local_port = socket.ntohs(row.local_port & 0xFFFF)
        row_remote_port = socket.ntohs(row.remote_port & 0xFFFF)
        if (row.local_addr == peer_addr and row.remote_addr == local_addr
                and row_local_port == peer_port and row_remote_port == local_port):
            return int(row.pid) or None
    return None


def tcp_client_pid(connection, peer: tuple) -> Optional[int]:
    """Resolve the process that owns the accepted socket's reverse TCP tuple."""
    try:
        local = connection.getsockname()
        if len(peer) >= 2 and len(local) >= 2 and ":" not in str(peer[0]) and ":" not in str(local[0]):
            return _tcp_owner_pid_v4(peer, local)
    except Exception:
        pass
    return None


def window_process_id(hwnd: int) -> Optional[int]:
    if not hwnd or not hasattr(ctypes, "windll"):
        return None
    try:
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def is_valid_window_target(hwnd: int, pid: int) -> bool:
    """验证目标窗口 HWND 是否仍有效且归属于指定进程 PID (不受焦点是否临时切走影响)"""
    if not hwnd or not pid or not hasattr(ctypes, "windll"):
        return False
    try:
        user32 = ctypes.windll.user32
        if hasattr(user32, "IsWindow") and not user32.IsWindow(hwnd):
            return False
        owner_pid = window_process_id(hwnd)
        return bool(owner_pid and owner_pid == int(pid))
    except Exception:
        return False


def is_foreground_target(hwnd: int, pid: int) -> bool:
    if not hwnd or not pid or not hasattr(ctypes, "windll"):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        return int(user32.GetForegroundWindow() or 0) == int(hwnd) and window_process_id(hwnd) == int(pid)
    except Exception:
        return False


def process_creation_time(pid: int) -> Optional[int]:
    if not pid or not hasattr(ctypes, "windll"):
        return None

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD), ("dwHighDateTime", ctypes.wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    creation, exit_time, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    try:
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = ctypes.wintypes.BOOL
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        ):
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def process_parent_map() -> Dict[int, int]:
    """Return a best-effort process -> parent PID map on Windows."""
    if not hasattr(ctypes, "windll"):
        return {}

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {}
    result: Dict[int, int] = {}
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _is_descendant_or_same(pid: int, ancestor_pid: int, parent_map: Dict[int, int]) -> bool:
    seen = set()
    current = int(pid or 0)
    ancestor_pid = int(ancestor_pid or 0)
    while current and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        current = parent_map.get(current, 0)
    return False


def _is_process_related(client_pid: int, target_pid: int, parent_map: Dict[int, int]) -> bool:
    """双向判定进程关联：检查 client_pid 是否为 target_pid 的后代，或 target_pid 是否为 client_pid 的后代"""
    if not client_pid or not target_pid:
        return False
    if client_pid == target_pid:
        return True
    return (
        _is_descendant_or_same(client_pid, target_pid, parent_map)
        or _is_descendant_or_same(target_pid, client_pid, parent_map)
    )


class VoiceContextRegistry:
    """Bounded in-memory map from CLI session/client process to plain-text history."""

    def __init__(self, max_sessions: int = 64, ttl_seconds: int = DEFAULT_SESSION_TTL,
                 max_chars: int = DEFAULT_CONTEXT_CHARS):
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_chars = max(256, int(max_chars))
        self._lock = threading.RLock()
        self._entries = {}
        self._pending = {}
        self._revision = 0

    def _render(self, messages: Iterable[Tuple[str, str]], max_chars: Optional[int] = None) -> str:
        msg_list = list(messages)
        if not msg_list:
            return ""
        limit = self.max_chars if max_chars is None else max(256, min(int(max_chars), self.max_chars))

        terms = extract_technical_terms(msg_list, max_terms=25)
        terms_header = ""
        if terms:
            terms_header = "参考标识符/术语: " + ", ".join(terms) + "\n\n"

        lines = []
        for role, text in msg_list:
            label = "用户" if role == "user" else "助手"
            clean_text = " ".join(text.split())
            lines.append(f"{label}: {clean_text}")
        joined_dialogue = "\n".join(lines)

        remaining_limit = limit - len(terms_header)
        if remaining_limit <= 64:
            return (terms_header + joined_dialogue)[-limit:]

        if len(joined_dialogue) > remaining_limit:
            clipped = joined_dialogue[-remaining_limit:]
            newline_idx = clipped.find("\n")
            if newline_idx != -1 and newline_idx < remaining_limit // 2:
                clipped = clipped[newline_idx + 1:]
            dialogue_part = clipped
        else:
            dialogue_part = joined_dialogue

        result = (terms_header + dialogue_part).strip()
        return result[-limit:]

    def observe_request(self, session_id: str, client_pid: Optional[int], model: str,
                        messages: Sequence[dict], is_main_session: bool = True,
                        now: Optional[float] = None) -> Optional[Tuple[str, int, int]]:
        session_id = str(session_id or "").strip()
        if not session_id or not client_pid or not is_main_session:
            return None
        created = time.monotonic() if now is None else float(now)
        key = (session_id, int(client_pid))
        process_start = process_creation_time(int(client_pid))
        conversation = extract_conversation(messages)
        if not conversation:
            return None
        with self._lock:
            self._revision += 1
            revision = self._revision
            self._pending[key] = {
                "session_id": session_id,
                "client_pid": int(client_pid),
                "process_start": process_start,
                "model": str(model or ""),
                "messages": conversation,
                "updated_at": created,
                "revision": revision,
            }
            self._prune_locked(created)
            return key[0], key[1], revision

    def observe_response(self, token: Optional[Tuple[str, int, int]], response_body: bytes,
                         now: Optional[float] = None) -> None:
        if not token:
            return
        text = extract_response_text(response_body)
        if not text:
            return
        updated = time.monotonic() if now is None else float(now)
        session_id, client_pid, revision = token
        key = (session_id, client_pid)
        with self._lock:
            pending = self._pending.get(key)
            if not pending or pending["revision"] != revision:
                return
            entry = dict(pending)
            entry["messages"] = (list(pending["messages"]) + [("assistant", text)])[-40:]
            entry["updated_at"] = updated
            self._entries[key] = entry
            self._pending.pop(key, None)

    def _prune_locked(self, now: float) -> None:
        for cache in (self._entries, self._pending):
            expired = [key for key, value in cache.items()
                       if now - value["updated_at"] > self.ttl_seconds]
            for key in expired:
                cache.pop(key, None)
            if len(cache) > self.max_sessions:
                oldest = sorted(cache.items(), key=lambda item: item[1]["updated_at"])
                for key, _ in oldest[:len(cache) - self.max_sessions]:
                    cache.pop(key, None)

    def snapshot_for_target(self, target_pid: int, target_title: str = "",
                            parent_map: Optional[Dict[int, int]] = None,
                            now: Optional[float] = None, max_chars: Optional[int] = None) -> Optional[VoiceContextSnapshot]:
        if isinstance(target_title, dict) and parent_map is None:
            parent_map = target_title
            target_title = ""
        parent_map = process_parent_map() if parent_map is None else parent_map
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune_locked(current)
            matches = {}
            process_starts = {}
            for cache in (self._entries, self._pending):
                for key, entry in cache.items():
                    client_pid = entry["client_pid"]
                    if target_pid and not _is_process_related(client_pid, int(target_pid), parent_map):
                        continue
                    if client_pid not in process_starts:
                        process_starts[client_pid] = process_creation_time(client_pid)
                    expected_start = entry.get("process_start")
                    if expected_start is not None and process_starts[client_pid] != expected_start:
                        continue
                    candidate_entry = self._entries.get(key) or self._pending.get(key)
                    if candidate_entry and key not in matches:
                        matches[key] = candidate_entry

            chosen = None
            if len(matches) == 1:
                chosen = next(iter(matches.values()))
            elif len(matches) > 1:
                # Disambiguate multiple sessions under the same terminal (e.g. Windows Terminal tabs)
                title_lower = str(target_title or "").strip().lower()
                exact_title = [e for e in matches.values() if e["session_id"].lower() in title_lower] if title_lower else []
                if len(exact_title) == 1:
                    chosen = exact_title[0]
                else:
                    # Fallback to the most recently active session under this window
                    chosen = max(matches.values(), key=lambda e: e.get("updated_at", 0))

            # 全局兜底：即便进程树因多进程应用/提权分离而断开，只要 Relay 存在活跃会话依然平滑复用
            all_entries = {**self._pending, **self._entries}
            if chosen is None and all_entries:
                if len(all_entries) == 1:
                    # 单机全局仅 1 个活跃会话时无条件自动关联
                    chosen = next(iter(all_entries.values()))
                else:
                    title_lower = str(target_title or "").strip().lower()
                    exact_title = [e for e in all_entries.values() if e["session_id"].lower() in title_lower] if title_lower else []
                    if len(exact_title) == 1:
                        chosen = exact_title[0]
                    else:
                        # 全局 MRU (Most Recently Used) 兜底
                        chosen = max(all_entries.values(), key=lambda e: e.get("updated_at", 0))

            if chosen is not None:
                context = self._render(chosen["messages"], max_chars=max_chars)
                return VoiceContextSnapshot(
                    session_id=chosen["session_id"],
                    model=chosen["model"],
                    context=context,
                    revision=chosen["revision"],
                )

            return None


VOICE_CONTEXTS = VoiceContextRegistry()
