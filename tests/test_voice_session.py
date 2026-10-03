"""会话调度器与状态机单元测试"""

import threading
import unittest
from unittest import mock

from tools.voice_input.config import VoiceConfig
from tools.voice_input.inject import (
    InjectionOutcome,
    InjectionResult,
    TargetWindowSnapshot,
    WindowsInjector,
)
from tools.voice_input.session import SessionCoordinator, SessionState


class FakeASREngine:
    def __init__(self):
        self._is_loaded = True
        self.created_sessions = []
        self.fed_chunks = []
        self.finalized_sessions = []
        self.cancelled_sessions = []

    @property
    def is_loaded(self):
        return self._is_loaded

    def get_capabilities(self):
        return {"engine": "fake", "is_loaded": True}

    def create_session(self, sid):
        self.created_sessions.append(sid)
        return mock.MagicMock()

    def feed_chunk(self, sid, pcm):
        self.fed_chunks.append((sid, pcm))
        return ("确认", "部分")

    def finalize_session(self, sid):
        self.finalized_sessions.append(sid)
        return "斜杠 cost。"

    def cancel_session(self, sid):
        self.cancelled_sessions.append(sid)


class FakeAudioRecorder:
    def __init__(self):
        self._recording = False
        self.chunks = [b"\x00" * 640, b"\x00" * 640]

    @property
    def is_recording(self):
        return self._recording

    def start(self):
        self._recording = True

    def get_chunk(self, timeout=0.05):
        if self.chunks:
            return self.chunks.pop(0)
        return None

    def drain_chunks(self):
        return [b"\x00" * 640]

    def stop(self):
        self._recording = False
        return b"\x00" * 1280


class TestVoiceSessionCoordinator(unittest.TestCase):
    def setUp(self):
        self.cfg = VoiceConfig()
        self.engine = FakeASREngine()
        self.injector = mock.MagicMock(spec=WindowsInjector)
        self.injector.inject_text.return_value = InjectionResult(True, InjectionOutcome.SUCCESS, "ok")
        self.recorder = FakeAudioRecorder()
        self.coord = SessionCoordinator(
            config=self.cfg,
            engine=self.engine,
            injector=self.injector,
            recorder=self.recorder,
        )

    def test_initial_state(self):
        self.assertEqual(self.coord.state, SessionState.IDLE)
        self.assertFalse(self.coord.is_recording)

    def test_reject_session_when_engine_loading(self):
        self.engine._is_loaded = False
        success = self.coord.start_session(source="widget", mode="toggle", output_mode="inject")
        self.assertFalse(success)
        self.assertEqual(self.coord.state, SessionState.IDLE)
        self.assertIn("加载中", self.coord.last_error)

    def test_reject_new_session_until_previous_finalization_completes(self):
        self.coord._state = SessionState.FINALIZING
        success = self.coord.start_session(source="hotkey", mode="ptt", output_mode="inject")
        self.assertFalse(success)
        self.assertFalse(self.recorder._recording)

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_start_and_stop_session(self, mock_snap):
        mock_snap.return_value = TargetWindowSnapshot(hwnd=12345, pid=6789, title="Test Window")

        # 启动会话
        success = self.coord.start_session(source="widget", mode="toggle", output_mode="inject")
        self.assertTrue(success)
        self.assertEqual(self.coord.state, SessionState.RECORDING)
        self.assertTrue(self.coord.is_recording)
        self.assertEqual(len(self.engine.created_sessions), 1)

        # 结束会话
        final_events = []
        self.coord.add_final_listener(lambda txt, res: final_events.append((txt, res)))

        self.coord.stop_session()

        self.assertEqual(self.coord.state, SessionState.IDLE)
        self.assertEqual(len(self.engine.finalized_sessions), 1)

        # 验证文本规范化 (斜杠 cost。 -> /cost)
        self.assertEqual(len(final_events), 1)
        txt, res = final_events[0]
        self.assertEqual(txt, "/cost")
        self.assertTrue(res.success)

        # 验证注入器被正确调用，且传入了目标 HWND 与 PID
        self.injector.inject_text.assert_called_once_with(
            text="/cost",
            target_hwnd=12345,
            target_pid=6789,
        )

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_cancel_session_suppresses_injection(self, mock_snap):
        mock_snap.return_value = TargetWindowSnapshot(hwnd=12345, pid=6789, title="Test Window")

        self.coord.start_session(source="hotkey", mode="ptt", output_mode="inject")
        pump_thread = self.coord._streaming_thread
        self.coord.cancel_session()

        self.assertFalse(pump_thread.is_alive())
        self.assertEqual(self.coord.state, SessionState.IDLE)
        self.assertEqual(len(self.engine.cancelled_sessions), 1)
        # 绝不调用注入
        self.injector.inject_text.assert_not_called()

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_preview_mode_does_not_inject(self, mock_snap):
        mock_snap.return_value = TargetWindowSnapshot(hwnd=12345, pid=6789, title="Test Window")

        self.coord.start_session(source="web", mode="toggle", output_mode="preview")
        self.coord.stop_session()

        self.injector.inject_text.assert_not_called()

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_correction_runs_once_and_replaces_using_frozen_context(self, mock_snap):
        mock_snap.return_value = TargetWindowSnapshot(hwnd=12345, pid=6789, title="Test Window")
        self.cfg.semantic_correction_enabled = True
        context = {
            "session_id": "cli-session",
            "model": "relay-main",
            "context": "用户: 正在处理 Relay。\n助手: 保留当前路由。",
            "revision": 3,
        }
        self.coord._correction_client = mock.Mock()
        self.coord._correction_client.capture_context.return_value = context
        self.coord._correction_client.correct.return_value = {
            "ok": True,
            "text": "请修正语义校正功能",
        }
        self.injector.begin_input_lease.return_value = "lease"
        self.injector.replace_pasted_text.return_value = InjectionResult(True, InjectionOutcome.SUCCESS, "applied")
        applied = threading.Event()
        self.coord.add_correction_listener(
            lambda status, text, message: applied.set() if status == "applied" else None
        )

        self.coord.start_session(source="widget", mode="toggle", output_mode="inject")
        self.coord.stop_session()

        self.assertTrue(applied.wait(2))
        self.coord._correction_client.capture_context.assert_called_once_with(mock_snap.return_value)
        self.coord._correction_client.correct.assert_called_once_with(
            mock_snap.return_value, context, "/cost"
        )
        self.injector.replace_pasted_text.assert_called_once_with(
            original_text="/cost",
            corrected_text="请修正语义校正功能",
            target_hwnd=12345,
            target_pid=6789,
            activity_lease="lease",
        )
        self.assertEqual(self.coord.last_transcript, "请修正语义校正功能")

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_cancel_during_finalization_suppresses_late_injection(self, mock_snap):
        entered_finalize = threading.Event()
        release_finalize = threading.Event()

        class BlockingEngine(FakeASREngine):
            def finalize_session(inner_self, sid):
                entered_finalize.set()
                release_finalize.wait(1)
                return "迟到文本"

        mock_snap.return_value = TargetWindowSnapshot(hwnd=12345, pid=6789, title="Test Window")
        self.coord.engine = BlockingEngine()
        self.coord.start_session(source="hotkey", mode="ptt", output_mode="inject")
        stopper = threading.Thread(target=self.coord.stop_session, daemon=True)
        stopper.start()
        self.assertTrue(entered_finalize.wait(1))
        self.coord.cancel_session()
        release_finalize.set()
        stopper.join(1)

        self.assertFalse(stopper.is_alive())
        self.injector.inject_text.assert_not_called()

    def test_failure_message_formatting(self):
        fmt = SessionCoordinator._format_correction_failure_message
        self.assertEqual(fmt({"error_type": "timeout"}, "network_error_timed out"), "校正超时，已保留原文")
        self.assertEqual(fmt({"error_type": "connection_refused"}, "10061"), "中转未启动 (8400端口)")
        self.assertEqual(fmt({}, "http_error_502"), "校正上游服务暂不可用 (502/503)")
        self.assertEqual(fmt({}, "http_error_429"), "校正上游请求超限 (429)")
        self.assertEqual(fmt({}, "invalid_correction_text"), "模型返回多行，已保留原文")
        self.assertEqual(fmt({}, "empty_correction_text"), "模型返回空文本，已保留原文")
        self.assertEqual(fmt({}, "correction_too_long"), "校正输出超长，已保留原文")

    @mock.patch("tools.voice_input.session.capture_target_snapshot")
    def test_start_session_uses_explicit_target_snapshot(self, mock_snap):
        explicit_snap = TargetWindowSnapshot(hwnd=54321, pid=9876, title="Explicit Window")
        success = self.coord.start_session(
            source="hotkey",
            mode="ptt",
            output_mode="inject",
            target_snapshot=explicit_snap,
        )
        self.assertTrue(success)
        mock_snap.assert_not_called()
        self.assertEqual(self.coord._current_session.target, explicit_snap)
        self.coord.cancel_session()

    @mock.patch("tools.voice_input.inject.is_window_valid")
    def test_retry_last_injection_prefers_original_target_when_valid(self, mock_valid):
        mock_valid.return_value = True
        self.coord.last_transcript = "测试补录"
        self.coord.last_target = TargetWindowSnapshot(hwnd=77777, pid=8888, title="Original Window")

        self.coord.retry_last_injection()

        self.injector.inject_text.assert_called_once_with(
            text="测试补录",
            target_hwnd=77777,
            target_pid=8888,
        )

    def test_long_correction_response_still_replaces_when_user_untouched(self):
        coord = self.coord
        client = mock.MagicMock()
        client.correct.return_value = {"ok": True, "text": "已校正文本"}
        coord._correction_client = client

        sess = mock.MagicMock()
        sess.target = TargetWindowSnapshot(hwnd=10, pid=20, title="T")
        sess.session_id = "s1"
        sess.correction_generation = 1
        coord._correction_generation = 1

        lease = (1, 1, 10, 20)
        job = {
            "generation": 1,
            "session": sess,
            "original": "原文本",
            "lease": lease,
            "context": {},
            "scheduled_at": 0.0, # scheduled 5s ago but user did not touch keys
        }
        coord._pending_correction = job

        coord.injector.replace_pasted_text.return_value = InjectionResult(
            success=True, outcome=InjectionOutcome.SUCCESS, message="已替换为语义校正文本"
        )

        notifications = []
        coord.add_correction_listener(lambda st, txt, msg: notifications.append((st, txt, msg)))

        coord._run_correction(job)

        self.assertTrue(any(st == "applied" and "已替换" in msg for st, _, msg in notifications))
        coord.injector.replace_pasted_text.assert_called_once()

    def test_async_context_capture_saved_even_after_session_cleared_by_stop(self):
        coord = self.coord
        capture_event = threading.Event()
        proceed_event = threading.Event()

        fake_snapshot = {
            "session_id": "real-cli-session",
            "model": "relay-main",
            "context": "用户: 之前的提问\n助手: 之前的回答",
            "revision": 2,
        }

        def _delayed_capture(target):
            capture_event.set()
            proceed_event.wait(timeout=1.0)
            return fake_snapshot

        client = mock.MagicMock()
        client.capture_context.side_effect = _delayed_capture
        coord._correction_client = client

        # 1. 启动会话
        success = coord.start_session(source="hotkey", mode="ptt", output_mode="inject")
        self.assertTrue(success)
        sess = coord._current_session
        self.assertIsNotNone(sess)

        # 确保后台捕获线程已开始执行
        capture_event.wait(timeout=1.0)

        # 2. 模拟停止会话（此时 stop_session 会将 _current_session 置为 None 并等待 context_thread）
        # 允许 _delayed_capture 完成
        proceed_event.set()
        coord.stop_session()

        # 3. 验证即使 _current_session 已为 None，异步捕获的结果仍成功写入 sess.context_snapshot
        self.assertIsNotNone(sess.context_snapshot)
        self.assertEqual(sess.context_snapshot["session_id"], "real-cli-session")
        self.assertIn("之前的回答", sess.context_snapshot["context"])


if __name__ == "__main__":
    unittest.main()
