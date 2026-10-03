"""Exercise dashboard status coalescing without a browser or network."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class UIPollingTests(unittest.TestCase):
    def test_status_and_config_fetch_use_shorter_timeouts_than_mutations(self):
        html = (Path(__file__).resolve().parents[1] / "ui.html").read_text(encoding="utf-8")
        self.assertIn("await api('/api/status', undefined, 8000)", html)
        self.assertIn("await api('/api/config', undefined, 4000)", html)
        self.assertIn("defaultTimeoutMs = body ? 60000 : 20000", html)

    def test_voice_status_exposes_startup_phase_and_elapsed_time(self):
        html = (Path(__file__).resolve().parents[1] / "ui.html").read_text(encoding="utf-8")
        self.assertIn("const startup = vstat.startup || {}", html)
        self.assertIn("startup.elapsed_seconds", html)
        self.assertIn("checking_dependencies: '检查依赖'", html)
        self.assertIn("model_loading: '加载语音模型'", html)
        self.assertIn("启动较慢，仍在等待", html)
        self.assertIn("当前阶段:", html)

    def test_status_requests_are_shared_and_recover_after_failure(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        html = (Path(__file__).resolve().parents[1] / "ui.html").read_text(encoding="utf-8")
        start = html.index("let _statusRequest = null;")
        end = html.index("\nfunction renderStatus()", start)
        script = r'''
const assert = require('node:assert/strict');
let st, calls = 0, rendered = 0, traffic = 0, resolveStatus, rejectStatus;
const nodes = { '#routedot': {}, '#tf-since': {}, '#codex-status': {}, '#gemini-status': {}, '#voice-status': {} };
const $ = key => nodes[key];
const api = () => { calls++; return new Promise((resolve, reject) => {
  resolveStatus = resolve; rejectStatus = reject;
}); };
const renderStatus = () => { rendered++; };
const loadTraffic = async snapshot => { assert.equal(snapshot, st); traffic++; };
''' + html[start:end] + r'''
(async () => {
  const first = refresh();
  assert.equal(refresh(), first);
  assert.equal(calls, 1);
  resolveStatus({ rows: [], antigravity_up: true });
  await first;
  assert.equal(rendered, 1);
  assert.equal(traffic, 1);
  const previous = st;
  const failed = refresh();
  const originalError = console.error;
  console.error = () => {};
  rejectStatus(new Error('offline'));
  await failed;
  console.error = originalError;
  assert.equal(st, previous);
  assert.equal(nodes['#routedot'].className, 'dot');
  assert.ok(nodes['#tf-since'].textContent);
  const retry = refresh();
  resolveStatus({ rows: [{ req: 2 }] });
  await retry;
  assert.equal(calls, 3);
  assert.equal(rendered, 2);
  assert.equal(traffic, 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-"], input=script, encoding="utf-8",
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(html, r"setInterval\(loadTraffic,")
        self.assertIn("await loadTraffic(st)", html)


if __name__ == "__main__":
    unittest.main()
