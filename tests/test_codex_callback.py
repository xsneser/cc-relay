"""Synthetic OAuth callbacks only: never exchanges real authorization codes."""
import http.client
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_callback import LoopbackCallback


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.forward = mock.Mock(return_value={"status": "ok"})
        self.callback = LoopbackCallback("test-state", self.forward, port=0)
        self.callback.start()
        self.addCleanup(self.callback.close)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.callback.port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_binds_loopback_and_forwards_once_without_disclosure(self):
        self.assertEqual(self.callback._server.server_address[0], "127.0.0.1")
        path = "/auth/callback?state=test-state&code=fake-code"
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("fake-code", body)
        self.forward.assert_called_once_with({"provider": "codex", "state": "test-state", "code": "fake-code", "error": ""})
        self.assertEqual(self.request(path)[0], 409)
        self.assertEqual(self.forward.call_count, 1)

    def test_invalid_or_foreign_callbacks_cannot_forward(self):
        for path in ("/auth/callback?state=wrong&code=x", "/other?state=test-state&code=x",
                     "/auth/callback?state=test-state&state=wrong&code=x", "/auth/callback?state=test-state",
                     "/auth/callback?state=test-state&code=x&code=y"):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/auth/callback?state=test-state&code=x", {"Host": "evil.example"})[0], 400)
        self.forward.assert_not_called()

    def test_forward_failure_is_safe_and_retryable(self):
        self.forward.side_effect = RuntimeError("secret")
        status, _, body = self.request("/auth/callback?state=test-state&code=fake")
        self.assertEqual(status, 502)
        self.assertNotIn("secret", body)
        self.assertFalse(self.callback._consumed)

    def test_close_is_idempotent_and_releases_port(self):
        port = self.callback.port
        self.callback.close()
        self.callback.close()
        with socket.socket() as check:
            self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)


if __name__ == "__main__":
    unittest.main()
