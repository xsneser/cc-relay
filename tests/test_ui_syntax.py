#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI syntax and integrity regression tests for cc-relay.
Verifies that ui.html exists, contains required scripts, passes JS syntax checks,
and includes proper stacking context rules for dropdowns.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
UI_PATH = os.path.join(BASE_DIR, "ui.html")


class TestUISyntax(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(UI_PATH), f"ui.html not found at {UI_PATH}")
        with open(UI_PATH, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_ui_version_meta_present(self):
        m = re.search(r'<meta\s+name=["\']ui-version["\']\s+content=["\']([0-9]+(?:\.[0-9]+)*)["\']', self.html)
        self.assertIsNotNone(m, "ui.html must have a valid ui-version meta tag")

    def test_ui_has_scripts(self):
        scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", self.html, re.DOTALL)
        self.assertGreater(len(scripts), 0, "ui.html should contain at least one <script> tag")

    def test_ui_js_syntax_with_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH; skipping node --check validation")

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

    def test_dropdown_css_stacking_rules(self):
        # 验证卡片与网格项在展开下拉时有明确的层叠与溢出规则
        self.assertIn(".spotlight-card.dropdown-open", self.html)
        self.assertIn(".mode-card.dropdown-open", self.html)
        self.assertIn(".card-hybrid.dropdown-open", self.html)
        self.assertIn(".tier.dropdown-open", self.html)
        self.assertIn(".sel.dropdown-open", self.html)

    def test_dropdown_js_lifecycle(self):
        # 验证关键函数存在于内嵌脚本中
        self.assertIn("function closeAllDropdowns", self.html)
        self.assertIn("function adjustSelPanelPosition", self.html)
        self.assertIn("function openSel", self.html)
        self.assertIn("function closeSel", self.html)
        self.assertIn("e.key === 'Escape'", self.html)

    def test_launcher_version_extractor(self):
        import main_launcher
        ver = main_launcher._extract_ui_version(UI_PATH)
        self.assertIsNotNone(ver)
        self.assertGreaterEqual(ver, (2, 1, 1))

    def test_capture_viewer_and_reset_safety(self):
        # 1. 验证剪贴板函数存在且已从行内属性解耦
        self.assertIn("function copyCallDetailJson", self.html)
        self.assertIn("function copyCallDetailSys", self.html)
        self.assertNotIn("decodeURIComponent('${encodeURIComponent", self.html)

        # 2. 验证清零交互包含二次确认与按钮 loading 状态
        self.assertIn("confirm('确定要清零所有流量统计指标与抓包历史记录吗？此操作不可恢复。')", self.html)
        self.assertIn("清零中…", self.html)

        # 3. 验证 loadCalls 具备并发轮询防撞守卫
        self.assertIn("if (_clLoading && !force) return;", self.html)

        # 4. 验证表格行点击使用事件委托
        self.assertIn("body._delegatedClick", self.html)

    def test_traffic_pause_ui_elements(self):
        # 验证流量启停按钮与横幅存在
        self.assertIn('id="btn-traffic-toggle"', self.html)
        self.assertIn('id="traffic-banner"', self.html)
        self.assertIn('id="btn-banner-resume"', self.html)
        self.assertIn(".btn-traffic", self.html)
        self.assertIn(".traffic-paused-banner", self.html)
        self.assertIn("function renderTrafficControl", self.html)
        self.assertIn("function toggleTraffic", self.html)
        self.assertIn("aria-pressed", self.html)
        self.assertIn("route === 'paused'", self.html)

    def test_restart_ui_elements(self):
        # 验证一键重启按钮、重新连接遮罩与前端状态函数存在
        self.assertIn('id="btn-restart"', self.html)
        self.assertIn('id="restart-overlay"', self.html)
        self.assertIn('id="btn-restart-retry"', self.html)
        self.assertIn('id="btn-restart-close"', self.html)
        self.assertIn(".btn-restart", self.html)
        self.assertIn("function restartService", self.html)
        self.assertIn("function startRestartPolling", self.html)
        self.assertIn("confirm('确定要重启 CC Relay 服务吗？正在处理中的请求将会中断。')", self.html)
        self.assertIn("CC Relay 重启成功", self.html)
        self.assertIn("/api/ping", self.html)
        self.assertIn("cc_traffic_paused", self.html)
        self.assertIn("window.location.reload()", self.html)
        self.assertIn("cc_restart_toast", self.html)

    def test_calls_table_responsive_column_priority(self):
        # 窄屏隐藏首列序号与请求来源，保留 h:opus / h:main 分流规则
        m = re.search(
            r'@media\s*\(max-width:\s*900px\)\s*\{(.*?)\n\s*\}\s*\n\s*@media\s*\(max-width:\s*800px\)',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "900px responsive block must exist")
        block = m.group(1)
        # 窄屏优先按列语义类隐藏序号等非核心列
        self.assertTrue(
            "table.cl th.col-idx" in block or "table.cl th:nth-child(1)" in block,
            "narrow calls-table hide selectors must exist"
        )
        self.assertIn(".replace('hybrid:', 'h:')", self.html)
        self.assertTrue("<th>分流规则</th>" in self.html or '<th class="col-rule">分流规则</th>' in self.html)

    def test_header_status_and_action_affordance(self):
        # 1. 顶栏采用双层清晰功能分区架构: header-top (品牌/版本/操作/导航) 与 header-sub (路由状态/上游服务)
        self.assertIn('class="header-top"', self.html)
        self.assertIn('class="header-sub"', self.html)
        self.assertIn('id="relay-version-badge"', self.html)
        self.assertIn('class="status-group"', self.html)
        self.assertIn('class="action-group"', self.html)

        # 2. 操作按钮使用独立 .btn-action 类, 保留既有 ID 兼容性
        self.assertIn('class="btn-action btn-traffic"', self.html)
        self.assertIn('class="btn-action btn-restart"', self.html)

        # 3. 状态胶囊必须显式声明只读 (默认光标, 无按下位移)
        self.assertRegex(
            self.html,
            re.compile(r"\.badge\s*\{[^}]*border-radius:\s*99px;[^}]*cursor:\s*default;", re.DOTALL),
        )
        # 4. 操作按钮必须是圆角矩形 + 手型光标
        self.assertRegex(
            self.html,
            re.compile(r"\.btn-action\s*\{[^}]*border-radius:\s*8px;[^}]*cursor:\s*pointer;", re.DOTALL),
        )

        # 5. 操作按钮必须具备悬停抬升 / 按下凹陷 / 键盘焦点可见反馈
        self.assertIn(".btn-action:hover", self.html)
        self.assertIn(".btn-action:active", self.html)
        self.assertIn(".btn-action:focus-visible", self.html)
        self.assertIn("translateY(-1px)", self.html)

        # 6. 分隔线与响应式换行规则存在
        self.assertIn(".header-divider", self.html)
        self.assertIn(".status-group .badge", self.html)

        # 7. renderTrafficControl 不得使用 className 赋值覆盖按钮基类
        self.assertNotIn("btn.className = 'btn-traffic", self.html)

    def test_locked_local_upstreams_ui(self):
        # 验证本地服务（Codex 与 Antigravity）的配置锁定与提示
        self.assertNotIn(".config-local-badge", self.html)
        self.assertIn(".config-locked-input", self.html)
        self.assertIn(".config-lock-hint", self.html)
        self.assertIn("localUpstreams", self.html)
        self.assertNotIn("内置受管服务", self.html)
        self.assertNotIn("本地伴侣工具", self.html)
        self.assertIn("base.input.readOnly = true", self.html)
        self.assertIn("btn-unlock-base", self.html)
        self.assertIn("🔒 解锁修改", self.html)
        self.assertNotIn("proxy.input.readOnly = true", self.html)
        # 确保已精简掉预设按钮与自动更新复选框
        self.assertNotIn("btn-codex-proxy-7890", self.html)
        self.assertNotIn("cfg-codex-auto-update", self.html)
        # 确保 Codex 外部代理输入框具备独立样式且没有误用 cfg-base 类名以防覆盖上游 base
        self.assertNotIn('id="cfg-codex-outbound-proxy" class="cfg-base"', self.html)
        self.assertIn(".cfg-cpa-proxy", self.html)
        self.assertIn("btn.classList.toggle('paused'", self.html)

    def test_upstream_controls_are_merged_into_header(self):
        # 代理状态与启动/停止操作只保留在顶栏, 不再重复渲染 dashboard 控制条
        header_start = self.html.index('<header>')
        header_end = self.html.index('</header>', header_start)
        header = self.html[header_start:header_end]
        self.assertIn('<div class="upstream-group"', header)
        for element_id in (
            'codex-dot', 'codex-status', 'b-codex-toggle',
            'gemini-dot', 'gemini-status', 'b-gemini-toggle',
        ):
            self.assertIn('id="' + element_id + '"', header)
        for element_id in ('b-codex-start', 'b-codex-stop', 'b-gemini-start', 'b-gemini-stop'):
            self.assertNotIn('id="' + element_id + '"', self.html)
        self.assertNotIn('class="proxybar"', self.html)
        self.assertNotIn('id="b-pstart"', self.html)
        self.assertNotIn('id="b-pstop"', self.html)
        self.assertIn("function isUpstreamRunning(provider, snapshot = st)", self.html)
        self.assertIn("function renderUpstreamToggle(provider, running, owned = true)", self.html)
        self.assertIn("async function controlUpstream(provider)", self.html)
        self.assertIn("api('/api/upstream', { name: meta.apiName, action })", self.html)
        self.assertIn("upstreamPending = { codex: false, antigravity: false, voice: false }", self.html)
        self.assertIn("btnId: 'b-codex-toggle'", self.html)
        self.assertIn("btnId: 'b-gemini-toggle'", self.html)
        self.assertIn("btnId: 'b-voice-toggle'", self.html)
        self.assertIn("const button = $('#' + meta.btnId);", self.html)
        self.assertIn("data-action=\"start\"", self.html)
        self.assertIn("setAttribute('aria-pressed', running ? 'true' : 'false')", self.html)

        # 三个服务各自独立布局, 每个服务只有一个可切换按钮 (Codex, Gemini, Voice)
        self.assertEqual(header.count('class="upstream-btn '), 3)
        self.assertIn('.status-group, .upstream-group, .action-group { width: 100%; }', self.html)
        self.assertIn('.upstream-group { grid-template-columns: 1fr; padding: 5px 8px; }', self.html)
        self.assertIn('.upstream-btn.start', self.html)
        self.assertIn('.upstream-btn.stop', self.html)
        self.assertIn('.upstream-btn.manual', self.html)
        self.assertIn('.upstream-btn.manual:disabled', self.html)
        self.assertIn('手动运行 · 不可停止', self.html)
        self.assertNotIn("' (手动)'", self.html)
        self.assertNotIn("? ' 手动' : ''", self.html)
        self.assertIn("renderUpstream('#gemini-dot', '#gemini-status', 'Gemini', '8045', grun);", self.html)
        self.assertIn('.upstream-icon-stop { display: none; }', self.html)
        self.assertIn('.upstream-btn:focus-visible', self.html)
        self.assertIn('.upstream-btn:disabled', self.html)
        self.assertIn('aria-label="启动 Codex 代理"', self.html)
        self.assertIn('aria-label="启动 Gemini 代理"', self.html)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true" id="codex-status-region"', self.html)
        self.assertIn('aria-controls="gemini-status-region"', self.html)

    def test_upstream_toggle_state_transition_in_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH")

        # 验证在 JS 运行环境中，renderUpstreamToggle 能够正确找到 #b-codex-toggle
        # 并在 running 为 true 时更新为 stop 类和“停止”文本，running 为 false 时更新为 start 类和“启动”
        js_code = """
        const metaBlock = `""" + self.html[self.html.index("const UPSTREAM_META = {"):self.html.index("function isUpstreamRunning")] + """`;
        const renderBlock = `""" + self.html[self.html.index("function renderUpstreamToggle(provider"):self.html.index("async function controlUpstream(")] + """`;

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

        // 1. 当 Codex 运行中 (running = true)
        renderUpstreamToggle('codex', true);
        const btn = buttons['#b-codex-toggle'];
        if (!btn.className.includes('stop')) {
            console.error('Expected stop class when running, got:', btn.className);
            process.exit(1);
        }
        if (btn.labelNode.textContent !== '停止') {
            console.error('Expected label 停止 when running, got:', btn.labelNode.textContent);
            process.exit(1);
        }
        if (btn.dataset.action !== 'stop') {
            console.error('Expected data-action stop, got:', btn.dataset.action);
            process.exit(1);
        }

        // 2. 当 Codex 未启动 (running = false)
        renderUpstreamToggle('codex', false);
        if (!btn.className.includes('start')) {
            console.error('Expected start class when stopped, got:', btn.className);
            process.exit(1);
        }
        if (btn.labelNode.textContent !== '启动') {
            console.error('Expected label 启动 when stopped, got:', btn.labelNode.textContent);
            process.exit(1);
        }
        if (btn.dataset.action !== 'start') {
            console.error('Expected data-action start, got:', btn.dataset.action);
            process.exit(1);
        }

        // 3. Gemini 由外部手动运行时进入橙色不可停止态
        const gemini = buttons['#b-gemini-toggle'];
        renderUpstreamToggle('antigravity', true, false);
        if (!gemini.className.includes('manual') || gemini.className.includes('stop')) {
            console.error('Expected manual class for unmanaged Gemini, got:', gemini.className);
            process.exit(1);
        }
        if (!gemini.disabled || gemini.dataset.action !== '') {
            console.error('Expected unmanaged Gemini toggle to be disabled with no action');
            process.exit(1);
        }
        if (gemini.labelNode.textContent !== '手动运行 · 不可停止') {
            console.error('Expected manual label, got:', gemini.labelNode.textContent);
            process.exit(1);
        }
        if (gemini.attributes['aria-pressed'] !== 'true' || !gemini.title.includes('无法停止')) {
            console.error('Expected manual Gemini accessibility state');
            process.exit(1);
        }

        // 4. Relay-owned Gemini 仍可正常停止
        renderUpstreamToggle('antigravity', true, true);
        if (!gemini.className.includes('stop') || gemini.disabled || gemini.labelNode.textContent !== '停止') {
            console.error('Expected owned Gemini to be stoppable');
            process.exit(1);
        }
        """

        res = subprocess.run([node_bin, "-e", js_code], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, f"Node verification failed: {res.stderr}")

    def test_model_efforts_definitions(self):
        # 验证 gpt-6 系列模型的推理挡位定义
        self.assertIn("'gpt-6-sol':                   ['instant', 'medium', 'high', 'xhigh', 'max']", self.html)
        self.assertIn("'gpt-6-luna':                  ['instant', 'medium', 'high', 'xhigh']", self.html)

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js is not installed or not in PATH")

        js_code = self.html[self.html.index("const MODEL_EFFORTS = {"):self.html.index("let st = null, busy = false;")] + """
        // gpt-6-sol 必须支持 5 挡 (包含 max)
        const solEffs = effortsFor('gpt-6-sol');
        if (!Array.isArray(solEffs) || solEffs.length !== 5 || !solEffs.includes('max')) {
            console.error('gpt-6-sol should have 5 efforts including max, got:', solEffs);
            process.exit(1);
        }

        // gpt-6-luna 必须支持 4 挡 (不含 max)
        const lunaEffs = effortsFor('gpt-6-luna');
        if (!Array.isArray(lunaEffs) || lunaEffs.length !== 4 || lunaEffs.includes('max')) {
            console.error('gpt-6-luna should have 4 efforts without max, got:', lunaEffs);
            process.exit(1);
        }
        """
        res = subprocess.run([node_bin, "-e", js_code], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, f"Efforts verification failed: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
