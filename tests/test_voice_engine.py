"""基于 sherpa-onnx 的统一 2-Pass 流式引擎与音频增强单元测试"""

import unittest
from unittest import mock
import numpy as np

from tools.voice_input.config import VoiceConfig
from tools.voice_input.asr_engine import (
    BaseStreamingASR,
    Sherpa2PassEngine,
    create_engine,
)
from tools.voice_input.audio import AudioRecorder


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

        # 验证会话独立
        self.assertIsNot(s1, s2)
        self.assertIsNot(s1.accumulated_pcm, s2.accumulated_pcm)

        # 注入空数据与非活动数据保护
        conf, part = self.engine.feed_chunk("sess_001", b"")
        self.assertEqual((conf, part), ("", ""))

        # 注入 PCM 块
        chunk = np.zeros(320, dtype=np.int16).tobytes()
        conf, part = self.engine.feed_chunk("sess_001", chunk)
        self.assertEqual(len(s1.accumulated_pcm), 640)
        self.assertEqual(len(s2.accumulated_pcm), 0)

        # 取消与销毁
        self.engine.cancel_session("sess_001")
        self.assertNotIn("sess_001", self.engine._sessions)
        self.assertIn("sess_002", self.engine._sessions)

    def test_factory_creation(self):
        cfg_2pass = VoiceConfig(engine="sherpa_2pass")
        eng = create_engine(cfg_2pass)
        self.assertIsInstance(eng, Sherpa2PassEngine)

        cfg_compat = VoiceConfig(engine="paraformer_streaming_2pass")
        eng_compat = create_engine(cfg_compat)
        self.assertIsInstance(eng_compat, BaseStreamingASR)


class TestAudioRecorderEnhancements(unittest.TestCase):
    def test_pre_roll_feed_injection(self):
        recorder = AudioRecorder(sample_rate=16000, frame_duration_ms=20, pre_roll_ms=60)
        # 每帧 640 字节，60ms = 3 帧
        self.assertEqual(recorder.pre_roll_frames_count, 3)

        frame1 = b"A" * 640
        frame2 = b"B" * 640
        frame3 = b"C" * 640
        frame4 = b"D" * 640

        recorder.feed_pre_roll(frame1 + frame2 + frame3 + frame4)
        # 应该只保留最新 3 帧：frame2, frame3, frame4
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


if __name__ == "__main__":
    unittest.main()
