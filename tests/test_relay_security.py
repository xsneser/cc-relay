#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Security regression tests for cc-relay and apply_settings.
Zero external dependencies (uses standard library unittest).
"""
import unittest
import threading
import json
import io
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import apply_settings
import cc_relay


class TestUpstreamKeySecurity(unittest.TestCase):
    def test_safe_key_env_name(self):
        # Legitimate canonical fields
        self.assertEqual(cc_relay._safe_key_env_name("deepseek", "real_deepseek_key"), "real_deepseek_key")
        self.assertEqual(cc_relay._safe_key_env_name("deepseek", "deepseek_key"), "deepseek_key")
        self.assertEqual(cc_relay._safe_key_env_name("codex", "codex_proxy_key"), "codex_proxy_key")
        self.assertEqual(cc_relay._safe_key_env_name("antigravity", "antigravity_key"), "antigravity_key")
        self.assertEqual(cc_relay._safe_key_env_name("gemini", "antigravity_key"), "antigravity_key")

        # Rejection of fake_api_key or arbitrary identifiers
        self.assertEqual(cc_relay._safe_key_env_name("deepseek", "fake_api_key"), "")
        self.assertEqual(cc_relay._safe_key_env_name("deepseek", "sk_live_token_12345"), "")
        self.assertEqual(cc_relay._safe_key_env_name("codex", "codex_secret_token"), "")

        # Rejection of cross-upstream key field names
        self.assertEqual(cc_relay._safe_key_env_name("deepseek", "codex_proxy_key"), "")
        self.assertEqual(cc_relay._safe_key_env_name("codex", "real_deepseek_key"), "")

    def test_repair_misplaced_upstream_key(self):
        conf = {
            "upstreams": {
                "deepseek": {"key_env": "sk-real-secret-12345"},
                "codex": {"key_env": "fake_api_key"},
            },
            "real_deepseek_key": "",
            "codex_proxy_key": "",
            "fake_api_key": "fake-relay-key",
        }
        changed = cc_relay._repair_misplaced_upstream_key(conf)
        self.assertTrue(changed)
        # Migrated secret into canonical field
        self.assertEqual(conf["real_deepseek_key"], "sk-real-secret-12345")
        self.assertEqual(conf["upstreams"]["deepseek"]["key_env"], "real_deepseek_key")

        # fake_api_key must NOT be copied into codex_proxy_key
        self.assertEqual(conf["codex_proxy_key"], "")
        self.assertEqual(conf["upstreams"]["codex"]["key_env"], "codex_proxy_key")

    def test_config_public_view_no_raw_tokens(self):
        conf = {
            "upstreams": {
                "deepseek": {
                    "base": "https://api.deepseek.com",
                    "proxy_url": "direct",
                    "key_env": "sk_secret_token_12345",  # Identifier-shaped token
                }
            },
            "real_deepseek_key": "sk-real-token",
        }
        view = cc_relay._config_public_view(conf)
        up = view["upstreams"]["deepseek"]
        # key_env in view MUST NOT be the identifier-shaped token!
        self.assertEqual(up["key_env"], "real_deepseek_key")
        self.assertEqual(up["key_mask"], cc_relay._CONFIG_KEY_MASK)
        self.assertTrue(up["has_key"])

    def test_prompts_lock_is_rlock(self):
        # PR #3 fix: PROMPTS_LOCK must be RLock to prevent recursive lock deadlock
        self.assertIsInstance(cc_relay.PROMPTS_LOCK, type(threading.RLock()))


class TestLoopbackSecurity(unittest.TestCase):
    def test_is_loopback_host(self):
        self.assertTrue(cc_relay.is_loopback_host("127.0.0.1"))
        self.assertTrue(cc_relay.is_loopback_host("localhost"))
        self.assertTrue(cc_relay.is_loopback_host("::1"))
        self.assertTrue(apply_settings.is_loopback_host("127.0.0.1"))
        self.assertTrue(apply_settings.is_loopback_host("localhost"))

        self.assertFalse(cc_relay.is_loopback_host("0.0.0.0"))
        self.assertFalse(cc_relay.is_loopback_host("192.168.1.100"))
        self.assertFalse(cc_relay.is_loopback_host("example.com"))
        self.assertFalse(apply_settings.is_loopback_host("0.0.0.0"))


class TestRedactionAndSanitization(unittest.TestCase):
    def test_sanitize_headers_for_record(self):
        headers = {
            "Host": "127.0.0.1:8400",
            "Authorization": "Bearer sk-relay-secret",
            "X-Api-Key": "sk-relay-secret",
            "Content-Type": "application/json",
        }
        sanitized = cc_relay._sanitize_headers_for_record(headers)
        self.assertEqual(sanitized["Authorization"], "<redacted>")
        self.assertEqual(sanitized["X-Api-Key"], "<redacted>")
        self.assertEqual(sanitized["Host"], "127.0.0.1:8400")

    def test_apply_settings_dry_run_preview_redaction(self):
        settings_data = {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8400",
                "ANTHROPIC_AUTH_TOKEN": "secret-fake-key",
                "SOME_OTHER_API_KEY": "sk-my-other-key",
                "USER": "lenovo",
            }
        }
        preview = apply_settings._sanitize_dict_for_preview(settings_data)
        env = preview["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "<redacted>")
        self.assertEqual(env["SOME_OTHER_API_KEY"], "<redacted>")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8400")
        self.assertEqual(env["USER"], "lenovo")


class TestRelayHttpAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import urllib.request
        from http.server import ThreadingHTTPServer

        cls.fake_key = "test-sk-auth-secret-1234"
        cls.dummy_conf = {
            "fake_api_key": cls.fake_key,
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "upstreams": {},
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cc_relay.Relay)
        cls.server.conf = cls.dummy_conf
        cls.server.reload_conf = lambda: cls.dummy_conf
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_auth_missing_or_invalid_key_401(self):
        import urllib.request
        import urllib.error

        # Request with no auth header
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/models")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

        # Request with wrong auth key
        req_bad = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/models")
        req_bad.add_header("Authorization", "Bearer wrong-key")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_bad)
        self.assertEqual(ctx.exception.code, 401)

    def test_auth_valid_bearer_and_x_api_key_200(self):
        import urllib.request

        # Bearer token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/models")
        req.add_header("Authorization", f"Bearer {self.fake_key}")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("object"), "list")

        # x-api-key
        req2 = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/models")
        req2.add_header("x-api-key", self.fake_key)
        with urllib.request.urlopen(req2) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
