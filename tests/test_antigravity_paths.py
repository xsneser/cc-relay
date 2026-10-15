# -*- coding: utf-8 -*-
"""Unit tests for Antigravity Tools executable path discovery and resolution."""

import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay.antigravity_paths import (
    find_antigravity_exe,
    get_standard_antigravity_candidates,
    inspect_antigravity_exe,
)


class TestAntigravityPaths(unittest.TestCase):
    def test_explicit_valid_top_level_path(self):
        with mock.patch("os.path.isfile", return_value=True):
            fake = r"C:\Custom\Antigravity\antigravity-tools.exe"
            conf = {"antigravity_exe": fake}
            self.assertEqual(find_antigravity_exe(conf), os.path.abspath(fake))
            info = inspect_antigravity_exe(conf)
            self.assertEqual(info["source"], "antigravity_exe")
            self.assertTrue(info["configured_valid"])
            self.assertFalse(info["is_fallback"])

    def test_explicit_valid_nested_tools_path(self):
        with mock.patch("os.path.isfile", return_value=True):
            fake = r"C:\Nested\Antigravity\antigravity-tools.exe"
            conf = {"tools": {"antigravity": {"exe": fake}}}
            self.assertEqual(find_antigravity_exe(conf), os.path.abspath(fake))
            info = inspect_antigravity_exe(conf)
            self.assertEqual(info["source"], "tools.antigravity.exe")
            self.assertTrue(info["configured_valid"])
            self.assertFalse(info["is_fallback"])

    def test_quoted_and_whitespace_stripped(self):
        with mock.patch("os.path.isfile", return_value=True):
            fake = r'  "C:\Program Files\Antigravity Tools\antigravity-tools.exe"  '
            conf = {"antigravity_exe": fake}
            expected = os.path.abspath(r"C:\Program Files\Antigravity Tools\antigravity-tools.exe")
            self.assertEqual(find_antigravity_exe(conf), expected)

    def test_invalid_explicit_path_returns_empty_by_default(self):
        fake_bad = r"C:\NonExistent\bad.exe"
        with mock.patch("os.path.isfile", return_value=False):
            conf = {"antigravity_exe": fake_bad}
            self.assertEqual(find_antigravity_exe(conf), "")
            info = inspect_antigravity_exe(conf)
            self.assertEqual(info["source"], "antigravity_exe")
            self.assertFalse(info["configured_valid"])
            self.assertEqual(info["configured_path"], fake_bad)

    def test_invalid_explicit_path_with_allow_fallback(self):
        fake_bad = r"C:\NonExistent\bad.exe"
        fake_good_std = r"C:\Users\test\AppData\Local\Antigravity Tools\antigravity-tools.exe"

        def fake_isfile(p):
            return os.path.abspath(p) == os.path.abspath(fake_good_std)

        with mock.patch("os.path.isfile", side_effect=fake_isfile), \
             mock.patch("relay.antigravity_paths.get_standard_antigravity_candidates", return_value=[fake_good_std]):
            conf = {"antigravity_exe": fake_bad}
            self.assertEqual(find_antigravity_exe(conf, allow_fallback=True), os.path.abspath(fake_good_std))
            info = inspect_antigravity_exe(conf, allow_fallback=True)
            self.assertEqual(info["source"], "standard")
            self.assertFalse(info["configured_valid"])
            self.assertTrue(info["is_fallback"])

    def test_empty_conf_resolves_standard_installation(self):
        fake_std = r"C:\Users\test\AppData\Local\Antigravity Tools\antigravity-tools.exe"

        def fake_isfile(p):
            return os.path.abspath(p) == os.path.abspath(fake_std)

        with mock.patch("os.path.isfile", side_effect=fake_isfile), \
             mock.patch("relay.antigravity_paths.get_standard_antigravity_candidates", return_value=[fake_std]):
            self.assertEqual(find_antigravity_exe({}), os.path.abspath(fake_std))
            info = inspect_antigravity_exe({})
            self.assertEqual(info["source"], "standard")
            self.assertFalse(info["is_fallback"])

    def test_path_environment_variable_resolution(self):
        fake_path_exe = r"C:\Tools\antigravity-tools.exe"

        with mock.patch("os.path.isfile", side_effect=lambda p: os.path.abspath(p) == os.path.abspath(fake_path_exe)), \
             mock.patch("relay.antigravity_paths.get_standard_antigravity_candidates", return_value=[]), \
             mock.patch("shutil.which", return_value=fake_path_exe):
            self.assertEqual(find_antigravity_exe({}), os.path.abspath(fake_path_exe))
            info = inspect_antigravity_exe({})
            self.assertEqual(info["source"], "path")

    def test_no_valid_candidates_returns_empty(self):
        with mock.patch("os.path.isfile", return_value=False), \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            self.assertEqual(find_antigravity_exe({}), "")
            info = inspect_antigravity_exe({})
            self.assertEqual(info["source"], "missing")
            self.assertEqual(info["path"], "")


if __name__ == "__main__":
    unittest.main()
