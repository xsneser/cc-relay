import json
import unittest
from unittest import mock

import cc_relay
from tools.voice_input.config import VoiceConfig
from tools.voice_input.correction import SemanticCorrectionClient


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
        response_body = json.dumps({
            "content": [{"type": "text", "text": "校正后的文本"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        conf = {
            "router": {"route": "hybrid"},
            "upstreams": {
                "deepseek": {"base": "https://example.invalid/anthropic", "key_env": ""},
                "codex": {"base": "http://127.0.0.1:8317", "key_env": ""},
            },
            "custom_providers": {},
        }
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value="test-key"):
            result = cc_relay.perform_voice_correction(
                conf,
                model="gpt-5.6-sol",
                context="用户: 设定 Codex 专属模型。\n助手: 好的。",
                transcript="原始输入",
                timeout=3,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"], "codex")
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(len(opener.requests), 1)
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8317/v1/messages")

    def test_8401_client_issues_standard_v1_messages_call(self):
        cfg = VoiceConfig()
        cfg.relay_host = "127.0.0.1"
        cfg.relay_port = 8400
        cfg.fake_api_key = "sk-test-fake"
        cfg.semantic_correction_model = "gpt-5.6-sol"
        client = SemanticCorrectionClient(cfg)

        response_body = json.dumps({
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "已修正的句子"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener):
            target = mock.Mock(hwnd=1, pid=2, title="Test Window")
            context = {"session_id": "sess_abc", "model": "relay-main", "context": "用户: 你好", "revision": 1}
            res = client.correct(target, context, "测试原句")

        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "已修正的句子")
        self.assertEqual(len(opener.requests), 1)
        req, timeout = opener.requests[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8400/v1/messages")
        self.assertEqual(req.headers.get("Authorization"), "Bearer sk-test-fake")
        self.assertEqual(req.headers.get("X-claude-code-session-id"), "sess_abc")
        self.assertEqual(req.headers.get("X-app"), "voice-input")
        self.assertEqual(req.headers.get("X-cc-relay-purpose"), "voice-correction")

        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertIn("测试原句", payload["messages"][0]["content"])

    def test_voice_correction_thinking_is_low_for_codex_despite_global_high(self):
        response_body = json.dumps({
            "content": [{"type": "text", "text": "校正后的文本"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        conf = {
            "router": {
                "route": "hybrid",
                "effort": "high",
                "tiers": {"main": "gemini-3.8-flash-tiered"},
            },
            "upstreams": {
                "codex": {"base": "http://127.0.0.1:8317", "key_env": ""},
            },
            "custom_providers": {},
        }
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            result = cc_relay.perform_voice_correction(
                conf,
                model="gpt-6-luna",
                context="上下文",
                transcript="原始文本",
                timeout=3,
            )

        self.assertTrue(result["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        # 验证没有被注入 high (16384)，而是使用了 low 快速思考 (1024)
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 1024})
        self.assertGreaterEqual(payload["max_tokens"], 1536)

    def test_voice_correction_thinking_is_off_for_deepseek(self):
        response_body = json.dumps({
            "content": [{"type": "text", "text": "校正文本"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        conf = {
            "router": {
                "route": "deepseek",
                "effort": "high",
            },
            "upstreams": {
                "deepseek": {"base": "https://example.invalid/anthropic", "key_env": ""},
            },
            "custom_providers": {},
        }
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            result = cc_relay.perform_voice_correction(
                conf,
                model="deepseek-flash",
                context="上下文",
                transcript="原始文本",
                timeout=3,
            )

        self.assertTrue(result["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_correction_client_detects_timeout_and_refused(self):
        import urllib.error
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")
        context = {"session_id": "sess_abc", "model": "relay-main", "context": "", "revision": 1}

        # 1. 模拟超时异常
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener") as mock_bo:
            mock_opener = mock.Mock()
            mock_opener.open.side_effect = urllib.error.URLError("timed out")
            mock_bo.return_value = mock_opener
            res = client.correct(target, context, "测试原句")
            self.assertFalse(res["ok"])
            self.assertEqual(res.get("error_type"), "timeout")

        # 2. 模拟连接拒绝异常
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener") as mock_bo:
            mock_opener = mock.Mock()
            mock_opener.open.side_effect = urllib.error.URLError("[WinError 10061] 由于目标计算机积极拒绝")
            mock_bo.return_value = mock_opener
            res = client.correct(target, context, "测试原句")
            self.assertFalse(res["ok"])
            self.assertEqual(res.get("error_type"), "connection_refused")

    def test_voice_correction_custom_effort_selection(self):
        response_body = json.dumps({
            "content": [{"type": "text", "text": "校正文本"}],
            "stop_reason": "end_turn",
        }).encode()
        opener = _FakeOpener(_FakeResponse(response_body))
        conf = {
            "router": {"route": "hybrid"},
            "tools": {
                "voice": {
                    "semantic_correction_effort": "medium",
                }
            },
            "upstreams": {
                "codex": {"base": "http://127.0.0.1:8317", "key_env": ""},
            },
            "custom_providers": {},
        }
        with mock.patch.object(cc_relay.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cc_relay, "_key_for", return_value=""):
            result = cc_relay.perform_voice_correction(
                conf,
                model="gpt-6-luna",
                context="上下文",
                transcript="原始文本",
                timeout=3,
            )

        self.assertTrue(result["ok"])
        req, _ = opener.requests[0]
        payload = json.loads(req.data.decode("utf-8"))
        # medium effort -> budget_tokens 8192
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 8192})
        self.assertGreaterEqual(payload["max_tokens"], 8192 + 512)

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
        # 验证 public view 正确暴露
        view = cc_relay._config_public_view(base_conf)
        self.assertEqual(view["tools"]["voice"]["semantic_correction_effort"], "low")

        # 验证合法更新 (mock _save_conf 以防测试写盘副作用覆盖真实 config.json)
        with mock.patch.object(cc_relay, "_save_conf"):
            for eff in ("off", "low", "medium", "high", "xhigh", "max", ""):
                data = {"tools": {"voice": {"semantic_correction_effort": eff}}}
                candidate = cc_relay._apply_config_update(base_conf, data)
                self.assertEqual(candidate["tools"]["voice"]["semantic_correction_effort"], eff)

            # 验证非法更新抛出异常
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

        # 模拟 Relay 返回 available: false (如 no_unique_session)
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
        # 验证消息不包含 <conversation_context> 块，但包含 <recognized_transcript>
        self.assertNotIn("<conversation_context>", payload["messages"][0]["content"])
        self.assertIn("<recognized_transcript>", payload["messages"][0]["content"])

    def test_correction_with_valid_context_includes_context_block_and_session_id(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")
        valid_snapshot = {
            "session_id": "session-12345",
            "model": "relay-main",
            "context": "参考标识符/术语: git, rebase\n\n用户: 帮我 rebase 到 master",
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
        self.assertIn("参考标识符/术语: git, rebase", content)
        self.assertIn("<recognized_transcript>", content)
        self.assertIn("帮我热贝斯到 master", content)

    def test_correction_cleans_outer_backticks_and_newlines(self):
        cfg = VoiceConfig()
        client = SemanticCorrectionClient(cfg)
        target = mock.Mock(hwnd=1, pid=2, title="Test Window")
        fallback_snapshot = {"session_id": "default", "model": "relay-main", "context": "", "revision": 0}

        # Case 1: wrapped in backticks
        response_body1 = json.dumps({
            "content": [{"type": "text", "text": " `git status` "}],
            "stop_reason": "end_turn",
        }).encode()
        opener1 = _FakeOpener(_FakeResponse(response_body1))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener1):
            res = client.correct(target, fallback_snapshot, "git status")
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "git status")

        # Case 2: wrapped in code block
        response_body2 = json.dumps({
            "content": [{"type": "text", "text": "```bash\nrebase master\n```"}],
            "stop_reason": "end_turn",
        }).encode()
        opener2 = _FakeOpener(_FakeResponse(response_body2))
        with mock.patch("tools.voice_input.correction.urllib.request.build_opener", return_value=opener2):
            res = client.correct(target, fallback_snapshot, "rebase master")
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "rebase master")


if __name__ == "__main__":
    unittest.main()
