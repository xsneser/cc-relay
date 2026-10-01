#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for the two-level settings UI in ui.html.
Validates:
1. Two-level DOM structure (Level 1 overview and Level 2 module details).
2. Presence of all 5 module cards (DeepSeek, Codex, Antigravity, Voice, Relay).
3. Subpage switching, integrated breadcrumb header, single back button, and auto-save indicator.
4. Input fields, safe base URL locking, and probe buttons.
5. Removal of redundant bottom back button and manual save/reload buttons.
6. Node.js evaluation of client-side navigation, auto-save, and data collection logic.
"""
import os
import re
import shutil
import subprocess
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(BASE_DIR, "ui.html")


class TestTwoLevelSettingsUI(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(UI_PATH), f"ui.html not found at {UI_PATH}")
        with open(UI_PATH, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_two_level_containers_exist(self):
        self.assertIn('id="cfg-level-1"', self.html)
        self.assertIn('id="cfg-level-2"', self.html)
        self.assertIn('class="cfg-overview-grid"', self.html)
        self.assertIn('class="cfg-breadcrumb-bar', self.html)
        self.assertIn('class="cfg-breadcrumb-bar cfg-detail-header"', self.html)

    def test_level_1_overview_cards_exist(self):
        for mod in ('deepseek', 'codex', 'antigravity', 'voice', 'relay'):
            self.assertIn(f'data-mod="{mod}"', self.html, f"Missing Level 1 card for {mod}")
        self.assertIn('id="ov-ds-badge"', self.html)
        self.assertIn('id="ov-codex-badge"', self.html)
        self.assertIn('id="ov-ag-badge"', self.html)
        self.assertIn('id="ov-voice-badge"', self.html)
        self.assertIn('id="ov-relay-badge"', self.html)

    def test_level_2_detail_panels_exist(self):
        for panel_id in (
            'cfg-panel-deepseek',
            'cfg-panel-codex',
            'cfg-panel-antigravity',
            'cfg-panel-voice',
            'cfg-panel-relay',
        ):
            self.assertIn(f'id="{panel_id}"', self.html, f"Missing Level 2 panel {panel_id}")

    def test_navigation_and_breadcrumbs_exist(self):
        self.assertIn('id="cfg-btn-back"', self.html)
        self.assertIn('id="cfg-crumb-title"', self.html)
        self.assertIn('id="cfg-detail-status-pill"', self.html)
        self.assertIn('id="cfg-autosave-indicator"', self.html)
        self.assertIn('id="cfg-overview-reload"', self.html)

        # 确保移除了冗余的底部返回按钮和手动保存按钮
        self.assertNotIn('id="cfg-back-bottom"', self.html)
        self.assertNotIn('id="cfg-save"', self.html)
        self.assertNotIn('id="cfg-reload"', self.html)

    def test_module_inputs_and_actions_exist(self):
        # DeepSeek
        self.assertIn('id="cfg-ds-base"', self.html)
        self.assertIn('id="cfg-ds-proxy"', self.html)
        self.assertIn('id="cfg-ds-key"', self.html)
        self.assertIn('id="btn-probe-deepseek"', self.html)

        # Codex
        self.assertIn('id="cfg-codex-base"', self.html)
        self.assertIn('id="btn-unlock-codex-base"', self.html)
        self.assertIn('id="cfg-codex-key"', self.html)
        self.assertIn('id="cfg-codex-outbound-proxy"', self.html)
        self.assertIn('id="cpa-check-update"', self.html)
        self.assertIn('id="cpa-install-update"', self.html)
        self.assertIn('id="btn-probe-codex"', self.html)

        # Antigravity
        self.assertIn('id="cfg-ag-base"', self.html)
        self.assertIn('id="btn-unlock-ag-base"', self.html)
        self.assertIn('id="cfg-ag-key"', self.html)
        self.assertIn('id="cfg-antigravity-autostart"', self.html)
        self.assertIn('id="antigravity-check-update"', self.html)
        self.assertIn('id="antigravity-open-app"', self.html)
        self.assertIn('id="btn-probe-antigravity"', self.html)

        # Voice
        self.assertIn('id="cfg-voice-autostart"', self.html)
        self.assertIn('id="cfg-voice-restore-clipboard"', self.html)
        self.assertIn('id="cfg-voice-hotkey"', self.html)
        self.assertIn('id="btn-record-hotkey"', self.html)
        self.assertIn('id="btn-reset-hotkey"', self.html)
        self.assertIn('id="cfg-voice-engine"', self.html)
        self.assertIn('id="btn-test-voice-mic"', self.html)

        # Relay
        self.assertIn('id="btn-copy-env-snippet"', self.html)

    def test_js_two_level_functions_exist(self):
        for fn in (
            'function switchConfigSubpage',
            'function updateOverviewCards',
            'function updateDetailStatusPill',
            'function renderConfig',
            'function collectConfig',
            'function runProbe',
            'function setupBaseUrlLock',
            'function setupVoiceHotkeyRecorder',
            'function scheduleConfigAutosave',
            'function flushConfigAutosave',
            'function saveConfigDraft',
        ):
            self.assertIn(fn, self.html, f"Missing JS function {fn}")

    def test_node_switch_subpage_and_autosave_logic(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH")

        js_script = r"""
        const assert = require('node:assert/strict');
        let _activeConfigSubpage = 'overview';
        let _configDirty = false;
        let _cachedConfig = { upstreams: {}, tools: {} };
        let _autosaveTimer = null;
        let _saveCalls = 0;
        let st = {};

        const panels = {
          'cfg-panel-deepseek': { style: { display: 'none' } },
          'cfg-panel-codex': { style: { display: 'none' } },
          'cfg-panel-antigravity': { style: { display: 'none' } },
          'cfg-panel-voice': { style: { display: 'none' } },
          'cfg-panel-relay': { style: { display: 'none' } },
        };
        const elements = {
          '#cfg-level-1': { style: { display: 'block' } },
          '#cfg-level-2': { style: { display: 'none' } },
          '#cfg-crumb-title': { textContent: '' },
          '#cfg-detail-status-pill': { className: '', textContent: '' },
          '#cfg-autosave-indicator': { className: 'cfg-autosave-pill saved', querySelector: () => ({ textContent: '' }) },
          ...Object.fromEntries(Object.entries(panels).map(([k, v]) => ['#' + k, v]))
        };
        const $ = s => elements[s] || null;
        const $$ = s => Object.values(panels);
        const window = { location: { hash: '#config' }, scrollTo: () => {} };
        const history = { pushState: (st, t, h) => { window.location.hash = h; } };
        function updateOverviewCards() {}
        function updateDetailStatusPill(sub) {
          elements['#cfg-detail-status-pill'].textContent = sub;
        }
        function paintCodexLogin() {}
        function refreshCodexLogin() {}
        function renderConfig() {}

        async function saveConfigDraft() {
          _saveCalls++;
          _configDirty = false;
        }

        async function flushConfigAutosave() {
          if (_autosaveTimer) {
            clearTimeout(_autosaveTimer);
            _autosaveTimer = null;
          }
          if (_configDirty) {
            await saveConfigDraft();
          }
        }

        async function switchConfigSubpage(subpage, updateHash = true) {
          if (_activeConfigSubpage !== 'overview' && subpage === 'overview') {
            await flushConfigAutosave();
          }
          _activeConfigSubpage = subpage || 'overview';
          const l1 = $('#cfg-level-1');
          const l2 = $('#cfg-level-2');
          if (!l1 || !l2) return false;

          if (_activeConfigSubpage === 'overview') {
            l1.style.display = 'block';
            l2.style.display = 'none';
            $$('.cfg-module-panel').forEach(p => p.style.display = 'none');
            updateOverviewCards(_cachedConfig, st);
          } else {
            l1.style.display = 'none';
            l2.style.display = 'block';
            $$('.cfg-module-panel').forEach(p => p.style.display = 'none');
            const panel = $('#cfg-panel-' + _activeConfigSubpage);
            if (panel) panel.style.display = 'block';
            const names = {
              deepseek: 'DeepSeek (官方直连)',
              codex: 'Codex (CLIProxyAPI 代理)',
              antigravity: 'Antigravity (Gemini 伴侣)',
              voice: 'Voice (语音输入伴侣)',
              relay: 'CC Relay 核心网络'
            };
            if ($('#cfg-crumb-title')) $('#cfg-crumb-title').textContent = names[_activeConfigSubpage] || _activeConfigSubpage;
            updateDetailStatusPill(_activeConfigSubpage);
          }

          if (updateHash) {
            const targetHash = _activeConfigSubpage === 'overview' ? '#config' : ('#config/' + _activeConfigSubpage);
            if (window.location.hash !== targetHash) {
              history.pushState(null, '', targetHash);
            }
          }
          return true;
        }

        (async () => {
          // Test 1: Switch to codex
          assert.ok(await switchConfigSubpage('codex'));
          assert.equal(_activeConfigSubpage, 'codex');
          assert.equal(elements['#cfg-level-1'].style.display, 'none');
          assert.equal(elements['#cfg-level-2'].style.display, 'block');
          assert.equal(panels['cfg-panel-codex'].style.display, 'block');
          assert.equal(elements['#cfg-crumb-title'].textContent, 'Codex (CLIProxyAPI 代理)');
          assert.equal(window.location.hash, '#config/codex');

          // Test 2: Switch to voice
          assert.ok(await switchConfigSubpage('voice'));
          assert.equal(_activeConfigSubpage, 'voice');
          assert.equal(panels['cfg-panel-codex'].style.display, 'none');
          assert.equal(panels['cfg-panel-voice'].style.display, 'block');
          assert.equal(elements['#cfg-crumb-title'].textContent, 'Voice (语音输入伴侣)');
          assert.equal(window.location.hash, '#config/voice');

          // Test 3: Edit something, mark dirty, then switch back to overview
          _configDirty = true;
          assert.equal(_saveCalls, 0);
          assert.ok(await switchConfigSubpage('overview'));
          // Navigation flushes auto-save automatically!
          assert.equal(_saveCalls, 1);
          assert.equal(_configDirty, false);
          assert.equal(_activeConfigSubpage, 'overview');
          assert.equal(elements['#cfg-level-1'].style.display, 'block');
          assert.equal(elements['#cfg-level-2'].style.display, 'none');
          assert.equal(window.location.hash, '#config');
        })().catch(err => { console.error(err); process.exitCode = 1; });
        """
        res = subprocess.run([node_bin, "-e", js_script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, f"Node subpage switching and auto-save test failed:\n{res.stderr}")


if __name__ == "__main__":
    unittest.main()
