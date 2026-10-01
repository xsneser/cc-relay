#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CC 统一中转 (unified relay)
- Claude Code 用假 sk 连本服务; 本服务持真实 key, 按 model 名路由到多上游:
    deepseek     -> https://api.deepseek.com/anthropic (直连)
    codex        -> CLIProxyAPI 127.0.0.1:8317 (Codex 额度, 需 clash; 随需自动拉起)
    antigravity  -> Antigravity Tools 127.0.0.1:8045 (Gemini, 随需自动拉起)
- 完整 dump 每次请求 (headers/body/路由决策/响应) 供分析
- 纯 stdlib

用法:
    python cc_relay.py serve
    python cc_relay.py stats | last [n] | dump <idx>
    python cc_relay.py startproxy | stopproxy     # 手动管理 codex 上游
"""
import os, sys, json, time, threading, argparse, subprocess, socket, select, re, shutil
import urllib.request, urllib.error
from urllib.parse import urlsplit
from cpa_updater import CPAUpdater
import relay_updater
import antigravity_updater
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.environ.get("CC_RELAY_DIR") or os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "config.json")
RECORDS = os.path.join(BASE, "records.jsonl")
LOCK = threading.Lock()
_IDX = [0]
_UP = {"last": "", "last_model": "", "last_ts": 0}
_PROCESS_INSTANCE_ID = f"{os.getpid()}-{time.time_ns()}"
_CPA_UPDATER = None
_CPA_UPDATER_LOCK = threading.Lock()
_CODEX_MAINTENANCE = threading.Condition()
_CODEX_ACTIVE_REQUESTS = 0
_CODEX_MAINTENANCE_ACTIVE = False
_CODEX_LAST_REQUEST_TS = 0.0


def _get_cpa_updater(conf=None):
    global _CPA_UPDATER
    conf = conf or load_conf()
    with _CPA_UPDATER_LOCK:
        if _CPA_UPDATER is None:
            exe = os.path.join(BASE, "codex-proxy", "cli-proxy-api.exe")
            cfg = conf.get("codex_config") or os.path.join(BASE, "codex-proxy", "config.yaml")
            _CPA_UPDATER = CPAUpdater(BASE, exe, cfg)
        return _CPA_UPDATER


def _get_relay_updater(conf=None):
    return relay_updater.get_relay_updater(conf or load_conf())


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        conf = json.load(f)
    if _repair_misplaced_upstream_key(conf):
        _save_conf(conf)
    return conf


CONF_LOCK = threading.Lock()
_CODEX_LOGIN = None
_CODEX_LOGIN_LOCK = threading.Lock()


def get_codex_login():
    global _CODEX_LOGIN
    with _CODEX_LOGIN_LOCK:
        if _CODEX_LOGIN is None:
            from codex_login import CodexLogin
            _CODEX_LOGIN = CodexLogin(BASE, load_conf, _save_conf, CONF_LOCK)
        return _CODEX_LOGIN


def _save_conf(conf):
    """原子写回配置: 先写 .tmp 再 replace, 免并发丢更新 / 读到半份 JSON"""
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF)


def traffic_paused(conf):
    """返回流量闸门状态: True 表示用户已手动暂停所有上游流量."""
    return (conf or {}).get("traffic_paused", False) is True


_TRAFFIC_UNPAUSED_EVENT = threading.Event()
_PAUSED_WAITING_COUNT = 0
_PAUSED_WAITING_LOCK = threading.Lock()


def _paused_waiting_inc():
    global _PAUSED_WAITING_COUNT
    with _PAUSED_WAITING_LOCK:
        _PAUSED_WAITING_COUNT += 1


def _paused_waiting_dec():
    global _PAUSED_WAITING_COUNT
    with _PAUSED_WAITING_LOCK:
        if _PAUSED_WAITING_COUNT > 0:
            _PAUSED_WAITING_COUNT -= 1


def _paused_waiting_count():
    with _PAUSED_WAITING_LOCK:
        return _PAUSED_WAITING_COUNT


def _sync_traffic_unpaused_event(conf=None):
    if conf is None:
        try:
            conf = load_conf()
        except Exception:
            conf = None
    if traffic_paused(conf):
        _TRAFFIC_UNPAUSED_EVENT.clear()
    else:
        _TRAFFIC_UNPAUSED_EVENT.set()


_sync_traffic_unpaused_event()


_RESTART_LOCK = threading.Lock()
_RESTART_QUEUED = False

# Only an Antigravity process launched by this relay may be stopped from the UI.
_ANTIGRAVITY_PROCESS_LOCK = threading.Lock()
_ANTIGRAVITY_PROCESS = None
_ANTIGRAVITY_EXE = ""


def _powershell_quote(arg):
    """Safely quote a string for PowerShell single-quoted string literal."""
    return "'" + str(arg).replace("'", "''") + "'"


def _restart_launch_spec():
    """Determine (executable, args_list, working_dir) for restarting."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, [], os.path.dirname(os.path.abspath(exe))

    exe = sys.executable
    script = os.path.abspath(sys.argv[0])
    base_name = os.path.basename(script).lower()

    if base_name == "main_launcher.py":
        return exe, [script], os.path.dirname(script)
    elif base_name == "cc_relay.py":
        return exe, [script, "serve"], BASE
    else:
        launcher = os.path.join(BASE, "main_launcher.py")
        if os.path.isfile(launcher):
            return exe, [launcher], BASE
        return exe, [os.path.join(BASE, "cc_relay.py"), "serve"], BASE


def _build_restart_supervisor_command(pid, launch_spec):
    """Build the PowerShell command that waits for old PID to exit then launches the new process."""
    exe, args_list, cwd = launch_spec
    exe_q = _powershell_quote(exe)
    cwd_q = _powershell_quote(cwd)
    if args_list:
        args_q = ", ".join(_powershell_quote(a) for a in args_list)
        arg_part = f"-ArgumentList @({args_q})"
    else:
        arg_part = ""

    return (
        f"Wait-Process -Id {pid} -Timeout 10 -ErrorAction SilentlyContinue; "
        f"Start-Sleep -Milliseconds 500; "
        f"Start-Process -WindowStyle Hidden -FilePath {exe_q} {arg_part} -WorkingDirectory {cwd_q}"
    )


def _spawn_restart_supervisor(pid=None):
    """Launch the detached PowerShell supervisor process. Returns True on success."""
    if pid is None:
        pid = os.getpid()

    launch_spec = _restart_launch_spec()
    ps_cmd = _build_restart_supervisor_command(pid, launch_spec)

    ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not os.path.isfile(ps_exe):
        ps_exe = "powershell.exe"

    detached_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    )

    env = dict(os.environ)
    env["CC_RELAY_NO_BROWSER"] = "1"

    try:
        subprocess.Popen(
            [ps_exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            creationflags=detached_flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        return True
    except Exception as e:
        print(f"[ERROR] Failed to spawn restart supervisor: {e}", file=sys.stderr)
        return False


def _restart_worker():
    global _RESTART_QUEUED
    time.sleep(0.4)
    if _spawn_restart_supervisor():
        os._exit(0)
    else:
        with _RESTART_LOCK:
            _RESTART_QUEUED = False


def schedule_restart():
    """Atomically schedule a background restart if not already queued. Returns True if scheduled."""
    global _RESTART_QUEUED
    with _RESTART_LOCK:
        if _RESTART_QUEUED:
            return True
        _RESTART_QUEUED = True
        threading.Thread(target=_restart_worker, daemon=True).start()
        return True


UPSTREAM_ALLOWED_KEYS = {
    "deepseek": {"real_deepseek_key", "deepseek_key"},
    "codex": {"codex_proxy_key", "codex_key"},
    "antigravity": {"antigravity_key", "gemini_key"},
    "gemini": {"antigravity_key", "gemini_key"},
}

UPSTREAM_CANONICAL_KEYS = {
    "deepseek": "real_deepseek_key",
    "codex": "codex_proxy_key",
    "antigravity": "antigravity_key",
    "gemini": "antigravity_key",
}


def is_loopback_host(host):
    """Only allow loopback addresses for local security."""
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "ip6-localhost")


def _canonical_key_field(upstream_name):
    return UPSTREAM_CANONICAL_KEYS.get(str(upstream_name or "").strip().lower(), "")


def _safe_key_env_name(upstream_name, value):
    """Return an allowed config field name for the given upstream, or empty string."""
    val = str(value or "").strip()
    allowed = UPSTREAM_ALLOWED_KEYS.get(str(upstream_name or "").strip().lower(), set())
    return val if val in allowed else ""


def _repair_misplaced_upstream_key(conf):
    """Recover a key pasted into upstream.key_env by an older/broken UI.

    key_env is metadata naming a top-level config field, never a place to keep
    the credential itself. If key_env is not an allowed field name, migrate
    the misplaced secret into the canonical provider key field (if not already set)
    and reset key_env to the canonical field name.
    """
    changed = False
    for name, upstream in (conf.get("upstreams") or {}).items():
        if not isinstance(upstream, dict):
            continue
        key_env = str(upstream.get("key_env") or "").strip()
        target = _canonical_key_field(name)
        if not target:
            continue
        safe_field = _safe_key_env_name(name, key_env)
        if safe_field:
            continue
        if key_env and not str(conf.get(target) or "").strip():
            if key_env not in ("fake_api_key", target):
                conf[target] = key_env
        upstream["key_env"] = target
        changed = True
    return changed


_CONFIG_KEY_MASK = "••••••••"


def _mask_config_key(value):
    """只向 UI 暴露固定掩码, 不泄露 key 长度或内容。"""
    return _CONFIG_KEY_MASK if value else ""


def _normalize_config_url(value, field, allow_empty=False, allow_direct=False):
    """校验并规范化配置页提交的 URL。"""
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{field} cannot be empty")
    if allow_direct and value.lower() == "direct":
        return "direct"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} contains control characters")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError(f"{field} is invalid")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL")
    if not parsed.hostname:
        raise ValueError(f"{field} must include a host")
    return value.rstrip("/")


def _config_public_view(conf):
    """返回配置页所需的非敏感配置快照。"""
    upstreams = {}
    for name, upstream in (conf.get("upstreams") or {}).items():
        if not isinstance(upstream, dict):
            continue
        key_env = _safe_key_env_name(name, upstream.get("key_env")) or _canonical_key_field(name)
        key = conf.get(key_env, "") if key_env else ""
        upstreams[str(name)] = {
            "base": str(upstream.get("base") or ""),
            "proxy_url": str(upstream.get("proxy_url") or ""),
            "key_env": key_env,
            "key_mask": _mask_config_key(key),
            "has_key": bool(key),
        }
    tools_conf = conf.get("tools") or {}
    codex_view = {
        "auto_update": ((tools_conf.get("codex") or {}).get("auto_update") is True),
        "outbound_proxy": _read_codex_outbound_proxy(conf),
    }
    v_conf = tools_conf.get("voice") or {}
    voice_view = {
        "auto_start": bool(v_conf.get("auto_start", False)),
        "hotkey": str(v_conf.get("hotkey") or "mouse_x1"),
        "engine": str(v_conf.get("engine") or "sensevoice_offline"),
        "port": int(v_conf.get("port") or 8401),
        "restore_clipboard": bool(v_conf.get("restore_clipboard", True)),
    }
    return {
        "upstreams": upstreams,
        "tools": {
            "codex": codex_view,
            "antigravity": {
                "auto_start": bool(((conf.get("tools") or {}).get("antigravity") or {}).get("auto_start", False)),
            },
            "voice": voice_view,
        },
    }


def _apply_config_update(conf, data):
    """校验并合并配置页更新, 返回新的非敏感快照。"""
    if not isinstance(data, dict):
        raise ValueError("request body must be an object")
    candidate = json.loads(json.dumps(conf, ensure_ascii=False))

    updates = data.get("upstreams")
    if updates is not None:
        if not isinstance(updates, dict):
            raise ValueError("upstreams must be an object")
        configured = candidate.get("upstreams") or {}
        unknown = set(updates) - set(configured)
        if unknown:
            raise ValueError("unknown upstream: " + ", ".join(sorted(map(str, unknown))))
        for name, patch in updates.items():
            if not isinstance(patch, dict):
                raise ValueError(f"upstreams.{name} must be an object")
            upstream = configured[name]
            if not isinstance(upstream, dict):
                raise ValueError(f"upstreams.{name} is invalid")
            if "base" in patch:
                upstream["base"] = _normalize_config_url(
                    patch["base"], f"upstreams.{name}.base")
            if "proxy_url" in patch:
                upstream["proxy_url"] = _normalize_config_url(
                    patch["proxy_url"], f"upstreams.{name}.proxy_url",
                    allow_empty=True, allow_direct=True)
            key_env = _safe_key_env_name(name, upstream.get("key_env")) or _canonical_key_field(name)
            if not key_env:
                raise ValueError(f"upstreams.{name} has an invalid key_env")
            upstream["key_env"] = key_env
            if "key" in patch:
                key = patch["key"]
                if not isinstance(key, str):
                    raise ValueError(f"upstreams.{name}.key must be a string")
                if key.strip() and key.strip() != _CONFIG_KEY_MASK:
                    candidate[key_env] = key.strip()

    tools_update = data.get("tools")
    if tools_update is not None:
        if not isinstance(tools_update, dict):
            raise ValueError("tools must be an object")
        tools = candidate.setdefault("tools", {})
        if "codex" in tools_update:
            c_patch = tools_update["codex"]
            if not isinstance(c_patch, dict):
                raise ValueError("tools.codex must be an object")
            if "auto_update" in c_patch:
                if not isinstance(c_patch["auto_update"], bool):
                    raise ValueError("tools.codex.auto_update must be a boolean")
                tools.setdefault("codex", {})["auto_update"] = c_patch["auto_update"]
            if "outbound_proxy" in c_patch:
                raw_proxy = str(c_patch["outbound_proxy"]).strip()
                if raw_proxy and raw_proxy.lower() not in ("direct", "none"):
                    if not (raw_proxy.startswith("http://") or raw_proxy.startswith("https://") or raw_proxy.startswith("socks5://")):
                        raise ValueError("Codex 外部代理必须以 http://、https:// 或 socks5:// 开头，或填 direct/留空")
                _write_codex_outbound_proxy(candidate, raw_proxy)
        if "antigravity" in tools_update:
            ag = tools.setdefault("antigravity", {})
            if "auto_start" in tools_update["antigravity"]:
                ag["auto_start"] = bool(tools_update["antigravity"]["auto_start"])
        if "voice" in tools_update:
            v_patch = tools_update["voice"]
            if not isinstance(v_patch, dict):
                raise ValueError("tools.voice must be an object")
            v_conf = tools.setdefault("voice", {})
            if "auto_start" in v_patch:
                v_conf["auto_start"] = bool(v_patch["auto_start"])
            if "hotkey" in v_patch:
                v_conf["hotkey"] = str(v_patch["hotkey"]).strip().lower()
            if "engine" in v_patch:
                engine = str(v_patch["engine"]).strip()
                if engine not in ("paraformer_streaming_2pass", "sensevoice_offline"):
                    raise ValueError(f"unsupported engine: {engine}")
                v_conf["engine"] = engine
            if "port" in v_patch:
                port = int(v_patch["port"])
                if not (1024 <= port <= 65535):
                    raise ValueError("port must be between 1024 and 65535")
                v_conf["port"] = port
            if "restore_clipboard" in v_patch:
                v_conf["restore_clipboard"] = bool(v_patch["restore_clipboard"])

    _save_conf(candidate)
    return _config_public_view(candidate)


MAX_RECORD_BYTES = 1024 * 1024 * 1024   # 1 GB 上限
KEEP_RATIO = 0.5                        # 超限时保留最近一半
_last_check = [0.0]


def _rotate_if_needed():
    """超过 MAX_RECORD_BYTES 时删除最旧的一半(保留最近记录)"""
    try:
        size = os.path.getsize(RECORDS)
    except Exception:
        return
    if size <= MAX_RECORD_BYTES:
        return
    keep_bytes = int(size * KEEP_RATIO)
    try:
        with open(RECORDS, "rb") as f:
            f.seek(size - keep_bytes)
            rest = f.read()
        # 丢弃首个不完整行
        nl = rest.find(b"\n")
        if nl >= 0:
            rest = rest[nl + 1:]
        tmp = RECORDS + ".tmp"
        with open(tmp, "wb") as f:
            f.write(rest)
        os.replace(tmp, RECORDS)
    except Exception:
        pass


def _sanitize_headers_for_record(headers):
    if not isinstance(headers, dict):
        return headers
    clean = {}
    for k, v in headers.items():
        if str(k).lower() in ("authorization", "x-api-key"):
            clean[k] = "<redacted>"
        else:
            clean[k] = v
    return clean


def record(entry):
    with LOCK:
        _IDX[0] += 1
        entry["idx"] = _IDX[0]
        if "headers" in entry and isinstance(entry["headers"], dict):
            entry["headers"] = _sanitize_headers_for_record(entry["headers"])
        with open(RECORDS, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # 实时轮转: 每条都检查(用缓存值快速跳过)
        if time.time() - _last_check[0] > 2.0:
            _last_check[0] = time.time()
            _rotate_if_needed()


def read_records():
    out = []
    if os.path.exists(RECORDS):
        with open(RECORDS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    return out


# ---------- codex 上游 (CLIProxyAPI) 管理 ----------

def _tcp(port, host="127.0.0.1", t=0.6):
    try:
        s = socket.create_connection((host, port), timeout=t); s.close(); return True
    except Exception:
        return False


def codex_up(conf=None):
    """CLIProxyAPI health check; port derives from configured base when possible."""
    try:
        import urllib.parse as _urlparse
        base = (_upstream_conf(conf or load_conf(), "codex").get("base") or "http://127.0.0.1:8317")
        u = _urlparse.urlparse(base)
        return _tcp(u.port or (443 if u.scheme == "https" else 80), u.hostname or "127.0.0.1")
    except Exception:
        return _tcp(8317)


def antigravity_up(conf=None):
    """Gemini sidecar health check; port derives from configured base when possible."""
    try:
        import urllib.parse as _urlparse
        base = (_upstream_conf(conf or load_conf(), "antigravity").get("base") or "http://127.0.0.1:8045")
        u = _urlparse.urlparse(base)
        return _tcp(u.port or (443 if u.scheme == "https" else 80), u.hostname or "127.0.0.1")
    except Exception:
        return _tcp(8045)


def antigravity_auto_start_enabled(conf=None):
    """Only explicit true enables implicit sidecar startup."""
    conf = conf or load_conf()
    tools = conf.get("tools") or {}
    settings = tools.get("antigravity") or {}
    return settings.get("auto_start") is True


_UPSTREAM_HEALTH = {
    "codex": {"api_ok": True, "reason": "", "ts": 0.0},
    "antigravity": {"api_ok": True, "reason": "", "ts": 0.0},
    "deepseek": {"api_ok": True, "reason": "", "ts": 0.0},
}
_UPSTREAM_HEALTH_LOCK = threading.Lock()
UPSTREAM_DEGRADED_TTL = 45.0


def record_upstream_health(name, ok, reason=""):
    """统一记录上游运行健康状态；gemini 自动映射为 antigravity。"""
    target = "antigravity" if name in ("gemini", "antigravity") else name
    with _UPSTREAM_HEALTH_LOCK:
        if ok:
            _UPSTREAM_HEALTH[target] = {"api_ok": True, "reason": "", "ts": time.time()}
        else:
            _UPSTREAM_HEALTH[target] = {"api_ok": False, "reason": reason or "API报错", "ts": time.time()}

_CODEX_PROXY_TARGET_CACHE = {"ts": 0.0, "target": None}
_CODEX_PROXY_TARGET_LOCK = threading.Lock()
_CODEX_PROXY_CHECK_CACHE = {"ts": 0.0, "ok": True, "port": None}
_CODEX_PROXY_CHECK_LOCK = threading.Lock()


def _read_codex_outbound_proxy(conf=None):
    """读取 codex-proxy/config.yaml 中顶层配置的外发 proxy-url。"""
    try:
        cfg_path = (conf or {}).get("codex_config") or os.path.join(BASE, "codex-proxy", "config.yaml")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith(" ") or line.startswith("\t"):
                        continue
                    m = re.match(r'^proxy-url:\s*["\']?([^"\'\s]+)["\']?', line)
                    if m:
                        return m.group(1).strip()
    except Exception:
        pass
    return ""


def _write_codex_outbound_proxy(conf, new_proxy):
    """原子更新 codex-proxy/config.yaml 中顶层 proxy-url 配置，并重置健康探活缓存。"""
    cfg_path = (conf or {}).get("codex_config") or os.path.join(BASE, "codex-proxy", "config.yaml")
    if not os.path.isfile(cfg_path):
        return False
    val = (new_proxy or "").strip()
    if not val:
        val = "direct"
    target_line = f'proxy-url: "{val}"\n'

    with open(cfg_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    replaced = False
    new_lines = []
    for line in lines:
        if not replaced and not line.startswith(" ") and not line.startswith("\t") and re.match(r'^proxy-url:\s*', line):
            new_lines.append(target_line)
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        new_lines.append("\n" + target_line)

    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.replace(tmp, cfg_path)

    with _CODEX_PROXY_TARGET_LOCK:
        _CODEX_PROXY_TARGET_CACHE["ts"] = 0.0
        _CODEX_PROXY_TARGET_CACHE["target"] = None
    with _CODEX_PROXY_CHECK_LOCK:
        _CODEX_PROXY_CHECK_CACHE["ts"] = 0.0
    return True


def _get_codex_proxy_target(conf=None):
    now = time.time()
    with _CODEX_PROXY_TARGET_LOCK:
        if now - _CODEX_PROXY_TARGET_CACHE["ts"] < 10.0:
            return _CODEX_PROXY_TARGET_CACHE["target"]
    target = None
    try:
        raw = _read_codex_outbound_proxy(conf)
        if raw and raw.lower() not in ("direct", "none"):
            import urllib.parse as _urlparse
            u = _urlparse.urlparse(raw)
            if u.hostname:
                port = u.port or (443 if u.scheme == "https" else 80)
                target = (u.hostname, port)
    except Exception:
        pass
    with _CODEX_PROXY_TARGET_LOCK:
        _CODEX_PROXY_TARGET_CACHE.update({"ts": now, "target": target})
    return target


def _check_codex_proxy_reachable(conf=None):
    now = time.time()
    with _CODEX_PROXY_CHECK_LOCK:
        if now - _CODEX_PROXY_CHECK_CACHE["ts"] < 3.0:
            return _CODEX_PROXY_CHECK_CACHE["ok"], _CODEX_PROXY_CHECK_CACHE["port"]
    target = _get_codex_proxy_target(conf)
    if not target:
        ok, port = True, None
    else:
        host, port = target
        ok = _tcp(port, host, t=0.15)
    with _CODEX_PROXY_CHECK_LOCK:
        _CODEX_PROXY_CHECK_CACHE.update({"ts": now, "ok": ok, "port": port})
    return ok, port


def get_upstream_health(conf, name, running=None):
    """三态健康检测:
    online: 运行中且 API 正常
    degraded: 运行中但 API 不可用 (如外发代理 7897 未开、认证失败、近期请求报 5xx)
    offline: 进程未运行或端口未监听
    """
    if name == "codex":
        is_run = codex_up() if running is None else bool(running)
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        now = time.time()
        with _UPSTREAM_HEALTH_LOCK:
            h = dict(_UPSTREAM_HEALTH.get("codex", {}))
        if h.get("api_ok") is False and (now - h.get("ts", 0.0) < UPSTREAM_DEGRADED_TTL):
            return {"state": "degraded", "running": True, "reason": h.get("reason") or "API报错"}
        ok, proxy_port = _check_codex_proxy_reachable(conf)
        if not ok and proxy_port:
            return {"state": "degraded", "running": True, "reason": f"代理未运行 (:{proxy_port})"}
        return {"state": "online", "running": True, "reason": ""}

    if name in ("antigravity", "gemini"):
        is_run = antigravity_up(conf) if running is None else bool(running)
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        now = time.time()
        with _UPSTREAM_HEALTH_LOCK:
            h = dict(_UPSTREAM_HEALTH.get("antigravity", {}))
        if h.get("api_ok") is False and (now - h.get("ts", 0.0) < UPSTREAM_DEGRADED_TTL):
            return {"state": "degraded", "running": True, "reason": h.get("reason") or "API报错"}
        return {"state": "online", "running": True, "reason": ""}

    if name == "voice":
        if running is not None and not running:
            return {"state": "offline", "running": False, "reason": ""}
        v_stat = voice_status_dict(conf)
        is_run = (bool(v_stat.get("ready", False) or voice_up(conf))) if running is None else bool(running)
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        if v_stat.get("status") == "loading_model":
            return {"state": "degraded", "running": True, "reason": "加载模型中"}
        if v_stat.get("ready"):
            return {"state": "online", "running": True, "reason": ""}
        return {"state": "degraded", "running": True, "reason": "未就绪"}

    return {"state": "online", "running": True, "reason": ""}


def codex_start(conf):
    if codex_up():
        return "already"
    exe = conf.get("codex_exe"); cfg = conf.get("codex_config")
    PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    cmd = (f"Start-Process -WindowStyle Hidden -FilePath '{exe}' "
           f"-ArgumentList '--config','{cfg}' -WorkingDirectory '{os.path.dirname(exe)}' "
           f"-RedirectStandardOutput '{os.path.dirname(exe)}\\proxy.out.log' "
           f"-RedirectStandardError '{os.path.dirname(exe)}\\proxy.err.log'")
    try:
        subprocess.Popen([PS, "-NoProfile", "-Command", cmd], creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return "start-failed"
    for _ in range(40):
        time.sleep(0.5)
        if codex_up():
            return "started"
    return "timeout"


def antigravity_start(conf):
    """按配置懒启动 Antigravity Tools; 已运行时不重复拉起。"""
    global _ANTIGRAVITY_PROCESS, _ANTIGRAVITY_EXE
    if antigravity_up(conf):
        return "already"
    exe = (conf.get("antigravity_exe") or "").strip()
    if not exe or not os.path.exists(exe):
        return "no-exe"
    try:
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return "start-failed"
    with _ANTIGRAVITY_PROCESS_LOCK:
        _ANTIGRAVITY_PROCESS = proc
        _ANTIGRAVITY_EXE = os.path.normcase(os.path.abspath(exe))
    for _ in range(40):
        time.sleep(0.5)
        if antigravity_up(conf):
            return "started"
        if proc.poll() is not None:
            break
    with _ANTIGRAVITY_PROCESS_LOCK:
        if _ANTIGRAVITY_PROCESS is proc:
            _ANTIGRAVITY_PROCESS = None
            _ANTIGRAVITY_EXE = ""
    return "timeout"


def antigravity_stop():
    """Stop only the Antigravity process started and still owned by this relay."""
    global _ANTIGRAVITY_PROCESS, _ANTIGRAVITY_EXE
    with _ANTIGRAVITY_PROCESS_LOCK:
        proc = _ANTIGRAVITY_PROCESS
        exe = _ANTIGRAVITY_EXE
        if proc is None:
            return "not-managed"
        if proc.poll() is not None:
            _ANTIGRAVITY_PROCESS = None
            _ANTIGRAVITY_EXE = ""
            return "not-running"
        pid = getattr(proc, "pid", None)
        if not pid or not exe:
            return "not-managed"
        try:
            result = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                                    text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                return "stop-failed"
        except Exception as e:
            return repr(e)
        finally:
            _ANTIGRAVITY_PROCESS = None
            _ANTIGRAVITY_EXE = ""
    return "stopped"


def antigravity_owned():
    with _ANTIGRAVITY_PROCESS_LOCK:
        return _ANTIGRAVITY_PROCESS is not None and _ANTIGRAVITY_PROCESS.poll() is None


def antigravity_open_ui(conf=None):
    """Wake up or launch Antigravity Tools window so user can access its native update interface."""
    conf = conf or load_conf()
    exe = (conf.get("antigravity_exe") or "").strip()
    if not exe:
        exe = os.path.expandvars(r"%LOCALAPPDATA%\Antigravity Tools\antigravity-tools.exe")
    if not os.path.exists(exe):
        return {"ok": False, "error": f"未找到 Antigravity Tools 可执行文件: {exe}"}

    # 1. Launch/wake up via process execution (Tauri single-instance triggers window restore & focus)
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    except Exception as e:
        return {"ok": False, "error": f"启动 Antigravity Tools 失败: {e}"}

    # 2. As an extra assist on Windows, bring its window to foreground via PowerShell AppActivate
    try:
        ps_cmd = (
            "$w = (Get-Process -Name 'antigravity-tools' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1); "
            "if ($w) { (New-Object -ComObject WScript.Shell).AppActivate($w.Id) }"
        )
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass

    return {
        "ok": True,
        "message": "已调出 Antigravity Tools 界面，请在软件中进行更新（设置 → 关于 → 检查更新）",
        "open_mode": "main_window"
    }


def codex_stop():
    try:
        result = subprocess.run(["taskkill", "/F", "/IM", "cli-proxy-api.exe"], capture_output=True,
                                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0 and codex_up():
            return "stop-failed"
        return "stopped"
    except Exception as e:
        return repr(e)


def _codex_request_enter():
    global _CODEX_ACTIVE_REQUESTS
    with _CODEX_MAINTENANCE:
        if _CODEX_MAINTENANCE_ACTIVE:
            return False
        _CODEX_ACTIVE_REQUESTS += 1
        return True


def _codex_request_leave():
    global _CODEX_ACTIVE_REQUESTS, _CODEX_LAST_REQUEST_TS
    with _CODEX_MAINTENANCE:
        _CODEX_ACTIVE_REQUESTS = max(0, _CODEX_ACTIVE_REQUESTS - 1)
        _CODEX_LAST_REQUEST_TS = time.time()
        _CODEX_MAINTENANCE.notify_all()


def _cpa_auto_update_enabled():
    try:
        return ((load_conf().get("tools") or {}).get("codex") or {}).get("auto_update") is True
    except Exception:
        return False


def _cpa_auto_update_idle():
    with _CODEX_MAINTENANCE:
        return _CODEX_ACTIVE_REQUESTS == 0 and time.time() - _CODEX_LAST_REQUEST_TS >= 60


def _codex_login_pending():
    try:
        manager = get_codex_login()
        with manager._lock:
            return manager._pending is not None
    except Exception:
        return False


def _install_cpa_update(staged_path, automatic=False):
    """Swap CPA only after requests drain; restore the previous executable on failure."""
    global _CODEX_MAINTENANCE_ACTIVE
    conf = load_conf()
    managed = os.path.abspath(os.path.join(BASE, "codex-proxy", "cli-proxy-api.exe"))
    configured = os.path.abspath(conf.get("codex_exe") or managed)
    if configured != managed or os.path.abspath(_get_cpa_updater(conf).exe_path) != managed:
        raise RuntimeError("refusing to update a CPA executable outside this project")
    deadline = time.time() + 300
    with _CODEX_MAINTENANCE:
        while True:
            active = _CODEX_ACTIVE_REQUESTS > 0
            quiet_left = max(0.0, 60.0 - (time.time() - _CODEX_LAST_REQUEST_TS)) if automatic else 0.0
            if not active and quiet_left <= 0:
                if automatic and not _cpa_auto_update_enabled():
                    raise RuntimeError("automatic update was disabled before installation")
                if _codex_login_pending():
                    raise RuntimeError("Codex login is in progress; update deferred")
                _CODEX_MAINTENANCE_ACTIVE = True
                break
            remaining = deadline - time.time()
            if remaining <= 0 or (automatic and not _cpa_auto_update_enabled()):
                raise RuntimeError("automatic update deferred; Codex is not idle or auto-update was disabled")
            wait_for = min(remaining, 1.0, quiet_left) if quiet_left > 0 else min(remaining, 1.0)
            _CODEX_MAINTENANCE.wait(wait_for)
    was_running = codex_up()
    backup = managed + ".bak"
    swapped = False
    try:
        if was_running:
            result = codex_stop()
            if result != "stopped":
                raise RuntimeError("could not stop current CPA process")
            for _ in range(50):
                if not codex_up():
                    break
                time.sleep(0.1)
            if codex_up():
                raise RuntimeError("CPA process did not exit")
        shutil.copy2(managed, backup)
        os.replace(staged_path, managed)
        swapped = True
        if was_running:
            started = codex_start(conf)
            if started not in ("started", "already"):
                raise RuntimeError("new CPA failed to start: " + str(started))
            for _ in range(10):
                if codex_up() and probe_upstream(conf, "codex", timeout=2).get("available"):
                    return
                time.sleep(0.5)
            raise RuntimeError("new CPA failed its /v1/models health check")
    except Exception as exc:
        if swapped:
            _get_cpa_updater(conf).set_update_state("rolling_back", error=str(exc)[:300])
            if codex_up():
                codex_stop()
                for _ in range(30):
                    if not codex_up():
                        break
                    time.sleep(0.1)
            try:
                os.replace(backup, managed)
                if was_running:
                    old_start = codex_start(conf)
                    if old_start not in ("started", "already"):
                        raise RuntimeError("rollback CPA failed to start: " + str(old_start))
            except Exception as rollback_exc:
                raise RuntimeError(f"{exc}; rollback failed: {rollback_exc}") from exc
        elif was_running and not codex_up():
            try:
                old_start = codex_start(conf)
                if old_start not in ("started", "already"):
                    raise RuntimeError("previous CPA failed to restart: " + str(old_start))
            except Exception as restart_exc:
                raise RuntimeError(f"{exc}; previous CPA restart failed: {restart_exc}") from exc
        raise
    finally:
        with _CODEX_MAINTENANCE:
            _CODEX_MAINTENANCE_ACTIVE = False
            _CODEX_MAINTENANCE.notify_all()


# ---------- 语音输入伴侣 (Voice Input Companion) ----------

_VOICE_PROCESS = None
_VOICE_PROCESS_LOCK = threading.Lock()
_VOICE_LAST_ERROR = None
_VOICE_JOB = None
_VOICE_JOB_LOCK = threading.Lock()
VOICE_PID_FILE = os.path.join(BASE, ".voice.pid")


def _get_voice_job():
    global _VOICE_JOB
    with _VOICE_JOB_LOCK:
        if _VOICE_JOB is not None:
            return _VOICE_JOB
        try:
            import win32job
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            _VOICE_JOB = job
        except Exception:
            _VOICE_JOB = False
        return _VOICE_JOB


def _probe_voice_service(conf=None, timeout=1.5):
    """向语音伴侣 HTTP /health 探针发起查询，成功则返回解析后的字典，否则返回 None。"""
    conf = conf or load_conf()
    port = conf.get("tools", {}).get("voice", {}).get("port", 8401)
    if not _tcp(port, t=0.08):
        return None
    try:
        import urllib.request as _urllib
        req = _urllib.Request(f"http://127.0.0.1:{port}/health", headers={"Origin": "http://127.0.0.1:8610"})
        with _urllib.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("service") == "voice":
                    return data
    except Exception:
        pass
    return None


def voice_up(conf=None):
    """探测语音 RPC 端口 (8401) 并通过 /health 握手验证是否确为 voice 服务"""
    return _probe_voice_service(conf) is not None


def _write_voice_pid(pid):
    """安全写出 .voice.pid 文件"""
    if not pid or not isinstance(pid, int) or pid <= 0:
        return
    try:
        tmp = VOICE_PID_FILE + f".tmp_{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "pid": pid,
                "create_time": time.time(),
                "cmdline_marker": "tools.voice_input",
            }, f)
        os.replace(tmp, VOICE_PID_FILE)
    except Exception:
        pass


def _get_listening_pid_win32(port):
    """在 Windows 上利用 netstat -ano 查找指定端口的监听 PID"""
    try:
        res = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        candidate = int(parts[-1])
                        if candidate > 0 and candidate != os.getpid():
                            return candidate
    except Exception:
        pass
    return None


def voice_status_dict(conf=None):
    """返回详细的语音状态机信息"""
    conf = conf or load_conf()
    v_conf = conf.get("tools", {}).get("voice", {})
    port = v_conf.get("port", 8401)
    status_info = {
        "status": "stopped",
        "ready": False,
        "pid": None,
        "owned": False,
        "hotkey": v_conf.get("hotkey", "mouse_x1"),
        "engine": v_conf.get("engine", "sensevoice_offline"),
        "last_error": _VOICE_LAST_ERROR,
    }

    # 1. 优先通过探针查询已在运行的服务
    probe_data = _probe_voice_service(conf, timeout=1.5)
    if probe_data:
        status_info["status"] = probe_data.get("status", "ready")
        status_info["ready"] = bool(probe_data.get("ready"))
        pid = probe_data.get("pid")
        if isinstance(pid, int) and pid > 0:
            status_info["pid"] = pid
            # Auto-adopt: 若此时磁盘缺失 .voice.pid，自动补写纳管
            if not os.path.isfile(VOICE_PID_FILE):
                _write_voice_pid(pid)
        status_info["engine"] = probe_data.get("engine", status_info["engine"])
        status_info["owned"] = True
        return status_info

    # 2. 若探针未响应，检查是否正处于启动中或已崩溃
    with _VOICE_PROCESS_LOCK:
        if _VOICE_PROCESS is not None:
            if _VOICE_PROCESS.poll() is None:
                status_info["status"] = "starting"
                status_info["pid"] = _VOICE_PROCESS.pid
                status_info["owned"] = True
            else:
                status_info["status"] = "error" if _VOICE_LAST_ERROR else "stopped"
        elif _VOICE_LAST_ERROR:
            status_info["status"] = "error"

    if not status_info["owned"] and os.path.isfile(VOICE_PID_FILE):
        status_info["owned"] = voice_owned(conf)

    return status_info


def voice_auto_start_enabled(conf=None):
    conf = conf or load_conf()
    tools = conf.get("tools") or {}
    settings = tools.get("voice") or {}
    return settings.get("auto_start") is True


def voice_start(conf=None):
    """启动语音伴侣子进程并写入 .voice.pid 与绑定 Job Object"""
    global _VOICE_PROCESS, _VOICE_LAST_ERROR
    conf = conf or load_conf()
    if voice_up(conf):
        _VOICE_LAST_ERROR = None
        return "already"

    v_conf = conf.get("tools", {}).get("voice", {})
    hotkey = v_conf.get("hotkey", "mouse_x1")
    engine = v_conf.get("engine", "sensevoice_offline")
    port = str(v_conf.get("port", 8401))

    try:
        from tools.voice_input.runtime import find_voice_python
        from pathlib import Path
        py_exe = find_voice_python(Path(BASE), engine=engine)
    except Exception:
        py_exe = sys.executable

    # 静默自愈依赖检测
    try:
        from tools.voice_input.runtime import ensure_voice_dependencies
        ensure_voice_dependencies(py_exe, timeout=30)
    except Exception:
        pass

    cmd = [py_exe, "-u", "-m", "tools.voice_input", "service", "--hotkey", hotkey, "--engine", engine, "--port", port]
    v_out = os.path.join(BASE, "voice.out.log")
    v_err = os.path.join(BASE, "voice.err.log")
    err_offset = os.path.getsize(v_err) if os.path.isfile(v_err) else 0

    try:
        out_f = open(v_out, "a", encoding="utf-8", errors="replace")
        err_f = open(v_err, "a", encoding="utf-8", errors="replace")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform == "win32":
            # 附加 BELOW_NORMAL_PRIORITY_CLASS (0x00004000) 确保语音子进程不抢占 Windows 前台与系统光标调度
            creationflags |= 0x00004000
        proc = subprocess.Popen(
            cmd,
            cwd=BASE,
            stdout=out_f,
            stderr=err_f,
            creationflags=creationflags,
        )
        try:
            out_f.close()
            err_f.close()
        except Exception:
            pass
    except Exception as e:
        _VOICE_LAST_ERROR = str(e)
        return f"start-failed: {e}"

    with _VOICE_PROCESS_LOCK:
        _VOICE_PROCESS = proc

    job = _get_voice_job()
    if job:
        try:
            import win32api, win32job
            handle = win32api.OpenProcess(win32api.PROCESS_ALL_ACCESS, False, proc.pid)
            win32job.AssignProcessToJobObject(job, handle)
        except Exception:
            pass

    try:
        with open(VOICE_PID_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "pid": proc.pid,
                "create_time": time.time(),
                "cmdline_marker": "tools.voice_input",
            }, f)
    except Exception:
        pass

    for _ in range(50):
        time.sleep(0.5)
        if voice_up(conf):
            _VOICE_LAST_ERROR = None
            return "started"
        if proc.poll() is not None:
            # 捕获真实闪退原因
            err_line = ""
            try:
                if os.path.isfile(v_err):
                    with open(v_err, "r", encoding="utf-8", errors="replace") as ef:
                        ef.seek(err_offset)
                        lines = [line.strip() for line in ef if line.strip()]
                        if lines:
                            err_line = lines[-1]
            except Exception:
                pass
            with _VOICE_PROCESS_LOCK:
                if _VOICE_PROCESS is proc:
                    _VOICE_PROCESS = None
            try:
                if os.path.isfile(VOICE_PID_FILE):
                    os.remove(VOICE_PID_FILE)
            except Exception:
                pass
            ret = proc.poll()
            reason = f": {err_line}" if err_line else f" (exit code {ret})"
            _VOICE_LAST_ERROR = f"exited with code {ret}{': ' + err_line if err_line else ''}"
            return f"start-failed{reason}"

    _VOICE_LAST_ERROR = "Startup wait timed out (25s)"
    return "timeout"


def voice_stop(conf=None):
    """安全停止语音服务：支持由内存句柄、.voice.pid 以及探针/端口解析的目标 PID"""
    global _VOICE_PROCESS, _VOICE_LAST_ERROR
    _VOICE_LAST_ERROR = None
    conf = conf or load_conf()
    port = conf.get("tools", {}).get("voice", {}).get("port", 8401)

    # 1. 尝试从内存句柄获取存活 PID
    pid_to_kill = None
    with _VOICE_PROCESS_LOCK:
        if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
            pid_to_kill = _VOICE_PROCESS.pid

    # 2. 尝试从 .voice.pid 读取记录
    if not pid_to_kill and os.path.isfile(VOICE_PID_FILE):
        try:
            with open(VOICE_PID_FILE, "r", encoding="utf-8") as f:
                pinfo = json.load(f)
            if pinfo.get("cmdline_marker") == "tools.voice_input":
                cand = pinfo.get("pid")
                if isinstance(cand, int) and cand > 0 and cand != os.getpid():
                    pid_to_kill = cand
        except Exception:
            pass

    # 3. 探针自愈：从 /health 获取真实自报 PID
    probe = None
    if not pid_to_kill:
        probe = _probe_voice_service(conf, timeout=1.5)
        if probe:
            cand = probe.get("pid")
            if isinstance(cand, int) and cand > 0 and cand != os.getpid():
                pid_to_kill = cand

    # 4. 端口监听定位兜底 (Windows)：仅在探针已核验是 voice 服务但未包含 pid 时查询
    if not pid_to_kill and probe and sys.platform == "win32":
        pid_to_kill = _get_listening_pid_win32(port)

    # 找不到任何可信目标 PID
    if not pid_to_kill:
        with _VOICE_PROCESS_LOCK:
            _VOICE_PROCESS = None
        if _tcp(port, t=0.08):
            return "not-managed"
        if os.path.isfile(VOICE_PID_FILE):
            try:
                os.remove(VOICE_PID_FILE)
            except Exception:
                pass
        return "not-running"

    # 执行终止
    stop_ok = False
    try:
        if sys.platform == "win32":
            res = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid_to_kill)],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stop_ok = res.returncode == 0
        else:
            import signal
            os.kill(pid_to_kill, signal.SIGTERM)
            stop_ok = True
    except Exception as e:
        _VOICE_LAST_ERROR = str(e)

    # 确认端口释放
    for _ in range(20):
        if not _tcp(port, t=0.05):
            stop_ok = True
            break
        time.sleep(0.1)

    with _VOICE_PROCESS_LOCK:
        _VOICE_PROCESS = None

    if os.path.isfile(VOICE_PID_FILE):
        try:
            os.remove(VOICE_PID_FILE)
        except Exception:
            pass

    if stop_ok or not _tcp(port, t=0.05):
        return "stopped"
    return "stop-failed"


def voice_owned(conf=None):
    with _VOICE_PROCESS_LOCK:
        if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
            return True
    if os.path.isfile(VOICE_PID_FILE):
        return True
    # 若服务端口响应且确为本系统 voice 伴侣服务，即便丢失 .voice.pid 也视作由本中继纳管
    if voice_up(conf):
        return True
    return False


# ---------- 路由决策 ----------

CODEX_MODELS = ["gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-luna",
                "gpt-5.6-terra", "gpt-5.5", "gpt-5.3-codex-spark"]
DEEPSEEK_MODELS = ["deepseek-flash", "deepseek-v4-pro"]
# Antigravity 8045 的运行时模型优先; 这些只用于上游不可用时的安全回退。
GEMINI_MODELS = ["gemini-3.7-flash-low", "gemini-3.7-flash-thinking", "gemini-3.7-flash", "gemini-2.5-flash", "gemini-2.5-pro"]
DEFAULT_CODEX_MAP = "gpt-5.6-sol"
DEFAULT_DS_MAP = "deepseek-flash"
DEFAULT_GEMINI_MAP = "gemini-3.7-flash-low"
# 推理强度 -> 请求 body 的 thinking 参数
# 档位依据: GPT API 原生档位支持 low/medium/high/xhigh/max; 经 CLIProxyAPI 转换后
#   budget 阈值映射到 codex 的 reasoning effort (513-1024->low, 1025-8192->medium, 8193-24576->high, 24577+->xhigh/max)
REASONING_MAP = {
    "off":     {"type": "disabled"},
    "on":      {"type": "enabled"},
    "instant": {"type": "enabled", "budget_tokens": 1024},  # 兼容别名, 映射至 low
    "low":     {"type": "enabled", "budget_tokens": 1024},  # CLIProxyAPI 513-1024 为 low
    "medium":  {"type": "enabled", "budget_tokens": 8192},  # 1025-8192 为 medium
    "high":    {"type": "enabled", "budget_tokens": 16384}, # 8193-24576 为 high
    "xhigh":   {"type": "enabled", "budget_tokens": 32768}, # 24577+ 为 xhigh
    "max":     {"type": "enabled", "budget_tokens": 65536}, # 65536+ 为 max
}
EFFORT_VALUES = tuple(REASONING_MAP)
_MODELS_CACHE = {"ts": 0, "ds": list(DEEPSEEK_MODELS), "cx": list(CODEX_MODELS),
                 "gm": list(GEMINI_MODELS), "health": {}}
_MODELS_LOCK = threading.Lock()
_MODELS_REFRESHING = False


def _fetch_models(base, key, timeout=6, prefix=None):
    """从上游 /v1/models 拉模型名(过滤图像类); 返回空表示上游不可用"""
    if not base:
        return []
    try:
        import urllib.request as _u
        req = _u.Request(base.rstrip("/") + "/v1/models")
        if key:
            req.add_header("Authorization", "Bearer " + key)
            req.add_header("x-api-key", key)
        d = json.load(_u.urlopen(req, timeout=timeout))
        out = []
        for m in d.get("data", []):
            mid = m.get("id") or ""
            if "image" in mid or "auto-review" in mid:
                continue
            if prefix and not mid.startswith(prefix):
                continue
            out.append(mid)
        return sorted(set(out))
    except Exception:
        return []


def _upstream_conf(conf, name):
    """读取 provider 配置, 兼容旧配置里的 antigravity upstream 命名"""
    ups = conf.get("upstreams") or {}
    if name == "antigravity":
        return ups.get("gemini") or ups.get("antigravity") or {}
    return ups.get(name) or {}


def _key_for(conf, up, name=None):
    if not isinstance(up, dict):
        return ""
    field = up.get("key_env")
    if name:
        field = _safe_key_env_name(name, field) or _canonical_key_field(name)
    else:
        all_allowed = set().union(*UPSTREAM_ALLOWED_KEYS.values())
        if field not in all_allowed:
            return ""
    return conf.get(field, "") or ""


def provider_for_model(model, conf=None):
    """显式模型归属优先, 前缀仅作兼容回退; 未知模型返回 None。"""
    model = (model or "").strip()
    if not model:
        return None
    conf = conf or {}
    routes = conf.get("model_routes") or {}
    if routes.get(model) in ("deepseek", "codex", "antigravity", "gemini"):
        p = routes[model]
        return "antigravity" if p == "gemini" else p
    for p, values in (("deepseek", DEEPSEEK_MODELS), ("codex", CODEX_MODELS),
                      ("antigravity", GEMINI_MODELS)):
        if model in values:
            return p
    if model.startswith("gpt-"):
        return "codex"
    if model.startswith("deepseek-"):
        return "deepseek"
    if model.startswith("gemini-"):
        return "antigravity"
    return None


def probe_upstream(conf, name="antigravity", timeout=6):
    """只读探测模型端点，不发送对话请求，也不返回任何凭据。"""
    import urllib.parse as _urlparse
    actual = "antigravity" if name in ("gemini", "antigravity") else name
    up = _upstream_conf(conf, actual)
    base = (up.get("base") or "").rstrip("/")
    if not base:
        record_upstream_health(actual, False, "missing base")
        return {"name": actual, "available": False, "error": "missing base"}
    try:
        req = urllib.request.Request(base + "/v1/models", method="GET")
        key = _key_for(conf, up, actual)
        if key:
            req.add_header("Authorization", "Bearer " + key)
            req.add_header("x-api-key", key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
        ids = [str(x.get("id") or "") for x in payload.get("data", []) if isinstance(x, dict)]
        record_upstream_health(actual, True)
        return {"name": actual, "available": True, "models": sorted(set(ids)),
                "messages_path": base + "/v1/messages"}
    except Exception as e:
        record_upstream_health(actual, False, str(e)[:240])
        return {"name": actual, "available": False, "error": str(e)[:240],
                "messages_path": base + "/v1/messages"}


def _bg_fetch_models(conf, now):
    global _MODELS_REFRESHING
    try:
        ds = _fetch_models((_upstream_conf(conf, "deepseek")).get("base", ""),
                           _key_for(conf, _upstream_conf(conf, "deepseek"), "deepseek"), prefix="deepseek-")
        cx = _fetch_models((_upstream_conf(conf, "codex")).get("base", ""),
                           _key_for(conf, _upstream_conf(conf, "codex"), "codex"), prefix="gpt-")
        gm = _fetch_models((_upstream_conf(conf, "antigravity")).get("base", ""),
                           _key_for(conf, _upstream_conf(conf, "antigravity"), "antigravity"), prefix="gemini-")
        if gm:
            record_upstream_health("antigravity", True)
        if cx:
            record_upstream_health("codex", True)
        if ds:
            record_upstream_health("deepseek", True)
        if not ds:
            ds = list(DEEPSEEK_MODELS)
        if not cx:
            cx = list(CODEX_MODELS)
        if not gm:
            gm = list(GEMINI_MODELS)
        with _MODELS_LOCK:
            _MODELS_CACHE.update({"ts": now, "ds": ds, "cx": cx, "gm": gm,
                                  "health": {"deepseek": bool(ds), "codex": bool(cx),
                                              "antigravity": bool(gm)}})
    finally:
        _MODELS_REFRESHING = False


def live_models(conf, ttl=120):
    """实时模型列表(带缓存): 后台异步从三上游拉, 避免网络阻塞主工作线程, 失败回退各自常量"""
    global _MODELS_REFRESHING
    now = time.time()
    with _MODELS_LOCK:
        expired = (now - _MODELS_CACHE["ts"] >= ttl)
        if expired and not _MODELS_REFRESHING:
            _MODELS_REFRESHING = True
            threading.Thread(target=_bg_fetch_models, args=(conf, now), daemon=True).start()
        return dict(_MODELS_CACHE)


def _strip_model_suffix(model):
    """剥离 CC 附加的后缀: deepseek-chat[1m] / relay-main[1m] / 本地中转[1m] -> 基名"""
    if not model:
        return "", ""
    import re as _re
    m = _re.match(r'^\s*(.+?)\s*\[([^\]]+)\]\s*$', model)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return model.strip(), ""


RELAY_MAIN_PLACEHOLDERS = {
    "本地中转",
    "relay",
    "local",
    "relay-main",
    "cc-relay",
    "main",
}


def is_relay_placeholder(model: str) -> bool:
    """判断是否为中转占位模型名 (不区分大小写，支持常见变体与前缀)"""
    if not model:
        return True
    m = str(model).strip().lower()
    if m in RELAY_MAIN_PLACEHOLDERS:
        return True
    if m.startswith(("relay-main", "relay_", "local-", "local_")):
        return True
    if m in ("relay", "local") or "本地中转" in m:
        return True
    return False


def is_cli_placeholder(model: str) -> bool:
    """判断是否为 CLI 占位符模型名 (如 relay-main, OPUS_MODEL, SONNET_MODEL, FAST_MODEL 等)"""
    if not model:
        return True
    m = str(model).strip().lower()
    if is_relay_placeholder(m):
        return True
    if m in ("opus_model", "sonnet_model", "fast_model", "relay-opus", "relay-sonnet", "relay-fast"):
        return True
    return False


def _classify_request_role(headers, body_json, requested_model=""):
    """统一角色分流函数：无论是占位符 relay-main 还是官方模型名 (claude-*)，
    统一根据请求头与 system 提示词内容区分角色分流：
    1. 优先子代理检测与角色细分：
       - Plan 代理 -> agent 档 (架构设计与长程规划)
       - Explore 代理 -> opus 档 (代码搜索与大上下文探索)
       - 安全审查 / 状态行 / Guide 等特定辅助代理 -> sonnet 档
       - 其他子代理 (携带 Agent ID 或 cc_is_subagent) -> agent 档 (默认主力智能体)
    2. 辅助轻量任务：
       - 会话命名 / 快速任务 (naming a coding session 等) -> fast 档
       - 未知纯 Haiku 请求 -> fast 档
    3. 主会话循环 (CLI 默认、Desktop 默认等) -> main 档
    """
    sys = (body_json or {}).get("system")
    txt = ""
    if isinstance(sys, str):
        txt = sys
    elif isinstance(sys, list):
        txt = " ".join((c.get("text") or "") for c in sys if isinstance(c, dict))

    # 1. Plan 规划代理 (优先级最高，确保精准走 agent)
    if any(sig in txt for sig in (
        "software architect and planning specialist",
        "planning specialist for Claude Code",
        "software architect agent for designing implementation plans",
    )):
        return "agent"

    # 2. Explore 搜索代理 (走 opus 档)
    if any(sig in txt for sig in (
        "file search specialist for Claude Code",
        "thoroughly navigating and exploring codebases",
    )):
        return "opus"

    # 3. 安全审查 / 状态行 / Guide 等特定辅助代理
    if any(sig in txt for sig in (
        "security monitor for autonomous AI coding agents",
        "status line setup agent for Claude Code",
        "You are the Claude guide agent",
        "claude-code-guide",
    )):
        return "sonnet"

    # 4. 会话命名 / 快速任务
    if any(sig in txt for sig in (
        "naming a coding session",
        "short noun phrase of two to five words",
    )):
        return "fast"

    # 5. 明确带有子代理标记 (有 agent-id 或 cc_is_subagent=true 或 Claude Agent SDK) -> 走 agent 档
    if _is_subagent(headers, body_json):
        return "agent"

    # 6. 未知纯 Haiku 请求 -> 走 fast 档
    if str(requested_model or "").lower().startswith("claude-haiku"):
        return "fast"

    # 7. 其余（包括带 <application_details> 的 Desktop 主循环等）一律视为主循环 -> main
    return "main"


# 保留别名以保持兼容
_route_by_system_prompt = _classify_request_role


def _is_subagent(headers, body_json):
    """识别子代理: 请求头带 x-claude-code-agent-id (大小写不敏感), 或 system 里 billing header
    含 cc_is_subagent=true, 或提示含 'Claude Agent SDK' / 'You are a Claude agent'
    / 'software architect and planning specialist' 等 (Plan 代理特定特征)"""
    try:
        hdrs = headers or {}
        if hasattr(hdrs, "items"):
            for k, v in hdrs.items():
                if str(k).strip().lower() == "x-claude-code-agent-id" and v:
                    return True
        sys = (body_json or {}).get("system")
        txt = ""
        if isinstance(sys, str):
            txt = sys
        elif isinstance(sys, list):
            txt = " ".join((c.get("text") or "") for c in sys if isinstance(c, dict))
        signatures = (
            "cc_is_subagent=true",
            "Claude Agent SDK",
            "You are a Claude agent",
            "software architect and planning specialist",
            "planning specialist for Claude Code",
            "software architect agent for designing implementation plans",
            "file search specialist for Claude Code",
            "thoroughly navigating and exploring codebases",
            "You are a software engineer for Claude Code",
        )
        return any(sig in txt for sig in signatures)
    except Exception:
        return False


# ---------- CC 指纹清理 (每档独立开关) ----------
CC_ID_PAT = (r"(?:You are Claude Code,?\s*Anthropic's official CLI for Claude\.?"
             r"|You are a Claude agent,\s*built on Anthropic's Claude Agent SDK\.?)")
CC_BILLING_PREFIX = "x-anthropic-billing-header:"

TIER_KEYS = ("main", "opus", "sonnet", "fast", "agent")
# 旧配置 strip_cc_banner=true 的迁移去向(主档 + fast 档)
LEGACY_STRIP_DEFAULTS = {"main": True, "opus": False, "sonnet": False, "fast": True, "agent": False}


def _strip_flags(router):
    """开关归一化 -> 五键布尔对象 (读接口 / 生效判断 / 写入合并的唯一入口)"""
    v = router.get("strip_cc_banner")
    if isinstance(v, dict):
        return {k: v.get(k) is True for k in TIER_KEYS}   # 严格布尔, 防 "false" 被当成真
    if v is True:
        return dict(LEGACY_STRIP_DEFAULTS)
    return {k: False for k in TIER_KEYS}


def _tier_from_reason(reason):
    """reason -> 档位名; 非档位(hybrid:gpt-direct / route:*)返回 None"""
    if not isinstance(reason, str) or not reason.startswith("hybrid:"):
        return None
    t = reason[len("hybrid:"):]
    return t if t in TIER_KEYS else None


def _strip_cc_fingerprint(body):
    """删除 system 里的 CC 身份句与 billing 指纹块, 返回 (新 body, 删除块数)
    - 整块命中 -> 整块丢弃; 句子混在别的文本里 -> 只摘句子
    - 被删块的 cache_control 顺延给其后第一个没有该标记的幸存块, 免得白白丢 prompt-cache 断点
    """
    import re as _re
    sysv = body.get("system")
    if sysv is None:
        return body, 0

    # 客户端直接发字符串形式
    if isinstance(sysv, str):
        ns = _re.sub(CC_ID_PAT, "", sysv)
        if ns == sysv:
            return body, 0
        nb = dict(body)
        if ns.strip():
            nb["system"] = ns
        else:
            nb.pop("system", None)
        return nb, 1

    if not isinstance(sysv, list):
        return body, 0

    kept = []
    carried = []   # (被删块本该落到的下标, cache_control)
    removed = 0
    for blk in sysv:
        txt = blk.get("text") if isinstance(blk, dict) else None
        if isinstance(txt, str):
            t = txt.strip()
            if t.startswith(CC_BILLING_PREFIX) or _re.fullmatch(CC_ID_PAT, t):
                removed += 1
                if blk.get("cache_control"):
                    carried.append((len(kept), blk["cache_control"]))
                continue
            if _re.search(CC_ID_PAT, txt):
                nb_blk = dict(blk)
                nb_blk["text"] = _re.sub(CC_ID_PAT, "", txt)
                removed += 1
                kept.append(nb_blk)
                continue
        kept.append(blk)

    if not removed:
        return body, 0

    # 缓存断点顺延: 优先落给其后最近的幸存块; 该处已有断点则不动;
    # 被删的是尾块时向前落到最近的幸存块
    for pos, cc in carried:
        placed = False
        for i in range(pos, len(kept)):
            blk = kept[i]
            if not isinstance(blk, dict):
                continue
            if blk.get("cache_control"):
                placed = True          # 已有断点覆盖该位置, 不必再往后推
                break
            nb_blk = dict(blk)
            nb_blk["cache_control"] = cc
            kept[i] = nb_blk
            placed = True
            break
        if placed:
            continue
        for i in range(min(pos, len(kept)) - 1, -1, -1):
            blk = kept[i]
            if not isinstance(blk, dict):
                continue
            if blk.get("cache_control"):
                break
            nb_blk = dict(blk)
            nb_blk["cache_control"] = cc
            kept[i] = nb_blk
            break

    nb = dict(body)
    if kept:
        nb["system"] = kept
    else:
        nb.pop("system", None)   # 别发 "system": []
    return nb, removed


# ---------- 自定义请求修改器 (热重载与安全加载) ----------
MODIFIER_FILE = os.path.join(BASE, "custom_modifier.py")


class ModifierManager:
    """管理 custom_modifier.py 的热重载与安全调用"""
    def __init__(self):
        self._mod = None
        self._mtime = 0.0
        self._err = None

    def get_modifier(self):
        """检查 custom_modifier.py 修改时间，有变更时自动热重载"""
        try:
            if not os.path.exists(MODIFIER_FILE):
                return None
            mtime = os.path.getmtime(MODIFIER_FILE)
            if mtime != self._mtime or self._mod is None:
                import importlib.util
                spec = importlib.util.spec_from_file_location("custom_modifier", MODIFIER_FILE)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    self._mod = mod
                    self._mtime = mtime
                    self._err = None
        except Exception as e:
            self._err = repr(e)
            print(f"[WARN] 加载 custom_modifier.py 失败: {e}", file=sys.stderr)
        return self._mod


_MOD_MGR = ModifierManager()


# ---------- 各档位 System 提示词管理 (复用运行时抓包 + 在线编辑) ----------
PROMPTS_FILE = os.path.join(BASE, "prompts.json")
# PromptManager.update_tier()/capture() hold this lock while calling save().
# This must be re-entrant: a plain Lock leaves the UI request waiting forever
# when it tries to persist a prompt or a per-tier modifier choice.
PROMPTS_LOCK = threading.RLock()


class PromptManager:
    """管理各档位 System 提示词的抓包捕获、在线编辑与请求体替换"""
    def __init__(self):
        self._data = {
            "captured": {k: "" for k in TIER_KEYS},
            "custom": {k: "" for k in TIER_KEYS},
            "enabled": {k: False for k in TIER_KEYS},
        }
        self.load()

    def load(self):
        with PROMPTS_LOCK:
            if os.path.exists(PROMPTS_FILE):
                try:
                    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        for section in ("captured", "custom", "enabled"):
                            if isinstance(d.get(section), dict):
                                self._data[section].update(d[section])
                except Exception:
                    pass
            else:
                self._preseed_from_records()

    def _preseed_from_records(self):
        """若无 prompts.json, 自动从历史 records.jsonl 提取各档位捕获的原始 system 提示词"""
        if not os.path.exists(RECORDS):
            return
        try:
            with open(RECORDS, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        reason = r.get("route_reason") or ""
                        tier = reason.replace("hybrid:", "") if reason.startswith("hybrid:") else None
                        if tier not in TIER_KEYS:
                            continue
                        b = r.get("body") or {}
                        sys = b.get("system")
                        if sys and not self._data["captured"][tier]:
                            if isinstance(sys, list):
                                txt = "\n\n".join(blk.get("text", "") for blk in sys if isinstance(blk, dict) and blk.get("text"))
                            else:
                                txt = str(sys)
                            self._data["captured"][tier] = txt
                    except Exception:
                        pass
            self.save()
        except Exception:
            pass

    def save(self):
        with PROMPTS_LOCK:
            tmp = PROMPTS_FILE + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, PROMPTS_FILE)
            except Exception:
                pass

    def get_data(self):
        with PROMPTS_LOCK:
            return {
                "captured": dict(self._data["captured"]),
                "custom": dict(self._data["custom"]),
                "enabled": dict(self._data["enabled"]),
                "counts": {
                    k: {
                        "captured_len": len(self._data["captured"].get(k, "")),
                        "custom_len": len(self._data["custom"].get(k, "")),
                        "enabled": bool(self._data["enabled"].get(k, False)),
                    }
                    for k in TIER_KEYS
                }
            }

    def update_tier(self, tier, custom_text=None, enabled=None):
        if tier not in TIER_KEYS:
            return False
        with PROMPTS_LOCK:
            if custom_text is not None:
                self._data["custom"][tier] = str(custom_text)
            if enabled is not None:
                self._data["enabled"][tier] = bool(enabled)
        self.save()
        return True

    def capture(self, tier, body_json):
        """运行时抓包: 记录进入中转的原版 system 提示词"""
        if tier not in TIER_KEYS or not isinstance(body_json, dict):
            return
        sys = body_json.get("system")
        if not sys:
            return
        if isinstance(sys, list):
            txt = "\n\n".join(blk.get("text", "") for blk in sys if isinstance(blk, dict) and blk.get("text"))
        else:
            txt = str(sys)
        txt = txt.strip()
        if not txt:
            return
        if self._data["captured"].get(tier) != txt:
            with PROMPTS_LOCK:
                self._data["captured"][tier] = txt
            self.save()

    def apply_custom(self, tier, body_json):
        """若开启了自定义提示词，将修改后的提示词注入/替换到 body_json 中，并保留 cache_control"""
        if tier not in TIER_KEYS or not isinstance(body_json, dict):
            return body_json, False
        if not self._data["enabled"].get(tier):
            return body_json, False
        custom_txt = (self._data["custom"].get(tier) or "").strip()
        if not custom_txt:
            return body_json, False

        orig_sys = body_json.get("system")
        nb = dict(body_json)
        if isinstance(orig_sys, list) and orig_sys:
            cc = None
            for blk in orig_sys:
                if isinstance(blk, dict) and blk.get("cache_control"):
                    cc = blk["cache_control"]
                    break
            new_blk = {"type": "text", "text": custom_txt}
            if cc:
                new_blk["cache_control"] = cc
            nb["system"] = [new_blk]
        else:
            nb["system"] = custom_txt
        return nb, True


_PROMPT_MGR = PromptManager()


def pick_route(conf, headers, body_json):
    """统一路由: 返回 (upstream_name, map_model|None, reason)
    router.route = "hybrid" | "codex" | "deepseek" | "antigravity"
    router.model = 全量路由的指定模型(可空=用默认)
    """
    router = conf.get("router") or {}
    route = (router.get("route") or "hybrid").strip().lower()
    # 对外兼容更直观的 gemini 命名, 内部统一使用实际后端 antigravity。
    if route == "gemini":
        route = "antigravity"
    forced = (router.get("model") or "").strip()
    raw_model = ""
    if isinstance(body_json, dict):
        raw_model = body_json.get("model") or ""
    # 剥离 [1m] 等后缀; 若基名是中转占位名(relay-main, 本地中转等), 视为未指定模型
    model, _suffix = _strip_model_suffix(raw_model)
    if is_relay_placeholder(model):
        model = ""

    if route == "codex":
        m = forced if provider_for_model(forced, conf) == "codex" else DEFAULT_CODEX_MAP
        return "codex", m, "route:codex"
    if route == "deepseek":
        m = forced or DEFAULT_DS_MAP
        return "deepseek", m, "route:deepseek"
    if route == "antigravity":
        m = forced or (router.get("hybrid_antigravity_model") or "").strip() or DEFAULT_GEMINI_MAP
        return "antigravity", m, "route:antigravity"

    # hybrid: 高价值档(plan) -> 高价值模型; 其余按档位 -> 各自模型; 上游随所选模型决定
    hv = (router.get("hybrid_codex_model") or router.get("model") or "").strip() or DEFAULT_CODEX_MAP
    dd = (router.get("hybrid_deepseek_model") or "").strip() or DEFAULT_DS_MAP
    # 五档位模型(hybrid 下)
    tier = router.get("tiers") or {}
    def _up(m):
        return provider_for_model(m, conf) or "deepseek"
    raw_base, _ = _strip_model_suffix(raw_model)
    m_lower = raw_base.lower()

    # ---------------- 阶段一：显式专属占位符 ----------------
    # 显式指定某档位的占位符，直接映射到对应档位
    if m_lower in ("opus_model", "relay-opus"):
        m = (tier.get("opus") or hv).strip()
        return _up(m), m, "hybrid:opus"
    if m_lower in ("sonnet_model", "relay-sonnet"):
        m = (tier.get("sonnet") or dd).strip()
        return _up(m), m, "hybrid:sonnet"
    if m_lower in ("fast_model", "relay-fast"):
        m = (tier.get("fast") or dd).strip()
        return _up(m), m, "hybrid:fast"

    # ---------------- 阶段二：直连已知 provider 模型 (gpt-*, gemini-*, deepseek-*) ----------------
    if provider_for_model(model, conf) == "codex":
        return "codex", model, "hybrid:gpt-direct"
    if provider_for_model(model, conf) == "antigravity":
        return "antigravity", model, "hybrid:antigravity-direct"

    # ---------------- 阶段三：统一智能分流引擎 ----------------
    # 无论收到的是 relay-main 占位符、无模型请求，还是官方模型名 (如 claude-opus-5, claude-sonnet-5 等)，
    # 统一通过提示词与 Agent 特征进行智能角色分流：
    role = _classify_request_role(headers, body_json, requested_model=raw_base)
    if role == "agent":
        m = (tier.get("agent") or dd).strip()
        return _up(m), m, "hybrid:agent"
    elif role == "opus":
        m = (tier.get("opus") or hv).strip()
        return _up(m), m, "hybrid:opus"
    elif role == "sonnet":
        m = (tier.get("sonnet") or dd).strip()
        return _up(m), m, "hybrid:sonnet"
    elif role == "fast":
        m = (tier.get("fast") or dd).strip()
        return _up(m), m, "hybrid:fast"
    else:
        # 主循环角色或默认
        m = (tier.get("main") or dd).strip()
        return _up(m), m, "hybrid:main"


def _resp_model(resp_body):
    """从响应体解析上游真实模型名"""
    if not resp_body:
        return None
    try:
        s = resp_body
        # streaming 以 'data: ' 行开头
        if s.lstrip().startswith("event:") or "\ndata:" in s[:200] or s.lstrip().startswith("data:"):
            for line in s.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        j = json.loads(line[5:].strip())
                        m = (j.get("message") or {}).get("model") or j.get("model")
                        if m:
                            return m
                    except Exception:
                        pass
            # fallback: regex
            import re as _re
            mm = _re.search(r'"model"\s*:\s*"([^"]+)"', s)
            return mm.group(1) if mm else None
        j = json.loads(s)
        return j.get("model")
    except Exception:
        import re as _re
        mm = _re.search(r'"model"\s*:\s*"([^"]+)"', resp_body)
        return mm.group(1) if mm else None


def _resp_cache_usage(resp_body):
    """从普通 JSON 或 Anthropic SSE 响应提取 prompt-cache usage。"""
    if not resp_body:
        return None

    merged = {}
    saw_usage = False

    def token_count(value):
        if isinstance(value, bool):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def merge_usage(usage):
        nonlocal saw_usage
        if not isinstance(usage, dict):
            return
        fields = ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "output_tokens")
        found = False
        cache_found = False
        for name in fields:
            if name in usage:
                value = token_count(usage.get(name))
                if value is not None:
                    merged[name] = value
                    found = True
                    cache_found = cache_found or (name != "input_tokens" and name != "output_tokens")
        if "completion_tokens" in usage and "output_tokens" not in merged:
            c_val = token_count(usage.get("completion_tokens"))
            if c_val is not None:
                merged["output_tokens"] = c_val
                found = True
        # Some compatible providers expose cache breakdowns instead of a total.
        if "cache_creation_input_tokens" not in merged:
            breakdown = usage.get("cache_creation")
            if isinstance(breakdown, dict):
                parts = [token_count(breakdown.get(k)) or 0 for k in
                         ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
                if any(k in breakdown for k in ("ephemeral_5m_input_tokens",
                                                "ephemeral_1h_input_tokens")):
                    merged["cache_creation_input_tokens"] = sum(parts)
                    found = True
                    cache_found = True
        # count_tokens responses often contain only input_tokens; they are not
        # message cache diagnostics and should remain unavailable in the viewer.
        saw_usage = saw_usage or cache_found or (found and "output_tokens" in merged)

    def walk(value):
        if isinstance(value, dict):
            merge_usage(value.get("usage"))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    text = str(resp_body)
    try:
        walk(json.loads(text))
    except Exception:
        pass
    # SSE usage snapshots are cumulative; merge later fields over earlier ones.
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        if "usage" not in payload and "cache" not in payload:
            continue
        try:
            walk(json.loads(payload))
        except Exception:
            pass
    if not saw_usage:
        return None

    input_tokens = merged.get("input_tokens", 0)
    cache_read = merged.get("cache_read_input_tokens", 0)
    cache_creation = merged.get("cache_creation_input_tokens", 0)
    output_tokens = merged.get("output_tokens", 0)
    total = input_tokens + cache_read + cache_creation
    return {
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cache_total_tokens": total,
        "cache_hit_rate": (cache_read / total) if total else None,
        "output_tokens": output_tokens,
    }


def _iter_records_tail(max_records=400, max_bytes=500_000_000):
    """从文件末尾逆序向前读, 拿够 max_records 条或到 max_bytes 为止(逆序块流式快速解析, 支持单条超大记录)"""
    if not os.path.exists(RECORDS):
        return []
    try:
        size = os.path.getsize(RECORDS)
    except OSError:
        return []
    if size == 0:
        return []

    chunk_size = 4_000_000
    pos = size
    records = []
    trailing = b""
    bytes_read = 0

    with open(RECORDS, "rb") as f:
        while pos > 0 and len(records) < max_records and bytes_read < max_bytes:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            bytes_read += read_size
            buf = chunk + trailing
            lines = buf.split(b"\n")
            if pos > 0:
                trailing = lines[0]
                complete_lines = lines[1:]
            else:
                trailing = b""
                complete_lines = lines

            for raw_line in reversed(complete_lines):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    records.append(json.loads(raw_line.decode("utf-8", "replace")))
                    if len(records) >= max_records:
                        break
                except Exception:
                    pass

        if trailing and len(records) < max_records:
            t = trailing.strip()
            if t:
                try:
                    records.append(json.loads(t.decode("utf-8", "replace")))
                except Exception:
                    pass

    records.reverse()
    return records


def _find_record_by_idx(target_idx, max_bytes=800_000_000):
    """高效逆序定位单个历史抓包详情, 支持从海量大记录中精准抓取"""
    if not os.path.exists(RECORDS):
        return None
    target_idx_str = str(target_idx)
    target_pattern = f'"idx": {target_idx_str}'.encode("utf-8")
    target_pattern2 = f'"idx":{target_idx_str}'.encode("utf-8")
    target_pattern3 = f'"idx": "{target_idx_str}"'.encode("utf-8")

    try:
        size = os.path.getsize(RECORDS)
    except OSError:
        return None
    if size == 0:
        return None

    chunk_size = 4_000_000
    pos = size
    trailing = b""
    bytes_read = 0

    with open(RECORDS, "rb") as f:
        while pos > 0 and bytes_read < max_bytes:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            bytes_read += read_size
            buf = chunk + trailing
            lines = buf.split(b"\n")
            if pos > 0:
                trailing = lines[0]
                complete_lines = lines[1:]
            else:
                trailing = b""
                complete_lines = lines

            for raw_line in reversed(complete_lines):
                if target_pattern in raw_line or target_pattern2 in raw_line or target_pattern3 in raw_line:
                    try:
                        r = json.loads(raw_line.decode("utf-8", "replace"))
                        if str(r.get("idx")) == target_idx_str:
                            return r
                    except Exception:
                        pass

        if trailing and (target_pattern in trailing or target_pattern2 in trailing or target_pattern3 in trailing):
            try:
                r = json.loads(trailing.decode("utf-8", "replace"))
                if str(r.get("idx")) == target_idx_str:
                    return r
            except Exception:
                pass

    return None


_STATUS_TAIL_BYTES = 500_000_000
_CALLS_TAIL_BYTES = 600_000_000
_CALLS_CACHE_LIMIT = 8
_STATS_CACHE = {"fingerprint": None, "rows": None, "total": 0, "overall_avg_speed": None}
_CALLS_CACHE = {}
_STATS_LOCK = threading.Lock()
_CALLS_LOCK = threading.Lock()


def _records_fingerprint():
    """On-disk cache key; intentionally independent of this process's _IDX."""
    try:
        stat = os.stat(RECORDS)
    except OSError:
        return (os.path.abspath(RECORDS), None, None, None, None)
    return (os.path.abspath(RECORDS), getattr(stat, "st_dev", None),
            getattr(stat, "st_ino", None), stat.st_mtime_ns, stat.st_size)


def stats_snapshot():
    """Cache record aggregates; configuration and model state remain live per call."""
    with _STATS_LOCK:
        # Capture before parsing. An append during the read must invalidate it next time.
        fingerprint = _records_fingerprint()
        if _STATS_CACHE["fingerprint"] == fingerprint and _STATS_CACHE["rows"] is not None:
            rows = [dict(row) for row in _STATS_CACHE["rows"]]
            total_records = _STATS_CACHE["total"]
        else:
            recs = _iter_records_tail(max_records=400, max_bytes=_STATUS_TAIL_BYTES)
            by = {}
            total_duration_all = 0.0
            total_output_tokens_all = 0
            for r in recs:
                real_model = _resp_model(r.get('resp_body')) or r.get('sent_model') or r.get('orig_model')
                key = (r.get('route'), real_model)
                b = by.setdefault(key, {'route': r.get('route'), 'model': real_model,
                                        'sent': r.get('sent_model'), 'req': 0, 'ok': 0, 'err': 0, 'last': 0,
                                        'input_tokens': 0, 'cache_read_tokens': 0,
                                        'cache_creation_tokens': 0, 'cache_requests': 0,
                                        'output_tokens': 0, 'total_duration': 0.0, 'speed_count': 0})
                usage = r.get('cache') if r.get('cache') is not None else _resp_cache_usage(r.get('resp_body'))
                if usage:
                    b['input_tokens'] += usage.get('cache_total_tokens', 0) or 0
                    b['cache_read_tokens'] += usage.get('cache_read_tokens', 0) or 0
                    b['cache_creation_tokens'] += usage.get('cache_creation_tokens', 0) or 0
                    b['cache_requests'] += 1

                dur = r.get('duration')
                out_tok = r.get('output_tokens')
                if out_tok is None and usage:
                    out_tok = usage.get('output_tokens')
                spd = r.get('speed')
                if spd is None and dur and out_tok and dur > 0 and out_tok > 0:
                    spd = round(out_tok / dur, 1)

                if dur and out_tok and dur > 0 and out_tok > 0:
                    b['output_tokens'] += out_tok
                    b['total_duration'] += dur
                    b['speed_count'] += 1
                    total_output_tokens_all += out_tok
                    total_duration_all += dur

                b['req'] += 1
                st = r.get('resp_status') or 0
                if st and st < 400:
                    b['ok'] += 1
                else:
                    b['err'] += 1
                b['last'] = r.get('idx', 0)
            for b in by.values():
                total = b['input_tokens']
                b['cache_hit_rate'] = (b['cache_read_tokens'] / total) if total else None
                if b['total_duration'] > 0 and b['output_tokens'] > 0:
                    b['avg_speed'] = round(b['output_tokens'] / b['total_duration'], 1)
                else:
                    b['avg_speed'] = None
            overall_avg_speed = round(total_output_tokens_all / total_duration_all, 1) if (total_duration_all > 0 and total_output_tokens_all > 0) else None
            rows = sorted(by.values(), key=lambda x: -x['last'])
            total_records = len(recs)
            _STATS_CACHE.update({"fingerprint": fingerprint, "rows": rows, "total": total_records, "overall_avg_speed": overall_avg_speed})
            rows = [dict(row) for row in rows]
    conf = load_conf()
    rt = conf.get('router') or {}
    lm = live_models(conf)
    route = rt.get('route', 'hybrid')
    if route == 'gemini':
        route = 'antigravity'
    codex_available = codex_up(conf)
    antigravity_available = antigravity_up(conf)
    v_stat = voice_status_dict(conf)
    voice_available = v_stat.get("ready", False) or voice_up(conf)
    cx_health = get_upstream_health(conf, "codex", running=codex_available)
    gm_health = get_upstream_health(conf, "antigravity", running=antigravity_available)
    vc_health = get_upstream_health(conf, "voice", running=voice_available)
    cpa_status = _get_cpa_updater(conf).status()
    relay_update_status = _get_relay_updater(conf).status()
    ag_update_status = antigravity_updater.get_antigravity_updater(conf).status()
    # 兼容旧 UI 字段名，同时暴露 Gemini 独立模型桶。
    res = {'route': route, 'mode': route,
            'traffic_paused': traffic_paused(conf),
            'paused_waiting_requests': _paused_waiting_count(),
            'antigravity_auto_start': antigravity_auto_start_enabled(conf),
            'instance_id': _PROCESS_INSTANCE_ID,
            'model': rt.get('model', ''),
            'models_ds': lm['ds'], 'models_codex': lm['cx'], 'models_gemini': lm['gm'],
            # 旧 UI / API 兼容别名
            'deepseek_models': lm['ds'], 'codex_models': lm['cx'], 'gemini_models': lm['gm'],
            'all_models': lm['ds'] + lm['cx'] + lm['gm'],
            'settings_model': rt.get('model', '') or '(自动)',
            'env_base': 'http://127.0.0.1:8400',
            'env_model': rt.get('model', '') or route,
            'codex_default_model': 'gpt-5.6-sol',
            'hybrid_codex_model': (rt.get('hybrid_codex_model') or rt.get('model') or '').strip() or 'gpt-5.6-sol',
            'hybrid_deepseek_model': (rt.get('hybrid_deepseek_model') or '').strip() or 'deepseek-flash',
            'hybrid_antigravity_model': (rt.get('hybrid_antigravity_model') or '').strip() or DEFAULT_GEMINI_MAP,
            # 五档位模型(hybrid): main/opus/sonnet/fast/agent(子代理)
            'tiers': {
                'main':   (rt.get('tiers') or {}).get('main')   or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
                'opus':   (rt.get('tiers') or {}).get('opus')   or (rt.get('hybrid_codex_model') or 'gpt-5.6-sol'),
                'sonnet': (rt.get('tiers') or {}).get('sonnet') or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
                'fast':   (rt.get('tiers') or {}).get('fast')   or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
                'agent':  (rt.get('tiers') or {}).get('agent')  or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
            },
            # 环境变量名与档位对应(供 UI 展示)
            'tier_env': {
                'main': 'ANTHROPIC_MODEL',
                'opus': 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                'sonnet': 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                'fast': 'ANTHROPIC_SMALL_FAST_MODEL',
                'agent': 'ANTHROPIC_MODEL·plan代理',
            },
            # 推理强度(预留字段, UI 可展示/回传); 每档可独立配置 tier_efforts
            'efforts': list(EFFORT_VALUES),
            'effort': (rt.get('effort') or 'medium'),
            # 各档指纹清理开关(五张档位卡右上角)
            'strip_cc_banner': _strip_flags(rt),
            'modifier_mode': (rt.get('modifier_mode') or conf.get('modifier_mode') or 'custom').strip().lower(),
            # 每档三态修改器: original(原版纯透传) / builtin(内置清理) / custom(自定义提示词); 空串=未显式配置, 回退全局
            'tier_modifier': {k: str(((rt.get('tier_modifier') or {}).get(k)) or '').strip().lower() for k in TIER_KEYS},
            'gemini_modifier': str(rt.get('gemini_modifier') or '').strip().lower(),
            'modifier_file_exists': os.path.exists(MODIFIER_FILE),
            'modifier_error': _MOD_MGR._err,
            'tier_efforts': {
                'main':   ('low' if (rt.get('tier_efforts') or {}).get('main') == 'instant' else ((rt.get('tier_efforts') or {}).get('main') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
                'opus':   ('low' if (rt.get('tier_efforts') or {}).get('opus') == 'instant' else ((rt.get('tier_efforts') or {}).get('opus') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
                'sonnet': ('low' if (rt.get('tier_efforts') or {}).get('sonnet') == 'instant' else ((rt.get('tier_efforts') or {}).get('sonnet') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
                'fast':   ('low' if (rt.get('tier_efforts') or {}).get('fast') == 'instant' else ((rt.get('tier_efforts') or {}).get('fast') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
                'agent':  ('low' if (rt.get('tier_efforts') or {}).get('agent') == 'instant' else ((rt.get('tier_efforts') or {}).get('agent') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
            },
            'deepseek_profile': {'default_model': 'deepseek-flash'},
            'hybrid_pins': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'gpt-5.6-sol'},
            'proxy_running': codex_available,
            'rows': rows, 'total': total_records,
            'overall_avg_speed': _STATS_CACHE.get('overall_avg_speed'),
            'codex_up': codex_available, 'antigravity_up': antigravity_available,
            'antigravity_owned': antigravity_owned(),
            'voice_up': voice_available,
            'voice_owned': voice_owned(conf),
            'voice_status': v_stat,
            'codex_state': cx_health["state"],
            'codex_reason': cx_health["reason"],
            'antigravity_state': gm_health["state"],
            'antigravity_reason': gm_health["reason"],
            'voice_state': vc_health["state"],
            'voice_reason': vc_health["reason"],
            'cpa_version': cpa_status.get('current_version'),
            'cpa_latest_version': cpa_status.get('latest_version'),
            'cpa_has_update': cpa_status.get('has_update', False),
            'cpa_update_checking': cpa_status.get('checking', False),
            'cpa_update_check_error': cpa_status.get('check_error'),
            'cpa_update_checked_at': cpa_status.get('last_checked'),
            'cpa_update_state': cpa_status.get('update_state', 'idle'),
            'cpa_update_message': cpa_status.get('update_message'),
            'cpa_update_error': cpa_status.get('update_error'),
            'cpa_release_notes': cpa_status.get('release_notes', ''),
            'cpa_download_progress': cpa_status.get('download_progress', 0.0),
            'cpa_downloaded_bytes': cpa_status.get('downloaded_bytes', 0),
            'cpa_total_bytes': cpa_status.get('total_bytes', 0),
            'relay_update': relay_update_status,
            'antigravity_version': ag_update_status.get('current_version'),
            'antigravity_latest_version': ag_update_status.get('latest_version'),
            'antigravity_has_update': ag_update_status.get('has_update', False),
            'antigravity_update': ag_update_status,
            'upstreams_status': {
                'deepseek': {'available': True, 'state': 'online'},
                'codex': {
                    'available': cx_health["state"] == "online",
                    'running': codex_available,
                    'state': cx_health["state"],
                    'reason': cx_health["reason"],
                    'managed': True,
                },
                'gemini': {
                    'available': gm_health["state"] == "online",
                    'running': antigravity_available,
                    'state': gm_health["state"],
                    'reason': gm_health["reason"],
                    'managed': antigravity_owned(),
                },
                'voice': {
                    'available': vc_health["state"] == "online",
                    'running': voice_available,
                    'state': vc_health["state"],
                    'reason': vc_health["reason"],
                    'status': v_stat.get('status', 'stopped'),
                    'managed': voice_owned(conf),
                    'hotkey': v_stat.get('hotkey', 'mouse_x1'),
                    'engine': v_stat.get('engine', 'sensevoice_offline'),
                },
            },
            'last_up': _UP['last'], 'last_model': _UP['last_model']}
    return res


def _calls_snapshot(n):
    """Return cached, body-free call viewer summaries for one requested range."""
    with _CALLS_LOCK:
        # Use the pre-read version just like status: append during parsing is not hidden.
        fingerprint = _records_fingerprint()
        key = (fingerprint, n)
        cached = _CALLS_CACHE.get(key)
        if cached is not None:
            calls, total = cached
            return {"calls": [dict(call) for call in calls], "total": total}
        recs = _iter_records_tail(max_records=n, max_bytes=_CALLS_TAIL_BYTES)
        calls = []
        for r in recs:
            body = r.get("body") or {}
            headers = {k.lower(): v for k, v in (r.get("headers") or {}).items()}
            cache = r.get("cache") if r.get("cache") is not None else _resp_cache_usage(r.get("resp_body"))
            duration = r.get("duration")
            out_tok = r.get("output_tokens")
            if out_tok is None and isinstance(cache, dict):
                out_tok = cache.get("output_tokens")
            speed = r.get("speed")
            if speed is None and duration and out_tok and duration > 0 and out_tok > 0:
                speed = round(out_tok / duration, 1)
            calls.append({
                "idx": r.get("idx"),
                "ts": r.get("ts"),
                "path": r.get("path"),
                "orig_model": r.get("orig_model"),
                "sent_model": r.get("sent_model"),
                "route": r.get("route"),
                "route_reason": r.get("route_reason"),
                "status": r.get("resp_status"),
                "agent_id": (headers.get("x-claude-code-agent-id") or "")[:12],
                "session": (headers.get("x-claude-code-session-id") or "")[:8],
                "msgs": len(body.get("messages") or []),
                "tools": len(body.get("tools") or []),
                "stream": body.get("stream"),
                "cache": cache,
                "resp_error": r.get("resp_error"),
                "has_custom_prompt": bool(r.get("custom_prompt")),
                "stripped_banner": bool(r.get("stripped_banner")),
                "traffic_paused": bool(r.get("traffic_paused") or r.get("route") == "paused"),
                "paused_wait_sec": r.get("paused_wait_sec"),
                "duration": duration,
                "output_tokens": out_tok or 0,
                "speed": speed,
            })
        # Keep a bounded number of small summaries even when the UI changes n.
        if len(_CALLS_CACHE) >= _CALLS_CACHE_LIMIT:
            _CALLS_CACHE.clear()
        _CALLS_CACHE[key] = (calls, len(calls))
        return {"calls": [dict(call) for call in calls], "total": len(calls)}


# ---------- HTTP ----------

def _send_sse_error(wfile, err_payload, is_anthropic=True):
    """向已建立 200 chunked SSE 连接的客户端下发协议级错误事件并正常关闭分块流."""
    try:
        if is_anthropic:
            chunk = f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n".encode("utf-8")
        else:
            chunk = f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n\ndata: [DONE]\n\n".encode("utf-8")
        wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
        wfile.write(b"0\r\n\r\n")
        wfile.flush()
    except Exception:
        pass


def _is_client_socket_closed(sock):
    """检测下游客户端 TCP 连接是否已断开 (FIN / RST / 异常)."""
    if not sock:
        return False
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            peek = sock.recv(1, socket.MSG_PEEK)
            if not peek:
                return True
    except Exception:
        return True
    return False


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"cc-relay/{getattr(relay_updater, 'RELAY_VERSION', '2.4.1')}"

    def log_message(self, *a):
        pass

    def _do(self, method):
        # 每次请求重读 config -> UI 改 mode 即时生效
        try:
            if hasattr(self.server, "reload_conf") and callable(self.server.reload_conf):
                conf = self.server.reload_conf()
            else:
                conf = load_conf()
            self.server.conf = conf
        except Exception:
            conf = getattr(self.server, "conf", None) or load_conf()

        # 下游 fake_api_key 认证校验 (支持 Bearer / x-api-key, 常量时间比对防时序攻击)
        fake_key = (conf.get("fake_api_key") or "").strip()
        if fake_key and not self.path.startswith("/api/"):
            auth_hdr = self.headers.get("Authorization", "").strip()
            bearer_tok = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
            x_api_key = self.headers.get("x-api-key", "").strip()
            import hmac
            valid = False
            if bearer_tok and hmac.compare_digest(bearer_tok, fake_key):
                valid = True
            elif x_api_key and hmac.compare_digest(x_api_key, fake_key):
                valid = True
            if not valid:
                msg = json.dumps({"error": {"type": "authentication_error", "message": "invalid api key"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(msg)
                return

        # /v1/models 探测: 聚合三上游模型列表(供 CC 识别)
        if self.path.split("?")[0] == "/v1/models" and method == "GET":
            lm = live_models(conf)
            names = sorted(set(lm['ds'] + lm['cx'] + lm['gm'] +
                               ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]))
            body = json.dumps({"data": [{"id": m, "object": "model", "owned_by": "relay"} for m in names],
                               "object": "list"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path

        body_json = None
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype and raw:
            try:
                body_json = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                body_json = None

        is_anthropic = path.startswith("/v1/messages") or "anthropic" in self.headers.get("anthropic-version", "").lower()

        # 流量闸门: 用户手动暂停所有上游流量时，流式请求模拟挂起与保活，非流式请求等待恢复
        is_stream_early = False
        t_pause_wait = 0.0
        if traffic_paused(conf):
            is_stream = False
            if isinstance(body_json, dict) and body_json.get("stream") is True:
                is_stream = True
            elif "text/event-stream" in self.headers.get("Accept", "").lower():
                is_stream = True

            t_pause_start = time.time()
            _paused_waiting_inc()
            client_aborted = False

            try:
                if is_stream:
                    # 模拟流式响应: 先行发送 HTTP 200 与分块传输头，使 Agent 认为请求已成功受理并进入流式接收
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache, no-transform")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("X-CC-Relay-Traffic-Paused", "1")
                        self.end_headers()
                    except Exception:
                        client_aborted = True

                    # 针对协议类型选用标准保活帧:
                    # Anthropic (/v1/messages): 标准 ping 事件 (SDK 自动 ignore 并重置超时计时器)
                    # OpenAI 等: 标准 SSE 注释行 (: keep-alive\n\n, SDK 自动丢弃注释保持长连接)
                    if is_anthropic:
                        ping_payload = b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
                    else:
                        ping_payload = b": keep-alive\n\n"

                    # 立即发送首个保活 chunk
                    if not client_aborted:
                        try:
                            self.wfile.write(f"{len(ping_payload):x}\r\n".encode("ascii") + ping_payload + b"\r\n")
                            self.wfile.flush()
                        except Exception:
                            client_aborted = True

                    # 挂起轮询保活循环
                    last_ping_t = time.time()
                    while not client_aborted:
                        if _is_client_socket_closed(self.connection):
                            client_aborted = True
                            break
                        # 检查恢复事件 (设置 0.25s 超时以便定期发送保活 chunk 并快速感知断开)
                        if _TRAFFIC_UNPAUSED_EVENT.wait(timeout=0.25):
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                break
                        else:
                            # 超时兜底校验磁盘配置
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                _TRAFFIC_UNPAUSED_EVENT.set()
                                break

                        # 仍处于暂停状态，每 3 秒发送一次保活 chunk (同时探测客户端写断开)
                        now = time.time()
                        if now - last_ping_t >= 3.0:
                            last_ping_t = now
                            try:
                                self.wfile.write(f"{len(ping_payload):x}\r\n".encode("ascii") + ping_payload + b"\r\n")
                                self.wfile.flush()
                            except Exception:
                                client_aborted = True
                                break

                    is_stream_early = not client_aborted
                else:
                    # 非流式请求: 不提前发响应头，在 socket 上等待恢复信号
                    while True:
                        if _is_client_socket_closed(self.connection):
                            client_aborted = True
                            break
                        if _TRAFFIC_UNPAUSED_EVENT.wait(timeout=0.25):
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                break
                        else:
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                _TRAFFIC_UNPAUSED_EVENT.set()
                                break
            finally:
                _paused_waiting_dec()

            t_pause_wait = round(time.time() - t_pause_start, 2)

            if client_aborted:
                # 客户端在挂起期间主动断开 (例如 Ctrl+C)
                self.close_connection = True
                try:
                    record({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "method": method, "path": path,
                        "client": self.client_address[0] if self.client_address else "127.0.0.1",
                        "headers": {k: v for k, v in self.headers.items()},
                        "body": body_json,
                        "body_raw": raw.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                        "route": "paused", "route_reason": "traffic_paused_aborted",
                        "orig_model": (body_json or {}).get("model") if isinstance(body_json, dict) else None,
                        "sent_model": None,
                        "stripped_banner": 0, "custom_prompt": False,
                        "upstream": None, "resp_status": 499,
                        "resp_body": "client disconnected while traffic was paused",
                        "cache": None,
                        "resp_error": "client_aborted",
                        "duration": t_pause_wait,
                        "paused_wait_sec": t_pause_wait,
                        "traffic_paused": True,
                    })
                except Exception:
                    pass
                return

        up_name, map_model, reason = pick_route(conf, self.headers, body_json)
        if up_name == "codex":
            with _CODEX_MAINTENANCE:
                if _CODEX_MAINTENANCE_ACTIVE:
                    msg = json.dumps({"error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}).encode()
                    if is_stream_early:
                        _send_sse_error(self.wfile, {"type": "error", "error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}, is_anthropic=is_anthropic)
                    else:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(msg)))
                        self.send_header("Retry-After", "3")
                        self.end_headers()
                        if method != "HEAD":
                            self.wfile.write(msg)
                    return

        # 若路由到外部 sidecar, 尽量懒启动; 不因启动失败吞掉后续可诊断错误。
        if up_name == "codex" and not codex_up():
            codex_start(conf)
        elif (up_name == "antigravity" and antigravity_auto_start_enabled(conf)
              and not antigravity_up(conf)):
            antigravity_start(conf)

        up = _upstream_conf(conf, up_name)
        base = (up.get("base") or "").strip()
        if not base:
            msg = json.dumps({"error": {"type": "relay_error", "message": "upstream is not configured: " + up_name}}).encode()
            if is_stream_early:
                _send_sse_error(self.wfile, {"type": "error", "error": {"type": "relay_error", "message": "upstream is not configured: " + up_name}}, is_anthropic=is_anthropic)
            else:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(msg)
            return
        codex_request_active = False
        upstream = base.rstrip("/") + path

        # model 改写
        orig_model = (body_json or {}).get("model") if isinstance(body_json, dict) else None
        sent_model = orig_model
        if map_model and isinstance(body_json, dict) and body_json.get("model") != map_model:
            body_json = dict(body_json); body_json["model"] = map_model
            sent_model = map_model
            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        # 推理强度注入: effort(全局, 可按档位覆盖) -> body.thinking
        router_cfg = conf.get("router") or {}
        tier_hit = _tier_from_reason(reason)   # 档位命中, 强度与去指纹共用
        effort = (router_cfg.get("effort") or "medium").strip()
        te = router_cfg.get("tier_efforts") or {}
        if tier_hit:
            effort = (te.get(tier_hit) or effort)
        # Gemini/Antigravity 的 thinking 语义由其兼容层决定，先保留客户端原值，
        # 不把 Codex 专用 budget_tokens 直接覆盖进去。
        if effort in REASONING_MAP and up_name != "antigravity" and isinstance(body_json, dict):
            body_json = dict(body_json)
            body_json["thinking"] = REASONING_MAP[effort]
            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        # 1. 运行时抓包: 记录进入中转的原版 system 提示词
        if tier_hit and isinstance(body_json, dict):
            _PROMPT_MGR.capture(tier_hit, body_json)

        # 2. 若当前档位开启了在线自定义提示词，用前端配置的内容替换 (并保留 prompt-cache)
        #    仅当该档三态显式选为 custom 才替换; 其余(含未配置, 默认原版)不替换
        custom_prompt_applied = False
        if tier_hit and isinstance(body_json, dict):
            _tm = str((router_cfg.get("tier_modifier") or {}).get(tier_hit) or "").strip().lower()
            if _tm == "custom":
                body_json, custom_prompt_applied = _PROMPT_MGR.apply_custom(tier_hit, body_json)
                if custom_prompt_applied:
                    raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        # 请求头基础过滤与鉴权注入
        fwd = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection", "authorization", "x-api-key", "accept-encoding"):
                continue
            fwd[k] = v
        key = _key_for(conf, up, up_name)
        if key:
            fwd["x-api-key"] = key
            fwd["authorization"] = "Bearer " + key
        fwd["Accept-Encoding"] = "identity"

        # 请求修改与指纹处理: 每档右上角三选框(原版/内置清理/自定义提示词)优先;
        # 未显式配置的档位 / 非 hybrid 请求回退到全局 modifier_mode
        mod_mode = (router_cfg.get("modifier_mode") or conf.get("modifier_mode") or "custom").strip().lower()
        tier_strip_on = bool(tier_hit and _strip_flags(router_cfg).get(tier_hit, False))
        tier_mod_map = router_cfg.get("tier_modifier") or {}
        eff_mode = mod_mode
        per_tier_explicit = False
        if tier_hit:
            _tm = str(tier_mod_map.get(tier_hit) or "") if isinstance(tier_mod_map, dict) else ""
            _tm = _tm.strip().lower()
            if _tm in ("original", "builtin", "custom"):
                eff_mode = _tm
            else:
                eff_mode = "original"   # 该档未显式配置 -> 默认原版纯透传
            per_tier_explicit = True
        elif up_name == "antigravity":
            _gm = str(router_cfg.get("gemini_modifier") or "").strip().lower()
            eff_mode = _gm if _gm in ("original", "builtin") else "builtin"
            per_tier_explicit = True
        stripped_n = 0

        # 构建上下文供修改器使用
        mod_ctx = {
            "upstream_name": up_name,
            "tier": tier_hit,
            "orig_model": orig_model,
            "target_model": sent_model,
            "path": path,
            "method": method,
            "is_subagent": _is_subagent(self.headers, body_json),
            "tier_strip_on": tier_strip_on,
            "modifier_mode": eff_mode,
        }

        if eff_mode == "original":
            # 【原版纯透传】: 不对 body 和 headers 做任何修改或指纹过滤
            stripped_n = 0
        elif eff_mode == "builtin":
            # 【内置清理】: 删除 CC 身份句 + billing 指纹块
            if isinstance(body_json, dict):
                body_json, stripped_n = _strip_cc_fingerprint(body_json)
                if stripped_n:
                    raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        else:
            # 【自定义】
            if per_tier_explicit:
                # 每档自定义提示词: system 已在上面 apply_custom 替换, 不再跑全局 python 脚本
                stripped_n = 0
            else:
                # 老全局 custom: 从 custom_modifier.py 热重载执行
                mod = _MOD_MGR.get_modifier()
                if mod:
                    # 1. 执行自定义请求体修改
                    try:
                        if hasattr(mod, "modify_body"):
                            nb_json, nb_raw, sn = mod.modify_body(body_json, raw, mod_ctx)
                            if nb_raw is not None:
                                raw = nb_raw
                                if nb_json is not None:
                                    body_json = nb_json
                            elif nb_json is not None:
                                body_json = nb_json
                                raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                            stripped_n = int(sn or 0)
                    except Exception as e:
                        print(f"[WARN] custom_modifier.modify_body 执行失败: {e}", file=sys.stderr)
                        if tier_strip_on and isinstance(body_json, dict):
                            body_json, stripped_n = _strip_cc_fingerprint(body_json)
                            if stripped_n:
                                raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

                    # 2. 执行自定义请求头修改
                    try:
                        if hasattr(mod, "modify_headers"):
                            new_fwd = mod.modify_headers(fwd, mod_ctx)
                            if isinstance(new_fwd, dict):
                                fwd = new_fwd
                    except Exception as e:
                        print(f"[WARN] custom_modifier.modify_headers 执行失败: {e}", file=sys.stderr)
                else:
                    # 若无自定义文件或加载失败，安全回退到内置规则
                    if tier_strip_on and isinstance(body_json, dict):
                        body_json, stripped_n = _strip_cc_fingerprint(body_json)
                        if stripped_n:
                            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        data = raw if raw else None
        if up_name == "codex":
            if not _codex_request_enter():
                msg = json.dumps({"error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}).encode()
                if is_stream_early:
                    _send_sse_error(self.wfile, {"type": "error", "error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}, is_anthropic=is_anthropic)
                else:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(msg)))
                    self.send_header("Retry-After", "3")
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(msg)
                return
            codex_request_active = True

        # 转发
        status = 502
        rbody = b""
        rheaders = {}
        err = None
        t0 = time.time()
        try:
            req = urllib.request.Request(upstream, data=data, method=method)
            for k, v in fwd.items():
                req.add_header(k, v)
            if up.get("proxy_url") and up["proxy_url"] != "direct":
                op = urllib.request.build_opener(urllib.request.ProxyHandler(
                    {"http": up["proxy_url"], "https": up["proxy_url"]}))
            elif conf.get("proxy_url") and conf["proxy_url"] != "direct" and up.get("proxy_url") == "direct":
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            else:
                op = urllib.request.build_opener()
            resp = op.open(req, timeout=900)
            status = resp.status
            rheaders = dict(resp.headers)
            if is_stream_early:
                rbody_chunks = []
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    rbody_chunks.append(chunk)
                    try:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                self.close_connection = True
                rbody = b"".join(rbody_chunks)
            else:
                rbody = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            rbody = e.read()
            rheaders = dict(e.headers) if e.headers else {}
            if is_stream_early:
                self.close_connection = True
                try:
                    err_json = json.loads(rbody.decode("utf-8", "replace"))
                except Exception:
                    err_json = {"type": "error", "error": {"type": "upstream_error", "message": rbody.decode("utf-8", "replace")[:500]}}
                _send_sse_error(self.wfile, err_json, is_anthropic=is_anthropic)
        except Exception as e:
            err = repr(e)
            rbody = json.dumps({"error": {"type": "relay_error", "message": err}}).encode()
            rheaders = {"Content-Type": "application/json"}
            if is_stream_early:
                self.close_connection = True
                err_json = {"type": "error", "error": {"type": "relay_error", "message": err}}
                _send_sse_error(self.wfile, err_json, is_anthropic=is_anthropic)
        finally:
            if codex_request_active:
                _codex_request_leave()

        duration = max(0.001, round(time.time() - t0, 2))
        cache_data = _resp_cache_usage(rbody.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)])
        out_tok = (cache_data.get("output_tokens") if isinstance(cache_data, dict) else 0) or 0
        speed = round(out_tok / duration, 1) if (out_tok > 0 and duration > 0) else None

        # 记录
        try:
            record({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": method, "path": path,
                "client": self.client_address[0],
                "headers": {k: v for k, v in self.headers.items()},
                "body": body_json, "body_raw": raw.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                "route": up_name, "route_reason": reason,
                "orig_model": orig_model, "sent_model": sent_model,
                "stripped_banner": stripped_n,
                "custom_prompt": custom_prompt_applied,
                "upstream": upstream, "resp_status": status,
                "resp_body": rbody.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                "cache": cache_data,
                "resp_error": err,
                "duration": duration,
                "paused_wait_sec": t_pause_wait if t_pause_wait > 0 else None,
                "traffic_paused": bool(t_pause_wait > 0),
                "output_tokens": out_tok,
                "speed": speed,
            })
        except Exception:
            pass

        # 更新上游运行时健康状态
        if up_name in ("codex", "antigravity", "gemini", "deepseek"):
            target_up = "antigravity" if up_name == "gemini" else up_name
            if status and status < 400:
                record_upstream_health(target_up, True)
            elif status and (400 <= status < 500) and status not in (401, 403):
                # 客户端请求错误 (400/404/422 等) 属于客户端请求语义问题，不影响上游服务本身的健康度
                pass
            else:
                err_str = ""
                if rbody:
                    try:
                        err_str = rbody.decode("utf-8", "replace")
                    except Exception:
                        err_str = ""
                if not err_str and err:
                    err_str = str(err)
                reason_text = "API报错"
                m_dial = re.search(r'dial tcp [^:\s]+:(\d+)', err_str)
                if m_dial and "dial HTTP proxy failed" in err_str:
                    reason_text = f"代理未运行 (:{m_dial.group(1)})"
                elif "dial HTTP proxy failed" in err_str:
                    reason_text = "代理连接失败"
                elif status in (401, 403):
                    reason_text = f"鉴权失败 ({status})"
                elif status and status >= 500:
                    reason_text = f"上游报错 ({status})"
                elif err:
                    reason_text = "网络超时"
                record_upstream_health(target_up, False, reason_text)

        _UP["last"] = up_name; _UP["last_model"] = sent_model or ""; _UP["last_ts"] = time.time()

        # 回写 (若之前在暂停期间已建立 chunked 流，此处不再重复发送头与包体)
        if not is_stream_early:
            try:
                self.send_response(status)
                self.send_header("Content-Type", rheaders.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(rbody)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(rbody)
            except Exception:
                pass

    def do_GET(self): self._do("GET")
    def do_POST(self): self._do("POST")
    def do_HEAD(self): self._do("HEAD")
    def do_DELETE(self): self._do("DELETE")


def read_ui_content():
    """读取 UI 页面: 优先读工作目录外部文件(方便调试热更), 回退读 PyInstaller 资源目录"""
    p = os.path.join(BASE, "ui.html")
    if os.path.isfile(p):
        try:
            with open(p, "rb") as f:
                return f.read()
        except Exception:
            pass
    if hasattr(sys, "_MEIPASS"):
        bp = os.path.join(sys._MEIPASS, "ui.html")
        if os.path.isfile(bp):
            try:
                with open(bp, "rb") as f:
                    return f.read()
            except Exception:
                pass
    raise FileNotFoundError("ui.html not found")


class UIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if self.path.split("?")[0].startswith(("/api/codex/login", "/api/cpa/", "/api/restart", "/api/relay/", "/api/antigravity/")):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def _local_ui_allowed(self, write=False, error_message="This operation requires the local relay UI"):
        try:
            host = self.headers.get("Host", "")
            target = urlsplit("http://" + host)
            allowed = (is_loopback_host(self.client_address[0])
                       and is_loopback_host(target.hostname)
                       and target.port == self.server.server_port
                       and not target.username and not target.password
                       and not target.path and not target.query and not target.fragment)
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                allowed = False
            if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                allowed = False
            if write:
                allowed = (allowed and origin == "http://" + host
                           and self.headers.get("X-CC-Relay-UI") == "1"
                           and self.headers.get_content_type() == "application/json")
        except ValueError:
            allowed = False
        if not allowed:
            self.close_connection = True
            # Drain only small, bounded bodies so Windows can deliver the 403
            # instead of resetting a connection with unread incoming data.
            if write and not self.headers.get("Transfer-Encoding"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 1024:
                        self.connection.settimeout(1)
                        self.rfile.read(length)
                except (ValueError, OSError):
                    pass
            self._json({"error": error_message}, 403)
        return allowed

    def _codex_login_allowed(self, write=False):
        return self._local_ui_allowed(write=write, error_message="Codex login requires the local relay UI")

    def _restart_allowed(self):
        return self._local_ui_allowed(write=True, error_message="Restart requires the local relay UI")

    def _calls(self):
        """最近 N 条调用摘要(抓包查看器): CC发了什么 / 我们选了谁转发"""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        n = int((q.get("n") or ["100"])[0])
        n = max(1, min(n, 1000))
        return _calls_snapshot(n)

    def _call_detail(self):
        """单条完整内容(headers/body/响应)"""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        idx = (q.get("idx") or [None])[0]
        if idx is None:
            return {"error": "missing idx"}
        r = _find_record_by_idx(idx)
        if r:
            out = dict(r)
            cache = r.get("cache") if r.get("cache") is not None else _resp_cache_usage(r.get("resp_body"))
            out["cache"] = cache
            duration = r.get("duration")
            out_tok = r.get("output_tokens")
            if out_tok is None and isinstance(cache, dict):
                out_tok = cache.get("output_tokens")
            speed = r.get("speed")
            if speed is None and duration and out_tok and duration > 0 and out_tok > 0:
                speed = round(out_tok / duration, 1)
            out["duration"] = duration
            out["output_tokens"] = out_tok or 0
            out["speed"] = speed
            return out
        return {"error": "not found"}

    def do_GET(self):
        if self.path.split("?")[0] == "/api/codex/login/status":
            if not self._codex_login_allowed():
                return
            try:
                self._json(get_codex_login().status())
            except Exception:
                self._json({"status": "error", "message": "无法读取 Codex 登录状态"}, 500)
        elif self.path.startswith("/api/ping"):
            try:
                conf = load_conf()
            except Exception:
                conf = {}
            self._json({
                "ok": True,
                "instance_id": _PROCESS_INSTANCE_ID,
                "traffic_paused": traffic_paused(conf),
                "paused_waiting_requests": _paused_waiting_count(),
            })
        elif self.path.split("?")[0] == "/api/cpa/version":
            try:
                self._json(_get_cpa_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.split("?")[0] == "/api/relay/version":
            try:
                self._json(_get_relay_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.split("?")[0] == "/api/antigravity/version":
            try:
                self._json(antigravity_updater.get_antigravity_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/api/config"):
            try:
                self._json(_config_public_view(load_conf()))
            except Exception as e:
                self._json({"error": "unable to read config: " + str(e)}, 500)
        elif self.path.startswith("/api/status"):
            self._json(stats_snapshot())
        elif self.path.startswith("/api/probe"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or ["antigravity"])[0]
            self._json(probe_upstream(load_conf(), name))
        elif self.path.startswith("/api/prompts"):
            self._json(_PROMPT_MGR.get_data())
        elif self.path.startswith("/api/calls"):
            self._json(self._calls())
        elif self.path.startswith("/api/call"):
            self._json(self._call_detail())
        elif self.path in ("/", "/index.html"):
            try:
                b = read_ui_content()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except Exception as e:
                self._json({"error": "ui missing: %s" % e}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p in ("/api/cpa/check", "/api/cpa/update"):
            if not self._local_ui_allowed(write=True, error_message="CPA update requires the local relay UI"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid CPA update body size")
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length or not isinstance(json.loads(raw or b"{}"), dict):
                    raise ValueError("invalid CPA update body")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid CPA update request"}, 400)
                return
            try:
                conf = load_conf()
                updater = _get_cpa_updater(conf)
                if p == "/api/cpa/check":
                    self._json(updater.request_check())
                else:
                    managed = os.path.abspath(os.path.join(BASE, "codex-proxy", "cli-proxy-api.exe"))
                    configured = os.path.abspath(conf.get("codex_exe") or managed)
                    if configured != managed or os.path.abspath(updater.exe_path) != managed:
                        raise RuntimeError("CPA executable is not managed by this project")
                    self._json(updater.request_update(_install_cpa_update))
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        elif p in ("/api/relay/check", "/api/relay/update"):
            if not self._local_ui_allowed(write=True, error_message="Relay update requires the local relay UI"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid relay update body size")
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length or not isinstance(json.loads(raw or b"{}"), dict):
                    raise ValueError("invalid relay update body")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid relay update request"}, 400)
                return
            try:
                conf = load_conf()
                updater = _get_relay_updater(conf)
                if p == "/api/relay/check":
                    self._json(updater.request_check(force=True))
                else:
                    self._json(updater.request_update(restart_callback=schedule_restart))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        elif p in ("/api/antigravity/check", "/api/antigravity/open"):
            if not self._local_ui_allowed(write=True, error_message="Antigravity operation requires the local relay UI"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid request body size")
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length or not isinstance(json.loads(raw or b"{}"), dict):
                    raise ValueError("invalid request body")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid request body"}, 400)
                return
            try:
                conf = load_conf()
                if p == "/api/antigravity/check":
                    updater = antigravity_updater.get_antigravity_updater(conf)
                    self._json(updater.request_check(force=True))
                else:
                    self._json(antigravity_open_ui(conf))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if p == "/api/codex/login":
            if not self._codex_login_allowed(write=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid login body size")
            except ValueError:
                self.close_connection = True
                self._json({"error": "Invalid login request size"}, 400)
                return
            self.connection.settimeout(10)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length or not isinstance(json.loads(raw or b"{}"), dict):
                    raise ValueError("invalid login body")
            except (ValueError, OSError):
                self.close_connection = True
                self._json({"error": "Invalid login request"}, 400)
                return
            try:
                with _CODEX_MAINTENANCE:
                    if _CODEX_MAINTENANCE_ACTIVE:
                        self._json({"error": "CPA is updating; retry login shortly"}, 409)
                    else:
                        self._json(get_codex_login().start())
            except Exception:
                self._json({"status": "error", "message": "无法启动 Codex 登录，请稍后重试"}, 500)
            return
        elif p == "/api/restart":
            if not self._restart_allowed():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid restart body size")
            except ValueError:
                self.close_connection = True
                self._json({"error": "Invalid restart request size"}, 400)
                return
            self.connection.settimeout(10)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length or not isinstance(json.loads(raw or b"{}"), dict):
                    raise ValueError("invalid restart body")
            except (ValueError, OSError):
                self.close_connection = True
                self._json({"error": "Invalid restart request body"}, 400)
                return
            self.close_connection = True
            self._json({"ok": True, "message": "cc-relay restarting..."})
            try:
                self.wfile.flush()
            except Exception:
                pass
            schedule_restart()
            return
        if p == "/api/upstream":
            if not self._local_ui_allowed(write=True, error_message="Upstream control requires the local relay UI"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid upstream body size")
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("invalid upstream body")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid upstream request"}, 400)
                return
            name = str(data.get("name") or "").strip().lower()
            act = str(data.get("action") or "").strip().lower()
            conf = load_conf()
            if name == "codex" and act in ("start", "stop"):
                with _CODEX_MAINTENANCE:
                    if _CODEX_MAINTENANCE_ACTIVE:
                        self._json({"error": "CPA is updating; retry shortly"}, 409)
                    else:
                        result = codex_start(conf) if act == "start" else codex_stop()
                        self._json({"result": result})
                return
            if name in ("gemini", "antigravity") and act == "start":
                self._json({"result": antigravity_start(conf), "probe": probe_upstream(conf, "antigravity")})
            elif name in ("gemini", "antigravity") and act == "stop":
                self._json({"result": antigravity_stop(), "probe": probe_upstream(conf, "antigravity")})
            elif name == "codex" and act == "start":
                self._json({"result": codex_start(conf)})
            elif name == "codex" and act == "stop":
                self._json({"result": codex_stop()})
            elif name == "voice" and act == "start":
                self._json({"result": voice_start(conf), "status": voice_status_dict(conf)})
            elif name == "voice" and act == "stop":
                self._json({"result": voice_stop(conf), "status": voice_status_dict(conf)})
            else:
                self._json({"error": "unsupported upstream action"}, 400)
            return
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln).decode() if ln else "{}"
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        p = self.path.split("?")[0]
        if p == "/api/config":
            try:
                with CONF_LOCK:
                    conf = load_conf()
                    view = _apply_config_update(conf, data)
                self._json({"ok": True, "config": view})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": "unable to save config: " + str(e)}, 500)
        elif p == "/api/traffic":
            if not isinstance(data, dict) or "paused" not in data or not isinstance(data["paused"], bool):
                self._json({"error": "invalid parameter: 'paused' must be a boolean"}, 400)
                return
            paused = data["paused"]
            changed = False
            try:
                with CONF_LOCK:
                    conf = load_conf()
                    old_state = traffic_paused(conf)
                    if old_state != paused:
                        conf["traffic_paused"] = paused
                        _save_conf(conf)
                        changed = True
                        if not paused:
                            _TRAFFIC_UNPAUSED_EVENT.set()
                        else:
                            _TRAFFIC_UNPAUSED_EVENT.clear()
                self._json({"ok": True, "traffic_paused": paused, "changed": changed, "paused_waiting_requests": _paused_waiting_count()})
            except Exception as e:
                self._json({"error": f"failed to update traffic state: {e}"}, 500)
            return
        elif p == "/api/route":
            route = str(data.get("route") or "").strip().lower()
            if route == "gemini":
                route = "antigravity"
            if route not in ("hybrid", "codex", "deepseek", "antigravity"):
                self._json({"error": "bad route"}, 400); return
            with CONF_LOCK:                      # 读-改-写整体串行, 免五档连点丢更新
                conf = load_conf()
                rt = conf.setdefault("router", {})
                rt["route"] = route
                # 各档指纹清理开关 (与路由档位无关, 任何模式下都可改; 按档合并更新)
                if "strip_cc_banner" in data:
                    cur = _strip_flags(rt)           # 先归一化, 兼容旧布尔配置
                    payload = data["strip_cc_banner"]
                    if isinstance(payload, bool):
                        cur["main"] = payload        # 旧客户端: 旧按钮本来就只代表主档
                    elif isinstance(payload, dict):
                        for k in TIER_KEYS:
                            if k not in payload:
                                continue
                            if not isinstance(payload[k], bool):
                                self._json({"error": "strip_cc_banner.%s must be boolean" % k}, 400)
                                return
                            cur[k] = payload[k]
                    else:
                        self._json({"error": "strip_cc_banner must be boolean or object"}, 400)
                        return
                    rt["strip_cc_banner"] = cur
                # 修改器模式: custom (默认自定义脚本) | builtin (内置清理) | original (原版纯透传)
                if "modifier_mode" in data:
                    m = str(data["modifier_mode"]).strip().lower()
                    if m in ("custom", "builtin", "original"):
                        rt["modifier_mode"] = m
                # 每档三态修改器 (UI 每档右上角三选框): original/builtin/custom, 按档合并更新
                # 选 custom 即启用该档自定义提示词; 选 original/builtin 则关闭该档自定义提示词
                if "tier_modifier" in data:
                    payload = data["tier_modifier"]
                    if not isinstance(payload, dict):
                        self._json({"error": "tier_modifier must be object"}, 400); return
                    cur = dict(rt.get("tier_modifier") or {})
                    for k in TIER_KEYS:
                        if k not in payload:
                            continue
                        v = str(payload[k]).strip().lower()
                        if v not in ("original", "builtin", "custom"):
                            self._json({"error": "tier_modifier.%s must be original|builtin|custom" % k}, 400)
                            return
                        cur[k] = v
                        _PROMPT_MGR.update_tier(k, enabled=(v == "custom"))
                    rt["tier_modifier"] = cur
                # Gemini(Antigravity) 全量二选: original | builtin (不支持替换协议头/自定义提示词)
                if "gemini_modifier" in data:
                    g = str(data["gemini_modifier"]).strip().lower()
                    if g in ("original", "builtin"):
                        rt["gemini_modifier"] = g
                # 推理强度 effort 为全局设置, 任何路由下都可修改 (档位见 EFFORT_VALUES)
                if data.get("effort") is not None:
                    v = str(data.get("effort") or "").strip()
                    if v == "instant":
                        v = "low"
                    if v in EFFORT_VALUES:
                        rt["effort"] = v
                    else:
                        rt.pop("effort", None)
                # 每档独立推理强度 tier_efforts (main/opus/sonnet/fast/agent), 合并更新
                if data.get("tier_efforts") is not None:
                    t = data.get("tier_efforts") or {}
                    cur = dict(rt.get("tier_efforts") or {})
                    for k in ("main", "opus", "sonnet", "fast", "agent"):
                        v = str((t or {}).get(k) or "").strip()
                        if v == "instant":
                            v = "low"
                        if v in EFFORT_VALUES:
                            cur[k] = v
                    if cur:
                        rt["tier_efforts"] = cur
                    else:
                        rt.pop("tier_efforts", None)
                if route == "hybrid":
                    # hybrid 五档: tiers.main/opus/sonnet/fast/agent (各自模型, 上游随模型归属决定)
                    if data.get("tiers") is not None:
                        t = data.get("tiers") or {}
                        cur = dict(rt.get("tiers") or {})
                        for k in ("main", "opus", "sonnet", "fast", "agent"):
                            v = str((t or {}).get(k) or "").strip()
                            if v:
                                p = provider_for_model(v, conf)
                                if p is None and not (v.startswith(("gpt-", "deepseek-", "gemini-"))):
                                    self._json({"error": "unknown model for tier %s: %s" % (k, v)}, 400)
                                    return
                                cur[k] = v
                        if cur:
                            rt["tiers"] = cur
                        else:
                            rt.pop("tiers", None)
                    # 兼容旧字段
                    if data.get("codex_model") is not None:
                        v = str(data.get("codex_model") or "").strip()
                        if v: rt["hybrid_codex_model"] = v
                        else: rt.pop("hybrid_codex_model", None)
                    if data.get("deepseek_model") is not None:
                        v = str(data.get("deepseek_model") or "").strip()
                        if v: rt["hybrid_deepseek_model"] = v
                        else: rt.pop("hybrid_deepseek_model", None)
                    if data.get("gemini_model") is not None or data.get("antigravity_model") is not None:
                        v = str(data.get("gemini_model", data.get("antigravity_model")) or "").strip()
                        if v: rt["hybrid_antigravity_model"] = v
                        else: rt.pop("hybrid_antigravity_model", None)
                    rt.pop("model", None)
                else:
                    m = (data.get("model") or "").strip()
                    if route == "codex" and m and provider_for_model(m, conf) != "codex":
                        self._json({"error": "codex route requires a gpt model"}, 400); return
                    if route == "antigravity" and m and provider_for_model(m, conf) != "antigravity":
                        self._json({"error": "Gemini route requires a gemini model"}, 400); return
                    if not m:
                        rt.pop("model", None)
                    else:
                        rt["model"] = m
                _save_conf(conf)
                self._json({"ok": True, "route": route, "model": rt.get("model", "")})
        elif p == "/api/proxy":
            if not self._local_ui_allowed(write=True, error_message="Proxy control requires the local relay UI"):
                return
            act = data.get("action")
            if act not in ("start", "stop"):
                self._json({"error": "bad action"}, 400)
            else:
                with _CODEX_MAINTENANCE:
                    if _CODEX_MAINTENANCE_ACTIVE:
                        self._json({"error": "CPA is updating; retry shortly"}, 409)
                    else:
                        result = codex_start(load_conf()) if act == "start" else codex_stop()
                        self._json({"result": result})
        elif p == "/api/reset":
            err = None
            for attempt in range(5):
                try:
                    with LOCK:
                        with open(RECORDS, "w", encoding="utf-8") as f:
                            pass
                        _IDX[0] = 0
                        with _STATS_LOCK:
                            _STATS_CACHE.update({"fingerprint": None, "rows": None, "total": 0})
                        with _CALLS_LOCK:
                            _CALLS_CACHE.clear()
                    err = None
                    break
                except Exception as e:
                    err = e
                    time.sleep(0.05)
            if err:
                self._json({"error": f"清零记录失败: {err}"}, 500)
            else:
                self._json({"ok": True})
        elif p == "/api/prompts":
            tier = data.get("tier")
            custom_txt = data.get("custom")
            enabled = data.get("enabled")
            if tier in TIER_KEYS:
                _PROMPT_MGR.update_tier(tier, custom_text=custom_txt, enabled=enabled)
                self._json({"ok": True, "tier": tier})
            else:
                self._json({"error": "invalid tier"}, 400)
        elif p == "/api/shutdown":
            self._json({"ok": True, "message": "cc-relay shutting down..."})
            def _kill():
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_kill, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204); self.end_headers()


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Use Windows exclusive binding so a second relay instance cannot share a port."""
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def serve_ui(conf):
    host = conf.get("listen_host", "127.0.0.1")
    if not is_loopback_host(host):
        raise ValueError(f"Refusing to bind UI to non-loopback host '{host}'.")
    port = conf.get("ui_port", 8610)
    srv = ExclusiveThreadingHTTPServer((host, port), UIHandler)
    print(f"cc-relay UI on http://{host}:{port}")
    srv.serve_forever()


def serve(a):
    conf = load_conf()
    _sync_traffic_unpaused_event(conf)
    _get_cpa_updater(conf).start(auto_update=lambda: _cpa_auto_update_enabled() and _cpa_auto_update_idle(),
                                 installer=_install_cpa_update)
    ru_conf = (conf.get("tools") or {}).get("relay_update") or {}
    if ru_conf.get("auto_check", True):
        _get_relay_updater(conf).start(auto_check=True)
    ag_conf = (conf.get("tools") or {}).get("antigravity") or {}
    if ag_conf.get("auto_check", True):
        antigravity_updater.get_antigravity_updater(conf).start(auto_check=True)
    host = conf.get("listen_host", "127.0.0.1")
    if not is_loopback_host(host):
        raise ValueError(f"Refusing to bind relay to non-loopback host '{host}'.")
    port = conf.get("listen_port", 8400)
    srv = ExclusiveThreadingHTTPServer((host, port), Relay)
    srv.reload_conf = lambda: load_conf()
    # 同时起 UI (可选)
    if not a.no_ui:
        t = threading.Thread(target=serve_ui, args=(conf,), daemon=True)
        t.start()
    srv.conf = conf
    print(f"cc-relay v2 on http://{host}:{port}")
    for n, u in (conf.get("upstreams") or {}).items():
        print(f"  [{n}] -> {u.get('base')}")
    print(f"  records: {RECORDS}")
    fake_key = conf.get("fake_api_key")
    masked_key = _mask_config_key(fake_key) if fake_key else "<none>"
    print(f"  CC: ANTHROPIC_BASE_URL=http://{host}:{port}  ANTHROPIC_AUTH_TOKEN={masked_key}")
    srv.serve_forever()


def stats(a):
    recs = read_records()
    print(f"总记录: {len(recs)}")
    by_route = {}
    by_model = {}
    for r in recs:
        by_route[r.get("route", "?")] = by_route.get(r.get("route", "?"), 0) + 1
        by_model[r.get("orig_model", "?")] = by_model.get(r.get("orig_model", "?"), 0) + 1
    print("路由分布:", by_route)
    print("原始 model 分布:", by_model)
    print("\n最近 12 条:")
    for r in recs[-12:]:
        print(f"  #{r.get('idx')} {r.get('ts')} {r.get('orig_model')} -> {r.get('sent_model')} "
              f"[{r.get('route')}] {r.get('path')[:24]} {r.get('resp_status')}")


def last(a):
    n = a.n or 5
    for r in read_records()[-n:]:
        b = r.get("body") or {}
        print("=" * 70)
        print(f"#{r.get('idx')} {r.get('ts')} {r.get('method')} {r.get('path')} -> {r.get('resp_status')}")
        print(f"route={r.get('route')} ({r.get('route_reason')}) | model {r.get('orig_model')} -> {r.get('sent_model')}")
        h = r.get("headers", {})
        print("agent-id:", h.get("x-claude-code-agent-id"), "| session:", (h.get("X-Claude-Code-Session-Id") or "")[:8])
        print("msgs:", len(b.get("messages") or []), "tools:", len(b.get("tools") or []))


def _redact_record_for_cli(rec):
    if not isinstance(rec, dict):
        return rec
    r = json.loads(json.dumps(rec, ensure_ascii=False))
    headers = r.get("headers")
    if isinstance(headers, dict):
        for k in list(headers.keys()):
            if k.lower() in ("authorization", "x-api-key"):
                headers[k] = "<redacted>"
    return r


def dump(a):
    recs = read_records()
    if a.which == "all":
        for r in recs[-20:]:
            print(json.dumps(_redact_record_for_cli(r), ensure_ascii=False, indent=1)[:4000])
    else:
        for r in recs:
            if str(r.get("idx")) == a.which:
                print(json.dumps(_redact_record_for_cli(r), ensure_ascii=False, indent=1)); return
        print("未找到 #%s" % a.which)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve").add_argument("--no-ui", action="store_true")
    sub.add_parser("ui")
    sub.add_parser("stats")
    l = sub.add_parser("last"); l.add_argument("n", type=int, nargs="?", default=5)
    d = sub.add_parser("dump"); d.add_argument("which")
    sub.add_parser("startproxy")
    sub.add_parser("stopproxy")
    sub.add_parser("proxycheck")
    a = ap.parse_args()
    os.makedirs(BASE, exist_ok=True)
    if a.cmd == "serve": serve(a)
    elif a.cmd == "ui": serve_ui(load_conf())
    elif a.cmd == "stats": stats(a)
    elif a.cmd == "last": last(a)
    elif a.cmd == "dump": dump(a)
    elif a.cmd == "startproxy": print(codex_start(load_conf()))
    elif a.cmd == "stopproxy": print(codex_stop())
    elif a.cmd == "proxycheck": print("up" if codex_up() else "down")
    else: ap.print_help()
