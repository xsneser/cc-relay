# -*- coding: utf-8 -*-
"""Comprehensive tests for one-click restart mechanism and endpoint."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_relay
import main_launcher


class RestartUnitTests(unittest.TestCase):
    def test_restart_script_path(self):
        p = cc_relay._restart_script_path()
        self.assertTrue(p.endswith("restart_relay.bat"))
        self.assertEqual(os.path.basename(p), "restart_relay.bat")

    def test_spawn_restart_process_missing_script(self):
        with mock.patch("os.path.isfile", return_value=False):
            self.assertFalse(cc_relay._spawn_restart_process())

    def test_spawn_restart_process_success(self):
        with mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.Popen") as mock_popen:
            self.assertTrue(cc_relay._spawn_restart_process())
            mock_popen.assert_called_once()
            called_args = mock_popen.call_args[0][0]
            called_env = mock_popen.call_args[1].get("env") or {}
            self.assertEqual(called_env.get("CC_RELAY_NO_BROWSER"), "1")
            self.assertEqual(called_env.get("CC_RELAY_RESTART"), "1")
            if sys.platform == "win32":
                self.assertIn("restart_relay.bat", " ".join(called_args))

    def test_create_exclusive_server_success(self):
        srv = cc_relay.create_exclusive_server(("127.0.0.1", 0), cc_relay.UIHandler, max_wait_sec=2.0)
        try:
            self.assertIsNotNone(srv)
            self.assertGreater(srv.server_port, 0)
        finally:
            srv.server_close()

    def test_create_exclusive_server_retry_on_addr_in_use(self):
        calls = []
        dummy_srv = mock.MagicMock()

        def fake_init(addr, handler):
            calls.append(addr)
            if len(calls) == 1:
                err = OSError(10048, "Address already in use")
                err.winerror = 10048
                raise err
            return dummy_srv

        with mock.patch("relay.handlers.ui_handler.ExclusiveThreadingHTTPServer", side_effect=fake_init):
            srv = cc_relay.create_exclusive_server(("127.0.0.1", 9999), cc_relay.UIHandler, max_wait_sec=2.0)
            self.assertEqual(srv, dummy_srv)
            self.assertEqual(len(calls), 2)

    def test_create_exclusive_server_timeout(self):
        def fake_init(addr, handler):
            err = OSError(10048, "Address already in use")
            err.winerror = 10048
            raise err

        with mock.patch("relay.handlers.ui_handler.ExclusiveThreadingHTTPServer", side_effect=fake_init):
            with self.assertRaises(OSError) as ctx:
                cc_relay.create_exclusive_server(("127.0.0.1", 9999), cc_relay.UIHandler, max_wait_sec=0.3)
            self.assertIn("10048", str(ctx.exception))

    def test_create_exclusive_server_non_retry_error(self):
        def fake_init(addr, handler):
            raise PermissionError("Access denied")

        with mock.patch("relay.handlers.ui_handler.ExclusiveThreadingHTTPServer", side_effect=fake_init):
            with self.assertRaises(PermissionError):
                cc_relay.create_exclusive_server(("127.0.0.1", 9999), cc_relay.UIHandler, max_wait_sec=2.0)

    def test_schedule_restart_single_flight(self):
        with mock.patch("threading.Thread") as mock_thread:
            with mock.patch.object(cc_relay, "_RESTART_QUEUED", False):
                self.assertTrue(cc_relay.schedule_restart())
                self.assertTrue(cc_relay._RESTART_QUEUED)
                mock_thread.assert_called_once()

                # Second call should be a no-op (still returns True, doesn't spawn second thread)
                mock_thread.reset_mock()
                self.assertTrue(cc_relay.schedule_restart())
                mock_thread.assert_not_called()

    def test_stats_snapshot_contains_instance_id(self):
        st = cc_relay.stats_snapshot()
        self.assertIn("instance_id", st)
        self.assertTrue(isinstance(st["instance_id"], str))
        self.assertGreater(len(st["instance_id"]), 0)

    def test_voice_probes_fail_fast_on_closed_port(self):
        closed_conf = {"tools": {"voice": {"port": 18498}}}
        t0 = time.time()
        self.assertFalse(cc_relay.voice_up(closed_conf))
        stat = cc_relay.voice_status_dict(closed_conf)
        self.assertEqual(stat["status"], "stopped")
        self.assertFalse(stat["ready"])
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.5, f"voice probes on closed port took too long: {elapsed:.2f}s")

    def test_voice_upstream_health_offline_short_circuit(self):
        with mock.patch("cc_relay.voice_status_dict") as mock_dict:
            res = cc_relay.get_upstream_health({}, "voice", running=False)
            self.assertEqual(res["state"], "offline")
            self.assertFalse(res["running"])
            mock_dict.assert_not_called()


class RestartLauncherTests(unittest.TestCase):
    def test_launcher_respects_no_browser_env(self):
        # When CC_RELAY_NO_BROWSER is "1", webbrowser.open must not be called
        with mock.patch.dict(os.environ, {"CC_RELAY_NO_BROWSER": "1"}), \
             mock.patch("webbrowser.open") as mock_open, \
             mock.patch("main_launcher.is_port_busy", return_value=True), \
             self.assertRaises(SystemExit):
            main_launcher.main()
        mock_open.assert_not_called()


class RestartEndpointSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.conf_path = os.path.join(cls.temp_dir.name, "config.json")
        cls.records_path = os.path.join(cls.temp_dir.name, "records.jsonl")

        with open(cls.conf_path, "w", encoding="utf-8") as f:
            json.dump({
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "ui_port": 0,
                "fake_api_key": "sk-test",
            }, f)

        cls.orig_conf = cc_relay.CONF
        cls.orig_records = cc_relay.RECORDS
        cc_relay.CONF = cls.conf_path
        cc_relay.RECORDS = cls.records_path

        # Isolated UI server on ephemeral port (port 0)
        cls.server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), cc_relay.UIHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cc_relay.CONF = cls.orig_conf
        cc_relay.RECORDS = cls.orig_records
        cls.temp_dir.cleanup()

    def _post(self, path, payload=b"{}", headers=None):
        hdrs = {
            "Content-Type": "application/json",
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-CC-Relay-UI": "1",
        }
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=payload, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                h = {k.lower(): v for k, v in resp.headers.items()} if resp.headers else {}
                return resp.status, h, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read()
            h = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            return e.code, h, parsed

    def test_restart_rejected_missing_ui_header(self):
        # Missing X-CC-Relay-UI header -> 403
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, _, res = self._post("/api/restart", headers={"X-CC-Relay-UI": "0"})
            self.assertEqual(st, 403)
            mock_sched.assert_not_called()

    def test_restart_rejected_cross_origin(self):
        # Cross origin -> 403
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, _, res = self._post("/api/restart", headers={"Origin": "http://evil.com"})
            self.assertEqual(st, 403)
            mock_sched.assert_not_called()

    def test_restart_rejected_non_loopback_host(self):
        # Host header spoofing -> 403
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, _, res = self._post("/api/restart", headers={"Host": f"evil.com:{self.port}"})
            self.assertEqual(st, 403)
            mock_sched.assert_not_called()

    def test_restart_rejected_invalid_json(self):
        # Invalid JSON body -> 400
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, _, res = self._post("/api/restart", payload=b"{bad-json")
            self.assertEqual(st, 400)
            mock_sched.assert_not_called()

    def test_restart_rejected_oversized_body(self):
        # Body > 1024 bytes -> 400
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, _, res = self._post("/api/restart", payload=b"{" + b"a" * 2000 + b"}")
            self.assertEqual(st, 400)
            mock_sched.assert_not_called()

    def test_restart_success(self):
        # Valid request -> 200, schedule_restart is called
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, hdrs, res = self._post("/api/restart")
            self.assertEqual(st, 200)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("message"), "cc-relay restarting...")
            self.assertEqual(hdrs.get("cache-control"), "no-store")
            mock_sched.assert_called_once()

    def test_restart_allowed_loopback_aliases(self):
        # Origin http://localhost:<port> with Host 127.0.0.1:<port> should be accepted
        with mock.patch("cc_relay.schedule_restart") as mock_sched:
            st, hdrs, res = self._post("/api/restart", headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": f"http://localhost:{self.port}",
            })
            self.assertEqual(st, 200)
            self.assertTrue(res.get("ok"))
            mock_sched.assert_called_once()

    def test_ping_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/ping")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("instance_id"), cc_relay._PROCESS_INSTANCE_ID)
            self.assertIn("traffic_paused", data)
            self.assertFalse(data.get("traffic_paused"))


if __name__ == "__main__":
    unittest.main()
