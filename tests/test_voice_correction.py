# -*- coding: utf-8 -*-
"""Comprehensive tests for active Claude Code context bridge and semantic correction."""
import json
import unittest
from unittest import mock

import cc_relay
from tools.voice_input.config import VoiceConfig
from tools.voice_input.correction import CORRECTION_SYSTEM_PROMPT, SemanticCorrectionClient
from relay.voice_context import VoiceContextRegistry, extract_conversation, extract_response_text


class TestVoiceContext(unittest.TestCase):
    def test_extract_conversation_keeps_only_top_level_user_assistant_text(self):
        messages = [
            {"role": "system", "content": "system instructions"},
            {"role": "user", "content": [
                {"type": "text", "text": "请改一下配置"},
                {"type": "tool_result", "content": [{"type": "text", "text": "secret output"}]},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "可以这样处理"},
                {"type": "tool_use", "input": {"text": "hidden tool input"}},
            ]},
            {"role": "assistant", "content": [{"type": "tool_use", "input": {"x": 1}}]},
        ]
        self.assertEqual(extract_conversation(messages), [
            ("user", "请改一下配置"),
            ("assistant", "可以这样处理"),
        ])

    def test_extract_response_text_requires_completed_response(self):
        complete = json.dumps({
            "content": [
                {"type": "thinking", "thinking": "do not retain"},
                {"type": "text", "text": "这是助手回答"},
            ],
            "stop_reason": "end_turn",
        }).encode()
        incomplete = json.dumps({"content": [{"type": "text", "text": "partial"}]}).encode()
        self.assertEqual(extract_response_text(complete), "这是助手回答")
        self.assertEqual(extract_response_text(incomplete), "")

    def test_extract_sse_text_requires_message_stop(self):
        body = (
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":"你好"}}\n\n'
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"世界"}}\n\n'
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        ).encode()
        self.assertEqual(extract_response_text(body), "你好世界")
        self.assertEqual(extract_response_text(body.replace(b"message_stop", b"message_end")), "")

    def test_registry_disambiguates_multiple_sessions_using_window_title(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        tok_a = registry.observe_request(
            "diagnostics-session", 200, "relay-main",
            [{"role": "user", "content": "diag"}], now=10,
        )
        registry.observe_response(tok_a, json.dumps({
            "content": [{"type": "text", "text": "diag answer"}],
            "stop_reason": "end_turn",
        }).encode(), now=11)

        tok_b = registry.observe_request(
            "feature-session", 201, "relay-main",
            [{"role": "user", "content": "feat"}], now=12,
        )
        registry.observe_response(tok_b, json.dumps({
            "content": [{"type": "text", "text": "feat answer"}],
            "stop_reason": "end_turn",
        }).encode(), now=13)

        snap = registry.snapshot_for_target(100, target_title="✳ diagnostics-session", parent_map={200: 100, 201: 100}, now=14)
        self.assertEqual(snap.session_id, "diagnostics-session")
        self.assertIn("diag answer", snap.context)

        snap_recent = registry.snapshot_for_target(100, target_title="Windows PowerShell", parent_map={200: 100, 201: 100}, now=14)
        self.assertEqual(snap_recent.session_id, "feature-session")
        self.assertIn("feat answer", snap_recent.context)

    def test_registry_returns_none_when_no_session_history(self):
        registry = VoiceContextRegistry()
        snap = registry.snapshot_for_target(100, target_title="Windows Terminal", now=14)
        self.assertIsNone(snap)

    def test_older_response_cannot_overwrite_newer_request(self):
        registry = VoiceContextRegistry()
        initial = registry.observe_request("s", 7, "m", [{"role": "user", "content": "initial"}], now=1)
        registry.observe_response(initial, json.dumps({
            "content": [{"type": "text", "text": "initial answer"}],
            "stop_reason": "end_turn",
        }).encode(), now=2)
        old = registry.observe_request("s", 7, "m", [{"role": "user", "content": "old"}], now=3)
        new = registry.observe_request("s", 7, "m", [{"role": "user", "content": "new"}], now=4)
        before_completion = registry.snapshot_for_target(7, {7: 7}, now=5)
        self.assertIn("initial answer", before_completion.context)
        self.assertNotIn("old", before_completion.context)
        registry.observe_response(old, json.dumps({
            "content": [{"type": "text", "text": "stale"}],
            "stop_reason": "end_turn",
        }).encode(), now=6)
        registry.observe_response(new, json.dumps({
            "content": [{"type": "text", "text": "fresh"}],
            "stop_reason": "end_turn",
        }).encode(), now=7)
        snap = registry.snapshot_for_target(7, {7: 7}, now=8)
        self.assertIn("用户: new", snap.context)
        self.assertIn("助手: fresh", snap.context)
        self.assertNotIn("stale", snap.context)

    def test_subagents_and_missing_ids_are_not_indexed(self):
        registry = VoiceContextRegistry()
        self.assertIsNone(registry.observe_request("", 1, "m", [{"role": "user", "content": "x"}]))
        self.assertIsNone(registry.observe_request("s", 1, "m", [{"role": "user", "content": "x"}], is_main_session=False))
        self.assertIsNone(registry.snapshot_for_target(1, {1: 1}))

    def test_render_dialogue_is_pure_and_preserves_clean_paragraphs(self):
        messages = [
            ("user", "请帮我分析一下问题"),
            ("assistant", "这两个问题定位非常精准！\n\n问题一：关于上下文消息。\n问题二：关于在抓包里查看。"),
        ]
        registry = VoiceContextRegistry(max_chars=500)
        rendered = registry._render(messages)
        self.assertNotIn("参考标识符/术语:", rendered)
        self.assertIn("用户: 请帮我分析一下问题", rendered)
        self.assertIn("助手: 这两个问题定位非常精准！", rendered)
        self.assertIn("问题一：关于上下文消息。", rendered)
        self.assertIn("问题二：关于在抓包里查看。", rendered)

    def test_single_active_session_automatically_matches_unrelated_pid_and_empty_title(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        tok = registry.observe_request(
            "single-session", 9999, "relay-main",
            [{"role": "user", "content": "单个活跃会话"}], now=10,
        )
        registry.observe_response(tok, json.dumps({
            "content": [{"type": "text", "text": "这是唯一会话回答"}],
            "stop_reason": "end_turn",
        }).encode(), now=11)

        snap = registry.snapshot_for_target(1234, target_title="", parent_map={9999: 8888}, now=12)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "single-session")
        self.assertIn("这是唯一会话回答", snap.context)

    def test_multiple_sessions_fallback_to_mru_with_empty_title(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        tok1 = registry.observe_request("older-sess", 101, "m", [{"role": "user", "content": "1"}], now=1)
        registry.observe_response(tok1, json.dumps({"content": [{"type": "text", "text": "旧"}], "stop_reason": "end_turn"}).encode(), now=2)

        tok2 = registry.observe_request("newer-sess", 102, "m", [{"role": "user", "content": "2"}], now=3)
        registry.observe_response(tok2, json.dumps({"content": [{"type": "text", "text": "新"}], "stop_reason": "end_turn"}).encode(), now=4)

        snap = registry.snapshot_for_target(999, target_title="", parent_map={}, now=5)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "newer-sess")

    def test_pending_only_request_is_selected_when_response_not_yet_received(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        tok = registry.observe_request(
            "pending-session", 300, "relay-main",
            [{"role": "user", "content": "正在执行的代码与提示词"}], now=10,
        )
        self.assertIsNotNone(tok)

        snap = registry.snapshot_for_target(300, target_title="", parent_map={300: 300}, now=11)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "pending-session")
        self.assertIn("正在执行的代码与提示词", snap.context)

        snap_global = registry.snapshot_for_target(999, target_title="", parent_map={}, now=12)
        self.assertIsNotNone(snap_global)
        self.assertEqual(snap_global.session_id, "pending-session")


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.payload


class _FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return self.response


class TestVoiceCorrectionRelay(unittest.TestCase):
    def test_correction_uses_current_route_once_without_tools(self):
        response_body = json.dumps({
            "content": [{"type": "text", "text": "请修复这个语义校正功能"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        conf = {
            "router": {"route": "deepseek"},
            "upstreams": {
                "deepseek": {"base": "https://example.invalid/anthropic", "key_env": ""},
            },
            "custom_providers": {},
        }
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value="test-key"):
            result = cc_relay.perform_voice_correction(
                conf,
                model="relay-main",
                context="用户: 正在实现语音校正。\n助手: 使用当前 Relay 路由。",
                transcript="请修复这个语义校正攻能",
                timeout=3,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "请修复这个语义校正功能")
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.invalid/anthropic/v1/messages")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertNotIn("tools", payload)
        self.assertIn("正在实现语音校正", payload["messages"][0]["content"])
        self.assertIn("语义校正攻能", payload["messages"][0]["content"])
        self.assertLessEqual(timeout, 3)

    def test_explicit_correction_model_routes_to_corresponding_provider(self):
        conf = {
            "router": {
                "route": "hybrid",
                "tiers": {"main": "deepseek-flash"},
                "model_routes": {
                    "gpt-6-luna": "codex",
                    "gemini-2.5-flash": "antigravity",
                },
            },
            "upstreams": {
                "deepseek": {"base": "https://example.invalid/deepseek", "key_env": ""},
                "codex": {"base": "http://127.0.0.1:8317", "key_env": ""},
                "antigravity": {"base": "http://127.0.0.1:8045", "key_env": ""},
            },
            "custom_providers": {},
        }

        # 1. 指定 codex 上的 gpt 模型
        opener_codex = _FakeOpener(_FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "修正后的 codex 结果"}],
            "stop_reason": "end_turn",
        }).encode()))
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener_codex), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            res = cc_relay.perform_voice_correction(
                conf,
                model="gpt-6-luna",
                context="上下文",
                transcript="原始文本",
            )
        self.assertTrue(res["ok"])
        self.assertEqual(res["route"], "codex")
        self.assertEqual(opener_codex.requests[0][0].full_url, "http://127.0.0.1:8317/v1/messages")

        # 2. 指定 gemini 模型
        opener_gemini = _FakeOpener(_FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "修正后的 gemini 结果"}],
            "stop_reason": "end_turn",
        }).encode()))
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener_gemini), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            res = cc_relay.perform_voice_correction(
                conf,
                model="gemini-2.5-flash",
                context="上下文",
                transcript="原始文本",
            )
        self.assertTrue(res["ok"])
        self.assertEqual(res["route"], "antigravity")
        self.assertEqual(opener_gemini.requests[0][0].full_url, "http://127.0.0.1:8045/v1/messages")

    def test_voice_correction_thinking_is_low_for_codex_despite_global_high(self):
        conf = {
            "router": {
                "route": "codex",
                "effort": "high",
                "tier_efforts": {"main": "high"},
            },
            "upstreams": {
                "codex": {"base": "http://127.0.0.1:8317/v1", "key_env": ""},
            },
            "custom_providers": {},
        }
        opener = _FakeOpener(_FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "校正结果"}],
            "stop_reason": "end_turn",
        }).encode()))
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            res = cc_relay.perform_voice_correction(
                conf,
                model="gpt-6-luna",
                context="用户: 代码\n助手: 回答",
                transcript="测试文本",
            )
        self.assertTrue(res["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertIn("thinking", payload)
        self.assertEqual(payload["thinking"].get("type"), "enabled")
        self.assertEqual(payload["thinking"].get("budget_tokens"), 1024)

    def test_voice_correction_thinking_is_off_for_deepseek(self):
        conf = {
            "router": {
                "route": "deepseek",
                "effort": "high",
            },
            "upstreams": {
                "deepseek": {"base": "https://api.deepseek.com/anthropic", "key_env": ""},
            },
            "custom_providers": {},
        }
        opener = _FakeOpener(_FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "校正结果"}],
            "stop_reason": "end_turn",
        }).encode()))
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            res = cc_relay.perform_voice_correction(
                conf,
                model="deepseek-flash",
                context="上下文",
                transcript="测试文本",
            )
        self.assertTrue(res["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertIn("thinking", payload)
        self.assertEqual(payload["thinking"].get("type"), "disabled")

    def test_voice_correction_custom_effort_selection(self):
        conf = {
            "router": {"route": "codex", "effort": "low"},
            "upstreams": {"codex": {"base": "http://127.0.0.1:8317/v1", "key_env": ""}},
            "tools": {"voice": {"semantic_correction_effort": "medium"}},
            "custom_providers": {},
        }
        opener = _FakeOpener(_FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "校正结果"}],
            "stop_reason": "end_turn",
        }).encode()))
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            res = cc_relay.perform_voice_correction(
                conf,
                model="gpt-6-luna",
                context="上下文",
                transcript="测试文本",
            )
        self.assertTrue(res["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["thinking"].get("budget_tokens"), 8192)

    def test_config_api_accepts_and_returns_semantic_correction_effort(self):
        base_conf = {
            "tools": {
                "voice": {
                    "semantic_correction_enabled": True,
                    "semantic_correction_model": "deepseek-flash",
                    "semantic_correction_effort": "low",
                }
            }
        }
        view = cc_relay._config_public_view(base_conf)
        self.assertEqual(view["tools"]["voice"]["semantic_correction_effort"], "low")

        with mock.patch.object(cc_relay, "_save_conf"):
            for eff in ("off", "low", "medium", "high", "xhigh", "max", ""):
                data = {"tools": {"voice": {"semantic_correction_effort": eff}}}
                candidate = cc_relay._apply_config_update(base_conf, data)
                self.assertEqual(candidate["tools"]["voice"]["semantic_correction_effort"], eff)

            with self.assertRaises(ValueError):
                cc_relay._apply_config_update(base_conf, {"tools": {"voice": {"semantic_correction_effort": "invalid_effort"}}})

    def test_refusal_or_truncated_response_is_rejected(self):
        conf = {
            "router": {"route": "deepseek"},
            "upstreams": {"deepseek": {"base": "https://example.invalid", "key_env": ""}},
        }
        for stop_reason in ("refusal", "max_tokens", "tool_use"):
            opener = _FakeOpener(_FakeResponse(json.dumps({
                "content": [{"type": "text", "text": "unsafe candidate"}],
                "stop_reason": stop_reason,
            }).encode()))
            with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                    mock.patch.object(cc_relay, "_key_for", return_value=""):
                result = cc_relay.perform_voice_correction(
                    conf, "relay-main", "上下文", "原始文本", timeout=2,
                )
            self.assertFalse(result["ok"], stop_reason)

    def test_capture_context_falls_back_gracefully_when_unavailable(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")

        response_body = json.dumps({"available": False, "reason": "no_unique_session"}).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener):
            snapshot = client.capture_context(target)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["session_id"], "default")
        self.assertEqual(snapshot["context"], "")

    def test_capture_context_without_context_still_issues_correction(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")

        response_body = json.dumps({
            "content": [{"type": "text", "text": "已修正文本"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener):
            fallback_snapshot = {"session_id": "default", "model": "relay-main", "context": "", "revision": 0}
            res = client.correct(target, fallback_snapshot, "原始语音文本")

        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "已修正文本")
        self.assertEqual(len(opener.requests), 1)
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertNotIn("<conversation_context>", payload["messages"][0]["content"])
        self.assertIn("<recognized_transcript>", payload["messages"][0]["content"])

    def test_correction_with_valid_context_includes_context_block_and_session_id(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")
        valid_snapshot = {
            "session_id": "session-12345",
            "model": "relay-main",
            "context": "用户: 帮我 rebase 到 master\n\n助手: 好的，这就 rebase 到 master",
            "revision": 3,
        }
        response_body = json.dumps({
            "content": [{"type": "text", "text": "帮我 rebase 到 master"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener):
            res = client.correct(target, valid_snapshot, "帮我热贝斯到 master")

        self.assertTrue(res["ok"])
        self.assertEqual(len(opener.requests), 1)
        req, _ = opener.requests[0]
        self.assertEqual(req.headers.get("X-claude-code-session-id"), "session-12345")
        payload = json.loads(req.data.decode("utf-8"))
        content = payload["messages"][0]["content"]
        self.assertIn("<conversation_context>", content)
        self.assertIn("用户: 帮我 rebase 到 master", content)
        self.assertIn("助手: 好的，这就 rebase 到 master", content)
        self.assertIn("<recognized_transcript>", content)
        self.assertIn("帮我热贝斯到 master", content)

    def test_correction_cleans_outer_backticks_and_newlines(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")
        fallback_snapshot = {"session_id": "default", "model": "relay-main", "context": "", "revision": 0}

        response_body1 = json.dumps({
            "content": [{"type": "text", "text": " `git status` "}],
            "stop_reason": "end_turn",
        }).encode()
        opener1 = _FakeOpener(_FakeResponse(response_body1))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener1):
            res = client.correct(target, fallback_snapshot, "git status")
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "git status")

        response_body2 = json.dumps({
            "content": [{"type": "text", "text": "```bash\nrebase master\n```"}],
            "stop_reason": "end_turn",
        }).encode()
        opener2 = _FakeOpener(_FakeResponse(response_body2))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener2):
            res = client.correct(target, fallback_snapshot, "rebase master")
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "rebase master")

    def test_system_prompt_instructs_filler_removal_and_order_restructuring(self):
        self.assertIn("filler words", CORRECTION_SYSTEM_PROMPT)
        self.assertIn("Restructure sentence word order", CORRECTION_SYSTEM_PROMPT)
        self.assertIn("呃", CORRECTION_SYSTEM_PROMPT)

    def test_calls_snapshot_includes_purpose_and_app_for_voice_correction(self):
        record_item = {
            "idx": 9999,
            "ts": "2026-10-03 19:30:00",
            "path": "/v1/messages",
            "orig_model": "gpt-6-luna",
            "sent_model": "gpt-6-luna",
            "route": "codex",
            "route_reason": "hybrid:gpt-direct",
            "resp_status": 200,
            "headers": {
                "x-cc-relay-purpose": "voice-correction",
                "x-app": "voice-input",
            },
            "body": {"messages": [{"role": "user", "content": "text"}]},
            "resp_body": b'{"content":[{"type":"text","text":"ok"}]}',
        }
        with mock.patch("cc_relay._iter_records_tail", return_value=[record_item]), \
                mock.patch.dict("cc_relay._CALLS_CACHE", {}, clear=True):
            data = cc_relay._calls_snapshot(1)
            calls = data.get("calls") or []
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["purpose"], "voice-correction")
            self.assertEqual(calls[0]["app"], "voice-input")


if __name__ == "__main__":
    unittest.main()
