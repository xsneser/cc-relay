#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpa_updater
import cc_relay


class CPAUpdaterTests(unittest.TestCase):
    def test_version_parsing_and_numeric_comparison(self):
        self.assertEqual(cpa_updater.parse_version_output("CLIProxyAPI Version: 7.3.19, Commit: abc"), "7.3.19")
        self.assertEqual(cpa_updater.parse_version_output("warning\nCLIProxyAPI Version: v7.2.155"), "7.2.155")
        self.assertIsNone(cpa_updater.parse_version_output("no version"))
        self.assertGreater(cpa_updater.version_tuple("7.3.19"), cpa_updater.version_tuple("7.2.155"))
        self.assertIsNone(cpa_updater.version_tuple("7.3.19-rc1"))

    def test_clean_release_notes(self):
        raw = ("<!-- cliproxyapi-linux-release-assets:start -->\n"
               "## Linux release assets\n- `CLIProxyAPI.tar.gz`\n"
               "<!-- cliproxyapi-linux-release-assets:end -->\n"
               "## Changelog\n"
               "- fix(config): preserve unknown legacy sections as comments (4a2c8186)\n"
               "**Full Changelog**: https://example.com\n")
        cleaned = cpa_updater.clean_release_notes(raw)
        self.assertIn("fix(config): preserve unknown legacy sections as comments", cleaned)
        self.assertNotIn("Linux release assets", cleaned)
        self.assertNotIn("Full Changelog", cleaned)
        self.assertNotIn("4a2c8186", cleaned)

    def test_local_version_reads_stdout_and_stderr_and_caches_by_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = os.path.join(directory, "cpa.exe")
            config = os.path.join(directory, "config.yaml")
            Path(exe).write_bytes(b"binary-one")
            updater = cpa_updater.CPAUpdater(directory, exe, config)
            result = mock.Mock(stdout="", stderr="CLIProxyAPI Version: 7.3.19, Commit: x")
            with mock.patch.object(cpa_updater.subprocess, "run", return_value=result) as run:
                self.assertEqual(updater._local_version(), "7.3.19")
                self.assertEqual(updater._local_version(), "7.3.19")
                self.assertEqual(run.call_count, 1)
                Path(exe).write_bytes(b"binary-two-longer")
                os.utime(exe, None)
                self.assertEqual(updater._local_version(), "7.3.19")
                self.assertEqual(run.call_count, 2)

    def test_release_check_updates_cached_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = os.path.join(directory, "cpa.exe")
            Path(exe).write_bytes(b"binary")
            updater = cpa_updater.CPAUpdater(directory, exe, "")
            payload = {
                "tag_name": "v7.4.1",
                "assets": [{"name": "CLIProxyAPI_7.4.1_windows_amd64.zip",
                            "browser_download_url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.4.1/cpa.zip"}],
            }
            import io
            response = io.BytesIO(json.dumps(payload).encode())
            response.headers = {"ETag": "etag-1"}
            version_result = mock.Mock(stdout="CLIProxyAPI Version: 7.3.19", stderr="")
            with mock.patch.object(cpa_updater.subprocess, "run", return_value=version_result), \
                 mock.patch.object(updater, "_request", return_value=response):
                status = updater.check(force=True)
            self.assertEqual(status["current_version"], "7.3.19")
            self.assertEqual(status["latest_version"], "7.4.1")
            self.assertTrue(status["has_update"])
            self.assertIsNotNone(status["last_checked"])

    def test_codex_auto_update_config_requires_boolean(self):
        config = {"upstreams": {}, "tools": {}}
        with mock.patch.object(cc_relay, "_save_conf"):
            view = cc_relay._apply_config_update(config, {"tools": {"codex": {"auto_update": True}}})
        self.assertTrue(view["tools"]["codex"]["auto_update"])
        with mock.patch.object(cc_relay, "_save_conf"):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                cc_relay._apply_config_update(config, {"tools": {"codex": {"auto_update": "yes"}}})

    def test_codex_outbound_proxy_read_write_and_update(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_yaml = os.path.join(directory, "config.yaml")
            with open(cfg_yaml, "w", encoding="utf-8") as f:
                f.write('host: "127.0.0.1"\nport: 8317\nproxy-url: "http://127.0.0.1:7900"\nopenai-compatibility:\n  - proxy-url: "direct"\n')
            conf = {"codex_config": cfg_yaml, "upstreams": {}, "tools": {}}

            # 1. 验证读取
            self.assertEqual(cc_relay._read_codex_outbound_proxy(conf), "http://127.0.0.1:7900")

            # 2. 验证写入新端口 7890
            self.assertTrue(cc_relay._write_codex_outbound_proxy(conf, "http://127.0.0.1:7890"))
            self.assertEqual(cc_relay._read_codex_outbound_proxy(conf), "http://127.0.0.1:7890")
            # 确认子项 openai-compatibility 中的 proxy-url 未被误修改
            with open(cfg_yaml, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('proxy-url: "http://127.0.0.1:7890"', content)
            self.assertIn('  - proxy-url: "direct"', content)

            # 3. 验证 _get_codex_proxy_target 能够解析 7890 端口
            target = cc_relay._get_codex_proxy_target(conf)
            self.assertEqual(target, ("127.0.0.1", 7890))

            # 4. 验证 _apply_config_update 校验与更新流程
            with mock.patch.object(cc_relay, "_save_conf"):
                view = cc_relay._apply_config_update(conf, {"tools": {"codex": {"outbound_proxy": "http://127.0.0.1:7890"}}})
            self.assertEqual(view["tools"]["codex"]["outbound_proxy"], "http://127.0.0.1:7890")

            # 5. 验证非法 URL 校验报错
            with mock.patch.object(cc_relay, "_save_conf"):
                with self.assertRaisesRegex(ValueError, "Codex 外部代理必须以"):
                    cc_relay._apply_config_update(conf, {"tools": {"codex": {"outbound_proxy": "ftp://127.0.0.1:7890"}}})

    def test_status_does_not_expose_etag(self):
        with tempfile.TemporaryDirectory() as directory:
            updater = cpa_updater.CPAUpdater(directory, os.path.join(directory, "missing.exe"), "")
            with updater._lock:
                updater._snapshot["etag"] = "opaque-etag"
            self.assertNotIn("etag", updater.status())

    def test_status_contains_progress_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            updater = cpa_updater.CPAUpdater(directory, os.path.join(directory, "missing.exe"), "")
            status = updater.status()
            self.assertIn("downloaded_bytes", status)
            self.assertIn("total_bytes", status)
            self.assertIn("download_progress", status)
            self.assertEqual(status["download_progress"], 0.0)


if __name__ == "__main__":
    unittest.main()
