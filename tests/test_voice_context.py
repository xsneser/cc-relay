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

    def test_render_dialogue_is_pure_and_preserves_clean_paragraphs(self):
        messages = [
            ("user", "请帮我分析一下问题"),
            ("assistant", "这两个问题定位非常精准！\n\n问题一：关于上下文消息。\n问题二：关于在抓包里查看。"),
        ]
        registry = VoiceContextRegistry(max_chars=500)
        rendered = registry._render(messages)
        # 确保没有伪术语头部，完全是纯净的原汁原味对话
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

    def test_multiple_sessions_disambiguated_by_session_name_in_window_title(self):
        registry = VoiceContextRegistry(ttl_seconds=60)
        # Session A: release-cc-relay-v2410 (PID 201, updated at 12)
        tok_a = registry.observe_request("uuid-sess-a", 201, "m", [{"role": "user", "content": "release v2.4.10"}], now=10)
        registry.observe_response(tok_a, json.dumps({"content": [{"type": "text", "text": "release answer"}], "stop_reason": "end_turn"}).encode(), now=12)

        # Session B: seamless-focus-replacement (PID 202, updated earlier at 8)
        tok_b = registry.observe_request("uuid-sess-b", 202, "m", [{"role": "user", "content": "seamless focus test"}], now=5)
        registry.observe_response(tok_b, json.dumps({"content": [{"type": "text", "text": "focus answer"}], "stop_reason": "end_turn"}).encode(), now=8)

        # 模拟 session_name 绑定
        with registry._lock:
            registry._entries[("uuid-sess-a", 201)]["session_name"] = "release-cc-relay-v2410"
            registry._entries[("uuid-sess-b", 202)]["session_name"] = "seamless-focus-replacement"

        # 即使 Session A 更新时间更晚（12 > 8），但当前活动窗口标题为 "◑ seamless-focus-replacement" 时，必须精准选取 Session B！
        snap = registry.snapshot_for_target(
            target_pid=100,  # Windows Terminal 父进程 PID
            target_title="◑ seamless-focus-replacement",
            parent_map={201: 100, 202: 100},
            now=15,
        )
        self.assertIsNotNone(snap)
        self.assertEqual(snap.session_id, "uuid-sess-b")
        self.assertIn("focus answer", snap.context)
        self.assertNotIn("release answer", snap.context)

    def test_direct_disk_session_resolution_when_in_memory_missing_or_mismatched(self):
        from unittest import mock
        registry = VoiceContextRegistry(ttl_seconds=60)
        # 内存中只有会话 A
        registry.observe_request("sess-a", 101, "m", [{"role": "user", "content": "会话A内容"}])
        with registry._lock:
            registry._pending[("sess-a", 101)]["session_name"] = "session-a-name"

        # 模拟磁盘解析器返回会话 B 的直接读取结果
        mock_disk_sess = {
            "session_id": "sess-b-uuid",
            "session_name": "session-b-name",
            "ai_title": "cc-relay 架构与文件精简",
            "jsonl_path": "/path/to/b.jsonl",
        }
        mock_msgs = [
            ("user", "分析架构精简"),
            ("assistant", "这是关于架构精简的 Plan 计划内容"),
        ]
        with mock.patch("voice_context._find_session_from_disk", return_value=mock_disk_sess), \
                mock.patch("voice_context._read_session_messages_from_jsonl", return_value=mock_msgs):
            # 当当前窗口标题是“✳ cc-relay 架构与文件精简”时，即使内存只有会话 A，也坚决直读磁盘会话 B！
            snap = registry.snapshot_for_target(
                target_pid=11948,
                target_title="✳ cc-relay 架构与文件精简",
                parent_map={101: 11948},
            )
            self.assertIsNotNone(snap)
            self.assertEqual(snap.session_id, "sess-b-uuid")
            self.assertIn("这是关于架构精简的 Plan 计划内容", snap.context)
            self.assertNotIn("会话A内容", snap.context)


if __name__ == "__main__":
    unittest.main()
