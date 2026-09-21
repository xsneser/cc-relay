import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_login


class CodexLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "codex-proxy").mkdir()
        (self.base / "codex-proxy" / "cli-proxy-api.exe").touch()
        self.conf = {"upstreams": {"codex": {"base": "http://127.0.0.1:8317"}}}
        self.lock = threading.RLock()
        self.login = codex_login.CodexLogin(self.base, self.load, self.save, self.lock)
        self.acl_patch = mock.patch.object(self.login, "_lock_new_runtime_dir")
        self.acl_patch.start()
        self.callback = mock.Mock()
        self.callback_patch = mock.patch.object(self.login, "_new_callback", return_value=self.callback)
        self.callback_patch.start()
        self.launch_patch = mock.patch.object(self.login, "_launch")
        self.launch_patch.start()

    def tearDown(self):
        self.callback_patch.stop()
        self.launch_patch.stop()
        self.acl_patch.stop()
        self.temp.cleanup()

    def load(self):
        return json.loads(json.dumps(self.conf))

    def save(self, value):
        self.conf = value

    def auth_reply(self):
        return {"status": "ok", "url": "https://auth.openai.com/oauth/authorize?state=good", "state": "good"}

    @staticmethod
    def no_ports(port=codex_login.PORT):
        return False

    def test_start_provisions_isolated_runtime_and_reuses_relay_key(self):
        self.conf["codex_proxy_key"] = "reused-key"
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value=self.auth_reply()):
            result = self.login.start()
        self.assertEqual(result, {"status": "waiting", "url": self.auth_reply()["url"]})
        self.assertEqual(self.conf["codex_proxy_key"], "reused-key")
        self.assertEqual(self.conf["upstreams"]["codex"]["key_env"], "codex_proxy_key")
        config = json.loads(self.login.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["auth-dir"], str(self.login.auth_dir))
        self.assertFalse(config["remote-management"]["allow-remote"])
        self.assertFalse(config["request-log"])
        self.assertTrue((self.login.runtime_dir / "management.json").exists())
        self.assertNotIn(".codex", self.login.auth_dir.parts)

    def test_start_rejects_external_configuration_without_writing_runtime(self):
        self.conf["codex_exe"] = str(self.base / "elsewhere.exe")
        result = self.login.start()
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.login.runtime_dir.exists())

    def test_timeout_and_status_do_not_start_service(self):
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value=self.auth_reply()):
            self.login.start()
        self.login._pending["created"] -= codex_login.TTL_SECONDS
        with mock.patch.object(self.login, "_launch") as launch:
            result = self.login.status()
        self.assertEqual(result["status"], "timeout")
        launch.assert_not_called()

    def test_status_is_idle_without_metadata_and_does_not_probe_or_provision(self):
        with mock.patch.object(self.login, "_port_open") as port, \
             mock.patch.object(self.login, "_management_works") as management:
            result = self.login.status()
        self.assertEqual(result["status"], "idle")
        self.assertFalse(self.login.runtime_dir.exists())
        port.assert_not_called()
        management.assert_not_called()

    def test_idempotency_and_concurrent_start_share_one_auth_request(self):
        calls = []
        def request(path, metadata):
            calls.append(path)
            return self.auth_reply()
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", side_effect=request):
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.login.start())) for _ in range(6)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(len(results), 6)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(item == results[0] for item in results))

    def test_bad_url_and_busy_port_are_safe_errors(self):
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value={"status": "ok", "url": "https://evil.example/?state=good", "state": "good"}):
            self.assertEqual(self.login.start()["status"], "error")
        second = CodexLoginTests._new_login(self)
        with mock.patch.object(second, "_lock_new_runtime_dir"):
            second._provision(None)
        with mock.patch.object(second, "_port_open", side_effect=lambda port=codex_login.PORT: port == codex_login.PORT), \
             mock.patch.object(second, "_management_works", return_value=False):
            self.assertEqual(second.start()["status"], "error")

    def test_windows_resource_exhaustion_is_a_safe_start_error(self):
        self.launch_patch.stop()
        error = OSError("not enough paging file")
        error.winerror = 1455
        with mock.patch("codex_login.subprocess.Popen", side_effect=error):
            with self.assertRaises(codex_login._SafeError) as caught:
                self.login._launch()
        self.assertEqual(caught.exception.message, "系统资源不足，无法启动 Codex 登录服务")

    def _new_login(self):
        other = Path(self.temp.name) / "other"
        (other / "codex-proxy").mkdir(parents=True)
        (other / "codex-proxy" / "cli-proxy-api.exe").touch()
        return codex_login.CodexLogin(other, self.load, self.save, self.lock)

    def test_status_counts_only_enabled_codex_metadata(self):
        self.login._provision(None)
        items = {"files": [{"provider": "codex", "disabled": False}, {"provider": "codex", "disabled": True},
                 {"provider": "gemini", "disabled": False}]}
        with mock.patch.object(self.login, "_port_open", return_value=True), \
             mock.patch.object(self.login, "_management_works", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value=items):
            result = self.login.status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["account_count"], 1)

    def test_poll_unknown_state_error_is_not_a_success(self):
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value=self.auth_reply()):
            self.login.start()
        def request(path, metadata):
            if "get-auth-status" in path:
                return {"status": "error", "error": "unknown or expired state"}
            return {"files": [{"provider": "codex", "disabled": False}]}
        with mock.patch.object(self.login, "_request_json", side_effect=request):
            result = self.login.status()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["account_count"], 1)

    def test_callback_bind_failure_is_safe_and_closes_no_process(self):
        self.callback.start.side_effect = OSError("port busy")
        with mock.patch.object(self.login, "_port_open", side_effect=self.no_ports), \
             mock.patch.object(self.login, "_wait_ready", return_value=True), \
             mock.patch.object(self.login, "_request_json", return_value=self.auth_reply()):
            result = self.login.start()
        self.assertEqual(result["status"], "error")
        self.callback.close.assert_called_once()

    def test_close_releases_pending_callback(self):
        self.login._callback = self.callback
        self.login._pending = {"url": "https://auth.openai.com/oauth/authorize?state=good", "state": "good", "created": 0}
        self.login.close()
        self.assertIsNone(self.login._pending)
        self.callback.close.assert_called_once()

    def test_poll_failure_clears_dead_pending_link(self):
        self.login._callback = self.callback
        self.login._pending = {"url": "https://auth.openai.com/oauth/authorize?state=good",
                               "state": "good", "created": codex_login.time.monotonic()}
        with mock.patch.object(self.login, "_read_metadata", side_effect=RuntimeError("offline")):
            result = self.login.status()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(self.login._pending)
        self.callback.close.assert_called_once()

    def test_slow_start_has_time_budget_and_does_not_stop_after_one_second(self):
        clock = [0.0]
        def pause(delay):
            clock[0] += delay
        with mock.patch("codex_login.time.monotonic", side_effect=lambda: clock[0]), \
             mock.patch("codex_login.time.sleep", side_effect=pause), \
             mock.patch.object(self.login, "_port_open", side_effect=lambda: clock[0] >= 2), \
             mock.patch.object(self.login, "_management_works", return_value=True):
            self.assertTrue(self.login._wait_ready({}))
        clock[0] = 0
        with mock.patch("codex_login.time.monotonic", side_effect=lambda: clock[0]), \
             mock.patch("codex_login.time.sleep", side_effect=pause), \
             mock.patch.object(self.login, "_port_open", return_value=False):
            self.assertFalse(self.login._wait_ready({}))
        self.assertLess(clock[0], 16)

    def test_windows_acl_is_applied_with_argument_array(self):
        self.acl_patch.stop()
        self.login.runtime_dir.mkdir()
        completed = mock.Mock(returncode=0)
        with mock.patch("codex_login.os.name", "nt"), \
             mock.patch.dict("codex_login.os.environ", {"USERDOMAIN": "ACME", "USERNAME": "Dev"}, clear=False), \
             mock.patch("codex_login.subprocess.run", return_value=completed) as run:
            self.login._lock_new_runtime_dir()
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["icacls", str(self.login.runtime_dir), "/inheritance:r", "/grant:r", "ACME\\Dev:(OI)(CI)F"])


if __name__ == "__main__":
    unittest.main()
