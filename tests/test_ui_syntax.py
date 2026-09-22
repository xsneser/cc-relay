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

    def test_header_status_and_action_affordance(self):
        # 1. 状态展示组、分隔线与操作组必须按顺序独立分组
        structure = re.compile(
            r'<div class="status-group"[^>]*>.*?'
            r'<span class="header-divider"[^>]*>.*?'
            r'<div class="action-group"[^>]*>',
            re.DOTALL,
        )
        self.assertRegex(self.html, structure)

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
        self.assertIn("btn.classList.toggle('paused'", self.html)


if __name__ == "__main__":
    unittest.main()
