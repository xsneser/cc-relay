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

    def test_finalize_tail_flush(self):
        self.engine.create_session("sess_004")
        chunk = np.zeros(4000, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_004", chunk)

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="Qwen收尾结果"):
            text = self.engine.finalize_session("sess_004")
            self.assertEqual(text, "Qwen收尾结果")

    def test_qwen_finalize_reuses_current_segment_partial(self):
        ctx = self.engine.create_session("sess_stream")
        ctx.accumulated_pcm.extend(b"\x00" * 32000)
        ctx.total_pcm_len = 32000
        ctx.last_inferred_pcm_len = len(ctx.accumulated_pcm)
        ctx.current_partial = "流式实时输出的文本"

        with mock.patch.object(self.engine, "_transcribe_samples") as mock_transcribe:
            result = self.engine.finalize_session("sess_stream")
            self.assertEqual(result, "流式实时输出的文本")
            mock_transcribe.assert_not_called()

    def test_qwen_tail_audio_decoded_without_loss(self):
        ctx = self.engine.create_session("sess_tail")
        tail_bytes = np.zeros(8000, dtype=np.int16).tobytes()
        self.engine.feed_chunk("sess_tail", tail_bytes)

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="尾部补全文本") as mock_transcribe:
            result = self.engine.finalize_session("sess_tail")
            self.assertEqual(result, "尾部补全文本")
            mock_transcribe.assert_called_once()
            self.assertEqual(len(mock_transcribe.call_args.args[0]), 8000)

    def test_qwen_streaming_no_repetition(self):
        ctx = self.engine.create_session("sess_norep")
        chunk_1s = np.zeros(16000, dtype=np.int16).tobytes()

        # 模拟第 1 秒出字
        with mock.patch.object(self.engine, "_transcribe_samples", return_value="现在右上角"):
            conf, part1 = self.engine.feed_chunk("sess_norep", chunk_1s)
            self.assertEqual(part1, "现在右上角")

        # 模拟第 2 秒出字，确认不会把第 1 秒短语重复拼接成两遍
        with mock.patch.object(self.engine, "_transcribe_samples", return_value="现在右上角这个悬浮窗"):
            conf, part2 = self.engine.feed_chunk("sess_norep", chunk_1s)
            self.assertEqual(part2, "现在右上角这个悬浮窗")
            self.assertNotIn("现在右上角现在右上角", part2)

    def test_qwen_stable_prefix_commits_only_after_vad_and_eight_second_guard(self):
        cfg = VoiceConfig(
            engine="qwen_2pass",
            qwen_trailing_audio_guard_seconds=8.0,
            qwen_stable_hypothesis_count=3,
            qwen_segment_silence_seconds=0.1,
        )
        engine = Qwen2PassEngine(cfg)
        ctx = engine.create_session("sess_stable_prefix")
        frame_index = 0

        def is_speech(_frame):
            nonlocal frame_index
            current = frame_index
            frame_index += 1
            return current < 40 or current >= 45

        engine._vad.is_speech_bytes = is_speech
        observed_lengths = []

        def transcribe(samples, context=""):
            observed_lengths.append(len(samples))
            if ctx.audio_base_offset == 0:
                return "第一句。" if len(samples) <= 14400 else "第一句。后续内容。"
            return "后续内容。更多文字。"

        with mock.patch.object(engine, "_transcribe_samples", side_effect=transcribe):
            engine.feed_chunk("sess_stable_prefix", np.zeros(12800, dtype=np.int16).tobytes())  # 0.8s speech
            engine.feed_chunk("sess_stable_prefix", np.zeros(1600, dtype=np.int16).tobytes())    # 0.1s VAD pause
            self.assertEqual(len(ctx.vad_boundaries), 1)
            boundary_offset = ctx.vad_boundaries[0]["offset"]
            engine.feed_chunk("sess_stable_prefix", np.zeros(127680, dtype=np.int16).tobytes()) # 7.98s after boundary

            self.assertEqual(ctx.confirmed_text, "")
            self.assertEqual(ctx.audio_base_offset, 0)
            engine.feed_chunk("sess_stable_prefix", np.zeros(320, dtype=np.int16).tobytes())    # reaches 8s guard

            self.assertEqual(ctx.confirmed_text, "第一句。")
            self.assertEqual(ctx.audio_base_offset, boundary_offset)
            self.assertEqual(len(ctx.accumulated_pcm), 8 * 16000 * 2)
            self.assertTrue(ctx.current_partial.startswith("后续内容"))

            engine.feed_chunk("sess_stable_prefix", np.zeros(12800, dtype=np.int16).tobytes()) # 0.8s more

        self.assertTrue(observed_lengths)
        self.assertEqual(observed_lengths[-1], int(8.8 * 16000))
        self.assertLess(observed_lengths[-1], ctx.total_pcm_len // 2)

    def test_qwen_continuous_speech_has_no_arbitrary_eight_second_cut(self):
        cfg = VoiceConfig(engine="qwen_2pass", qwen_trailing_audio_guard_seconds=8.0)
        engine = Qwen2PassEngine(cfg)
        ctx = engine.create_session("sess_no_forced_cap")
        engine._vad.is_speech_bytes = mock.Mock(return_value=True)

        with mock.patch.object(engine, "_transcribe_samples", return_value="连续讲话"):
            engine.feed_chunk("sess_no_forced_cap", np.zeros(10 * 16000, dtype=np.int16).tobytes())

        self.assertEqual(ctx.confirmed_text, "")
        self.assertEqual(ctx.audio_base_offset, 0)
        self.assertEqual(len(ctx.accumulated_pcm), 10 * 16000 * 2)

    def test_qwen_failed_pause_decode_keeps_audio_for_final_retry(self):
        cfg = VoiceConfig(engine="qwen_2pass", qwen_segment_silence_seconds=0.1)
        engine = Qwen2PassEngine(cfg)
        ctx = engine.create_session("sess_retry_pause")
        frame_index = 0

        def is_speech(_frame):
            nonlocal frame_index
            current = frame_index
            frame_index += 1
            return current < 40

        engine._vad.is_speech_bytes = is_speech
        with mock.patch.object(
            engine,
            "_transcribe_samples",
            side_effect=["partial draft", "", "recovered final"],
        ) as mock_transcribe:
            engine.feed_chunk("sess_retry_pause", np.zeros(12800, dtype=np.int16).tobytes())
            engine.feed_chunk("sess_retry_pause", np.zeros(1600, dtype=np.int16).tobytes())
            self.assertGreater(len(ctx.accumulated_pcm), 0)
            self.assertEqual(ctx.confirmed_text, "")
            result = engine.finalize_session("sess_retry_pause")

        self.assertEqual(result, "recovered final")
        self.assertEqual(mock_transcribe.call_count, 3)

    def test_qwen_finalize_does_not_repeat_already_decoded_segment(self):
        ctx = self.engine.create_session("sess_no_final_repeat")
        segment = np.zeros(12800, dtype=np.int16).tobytes()  # 0.8 seconds

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="partial文本") as mock_transcribe:
            self.engine.feed_chunk("sess_no_final_repeat", segment)
            self.assertEqual(ctx.last_inferred_pcm_len, len(ctx.accumulated_pcm))
            result = self.engine.finalize_session("sess_no_final_repeat")

        self.assertEqual(result, "partial文本")
        mock_transcribe.assert_called_once()

    def test_qwen_silence_tail_fast_path_avoids_redundant_inference(self):
        """当语音在按键期间已完成识别，且松键尾部仅为静音时，必须命中 Fast-path 免重算。"""
        ctx = self.engine.create_session("sess_fast_path")
        frame_index = 0

        # 前 40 帧 (0.8s) 为有声，后 25 帧 (0.5s) 为静音
        def is_speech(_frame):
            nonlocal frame_index
            current = frame_index
            frame_index += 1
            return current < 40

        self.engine._vad.is_speech_bytes = is_speech
        speech_bytes = np.zeros(12800, dtype=np.int16).tobytes()  # 0.8s
        silence_bytes = np.zeros(8000, dtype=np.int16).tobytes()  # 0.5s

        with mock.patch.object(self.engine, "_transcribe_samples", return_value="完整说话文本") as mock_transcribe:
            # 1. 说话期间触发流式推理
            self.engine.feed_chunk("sess_fast_path", speech_bytes)
            self.assertEqual(mock_transcribe.call_count, 1)
            self.assertEqual(ctx.current_partial, "完整说话文本")
            self.assertGreater(ctx.last_inferred_pcm_len, 0)

            # 2. 松键阶段排空静音帧
            self.engine.feed_chunk("sess_fast_path", silence_bytes)
            self.assertGreater(len(ctx.accumulated_pcm), ctx.last_inferred_pcm_len)

            # 3. 终态结算：断言直接复用 partial，不发起二次 GPU 运算
            result = self.engine.finalize_session("sess_fast_path")
            self.assertEqual(result, "完整说话文本")
            self.assertEqual(mock_transcribe.call_count, 1)

    def test_qwen_speech_tail_triggers_inference_to_avoid_loss(self):
        """若松键前夕仍有新的有效说话语音，必须触发增量终态推理，确保不漏字。"""
        ctx = self.engine.create_session("sess_voiced_tail")
        frame_index = 0

        # 所有帧皆为人声
        self.engine._vad.is_speech_bytes = mock.Mock(return_value=True)
        part1_bytes = np.zeros(12800, dtype=np.int16).tobytes()  # 0.8s
        tail_bytes = np.zeros(6400, dtype=np.int16).tobytes()   # 0.4s

        with mock.patch.object(
            self.engine,
            "_transcribe_samples",
            side_effect=["第一部分", "第一部分加尾音"],
        ) as mock_transcribe:
            self.engine.feed_chunk("sess_voiced_tail", part1_bytes)
            self.assertEqual(mock_transcribe.call_count, 1)

            # 尾部继续有有效语音
            self.engine.feed_chunk("sess_voiced_tail", tail_bytes)

            # 终态结算：必须执行第 2 次解码以捕获尾音
            result = self.engine.finalize_session("sess_voiced_tail")
            self.assertEqual(result, "第一部分加尾音")
            self.assertEqual(mock_transcribe.call_count, 2)

    def test_qwen_drain_mode_suppresses_speculative_decode(self):
        """进入 Drain 阶段后，feed_chunk 仅摄入音频，不应触发投机性中间推断。"""
        ctx = self.engine.create_session("sess_drain_suppress")
        self.engine.begin_drain("sess_drain_suppress")
        self.assertTrue(ctx.is_draining)

        long_chunk = np.zeros(25600, dtype=np.int16).tobytes()  # 1.6s (> stride)
        with mock.patch.object(self.engine, "_transcribe_samples") as mock_transcribe:
            self.engine.feed_chunk("sess_drain_suppress", long_chunk)
            mock_transcribe.assert_not_called()
            self.assertEqual(len(ctx.accumulated_pcm), len(long_chunk))

    def test_qwen_inference_lock_reentrancy_no_deadlock(self):
        import threading
        self.assertIsInstance(self.engine._inference_lock, type(threading.RLock()))
        ctx = self.engine.create_session("sess_deadlock")
        # 验证 RLock 可安全重入，杜绝 feed_chunk 与内部 _transcribe_samples 嵌套时自死锁
        self.engine._inference_lock.acquire()
        try:
            with self.engine._inference_lock:
                pass
        finally:
            self.engine._inference_lock.release()

    def test_on_demand_mode_moves_weights_for_session_then_offloads(self):
        import threading
        import time
        cfg = VoiceConfig(engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload")
        engine = Qwen2PassEngine(cfg)
        engine._is_loaded = True
        engine._resolved_device = "cuda:0"
        engine._active_device = "cpu"
        engine._resource_state = "cpu_ready"
        engine._gpu_ready.clear()
        moved = []
        engine._move_backends = lambda device: moved.append(device)

        with mock.patch("tools.voice_input.asr_engine.torch", None):
            engine._resource_thread = threading.Thread(target=engine._resource_worker, daemon=True)
            engine._resource_thread.start()
            engine.create_session("on_demand")
            self.assertTrue(engine._gpu_ready.wait(1.0))
            self.assertEqual(engine.get_capabilities()["model_device"], "cuda:0")
            self.assertTrue(engine.get_capabilities()["gpu_resident"])

            engine.finalize_session("on_demand")
            deadline = time.monotonic() + 1.0
            while engine._active_device != "cpu" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(engine._active_device, "cpu")
        self.assertEqual(moved, ["cuda:0", "cpu"])
        engine.shutdown()

    def test_on_demand_pcm_replay_matches_resident_frame_path(self):
        import threading
        import time

        cfg_resident = VoiceConfig(
            engine="qwen_2pass", device="cuda:0", vram_mode="resident",
            qwen_segment_silence_seconds=0.06,
        )
        cfg_demand = VoiceConfig(
            engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload",
            qwen_segment_silence_seconds=0.06,
        )
        resident = Qwen2PassEngine(cfg_resident)
        demand = Qwen2PassEngine(cfg_demand)
        for engine in (resident, demand):
            engine._is_loaded = True
            engine._resolved_device = "cuda:0"
            engine._segment_silence_bytes = 3 * engine._frame_bytes
            engine._vad.is_speech_bytes = lambda frame: bool(np.frombuffer(frame, dtype=np.int16)[0])

        resident._active_device = "cuda:0"
        resident._resource_state = "gpu_ready"
        resident._gpu_ready.set()

        move_started = threading.Event()
        allow_move = threading.Event()
        moved = []

        def move_backend(device):
            moved.append(device)
            if device.startswith("cuda"):
                move_started.set()
                if not allow_move.wait(2.0):
                    raise TimeoutError("test GPU activation gate timed out")

        demand._active_device = "cpu"
        demand._resource_state = "cpu_ready"
        demand._gpu_ready.clear()
        demand._move_backends = move_backend

        resident_calls = []
        demand_calls = []

        def install_decoder(engine, calls):
            def decode(samples):
                calls.append(np.array(samples, copy=True))
                return f"segment-{len(calls)}"
            engine._transcribe_samples = decode

        install_decoder(resident, resident_calls)
        install_decoder(demand, demand_calls)

        frames = [
            np.full(320, i + 1 if i < 6 else 0, dtype=np.int16).tobytes()
            for i in range(20)
        ]
        first_chunk = b"".join(frames[:12])
        second_chunk = b"".join(frames[12:])
        resident_ctx = resident.create_session("resident")
        demand_ctx = demand.create_session("demand")
        resident.feed_chunk("resident", first_chunk)
        resident.feed_chunk("resident", second_chunk)

        with mock.patch("tools.voice_input.asr_engine.torch", None):
            demand._resource_thread = threading.Thread(target=demand._resource_worker, daemon=True)
            demand._resource_thread.start()
            self.assertTrue(move_started.wait(1.0))
            demand.feed_chunk("demand", first_chunk)
            self.assertEqual(demand_calls, [])
            self.assertEqual(demand._sessions["demand"].total_pcm_len, len(first_chunk))

            allow_move.set()
            self.assertTrue(demand._gpu_ready.wait(1.0))
            # An empty feed drains pending chunks before any newer live PCM.
            demand.feed_chunk("demand", b"")
            demand.feed_chunk("demand", second_chunk)
            resident_text = resident.finalize_session("resident")
            demand_text = demand.finalize_session("demand")

            deadline = time.monotonic() + 1.0
            while demand._active_device != "cpu" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(demand_text, resident_text)
        self.assertEqual(demand._sessions, {})
        self.assertEqual(demand._active_device, "cpu")
        self.assertEqual(moved, ["cuda:0", "cpu"])
        self.assertEqual(demand_ctx.total_pcm_len, len(first_chunk) + len(second_chunk))
        self.assertEqual(resident_ctx.total_pcm_len, len(first_chunk) + len(second_chunk))
        self.assertEqual(len(demand_calls), len(resident_calls))
        for expected, actual in zip(resident_calls, demand_calls):
            np.testing.assert_array_equal(actual, expected)
        self.assertEqual(
            [s["text"] for s in demand_ctx.completed_segments],
            [s["text"] for s in resident_ctx.completed_segments],
        )
        self.assertEqual(demand_ctx.accumulated_pcm, resident_ctx.accumulated_pcm)
        self.assertEqual(resident._sessions, {})
        demand.shutdown()

    def test_cancel_discards_pending_pcm_during_gpu_activation(self):
        import threading
        import time

        cfg = VoiceConfig(engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload")
        engine = Qwen2PassEngine(cfg)
        engine._is_loaded = True
        engine._resolved_device = "cuda:0"
        engine._active_device = "cpu"
        engine._resource_state = "cpu_ready"
        engine._gpu_ready.clear()
        move_started = threading.Event()
        allow_move = threading.Event()
        calls = []

        def move_backend(device):
            if device.startswith("cuda"):
                move_started.set()
                if not allow_move.wait(2.0):
                    raise TimeoutError("test GPU activation gate timed out")

        engine._move_backends = move_backend
        engine._transcribe_samples = lambda samples: calls.append(samples) or "unexpected"
        ctx = engine.create_session("cancel-during-load")

        with mock.patch("tools.voice_input.asr_engine.torch", None):
            engine._resource_thread = threading.Thread(target=engine._resource_worker, daemon=True)
            engine._resource_thread.start()
            self.assertTrue(move_started.wait(1.0))
            engine.feed_chunk("cancel-during-load", np.full(5000, 3, dtype=np.int16).tobytes())
            self.assertTrue(ctx.pending_pcm_chunks)
            engine.cancel_session("cancel-during-load")
            self.assertFalse(ctx.pending_pcm_chunks)
            self.assertNotIn("cancel-during-load", engine._active_sessions)
            allow_move.set()

            deadline = time.monotonic() + 1.0
            while engine._active_device != "cpu" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(calls, [])
        self.assertNotIn("cancel-during-load", engine._sessions)
        self.assertEqual(engine._active_device, "cpu")
        engine.shutdown()

    def test_finalize_replays_pending_pcm_when_no_more_chunks_arrive(self):
        import threading
        import time

        cfg = VoiceConfig(engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload")
        engine = Qwen2PassEngine(cfg)
        engine._is_loaded = True
        engine._resolved_device = "cuda:0"
        engine._active_device = "cpu"
        engine._resource_state = "cpu_ready"
        engine._gpu_ready.clear()
        move_started = threading.Event()
        allow_move = threading.Event()
        finalized = threading.Event()
        gpu_waiting = threading.Event()
        calls = []
        engine._move_backends = lambda device: (
            move_started.set() or allow_move.wait(2.0)
            if device.startswith("cuda") else None
        )
        original_ensure_gpu_ready = engine._ensure_gpu_ready
        def ensure_gpu_ready():
            gpu_waiting.set()
            original_ensure_gpu_ready()
        engine._ensure_gpu_ready = ensure_gpu_ready
        engine._transcribe_samples = lambda samples: calls.append(np.array(samples, copy=True)) or "final text"
        ctx = engine.create_session("stop-before-ready")

        with mock.patch("tools.voice_input.asr_engine.torch", None):
            engine._resource_thread = threading.Thread(target=engine._resource_worker, daemon=True)
            engine._resource_thread.start()
            self.assertTrue(move_started.wait(1.0))
            pcm = np.full(5000, 7, dtype=np.int16).tobytes()
            engine.feed_chunk("stop-before-ready", pcm)
            self.assertEqual(calls, [])

            result = []
            def finalize():
                result.append(engine.finalize_session("stop-before-ready"))
                finalized.set()

            finalizer = threading.Thread(target=finalize)
            finalizer.start()
            self.assertTrue(gpu_waiting.wait(1.0))
            self.assertFalse(finalized.is_set())
            allow_move.set()
            finalizer.join(2.0)
            self.assertFalse(finalizer.is_alive())

            deadline = time.monotonic() + 1.0
            while engine._active_device != "cpu" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(result, ["final text"])
        self.assertEqual(len(calls), 1)
        np.testing.assert_array_equal(calls[0], np.full(5000, 7, dtype=np.float32) / 32768.0)
        self.assertEqual(ctx.total_pcm_len, len(pcm))
        self.assertEqual(list(ctx.pending_pcm_chunks), [])
        self.assertNotIn("stop-before-ready", engine._sessions)
        engine.shutdown()

    def test_short_vad_segment_replays_before_minimum_duration_return(self):
        import threading
        import time

        cfg_resident = VoiceConfig(
            engine="qwen_2pass", device="cuda:0", vram_mode="resident",
            min_recording_seconds=0.25, qwen_segment_silence_seconds=0.04,
        )
        cfg_demand = VoiceConfig(
            engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload",
            min_recording_seconds=0.25, qwen_segment_silence_seconds=0.04,
        )
        resident = Qwen2PassEngine(cfg_resident)
        demand = Qwen2PassEngine(cfg_demand)
        for engine in (resident, demand):
            engine._is_loaded = True
            engine._resolved_device = "cuda:0"
            engine._segment_silence_bytes = 2 * engine._frame_bytes
            engine._vad.is_speech_bytes = lambda frame: bool(np.frombuffer(frame, dtype=np.int16)[0])

        resident._active_device = "cuda:0"
        resident._resource_state = "gpu_ready"
        resident._gpu_ready.set()
        resident_calls = []
        demand_calls = []
        resident._transcribe_samples = lambda samples: resident_calls.append(np.array(samples, copy=True)) or "short"
        demand._transcribe_samples = lambda samples: demand_calls.append(np.array(samples, copy=True)) or "short"

        move_started = threading.Event()
        allow_move = threading.Event()
        gpu_waiting = threading.Event()
        demand._active_device = "cpu"
        demand._resource_state = "cpu_ready"
        demand._gpu_ready.clear()

        def move_backend(device):
            if device.startswith("cuda"):
                move_started.set()
                if not allow_move.wait(2.0):
                    raise TimeoutError("test GPU activation gate timed out")
        demand._move_backends = move_backend
        original_ensure_gpu_ready = demand._ensure_gpu_ready
        def ensure_gpu_ready():
            gpu_waiting.set()
            original_ensure_gpu_ready()
        demand._ensure_gpu_ready = ensure_gpu_ready

        # Two speech frames followed by silence trigger a segment shorter than the
        # minimum full-utterance duration, which resident mode still partially decodes.
        frames = [
            np.full(320, 1 if i < 2 else 0, dtype=np.int16).tobytes()
            for i in range(5)
        ]
        pcm = b"".join(frames)
        resident.create_session("resident-short")
        demand_ctx = demand.create_session("demand-short")
        resident.feed_chunk("resident-short", pcm)

        with mock.patch("tools.voice_input.asr_engine.torch", None):
            demand._resource_thread = threading.Thread(target=demand._resource_worker, daemon=True)
            demand._resource_thread.start()
            self.assertTrue(move_started.wait(1.0))
            demand.feed_chunk("demand-short", pcm)

            result = []
            finalizer = threading.Thread(
                target=lambda: result.append(demand.finalize_session("demand-short"))
            )
            finalizer.start()
            self.assertTrue(gpu_waiting.wait(1.0))
            self.assertEqual(result, [])
            allow_move.set()
            finalizer.join(2.0)
            self.assertFalse(finalizer.is_alive())

            deadline = time.monotonic() + 1.0
            while demand._active_device != "cpu" and time.monotonic() < deadline:
                time.sleep(0.01)

        resident_text = resident.finalize_session("resident-short")
        self.assertEqual(result, [resident_text])
        self.assertEqual(result, ["short"])
        self.assertEqual(demand_ctx.total_pcm_len, len(pcm))
        self.assertEqual(len(demand_calls), len(resident_calls))
        for expected, actual in zip(resident_calls, demand_calls):
            np.testing.assert_array_equal(actual, expected)
        demand.shutdown()


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
