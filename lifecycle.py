#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-relay 生命周期管理 (被 claude wrapper 调用)
    autostart : 确保中转+UI 运行 (未跑则拉起); 退出码 0 = 刚启动(可开浏览器)
    watch     : 单实例看门狗; 观察到 claude 运行过→归零 → 停中转+codex上游
    stopall   : 立即停中转+codex
    status    : 打印状态
"""
import os, sys, time, json, socket, subprocess
from urllib.parse import urlparse

BASE = os.environ.get("CC_RELAY_DIR") or os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "config.json")
RELAY = os.path.join(BASE, "cc_relay.py")
PYW = os.environ.get("CC_RELAY_PYTHONW") or sys.executable.replace("python.exe","pythonw.exe")
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CODEX_PORT = 8317
ANTIGRAVITY_PORT = int(os.environ.get("CC_RELAY_ANTIGRAVITY_PORT", "8045"))
RELAY_DEFAULT_PORT = 8400
UI_DEFAULT_PORT = 8610
VOICE_DEFAULT_PORT = 8401
WATCH_PID = os.path.join(BASE, ".watch.pid")


def tcp(port, host="127.0.0.1", t=0.6):
    try:
        s = socket.create_connection((host, port), timeout=t); s.close(); return True
    except Exception:
        return False


def _load_conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def relay_port(conf=None):
    conf = conf or _load_conf()
    return int(conf.get("listen_port") or RELAY_DEFAULT_PORT)


def ui_port(conf=None):
    conf = conf or _load_conf()
    return int(conf.get("ui_port") or UI_DEFAULT_PORT)


def relay_up(conf=None):
    return tcp(relay_port(conf))


def antigravity_port(conf=None):
    conf = conf or _load_conf()
    ups = conf.get("upstreams") or {}
    u = ups.get("gemini") or ups.get("antigravity") or {}
    try:
        parsed = urlparse(u.get("base") or "")
        return parsed.port or (443 if parsed.scheme == "https" else ANTIGRAVITY_PORT)
    except Exception:
        return ANTIGRAVITY_PORT


def antigravity_up(conf=None):
    return tcp(antigravity_port(conf))


def antigravity_auto_start_enabled(conf=None):
    """Return whether implicit Antigravity startup is explicitly enabled."""
    conf = conf or _load_conf()
    tools = conf.get("tools") or {}
    settings = tools.get("antigravity") or {}
    return settings.get("auto_start") is True


def antigravity_ensure(conf=None):
    """只负责确保外部 Gemini sidecar 已启动, 不默认停止用户进程。"""
    conf = conf or _load_conf()
    if antigravity_up(conf):
        return "already"
    exe = (conf.get("antigravity_exe") or "").strip()
    if not exe or not os.path.exists(exe):
        return "no-exe"
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return "start-failed"
    for _ in range(40):
        time.sleep(0.5)
        if antigravity_up(conf):
            return "started"
    return "timeout"


def voice_port(conf=None):
    conf = conf or _load_conf()
    return int((conf.get("tools") or {}).get("voice", {}).get("port") or VOICE_DEFAULT_PORT)


def voice_up(conf=None):
    port = voice_port(conf)
    try:
        import urllib.request as _urllib
        req = _urllib.Request(f"http://127.0.0.1:{port}/health", headers={"Origin": "http://127.0.0.1:8610"})
        with _urllib.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("service") == "voice"
    except Exception:
        pass
    return False


def voice_auto_start_enabled(conf=None):
    conf = conf or _load_conf()
    tools = conf.get("tools") or {}
    settings = tools.get("voice") or {}
    return settings.get("auto_start") is True


def voice_ensure(conf=None):
    """确保语音伴侣后台服务已拉起"""
    conf = conf or _load_conf()
    if voice_up(conf):
        return "already"
    try:
        import cc_relay
        return cc_relay.voice_start(conf)
    except Exception as e:
        return f"start-failed: {e}"


def voice_stop():
    """安全停止语音伴侣进程"""
    try:
        import cc_relay
        return cc_relay.voice_stop()
    except Exception as e:
        return f"stop-failed: {e}"


def _route_needs_gemini(conf):
    rt = conf.get("router") or {}
    route = str(rt.get("route") or "hybrid").lower()
    if route in ("gemini", "antigravity"):
        return True
    if route != "hybrid":
        return False
    models = list((rt.get("tiers") or {}).values())
    models += [rt.get("hybrid_antigravity_model")]
    routes = conf.get("model_routes") or {}
    return any(str(m or "").lower().startswith("gemini-") or routes.get(m) in ("gemini", "antigravity")
               for m in models)


def _pythonw_pids_like(needle):
    try:
        out = subprocess.run(
            [PS, "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*%s*' } | ForEach-Object { $_.ProcessId }" % needle],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _pids_on_ports(ports):
    """按端口占用反查 PID (比命令行匹配可靠)"""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            for p in ports:
                if (":%d " % p) in line or line.rstrip().endswith(":%d" % p):
                    toks = line.split()
                    if toks and toks[-1].isdigit():
                        pids.add(int(toks[-1]))
    except Exception:
        pass
    return pids


def relay_start():
    """启动 cc-relay (含 UI); 返回 True=刚启动"""
    conf = _load_conf()
    rp = relay_port(conf)
    up = ui_port(conf)
    if relay_up(conf):
        return False
    # 按端口 + 命令行双重清掉僵尸实例
    for pid in _pids_on_ports([rp, up]):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    for pid in _pythonw_pids_like("cc_relay"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(1.2)
    if tcp(rp):
        return False  # 端口仍被占, 让现有的用
    subprocess.Popen([PYW, RELAY, "serve"], cwd=BASE, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(30):
        time.sleep(0.4)
        if relay_up(conf):
            return True
    return False


def relay_stop():
    conf = _load_conf()
    rp = relay_port(conf)
    up = ui_port(conf)
    for pid in _pids_on_ports([rp, up]) | set(_pythonw_pids_like("cc_relay")):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return True


def codex_stop():
    subprocess.run(["taskkill", "/F", "/IM", "cli-proxy-api.exe"], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def claude_count():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe"],
                             capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return sum(1 for l in out.splitlines() if l.strip().lower().startswith("claude.exe"))
    except Exception:
        return 0


def _watch_alive():
    try:
        pid = int(open(WATCH_PID).read().strip())
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True,
                             text=True, timeout=15, encoding="utf-8", errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return str(pid) in out
    except Exception:
        return False


def cmd_autostart():
    conf = _load_conf()
    started = relay_start()
    ag_auto = antigravity_auto_start_enabled(conf)
    ag_result = "disabled" if not ag_auto else "skipped"
    if ag_auto and _route_needs_gemini(conf) and not os.environ.get("CC_RELAY_SKIP_TOOLS"):
        ag_result = antigravity_ensure(conf)
    voice_auto = voice_auto_start_enabled(conf)
    voice_result = "disabled" if not voice_auto else "skipped"
    if voice_auto and not os.environ.get("CC_RELAY_SKIP_TOOLS"):
        voice_result = voice_ensure(conf)
    # relay 启动成功仍返回原有语义; Gemini 与 Voice 状态通过 status/JSON 诊断。
    if os.environ.get("CC_RELAY_STATUS_JSON") == "1":
        print(json.dumps({"ok": relay_up(), "relay": "started" if started else "already_running",
                          "antigravity": ag_result, "antigravity_up": antigravity_up(conf),
                          "voice": voice_result, "voice_up": voice_up(conf)}, ensure_ascii=False))
    return 0 if started else 3


def cmd_watch():
    if _watch_alive():
        return
    with open(WATCH_PID, "w") as f:
        f.write(str(os.getpid()))
    was_running = False
    idle_since = time.time()
    _last_resurrect = 0.0
    try:
        while True:
            c = claude_count()
            # 保活: claude 在跑而中转不在 -> 自动拉起 (防抖 15s, 避免反复拉起风暴)
            if c > 0 and not relay_up() and time.time() - _last_resurrect > 15:
                relay_start()
                _last_resurrect = time.time()
            if was_running and c == 0:
                time.sleep(6)
                if claude_count() == 0:
                    relay_stop()
                    codex_stop()
                    voice_stop()
                    break
                was_running = False
            if c > 0:
                was_running = True
                idle_since = time.time()
            elif not was_running and time.time() - idle_since > 90:
                relay_stop()
                codex_stop()
                voice_stop()
                break
            time.sleep(3)
    finally:
        try:
            os.remove(WATCH_PID)
        except Exception:
            pass


def cmd_stopall():
    relay_stop()
    codex_stop()
    voice_stop()


def cmd_status():
    conf = _load_conf()
    print(json.dumps({"relay_up": relay_up(conf), "ui_up": tcp(ui_port(conf)),
                      "codex_up": tcp(CODEX_PORT), "antigravity_up": antigravity_up(conf),
                      "gemini_up": antigravity_up(conf), "voice_up": voice_up(conf),
                      "claude_procs": claude_count()}, ensure_ascii=False))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "autostart":
        sys.exit(cmd_autostart())
    elif cmd == "watch":
        cmd_watch()
    elif cmd == "stopall":
        cmd_stopall()
    else:
        cmd_status()
