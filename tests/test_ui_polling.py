"""Exercise dashboard status coalescing without a browser or network."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class UIPollingTests(unittest.TestCase):
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
const nodes = { '#routedot': {}, '#tf-since': {} };
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
