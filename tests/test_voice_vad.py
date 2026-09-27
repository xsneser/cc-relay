"""VAD 切片、Ring Buffer Pre-roll 与首音节保护单元测试"""

import unittest
import numpy as np

from tools.voice_input.audio import VADDetector, AudioRecorder
from tools.voice_input.config import VoiceConfig


class TestVoiceVADAndFraming(unittest.TestCase):
    def setUp(self):
        self.vad = VADDetector(mode=2, sample_rate=16000)

    def test_20ms_frame_size(self):
        # 16000Hz * 0.02s = 320 samples, 2 bytes per sample = 640 bytes
        recorder = AudioRecorder(sample_rate=16000, frame_duration_ms=20, pre_roll_ms=240)
        self.assertEqual(recorder.frame_samples, 320)
        self.assertEqual(recorder.frame_bytes, 640)
        # Pre-roll 应当保存 12 帧 (240 / 20 = 12)
        self.assertEqual(recorder.pre_roll_frames_count, 12)

    def test_silence_detection(self):
        # 纯静音音频
        silence_bytes = np.zeros(320, dtype=np.int16).tobytes()
        self.assertFalse(self.vad.is_speech_bytes(silence_bytes))

    def test_speech_energy_detection(self):
        # 模拟高能量正弦波语音信号 (440Hz, 幅度 15000)
        t = np.linspace(0, 0.02, 320, endpoint=False)
        sine = (np.sin(2 * np.pi * 440 * t) * 15000).astype(np.int16)
        sine_bytes = sine.tobytes()
        self.assertTrue(self.vad.is_speech_bytes(sine_bytes))

    def test_pre_roll_ring_buffer(self):
        recorder = AudioRecorder(sample_rate=16000, frame_duration_ms=20, pre_roll_ms=60)
        # 3 帧
        self.assertEqual(recorder.pre_roll_frames_count, 3)
        recorder._pre_roll_buffer.append(b"frame1")
        recorder._pre_roll_buffer.append(b"frame2")
        recorder._pre_roll_buffer.append(b"frame3")
        recorder._pre_roll_buffer.append(b"frame4")
        # 应该只保留最新的 3 帧
        self.assertEqual(list(recorder._pre_roll_buffer), [b"frame2", b"frame3", b"frame4"])


if __name__ == "__main__":
    unittest.main()
