# -*- coding: utf-8 -*-
"""cc-relay 8610 端口 UIHandler 控制面服务器与全量 REST 控制 API"""
import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

try:
    from relay.upstreams import antigravity_updater
except ImportError:
    import antigravity_updater
from relay.audit import (
    _CALLS_CACHE,
    _CALLS_LOCK,
    _STATS_CACHE,
    _STATS_LOCK,
    _calls_snapshot,
    _find_record_by_idx,
    _resp_cache_usage,
    stats_snapshot,
)
from relay.config import (
    CONF_LOCK,
    _apply_config_update,
    _config_public_view,
    _save_conf,
    get_codex_login,
    is_loopback_host,
    load_conf,
)
from relay.constants import (
    BASE,
    LOCK,
    VOICE_PID_FILE,
    _IDX,
    _PROCESS_INSTANCE_ID,
    get_records_path,
)
from relay.modifier import _PROMPT_MGR
from relay.router import (
    EFFORT_VALUES,
    TIER_KEYS,
    _strip_flags,
    provider_for_model,
)
from relay.traffic import (
    _CODEX_MAINTENANCE,
    _CODEX_MAINTENANCE_ACTIVE,
    _TRAFFIC_UNPAUSED_EVENT,
    _paused_waiting_count,
    traffic_paused,
)
from relay.upstreams import (
    _get_cpa_updater,
    _get_relay_updater,
    _install_cpa_update,
    antigravity_open_ui,
    antigravity_start,
    antigravity_stop,
    codex_start,
    codex_stop,
    probe_upstream,
)
from relay.voice_bridge import (
    _VOICE_DOWNLOAD_LOCK,
    _VOICE_DOWNLOAD_STATE,
    _VOICE_PROCESS,
    _VOICE_PROCESS_LOCK,
    _probe_voice_service,
    _write_voice_pid,
    perform_voice_correction,
    start_voice_model_download,
    voice_download_status,
    voice_start,
    voice_status_dict,
    voice_stop,
    voice_up,
)
try:
    from relay.voice_context import (
        VOICE_CONTEXTS,
        is_foreground_target,
        is_valid_window_target,
        tcp_client_pid,
    )
except ImportError:
    from voice_context import (
        VOICE_CONTEXTS,
        is_foreground_target,
        is_valid_window_target,
        tcp_client_pid,
    )

_RESTART_LOCK = threading.Lock()
_RESTART_QUEUED = False


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
        f"Wait-Process -Id {pid} -Timeout 15 -ErrorAction SilentlyContinue; "
        f"Start-Sleep -Milliseconds 1200; "
        f"if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ "
        f"Start-Process -WindowStyle Hidden -FilePath {exe_q} {arg_part} -WorkingDirectory {cwd_q} "
        f"}}"
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
    env["CC_RELAY_RESTART"] = "1"

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
    try:
        time.sleep(0.4)
        if _spawn_restart_supervisor():
            os._exit(0)
    except Exception as e:
        print(f"[ERROR] Restart worker error: {e}", file=sys.stderr)
    finally:
        mod = sys.modules.get("cc_relay")
        lock = getattr(mod, "_RESTART_LOCK", _RESTART_LOCK) if mod else _RESTART_LOCK
        with lock:
            _RESTART_QUEUED = False
            if mod and hasattr(mod, "_RESTART_QUEUED"):
                setattr(mod, "_RESTART_QUEUED", False)


def schedule_restart():
    """Atomically schedule a background restart if not already queued. Returns True if scheduled."""
    global _RESTART_QUEUED
    mod = sys.modules.get("cc_relay")
    lock = getattr(mod, "_RESTART_LOCK", _RESTART_LOCK) if mod else _RESTART_LOCK
    with lock:
        queued = getattr(mod, "_RESTART_QUEUED", _RESTART_QUEUED) if mod else _RESTART_QUEUED
        if queued:
            return True
        _RESTART_QUEUED = True
        if mod and hasattr(mod, "_RESTART_QUEUED"):
            setattr(mod, "_RESTART_QUEUED", True)
        threading.Thread(target=_restart_worker, daemon=True).start()
        return True


def _dispatch_schedule_restart():
    """支持测试对 cc_relay.schedule_restart 的 mock.patch"""
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, "schedule_restart") and mod.schedule_restart != schedule_restart:
        return mod.schedule_restart()
    return schedule_restart()


def _dispatch_fn(name, default_fn, *args, **kwargs):
    """支持测试对 cc_relay 下函数的 mock.patch"""
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, name) and getattr(mod, name) != default_fn:
        return getattr(mod, name)(*args, **kwargs)
    return default_fn(*args, **kwargs)


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

            def _is_loopback_origin(orig):
                if not orig:
                    return False
                try:
                    p = urlsplit(orig)
                    return (p.scheme == "http"
                            and is_loopback_host(p.hostname)
                            and p.port == self.server.server_port
                            and not p.username and not p.password
                            and (not p.path or p.path == "/")
                            and not p.query and not p.fragment)
                except Exception:
                    return False

            if origin is not None and origin != "http://" + host and not _is_loopback_origin(origin):
                allowed = False
            if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                allowed = False
            if write:
                origin_ok = (origin == "http://" + host) or _is_loopback_origin(origin)
                allowed = (allowed and origin_ok
                           and self.headers.get("X-CC-Relay-UI") == "1"
                           and self.headers.get_content_type() == "application/json")
        except ValueError:
            allowed = False
        if not allowed:
            self.close_connection = True
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

    def _voice_daemon_allowed(self):
        try:
            if not is_loopback_host(self.client_address[0]):
                return False
            client_pid = tcp_client_pid(self.connection, self.client_address)
            if not client_pid:
                return False

            try:
                with open(VOICE_PID_FILE, encoding="utf-8") as f:
                    expected_pid = int(json.load(f).get("pid") or 0)
                if expected_pid and client_pid == expected_pid:
                    return True
            except Exception:
                pass

            with _VOICE_PROCESS_LOCK:
                if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None and _VOICE_PROCESS.pid == client_pid:
                    return True

            probe = _probe_voice_service()
            if isinstance(probe, dict) and probe.get("service") == "voice":
                live_pid = int(probe.get("pid") or 0)
                if live_pid and client_pid == live_pid:
                    _write_voice_pid(live_pid)
                    return True

            return False
        except Exception:
            return False

    def _restart_allowed(self):
        return self._local_ui_allowed(write=True, error_message="Restart requires the local relay UI")

    def _calls(self):
        q = parse_qs(urlparse(self.path).query)
        n = int((q.get("n") or ["100"])[0])
        n = max(1, min(n, 1000))
        return _calls_snapshot(n)

    def _call_detail(self):
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
        p = self.path.split("?")[0]
        if p == "/api/codex/login/status":
            if not self._codex_login_allowed():
                return
            try:
                self._json(_dispatch_fn("get_codex_login", get_codex_login).status())
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
        elif p == "/api/cpa/version":
            try:
                self._json(_get_cpa_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p == "/api/relay/version":
            try:
                self._json(_get_relay_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p == "/api/antigravity/version":
            try:
                self._json(antigravity_updater.get_antigravity_updater(load_conf()).status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p == "/api/voice/download_status":
            self._json(voice_download_status())
        elif self.path.startswith("/api/config"):
            request_started = time.perf_counter()
            try:
                self._json(_config_public_view(load_conf()))
            except Exception as e:
                self._json({"error": "unable to read config: " + str(e)}, 500)
            finally:
                elapsed_ms = (time.perf_counter() - request_started) * 1000
                if elapsed_ms >= 750:
                    print(f"[ui-status] /api/config slow total_ms={elapsed_ms:.1f}", file=sys.stderr, flush=True)
        elif self.path.startswith("/api/status"):
            self._json(stats_snapshot())
        elif self.path.startswith("/api/probe"):
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
        if p == "/api/voice/correct":
            if not self._local_ui_allowed(write=True, error_message="Voice correction requires the local relay"):
                return
            if not self._voice_daemon_allowed():
                self._json({"error": "Voice correction is available only to the managed voice service"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid voice correction request size")
                self.connection.settimeout(3)
                raw = self.rfile.read(length)
                data = json.loads(raw)
                if len(raw) != length or not isinstance(data, dict):
                    raise ValueError("invalid voice correction request")
                target_pid = int(data.get("pid") or 0)
                target_hwnd = int(data.get("hwnd") or 0)
                session_id = str(data.get("session_id") or "")
                revision = int(data.get("revision") or 0)
                model = str(data.get("model") or "")
                transcript = str(data.get("transcript") or "")
                context = str(data.get("context") or "")
                timeout = float(data.get("timeout") or 8)
                if (target_pid <= 0 or target_hwnd <= 0 or not session_id or revision <= 0
                        or not transcript or not context or len(context) > 12000):
                    raise ValueError("invalid voice correction fields")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid voice correction request"}, 400)
                return
            if not is_foreground_target(target_hwnd, target_pid):
                self._json({"ok": False, "reason": "target_changed"})
                return
            current = VOICE_CONTEXTS.snapshot_for_target(target_pid)
            if (not current or current.session_id != session_id or current.revision != revision):
                self._json({"ok": False, "reason": "context_changed"})
                return
            conf = load_conf()
            v_conf = conf.get("tools", {}).get("voice", {})
            if not v_conf.get("semantic_correction_enabled"):
                self._json({"ok": False, "reason": "disabled"})
                return
            if traffic_paused(conf):
                self._json({"ok": False, "reason": "traffic_paused"})
                return
            chosen_model = str(v_conf.get("semantic_correction_model") or "").strip() or model
            result = perform_voice_correction(conf, chosen_model, context, transcript, timeout=timeout)
            self._json(result)
            return
        if p == "/api/voice/context":
            if not self._local_ui_allowed(write=True, error_message="Voice context requires the local relay"):
                return
            if not self._voice_daemon_allowed():
                self._json({"error": "Voice context is available only to the managed voice service"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid voice context request size")
                self.connection.settimeout(2)
                raw = self.rfile.read(length)
                data = json.loads(raw)
                if len(raw) != length or not isinstance(data, dict):
                    raise ValueError("invalid voice context request")
                target_pid = int(data.get("pid") or 0)
                target_hwnd = int(data.get("hwnd") or 0)
                target_title = str(data.get("title") or "")
                max_chars = int(data.get("max_chars") or 12000)
                if target_pid <= 0 or target_hwnd <= 0 or not (512 <= max_chars <= 12000):
                    raise ValueError("invalid voice target")
            except (ValueError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self._json({"error": "Invalid voice context request"}, 400)
                return
            if not is_valid_window_target(target_hwnd, target_pid):
                self._json({"available": False, "reason": "target_invalid"})
                return
            snapshot = VOICE_CONTEXTS.snapshot_for_target(target_pid, target_title=target_title, max_chars=max_chars)
            if not snapshot:
                self._json({"available": False, "reason": "no_unique_session"})
                return
            self._json({
                "available": True,
                "session_id": snapshot.session_id,
                "model": snapshot.model,
                "context": snapshot.context,
                "revision": snapshot.revision,
            })
            return
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
                    self._json(updater.request_update(restart_callback=_dispatch_schedule_restart))
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
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 65536:
                        self.connection.settimeout(1)
                        self.rfile.read(length)
                except Exception:
                    pass
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
                        self._json(_dispatch_fn("get_codex_login", get_codex_login).start())
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
            _dispatch_schedule_restart()
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
                        result = _dispatch_fn("codex_start", codex_start, conf) if act == "start" else _dispatch_fn("codex_stop", codex_stop)
                        self._json({"result": result})
                return
            if name in ("gemini", "antigravity") and act == "start":
                self._json({"result": _dispatch_fn("antigravity_start", antigravity_start, conf), "probe": _dispatch_fn("probe_upstream", probe_upstream, conf, "antigravity")})
            elif name in ("gemini", "antigravity") and act == "stop":
                self._json({"result": _dispatch_fn("antigravity_stop", antigravity_stop), "probe": _dispatch_fn("probe_upstream", probe_upstream, conf, "antigravity")})
            elif name == "codex" and act == "start":
                self._json({"result": _dispatch_fn("codex_start", codex_start, conf)})
            elif name == "codex" and act == "stop":
                self._json({"result": _dispatch_fn("codex_stop", codex_stop)})
            elif name == "voice" and act == "start":
                self._json({"result": _dispatch_fn("voice_start", voice_start, conf), "status": _dispatch_fn("voice_status_dict", voice_status_dict, conf)})
            elif name == "voice" and act == "stop":
                self._json({"result": _dispatch_fn("voice_stop", voice_stop, conf), "status": _dispatch_fn("voice_status_dict", voice_status_dict, conf)})
            elif name == "voice" and act == "download_model":
                self._json(start_voice_model_download(conf, model=str(data.get("model") or "qwen").strip().lower()))
            elif name == "voice" and act == "delete_model":
                if voice_up(conf):
                    v_conf = conf.get("tools", {}).get("voice", {})
                    active_eng = (v_conf.get("engine") or "sensevoice_offline").lower()
                    if "qwen" in active_eng:
                        self._json({"ok": False, "error": "语音伴侣正在运行 Qwen 模型，请先停止服务再执行删除"}, 409)
                        return
                try:
                    from tools.voice_input.model_download import delete_qwen_model
                    ok, msg = delete_qwen_model()
                    with _VOICE_DOWNLOAD_LOCK:
                        _VOICE_DOWNLOAD_STATE["status"] = "idle"
                        _VOICE_DOWNLOAD_STATE["progress"] = 0
                        _VOICE_DOWNLOAD_STATE["message"] = "模型已被删除"
                        _VOICE_DOWNLOAD_STATE["error"] = None
                    self._json({"ok": ok, "status": "deleted", "message": msg})
                except Exception as ex:
                    self._json({"ok": False, "error": str(ex)}, 500)
                return
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
            allowed_routes = {"hybrid", "codex", "deepseek", "antigravity"}
            with CONF_LOCK:
                conf = load_conf()
                allowed_routes.update((conf.get("custom_providers") or {}).keys())
                if route not in allowed_routes:
                    self._json({"error": "bad route"}, 400)
                    return
                rt = conf.setdefault("router", {})
                rt["route"] = route
                if "strip_cc_banner" in data:
                    cur = _strip_flags(rt)
                    payload = data["strip_cc_banner"]
                    if isinstance(payload, bool):
                        cur["main"] = payload
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
                if "modifier_mode" in data:
                    m = str(data["modifier_mode"]).strip().lower()
                    if m in ("custom", "builtin", "original"):
                        rt["modifier_mode"] = m
                if "tier_modifier" in data:
                    payload = data["tier_modifier"]
                    if not isinstance(payload, dict):
                        self._json({"error": "tier_modifier must be object"}, 400)
                        return
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
                if "gemini_modifier" in data:
                    g = str(data["gemini_modifier"]).strip().lower()
                    if g in ("original", "builtin"):
                        rt["gemini_modifier"] = g
                if data.get("effort") is not None:
                    v = str(data.get("effort") or "").strip()
                    if v == "instant":
                        v = "low"
                    if v in EFFORT_VALUES:
                        rt["effort"] = v
                    else:
                        rt.pop("effort", None)
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
                    if data.get("tiers") is not None:
                        t = data.get("tiers") or {}
                        cur = dict(rt.get("tiers") or {})
                        for k in ("main", "opus", "sonnet", "fast", "agent"):
                            v = str((t or {}).get(k) or "").strip()
                            if v:
                                p_model = provider_for_model(v, conf)
                                if p_model is None and not (v.startswith(("gpt-", "deepseek-", "gemini-"))):
                                    self._json({"error": "unknown model for tier %s: %s" % (k, v)}, 400)
                                    return
                                cur[k] = v
                        if cur:
                            rt["tiers"] = cur
                        else:
                            rt.pop("tiers", None)
                    if data.get("codex_model") is not None:
                        v = str(data.get("codex_model") or "").strip()
                        if v:
                            rt["hybrid_codex_model"] = v
                        else:
                            rt.pop("hybrid_codex_model", None)
                    if data.get("deepseek_model") is not None:
                        v = str(data.get("deepseek_model") or "").strip()
                        if v:
                            rt["hybrid_deepseek_model"] = v
                        else:
                            rt.pop("hybrid_deepseek_model", None)
                    if data.get("gemini_model") is not None or data.get("antigravity_model") is not None:
                        v = str(data.get("gemini_model", data.get("antigravity_model")) or "").strip()
                        if v:
                            rt["hybrid_antigravity_model"] = v
                        else:
                            rt.pop("hybrid_antigravity_model", None)
                    rt.pop("model", None)
                else:
                    m = (data.get("model") or "").strip()
                    if route == "codex" and m and provider_for_model(m, conf) != "codex":
                        self._json({"error": "codex route requires a gpt model"}, 400)
                        return
                    if route == "antigravity" and m and provider_for_model(m, conf) != "antigravity":
                        self._json({"error": "Gemini route requires a gemini model"}, 400)
                        return
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
                        result = _dispatch_fn("codex_start", codex_start, load_conf()) if act == "start" else _dispatch_fn("codex_stop", codex_stop)
                        self._json({"result": result})
        elif p == "/api/reset":
            err = None
            records_file = get_records_path()
            for attempt in range(5):
                try:
                    with LOCK:
                        with open(records_file, "w", encoding="utf-8") as f:
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
        elif p == "/api/voice/download_model":
            if not self._local_ui_allowed(write=True, error_message="Voice download requires local relay UI"):
                return
            conf = load_conf()
            model_target = str(data.get("model") or "qwen").strip().lower()
            self._json(start_voice_model_download(conf, model=model_target))
            return
        elif p == "/api/voice/delete_model":
            if not self._local_ui_allowed(write=True, error_message="Voice model deletion requires local relay UI"):
                return
            conf = load_conf()
            if voice_up(conf):
                v_conf = conf.get("tools", {}).get("voice", {})
                active_eng = (v_conf.get("engine") or "sensevoice_offline").lower()
                if "qwen" in active_eng:
                    self._json({"ok": False, "error": "语音伴侣正在运行 Qwen 模型，请先在控制台停止语音伴侣服务再执行删除"}, 409)
                    return
            try:
                from tools.voice_input.model_download import delete_qwen_model
                ok, msg = delete_qwen_model()
                with _VOICE_DOWNLOAD_LOCK:
                    _VOICE_DOWNLOAD_STATE["status"] = "idle"
                    _VOICE_DOWNLOAD_STATE["progress"] = 0
                    _VOICE_DOWNLOAD_STATE["message"] = "模型已被删除"
                    _VOICE_DOWNLOAD_STATE["error"] = None
                self._json({"ok": ok, "status": "deleted", "message": msg})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 500)
            return
        elif p == "/api/shutdown":
            self._json({"ok": True, "message": "cc-relay shutting down..."})
            def _kill():
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_kill, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Use Windows exclusive binding so a second relay instance cannot share a port."""
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def create_exclusive_server(server_address, RequestHandlerClass, max_wait_sec=15.0):
    """Attempt to bind ExclusiveThreadingHTTPServer with bounded retry on address-in-use errors.

    Protects against Windows SO_EXCLUSIVEADDRUSE / WinError 10048 during fast process restart.
    """
    deadline = time.monotonic() + max(0.0, float(max_wait_sec))
    delay = 0.15
    while True:
        try:
            return ExclusiveThreadingHTTPServer(server_address, RequestHandlerClass)
        except OSError as e:
            is_addr_in_use = (
                getattr(e, "winerror", None) == 10048
                or getattr(e, "errno", None) in (getattr(errno, "EADDRINUSE", 98), 10048)
                or "10048" in str(e)
                or "Address already in use" in str(e)
            )
            if not is_addr_in_use or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def serve_ui(conf, max_wait_sec=15.0):
    host = conf.get("listen_host", "127.0.0.1")
    if not is_loopback_host(host):
        raise ValueError(f"Refusing to bind UI to non-loopback host '{host}'.")
    port = conf.get("ui_port", 8610)
    try:
        srv = create_exclusive_server((host, port), UIHandler, max_wait_sec=max_wait_sec)
    except Exception as e:
        print(f"[ERROR] Failed to bind UI server on {host}:{port}: {e}", file=sys.stderr)
        raise
    srv.serve_forever()
