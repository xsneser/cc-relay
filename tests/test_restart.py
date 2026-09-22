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
    def test_powershell_quote(self):
        self.assertEqual(cc_relay._powershell_quote("simple"), "'simple'")
        self.assertEqual(cc_relay._powershell_quote("with space"), "'with space'")
        self.assertEqual(cc_relay._powershell_quote("with'quote"), "'with''quote'")
        self.assertEqual(cc_relay._powershell_quote(r"C:\Program Files\cc-relay"), r"'C:\Program Files\cc-relay'")

    def test_restart_launch_spec_frozen(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", r"C:\app\cc-relay.exe"):
            exe, args, cwd = cc_relay._restart_launch_spec()
            self.assertEqual(exe, r"C:\app\cc-relay.exe")
            self.assertEqual(args, [])
            self.assertEqual(cwd, r"C:\app")

    def test_restart_launch_spec_main_launcher(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
             mock.patch.object(sys, "executable", r"C:\python\python.exe"), \
             mock.patch.object(sys, "argv", [r"C:\repo\main_launcher.py"]):
            exe, args, cwd = cc_relay._restart_launch_spec()
            self.assertEqual(exe, r"C:\python\python.exe")
            self.assertEqual(args, [os.path.abspath(r"C:\repo\main_launcher.py")])
            self.assertEqual(cwd, os.path.dirname(os.path.abspath(r"C:\repo\main_launcher.py")))

    def test_restart_launch_spec_cc_relay(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
             mock.patch.object(sys, "executable", r"C:\python\python.exe"), \
             mock.patch.object(sys, "argv", [r"C:\repo\cc_relay.py", "serve"]):
            exe, args, cwd = cc_relay._restart_launch_spec()
            self.assertEqual(exe, r"C:\python\python.exe")
            self.assertEqual(args, [os.path.abspath(r"C:\repo\cc_relay.py"), "serve"])

    def test_build_restart_supervisor_command(self):
        spec = (r"C:\app\cc-relay.exe", [], r"C:\app")
        cmd = cc_relay._build_restart_supervisor_command(1234, spec)
        self.assertIn("Wait-Process -Id 1234", cmd)
        self.assertIn("Start-Sleep -Milliseconds 500", cmd)
        self.assertIn("Start-Process", cmd)
        self.assertIn(r"'C:\app\cc-relay.exe'", cmd)
        self.assertIn(r"-WorkingDirectory 'C:\app'", cmd)

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


if __name__ == "__main__":
    unittest.main()
