"""桌面悬浮胶囊组件测试"""

import unittest
from unittest import mock

from tools.voice_input.config import VoiceConfig
from tools.voice_input.desktop import DesktopVoiceWidget
from tools.voice_input.inject import InjectionOutcome, InjectionResult


class FakeCoordinator:
    def __init__(self):
        self.config = VoiceConfig()
        self.is_recording = False
        self.last_transcript = "测试转写"
        self.state_listeners = []
        self.partial_listeners = []
        self.final_listeners = []
        self.audio_level_listeners = []

    def add_state_listener(self, cb):
        self.state_listeners.append(cb)

    def add_partial_listener(self, cb):
        self.partial_listeners.append(cb)

    def add_final_listener(self, cb):
        self.final_listeners.append(cb)

    def add_audio_level_listener(self, cb):
        self.audio_level_listeners.append(cb)

    def start_session(self, **kwargs):
        self.is_recording = True
        return True

    def stop_session(self, **kwargs):
        self.is_recording = False

    def cancel_session(self):
        self.is_recording = False

    def retry_last_injection(self):
        return InjectionResult(True, InjectionOutcome.SUCCESS, "ok")


class TestDesktopVoiceWidget(unittest.TestCase):
    def setUp(self):
        self.coord = FakeCoordinator()
        self.widget = DesktopVoiceWidget(self.coord, default_x=100, default_y=100)

    def tearDown(self):
        self.widget.stop()

    @mock.patch("tools.voice_input.desktop.user32")
    def test_create_window_and_non_activate_style(self, mock_user32):
        mock_user32.GetParent.return_value = 12345
        mock_user32.GetWindowLongW.return_value = 0x00040000

        self.widget.create_window()

        self.assertIsNotNone(self.widget.root)
        self.assertEqual(self.widget.status_label.cget("text"), "点击语音输入")

        # 验证 Win32 SetWindowLongW 被调用且包含了 WS_EX_NOACTIVATE (0x08000000)
        mock_user32.SetWindowLongW.assert_called_once()
        args = mock_user32.SetWindowLongW.call_args[0]
        applied_style = args[2]
        self.assertTrue(applied_style & 0x08000000)  # WS_EX_NOACTIVATE

    def test_toggle_record(self):
        self.widget._toggle_record()
        self.assertTrue(self.coord.is_recording)

        self.widget._toggle_record()
        self.assertFalse(self.coord.is_recording)

    def test_cancel_click(self):
        self.coord.is_recording = True
        self.widget._on_cancel_click()
        self.assertFalse(self.coord.is_recording)

    def test_loading_and_set_ready(self):
        class FakeLoadingEngine:
            is_loaded = False
        self.coord.engine = FakeLoadingEngine()

        self.widget.create_window()
        self.assertIn("加载中", self.widget.status_label.cget("text"))

        # 触发 set_ready
        self.coord.engine.is_loaded = True
        self.widget.set_ready()
        self.widget.root.update()
        self.assertEqual(self.widget.status_label.cget("text"), "● 点击语音输入")

    def test_toggle_record_blocked_when_loading(self):
        class FakeLoadingEngine:
            is_loaded = False
        self.coord.engine = FakeLoadingEngine()
        self.widget.create_window()

        self.widget._toggle_record()
        self.assertFalse(self.coord.is_recording)
        self.assertIn("加载中", self.widget.status_label.cget("text"))

    def test_error_state_shows_feedback(self):
        from tools.voice_input.session import SessionState
        self.widget.create_window()
        self.coord.last_error = "麦克风设备未就绪"
        self.widget._apply_state_change(SessionState.ERROR, None)
        self.assertIn("麦克风设备未就绪", self.widget.status_label.cget("text"))

    @mock.patch.object(VoiceConfig, "is_sensevoice_installed", return_value=True)
    def test_model_badge_and_vertical_expansion(self, mock_sv):
        from tools.voice_input.session import SessionState
        self.widget.create_window()

        # 检查模型徽章在 SenseVoice 就绪时显示 SenseVoice
        self.assertIsNotNone(self.widget.model_badge)
        self.assertEqual(self.widget.model_badge.cget("text"), "SenseVoice")

        # 触发录音，验证向下竖向展开抽屉
        self.widget._apply_state_change(SessionState.RECORDING, None)
        self.widget.root.update()
        self.assertTrue(self.widget.drawer_frame.winfo_ismapped())
        self.assertEqual(self.widget.status_label.cget("text"), "● 正在聆听")

        # 触发流式文字更新
        self.widget._apply_partial_text("测试流式语音输入")
        self.assertIn("测试流式语音输入", self.widget.stream_label.cget("text"))

        # 恢复折叠
        self.widget._collapse_capsule()
        self.widget.root.update()
        self.assertFalse(self.widget.drawer_frame.winfo_ismapped())
        self.assertEqual(self.widget.status_label.cget("text"), "点击语音输入")

    @mock.patch.object(VoiceConfig, "is_sensevoice_installed", return_value=False)
    def test_preflight_fallback_detection(self, mock_sv):
        # 当 SenseVoice 未下载但本地已缓存 Paraformer 时，启动前预检直接显示 Paraformer
        self.widget.create_window()
        self.assertEqual(self.widget.model_badge.cget("text"), "Paraformer")

    def test_paraformer_badge_detection(self):
        class FakeParaformerEngine:
            is_loaded = True
            def get_capabilities(self):
                return {"engine": "paraformer_streaming_2pass"}
        self.coord.engine = FakeParaformerEngine()
        self.widget.create_window()
        self.assertEqual(self.widget.model_badge.cget("text"), "Paraformer")


if __name__ == "__main__":
    unittest.main()
