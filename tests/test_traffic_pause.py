# -*- coding: utf-8 -*-
"""Comprehensive tests for traffic pause / resume gate."""

import http.server
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_relay


class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length) if length else b""
        self.server.request_count += 1
        resp = json.dumps({
            "id": "msg_mock_01",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello from upstream"}],
            "model": "deepseek-flash",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class TrafficPauseUnitTests(unittest.TestCase):
    def test_traffic_paused_helper(self):
        self.assertFalse(cc_relay.traffic_paused(None))
        self.assertFalse(cc_relay.traffic_paused({}))
        self.assertFalse(cc_relay.traffic_paused({"traffic_paused": False}))
        self.assertFalse(cc_relay.traffic_paused({"traffic_paused": "true"}))  # strict bool check
        self.assertFalse(cc_relay.traffic_paused({"traffic_paused": 1}))
        self.assertTrue(cc_relay.traffic_paused({"traffic_paused": True}))


class TrafficPauseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Start mock upstream server on ephemeral port
        cls.upstream_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.upstream_server.request_count = 0
        cls.upstream_port = cls.upstream_server.server_address[1]
        cls.upstream_thread = threading.Thread(target=cls.upstream_server.serve_forever, daemon=True)
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream_server.shutdown()
        cls.upstream_server.server_close()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conf_path = os.path.join(self.temp_dir.name, "config.json")
        self.records_path = os.path.join(self.temp_dir.name, "records.jsonl")

        self.fake_key = "sk-test-local-key"
        self.config_data = {
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "ui_port": 0,
            "fake_api_key": self.fake_key,
            "real_deepseek_key": "sk-fake-ds-key",
            "traffic_paused": False,
            "upstreams": {
                "deepseek": {
                    "base": f"http://127.0.0.1:{self.upstream_port}",
                    "key_env": "real_deepseek_key",
                    "proxy_url": "direct",
                }
            },
            "router": {
                "route": "deepseek",
                "model": "deepseek-flash",
                "tiers": {"main": "deepseek-flash"},
            },
        }
        with open(self.conf_path, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f, ensure_ascii=False, indent=2)

        # Monkey-patch global paths in cc_relay
        self.orig_conf = cc_relay.CONF
        self.orig_records = cc_relay.RECORDS
        cc_relay.CONF = self.conf_path
        cc_relay.RECORDS = self.records_path

        with cc_relay._STATS_LOCK:
            cc_relay._STATS_CACHE.update({"fingerprint": None, "rows": None, "total": 0})
        with cc_relay._CALLS_LOCK:
            cc_relay._CALLS_CACHE.clear()

        # Start isolated UI server on ephemeral port
        self.ui_server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), cc_relay.UIHandler)
        self.ui_port = self.ui_server.server_address[1]
        self.ui_thread = threading.Thread(target=self.ui_server.serve_forever, daemon=True)
        self.ui_thread.start()

        # Start isolated Relay server on ephemeral port
        self.relay_server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), cc_relay.Relay)
        self.relay_server.conf = self.config_data
        self.relay_server.reload_conf = lambda: cc_relay.load_conf()
        self.relay_port = self.relay_server.server_address[1]
        self.relay_thread = threading.Thread(target=self.relay_server.serve_forever, daemon=True)
        self.relay_thread.start()

    def tearDown(self):
        self.relay_server.shutdown()
        self.relay_server.server_close()
        self.ui_server.shutdown()
        self.ui_server.server_close()

        cc_relay.CONF = self.orig_conf
        cc_relay.RECORDS = self.orig_records
        self.temp_dir.cleanup()

    def _post_json(self, url, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()} if resp.headers else {}
                return resp.status, hdrs, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return e.code, hdrs, parsed

    def _get_json(self, url, headers=None):
        hdrs = {}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()} if resp.headers else {}
                return resp.status, hdrs, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return e.code, hdrs, parsed

    def test_status_returns_traffic_paused(self):
        status, _, data = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertEqual(status, 200)
        self.assertIn("traffic_paused", data)
        self.assertFalse(data["traffic_paused"])

    def test_api_traffic_control(self):
        # 1. Invalid payload: missing 'paused'
        status, _, err = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {})
        self.assertEqual(status, 400)

        # 2. Invalid payload: non-boolean
        status, _, err = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": "yes"})
        self.assertEqual(status, 400)

        status, _, err = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": 1})
        self.assertEqual(status, 400)

        # 3. Pause traffic
        status, _, res = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("traffic_paused"))
        self.assertTrue(res.get("changed"))

        # Verify config on disk
        with open(self.conf_path, "r", encoding="utf-8") as f:
            disk_conf = json.load(f)
        self.assertTrue(disk_conf.get("traffic_paused"))

        # Verify /api/status shows paused
        _, _, st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertTrue(st.get("traffic_paused"))

        # 4. Idempotent call with same state (changed should be False)
        status, _, res2 = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})
        self.assertEqual(status, 200)
        self.assertTrue(res2.get("ok"))
        self.assertTrue(res2.get("traffic_paused"))
        self.assertFalse(res2.get("changed"))

        # 5. Resume traffic
        status, _, res3 = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": False})
        self.assertEqual(status, 200)
        self.assertTrue(res3.get("ok"))
        self.assertFalse(res3.get("traffic_paused"))
        self.assertTrue(res3.get("changed"))

        with open(self.conf_path, "r", encoding="utf-8") as f:
            disk_conf = json.load(f)
        self.assertFalse(disk_conf.get("traffic_paused"))

    def test_relay_pause_interception_and_records(self):
        msg_payload = {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
        }
        auth_hdr = {"x-api-key": self.fake_key}

        # Step 1: Normal traffic flows when unpaused
        count_before = self.upstream_server.request_count
        status, _, res = self._post_json(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            msg_payload,
            headers=auth_hdr,
        )
        self.assertEqual(status, 200)
        self.assertEqual(res.get("role"), "assistant")
        self.assertEqual(self.upstream_server.request_count, count_before + 1)

        # Step 2: Pause traffic via API
        status, _, _ = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})
        self.assertEqual(status, 200)

        # Step 3: Paused behavior
        # 3a. Invalid auth still returns 401
        bad_auth_status, _, bad_auth_res = self._post_json(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            msg_payload,
            headers={"x-api-key": "invalid-key"},
        )
        self.assertEqual(bad_auth_status, 401)

        # 3b. Local /v1/models is still allowed through
        models_status, _, models_res = self._get_json(
            f"http://127.0.0.1:{self.relay_port}/v1/models",
            headers=auth_hdr,
        )
        self.assertEqual(models_status, 200)
        self.assertIn("data", models_res)

        # 3c. /v1/messages is intercepted and returns 503
        count_during = self.upstream_server.request_count
        pause_status, pause_headers, pause_res = self._post_json(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            msg_payload,
            headers=auth_hdr,
        )
        self.assertEqual(pause_status, 503)
        self.assertEqual(pause_headers.get("x-cc-relay-traffic-paused"), "1")
        self.assertEqual(pause_headers.get("cache-control"), "no-store")
        self.assertIn("error", pause_res)
        self.assertEqual(pause_res["error"]["type"], "api_error")
        self.assertIn("paused by the operator", pause_res["error"]["message"])

        # Upstream request count MUST NOT increase during pause
        self.assertEqual(self.upstream_server.request_count, count_during)

        # 3d. Verify audit log entry for paused request
        records = cc_relay.read_records()
        self.assertTrue(len(records) >= 2)
        paused_records = [r for r in records if r.get("route") == "paused"]
        self.assertEqual(len(paused_records), 1)
        pr = paused_records[0]
        self.assertEqual(pr.get("resp_status"), 503)
        self.assertEqual(pr.get("route_reason"), "traffic_paused")
        self.assertTrue(pr.get("traffic_paused"))
        self.assertEqual(pr.get("orig_model"), "deepseek-flash")
        self.assertIsNone(pr.get("sent_model"))
        self.assertIsNone(pr.get("upstream"))
        clean_hdrs = {k.lower(): v for k, v in pr.get("headers", {}).items()}
        self.assertEqual(clean_hdrs.get("x-api-key"), "<redacted>")

        # 3e. Verify calls snapshot includes traffic_paused flag
        _, _, calls_data = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/calls")
        paused_calls = [c for c in calls_data.get("calls", []) if c.get("route") == "paused"]
        self.assertEqual(len(paused_calls), 1)
        self.assertTrue(paused_calls[0].get("traffic_paused"))
        self.assertEqual(paused_calls[0].get("status"), 503)

        # Step 4: Resume traffic
        status, _, _ = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": False})
        self.assertEqual(status, 200)

        # Step 5: Traffic flows normally again
        count_after = self.upstream_server.request_count
        resume_status, _, resume_res = self._post_json(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            msg_payload,
            headers=auth_hdr,
        )
        self.assertEqual(resume_status, 200)
        self.assertEqual(resume_res.get("role"), "assistant")
        self.assertEqual(self.upstream_server.request_count, count_after + 1)


if __name__ == "__main__":
    unittest.main()
