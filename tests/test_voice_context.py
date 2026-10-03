import json
import unittest

from voice_context import VoiceContextRegistry, extract_conversation, extract_response_text


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

        # Title matching picks the specific active tab
        snap = registry.snapshot_for_target(100, target_title="✳ diagnostics-session", parent_map={200: 100, 201: 100}, now=14)
        self.assertEqual(snap.session_id, "diagnostics-session")
        self.assertIn("diag answer", snap.context)

        # Without title match, picks the most recently active session under this window
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

    def test_extract_technical_terms_and_render_header(self):
        from voice_context import extract_technical_terms
        messages = [
            ("user", "请修改 `cc_relay.py` 里的 `pick_route` 函数，并且调用 SessionCoordinator"),
            ("assistant", "好的，我们来看一下 tools/voice_input/session.py 中的实现以及 my_custom_var"),
        ]
        terms = extract_technical_terms(messages)
        self.assertIn("cc_relay.py", terms)
        self.assertIn("pick_route", terms)
        self.assertIn("SessionCoordinator", terms)
        self.assertIn("my_custom_var", terms)

        registry = VoiceContextRegistry(max_chars=500)
        rendered = registry._render(messages)
        self.assertIn("参考标识符/术语:", rendered)
        self.assertIn("pick_route", rendered)
        self.assertIn("用户: 请修改 `cc_relay.py`", rendered)

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

        # 进程树断开且标题为空时，单会话环境依然能成功自动关联
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

        # 无标题且 PID 不匹配时，自动 fallback 到最新的 newer-sess
        snap = registry.snapshot_for_target(999, target_title="", parent_map={}, now=5)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "newer-sess")

    def test_pending_only_request_is_selected_when_response_not_yet_received(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        # 仅调用 observe_request，没有 observe_response（模拟会话首轮、流式中或未解析）
        tok = registry.observe_request(
            "pending-session", 300, "relay-main",
            [{"role": "user", "content": "正在执行的代码与提示词"}], now=10,
        )
        self.assertIsNotNone(tok)

        # 进程匹配 pending 请求
        snap = registry.snapshot_for_target(300, target_title="", parent_map={300: 300}, now=11)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "pending-session")
        self.assertIn("正在执行的代码与提示词", snap.context)

        # 全局兜底同样能匹配 pending 请求
        snap_global = registry.snapshot_for_target(999, target_title="", parent_map={}, now=12)
        self.assertIsNotNone(snap_global)
        self.assertEqual(snap_global.session_id, "pending-session")


if __name__ == "__main__":
    unittest.main()
