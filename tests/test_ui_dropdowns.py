#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI dropdown and lifecycle regression tests for cc-relay.
Verifies that all dropdown (.sel) elements initialize safely without throwing
TypeErrors on selectors lacking effort sliders (such as #sel-voice-correction),
and that initial polling (refresh) is correctly reached.
"""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


class TestUIDropdowns(unittest.TestCase):
    def setUp(self):
        self.html = UI_PATH.read_text(encoding="utf-8")

    def test_dropdown_structure_and_counts(self):
        # 确保所有 .sel 存在，并且语音校正选择器为无强度滑块的纯模型选择器
        sel_matches = re.findall(r'<div\s+class="sel"([^>]*)>', self.html)
        self.assertEqual(len(sel_matches), 9, f"Expected 9 .sel elements, found {len(sel_matches)}")

        # 验证 #sel-voice-correction 存在
        self.assertIn('id="sel-voice-correction"', self.html)
        self.assertIn('data-kind="voice"', self.html)

        # 提取 #sel-voice-correction 的容器内容
        start = self.html.index('id="sel-voice-correction"')
        end = self.html.index('id="cfg-voice-correction-timeout"', start)
        voice_sel_block = self.html[start:end]
        self.assertIn("ve-head", voice_sel_block, "#sel-voice-correction should contain .ve-head")
        self.assertIn("eff-rng", voice_sel_block, "#sel-voice-correction should contain .eff-rng")
        self.assertIn("view-effort", voice_sel_block, "#sel-voice-correction should contain .view-effort")

    def test_dropdown_initialization_and_open_close_behavior_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed or not in PATH")

        # 提取 openSel, closeSel, closeAllDropdowns 以及 $$('.sel').forEach 初始化逻辑
        start_fn = self.html.index("function closeAllDropdowns(exceptSel) {")
        end_fn = self.html.index("\n// 每档三选修改器", start_fn)
        dropdown_code = self.html[start_fn:end_fn]

        test_script = r"""
const assert = require('node:assert/strict');

// 创建轻量 DOM 模拟节点
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

// 创建 8 个带强度的路由下拉框与 1 个纯模型语音下拉框
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

// 语音语义校正下拉框 (具备 view-effort, ve-head, eff-rng)
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

// 纯模型选择器 (fallback: 无 ve-head, 无 eff-rng, 无 view-effort)
const pureSel = new FakeElement('div', 'sel-pure', 'sel');
pureSel.dataset.kind = 'custom';
const pBtn = new FakeElement('div', '', 'sel-btn');
pBtn.appendChild(new FakeElement('span', '', 'sel-model'));
pureSel.appendChild(pBtn);
const pPanel = new FakeElement('div', '', 'sel-panel model');
pureSel.appendChild(pPanel);
body.appendChild(pureSel);

// 模拟全局环境与选择器
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

// 执行提取的 ui.html 下拉框逻辑代码
""" + dropdown_code + r"""

// 1. 验证初始化执行后，语音选择器没有崩溃，且没有挂载错误的 ve-head / eff-rng 监听 (因 dataset.kind === 'voice' 隔离)
assert.ok(voiceSel.listeners['click'] === undefined || voiceSel.listeners['click'].length === 0);

// 2. 验证 openSel / closeSel 对具备双视图的语音校正选择器的行为
openSel(voiceSel);
assert.equal(vPanel.classList.contains('open'), true, 'Voice panel should be open');
assert.equal(vBtn.classList.contains('open'), true, 'Voice btn should be open');
assert.equal(vBtn.classList.contains('mode-eff'), true, 'Voice btn should have mode-eff class on open');

closeSel(voiceSel);
assert.equal(vPanel.classList.contains('open'), false, 'Voice panel should be closed');
assert.equal(vBtn.classList.contains('open'), false, 'Voice btn should be closed');
assert.equal(vBtn.classList.contains('mode-eff'), false, 'Voice btn should not have mode-eff');

// 3. 验证 openSel / closeSel 对无强度面板的纯模型选择器的回退兼容行为
openSel(pureSel);
assert.equal(pPanel.classList.contains('open'), true);
assert.equal(pBtn.classList.contains('mode-eff'), false);
closeSel(pureSel);
assert.equal(pPanel.classList.contains('open'), false);

// 4. 验证 openSel / closeSel 对具备强度面板的控制台路由选择器的行为
openSel(routingSel);
assert.equal(rPanel.classList.contains('open'), true);
assert.equal(rBtn.classList.contains('mode-eff'), true, 'Routing selector should have mode-eff on open');
closeSel(routingSel);
assert.equal(rPanel.classList.contains('open'), false);
assert.equal(rBtn.classList.contains('mode-eff'), false);

console.log("ALL_DROPDOWN_TESTS_PASSED");
"""
        res = subprocess.run(
            [node, "-e", test_script],
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Dropdown Node test failed:\nstdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("ALL_DROPDOWN_TESTS_PASSED", res.stdout)

    def test_voice_correction_model_selection_without_routing_mutation(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed or not in PATH")

        start_build = self.html.index("function buildSel(sel, models, curModel, curEff) {")
        end_build = self.html.index("function closeAllDropdowns(exceptSel) {", start_build)
        build_code = self.html[start_build:end_build]

        start_fn = self.html.index("function populateVoiceCorrectionModels(selectedModel, selectedEffort) {")
        end_fn = self.html.index("\nfunction renderConfig(cfg) {", start_fn)
        voice_code = self.html[start_fn:end_fn]

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

let routeApiCalls = 0;
let autosaveCalls = 0;
let toasts = [];

const optListEl = {
  innerHTML: '',
  listeners: {},
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }
};
const modelSpanEl = { textContent: '' };
const effSpanEl = { textContent: '' };
const btnEl = {
  classList: createClassList(),
  listeners: {},
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  },
  querySelector(sel) {
    if (sel === '.sel-model') return modelSpanEl;
    if (sel === '.sel-eff') return effSpanEl;
    return null;
  }
};
const veEffEl = { textContent: '' };
const veModelEl = { textContent: '' };
const rngEl = {
  min: 0, max: 1, step: 1, value: 0,
  listeners: {},
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }
};
const veHeadEl = {
  listeners: {},
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }
};
const veTicksEl = { innerHTML: '', appendChild() {} };
const veLabelsEl = { innerHTML: '' };
const viewEffortEl = {
  querySelector(sel) {
    if (sel === '.ve-eff') return veEffEl;
    if (sel === '.ve-model') return veModelEl;
    if (sel === '.eff-rng') return rngEl;
    if (sel === '.eff-ticks') return veTicksEl;
    if (sel === '.eff-labels') return veLabelsEl;
    return null;
  }
};
const panelEl = {
  classList: createClassList(),
  querySelector(sel) {
    if (sel === '.view-effort') return viewEffortEl;
    if (sel === '.opt-list') return optListEl;
    return null;
  }
};

const voiceSel = {
  id: 'sel-voice-correction',
  dataset: { model: 'deepseek-flash', kind: 'voice' },
  querySelector(sel) {
    if (sel === '.opt-list') return optListEl;
    if (sel === '.sel-model') return modelSpanEl;
    if (sel === '.sel-btn') return btnEl;
    if (sel === '.sel-btn .sel-eff') return effSpanEl;
    if (sel === '.sel-panel') return panelEl;
    if (sel === '.view-effort') return viewEffortEl;
    if (sel === '.ve-head') return veHeadEl;
    if (sel === '.eff-rng') return rngEl;
    return null;
  }
};

const $ = sel => (sel === '#sel-voice-correction' ? voiceSel : null);
const window = { _cachedConfig: null };
const document = { createElement: () => ({}) };
const noJunk = () => true;
const isCodex = () => true;
const escHtml = s => s;
const closeSel = () => {};
const openSel = () => {};
const toast = msg => { toasts.push(msg); };
const scheduleConfigAutosave = () => { autosaveCalls++; };
const api = async (endpoint) => {
  if (endpoint === '/api/route') routeApiCalls++;
  return { ok: true };
};
const EFFORT_CN = { off: '关', low: '低', medium: '中', high: '高', xhigh: '超高', max: '极高' };
const effortsFor = m => (m && m.startsWith('gemini-') ? ['off'] : (m && m.includes('deepseek') ? ['off', 'high'] : ['low', 'medium', 'high']));

const st = {
  models_ds: ['deepseek-chat', 'deepseek-coder'],
  models_codex: ['gpt-5.6-sol'],
  models_gemini: ['gemini-3.7-flash-low']
};

""" + build_code + voice_code + r"""

// 1. 初始化选项填充
populateVoiceCorrectionModels('deepseek-coder', 'off');
assert.equal(voiceSel.dataset.model, 'deepseek-coder');
assert.equal(voiceSel.dataset.effort, 'off');
assert.equal(modelSpanEl.textContent, 'deepseek-coder');

// 2. 模拟滑块拖动与确认
const rngChangeListeners = rngEl.listeners['change'] || [];
assert.ok(rngChangeListeners.length > 0, 'Must have range change listener');
rngEl.value = 1; // 切换到 high
for (const fn of rngChangeListeners) {
  fn();
}
assert.equal(voiceSel.dataset.effort, 'high');
assert.equal(autosaveCalls, 1, 'Changing effort slider should trigger autosave');
assert.equal(routeApiCalls, 0, 'Changing voice effort MUST NEVER call /api/route');

// 3. 模拟点击模型选项
const optListeners = optListEl.listeners['click'] || [];
assert.ok(optListeners.length > 0, 'Must have option list click listener');

const clickEvent = {
  target: {
    closest(s) {
      if (s === '.opt') return { dataset: { m: 'gpt-5.6-sol' } };
      return null;
    }
  }
};

for (const fn of optListeners) {
  fn(clickEvent);
}

assert.equal(voiceSel.dataset.model, 'gpt-5.6-sol', 'Model should be updated to selected option');
assert.equal(autosaveCalls, 2, 'Should trigger autosave on model change');
assert.equal(routeApiCalls, 0, 'Selecting voice correction model MUST NEVER call /api/route');
assert.ok(toasts.some(t => t.includes('gpt-5.6-sol')));

console.log("VOICE_SELECTION_TEST_PASSED");
"""
        res = subprocess.run(
            [node, "-e", test_script],
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        self.assertEqual(res.returncode, 0, f"Voice selection test failed:\nstdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("VOICE_SELECTION_TEST_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
