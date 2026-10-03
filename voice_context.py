"""Ephemeral, text-only context for the selected Claude CLI window."""

import ctypes
import ctypes.wintypes
import json
import os
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


def _lookup_session_name(client_pid: int, parent_map: Optional[Dict[int, int]] = None) -> str:
    """Best-effort lookup of Claude Code session name from ~/.claude/sessions/{pid}.json."""
    if not client_pid:
        return ""
    try:
        pids_to_check = [int(client_pid)]
        if parent_map and int(client_pid) in parent_map:
            pids_to_check.append(parent_map[int(client_pid)])
        sessions_dir = os.path.expanduser("~/.claude/sessions")
        for pid in pids_to_check:
            session_file = os.path.join(sessions_dir, f"{pid}.json")
            if os.path.isfile(session_file):
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        name = str(data.get("name") or "").strip()
                        if name:
                            return name
    except Exception:
        pass
    return ""


def _read_session_messages_from_jsonl(jsonl_path: str, max_turns: int = 25) -> List[Tuple[str, str]]:
    """Extract recent user/assistant dialogue turns directly from Claude Code session transaction jsonl."""
    if not jsonl_path or not os.path.isfile(jsonl_path):
        return []
    try:
        raw_msgs = []
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    etype = entry.get("type")
                    if etype in ("user", "assistant") and "message" in entry:
                        raw_msgs.append(entry["message"])
                except Exception:
                    pass
        return extract_conversation(raw_msgs)[-max_turns:]
    except Exception:
        return []


def _find_session_from_disk(target_title: str = "", target_pid: Optional[int] = None,
                            parent_map: Optional[Dict[int, int]] = None) -> Optional[dict]:
    """Directly resolve active Claude Code session from ~/.claude/ on disk without requiring HTTP traffic."""
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(sessions_dir):
        return None

    title_lower = str(target_title or "").strip().lower()
    candidates = []

    try:
        session_files = [f for f in os.listdir(sessions_dir) if f.endswith(".json")]
    except Exception:
        return None

    for fname in session_files:
        filepath = os.path.join(sessions_dir, fname)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                sdata = json.load(f)
            if not isinstance(sdata, dict):
                continue
            pid = int(sdata.get("pid") or 0)
            sid = str(sdata.get("sessionId") or "").strip()
            name = str(sdata.get("name") or "").strip()
            if not sid:
                continue

            # Verify process is still alive on Windows
            if pid and hasattr(ctypes, "windll"):
                h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
                if not h:
                    continue
                ctypes.windll.kernel32.CloseHandle(h)

            ai_title = ""
            jsonl_path = ""
            # Locate the session jsonl in projects directory
            if os.path.isdir(projects_dir):
                for root, _, files in os.walk(projects_dir):
                    if f"{sid}.jsonl" in files:
                        jsonl_path = os.path.join(root, f"{sid}.jsonl")
                        try:
                            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as jf:
                                for line in reversed(jf.readlines()[-60:]):
                                    if '"ai-title"' in line or '"aiTitle"' in line:
                                        d = json.loads(line)
                                        ai_title = str(d.get("aiTitle") or "").strip()
                                        if ai_title:
                                            break
                        except Exception:
                            pass
                        break

            candidates.append({
                "pid": pid,
                "session_id": sid,
                "session_name": name,
                "ai_title": ai_title,
                "jsonl_path": jsonl_path,
                "updated_at": float(sdata.get("updatedAt") or 0) / 1000.0,
            })
        except Exception:
            pass

    if not candidates:
        return None

    # 1. Title match (ai_title, session_name, or session_id in window title)
    if title_lower:
        title_matches = []
        for c in candidates:
            ai_t = c["ai_title"].lower()
            s_n = c["session_name"].lower()
            s_id = c["session_id"].lower()
            if ai_t and ai_t in title_lower:
                title_matches.append((len(ai_t) + 100, c))
            elif s_n and s_n in title_lower:
                title_matches.append((len(s_n), c))
            elif s_id and s_id in title_lower:
                title_matches.append((len(s_id), c))
        if title_matches:
            title_matches.sort(key=lambda item: item[0], reverse=True)
            return title_matches[0][1]

    # 2. Process relationship match
    if target_pid and parent_map:
        proc_matches = [
            c for c in candidates
            if c["pid"] and _is_process_related(c["pid"], int(target_pid), parent_map)
        ]
        if len(proc_matches) == 1:
            return proc_matches[0]
        elif len(proc_matches) > 1:
            return max(proc_matches, key=lambda c: c["updated_at"])

    return None


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

        # 从最新轮次倒序收集纯净问答块，最新一轮的助手回复与用户输入拥有最高优先级
        dialogue_blocks = []
        chars_used = 0

        for role, text in reversed(msg_list):
            clean_text = text.strip()
            if not clean_text:
                continue
            label = "用户" if role == "user" else "助手"
            block = f"{label}: {clean_text}"
            block_len = len(block) + 2  # account for \n\n

            if chars_used + block_len <= limit:
                dialogue_blocks.append(block)
                chars_used += block_len
            else:
                remaining = limit - chars_used
                if remaining >= 200:
                    # 较早的历史轮次放不下时，按段落/换行边界整块截断，坚决杜绝把词语或句子切断
                    clipped = block[-remaining:]
                    nl = clipped.find("\n\n")
                    if nl != -1 and nl < remaining // 2:
                        clipped = clipped[nl + 2:].strip()
                    else:
                        nl_single = clipped.find("\n")
                        if nl_single != -1 and nl_single < remaining // 2:
                            clipped = clipped[nl_single + 1:].strip()
                    if clipped:
                        dialogue_blocks.append(clipped)
                break

        dialogue_blocks.reverse()
        return "\n\n".join(dialogue_blocks).strip()

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
        session_name = _lookup_session_name(int(client_pid))
        with self._lock:
            self._revision += 1
            revision = self._revision
            self._pending[key] = {
                "session_id": session_id,
                "session_name": session_name,
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

            def _find_by_title(pool):
                title_lower = str(target_title or "").strip().lower()
                if not title_lower:
                    return None
                matched = []
                for e in pool:
                    s_name = str(e.get("session_name") or "").strip().lower()
                    s_id = str(e.get("session_id") or "").strip().lower()
                    if s_name and s_name in title_lower:
                        matched.append((len(s_name), e))
                    elif s_id and s_id in title_lower:
                        matched.append((len(s_id), e))
                if not matched:
                    return None
                matched.sort(key=lambda item: item[0], reverse=True)
                return matched[0][1]

            chosen = None
            if len(matches) == 1:
                chosen = next(iter(matches.values()))
            elif len(matches) > 1:
                # Disambiguate multiple sessions under the same terminal (e.g. Windows Terminal tabs)
                title_match = _find_by_title(matches.values())
                if title_match is not None:
                    chosen = title_match
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
                    title_match = _find_by_title(all_entries.values())
                    if title_match is not None:
                        chosen = title_match
                    else:
                        # 全局 MRU (Most Recently Used) 兜底
                        chosen = max(all_entries.values(), key=lambda e: e.get("updated_at", 0))

            # 本地磁盘会话直读检查：
            # 若内存中没有选出有效会话，或者当前窗口标题与内存选取结果不匹配（跨窗口/分屏），
            # 直接从 ~/.claude/ 本地磁盘文件 0ms 直读该窗口的会话 ID 与完整问答！
            need_disk_lookup = False
            if chosen is None:
                need_disk_lookup = True
            elif target_title:
                title_lower = str(target_title or "").strip().lower()
                chosen_sname = str(chosen.get("session_name") or "").strip().lower()
                chosen_sid = str(chosen.get("session_id") or "").strip().lower()
                has_match = (
                    (chosen_sname and chosen_sname in title_lower)
                    or (chosen_sid and chosen_sid in title_lower)
                )
                if not has_match:
                    need_disk_lookup = True

            if need_disk_lookup:
                disk_session = _find_session_from_disk(target_title, target_pid, parent_map)
                if disk_session:
                    disk_msgs = _read_session_messages_from_jsonl(disk_session.get("jsonl_path") or "")
                    if disk_msgs:
                        context = self._render(disk_msgs, max_chars=max_chars)
                        return VoiceContextSnapshot(
                            session_id=disk_session["session_id"],
                            model="relay-main",
                            context=context,
                            revision=1,
                        )

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
