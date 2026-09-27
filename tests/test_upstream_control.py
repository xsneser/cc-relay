import http.client
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_relay


class UpstreamControlEndpointTests(unittest.TestCase):
    def setUp(self):
        self.conf = {"antigravity_exe": "C:\\Antigravity Tools\\antigravity-tools.exe"}
        self.patches = [
            mock.patch.object(cc_relay, "load_conf", return_value=self.conf),
            mock.patch.object(cc_relay, "probe_upstream", return_value={"available": False}),
            mock.patch.object(cc_relay, "codex_start", return_value="started"),
            mock.patch.object(cc_relay, "codex_stop", return_value="stopped"),
            mock.patch.object(cc_relay, "antigravity_start", return_value="started"),
            mock.patch.object(cc_relay, "antigravity_stop", return_value="stopped"),
            mock.patch.object(cc_relay, "voice_start", return_value="started"),
            mock.patch.object(cc_relay, "voice_stop", return_value="stopped"),
            mock.patch.object(cc_relay, "voice_status_dict", return_value={"status": "ready"}),
        ]
        self.mocks = [patch.start() for patch in self.patches]
        self.codex_start, self.codex_stop = self.mocks[2:4]
        self.gemini_start, self.gemini_stop = self.mocks[4:6]
        self.voice_start, self.voice_stop = self.mocks[6:8]
        self.server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), cc_relay.UIHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.host = "127.0.0.1:" + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        for patch in reversed(self.patches):
            patch.stop()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        fields = {
            "Origin": "http://" + self.host,
            "Content-Type": "application/json",
            "X-CC-Relay-UI": "1",
        }
        fields.update(headers or {})
        payload = json.dumps(body or {})
        try:
            connection.request("POST", path, body=payload, headers=fields)
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_each_provider_has_independent_actions(self):
        cases = [
            ({"name": "codex", "action": "start"}, self.codex_start),
            ({"name": "codex", "action": "stop"}, self.codex_stop),
            ({"name": "gemini", "action": "start"}, self.gemini_start),
            ({"name": "gemini", "action": "stop"}, self.gemini_stop),
            ({"name": "voice", "action": "start"}, self.voice_start),
            ({"name": "voice", "action": "stop"}, self.voice_stop),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                status, data = self.request("/api/upstream", payload)
                self.assertEqual(status, 200)
                self.assertEqual(data["result"], "started" if payload["action"] == "start" else "stopped")
                expected.assert_called_once()
                expected.reset_mock()

    def test_antigravity_alias_stops_through_same_handler(self):
        status, data = self.request("/api/upstream", {"name": "antigravity", "action": "stop"})
        self.assertEqual(status, 200)
        self.assertEqual(data["result"], "stopped")
        self.gemini_stop.assert_called_once_with()

    def test_write_controls_require_local_ui_header(self):
        status, _ = self.request(
            "/api/upstream", {"name": "gemini", "action": "stop"},
            headers={"X-CC-Relay-UI": ""},
        )
        self.assertEqual(status, 403)
        self.gemini_stop.assert_not_called()

    def test_legacy_proxy_control_is_protected(self):
        status, _ = self.request(
            "/api/proxy", {"action": "stop"},
            headers={"X-CC-Relay-UI": ""},
        )
        self.assertEqual(status, 403)
        self.codex_stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
