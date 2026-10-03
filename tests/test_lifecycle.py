import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lifecycle
import cc_relay


class AntigravityAutoStartTests(unittest.TestCase):
    def test_missing_setting_is_disabled(self):
        self.assertFalse(lifecycle.antigravity_auto_start_enabled({}))
        self.assertFalse(cc_relay.antigravity_auto_start_enabled({}))

    def test_only_boolean_true_enables_start(self):
        for value in (False, 0, 1, "true", "yes", None):
            conf = {"tools": {"antigravity": {"auto_start": value}}}
            self.assertFalse(lifecycle.antigravity_auto_start_enabled(conf))
            self.assertFalse(cc_relay.antigravity_auto_start_enabled(conf))

        conf = {"tools": {"antigravity": {"auto_start": True}}}
        self.assertTrue(lifecycle.antigravity_auto_start_enabled(conf))
        self.assertTrue(cc_relay.antigravity_auto_start_enabled(conf))

    def test_cmd_autostart_does_not_start_sidecar_when_disabled(self):
        conf = {
            "tools": {"antigravity": {"auto_start": False}},
            "router": {"route": "antigravity"},
        }
        with mock.patch.object(lifecycle, "_load_conf", return_value=conf), \
                mock.patch.object(lifecycle, "relay_start", return_value=False), \
                mock.patch.object(lifecycle, "antigravity_ensure") as ensure, \
                mock.patch.dict(os.environ, {}, clear=False):
            lifecycle.cmd_autostart()
        ensure.assert_not_called()

    def test_cmd_autostart_keeps_explicitly_enabled_behavior(self):
        conf = {
            "tools": {"antigravity": {"auto_start": True}},
            "router": {"route": "antigravity"},
        }
        with mock.patch.object(lifecycle, "_load_conf", return_value=conf), \
                mock.patch.object(lifecycle, "relay_start", return_value=False), \
                mock.patch.object(lifecycle, "antigravity_ensure", return_value="started") as ensure, \
                mock.patch.object(lifecycle, "antigravity_up", return_value=True):
            lifecycle.cmd_autostart()
        ensure.assert_called_once_with(conf)


class VoiceAutoStartTests(unittest.TestCase):
    def test_missing_setting_is_disabled(self):
        self.assertFalse(lifecycle.voice_auto_start_enabled({}))
        self.assertFalse(cc_relay.voice_auto_start_enabled({}))

    def test_only_boolean_true_enables_start(self):
        for value in (False, 0, 1, "true", "yes", None):
            conf = {"tools": {"voice": {"auto_start": value}}}
            self.assertFalse(lifecycle.voice_auto_start_enabled(conf))
            self.assertFalse(cc_relay.voice_auto_start_enabled(conf))

        conf = {"tools": {"voice": {"auto_start": True}}}
        self.assertTrue(lifecycle.voice_auto_start_enabled(conf))
        self.assertTrue(cc_relay.voice_auto_start_enabled(conf))

    def test_cmd_autostart_does_not_start_voice_when_disabled(self):
        conf = {"tools": {"voice": {"auto_start": False}}}
        with mock.patch.object(lifecycle, "_load_conf", return_value=conf), \
                mock.patch.object(lifecycle, "relay_start", return_value=False), \
                mock.patch.object(lifecycle, "voice_ensure") as ensure, \
                mock.patch.dict(os.environ, {}, clear=False):
            lifecycle.cmd_autostart()
        ensure.assert_not_called()

    def test_cmd_autostart_starts_voice_when_enabled(self):
        conf = {"tools": {"voice": {"auto_start": True}}}
        with mock.patch.object(lifecycle, "_load_conf", return_value=conf), \
                mock.patch.object(lifecycle, "relay_start", return_value=False), \
                mock.patch.object(lifecycle, "voice_ensure", return_value="started") as ensure, \
                mock.patch.object(lifecycle, "voice_up", return_value=True):
            lifecycle.cmd_autostart()
        ensure.assert_called_once_with(conf)

    def test_cmd_stopall_calls_voice_stop(self):
        with mock.patch.object(lifecycle, "relay_stop"), \
                mock.patch.object(lifecycle, "codex_stop"), \
                mock.patch.object(lifecycle, "voice_stop") as v_stop:
            lifecycle.cmd_stopall()
        v_stop.assert_called_once()

    def test_voice_stop_does_not_kill_when_not_running(self):
        with mock.patch.object(cc_relay, "_VOICE_PROCESS", None), \
                mock.patch.object(os.path, "isfile", return_value=False), \
                mock.patch.object(cc_relay, "voice_up", return_value=False), \
                mock.patch.object(cc_relay, "_tcp", return_value=False), \
                mock.patch.object(cc_relay.subprocess, "run") as run:
            self.assertEqual(cc_relay.voice_stop(), "not-running")
        run.assert_not_called()

    def test_voice_stop_returns_not_managed_when_unowned_but_up(self):
        with mock.patch.object(cc_relay, "_VOICE_PROCESS", None), \
                mock.patch.object(os.path, "isfile", return_value=False), \
                mock.patch.object(cc_relay, "_probe_voice_service", return_value=None), \
                mock.patch.object(cc_relay, "_tcp", return_value=True):
            self.assertEqual(cc_relay.voice_stop(), "not-managed")

    def test_voice_stop_adopts_pid_from_probe_when_untracked(self):
        # 模拟 Relay 重启后无 _VOICE_PROCESS 也无 .voice.pid，但探针返回了有效 PID
        with mock.patch.object(cc_relay, "_VOICE_PROCESS", None), \
                mock.patch.object(os.path, "isfile", return_value=False), \
                mock.patch.object(cc_relay, "_probe_voice_service", return_value={"service": "voice", "pid": 7890}), \
                mock.patch.object(cc_relay, "_tcp", return_value=False), \
                mock.patch.object(cc_relay.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cc_relay.voice_stop(), "stopped")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ["/PID", "7890"])

    def test_voice_status_dict_auto_adopts_pid(self):
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value={"service": "voice", "status": "ready", "ready": True, "pid": 4321}), \
                mock.patch.object(os.path, "isfile", return_value=False), \
                mock.patch.object(cc_relay, "_write_voice_pid") as mock_write:
            st = cc_relay.voice_status_dict()
            self.assertTrue(st["owned"])
            self.assertEqual(st["pid"], 4321)
            mock_write.assert_called_once_with(4321)

    def test_starting_status_skips_slow_device_discovery(self):
        with mock.patch.object(cc_relay, "_VOICE_STARTING", True), \
                mock.patch.object(cc_relay, "get_voice_available_devices") as devices:
            status = cc_relay.voice_status_dict({"tools": {"voice": {}}})
        self.assertEqual(status["status"], "starting")
        self.assertIn("startup", status)
        devices.assert_not_called()

    def test_voice_status_exposes_runtime_residency(self):
        probe = {
            "service": "voice", "status": "ready", "ready": True,
            "capabilities": {
                "vram_mode": "on_demand_offload", "model_device": "cpu",
                "gpu_resident": False, "residency_state": "cpu_ready",
            },
        }
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value=probe), \
                mock.patch.object(cc_relay, "get_voice_available_devices", return_value=[]), \
                mock.patch("tools.voice_input.config.VoiceConfig.from_relay_config") as load_cfg:
            cfg = load_cfg.return_value
            cfg.is_qwen_installed.return_value = False
            cfg.is_sensevoice_installed.return_value = False
            st = cc_relay.voice_status_dict({"tools": {"voice": {"vram_mode": "on_demand_offload"}}})
        self.assertEqual(st["vram_mode"], "on_demand_offload")
        self.assertEqual(st["model_device"], "cpu")
        self.assertFalse(st["gpu_resident"])
        self.assertEqual(st["residency_state"], "cpu_ready")

    def test_voice_stop_kills_only_tracked_pid(self):
        proc = mock.Mock(pid=5678)
        proc.poll.return_value = None
        with mock.patch.object(cc_relay, "_VOICE_PROCESS", proc), \
                mock.patch.object(cc_relay, "_tcp", return_value=False), \
                mock.patch.object(cc_relay.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cc_relay.voice_stop(), "stopped")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ["/PID", "5678"])

    def test_voice_start_returns_already_when_up(self):
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value={"service": "voice", "ready": True}):
            self.assertEqual(cc_relay.voice_start(), "already")

    def test_voice_start_acknowledges_without_waiting_for_model(self):
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value=None), \
                mock.patch.object(cc_relay, "_voice_start_sync", return_value="started"), \
                mock.patch.object(cc_relay.threading.Thread, "start", autospec=True):
            self.assertEqual(cc_relay.voice_start(conf={"tools": {"voice": {}}}), "starting")
            self.assertTrue(cc_relay._VOICE_STARTING)
        cc_relay._VOICE_STARTING = False

    def test_failed_dependency_repair_prevents_child_spawn(self):
        conf = {"tools": {"voice": {"engine": "qwen_2pass"}}}
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value=None), \
                mock.patch("tools.voice_input.runtime.find_voice_python", return_value=sys.executable), \
                mock.patch("tools.voice_input.runtime.ensure_voice_dependencies", return_value=False), \
                mock.patch.object(cc_relay.subprocess, "Popen") as popen:
            result = cc_relay._voice_start_sync(conf, attempt_id="dependency-fail")
        self.assertTrue(result.startswith("start-failed"))
        popen.assert_not_called()

    def test_readiness_monitor_waits_for_ready_not_just_health(self):
        proc = mock.Mock(pid=9999)
        proc.poll.return_value = None
        conf = {"tools": {"voice": {}}}
        health_loading = {"service": "voice", "pid": proc.pid, "status": "loading_model", "ready": False}
        health_ready = {"service": "voice", "pid": proc.pid, "status": "ready", "ready": True}
        with cc_relay._VOICE_START_LOCK:
            cc_relay._VOICE_STARTUP = {
                "attempt_id": "monitor-test", "started_at": time.time(),
                "started_monotonic": time.monotonic(), "phase": "waiting_for_health",
                "phase_started_monotonic": time.monotonic(), "phase_durations": {}, "details": {},
            }
        with mock.patch.object(cc_relay, "_probe_voice_service", side_effect=[health_loading, health_ready]), \
                mock.patch.object(cc_relay.time, "sleep"):
            cc_relay._monitor_voice_startup(proc, conf, 0, "monitor-test")
        self.assertEqual(cc_relay._VOICE_STARTUP["phase"], "ready")
        self.assertNotEqual(cc_relay._VOICE_STARTUP["phase"], "loading_model")

    def test_voice_start_reports_child_exit_as_start_failed(self):
        proc = mock.Mock(pid=9999)
        proc.poll.return_value = 1  # 模拟子进程闪退
        conf = {"tools": {"voice": {}}}
        with mock.patch.object(cc_relay, "_probe_voice_service", return_value=None), \
                mock.patch("tools.voice_input.runtime.find_voice_python", return_value=sys.executable), \
                mock.patch("tools.voice_input.runtime.ensure_voice_dependencies", return_value=True), \
                mock.patch("tools.voice_input.tk_runtime.setup_tk_environment", return_value={}), \
                mock.patch.object(cc_relay.subprocess, "Popen", return_value=proc), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(cc_relay, "_get_voice_job", return_value=False), \
                mock.patch.object(cc_relay.threading.Thread, "start", autospec=True), \
                mock.patch.object(cc_relay.os.path, "isfile", return_value=False):
            res = cc_relay._voice_start_sync(conf=conf, attempt_id="test-attempt")
            self.assertEqual(res, "starting")
            cc_relay._monitor_voice_startup(proc, conf, 0, "test-attempt")
            self.assertIn("code 1", cc_relay._VOICE_LAST_ERROR)


class RequestAutoStartGateTests(unittest.TestCase):
    def test_request_gate_does_not_enable_implicit_start(self):
        self.assertFalse(cc_relay.antigravity_auto_start_enabled({
            "router": {"route": "antigravity"},
        }))

    def test_manual_start_endpoint_remains_independent(self):
        # The explicit API continues to call antigravity_start; the request gate
        # only controls the implicit route-time startup path.
        self.assertTrue(hasattr(cc_relay, "antigravity_start"))

    def test_antigravity_stop_does_not_kill_unmanaged_instance(self):
        with mock.patch.object(cc_relay, "_ANTIGRAVITY_PROCESS", None), \
                mock.patch.object(cc_relay.subprocess, "run") as run:
            self.assertEqual(cc_relay.antigravity_stop(), "not-managed")
        run.assert_not_called()

    def test_antigravity_stop_kills_only_tracked_process(self):
        proc = mock.Mock(pid=1234)
        proc.poll.return_value = None
        with mock.patch.object(cc_relay, "_ANTIGRAVITY_PROCESS", proc), \
                mock.patch.object(cc_relay, "_ANTIGRAVITY_EXE", "C:\\\\Antigravity Tools\\\\antigravity-tools.exe"), \
                mock.patch.object(cc_relay.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cc_relay.antigravity_stop(), "stopped")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ["/PID", "1234"])


if __name__ == "__main__":
    unittest.main()
