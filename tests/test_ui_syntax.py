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
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


if __name__ == "__main__":
    unittest.main()
