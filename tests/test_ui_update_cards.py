#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for update progress cards behavior in ui.html.
Ensures that update cards:
1. Do not automatically display during silent background/startup checks.
2. Only display automatically when an update is genuinely detected.
3. Display when user triggers a manual update check.
"""
import os
import re
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(BASE_DIR, "ui.html")


class TestUIUpdateCards(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(UI_PATH), f"ui.html not found at {UI_PATH}")
        with open(UI_PATH, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_relay_progress_card_display_conditions(self):
        # Relay card should require isUpdating, isManual, or hasNewUpdate (not checking alone)
        self.assertIn("let _relayManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'fetching', 'applying'].includes(state);", self.html)
        self.assertIn("const isManual = !!_relayManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_cpa_progress_card_display_conditions(self):
        # CPA card should require isUpdating, isManual, or hasNewUpdate (not checking alone)
        self.assertIn("let _cpaManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'verifying', 'staged', 'installing', 'rolling_back'].includes(state);", self.html)
        self.assertIn("const isManual = !!_cpaManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_antigravity_progress_card_display_conditions(self):
        # Antigravity card should require isManual or hasNewUpdate (not checking alone)
        self.assertIn("let _antigravityManualCheck = false;", self.html)
        self.assertIn("const isManual = !!_antigravityManualCheck;", self.html)
        self.assertIn("const shouldShow = isManual || hasNewUpdate;", self.html)

    def test_auto_dismiss_timer_requires_shown_card(self):
        # Auto-dismiss timer should only attach if card currently has .show class
        # (avoiding silent auto-checks from creating/cancelling timers or popping up)
        self.assertIn("if (card.classList.contains('show'))", self.html)

    def test_manual_check_triggers_exist(self):
        # All three components must have manual check triggers
        self.assertIn('id="btn-relay-update"', self.html)
        self.assertIn('id="cpa-check-update"', self.html)
        self.assertIn('id="antigravity-check-update"', self.html)

        # Event listeners must set manual check flag and show the card
        self.assertIn("_relayManualCheck = true;", self.html)
        self.assertIn("_cpaManualCheck = true;", self.html)
        self.assertIn("_antigravityManualCheck = true;", self.html)

    def test_card_static_structure_and_component_names(self):
        # 1. HTML should contain component names in card titles
        self.assertIn('<div class="cpa-card-title" id="cpa-card-title">Codex (CLIProxyAPI)</div>', self.html)
        self.assertIn('<div class="cpa-card-title" id="relay-card-title">CC Relay</div>', self.html)
        self.assertIn('<div class="cpa-card-title" id="antigravity-card-title">Antigravity Tools</div>', self.html)

        # 2. HTML should contain status and version elements in subtitle container
        self.assertIn('id="cpa-card-status"', self.html)
        self.assertIn('id="cpa-card-ver"', self.html)
        self.assertIn('id="relay-card-status"', self.html)
        self.assertIn('id="relay-card-ver"', self.html)
        self.assertIn('id="antigravity-card-status"', self.html)
        self.assertIn('id="antigravity-card-ver"', self.html)

        # 3. CSS should style subtitle, status tag and dot separator
        self.assertIn(".cpa-card-sub", self.html)
        self.assertIn(".cpa-card-status", self.html)
        self.assertIn(".cpa-card-status.has-update", self.html)
        self.assertIn(".cpa-card-status:not(:empty) + .cpa-card-ver:not(:empty)::before", self.html)

    def test_card_renderers_preserve_identity_in_node(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available")

        # Extract renderer functions and run mock rendering in Node
        script = r'''
const assert = require('node:assert/strict');

function makeMockNode() {
  return {
    textContent: '',
    style: {},
    classList: {
      _classes: new Set(),
      add(c) { this._classes.add(c); },
      remove(c) { this._classes.delete(c); },
      toggle(c, v) { if (v) this._classes.add(c); else this._classes.delete(c); },
      contains(c) { return this._classes.has(c); }
    },
    addEventListener() {},
    setAttribute() {}
  };
}

const elements = {};
function getEl(id) {
  if (!elements[id]) elements[id] = makeMockNode();
  return elements[id];
}
const $ = sel => getEl(sel);

let _cpaCardTimer = null;
let _cpaFastPollTimer = null;
let _cpaCardDismissedVer = null;
let _cpaManualCheck = false;

let _relayCardTimer = null;
let _relayFastPollTimer = null;
let _relayCardDismissedVer = null;
let _relayManualCheck = false;

let _antigravityCardTimer = null;
let _antigravityFastPollTimer = null;
let _antigravityCardDismissedVer = null;
let _antigravityManualCheck = false;

let st = {};

global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.setInterval = () => 1;
global.clearInterval = () => {};


'''
        # Extract the 3 renderers from ui.html
        cpa_start = self.html.index("function renderCpaProgressCard(data) {")
        cpa_end = self.html.index("\nconst cpaCardClose", cpa_start)

        relay_start = self.html.index("function renderRelayProgressCard(data) {")
        relay_end = self.html.index("\nconst relayCardClose", relay_start)

        ag_start = self.html.index("function renderAntigravityProgressCard(data) {")
        ag_end = self.html.index("\nconst antigravityCardClose", ag_start)

        test_runner = r'''
// 1. Test CPA Progress Card
// Case A: Discovered update idle
renderCpaProgressCard({
  has_update: true,
  latest_version: '8.0.10',
  current_version: '8.0.9',
  update_state: 'idle'
});
assert.equal($('#cpa-card-title').textContent, 'Codex (CLIProxyAPI)', 'CPA title must be Codex (CLIProxyAPI)');
assert.equal($('#cpa-card-status').textContent, '发现新版本');
assert.equal($('#cpa-card-ver').textContent, 'v8.0.10');

// Case B: Downloading update (Screenshot 1 scenario)
renderCpaProgressCard({
  has_update: true,
  latest_version: '8.0.10',
  current_version: '8.0.9',
  update_state: 'downloading',
  download_progress: 28.8,
  downloaded_bytes: 6710886,
  total_bytes: 23173530
});
assert.equal($('#cpa-card-title').textContent, 'Codex (CLIProxyAPI)', 'CPA title must remain Codex (CLIProxyAPI) while downloading');
assert.equal($('#cpa-card-status').textContent, '正在下载');
assert.equal($('#cpa-card-ver').textContent, 'v8.0.10');

// Case C: Rolling back with error
renderCpaProgressCard({
  update_state: 'rolling_back',
  update_error: 'health check failed'
});
assert.equal($('#cpa-card-title').textContent, 'Codex (CLIProxyAPI)');
assert.equal($('#cpa-card-status').textContent, '正在回滚');

// 2. Test Relay Progress Card
renderRelayProgressCard({
  has_update: true,
  latest_version: '2.5.0',
  current_version: '2.4.9',
  update_state: 'idle'
});
assert.equal($('#relay-card-title').textContent, 'CC Relay', 'Relay title must be CC Relay');
assert.equal($('#relay-card-status').textContent, '发现新版本');
assert.equal($('#relay-card-ver').textContent, 'v2.5.0');

renderRelayProgressCard({
  has_update: true,
  latest_version: '2.5.0',
  current_version: '2.4.9',
  update_state: 'downloading'
});
assert.equal($('#relay-card-title').textContent, 'CC Relay');
assert.equal($('#relay-card-status').textContent, '正在下载');

// 3. Test Antigravity Tools Card (Screenshot 2 scenario)
renderAntigravityProgressCard({
  has_update: true,
  latest_version: '4.9.0',
  current_version: '4.8.9'
});
assert.equal($('#antigravity-card-title').textContent, 'Antigravity Tools', 'Antigravity title must be Antigravity Tools');
assert.equal($('#antigravity-card-status').textContent, '发现新版本');
assert.equal($('#antigravity-card-ver').textContent, 'v4.9.0');

renderAntigravityProgressCard({
  has_update: false,
  check_error: 'Connection refused'
});
assert.equal($('#antigravity-card-title').textContent, 'Antigravity Tools');
assert.equal($('#antigravity-card-status').textContent, '检查失败');

console.log('ALL_UPDATE_CARD_RENDER_TESTS_PASSED');
process.exit(0);
'''
        full_node_script = script + "\n" + self.html[cpa_start:cpa_end] + "\n" + self.html[relay_start:relay_end] + "\n" + self.html[ag_start:ag_end] + "\n" + test_runner
        res = subprocess.run([node, "-"], input=full_node_script, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Node render test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("ALL_UPDATE_CARD_RENDER_TESTS_PASSED", res.stdout)



if __name__ == "__main__":
    unittest.main()
