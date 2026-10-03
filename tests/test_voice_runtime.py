"""运行环境探测器与候选解释器自愈测试"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import tools.voice_input.runtime as voice_runtime
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
        # 模拟 .venv 与 runtime python 均不可用时 (如缺少 numpy)
        def probe_side_effect(path):
            if ".venv" in path or "runtime" in path:
                return False
            return True

        mock_probe.side_effect = probe_side_effect

        resolved = find_voice_python()
        # 应自动回退至宿主系统 python
        self.assertNotIn(".venv", resolved)
        self.assertNotIn("runtime", resolved)
        self.assertEqual(resolved, sys.executable)

    @mock.patch("tools.voice_input.runtime._check_module_in_interpreter", return_value=True)
    @mock.patch("tools.voice_input.runtime._probe_interpreter")
    def test_find_voice_python_prefers_healthy_venv(self, mock_probe, mock_check_mod):
        # 模拟 .venv 完备 (无 bundled runtime 时优先选择 .venv)
        mock_probe.return_value = True

        def is_file_mock(path_obj=None):
            p_str = str(path_obj) if path_obj is not None else ""
            if "runtime" in p_str:
                return False
            if ".venv" in p_str:
                return True
            return False

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=is_file_mock):
            resolved = find_voice_python()
            self.assertIn(".venv", resolved)

    @mock.patch("tools.voice_input.runtime._check_module_in_interpreter", return_value=True)
    @mock.patch("tools.voice_input.runtime._probe_interpreter")
    def test_find_voice_python_prefers_bundled_runtime(self, mock_probe, mock_check_mod):
        # 模拟安装包内置便携 runtime 存在且完备时，优先选择 bundled runtime
        mock_probe.return_value = True

        def is_file_mock(path_obj=None):
            p_str = str(path_obj) if path_obj is not None else ""
            if "runtime" in p_str:
                return True
            return False

        with mock.patch("pathlib.Path.is_file", autospec=True, side_effect=is_file_mock):
            resolved = find_voice_python()
            self.assertIn("runtime", resolved)

    def test_diagnose_python_environment_returns_dict(self):
        diag = diagnose_python_environment(sys.executable)
        self.assertIsInstance(diag, dict)
        self.assertIn("numpy", diag)
        self.assertIn("transformers", diag)
        self.assertTrue(diag["numpy"])

    def test_check_deps_ready_system_python(self):
        # 系统 python 已安装 sounddevice 和 pynput
        ready = check_deps_ready(sys.executable)
        self.assertTrue(ready)

    @mock.patch("tools.voice_input.runtime._probe_dependency_report")
    @mock.patch("tools.voice_input.runtime.subprocess.run")
    def test_ensure_voice_dependencies_no_op_when_ready(self, mock_run, mock_probe):
        ready = {name: True for name in ("numpy", "sounddevice", "pynput", "websockets", "win32gui")}
        mock_probe.return_value = (ready, None)
        self.assertTrue(ensure_voice_dependencies(python_exe=sys.executable))
        mock_run.assert_not_called()

    @mock.patch("tools.voice_input.runtime._probe_dependency_report")
    @mock.patch("tools.voice_input.runtime.subprocess.run")
    def test_ensure_voice_dependencies_installs_when_missing(self, mock_run, mock_probe):
        missing = {name: True for name in ("numpy", "sounddevice", "websockets", "win32gui")}
        missing["pynput"] = False
        ready = {name: True for name in ("numpy", "sounddevice", "pynput", "websockets", "win32gui")}
        mock_probe.side_effect = [(missing, None), (ready, None)]
        mock_run.return_value = mock.Mock(returncode=0)
        self.assertTrue(ensure_voice_dependencies(python_exe=sys.executable))
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-m", cmd)
        self.assertIn("pip", cmd)

    @mock.patch("tools.voice_input.runtime._probe_dependency_report", return_value=(None, "probe timed out"))
    @mock.patch("tools.voice_input.runtime.subprocess.run")
    def test_dependency_probe_failure_does_not_trigger_pip(self, mock_run, _mock_probe):
        progress = []
        self.assertFalse(ensure_voice_dependencies(
            python_exe=sys.executable,
            on_progress=lambda phase, details: progress.append((phase, details)),
        ))
        mock_run.assert_not_called()
        self.assertEqual(progress[-1][0], "dependency_probe_failed")

    @mock.patch("tools.voice_input.runtime._probe_dependency_report")
    @mock.patch("tools.voice_input.runtime.subprocess.run", return_value=mock.Mock(returncode=1, stderr=b"offline"))
    def test_failed_pip_repair_is_reported(self, _mock_run, mock_probe):
        missing = {name: True for name in ("numpy", "sounddevice", "websockets", "win32gui")}
        missing["pynput"] = False
        mock_probe.return_value = (missing, None)
        progress = []
        self.assertFalse(ensure_voice_dependencies(
            python_exe=sys.executable,
            on_progress=lambda phase, details: progress.append((phase, details)),
        ))
        self.assertEqual(progress[-1][0], "dependency_install_failed")
        self.assertIn("offline", progress[-1][1]["error"])

    def test_find_voice_python_routes_by_engine(self):
        # 验证针对 Qwen 引擎能正确路由到具备 transformers 的系统解释器
        py_qwen = find_voice_python(engine="qwen_2pass")
        self.assertNotIn(".venv", py_qwen)
        # 验证针对 SenseVoice 引擎能正确路由到具备 sherpa_onnx 的 .venv 或 bundled runtime
        py_sv = find_voice_python(engine="sensevoice_offline")
        self.assertTrue(".venv" in py_sv or "runtime" in py_sv)

    @mock.patch.dict(os.environ, {"CC_VOICE_PYTHON": "candidate-python"})
    @mock.patch("pathlib.Path.is_file", return_value=False)
    @mock.patch("tools.voice_input.runtime._check_module_in_interpreter", return_value=False)
    @mock.patch("tools.voice_input.runtime._probe_interpreter", return_value=True)
    def test_resolver_probes_each_candidate_only_once(self, mock_probe, mock_module, _mock_is_file):
        result = find_voice_python(repo_root=Path("Z:/fake-repo"), engine="qwen_2pass")
        self.assertEqual(result, "candidate-python")
        # Target-module and fallback passes share the same per-candidate probe cache.
        self.assertEqual(mock_probe.call_count, len({item.args[0] for item in mock_probe.call_args_list}))
        self.assertEqual(mock_module.call_count, len({item.args[0] for item in mock_module.call_args_list}))

    @mock.patch("tools.voice_input.runtime.subprocess.run", return_value=mock.Mock(
        returncode=0, stdout="TK_STATIC_OK"
    ))
    def test_desktop_candidate_probe_does_not_create_tk_window(self, run):
        self.assertTrue(voice_runtime._probe_desktop_capability(sys.executable))
        code = run.call_args.args[0][2]
        self.assertNotIn("tkinter.Tk(", code)
        self.assertIn("find_tcl_tk_dirs", code)

    def test_routine_dependency_report_does_not_create_tk_window(self):
        import json
        payload = {name: True for name in voice_runtime._DIAGNOSTIC_MODULES}
        completed = mock.Mock(returncode=0, stdout="__JSON_START__" + json.dumps(payload))
        with mock.patch.object(voice_runtime.subprocess, "run", return_value=completed) as run:
            report = diagnose_python_environment(sys.executable)
        self.assertTrue(report["tkinter"])
        probe_code = run.call_args.args[0][2]
        self.assertNotIn("validate_tk_runtime", probe_code)
        self.assertNotIn("tkinter.Tk(", probe_code)


if __name__ == "__main__":
    unittest.main()
