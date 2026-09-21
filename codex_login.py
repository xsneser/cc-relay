"""Isolated CLIProxyAPI Codex OAuth support for cc-relay's UI.

This module owns only ``BASE/codex-proxy/ui-auth-runtime``.  In particular it
does not import cc_relay or read the user's Codex CLI credentials.
"""
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit


PORT = 8317
TTL_SECONDS = 5 * 60
READY_ATTEMPTS = 75
READY_DELAY = 0.2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CodexLogin:
    """Manage one in-memory, short-lived CLIProxyAPI Codex login attempt."""

    def __init__(self, base, load_conf, save_conf, conf_lock):
        self.base = Path(base).resolve()
        self.load_conf = load_conf
        self.save_conf = save_conf
        self.conf_lock = conf_lock
        self._lock = threading.RLock()
        self._pending = None
        self._callback = None

    @property
    def proxy_dir(self):
        return self.base / "codex-proxy"

    @property
    def exe_path(self):
        return self.proxy_dir / "cli-proxy-api.exe"

    @property
    def runtime_dir(self):
        return self.proxy_dir / "ui-auth-runtime"

    @property
    def auth_dir(self):
        return self.runtime_dir / "auth"

    @property
    def config_path(self):
        return self.runtime_dir / "config.yaml"

    @property
    def metadata_path(self):
        return self.runtime_dir / "management.json"

    def start(self):
        """Start the local service if needed and return the approved OAuth URL."""
        with self._lock:
            if self._pending and not self._expired(self._pending):
                return {"status": "waiting", "url": self._pending["url"]}
            self._close_callback()
            self._pending = None
            try:
                metadata = self._preflight()
                if not self.exe_path.is_file():
                    raise _SafeError("未找到 CLIProxyAPI 程序")
                if self._port_open():
                    if metadata is None:
                        raise _SafeError("8317 端口已被其他程序占用")
                    if not self._management_works(metadata):
                        return self._error("8317 端口已被其他程序占用")
                else:
                    metadata = self._provision(metadata)
                    self._launch()
                    if not self._wait_ready(metadata):
                        return self._error("Codex 登录服务启动超时")

                self._save_relay_conf(metadata)
                reply = self._request_json(
                    "/v0/management/codex-auth-url", metadata
                )
                url, state = self._validated_auth_reply(reply)
                callback = self._new_callback(
                    state, lambda payload: self._request_json(
                        "/v0/management/oauth-callback", metadata, payload
                    )
                )
                self._callback = callback
                try:
                    callback.start()
                except OSError as exc:
                    raise _SafeError("OAuth 回调端口 1455 无法启动，请检查是否已有登录任务") from exc
                self._pending = {"url": url, "state": state, "created": time.monotonic()}
                return {"status": "waiting", "url": url}
            except _SafeError as exc:
                self._close_callback()
                return self._error(exc.message)
            except Exception:
                self._close_callback()
                return self._error("Codex 登录服务暂时不可用")

    def status(self):
        """Return safe UI state.  This method never starts a process."""
        with self._lock:
            pending = self._pending
            if pending:
                if self._expired(pending):
                    self._pending = None
                    self._close_callback()
                    return {"status": "timeout", "message": "登录链接已超时，请重新开始", "account_count": 0}
                try:
                    metadata = self._read_metadata()
                    reply = self._request_json(
                        "/v0/management/get-auth-status?state=" + quote(pending["state"], safe=""),
                        metadata,
                    )
                    state = str(reply.get("status", "")).lower()
                    entries = self._codex_auth_entries(metadata)
                    count = len(entries)
                    if state in ("waiting", "wait", "pending", "processing"):
                        return {"status": "waiting", "message": "等待在浏览器中完成登录",
                                "account_count": count, "url": pending["url"]}
                    if state in ("ok", "success", "authorized", "completed"):
                        self._pending = None
                        self._close_callback()
                        # v7.2.155 returns error for a nonempty unknown state, so a
                        # completed status is authoritative even when a relogin keeps
                        # the same auth-file identity.
                        return {"status": "ok", "message": "Codex 账号登录成功", "account_count": count}
                    self._pending = None
                    self._close_callback()
                    return {"status": "error", "message": "Codex 登录未完成，请重新开始", "account_count": count}
                except _SafeError as exc:
                    self._pending = None
                    self._close_callback()
                    return self._error(exc.message)
                except Exception:
                    self._pending = None
                    self._close_callback()
                    return self._error("无法查询 Codex 登录状态")

            try:
                self._assert_managed_paths()
                metadata = self._read_metadata()
                if not self._port_open() or not self._management_works(metadata):
                    return {"status": "idle", "message": "尚未开始 Codex 登录", "account_count": 0}
                count = self._account_count(metadata)
                if count:
                    return {"status": "ok", "message": "Codex 账号已登录", "account_count": count}
            except _SafeError:
                pass
            except Exception:
                pass
            return {"status": "idle", "message": "尚未开始 Codex 登录", "account_count": 0}

    def close(self):
        """Release a pending loopback callback listener for shutdown/test teardown."""
        with self._lock:
            self._pending = None
            self._close_callback()

    def _preflight(self):
        """Validate ownership without changing config, files, or process state."""
        self._assert_managed_paths()
        expected_exe = str(self.exe_path)
        expected_config = str(self.config_path)
        with self.conf_lock:
            conf = self.load_conf() or {}
            for field, expected in (("codex_exe", expected_exe), ("codex_config", expected_config)):
                value = str(conf.get(field) or "").strip()
                if value and os.path.normcase(os.path.abspath(value)) != os.path.normcase(os.path.abspath(expected)):
                    raise _SafeError("检测到外部 Codex 代理配置，无法接管")
            upstream = (conf.get("upstreams") or {}).get("codex") or {}
            if upstream.get("base") and not self._is_local_base(upstream.get("base")):
                raise _SafeError("Codex 上游必须使用本机 8317 端口")
        metadata = self._read_metadata(optional=True)
        if self.config_path.exists() != bool(metadata):
            raise _SafeError("登录运行目录不是受管目录，已拒绝覆盖")
        return metadata

    def _provision(self, metadata):
        """Create the runtime once; existing managed configuration is untouched."""
        self._assert_managed_paths()
        if metadata is None:
            if self.runtime_dir.exists():
                raise _SafeError("登录运行目录不是受管目录，已拒绝覆盖")
            metadata = {"format": 1, "management_key": secrets.token_urlsafe(32),
                        "relay_key": self._relay_key()}
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._lock_new_runtime_dir()
            self.auth_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(self.config_path, {
                "host": "127.0.0.1", "port": PORT, "auth-dir": str(self.auth_dir),
                "api-keys": [metadata["relay_key"]], "remote-management": {
                    "allow-remote": False, "secret-key": metadata["management_key"],
                    "disable-control-panel": True,
                }, "debug": False, "logging-to-file": False, "request-log": False,
            })
            self._write_json(self.metadata_path, metadata)
        self._save_relay_conf(metadata)
        return metadata

    def _relay_key(self):
        with self.conf_lock:
            conf = self.load_conf() or {}
            return str(conf.get("codex_proxy_key") or "").strip() or secrets.token_urlsafe(32)

    def _save_relay_conf(self, metadata):
        with self.conf_lock:
            conf = self.load_conf() or {}
            configured_key = str(conf.get("codex_proxy_key") or "").strip()
            relay_key = metadata["relay_key"]
            if configured_key and configured_key != relay_key:
                raise _SafeError("本地 Codex 密钥与受管登录配置不一致")
            conf["codex_exe"] = str(self.exe_path)
            conf["codex_config"] = str(self.config_path)
            conf["codex_proxy_key"] = relay_key
            upstreams = conf.setdefault("upstreams", {})
            codex = upstreams.setdefault("codex", {})
            codex["base"] = self._local_base()
            codex["key_env"] = "codex_proxy_key"
            self.save_conf(conf)

    def _launch(self):
        if not self.exe_path.is_file():
            raise _SafeError("未找到 CLIProxyAPI 程序")
        try:
            subprocess.Popen([str(self.exe_path), "--config", str(self.config_path)],
                             cwd=str(self.proxy_dir), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1455:
                raise _SafeError("系统资源不足，无法启动 Codex 登录服务")
            raise _SafeError("无法启动 Codex 登录服务")

    def _wait_ready(self, metadata):
        deadline = time.monotonic() + 15
        for _ in range(READY_ATTEMPTS):
            if time.monotonic() >= deadline:
                break
            if self._port_open() and self._management_works(metadata):
                return True
            time.sleep(READY_DELAY)
        return False

    def _management_works(self, metadata):
        try:
            self._auth_files(metadata)
            return True
        except _SafeError:
            return False

    def _request_json(self, path, metadata, body=None):
        key = str((metadata or {}).get("management_key") or "")
        if not key:
            raise _SafeError("Codex 登录运行信息无效")
        url = "http://127.0.0.1:%d%s" % (PORT, path)
        data = None
        headers = {"Authorization": "Bearer " + key}
        if body is not None:
            data = json.dumps(body, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=3) as response:
                if response.geturl() != url:
                    raise _SafeError("Codex 登录服务响应异常")
                data = response.read(65537)
                if len(data) > 65536:
                    raise _SafeError("Codex 登录服务响应异常")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            raise _SafeError("无法连接 Codex 登录服务")
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _SafeError("Codex 登录服务响应异常")
        if not isinstance(parsed, (dict, list)):
            raise _SafeError("Codex 登录服务响应异常")
        return parsed

    def _validated_auth_reply(self, reply):
        if not isinstance(reply, dict) or reply.get("status") != "ok":
            raise _SafeError("无法获取 Codex 登录链接")
        url, state = reply.get("url"), reply.get("state")
        parsed = urlsplit(str(url or ""))
        query_states = parse_qs(parsed.query, keep_blank_values=True).get("state", [])
        if (parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.port is not None
                or parsed.username is not None or parsed.password is not None or parsed.path != "/oauth/authorize"
                or not isinstance(state, str) or not state or query_states != [state]):
            raise _SafeError("Codex 登录链接校验失败")
        return str(url), state

    def _account_count(self, metadata):
        return len(self._codex_auth_entries(metadata))

    def _codex_auth_entries(self, metadata):
        return [item for item in self._auth_files(metadata) if self._enabled_codex(item)]

    def _auth_files(self, metadata):
        items = self._request_json("/v0/management/auth-files", metadata)
        if not isinstance(items, dict) or not isinstance(items.get("files"), list):
            raise _SafeError("Codex 登录服务响应异常")
        return items["files"]

    @staticmethod
    def _enabled_codex(item):
        return (isinstance(item, dict)
                and str(item.get("provider") or item.get("type") or "").lower() == "codex"
                and item.get("disabled") is not True)

    def _read_metadata(self, optional=False):
        try:
            with self.metadata_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            if (not isinstance(value, dict) or value.get("format") != 1
                    or not isinstance(value.get("management_key"), str) or not value["management_key"]
                    or not isinstance(value.get("relay_key"), str) or not value["relay_key"]):
                raise ValueError()
            return value
        except FileNotFoundError:
            if optional:
                return None
            raise _SafeError("Codex 登录运行信息无效")
        except (OSError, ValueError, json.JSONDecodeError):
            raise _SafeError("Codex 登录运行信息无效")

    @staticmethod
    def _write_json(path, value):
        temporary = Path(str(path) + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
        os.replace(str(temporary), str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    def _lock_new_runtime_dir(self):
        """Apply platform ACLs before management or relay keys reach this directory."""
        if os.name != "nt":
            try:
                os.chmod(str(self.runtime_dir), 0o700)
            except OSError as exc:
                raise _SafeError("无法保护 Codex 登录运行目录") from exc
            return
        domain = str(os.environ.get("USERDOMAIN") or "").strip()
        user = str(os.environ.get("USERNAME") or "").strip()
        identity = (domain + "\\" if domain else "") + user
        if not identity:
            raise _SafeError("无法确定当前 Windows 用户")
        command = ["icacls", str(self.runtime_dir), "/inheritance:r", "/grant:r",
                   identity + ":(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                   "/grant:r", "*S-1-5-32-544:(OI)(CI)F"]
        try:
            completed = subprocess.run(command, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            raise _SafeError("无法保护 Codex 登录运行目录")
        if completed.returncode != 0:
            raise _SafeError("无法保护 Codex 登录运行目录")

    @staticmethod
    def _is_local_base(value):
        parsed = urlsplit(str(value))
        try:
            return (parsed.scheme == "http" and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment and parsed.path in ("", "/")
                    and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                    and parsed.port == PORT)
        except ValueError:
            return False

    @staticmethod
    def _local_base():
        return "http://127.0.0.1:%d" % PORT

    @staticmethod
    def _port_open(port=None):
        try:
            with socket.create_connection(("127.0.0.1", PORT if port is None else port), timeout=0.2):
                return True
        except OSError:
            return False

    @staticmethod
    def _new_callback(state, forward):
        from codex_callback import LoopbackCallback
        return LoopbackCallback(state, forward)

    def _close_callback(self):
        callback, self._callback = self._callback, None
        if callback is not None:
            try:
                callback.close()
            except Exception:
                pass

    @staticmethod
    def _expired(pending):
        return time.monotonic() - pending["created"] >= TTL_SECONDS

    @staticmethod
    def _error(message):
        return {"status": "error", "message": message, "account_count": 0}

    def _assert_managed_paths(self):
        proxy = self.proxy_dir.resolve(strict=False)
        runtime = self.runtime_dir.resolve(strict=False)
        auth = self.auth_dir.resolve(strict=False)
        for path in (self.proxy_dir, self.runtime_dir, self.config_path, self.metadata_path, self.auth_dir):
            if (path.is_symlink() or os.path.normcase(str(path.resolve(strict=False)))
                    != os.path.normcase(os.path.abspath(path))):
                raise _SafeError("登录运行目录路径不安全")
        if not self._within(runtime, proxy) or not self._within(auth, runtime):
            raise _SafeError("登录运行目录路径不安全")

    @staticmethod
    def _within(path, parent):
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


class _SafeError(Exception):
    def __init__(self, message):
        self.message = message
