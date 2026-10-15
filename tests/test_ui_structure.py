#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive DOM structure, CSS rules, and UI markup integrity tests for ui.html."""
import os
import re
import unittest
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


class TestUIStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    # ---------- 1. 版本元数据与基础脚本 ----------

    def test_ui_version_meta_present(self):
        m = re.search(r'<meta\s+name=["\']ui-version["\']\s+content=["\']([0-9]+(?:\.[0-9]+)*)["\']', self.html)
        self.assertIsNotNone(m, "ui.html must have a valid ui-version meta tag")

    def test_ui_has_scripts(self):
        scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", self.html, re.DOTALL)
        self.assertGreater(len(scripts), 0, "ui.html should contain at least one <script> tag")

    def test_launcher_version_extractor(self):
        from relay.upstreams.relay_updater import extract_ui_version, parse_version_tuple
        ver_str = extract_ui_version(self.html[:4096])
        self.assertIsNotNone(ver_str)
        ver = parse_version_tuple(ver_str)
        self.assertIsNotNone(ver)
        self.assertGreaterEqual(ver, (2, 1, 1))

    # ---------- 2. 下拉菜单与 CSS 层叠规则 ----------

    def test_dropdown_css_stacking_rules(self):
        self.assertIn(".spotlight-card.dropdown-open", self.html)
        self.assertIn(".mode-card.dropdown-open", self.html)
        self.assertIn(".card-hybrid.dropdown-open", self.html)
        self.assertIn(".tier.dropdown-open", self.html)
        self.assertIn(".sel.dropdown-open", self.html)

    def test_dropdown_structure_and_counts(self):
        sel_matches = re.findall(r'<div\s+class="sel"([^>]*)>', self.html)
        self.assertEqual(len(sel_matches), 9, f"Expected 9 .sel elements, found {len(sel_matches)}")
        self.assertIn('id="sel-voice-correction"', self.html)
        self.assertIn('data-kind="voice"', self.html)
        start = self.html.index('id="sel-voice-correction"')
        end = self.html.index('id="cfg-voice-correction-context"', start)
        voice_sel_block = self.html[start:end]
        self.assertIn("ve-head", voice_sel_block)
        self.assertIn("eff-rng", voice_sel_block)
        self.assertIn("view-effort", voice_sel_block)

    # ---------- 3. 二级配置中心架构 ----------

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
        self.assertIn('id="cfg-voice-device"', self.html)
        self.assertIn('id="cfg-voice-vram-mode"', self.html)
        self.assertIn('id="voice-vram-status"', self.html)
        self.assertIn('按相同识别流程顺序处理', self.html)
        self.assertIn('id="card-engine-sensevoice"', self.html)
        self.assertIn('id="card-engine-qwen"', self.html)
        self.assertIn('id="voice-group-compute"', self.html)
        self.assertIn('id="voice-device-advice"', self.html)
        self.assertIn('id="voice-qwen-download-banner"', self.html)
        self.assertIn('id="btn-download-qwen"', self.html)
        self.assertIn('id="btn-delete-qwen"', self.html)

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
        ):
            self.assertIn(fn, self.html)

    # ---------- 4. 路由卡片、提示词标签与强度定义 ----------

    def test_hybrid_cards_and_prompt_tabs_order(self):
        self.assertIn('data-tier="main"', self.html)
        self.assertIn('data-tier="opus"', self.html)
        self.assertIn('data-tier="sonnet"', self.html)
        self.assertIn('data-tier="fast"', self.html)
        self.assertIn('data-tier="agent"', self.html)

    def test_hybrid_card_subtitles(self):
        grid = self.html.split('<div class="hb-grid">', 1)[1]
        grid = grid.split('<!-- 提示词实时编辑卡片 -->', 1)[0]
        cards = re.split(r'<!-- 档位 \d+: .*? -->', grid)[1:]
        self.assertEqual(len(cards), 5)
        for card in cards:
            self.assertIn('<span class="hint">', card)
        self.assertIn("<em>SUBAGENT·FILE_SEARCH</em>", cards[3])
        self.assertIn('<span class="hint">广域代码检索 · 轻量快速</span>', cards[3])
        self.assertNotIn("DEFAULT_OPUS_MODEL", cards[3])
        self.assertIn("<em>SUBAGENT·ARCH_PLAN</em>", cards[4])
        self.assertIn('<span class="hint">顶层架构规划 · 推荐顶级大模型</span>', cards[4])
        self.assertNotIn("AGENT_SDK", cards[4])
        self.assertIn("grid-auto-rows: 1fr;", self.html)

    def test_locked_local_upstreams_ui(self):
        self.assertIn('id="btn-unlock-codex-base"', self.html)
        self.assertIn('id="btn-unlock-ag-base"', self.html)

    def test_upstream_controls_are_merged_into_header(self):
        header_start = self.html.index('<header>')
        header_end = self.html.index('</header>', header_start)
        header = self.html[header_start:header_end]
        self.assertIn('<div class="upstream-group"', header)

    def test_model_efforts_definitions(self):
        self.assertIn("const MODEL_EFFORTS =", self.html)
        self.assertIn("'gpt-6-sol'", self.html)
        self.assertIn("'gpt-6-luna'", self.html)

    # ---------- 5. 流量控制、重启与调用记录 ----------

    def test_capture_viewer_and_reset_safety(self):
        self.assertIn("function copyCallDetailJson", self.html)
        self.assertIn("function copyCallDetailSys", self.html)
        self.assertIn("confirm('确定要清零所有流量统计指标与抓包历史记录吗？此操作不可恢复。')", self.html)
        self.assertIn("清零中…", self.html)
        self.assertIn("if (_clLoading && !force) return;", self.html)
        self.assertIn("body._delegatedClick", self.html)

    def test_traffic_pause_ui_elements(self):
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
        self.assertIn(".col-ts", self.html)
        self.assertIn(".col-model", self.html)
        self.assertIn(".col-route", self.html)
        self.assertIn(".col-status", self.html)

    def test_header_status_and_action_affordance(self):
        self.assertIn('class="header-top"', self.html)
        self.assertIn('class="header-sub"', self.html)
        self.assertIn('id="relay-version-badge"', self.html)
        self.assertIn('class="status-group"', self.html)
        self.assertIn('class="action-group"', self.html)
        self.assertIn('class="btn-action btn-traffic"', self.html)
        self.assertIn('class="btn-action btn-restart"', self.html)
        self.assertIn('id="routedot"', self.html)
        self.assertIn('id="routetxt"', self.html)

    # ---------- 6. 轮询超时与更新卡片静态结构 ----------

    def test_status_and_config_fetch_use_shorter_timeouts_than_mutations(self):
        self.assertIn("await api('/api/status', undefined, 8000)", self.html)
        self.assertIn("await api('/api/config', undefined, 4000)", self.html)
        self.assertIn("defaultTimeoutMs = body ? 60000 : 20000", self.html)

    def test_voice_status_exposes_startup_phase_and_elapsed_time(self):
        self.assertIn("const startup = vstat.startup || {}", self.html)
        self.assertIn("startup.elapsed_seconds", self.html)
        self.assertIn("checking_dependencies: '检查依赖'", self.html)
        self.assertIn("model_loading: '加载语音模型'", self.html)
        self.assertIn("启动较慢，仍在等待", self.html)
        self.assertIn("当前阶段:", self.html)

    def test_relay_progress_card_display_conditions(self):
        self.assertIn("let _relayManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'fetching', 'applying'].includes(state);", self.html)
        self.assertIn("const isManual = !!_relayManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_cpa_progress_card_display_conditions(self):
        self.assertIn("let _cpaManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'verifying', 'staged', 'installing', 'rolling_back'].includes(state);", self.html)
        self.assertIn("const isManual = !!_cpaManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_antigravity_progress_card_display_conditions(self):
        self.assertIn("let _antigravityManualCheck = false;", self.html)
        self.assertIn("const isManual = !!_antigravityManualCheck;", self.html)
        self.assertIn("const shouldShow = isManual || hasNewUpdate;", self.html)

    def test_manual_check_triggers_exist(self):
        self.assertIn('id="btn-relay-update"', self.html)
        self.assertIn('id="cpa-check-update"', self.html)
        self.assertIn('id="antigravity-check-update"', self.html)
        self.assertIn("_relayManualCheck = true;", self.html)
        self.assertIn("_cpaManualCheck = true;", self.html)
        self.assertIn("_antigravityManualCheck = true;", self.html)

    def test_card_static_structure_and_component_names(self):
        self.assertIn('<div class="cpa-card-title" id="cpa-card-title">Codex (CLIProxyAPI)</div>', self.html)
        self.assertIn('<div class="cpa-card-title" id="relay-card-title">CC Relay</div>', self.html)
        self.assertIn('<div class="cpa-card-title" id="antigravity-card-title">Antigravity Tools</div>', self.html)
        self.assertIn('id="cpa-card-status"', self.html)
        self.assertIn('id="cpa-card-ver"', self.html)
        self.assertIn('id="relay-card-status"', self.html)
        self.assertIn('id="relay-card-ver"', self.html)
        self.assertIn('id="antigravity-card-status"', self.html)
        self.assertIn('id="antigravity-card-ver"', self.html)
        self.assertIn(".cpa-card-sub", self.html)
        self.assertIn(".cpa-card-status", self.html)
        self.assertIn(".cpa-card-status.has-update", self.html)


if __name__ == "__main__":
    unittest.main()
