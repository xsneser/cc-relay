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
