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
        self.assertEqual(self.widget.status_label.cget("text"), "按快捷键说话")

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
        self.assertEqual(self.widget.status_label.cget("text"), "● 按快捷键说话")

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
        self.coord.config.engine = "sensevoice_offline"
        self.widget.create_window()

        # 检查模型徽章在 SenseVoice 就绪时显示 SenseVoice · CPU
        self.assertIsNotNone(self.widget.model_badge)
        self.assertEqual(self.widget.model_badge.cget("text"), "SenseVoice · CPU")

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
        self.assertEqual(self.widget.status_label.cget("text"), "按快捷键说话")

    def test_left_release_does_not_toggle_record(self):
        self.widget.create_window()
        event = mock.MagicMock()
        self.widget._on_left_release(event)
        self.assertFalse(self.coord.is_recording)

    def test_preflight_qwen_badge(self):
        # 当配置为 qwen 引擎时，徽章展示 Qwen 1.7B 及运行硬件平台
        self.coord.config.engine = "qwen_2pass"
        self.widget.create_window()
        badge_txt = self.widget.model_badge.cget("text")
        self.assertIn("Qwen 1.7B", badge_txt)
        self.assertTrue("GPU" in badge_txt or "CPU" in badge_txt)

    def test_qwen_badge_detection(self):
        class FakeQwenEngineGPU:
            is_loaded = True
            def get_capabilities(self):
                return {"engine": "qwen_2pass", "device": "cuda:0"}

        self.coord.engine = FakeQwenEngineGPU()
        self.widget.create_window()
        self.assertEqual(self.widget.model_badge.cget("text"), "Qwen 1.7B · GPU")

    def test_qwen_cpu_badge_detection(self):
        class FakeQwenEngineCPU:
            is_loaded = True
            def get_capabilities(self):
                return {"engine": "qwen_2pass", "device": "cpu"}

        self.coord.config.device = "cpu"
        self.coord.engine = FakeQwenEngineCPU()
        self.widget.create_window()
        self.assertEqual(self.widget.model_badge.cget("text"), "Qwen 1.7B · CPU")

    def test_missing_model_set_error(self):
        self.widget.create_window()
        self.widget.set_error("未在本地检测到 Qwen ASR 1.7B 离线模型，无法启动。请在 Web 控制台点击【一键自动下载】")
        self.widget.root.update()
        self.assertEqual(self.widget.status_label.cget("text"), "✕ 无法启动 (缺少模型)")
        self.assertEqual(self.widget.partial_label.cget("text"), "请在 Web 控制台点击自动下载")

    @mock.patch("os._exit")
    def test_btn_close_and_close_system(self, mock_exit):
        close_called = []
        def my_on_close():
            close_called.append(True)

        widget = DesktopVoiceWidget(self.coord, on_close=my_on_close)
        widget.create_window()
        self.assertIsNotNone(widget.btn_close)
        self.assertEqual(widget.btn_close.cget("text"), "✕")

        widget.close_system()
        self.assertTrue(close_called)
        self.assertIsNone(widget.root)


if __name__ == "__main__":
    unittest.main()
