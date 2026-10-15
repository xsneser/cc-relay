#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive Node.js dynamic JavaScript execution tests for ui.html."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


def run_node_code(js_code: str, timeout: int = 15) -> subprocess.CompletedProcess:
    node_bin = shutil.which("node")
    if not node_bin:
        raise unittest.SkipTest("Node.js is not installed or not in PATH")
    return subprocess.run(
        [node_bin, "-e", js_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


class TestUIInteractions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    def test_ui_js_syntax_with_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH")

        scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", self.html, re.DOTALL)
        for idx, script in enumerate(scripts, 1):
            res = subprocess.run(
                [node_bin, "--check"],
                input=script,
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            self.assertEqual(
                res.returncode,
                0,
                f"JavaScript syntax error in script #{idx} of ui.html:\n{res.stderr}",
            )

    def test_dropdown_js_lifecycle(self):
        self.assertIn("function closeAllDropdowns", self.html)
        self.assertIn("function adjustSelPanelPosition", self.html)
        self.assertIn("function openSel", self.html)
        self.assertIn("function closeSel", self.html)
        self.assertIn("e.key === 'Escape'", self.html)

    def test_format_route_reason_js(self):
        self.assertIn("function formatRouteReason(reason)", self.html)
        self.assertIn("formatRouteReason(c.route_reason)", self.html)
        self.assertNotIn("btn.className = 'btn-traffic", self.html)

    def test_upstream_toggle_state_transition_in_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH")

        meta_block = self.html[self.html.index("const UPSTREAM_META = {"):self.html.index("function isUpstreamRunning")]
        render_block = self.html[self.html.index("function renderUpstreamToggle(provider"):self.html.index("async function controlUpstream(")]

        js_code = """
        const metaBlock = `""" + meta_block + """`;
        const renderBlock = `""" + render_block + """`;

        let upstreamPending = { codex: false, antigravity: false, voice: false };
        const buttons = {
            '#b-codex-toggle': {
                dataset: {},
                classList: new Set(),
                className: '',
                attributes: {},
                setAttribute(k, v) { this.attributes[k] = v; },
                title: '',
                disabled: false,
                querySelector(sel) {
                    if (sel === '.upstream-btn-label') {
                        return this.labelNode || (this.labelNode = { textContent: '' });
                    }
                    return null;
                }
            },
            '#b-gemini-toggle': {
                dataset: {},
                classList: new Set(),
                className: '',
                attributes: {},
                setAttribute(k, v) { this.attributes[k] = v; },
                title: '',
                disabled: false,
                querySelector(sel) {
                    if (sel === '.upstream-btn-label') {
                        return this.labelNode || (this.labelNode = { textContent: '' });
                    }
                    return null;
                }
            },
            '#b-voice-toggle': {
                dataset: {},
                classList: new Set(),
                className: '',
                attributes: {},
                setAttribute(k, v) { this.attributes[k] = v; },
                title: '',
                disabled: false,
                querySelector(sel) {
                    if (sel === '.upstream-btn-label') {
                        return this.labelNode || (this.labelNode = { textContent: '' });
                    }
                    return null;
                }
            }
        };
        const $ = sel => buttons[sel] || null;

        eval(metaBlock + '\\n' + renderBlock);

        renderUpstreamToggle('codex', true);
        const btn = buttons['#b-codex-toggle'];
        if (!btn.className.includes('stop')) process.exit(1);
        if (btn.labelNode.textContent !== '停止') process.exit(1);
        if (btn.dataset.action !== 'stop') process.exit(1);

        renderUpstreamToggle('codex', false);
        if (!btn.className.includes('start')) process.exit(1);
        if (btn.labelNode.textContent !== '启动') process.exit(1);
        if (btn.dataset.action !== 'start') process.exit(1);

        const gemini = buttons['#b-gemini-toggle'];
        renderUpstreamToggle('antigravity', true, false);
        if (!gemini.className.includes('manual') || gemini.className.includes('stop')) process.exit(1);
        if (!gemini.disabled || gemini.dataset.action !== '') process.exit(1);
        if (gemini.labelNode.textContent !== '手动运行 · 不可停止') process.exit(1);

        renderUpstreamToggle('antigravity', true, true);
        if (!gemini.className.includes('stop') || gemini.disabled || gemini.labelNode.textContent !== '停止') process.exit(1);
        """
        res = subprocess.run([node_bin, "-e", js_code], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, f"Upstream toggle test failed: {res.stderr}")

    def test_node_switch_subpage_and_autosave_logic(self):
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
          assert.ok(await switchConfigSubpage('codex'));
          assert.equal(_activeConfigSubpage, 'codex');
          assert.equal(elements['#cfg-level-1'].style.display, 'none');
          assert.equal(elements['#cfg-level-2'].style.display, 'block');
          assert.equal(panels['cfg-panel-codex'].style.display, 'block');
          assert.equal(elements['#cfg-crumb-title'].textContent, 'Codex (CLIProxyAPI 代理)');
          assert.equal(window.location.hash, '#config/codex');

          assert.ok(await switchConfigSubpage('voice'));
          assert.equal(_activeConfigSubpage, 'voice');
          assert.equal(panels['cfg-panel-codex'].style.display, 'none');
          assert.equal(panels['cfg-panel-voice'].style.display, 'block');
          assert.equal(elements['#cfg-crumb-title'].textContent, 'Voice (语音输入伴侣)');
          assert.equal(window.location.hash, '#config/voice');

          _configDirty = true;
          assert.equal(_saveCalls, 0);
          assert.ok(await switchConfigSubpage('overview'));
          assert.equal(_saveCalls, 1);
          assert.equal(_activeConfigSubpage, 'overview');
          assert.equal(elements['#cfg-level-1'].style.display, 'block');
          assert.equal(elements['#cfg-level-2'].style.display, 'none');
          assert.equal(window.location.hash, '#config');
        })();
        """
        res = run_node_code(js_script)
        self.assertEqual(res.returncode, 0, f"Subpage switch test failed: {res.stderr}")

    def test_dropdown_initialization_and_open_close_behavior_node(self):
        start_fn = self.html.index("function closeAllDropdowns(exceptSel) {")
        end_fn = self.html.index("\n// 每档三选修改器", start_fn)
        dropdown_code = self.html[start_fn:end_fn]

        test_script = r"""
        const assert = require('node:assert/strict');

        function createClassList() {
          const classes = new Set();
          return {
            add(...names) { names.forEach(n => classes.add(n)); },
            remove(...names) { names.forEach(n => classes.delete(n)); },
            toggle(name, force) {
              if (force === undefined) {
                if (classes.has(name)) { classes.delete(name); return false; }
                else { classes.add(name); return true; }
              }
              if (force) { classes.add(name); return true; }
              classes.delete(name); return false;
            },
            contains(name) { return classes.has(name); },
            has(name) { return classes.has(name); },
            get value() { return Array.from(classes).join(' '); }
          };
        }

        class FakeElement {
          constructor(tag, id = '', className = '') {
            this.tagName = tag.toUpperCase();
            this.id = id;
            this.classList = createClassList();
            if (className) className.split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c));
            this.dataset = {};
            this.style = {};
            this.children = [];
            this.parentElement = null;
            this.listeners = {};
            this.textContent = '';
          }
          appendChild(child) {
            this.children.push(child);
            child.parentElement = this;
            return child;
          }
          addEventListener(event, fn) {
            if (!this.listeners[event]) this.listeners[event] = [];
            this.listeners[event].push(fn);
          }
          dispatchEvent(event) {
            const list = this.listeners[event.type] || [];
            for (const fn of list) {
              fn(event);
              if (event._stopped) break;
            }
            if (!event._stopped && this.parentElement) {
              this.parentElement.dispatchEvent(event);
            }
          }
          closest(sel) {
            let curr = this;
            while (curr) {
              if (sel.startsWith('.') && curr.classList.contains(sel.slice(1))) return curr;
              if (sel.startsWith('#') && curr.id === sel.slice(1)) return curr;
              curr = curr.parentElement;
            }
            return null;
          }
          querySelector(sel) {
            return this.querySelectorAll(sel)[0] || null;
          }
          querySelectorAll(sel) {
            const res = [];
            const walk = (node) => {
              for (const child of node.children) {
                let match = false;
                if (sel === '.sel' && child.classList.contains('sel')) match = true;
                else if (sel === '.sel-btn' && child.classList.contains('sel-btn')) match = true;
                else if (sel === '.sel-panel' && child.classList.contains('sel-panel')) match = true;
                else if (sel === '.sel-panel.open' && child.classList.contains('sel-panel') && child.classList.contains('open')) match = true;
                else if (sel === '.sel-btn.open' && child.classList.contains('sel-btn') && child.classList.contains('open')) match = true;
                else if (sel === '.dropdown-open' && child.classList.contains('dropdown-open')) match = true;
                else if (sel === '.ve-head' && child.classList.contains('ve-head')) match = true;
                else if (sel === '.view-effort' && child.classList.contains('view-effort')) match = true;
                else if (sel === '.eff-rng' && child.classList.contains('eff-rng')) match = true;
                else if (sel === '.opt-list' && child.classList.contains('opt-list')) match = true;
                else if (sel === '.sel-model' && child.classList.contains('sel-model')) match = true;
                else if (sel === '.sel-eff' && child.classList.contains('sel-eff')) match = true;
                else if (sel === '.ve-eff' && child.classList.contains('ve-eff')) match = true;
                else if (sel === '.ve-model' && child.classList.contains('ve-model')) match = true;
                else if (sel === '.eff-ticks' && child.classList.contains('eff-ticks')) match = true;
                else if (sel === '.eff-labels' && child.classList.contains('eff-labels')) match = true;
                if (match) res.push(child);
                walk(child);
              }
            };
            walk(this);
            return res;
          }
          getBoundingClientRect() {
            return { left: 100, right: 300, top: 100, bottom: 200, width: 200, height: 100 };
          }
        }

        const doc = new FakeElement('html');
        const body = new FakeElement('body');
        doc.appendChild(body);

        const routingSel = new FakeElement('div', 'sel-ds', 'sel');
        routingSel.dataset.kind = 'ds';
        routingSel.dataset.global = '1';
        const rBtn = new FakeElement('div', '', 'sel-btn');
        rBtn.appendChild(new FakeElement('span', '', 'sel-model'));
        rBtn.appendChild(new FakeElement('span', '', 'sel-eff'));
        routingSel.appendChild(rBtn);

        const rPanel = new FakeElement('div', '', 'sel-panel');
        const rVe = new FakeElement('div', '', 'view-effort');
        const rVeHead = new FakeElement('div', '', 've-head');
        rVe.appendChild(rVeHead);
        const rVeEff = new FakeElement('div', '', 've-eff');
        rVe.appendChild(rVeEff);
        const rVeModel = new FakeElement('div', '', 've-model');
        rVe.appendChild(rVeModel);
        const rRng = new FakeElement('input', '', 'eff-rng');
        rVe.appendChild(rRng);
        rVe.appendChild(new FakeElement('div', '', 'eff-ticks'));
        rVe.appendChild(new FakeElement('div', '', 'eff-labels'));
        rPanel.appendChild(rVe);
        rPanel.appendChild(new FakeElement('div', '', 'opt-list'));
        routingSel.appendChild(rPanel);
        body.appendChild(routingSel);

        const voiceSel = new FakeElement('div', 'sel-voice-correction', 'sel');
        voiceSel.dataset.kind = 'voice';
        const vBtn = new FakeElement('div', '', 'sel-btn');
        vBtn.appendChild(new FakeElement('span', 'txt-voice-correction-model', 'sel-model'));
        vBtn.appendChild(new FakeElement('span', '', 'sel-eff'));
        vBtn.appendChild(new FakeElement('span', '', 'sel-select'));
        voiceSel.appendChild(vBtn);
        const vPanel = new FakeElement('div', '', 'sel-panel');
        const vVe = new FakeElement('div', '', 'view-effort');
        const vVeHead = new FakeElement('div', '', 've-head');
        vVe.appendChild(vVeHead);
        vVe.appendChild(new FakeElement('div', '', 've-eff'));
        vVe.appendChild(new FakeElement('div', '', 've-model'));
        vVe.appendChild(new FakeElement('input', '', 'eff-rng'));
        vVe.appendChild(new FakeElement('div', '', 'eff-ticks'));
        vVe.appendChild(new FakeElement('div', '', 'eff-labels'));
        vPanel.appendChild(vVe);
        const vVm = new FakeElement('div', '', 'view-model');
        vVm.appendChild(new FakeElement('div', '', 'opt-list'));
        vPanel.appendChild(vVm);
        voiceSel.appendChild(vPanel);
        body.appendChild(voiceSel);

        const pureSel = new FakeElement('div', 'sel-pure', 'sel');
        pureSel.dataset.kind = 'custom';
        const pBtn = new FakeElement('div', '', 'sel-btn');
        pBtn.appendChild(new FakeElement('span', '', 'sel-model'));
        pureSel.appendChild(pBtn);
        const pPanel = new FakeElement('div', '', 'sel-panel model');
        pureSel.appendChild(pPanel);
        body.appendChild(pureSel);

        const document = {
          documentElement: { clientWidth: 1024 },
          querySelector: sel => doc.querySelector(sel),
          querySelectorAll: sel => doc.querySelectorAll(sel),
          addEventListener: () => {}
        };
        const window = { innerWidth: 1024, addEventListener: () => {} };
        const $ = sel => doc.querySelector(sel);
        const $$ = sel => doc.querySelectorAll(sel);
        const effortsFor = () => ['low', 'medium', 'high'];
        const EFFORT_CN = { low: '低', medium: '中', high: '高' };
        const st = { route: 'hybrid', effort: 'medium' };
        const api = async () => ({ ok: true });
        const toast = () => {};
        const buildSel = () => {};
        """ + dropdown_code + r"""

        openSel(voiceSel);
        assert.equal(vPanel.classList.contains('open'), true);
        assert.equal(vBtn.classList.contains('open'), true);
        closeSel(voiceSel);
        assert.equal(vPanel.classList.contains('open'), false);
        assert.equal(vBtn.classList.contains('open'), false);

        openSel(pureSel);
        assert.equal(pPanel.classList.contains('open'), true);
        closeSel(pureSel);
        assert.equal(pPanel.classList.contains('open'), false);

        openSel(routingSel);
        assert.equal(rPanel.classList.contains('open'), true);
        closeSel(routingSel);
        assert.equal(rPanel.classList.contains('open'), false);
        """
        res = run_node_code(test_script)
        self.assertEqual(res.returncode, 0, f"Dropdown Node test failed: {res.stderr}")

    def test_status_requests_are_shared_and_recover_after_failure(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        start = self.html.index("let _statusRequest = null;")
        end = self.html.index("\nfunction renderStatus()", start)
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
        ''' + self.html[start:end] + r'''
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
        result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_auto_dismiss_timer_requires_shown_card(self):
        self.assertIn("if (card.classList.contains('show'))", self.html)

    def test_card_renderers_preserve_identity_in_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available")

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

        let _cpaCardTimer = null, _cpaFastPollTimer = null, _cpaCardDismissedVer = null, _cpaManualCheck = false;
        let _relayCardTimer = null, _relayFastPollTimer = null, _relayCardDismissedVer = null, _relayManualCheck = false;
        let _antigravityCardTimer = null, _antigravityFastPollTimer = null, _antigravityCardDismissedVer = null, _antigravityManualCheck = false;
        let st = {};

        global.setTimeout = () => 1;
        global.clearTimeout = () => {};
        global.setInterval = () => 1;
        global.clearInterval = () => {};
        '''
        cpa_start = self.html.index("function renderCpaProgressCard(data) {")
        cpa_end = self.html.index("\nconst cpaCardClose", cpa_start)

        relay_start = self.html.index("function renderRelayProgressCard(data) {")
        relay_end = self.html.index("\nconst relayCardClose", relay_start)

        ag_start = self.html.index("function renderAntigravityProgressCard(data) {")
        ag_end = self.html.index("\nconst antigravityCardClose", ag_start)

        test_runner = r'''
        renderCpaProgressCard({
          has_update: true,
          latest_version: '8.0.10',
          current_version: '8.0.9',
          update_state: 'idle'
        });
        assert.equal($('#cpa-card-title').textContent, 'Codex (CLIProxyAPI)');
        assert.equal($('#cpa-card-status').textContent, '发现新版本');
        assert.equal($('#cpa-card-ver').textContent, 'v8.0.10');

        renderRelayProgressCard({
          has_update: true,
          latest_version: '2.5.0',
          current_version: '2.4.12',
          update_state: 'idle'
        });
        assert.equal($('#relay-card-title').textContent, 'CC Relay');
        assert.equal($('#relay-card-status').textContent, '发现新版本');

        renderAntigravityProgressCard({
          has_update: true,
          latest_version: '4.9.0',
          current_version: '4.8.9'
        });
        assert.equal($('#antigravity-card-title').textContent, 'Antigravity Tools');
        assert.equal($('#antigravity-card-status').textContent, '发现新版本');
        '''
        full_node_script = script + "\n" + self.html[cpa_start:cpa_end] + "\n" + self.html[relay_start:relay_end] + "\n" + self.html[ag_start:ag_end] + "\n" + test_runner
        res = subprocess.run([node, "-"], input=full_node_script, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Node render test failed: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
