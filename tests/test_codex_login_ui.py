"""Local OAuth endpoint isolation and UI state regressions; no real credentials."""
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_relay


class LoginEndpointTests(unittest.TestCase):
    def setUp(self):
        self.login = mock.Mock()
        self.login.start.return_value = {"status": "waiting", "url": "https://auth.openai.com/oauth/authorize"}
        self.login.status.return_value = {"status": "idle", "account_count": 0}
        patch = mock.patch.object(cc_relay, "get_codex_login", return_value=self.login)
        patch.start()
        self.addCleanup(patch.stop)
        self.server = cc_relay.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), cc_relay.UIHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.host = "127.0.0.1:" + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method="POST", path="/api/codex/login", headers=None, body="{}"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        fields = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-CC-Relay-UI": "1"}
        fields.update(headers or {})
        try:
            connection.request(method, path, body=body if method == "POST" else None, headers=fields)
            result = connection.getresponse()
            return result.status, dict(result.getheaders()), json.loads(result.read())
        finally:
            connection.close()

    def test_start_and_status_are_no_store(self):
        code, headers, data = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(data["status"], "waiting")
        self.login.start.assert_called_once_with()
        code, headers, data = self.request("GET", "/api/codex/login/status")
        self.assertEqual(code, 200)
        self.assertEqual(data["account_count"], 0)
        self.login.status.assert_called_once_with()

    def test_cross_origin_and_rebinding_are_rejected(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"},
                        {"X-CC-Relay-UI": ""}, {"Content-Type": "text/plain"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Host": "localhost:1"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.login.start.assert_not_called()

    def test_cross_site_status_cannot_read_authorization_url(self):
        code, _, _ = self.request("GET", "/api/codex/login/status", {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        self.login.status.assert_not_called()

    def test_bounded_and_validated_request_body(self):
        for body in ("x" * 1025, "[]", "not-json"):
            self.assertEqual(self.request(body=body)[0], 400)
        self.login.start.assert_not_called()

    def test_internal_exception_is_not_disclosed(self):
        self.login.start.side_effect = RuntimeError("private-management-secret")
        code, _, data = self.request()
        self.assertEqual(code, 500)
        self.assertNotIn("private-management-secret", json.dumps(data))


class LoginBrowserLogicTests(unittest.TestCase):
    def test_popup_fallback_singleflight_and_state_recovery(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        html = (Path(__file__).resolve().parents[1] / "ui.html").read_text(encoding="utf-8")
        start = html.index("let _configDirty = false, _configLoaded = false;")
        end = html.index("function appendCodexLogin(card)", start)
        script = r'''
const assert = require('node:assert/strict');
const $ = () => null;
let calls = 0, resolveRequest, alerts = 0, opens = 0, closed = 0;
const toast = () => { alerts++; };
let popupBlocked = false;
const window = { open: () => { opens++; return popupBlocked ? null : {
  opener: {}, closed: false, close: () => { closed++; }, location: { replace: url => { assert.ok(url.startsWith('https://auth.openai.com/')); } }
}; } };
const api = () => { calls++; return new Promise(resolve => { resolveRequest = resolve; }); };
''' + html[start:end] + r'''
(async () => {
  assert.equal(codexAuthURL('https://evil.example/'), '');
  assert.equal(codexAuthURL('https://auth.openai.com.evil.example/'), '');
  assert.equal(codexAuthURL('https://user:pass@auth.openai.com/'), '');
  assert.equal(codexAuthURL('http://auth.openai.com/'), '');
  _configDirty = true;
  await startCodexLogin();
  assert.equal(opens, 0); assert.equal(alerts, 1);
  _configDirty = false;
  const stale = refreshCodexLogin();
  assert.equal(refreshCodexLogin(), stale);
  const resolveStale = resolveRequest;
  const login = startCodexLogin();
  await startCodexLogin();
  assert.equal(calls, 2); assert.equal(opens, 1);
  resolveRequest({status:'waiting',url:'https://auth.openai.com/oauth/authorize?state=test'});
  await login;
  resolveStale({status:'idle'}); await stale;
  assert.equal(_codexLogin.status, 'waiting');
  const done = refreshCodexLogin(); resolveRequest({status:'ok',account_count:1}); await done;
  assert.equal(_codexLogin.status, 'ok');
  popupBlocked = true;
  const second = startCodexLogin();
  resolveRequest({status:'waiting',url:'https://auth.openai.com/oauth/authorize?state=test2'}); await second;
  assert.ok(codexAuthURL(_codexLogin.url));
  _codexLogin = {status:'idle'}; clearTimeout(_codexLoginTimer);
  const invalid = startCodexLogin();
  resolveRequest({status:'waiting',url:'https://evil.example/'}); await invalid;
  assert.equal(_codexLogin.status, 'error');
  clearTimeout(_codexLoginTimer);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
