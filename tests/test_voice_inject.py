"""Win32 安全注入器单元测试"""

import unittest
from unittest import mock

from tools.voice_input.inject import (
    InjectionOutcome,
    InjectionResult,
    TargetWindowSnapshot,
    WindowsInjector,
    get_foreground_window,
    safe_backspace_count,
    send_backspace_and_paste_keystrokes,
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

    @mock.patch("tools.voice_input.inject.is_window_valid", return_value=True)
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_successful_injection(self, mock_get_clip, mock_paste, mock_set_clip, mock_pid, mock_hwnd, mock_is_valid):
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

    def test_safe_backspace_count_rejects_complex_unicode_and_lines(self):
        self.assertEqual(safe_backspace_count("请修正 /cost"), len("请修正 /cost"))
        self.assertIsNone(safe_backspace_count("第一行\n第二行"))
        self.assertIsNone(safe_backspace_count("emoji 😀"))
        self.assertIsNone(safe_backspace_count("é"))

    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_backspace_and_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_guarded_replace_deletes_exact_supported_character_count(
        self, mock_get_clip, mock_send, mock_set_clip, mock_pid, mock_hwnd
    ):
        mock_get_clip.side_effect = ["old clipboard", "校正版"]
        mock_send.return_value = True
        mock_set_clip.return_value = True
        mock_hwnd.return_value = 11111
        mock_pid.return_value = 100
        self.injector.activity_monitor.is_valid = mock.Mock(return_value=True)

        result = self.injector.replace_pasted_text(
            original_text="原始文本",
            corrected_text="校正版",
            target_hwnd=11111,
            target_pid=100,
            activity_lease="lease",
        )

        self.assertTrue(result)
        mock_send.assert_called_once_with(len("原始文本"))
        mock_set_clip.assert_any_call("校正版")
        mock_set_clip.assert_any_call("old clipboard")

    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_backspace_and_paste_keystrokes")
    def test_guarded_replace_skips_when_user_activity_invalidates_lease(self, mock_send, mock_set_clip):
        self.injector.activity_monitor.is_valid = mock.Mock(return_value=False)
        result = self.injector.replace_pasted_text(
            original_text="原始文本",
            corrected_text="校正版",
            target_hwnd=11111,
            target_pid=100,
            activity_lease="stale",
        )
        self.assertFalse(result)
        self.assertEqual(result.outcome, InjectionOutcome.ACTIVITY_CHANGED)
        mock_set_clip.assert_not_called()
        mock_send.assert_not_called()

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.restore_foreground_window")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_auto_reactivate_and_return_focus_on_window_switch(
        self, mock_get_clip, mock_paste, mock_set_clip, mock_pid, mock_fg, mock_restore, mock_valid
    ):
        injector = WindowsInjector(
            restore_clipboard=True,
            restore_delay=0.01,
            auto_reactivate_target=True,
            restore_switched_focus=True,
        )
        # 用户说完话切到了窗口 99999 (PID 200)，原目标是窗口 11111 (PID 100)
        mock_fg.side_effect = [99999, 11111, 11111]
        mock_pid.side_effect = [200, 100, 100]
        mock_valid.return_value = True
        mock_restore.return_value = True
        mock_set_clip.return_value = True
        mock_paste.return_value = True
        mock_get_clip.side_effect = ["old", "测试", "old"]

        res = injector.inject_text("测试", target_hwnd=11111, target_pid=100)

        self.assertTrue(res)
        self.assertEqual(res.outcome, InjectionOutcome.SUCCESS)
        # 验证调用序列：首先恢复目标窗口 11111，粘贴后归还焦点给 99999
        self.assertEqual(mock_restore.call_count, 2)
        mock_restore.assert_has_calls([mock.call(11111), mock.call(99999)])

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.restore_foreground_window")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_auto_reactivate_stays_on_target_when_return_focus_disabled(
        self, mock_get_clip, mock_paste, mock_set_clip, mock_pid, mock_fg, mock_restore, mock_valid
    ):
        injector = WindowsInjector(
            restore_clipboard=True,
            restore_delay=0.01,
            auto_reactivate_target=True,
            restore_switched_focus=False,
        )
        mock_fg.side_effect = [99999, 11111, 11111]
        mock_pid.side_effect = [200, 100, 100]
        mock_valid.return_value = True
        mock_restore.return_value = True
        mock_set_clip.return_value = True
        mock_paste.return_value = True
        mock_get_clip.side_effect = ["old", "测试", "old"]

        res = injector.inject_text("测试", target_hwnd=11111, target_pid=100)

        self.assertTrue(res)
        self.assertEqual(res.outcome, InjectionOutcome.SUCCESS)
        # 仅切回目标窗口 11111，不归还焦点
        self.assertEqual(mock_restore.call_count, 1)
        mock_restore.assert_called_once_with(11111)

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    def test_reactivate_fails_when_target_window_closed(
        self, mock_pid, mock_fg, mock_valid
    ):
        injector = WindowsInjector(auto_reactivate_target=True)
        mock_fg.return_value = 99999
        mock_pid.return_value = 200
        mock_valid.return_value = False  # 目标窗口已被关闭

        res = injector.inject_text("测试", target_hwnd=11111, target_pid=100)

        self.assertFalse(res)
        self.assertEqual(res.outcome, InjectionOutcome.FOCUS_CHANGED)
        self.assertIn("已关闭", res.message)

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    def test_auto_reactivate_disabled_rejects_on_mismatch(
        self, mock_pid, mock_fg, mock_valid
    ):
        injector = WindowsInjector(auto_reactivate_target=False)
        mock_fg.return_value = 99999
        mock_pid.return_value = 200
        mock_valid.return_value = True

        res = injector.inject_text("测试", target_hwnd=11111, target_pid=100)

        self.assertFalse(res)
        self.assertEqual(res.outcome, InjectionOutcome.FOCUS_CHANGED)
        self.assertIn("焦点已切换", res.message)

    def test_mouse_move_does_not_invalidate_activity_monitor(self):
        from tools.voice_input.inject import InputActivityMonitor, WM_MOUSEMOVE, WM_LBUTTONDOWN
        monitor = InputActivityMonitor()
        monitor._active_lease = (1, 0, 11111, 100)
        monitor._invalid = False

        # 模拟鼠标移动 WM_MOUSEMOVE
        monitor._on_mouse(0, WM_MOUSEMOVE, 0)
        self.assertFalse(monitor._invalid)

    @mock.patch("tools.voice_input.inject.get_foreground_window")
    def test_activity_monitor_allows_typing_in_other_window(self, mock_fg):
        from tools.voice_input.inject import InputActivityMonitor, WM_KEYDOWN
        monitor = InputActivityMonitor()
        monitor._active_lease = (1, 0, 11111, 100)
        monitor._invalid = False

        # 1. 用户在前台外窗 99999 (如 Chrome) 敲击键盘：不使目标租约失效
        mock_fg.return_value = 99999
        monitor._on_keyboard(0, WM_KEYDOWN, 0)
        self.assertFalse(monitor._invalid)

        # 2. 用户在前台目标窗口 11111 敲击键盘：标记活动并失效租约
        mock_fg.return_value = 11111
        monitor._on_keyboard(0, WM_KEYDOWN, 0)
        self.assertTrue(monitor._invalid)

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.restore_foreground_window")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_backspace_and_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_replace_pasted_text_reactivates_target_and_returns_focus(
        self, mock_get_clip, mock_send, mock_set_clip, mock_pid, mock_fg, mock_restore, mock_valid
    ):
        injector = WindowsInjector(
            restore_clipboard=True,
            restore_delay=0.01,
            auto_reactivate_target=True,
            restore_switched_focus=True,
        )
        injector.activity_monitor.is_valid = mock.Mock(return_value=True)

        # 用户当前在前台外窗 99999 (PID 200)，目标窗口是 11111 (PID 100)
        mock_fg.side_effect = [99999, 11111, 11111]
        mock_pid.side_effect = [200, 100, 100]
        mock_valid.return_value = True
        mock_restore.return_value = True
        mock_set_clip.return_value = True
        mock_send.return_value = True
        mock_get_clip.side_effect = ["old clipboard", "校正版", "old clipboard"]

        result = injector.replace_pasted_text(
            original_text="原始文本",
            corrected_text="校正版",
            target_hwnd=11111,
            target_pid=100,
            activity_lease="lease_token",
        )

        self.assertTrue(result)
        self.assertEqual(result.outcome, InjectionOutcome.SUCCESS)
        mock_send.assert_called_once_with(len("原始文本"))
        mock_set_clip.assert_any_call("校正版")
        # 验证调用序列：首先恢复目标窗口 11111，替换完成后归还焦点给外窗 99999
        self.assertEqual(mock_restore.call_count, 2)
        mock_restore.assert_has_calls([mock.call(11111), mock.call(99999)])

    @mock.patch("tools.voice_input.inject.is_window_valid")
    @mock.patch("tools.voice_input.inject.restore_foreground_window")
    @mock.patch("tools.voice_input.inject.get_foreground_window")
    @mock.patch("tools.voice_input.inject.get_window_pid")
    @mock.patch("tools.voice_input.inject.set_clipboard_text")
    @mock.patch("tools.voice_input.inject.send_backspace_and_paste_keystrokes")
    @mock.patch("tools.voice_input.inject.get_clipboard_text")
    def test_replace_pasted_text_reactivates_target_even_if_auto_reactivate_config_false(
        self, mock_get_clip, mock_send, mock_set_clip, mock_pid, mock_fg, mock_restore, mock_valid
    ):
        injector = WindowsInjector(
            restore_clipboard=True,
            restore_delay=0.01,
            auto_reactivate_target=False,
            restore_switched_focus=True,
        )
        injector.activity_monitor.is_valid = mock.Mock(return_value=True)

        mock_fg.side_effect = [99999, 11111, 11111]
        mock_pid.side_effect = [200, 100, 100]
        mock_valid.return_value = True
        mock_restore.return_value = True
        mock_set_clip.return_value = True
        mock_send.return_value = True
        mock_get_clip.side_effect = ["old clipboard", "校正版", "old clipboard"]

        result = injector.replace_pasted_text(
            original_text="原始文本",
            corrected_text="校正版",
            target_hwnd=11111,
            target_pid=100,
            activity_lease="lease_token",
        )

        self.assertTrue(result)
        self.assertEqual(result.outcome, InjectionOutcome.SUCCESS)
        self.assertEqual(mock_restore.call_count, 2)
        mock_restore.assert_has_calls([mock.call(11111), mock.call(99999)])


if __name__ == "__main__":
    unittest.main()
