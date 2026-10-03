"""Tcl/Tk 运行时自愈与桌面图形能力测试"""

import os
import unittest
from pathlib import Path
from unittest import mock

from tools.voice_input import tk_runtime
from tools.voice_input.tk_runtime import (
    _is_valid_tcl_dir,
    _is_valid_tk_dir,
    find_tcl_tk_dirs,
    setup_tk_environment,
    validate_tk_runtime,
)
from tools.voice_input.runtime import find_voice_python, _probe_desktop_capability


class TestVoiceTkRuntime(unittest.TestCase):
    def test_dir_validators(self):
        tmp_dir = Path(os.environ.get("TEMP", ".")) / "test_tk_val"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.assertFalse(_is_valid_tcl_dir(tmp_dir))
            self.assertFalse(_is_valid_tk_dir(tmp_dir))

            (tmp_dir / "init.tcl").write_text("# tcl", encoding="utf-8")
            self.assertTrue(_is_valid_tcl_dir(tmp_dir))
            self.assertFalse(_is_valid_tk_dir(tmp_dir))

            (tmp_dir / "tk.tcl").write_text("# tk", encoding="utf-8")
            self.assertTrue(_is_valid_tk_dir(tmp_dir))
        finally:
            for f in tmp_dir.glob("*"):
                try:
                    f.unlink()
                except Exception:
                    pass
            try:
                tmp_dir.rmdir()
            except Exception:
                pass

    def test_find_tcl_tk_dirs(self):
        tcl_dir, tk_dir = find_tcl_tk_dirs()
        self.assertIsNotNone(tcl_dir, "应当能探测到至少一个有效的 Tcl 目录")
        self.assertIsNotNone(tk_dir, "应当能探测到至少一个有效的 Tk 目录")
        self.assertTrue((tcl_dir / "init.tcl").is_file())
        self.assertTrue((tk_dir / "tk.tcl").is_file())

    def test_setup_tk_environment_injection(self):
        # 模拟环境变量未设置场景
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TCL_LIBRARY", None)
            os.environ.pop("TK_LIBRARY", None)

            env_vars = setup_tk_environment()
            self.assertIn("TCL_LIBRARY", env_vars)
            self.assertIn("TK_LIBRARY", env_vars)
            self.assertTrue(Path(env_vars["TCL_LIBRARY"]).is_dir())
            self.assertTrue((Path(env_vars["TCL_LIBRARY"]) / "init.tcl").is_file())

    def test_setup_tk_environment_preserves_valid(self):
        tcl_dir, tk_dir = find_tcl_tk_dirs()
        if tcl_dir and tk_dir:
            with mock.patch.dict(os.environ, {
                "TCL_LIBRARY": str(tcl_dir),
                "TK_LIBRARY": str(tk_dir),
            }):
                env_vars = setup_tk_environment()
                self.assertEqual(env_vars.get("TCL_LIBRARY"), str(tcl_dir))
                self.assertEqual(env_vars.get("TK_LIBRARY"), str(tk_dir))

    def test_validate_tk_runtime_current(self):
        self.assertTrue(validate_tk_runtime(), "当前环境应能成功验证 Tkinter 运行能力")

    def test_validate_tk_runtime_caches_probe_result(self):
        cache = tk_runtime._TK_VALIDATION_CACHE
        cache.clear()
        try:
            with mock.patch.object(tk_runtime, "setup_tk_environment", return_value={}), \
                    mock.patch.object(tk_runtime.subprocess, "run", return_value=mock.Mock(
                        returncode=0, stdout="TK_OK"
                    )) as run:
                self.assertTrue(validate_tk_runtime("test-python.exe"))
                self.assertTrue(validate_tk_runtime("test-python.exe"))
                run.assert_called_once()
        finally:
            cache.clear()

    def test_failed_tk_validation_can_succeed_after_repair(self):
        cache = tk_runtime._TK_VALIDATION_CACHE
        cache.clear()
        try:
            with mock.patch.object(tk_runtime, "setup_tk_environment", return_value={}), \
                    mock.patch.object(tk_runtime.subprocess, "run", side_effect=[
                        mock.Mock(returncode=1, stdout=""),
                        mock.Mock(returncode=0, stdout="TK_OK"),
                    ]) as run:
                self.assertFalse(validate_tk_runtime("repair-python.exe"))
                self.assertTrue(validate_tk_runtime("repair-python.exe"))
                self.assertEqual(run.call_count, 2)
        finally:
            cache.clear()

    def test_find_voice_python_with_desktop_requirement(self):
        py_sense = find_voice_python(engine="sensevoice_offline", require_desktop=True)
        self.assertTrue(py_sense)
        # 验证所选出的解释器具备桌面能力
        self.assertTrue(_probe_desktop_capability(py_sense), f"解释器 {py_sense} 必须支持桌面 GUI")


if __name__ == "__main__":
    unittest.main()
