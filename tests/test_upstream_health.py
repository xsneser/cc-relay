#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for upstream 3-state health checking (online, degraded, offline)
and UI status badge rendering.
"""
import time
import unittest
from unittest import mock
import cc_relay


class TestUpstreamHealth(unittest.TestCase):
    def setUp(self):
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            cc_relay._UPSTREAM_HEALTH["codex"] = {"api_ok": True, "reason": "", "ts": 0.0}
            cc_relay._UPSTREAM_HEALTH["antigravity"] = {"api_ok": True, "reason": "", "ts": 0.0}
        with cc_relay._CODEX_PROXY_TARGET_LOCK:
            cc_relay._CODEX_PROXY_TARGET_CACHE.update({"ts": 0.0, "target": None})
        with cc_relay._CODEX_PROXY_CHECK_LOCK:
            cc_relay._CODEX_PROXY_CHECK_CACHE.update({"ts": 0.0, "ok": True, "port": None})

    def test_codex_offline_when_port_closed(self):
        conf = {}
        res = cc_relay.get_upstream_health(conf, "codex", running=False)
        self.assertEqual(res["state"], "offline")
        self.assertFalse(res["running"])

    def test_codex_degraded_when_proxy_unreachable(self):
        conf = {}
        with mock.patch.object(cc_relay, "_get_codex_proxy_target", return_value=("127.0.0.1", 7897)), \
             mock.patch.object(cc_relay, "_tcp", return_value=False):
            res = cc_relay.get_upstream_health(conf, "codex", running=True)
            self.assertEqual(res["state"], "degraded")
            self.assertTrue(res["running"])
            self.assertIn("7897", res["reason"])

    def test_codex_online_when_proxy_reachable(self):
        conf = {}
        with mock.patch.object(cc_relay, "_get_codex_proxy_target", return_value=("127.0.0.1", 7897)), \
             mock.patch.object(cc_relay, "_tcp", return_value=True):
            res = cc_relay.get_upstream_health(conf, "codex", running=True)
            self.assertEqual(res["state"], "online")
            self.assertTrue(res["running"])
            self.assertEqual(res["reason"], "")

    def test_runtime_error_triggers_degraded(self):
        conf = {}
        now = time.time()
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            cc_relay._UPSTREAM_HEALTH["codex"] = {"api_ok": False, "reason": "代理未运行 (:7897)", "ts": now}
        with mock.patch.object(cc_relay, "_get_codex_proxy_target", return_value=None):
            res = cc_relay.get_upstream_health(conf, "codex", running=True)
            self.assertEqual(res["state"], "degraded")
            self.assertEqual(res["reason"], "代理未运行 (:7897)")

    def test_record_upstream_health_gemini_alias(self):
        cc_relay.record_upstream_health("gemini", False, "上游报错 (503)")
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            h = dict(cc_relay._UPSTREAM_HEALTH["antigravity"])
        self.assertFalse(h["api_ok"])
        self.assertEqual(h["reason"], "上游报错 (503)")

        # 标记恢复正常
        cc_relay.record_upstream_health("gemini", True)
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            h = dict(cc_relay._UPSTREAM_HEALTH["antigravity"])
        self.assertTrue(h["api_ok"])
        self.assertEqual(h["reason"], "")

    def test_probe_upstream_recovers_degraded_state(self):
        conf = {"upstreams": {"gemini": {"base": "http://127.0.0.1:8045"}}}
        # 先模拟 degraded 状态
        now = time.time()
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            cc_relay._UPSTREAM_HEALTH["antigravity"] = {"api_ok": False, "reason": "API报错", "ts": now}
        self.assertEqual(cc_relay.get_upstream_health(conf, "gemini", running=True)["state"], "degraded")

        # 模拟 probe 成功返回模型列表
        fake_payload = {"data": [{"id": "gemini-2.5-flash"}, {"id": "gemini-3.7-flash"}]}
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = cc_relay.json.dumps(fake_payload).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with mock.patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
            res = cc_relay.probe_upstream(conf, "gemini")
            self.assertTrue(res["available"])
            self.assertIn("gemini-2.5-flash", res["models"])

        # 验证探测成功后健康状态已自动恢复为 online
        health = cc_relay.get_upstream_health(conf, "gemini", running=True)
        self.assertEqual(health["state"], "online")
        self.assertEqual(health["reason"], "")

    def test_probe_upstream_failure_marks_degraded(self):
        conf = {"upstreams": {"gemini": {"base": "http://127.0.0.1:8045"}}}
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=Exception("Connection refused")):
            res = cc_relay.probe_upstream(conf, "gemini")
            self.assertFalse(res["available"])

        health = cc_relay.get_upstream_health(conf, "gemini", running=True)
        self.assertEqual(health["state"], "degraded")
        self.assertIn("Connection refused", health["reason"])

    def test_bg_fetch_models_recovers_health_on_success(self):
        conf = {"upstreams": {"gemini": {"base": "http://127.0.0.1:8045"}}}
        now = time.time()
        with cc_relay._UPSTREAM_HEALTH_LOCK:
            cc_relay._UPSTREAM_HEALTH["antigravity"] = {"api_ok": False, "reason": "网络超时", "ts": now}
        self.assertEqual(cc_relay.get_upstream_health(conf, "gemini", running=True)["state"], "degraded")

        with mock.patch.object(cc_relay, "_fetch_models", side_effect=lambda base, key, timeout=6, prefix=None: ["gemini-2.5-flash"] if prefix == "gemini-" else []):
            cc_relay._bg_fetch_models(conf, now)

        health = cc_relay.get_upstream_health(conf, "gemini", running=True)
        self.assertEqual(health["state"], "online")

    def test_stats_snapshot_includes_states(self):
        conf = {"router": {"route": "hybrid"}, "upstreams": {}}
        models = {"ds": [], "cx": [], "gm": []}
        with mock.patch.object(cc_relay, "load_conf", return_value=conf), \
             mock.patch.object(cc_relay, "live_models", return_value=models), \
             mock.patch.object(cc_relay, "codex_up", return_value=True), \
             mock.patch.object(cc_relay, "antigravity_up", return_value=True), \
             mock.patch.object(cc_relay, "_check_codex_proxy_reachable", return_value=(False, 7897)):
            snap = cc_relay.stats_snapshot()
            self.assertTrue(snap["codex_up"])
            self.assertEqual(snap["codex_state"], "degraded")
            self.assertIn("7897", snap["codex_reason"])
            self.assertIn("codex", snap["upstreams_status"])
            self.assertEqual(snap["upstreams_status"]["codex"]["state"], "degraded")
            self.assertFalse(snap["upstreams_status"]["codex"]["available"])


class TestUIBadgeStyles(unittest.TestCase):
    def test_warn_dot_css_exists(self):
        import os
        ui_path = os.path.join(cc_relay.BASE, "ui.html")
        with open(ui_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn(".dot.warn", html)
        self.assertIn("var(--amber)", html)
        self.assertIn("function getUpstreamState", html)


if __name__ == "__main__":
    unittest.main()
