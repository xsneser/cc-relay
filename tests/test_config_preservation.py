#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for configuration resilience and route fallback in cc-relay.
Verifies that:
1. Partial config updates do not wipe out critical sections (upstreams, router).
2. load_conf gracefully recovers from missing or corrupted configuration files.
3. pick_route gracefully falls back to available upstreams when a target upstream is unconfigured.
"""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import cc_relay


class TestConfigPreservation(unittest.TestCase):
    def test_deep_merge_dict(self):
        base = {
            "a": 1,
            "b": {"x": 10, "y": 20},
            "c": [1, 2],
        }
        patch = {
            "b": {"y": 99, "z": 30},
            "d": "hello",
        }
        merged = cc_relay._deep_merge_dict(base, patch)
        self.assertEqual(merged["a"], 1)
        self.assertEqual(merged["b"]["x"], 10)
        self.assertEqual(merged["b"]["y"], 99)
        self.assertEqual(merged["b"]["z"], 30)
        self.assertEqual(merged["d"], "hello")

    def test_save_conf_prevents_wiping_upstreams_and_router(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf_path = os.path.join(tmpdir, "config.json")
            initial_conf = {
                "upstreams": {
                    "deepseek": {"base": "https://api.deepseek.com", "key_env": "real_deepseek_key"},
                    "codex": {"base": "http://127.0.0.1:8317", "key_env": "codex_proxy_key"},
                },
                "router": {
                    "route": "hybrid",
                    "tiers": {"main": "deepseek-flash", "opus": "gpt-5.6-luna"},
                },
                "tools": {"voice": {"enabled": False}},
            }
            with open(conf_path, "w", encoding="utf-8") as f:
                json.dump(initial_conf, f)

            with mock.patch.object(cc_relay, "CONF", conf_path):
                # 模拟只传了 tools.voice 的局部字典
                stripped_conf = {
                    "tools": {
                        "voice": {
                            "semantic_correction_enabled": True,
                            "semantic_correction_model": "deepseek-flash",
                        }
                    }
                }
                cc_relay._save_conf(stripped_conf)

                # 重新读取磁盘文件，验证核心节没有被清除
                with open(conf_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)

                self.assertIn("upstreams", saved, "upstreams must not be wiped by partial save")
                self.assertIn("deepseek", saved["upstreams"])
                self.assertIn("codex", saved["upstreams"])
                self.assertIn("router", saved, "router must not be wiped by partial save")
                self.assertEqual(saved["router"]["route"], "hybrid")
                self.assertTrue(saved["tools"]["voice"]["semantic_correction_enabled"])

    def test_load_conf_recovers_missing_upstreams_from_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf_path = os.path.join(tmpdir, "config.json")
            # 写入一个缺少 upstreams 的空配置
            with open(conf_path, "w", encoding="utf-8") as f:
                json.dump({"tools": {"voice": {}}}, f)

            with mock.patch.object(cc_relay, "CONF", conf_path):
                loaded = cc_relay.load_conf()
                self.assertIn("upstreams", loaded)
                self.assertIn("deepseek", loaded["upstreams"])
                self.assertIn("codex", loaded["upstreams"])
                self.assertIn("router", loaded)
                self.assertEqual(loaded["router"]["route"], "hybrid")

    def test_pick_route_fallback_when_codex_unconfigured(self):
        # 仅配置了 deepseek，未配置 codex
        conf = {
            "upstreams": {
                "deepseek": {"base": "https://api.deepseek.com/anthropic", "key_env": "real_deepseek_key"},
                "codex": {"base": "", "key_env": "codex_proxy_key"},  # base 为空，视为未配置
            },
            "router": {
                "route": "hybrid",
                "hybrid_codex_model": "gpt-5.6-sol",
                "hybrid_deepseek_model": "deepseek-flash",
                "tiers": {
                    "main": "deepseek-flash",
                    "opus": "gpt-5.6-luna",
                },
            },
        }
        # 模拟 Explore 代理请求 (提示词命中 opus 档位，原本映射到 codex)
        headers = {}
        body = {
            "model": "relay-main",
            "system": "file search specialist for Claude Code",
            "messages": [{"role": "user", "content": "search files"}],
        }

        up_name, map_model, reason = cc_relay.pick_route(conf, headers, body)
        self.assertEqual(up_name, "deepseek", "Should fall back to configured deepseek")
        self.assertIn("fallback:codex->deepseek", reason)
        self.assertEqual(map_model, "deepseek-flash")

    def test_pick_route_fallback_when_deepseek_unconfigured(self):
        # 仅配置了 antigravity，未配置 deepseek
        conf = {
            "upstreams": {
                "deepseek": {"base": "", "key_env": ""},
                "antigravity": {"base": "http://127.0.0.1:8045", "key_env": "antigravity_key"},
            },
            "router": {
                "route": "hybrid",
                "hybrid_antigravity_model": "gemini-3.7-flash-low",
                "tiers": {
                    "main": "deepseek-flash",
                },
            },
        }
        # 主循环请求 (原本映射到 deepseek)
        headers = {}
        body = {
            "model": "relay-main",
            "messages": [{"role": "user", "content": "hello"}],
        }

        up_name, map_model, reason = cc_relay.pick_route(conf, headers, body)
        self.assertEqual(up_name, "antigravity", "Should fall back to configured antigravity")
        self.assertIn("fallback:deepseek->antigravity", reason)
        self.assertEqual(map_model, "gemini-3.7-flash-low")


if __name__ == "__main__":
    unittest.main()
