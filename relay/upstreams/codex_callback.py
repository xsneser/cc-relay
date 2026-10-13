"""Short-lived loopback-only bridge to CLIProxyAPI's authenticated callback API."""
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class _LoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LoopbackCallback:
    def __init__(self, state, forward, port=1455, ttl=300):
        self.state = state
        self.forward = forward
        self.port = port
        self.ttl = ttl
        self._server = None
        self._timer = None
        self._lock = threading.Lock()
        self._accepted = threading.Lock()
        self._consumed = False

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def respond(self, code, message):
                body = ("<!doctype html><meta charset=utf-8><title>Codex Login</title>"
                        "<p>" + message + "</p>").encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                try:
                    host = urlsplit("http://" + self.headers.get("Host", ""))
                    url = urlsplit(self.path)
                    if (host.hostname not in ("127.0.0.1", "localhost")
                            or host.port != self.server.server_port or host.username or host.password
                            or host.path or host.query or host.fragment or len(self.path) > 8192
                            or url.path != "/auth/callback" or url.scheme or url.netloc):
                        raise ValueError()
                    query = parse_qs(url.query, max_num_fields=20)
                    states = query.get("state", [])
                    if len(states) != 1 or not secrets.compare_digest(states[0], owner.state):
                        raise ValueError()
                    code = query.get("code", [""])
                    error = query.get("error", [""])
                    if len(code) != 1 or len(error) != 1 or not (code[0] or error[0]):
                        raise ValueError()
                except (ValueError, TypeError):
                    self.respond(400, "Invalid authentication callback.")
                    return
                with owner._accepted:
                    if owner._consumed:
                        self.respond(409, "This callback was already received.")
                        return
                    try:
                        result = owner.forward({"provider": "codex", "state": states[0],
                                                "code": code[0], "error": error[0]})
                        if not isinstance(result, dict) or result.get("status") != "ok":
                            raise ValueError()
                    except Exception:
                        self.respond(502, "Unable to deliver authorization. Return to CC Relay and retry.")
                        return
                    owner._consumed = True
                self.respond(200, "Authorization received. Return to CC Relay to check login status. You can close this tab.")

        with self._lock:
            if self._server is not None:
                return
            server = _LoopbackServer(("127.0.0.1", self.port), Handler)
            self._server = server
            self.port = server.server_port
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self._timer = threading.Timer(self.ttl, self.close)
            self._timer.daemon = True
            self._timer.start()

    def close(self):
        with self._lock:
            server, self._server = self._server, None
            timer, self._timer = self._timer, None
        if timer:
            timer.cancel()
        if server:
            server.shutdown()
            server.server_close()
