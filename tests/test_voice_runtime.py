"""运行环境探测器与候选解释器自愈测试"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.voice_input.runtime import (
    _probe_interpreter,
    find_voice_python,
    diagnose_python_environment,
    check_deps_ready,
    ensure_voice_dependencies,
)


class TestVoiceRuntime(unittest.TestCase):
    def test_probe_interpreter_invalid_path(self):
        self.assertFalse(_probe_interpreter("C:\\non_existent_python_12345.exe"))
        self.assertFalse(_probe_interpreter(""))

    def test_probe_interpreter_system_python(self):
        # 系统 python 具备 numpy，应返回 True
        self.assertTrue(_probe_interpreter(sys.executable))

    @mock.patch("tools.voice_input.runtime._probe_interpreter")
    def test_find_voice_python_bypasses_broken_venv(self, mock_probe):
        # 模拟 .venv python 文件存在但 probe 失败 (如缺少 numpy)
        def probe_side_effect(path):
            if ".venv" in path:
                return False
            return True

        mock_probe.side_effect = probe_side_effect

        resolved = find_voice_python()
        # 应自动回退至宿主系统 python
        self.assertNotIn(".venv", resolved)
        self.assertEqual(resolved, sys.executable)

    @mock.patch("tools.voice_input.runtime._probe_interpreter")
    def test_find_voice_python_prefers_healthy_venv(self, mock_probe):
        # 模拟 .venv 完备
        mock_probe.return_value = True

        with mock.patch("pathlib.Path.is_file", return_value=True):
            resolved = find_voice_python()
            self.assertIn(".venv", resolved)

    def test_diagnose_python_environment_returns_dict(self):
        diag = diagnose_python_environment(sys.executable)
        self.assertIsInstance(diag, dict)
        self.assertIn("numpy", diag)
        self.assertIn("funasr", diag)
        self.assertTrue(diag["numpy"])

    def test_check_deps_ready_system_python(self):
        # 系统 python 已安装 sounddevice 和 pynput
        ready = check_deps_ready(sys.executable)
        self.assertTrue(ready)

    @mock.patch("tools.voice_input.runtime.check_deps_ready")
    @mock.patch("tools.voice_input.runtime.subprocess.run")
    def test_ensure_voice_dependencies_no_op_when_ready(self, mock_run, mock_ready):
        mock_ready.return_value = True
        self.assertTrue(ensure_voice_dependencies(python_exe=sys.executable))
        mock_run.assert_not_called()

    @mock.patch("tools.voice_input.runtime.check_deps_ready")
    @mock.patch("tools.voice_input.runtime.subprocess.run")
    def test_ensure_voice_dependencies_installs_when_missing(self, mock_run, mock_ready):
        mock_ready.side_effect = [False, True]
        mock_run.return_value = mock.Mock(returncode=0)
        self.assertTrue(ensure_voice_dependencies(python_exe=sys.executable))
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-m", cmd)
        self.assertIn("pip", cmd)

    def test_find_voice_python_routes_by_engine(self):
        # 验证针对 Paraformer 引擎能正确路由到具备 funasr 的系统解释器
        py_para = find_voice_python(engine="paraformer_streaming_2pass")
        self.assertNotIn(".venv", py_para)
        # 验证针对 SenseVoice 引擎能正确路由到具备 sherpa_onnx 的 .venv
        py_sv = find_voice_python(engine="sensevoice_offline")
        self.assertIn(".venv", py_sv)


if __name__ == "__main__":
    unittest.main()
