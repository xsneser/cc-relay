#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security and functionality tests for relay update API endpoints."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_relay
import relay_updater


class RelayUpdateAPITests(unittest.TestCase):
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

    def _get(self, path, headers=None):
        hdrs = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Sec-Fetch-Site": "same-origin",
        }
        if headers:
            hdrs.update(headers)
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers=hdrs)
        return urllib.request.urlopen(req, timeout=5)

    def _post(self, path, payload=b"{}", headers=None):
        hdrs = {
            "Content-Type": "application/json",
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-CC-Relay-UI": "1",
            "Sec-Fetch-Site": "same-origin",
        }
        if headers:
            hdrs.update(headers)
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def test_stats_snapshot_contains_relay_update(self):
        with self._get("/api/status") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertIn("relay_update", data)
            ru = data["relay_update"]
            self.assertIn("is_git", ru)
            self.assertIn("current_version", ru)
            self.assertIn("has_update", ru)

    def test_get_relay_version_endpoint(self):
        with self._get("/api/relay/version") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertIn("is_git", data)
            self.assertIn("branch", data)
            self.assertIn("latest_version", data)

    def test_post_relay_check_rejects_missing_ui_header(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/relay/check", payload=b"{}", headers={"X-CC-Relay-UI": "0"})
        self.assertEqual(ctx.exception.code, 403)

    def test_post_relay_check_triggers_check(self):
        with mock.patch("relay_updater.RelayUpdater.request_check") as mock_check:
            mock_check.return_value = {"status": "ok", "checking": True}
            with self._post("/api/relay/check", payload=b"{}") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data.get("status"), "ok")
                mock_check.assert_called_once()

    def test_post_relay_update_triggers_update(self):
        with mock.patch("relay_updater.RelayUpdater.request_update") as mock_update:
            mock_update.return_value = {"status": "updating", "update_state": "queued"}
            with self._post("/api/relay/update", payload=b"{}") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data.get("update_state"), "queued")
                mock_update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
