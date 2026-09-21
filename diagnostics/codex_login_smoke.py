"""Temporary UI + real CLIProxyAPI smoke fixture. No production config or tokens.

Run from the repository with --ui-port 8611 --proxy-port 18317. Stop by POSTing
to /_test/stop; the fixture also self-terminates after 15 minutes.
"""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cc_relay
import codex_login


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-port", type=int, default=8611)
    parser.add_argument("--proxy-port", type=int, default=18317)
    args = parser.parse_args()
    codex_login.PORT = args.proxy_port
    child = None
    with tempfile.TemporaryDirectory(prefix="cc-relay-oauth-smoke-") as temporary:
        base = Path(temporary)
        (base / "codex-proxy").mkdir()
        shutil.copy2(ROOT / "codex-proxy/cli-proxy-api.exe", base / "codex-proxy/cli-proxy-api.exe")
        config = {"upstreams": {"codex": {"base": "http://127.0.0.1:" + str(args.proxy_port), "key_env": "codex_proxy_key"}}}

        def save(value):
            config.clear()
            config.update(copy.deepcopy(value))

        login = codex_login.CodexLogin(base, lambda: copy.deepcopy(config), save, threading.Lock())
        port_open = login._port_open
        login._port_open = lambda port=None: port_open(args.proxy_port if port is None else port)

        def launch():
            nonlocal child
            child = subprocess.Popen([str(login.exe_path), "--config", str(login.config_path)],
                                     cwd=str(login.proxy_dir), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        login._launch = launch
        cc_relay.get_codex_login = lambda: login
        cc_relay.load_conf = lambda: copy.deepcopy(config)
        cc_relay.read_ui_content = lambda: (ROOT / "ui.html").read_bytes()
        cc_relay.stats_snapshot = lambda: {"rows": [], "models": [], "route": "hybrid", "total": 0}
        cc_relay._calls_snapshot = lambda *a: []

        class Handler(cc_relay.UIHandler):
            def do_GET(self):
                if self.path == "/_test/proxy-health":
                    request = urllib.request.Request(
                        "http://127.0.0.1:%d/v1/models" % args.proxy_port,
                        headers={"Authorization": "Bearer " + config.get("codex_proxy_key", "")})
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(request, timeout=3) as response:
                            self._json({"proxy_http": response.status})
                    except Exception:
                        self._json({"proxy_http": None})
                else:
                    super().do_GET()

            def do_POST(self):
                if self.path == "/_test/stop":
                    self._json({"ok": True})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    super().do_POST()

        server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", args.ui_port), Handler)
        timer = threading.Timer(900, server.shutdown)
        timer.daemon = True
        timer.start()
        print(json.dumps({"ui_port": args.ui_port, "proxy_port": args.proxy_port, "isolated": True}), flush=True)
        try:
            server.serve_forever()
        finally:
            timer.cancel()
            server.server_close()
            login.close()
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


if __name__ == "__main__":
    main()
