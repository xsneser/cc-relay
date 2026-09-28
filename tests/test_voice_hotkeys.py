"""键盘与鼠标按键规范化与匹配单元测试"""

import unittest
from unittest import mock

from tools.voice_input.hotkeys import (
    HotkeyController,
    HotkeyState,
    key_to_name,
    KEY_ALIASES,
    MOUSE_BUTTON_ALIASES,
    VK_MOUSE_BUTTONS,
    VK_ESCAPE,
    WindowsMousePoller,
)


class MockKey:
    def __init__(self, name=None, char=None, vk=None):
        self.name = name
        self.char = char
        self.vk = vk


class TestVoiceHotkeys(unittest.TestCase):
    def test_key_to_name_special_keys(self):
        self.assertEqual(key_to_name(MockKey(name="caps_lock")), "caps_lock")
        self.assertEqual(key_to_name(MockKey(name="capslock")), "caps_lock")
        self.assertEqual(key_to_name(MockKey(name="ctrl_r")), "ctrl_r")
        self.assertEqual(key_to_name(MockKey(name="right_ctrl")), "ctrl_r")
        self.assertEqual(key_to_name(MockKey(name="f8")), "f8")
        self.assertEqual(key_to_name(MockKey(name="f12")), "f12")
        self.assertEqual(key_to_name(MockKey(name="space")), "space")
        self.assertEqual(key_to_name(MockKey(name="tab")), "tab")

    def test_key_to_name_char_keys(self):
        self.assertEqual(key_to_name(MockKey(char="a")), "a")
        self.assertEqual(key_to_name(MockKey(char="Q")), "q")
        self.assertEqual(key_to_name(MockKey(char="1")), "1")

    def test_key_to_name_vk_fkeys(self):
        # VK_F1 = 0x70 -> f1, VK_F9 = 0x78 -> f9
        self.assertEqual(key_to_name(MockKey(vk=0x70)), "f1")
        self.assertEqual(key_to_name(MockKey(vk=0x78)), "f9")
        self.assertEqual(key_to_name(MockKey(vk=0x7B)), "f12")

    def test_match_keyboard_key(self):
        ctrl_f9 = HotkeyController(hotkey_name="f9")
        ctrl_caps = HotkeyController(hotkey_name="caps_lock")
        ctrl_q = HotkeyController(hotkey_name="q")

        # F9
        self.assertTrue(ctrl_f9._match_keyboard_key(MockKey(name="f9")))
        self.assertFalse(ctrl_f9._match_keyboard_key(MockKey(name="f8")))

        # CapsLock
        self.assertTrue(ctrl_caps._match_keyboard_key(MockKey(name="caps_lock")))
        self.assertTrue(ctrl_caps._match_keyboard_key(MockKey(name="capslock")))

        # Char 'q'
        self.assertTrue(ctrl_q._match_keyboard_key(MockKey(char="Q")))
        self.assertFalse(ctrl_q._match_keyboard_key(MockKey(char="W")))

    def test_get_display_name(self):
        self.assertIn("后退键", HotkeyController("mouse_x1").get_display_name())
        self.assertIn("前进键", HotkeyController("mouse_x2").get_display_name())
        self.assertIn("CapsLock", HotkeyController("caps_lock").get_display_name())
        self.assertIn("F9", HotkeyController("f9").get_display_name())
        self.assertIn("右Ctrl", HotkeyController("ctrl_r").get_display_name())


    def test_vk_mouse_button_mappings(self):
        self.assertEqual(VK_MOUSE_BUTTONS.get("x1"), 0x05)
        self.assertEqual(VK_MOUSE_BUTTONS.get("x2"), 0x06)
        self.assertEqual(VK_MOUSE_BUTTONS.get("middle"), 0x04)
        self.assertEqual(VK_ESCAPE, 0x1B)

        for alias, target in MOUSE_BUTTON_ALIASES.items():
            self.assertIn(target, VK_MOUSE_BUTTONS)

    def test_mouse_trigger_state_machine(self):
        started = []
        stopped = []
        cancelled = []

        ctrl = HotkeyController(
            hotkey_name="mouse_x1",
            on_start_record=lambda: started.append(True),
            on_stop_record=lambda: stopped.append(True),
            on_cancel=lambda: cancelled.append(True),
        )

        self.assertEqual(ctrl.state, HotkeyState.IDLE)

        # 模拟按下鼠标侧键
        ctrl._on_mouse_trigger_down()
        self.assertEqual(ctrl.state, HotkeyState.RECORDING)

        # 重复按下应忽略
        ctrl._on_mouse_trigger_down()
        self.assertEqual(ctrl.state, HotkeyState.RECORDING)

        # 模拟松开鼠标侧键
        ctrl._on_mouse_trigger_up()
        self.assertEqual(ctrl.state, HotkeyState.PROCESSING)

        # 重置回空闲
        ctrl.set_idle()
        self.assertEqual(ctrl.state, HotkeyState.IDLE)

        # 测试 Esc 紧急取消
        ctrl._on_mouse_trigger_down()
        self.assertEqual(ctrl.state, HotkeyState.RECORDING)
        ctrl._on_esc_cancel()
        self.assertEqual(ctrl.state, HotkeyState.IDLE)


if __name__ == "__main__":
    unittest.main()
