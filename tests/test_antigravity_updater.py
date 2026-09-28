#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import antigravity_updater
from antigravity_updater import (
    AntigravityUpdater,
    parse_version_tuple,
    clean_release_notes,
    get_antigravity_updater,
)
import cc_relay


class TestAntigravityUpdater(unittest.TestCase):
    def test_parse_version_tuple(self):
        self.assertEqual(parse_version_tuple("4.8.4"), (4, 8, 4))
        self.assertEqual(parse_version_tuple("v4.8.5"), (4, 8, 5))
        self.assertEqual(parse_version_tuple("v4.10.0"), (4, 10, 0))
        self.assertIsNone(parse_version_tuple("unknown"))
        self.assertIsNone(parse_version_tuple(""))
        self.assertIsNone(parse_version_tuple(None))
        self.assertTrue(parse_version_tuple("v4.9.0") > parse_version_tuple("4.8.4"))
        self.assertFalse(parse_version_tuple("4.8.4") > parse_version_tuple("4.8.4"))

    def test_clean_release_notes(self):
        sample = """
        <!-- hidden comment -->
        ## What's Changed
        * Fix model mapping (abc1234)
        * Improve performance
        **Full Changelog**: https://github.com/...
        """
        cleaned = clean_release_notes(sample)
        self.assertNotIn("<!--", cleaned)
        self.assertNotIn("Full Changelog", cleaned)
        self.assertIn("Fix model mapping", cleaned)
        self.assertIn("Improve performance", cleaned)

    @patch("urllib.request.build_opener")
    def test_query_local_version_success(self, mock_opener_cls):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status":"ok","version":"4.8.4"}'
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_opener_cls.return_value = mock_opener

        updater = AntigravityUpdater(upstream_url="http://127.0.0.1:8045")
        ver = updater._query_local_version()
        self.assertEqual(ver, "4.8.4")

    @patch("urllib.request.build_opener")
    def test_query_local_version_failure(self, mock_opener_cls):
        mock_opener = MagicMock()
        mock_opener.open.side_effect = Exception("Connection refused")
        mock_opener_cls.return_value = mock_opener

        updater = AntigravityUpdater(upstream_url="http://127.0.0.1:8045")
        ver = updater._query_local_version()
        self.assertIsNone(ver)

    def test_check_with_update_available(self):
        updater = AntigravityUpdater(upstream_url="http://127.0.0.1:8045")
        updater._query_local_version = MagicMock(return_value="4.8.4")

        mock_resp = MagicMock()
        mock_resp.headers = {"ETag": '"etag123"'}
        mock_resp.read.return_value = json.dumps({
            "tag_name": "v4.9.0",
            "html_url": "https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v4.9.0",
            "body": "- Add new feature"
        }).encode("utf-8")

        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp

        with patch("antigravity_updater._openers", return_value=[mock_opener]):
            status = updater.check(force=True)

        self.assertEqual(status["current_version"], "4.8.4")
        self.assertEqual(status["latest_version"], "4.9.0")
        self.assertTrue(status["has_update"])
        self.assertFalse(status["checking"])
        self.assertIsNotNone(status["last_checked"])
        self.assertIn("Add new feature", status["release_notes"])

    def test_check_no_update_needed(self):
        updater = AntigravityUpdater(upstream_url="http://127.0.0.1:8045")
        updater._query_local_version = MagicMock(return_value="4.8.4")

        mock_resp = MagicMock()
        mock_resp.headers = {"ETag": '"etag123"'}
        mock_resp.read.return_value = json.dumps({
            "tag_name": "v4.8.4",
            "html_url": "https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v4.8.4",
            "body": "- Current release"
        }).encode("utf-8")

        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp

        with patch("antigravity_updater._openers", return_value=[mock_opener]):
            status = updater.check(force=True)

        self.assertEqual(status["current_version"], "4.8.4")
        self.assertEqual(status["latest_version"], "4.8.4")
        self.assertFalse(status["has_update"])

    def test_caching_behavior(self):
        updater = AntigravityUpdater(upstream_url="http://127.0.0.1:8045", cache_ttl=3600)
        updater._query_local_version = MagicMock(return_value="4.8.4")
        updater._snapshot["latest_version"] = "4.8.4"
        updater._snapshot["last_checked"] = 9999999999.0

        with patch("antigravity_updater._openers") as mock_openers:
            status = updater.check(force=False)
            mock_openers.assert_not_called()
            self.assertEqual(status["latest_version"], "4.8.4")

    def test_antigravity_open_ui_missing_exe(self):
        res = cc_relay.antigravity_open_ui({"antigravity_exe": r"C:\non_existent_path\fake.exe"})
        self.assertFalse(res["ok"])
        self.assertIn("未找到", res["error"])

    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    def test_antigravity_open_ui_success(self, mock_exists, mock_popen):
        res = cc_relay.antigravity_open_ui({"antigravity_exe": r"C:\fake\antigravity-tools.exe"})
        self.assertTrue(res["ok"])
        self.assertIn("已调出", res["message"])
        self.assertEqual(res["open_mode"], "main_window")
        mock_popen.assert_called()

    def test_stats_snapshot_includes_antigravity_update(self):
        with patch("cc_relay.load_conf", return_value={}), \
             patch("cc_relay.live_models", return_value={"ds": [], "cx": [], "gm": []}), \
             patch("cc_relay.get_upstream_health", return_value={"state": "offline", "reason": ""}), \
             patch("cc_relay._get_cpa_updater") as mock_cpa, \
             patch("cc_relay._get_relay_updater") as mock_ru, \
             patch("antigravity_updater.get_antigravity_updater") as mock_ag:

            mock_cpa.return_value.status.return_value = {}
            mock_ru.return_value.status.return_value = {}
            mock_ag.return_value.status.return_value = {
                "current_version": "4.8.4",
                "latest_version": "4.9.0",
                "has_update": True,
            }

            snap = cc_relay.stats_snapshot()
            self.assertIn("antigravity_update", snap)
            self.assertIn("antigravity_has_update", snap)
            self.assertTrue(snap["antigravity_has_update"])
            self.assertEqual(snap["antigravity_latest_version"], "4.9.0")
            self.assertEqual(snap["antigravity_version"], "4.8.4")


if __name__ == "__main__":
    unittest.main()
