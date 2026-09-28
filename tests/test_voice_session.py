"""会话调度器与状态机单元测试"""

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
        self.coord.cancel_session()

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


if __name__ == "__main__":
    unittest.main()
