# -*- coding: utf-8 -*-
"""cc-relay 上游进程管理、健康检测、版本探测与更新协调"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from relay.upstreams import antigravity_updater
    from relay.upstreams.cpa_updater import CPAUpdater
    from relay.upstreams import relay_updater
    from relay.antigravity_paths import find_antigravity_exe
except ImportError:
    import antigravity_updater
    from cpa_updater import CPAUpdater
    import relay_updater
    from antigravity_paths import find_antigravity_exe
from relay.config import get_codex_login, load_conf
from relay.constants import BASE, UPSTREAM_DEGRADED_TTL, _tcp
from relay.router import CODEX_MODELS, DEEPSEEK_MODELS, GEMINI_MODELS, _key_for, _upstream_conf
from relay.traffic import (
    _CODEX_ACTIVE_REQUESTS,
    _CODEX_LAST_REQUEST_TS,
    _CODEX_MAINTENANCE,
    _CODEX_MAINTENANCE_ACTIVE,
)

_CPA_UPDATER = None
_CPA_UPDATER_LOCK = threading.Lock()


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


def codex_up(conf=None):
    """CLIProxyAPI health check; port derives from configured base when possible."""
    mod = sys.modules.get("cc_relay")
    _tcp_fn = getattr(mod, "_tcp", _tcp) if mod else _tcp
    try:
        base = (_upstream_conf(conf or load_conf(), "codex").get("base") or "http://127.0.0.1:8317")
        u = urllib.parse.urlparse(base)
        return _tcp_fn(u.port or (443 if u.scheme == "https" else 80), u.hostname or "127.0.0.1")
    except Exception:
        return _tcp_fn(8317)


def antigravity_up(conf=None):
    """Gemini sidecar health check; port derives from configured base when possible."""
    mod = sys.modules.get("cc_relay")
    _tcp_fn = getattr(mod, "_tcp", _tcp) if mod else _tcp
    try:
        base = (_upstream_conf(conf or load_conf(), "antigravity").get("base") or "http://127.0.0.1:8045")
        u = urllib.parse.urlparse(base)
        return _tcp_fn(u.port or (443 if u.scheme == "https" else 80), u.hostname or "127.0.0.1")
    except Exception:
        return _tcp_fn(8045)


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


def record_upstream_health(name, ok, reason=""):
    """统一记录上游运行健康状态；gemini 自动映射为 antigravity。"""
    target = "antigravity" if name in ("gemini", "antigravity") else name
    mod = sys.modules.get("cc_relay")
    health_dict = getattr(mod, "_UPSTREAM_HEALTH", _UPSTREAM_HEALTH) if mod else _UPSTREAM_HEALTH
    health_lock = getattr(mod, "_UPSTREAM_HEALTH_LOCK", _UPSTREAM_HEALTH_LOCK) if mod else _UPSTREAM_HEALTH_LOCK
    with health_lock:
        if ok:
            health_dict[target] = {"api_ok": True, "reason": "", "ts": time.time()}
        else:
            health_dict[target] = {"api_ok": False, "reason": reason or "API报错", "ts": time.time()}


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
    mod = sys.modules.get("cc_relay")
    target_lock = getattr(mod, "_CODEX_PROXY_TARGET_LOCK", _CODEX_PROXY_TARGET_LOCK) if mod else _CODEX_PROXY_TARGET_LOCK
    target_cache = getattr(mod, "_CODEX_PROXY_TARGET_CACHE", _CODEX_PROXY_TARGET_CACHE) if mod else _CODEX_PROXY_TARGET_CACHE
    with target_lock:
        if now - target_cache["ts"] < 10.0:
            return target_cache["target"]
    target = None
    try:
        raw = _read_codex_outbound_proxy(conf)
        if raw and raw.lower() not in ("direct", "none"):
            u = urllib.parse.urlparse(raw)
            if u.hostname:
                port = u.port or (443 if u.scheme == "https" else 80)
                target = (u.hostname, port)
    except Exception:
        pass
    with target_lock:
        target_cache.update({"ts": now, "target": target})
    return target


def _check_codex_proxy_reachable(conf=None):
    now = time.time()
    mod = sys.modules.get("cc_relay")
    check_lock = getattr(mod, "_CODEX_PROXY_CHECK_LOCK", _CODEX_PROXY_CHECK_LOCK) if mod else _CODEX_PROXY_CHECK_LOCK
    check_cache = getattr(mod, "_CODEX_PROXY_CHECK_CACHE", _CODEX_PROXY_CHECK_CACHE) if mod else _CODEX_PROXY_CHECK_CACHE
    with check_lock:
        if now - check_cache["ts"] < 3.0:
            return check_cache["ok"], check_cache["port"]
    target_fn = getattr(mod, "_get_codex_proxy_target", _get_codex_proxy_target) if mod else _get_codex_proxy_target
    target = target_fn(conf)
    tcp_fn = getattr(mod, "_tcp", _tcp) if mod else _tcp
    if not target:
        ok, port = True, None
    else:
        host, port = target
        ok = tcp_fn(port, host, t=0.15)
    with check_lock:
        check_cache.update({"ts": now, "ok": ok, "port": port})
    return ok, port


def get_upstream_health(conf, name, running=None):
    """三态健康检测:
    online: 运行中且 API 正常
    degraded: 运行中但 API 不可用 (如外发代理 7897 未开、认证失败、近期请求报 5xx)
    offline: 进程未运行或端口未监听
    """
    mod = sys.modules.get("cc_relay")
    codex_up_fn = getattr(mod, "codex_up", codex_up) if mod else codex_up
    antigravity_up_fn = getattr(mod, "antigravity_up", antigravity_up) if mod else antigravity_up
    check_proxy_fn = getattr(mod, "_check_codex_proxy_reachable", _check_codex_proxy_reachable) if mod else _check_codex_proxy_reachable
    health_dict = getattr(mod, "_UPSTREAM_HEALTH", _UPSTREAM_HEALTH) if mod else _UPSTREAM_HEALTH
    health_lock = getattr(mod, "_UPSTREAM_HEALTH_LOCK", _UPSTREAM_HEALTH_LOCK) if mod else _UPSTREAM_HEALTH_LOCK

    if name == "codex":
        is_run = codex_up_fn() if running is None else bool(running)
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        now = time.time()
        with health_lock:
            h = dict(health_dict.get("codex", {}))
        if h.get("api_ok") is False and (now - h.get("ts", 0.0) < UPSTREAM_DEGRADED_TTL):
            return {"state": "degraded", "running": True, "reason": h.get("reason") or "API报错"}
        ok, proxy_port = check_proxy_fn(conf)
        if not ok and proxy_port:
            return {"state": "degraded", "running": True, "reason": f"代理未运行 (:{proxy_port})"}
        return {"state": "online", "running": True, "reason": ""}

    if name in ("antigravity", "gemini"):
        is_run = antigravity_up_fn(conf) if running is None else bool(running)
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        now = time.time()
        with health_lock:
            h = dict(health_dict.get("antigravity", {}))
        if h.get("api_ok") is False and (now - h.get("ts", 0.0) < UPSTREAM_DEGRADED_TTL):
            return {"state": "degraded", "running": True, "reason": h.get("reason") or "API报错"}
        return {"state": "online", "running": True, "reason": ""}

    if name == "voice":
        if running is not None and not running:
            return {"state": "offline", "running": False, "reason": ""}
        from relay.voice_bridge import voice_status_dict, voice_up
        v_stat = voice_status_dict(conf)
        is_run = (
            bool(v_stat.get("ready", False) or voice_up(conf) or v_stat.get("status") == "starting")
            if running is None else bool(running)
        )
        if not is_run:
            return {"state": "offline", "running": False, "reason": ""}
        if v_stat.get("status") == "starting":
            return {"state": "degraded", "running": True, "reason": "启动中"}
        if v_stat.get("status") == "loading_model":
            return {"state": "degraded", "running": True, "reason": "加载模型中"}
        if v_stat.get("ready"):
            return {"state": "online", "running": True, "reason": ""}
        return {"state": "degraded", "running": True, "reason": "未就绪"}

    return {"state": "online", "running": True, "reason": ""}


def codex_start(conf):
    if codex_up():
        return "already"
    exe = conf.get("codex_exe")
    cfg = conf.get("codex_config")
    PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    cmd = (f"Start-Process -WindowStyle Hidden -FilePath '{exe}' "
           f"-ArgumentList '--config','{cfg}' -WorkingDirectory '{os.path.dirname(exe)}' "
           f"-RedirectStandardOutput '{os.path.dirname(exe)}\\proxy.out.log' "
           f"-RedirectStandardError '{os.path.dirname(exe)}\\proxy.err.log'")
    try:
        _sub = getattr(sys.modules.get("cc_relay"), "subprocess", subprocess)
        _sub.Popen([PS, "-NoProfile", "-Command", cmd], creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return "start-failed"
    for _ in range(40):
        time.sleep(0.5)
        if codex_up():
            return "started"
    return "timeout"


_ANTIGRAVITY_PROCESS_LOCK = threading.Lock()
_ANTIGRAVITY_PROCESS = None
_ANTIGRAVITY_EXE = ""


def antigravity_start(conf):
    """按配置懒启动 Antigravity Tools; 已运行时不重复拉起。"""
    global _ANTIGRAVITY_PROCESS, _ANTIGRAVITY_EXE
    if antigravity_up(conf):
        return "already"
    exe = find_antigravity_exe(conf)
    if not exe:
        return "no-exe"
    try:
        _sub = getattr(sys.modules.get("cc_relay"), "subprocess", subprocess)
        proc = _sub.Popen([exe], cwd=os.path.dirname(exe),
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
        mod = sys.modules.get("cc_relay")
        proc = getattr(mod, "_ANTIGRAVITY_PROCESS", _ANTIGRAVITY_PROCESS) if mod else _ANTIGRAVITY_PROCESS
        exe = getattr(mod, "_ANTIGRAVITY_EXE", _ANTIGRAVITY_EXE) if mod else _ANTIGRAVITY_EXE
        if proc is None:
            return "not-managed"
        if proc.poll() is not None:
            _ANTIGRAVITY_PROCESS = None
            _ANTIGRAVITY_EXE = ""
            if mod and hasattr(mod, "_ANTIGRAVITY_PROCESS"):
                setattr(mod, "_ANTIGRAVITY_PROCESS", None)
            return "not-running"
        pid = getattr(proc, "pid", None)
        if not pid or not exe:
            return "not-managed"
        try:
            _sub = getattr(mod, "subprocess", subprocess) if mod else subprocess
            result = _sub.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                              text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                return "stop-failed"
        except Exception as e:
            return repr(e)
        finally:
            _ANTIGRAVITY_PROCESS = None
            _ANTIGRAVITY_EXE = ""
            if mod and hasattr(mod, "_ANTIGRAVITY_PROCESS"):
                setattr(mod, "_ANTIGRAVITY_PROCESS", None)
    return "stopped"


def antigravity_owned():
    with _ANTIGRAVITY_PROCESS_LOCK:
        return _ANTIGRAVITY_PROCESS is not None and _ANTIGRAVITY_PROCESS.poll() is None


def antigravity_open_ui(conf=None):
    """Wake up or launch Antigravity Tools window so user can access its native update interface."""
    conf = conf or load_conf()
    exe = find_antigravity_exe(conf)
    if not exe:
        raw_cfg = (conf.get("antigravity_exe") or "").strip()
        target_hint = f": {raw_cfg}" if raw_cfg else ""
        return {"ok": False, "error": f"未找到 Antigravity Tools 可执行文件{target_hint}"}

    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    except Exception as e:
        return {"ok": False, "error": f"启动 Antigravity Tools 失败: {e}"}

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
        _sub = getattr(sys.modules.get("cc_relay"), "subprocess", subprocess)
        result = _sub.run(["taskkill", "/F", "/IM", "cli-proxy-api.exe"], capture_output=True,
                          text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0 and codex_up():
            return "stop-failed"
        return "stopped"
    except Exception as e:
        return repr(e)


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


def _fetch_models(base, key, timeout=6, prefix=None):
    """从上游 /v1/models 拉模型名(过滤图像类); 返回空表示上游不可用"""
    if not base:
        return []
    try:
        req = urllib.request.Request(base.rstrip("/") + "/v1/models")
        if key:
            req.add_header("Authorization", "Bearer " + key)
            req.add_header("x-api-key", key)
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
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


def probe_upstream(conf, name="antigravity", timeout=6):
    """只读探测模型端点，不发送对话请求，也不返回任何凭据。"""
    actual = "antigravity" if name in ("gemini", "antigravity") else name
    up = _upstream_conf(conf, actual)
    base = (up.get("base") or "").rstrip("/")
    if not base:
        record_upstream_health(actual, False, "missing base")
        return {"name": actual, "available": False, "error": "missing base"}

    proto = str(up.get("protocol") or "").strip().lower()
    if not proto:
        proto = "anthropic" if actual in ("deepseek", "antigravity") else "openai"

    probe_url = base + ("/models" if proto == "openai" else "/v1/models")
    key = _key_for(conf, up, actual)

    try:
        req = urllib.request.Request(probe_url, method="GET")
        if key:
            req.add_header("Authorization", "Bearer " + key)
            req.add_header("x-api-key", key)
        proxy_url = up.get("proxy_url")
        if proxy_url and proxy_url != "direct":
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            payload = json.load(resp)
        ids = [str(x.get("id") or "") for x in payload.get("data", []) if isinstance(x, dict)]
        record_upstream_health(actual, True)
        return {"name": actual, "available": True, "protocol": proto, "models": sorted(set(ids)),
                "messages_path": base + ("/v1/messages" if proto == "anthropic" else "/chat/completions")}
    except Exception as e:
        if proto == "openai" and not base.endswith("/v1"):
            try:
                probe_url = base + "/v1/models"
                req = urllib.request.Request(probe_url, method="GET")
                if key:
                    req.add_header("Authorization", "Bearer " + key)
                with opener.open(req, timeout=timeout) as resp:
                    payload = json.load(resp)
                ids = [str(x.get("id") or "") for x in payload.get("data", []) if isinstance(x, dict)]
                record_upstream_health(actual, True)
                return {"name": actual, "available": True, "protocol": proto, "models": sorted(set(ids)),
                        "messages_path": base + "/v1/chat/completions"}
            except Exception:
                pass
        record_upstream_health(actual, False, str(e)[:240])
        return {"name": actual, "available": False, "protocol": proto, "error": str(e)[:240]}


_MODELS_CACHE = {"ts": 0, "ds": [], "cx": [], "gm": [], "health": {}}
_MODELS_LOCK = threading.Lock()
_MODELS_REFRESHING = False


def _bg_fetch_models(conf, now):
    global _MODELS_REFRESHING
    mod = sys.modules.get("cc_relay")
    fetch_fn = getattr(mod, "_fetch_models", _fetch_models) if mod else _fetch_models
    record_health_fn = getattr(mod, "record_upstream_health", record_upstream_health) if mod else record_upstream_health
    try:
        ds = fetch_fn((_upstream_conf(conf, "deepseek")).get("base", ""),
                      _key_for(conf, _upstream_conf(conf, "deepseek"), "deepseek"), prefix="deepseek-")
        cx = fetch_fn((_upstream_conf(conf, "codex")).get("base", ""),
                      _key_for(conf, _upstream_conf(conf, "codex"), "codex"), prefix="gpt-")
        gm = fetch_fn((_upstream_conf(conf, "antigravity")).get("base", ""),
                      _key_for(conf, _upstream_conf(conf, "antigravity"), "antigravity"), prefix="gemini-")
        if gm:
            record_health_fn("antigravity", True)
        if cx:
            record_health_fn("codex", True)
        if ds:
            record_health_fn("deepseek", True)
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
