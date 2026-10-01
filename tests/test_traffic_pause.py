# -*- coding: utf-8 -*-
"""Comprehensive tests for traffic pause / resume gate with streaming keep-alive simulation."""

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
        raw = self.rfile.read(length) if length else b""
        self.server.request_count += 1

        is_stream = False
        try:
            body = json.loads(raw.decode("utf-8"))
            is_stream = bool(body.get("stream"))
        except Exception:
            pass

        if is_stream:
            sse_content = (
                b"event: message_start\ndata: {\"type\": \"message_start\", \"message\": {\"id\": \"msg_01\", \"role\": \"assistant\"}}\n\n"
                b"event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"delta\": {\"type\": \"text_delta\", \"text\": \"hello from stream\"}}\n\n"
                b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse_content)))
            self.end_headers()
            self.wfile.write(sse_content)
        else:
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

    def test_paused_waiting_counter_and_event(self):
        self.assertEqual(cc_relay._paused_waiting_count(), 0)
        cc_relay._paused_waiting_inc()
        self.assertEqual(cc_relay._paused_waiting_count(), 1)
        cc_relay._paused_waiting_dec()
        self.assertEqual(cc_relay._paused_waiting_count(), 0)
        # Verify no underflow
        cc_relay._paused_waiting_dec()
        self.assertEqual(cc_relay._paused_waiting_count(), 0)

        # Event sync
        cc_relay._sync_traffic_unpaused_event({"traffic_paused": True})
        self.assertFalse(cc_relay._TRAFFIC_UNPAUSED_EVENT.is_set())
        cc_relay._sync_traffic_unpaused_event({"traffic_paused": False})
        self.assertTrue(cc_relay._TRAFFIC_UNPAUSED_EVENT.is_set())


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
        cc_relay._sync_traffic_unpaused_event(self.config_data)

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
        cc_relay._TRAFFIC_UNPAUSED_EVENT.set()
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
        self.assertIn("paused_waiting_requests", data)
        self.assertEqual(data["paused_waiting_requests"], 0)

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
        self.assertFalse(cc_relay._TRAFFIC_UNPAUSED_EVENT.is_set())

        # Verify config on disk
        with open(self.conf_path, "r", encoding="utf-8") as f:
            disk_conf = json.load(f)
        self.assertTrue(disk_conf.get("traffic_paused"))

        # Verify /api/status and /api/ping show paused
        _, _, st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertTrue(st.get("traffic_paused"))
        _, _, ping_st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/ping")
        self.assertTrue(ping_st.get("traffic_paused"))

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
        self.assertTrue(cc_relay._TRAFFIC_UNPAUSED_EVENT.is_set())

        with open(self.conf_path, "r", encoding="utf-8") as f:
            disk_conf = json.load(f)
        self.assertFalse(disk_conf.get("traffic_paused"))
        _, _, ping_st2 = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/ping")
        self.assertFalse(ping_st2.get("traffic_paused"))

    def test_relay_pause_streaming_simulation_and_resume(self):
        msg_payload = {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            "stream": True,
        }
        auth_hdr = {"x-api-key": self.fake_key}

        # Step 1: Normal traffic flows when unpaused
        count_before = self.upstream_server.request_count
        req0 = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            data=json.dumps(msg_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **auth_hdr},
            method="POST",
        )
        with urllib.request.urlopen(req0) as r0:
            self.assertEqual(r0.status, 200)
            body0 = r0.read().decode("utf-8")
            self.assertIn("message_start", body0)
        self.assertEqual(self.upstream_server.request_count, count_before + 1)

        # Step 2: Pause traffic via API
        status, _, _ = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})
        self.assertEqual(status, 200)

        # Step 3: Bad auth check still returns 401 even when paused
        bad_auth_status, _, _ = self._post_json(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            msg_payload,
            headers={"x-api-key": "invalid-key"},
        )
        self.assertEqual(bad_auth_status, 401)

        # Step 4: Local /v1/models is still allowed through
        models_status, _, models_res = self._get_json(
            f"http://127.0.0.1:{self.relay_port}/v1/models",
            headers=auth_hdr,
        )
        self.assertEqual(models_status, 200)
        self.assertIn("data", models_res)

        # Step 5: Streaming request arrives during pause
        # It must receive 200 OK immediately with chunked transfer and event: ping
        count_during = self.upstream_server.request_count
        req1 = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}/v1/messages",
            data=json.dumps(msg_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **auth_hdr},
            method="POST",
        )
        r1 = urllib.request.urlopen(req1, timeout=10)
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.headers.get("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(r1.headers.get("Transfer-Encoding"), "chunked")
        self.assertEqual(r1.headers.get("X-CC-Relay-Traffic-Paused"), "1")

        # Read the first ping event from stream
        first_chunk = r1.readline()
        self.assertTrue(b"ping" in first_chunk or b"event" in first_chunk)

        # Upstream request count MUST NOT have increased yet!
        self.assertEqual(self.upstream_server.request_count, count_during)

        # Check /api/status shows waiting requests >= 1
        _, _, st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertEqual(st.get("paused_waiting_requests"), 1)

        # Step 6: Resume traffic
        status, _, _ = self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": False})
        self.assertEqual(status, 200)

        # Step 7: Read the remainder of the stream; it should receive upstream response
        rest = r1.read()
        r1.close()
        self.assertIn(b"hello from stream", rest)
        self.assertIn(b"message_stop", rest)

        # Upstream count must now have increased by 1!
        self.assertEqual(self.upstream_server.request_count, count_during + 1)

        # Check /api/status shows waiting requests back to 0
        _, _, st_after = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertEqual(st_after.get("paused_waiting_requests"), 0)

        # Verify records.jsonl
        records = cc_relay.read_records()
        self.assertTrue(len(records) >= 2)
        last_rec = records[-1]
        self.assertEqual(last_rec.get("resp_status"), 200)
        self.assertTrue(last_rec.get("traffic_paused"))
        self.assertIsNotNone(last_rec.get("paused_wait_sec"))

    def test_relay_pause_client_disconnect(self):
        # Pause traffic
        self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})

        msg_payload = {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "test disconnect"}],
            "stream": True,
        }
        body_bytes = json.dumps(msg_payload).encode("utf-8")

        # Connect directly via socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", self.relay_port))
        req_text = (
            f"POST /v1/messages HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.relay_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"x-api-key: {self.fake_key}\r\n"
            f"\r\n"
        ).encode("utf-8") + body_bytes
        s.sendall(req_text)

        # Read initial 200 response line
        res_line = s.recv(1024)
        self.assertIn(b"200 OK", res_line)

        # Close client socket immediately
        s.close()
        time.sleep(0.3)

        # Resume traffic and verify waiting requests is 0
        self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": False})
        _, _, st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertEqual(st.get("paused_waiting_requests"), 0)

        records = cc_relay.read_records()
        aborted_recs = [r for r in records if r.get("resp_status") == 499]
        self.assertTrue(len(aborted_recs) >= 1)
        self.assertEqual(aborted_recs[-1].get("route_reason"), "traffic_paused_aborted")

    def test_relay_pause_non_streaming(self):
        # Pause traffic
        self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": True})

        msg_payload = {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "non streaming test"}],
            "stream": False,
        }
        auth_hdr = {"x-api-key": self.fake_key}

        result = {}

        def _worker():
            status, _, res = self._post_json(
                f"http://127.0.0.1:{self.relay_port}/v1/messages",
                msg_payload,
                headers=auth_hdr,
            )
            result["status"] = status
            result["res"] = res

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        time.sleep(0.3)

        # Thread is still running and waiting for unpause
        self.assertTrue(t.is_alive())
        _, _, st = self._get_json(f"http://127.0.0.1:{self.ui_port}/api/status")
        self.assertEqual(st.get("paused_waiting_requests"), 1)

        # Unpause
        self._post_json(f"http://127.0.0.1:{self.ui_port}/api/traffic", {"paused": False})
        t.join(timeout=5.0)

        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("status"), 200)
        self.assertEqual(result.get("res", {}).get("role"), "assistant")


if __name__ == "__main__":
    unittest.main()
