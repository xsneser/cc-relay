"""基于 Qwen ASR 1.7B 与 sherpa-onnx 的 2-Pass 流式引擎与音频增强单元测试"""

import unittest
from pathlib import Path
from unittest import mock
import numpy as np

from tools.voice_input.config import VoiceConfig
from tools.voice_input.asr_engine import (
    BaseStreamingASR,
    QwenOfflineEngine,
    Qwen2PassEngine,
    Sherpa2PassEngine,
    create_engine,
)
from tools.voice_input.audio import AudioRecorder


class TestQwenOfflineEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = VoiceConfig(engine="qwen_offline")
        self.engine = QwenOfflineEngine(self.cfg)

    def test_capabilities_structure(self):
        caps = self.engine.get_capabilities()
        self.assertEqual(caps["engine"], "qwen_offline")
        self.assertTrue(caps["streaming_available"])
        self.assertTrue(caps["offline_available"])
        self.assertTrue(caps["punc_available"])

    def test_session_lifecycle_and_isolation(self):
        s1 = self.engine.create_session("sess_001")
        s2 = self.engine.create_session("sess_002")

        self.assertIsNot(s1, s2)
        self.assertIsNot(s1.accumulated_pcm, s2.accumulated_pcm)

        # 注入空数据
        conf, part = self.engine.feed_chunk("sess_001", b"")
        self.assertEqual((conf, part), ("", "正在说话..."))

        # 注入有效 PCM
        chunk = np.zeros(320, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_001", chunk)
        self.assertEqual(len(s1.accumulated_pcm), 640)
        self.assertEqual(len(s2.accumulated_pcm), 0)

        # 取消与销毁
        self.engine.cancel_session("sess_001")
        self.assertNotIn("sess_001", self.engine._sessions)
        self.assertIn("sess_002", self.engine._sessions)

    def test_finalize_transcription(self):
        s1 = self.engine.create_session("sess_003")
        # 写入至少 0.25 秒的音频 (4000 samples = 8000 bytes)
        chunk = np.zeros(4000, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_003", chunk)

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="测试识别文本"):
            text = self.engine.finalize_session("sess_003")
            self.assertEqual(text, "测试识别文本")

    def test_load_raises_when_model_missing(self):
        with mock.patch("tools.voice_input.asr_engine._find_local_qwen_dir", return_value=None):
            with self.assertRaises(FileNotFoundError) as ctx:
                self.engine.load()
            self.assertIn("未在本地检测到", str(ctx.exception))


class TestQwen2PassEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = VoiceConfig(engine="qwen_2pass")
        self.engine = Qwen2PassEngine(self.cfg)

    def test_capabilities_structure(self):
        caps = self.engine.get_capabilities()
        self.assertEqual(caps["engine"], "qwen_2pass")
        self.assertIn("streaming_available", caps)
        self.assertTrue(caps["offline_available"])
        self.assertTrue(caps["punc_available"])

    def test_session_lifecycle_and_isolation(self):
        s1 = self.engine.create_session("sess_001")
        s2 = self.engine.create_session("sess_002")

        self.assertIsNot(s1, s2)
        self.assertIsNot(s1.accumulated_pcm, s2.accumulated_pcm)

        chunk = np.zeros(320, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_001", chunk)
        self.assertEqual(len(s1.accumulated_pcm), 640)
        self.assertEqual(len(s2.accumulated_pcm), 0)

        self.engine.cancel_session("sess_001")
        self.assertNotIn("sess_001", self.engine._sessions)

    def test_finalize_uses_qwen_pass2(self):
        self.engine.create_session("sess_004")
        chunk = np.zeros(4000, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_004", chunk)

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="Qwen终审结果"):
            text = self.engine.finalize_session("sess_004")
            self.assertEqual(text, "Qwen终审结果")

    def test_qwen_fast_path_finalize_skips_duplicate_inference(self):
        ctx = self.engine.create_session("sess_fast")
        # 模拟 10 秒音频 (160,000 samples = 320,000 bytes)
        total_bytes = 320000
        chunk = np.zeros(total_bytes // 2, dtype=np.int16).tobytes()
        ctx.accumulated_pcm.extend(chunk)

        # 模拟流式阶段已推演至 9.5 秒处 (相差 0.5 秒，即 16000 字节，在 1.0 秒容差内)
        ctx.last_inferred_pcm_len = total_bytes - 16000
        ctx.current_partial = "这是流式实时出字的文本"

        # 校验 fast-path 直接返回 partial，跳过耗时的二次重复全音频推演
        with mock.patch.object(self.engine, "_transcribe_samples") as mock_transcribe:
            result = self.engine.finalize_session("sess_fast")
            self.assertEqual(result, "这是流式实时出字的文本")
            mock_transcribe.assert_not_called()


class TestSherpa2PassEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = VoiceConfig(engine="sherpa_2pass")
        self.engine = Sherpa2PassEngine(self.cfg)

    def test_capabilities_structure(self):
        caps = self.engine.get_capabilities()
        self.assertEqual(caps["engine"], "sherpa_2pass")
        self.assertIn("streaming_available", caps)
        self.assertIn("offline_available", caps)
        self.assertTrue(caps["punc_available"])

    def test_session_lifecycle_and_isolation(self):
        s1 = self.engine.create_session("sess_001")
        s2 = self.engine.create_session("sess_002")

        self.assertIsNot(s1, s2)
        self.assertIsNot(s1.accumulated_pcm, s2.accumulated_pcm)

        conf, part = self.engine.feed_chunk("sess_001", b"")
        self.assertEqual((conf, part), ("", ""))

        chunk = np.zeros(320, dtype=np.int16).tobytes()
        conf, part = self.engine.feed_chunk("sess_001", chunk)
        self.assertEqual(len(s1.accumulated_pcm), 640)
        self.assertEqual(len(s2.accumulated_pcm), 0)

        self.engine.cancel_session("sess_001")
        self.assertNotIn("sess_001", self.engine._sessions)
        self.assertIn("sess_002", self.engine._sessions)

    def test_factory_creation(self):
        cfg_qwen_2pass = VoiceConfig(engine="qwen_2pass")
        eng_qwen = create_engine(cfg_qwen_2pass)
        self.assertIsInstance(eng_qwen, Qwen2PassEngine)

        cfg_qwen_offline = VoiceConfig(engine="qwen_offline")
        eng_qwen_off = create_engine(cfg_qwen_offline)
        self.assertIsInstance(eng_qwen_off, QwenOfflineEngine)

        cfg_2pass = VoiceConfig(engine="sherpa_2pass")
        eng = create_engine(cfg_2pass)
        self.assertIsInstance(eng, Sherpa2PassEngine)

        # 验证兼容向后配置
        cfg_compat = VoiceConfig(engine="paraformer_streaming_2pass")
        eng_compat = create_engine(cfg_compat)
        self.assertIsInstance(eng_compat, Qwen2PassEngine)


class TestAudioRecorderEnhancements(unittest.TestCase):
    def test_pre_roll_feed_injection(self):
        recorder = AudioRecorder(sample_rate=16000, frame_duration_ms=20, pre_roll_ms=60)
        self.assertEqual(recorder.pre_roll_frames_count, 3)

        frame1 = b"A" * 640
        frame2 = b"B" * 640
        frame3 = b"C" * 640
        frame4 = b"D" * 640

        recorder.feed_pre_roll(frame1 + frame2 + frame3 + frame4)
        self.assertEqual(list(recorder._pre_roll_buffer), [frame2, frame3, frame4])

    def test_timeout_flags_reset_on_start(self):
        recorder = AudioRecorder(sample_rate=16000, max_duration=10.0, silence_timeout_seconds=1.2)
        recorder._silence_timeout_triggered = True
        recorder._max_duration_triggered = True

        with mock.patch("tools.voice_input.audio.sd.InputStream"):
            recorder.start()
            self.assertFalse(recorder._silence_timeout_triggered)
            self.assertFalse(recorder._max_duration_triggered)
            recorder.stop()


class TestQwenModelDeletion(unittest.TestCase):
    def test_delete_qwen_model_removes_directories(self):
        import tempfile
        from tools.voice_input.model_download import delete_qwen_model
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg = VoiceConfig(models_dir=tmp_path / "models")
            qwen_dir = cfg.qwen_model_dir
            qwen_dir.mkdir(parents=True, exist_ok=True)
            (qwen_dir / "config.json").write_text("{}", encoding="utf-8")
            (qwen_dir / "model.safetensors").write_bytes(b"dummy")

            self.assertTrue(cfg.is_qwen_installed())
            ok, msg = delete_qwen_model(cfg)
            self.assertTrue(ok)
            self.assertFalse(qwen_dir.exists())
            self.assertFalse(cfg.is_qwen_installed())


class TestVoiceDeviceFormatting(unittest.TestCase):
    def test_cpu_display_name_returns_string(self):
        from cc_relay import _voice_cpu_display_name, _format_vram_gb
        cpu_name = _voice_cpu_display_name()
        self.assertIsInstance(cpu_name, str)
        self.assertTrue(len(cpu_name) > 0)
        self.assertIn("线程", cpu_name)

    def test_format_vram_gb(self):
        from cc_relay import _format_vram_gb
        self.assertEqual(_format_vram_gb(6144), "6GB")
        self.assertEqual(_format_vram_gb(8192), "8GB")
        self.assertEqual(_format_vram_gb(2560), "2.5GB")
        self.assertEqual(_format_vram_gb(512), "512MB")

    def test_available_devices_includes_cpu_and_auto(self):
        from cc_relay import get_voice_available_devices
        devs = get_voice_available_devices()
        dev_ids = [d["id"] for d in devs]
        self.assertIn("auto", dev_ids)
        self.assertIn("cpu", dev_ids)
        cpu_opt = next(d for d in devs if d["id"] == "cpu")
        self.assertIn("CPU:", cpu_opt["name"])
        self.assertIn("线程", cpu_opt["name"])


if __name__ == "__main__":
    unittest.main()
