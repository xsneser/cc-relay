"""Win32 安全注入器单元测试"""

import unittest
from unittest import mock

from tools.voice_input.inject import (
    InjectionOutcome,
    InjectionResult,
    TargetWindowSnapshot,
    WindowsInjector,
    get_foreground_window,
    get_window_pid,
    set_clipboard_text,
    get_clipboard_text,
)


class TestWindowsInjector(unittest.TestCase):
    def setUp(self):
        self.injector = WindowsInjector(restore_clipboard=True, restore_delay=0.01)

    def test_empty_text_rejected(self):
        res = self.injector.inject_text("", target_hwnd=123)
        self.assertFalse(res)
        self.assertEqual(res.outcome, InjectionOutcome.EMPTY_TEXT)

    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    def test_focus_changed_hwnd_mismatch(self, mock_pid, mock_hwnd):
        mock_hwnd.return_value = 99999
        mock_pid.return_value = 100
        # 目标是 11111，当前是 99999
        res = self.injector.inject_text("测试文本", target_hwnd=11111, target_pid=100)
        self.assertFalse(res)
        self.assertEqual(res.outcome, InjectionOutcome.FOCUS_CHANGED)

    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    def test_focus_changed_pid_mismatch(self, mock_pid, mock_hwnd):
        mock_hwnd.return_value = 11111
        mock_pid.return_value = 200
        # 目标 PID 是 100，当前是 200
        res = self.injector.inject_text("测试文本", target_hwnd=11111, target_pid=100)
        self.assertFalse(res)
        self.assertEqual(res.outcome, InjectionOutcome.FOCUS_CHANGED)

    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_successful_injection(self, mock_get_clip, mock_paste, mock_set_clip, mock_pid, mock_hwnd):
        mock_hwnd.return_value = 11111
        mock_pid.return_value = 100
        mock_set_clip.return_value = True
        mock_paste.return_value = True
        mock_get_clip.side_effect = ["old_data", "新文本", "old_data"]

        res = self.injector.inject_text("新文本", target_hwnd=11111, target_pid=100)
        self.assertTrue(res)
        self.assertEqual(res.outcome, InjectionOutcome.SUCCESS)

        # 验证写入了新文本
        mock_set_clip.assert_any_call("新文本")
        mock_paste.assert_called_once()


if __name__ == "__main__":
    unittest.main()
