"""WebSocket 协议、会话隔离与健康探针单元测试"""

import asyncio
import json
import unittest

import numpy as np
from unittest import mock

from tools.voice_input.config import VoiceConfig
from tools.voice_input.server import VoiceServer
from tools.voice_input.asr_engine import SessionContext, Qwen2PassEngine


class TestVoiceProtocolAndSessions(unittest.TestCase):
    def setUp(self):
        self.cfg = VoiceConfig(port=8499)
        self.server = VoiceServer(self.cfg)

    def test_origin_security_whitelist(self):
        # 允许 localhost 与 127.0.0.1 及非浏览器客户端 (None)
        self.assertTrue(self.server.is_allowed_origin("http://127.0.0.1:8610"))
        self.assertTrue(self.server.is_allowed_origin("http://localhost:8610"))
        self.assertTrue(self.server.is_allowed_origin("http://127.0.0.1:8400"))
        self.assertTrue(self.server.is_allowed_origin(None))

        # 拒绝外部恶意域名与非本地 IP
        self.assertFalse(self.server.is_allowed_origin("http://malicious-site.com"))
        self.assertFalse(self.server.is_allowed_origin("http://192.168.1.100:8610"))

    def test_session_context_isolation(self):
        engine = Qwen2PassEngine(self.cfg)
        s1 = engine.create_session("sess_001")
        s2 = engine.create_session("sess_002")

        # 验证 cache 对象完全独立，禁止跨 session 共享
        self.assertIsNot(s1.cache, s2.cache)
        s1.cache["token_history"] = ["hello"]
        self.assertNotIn("token_history", s2.cache)

        # 验证 feed_chunk 不跨 session 污染
        s1.confirmed_text = "第1句"
        s2.confirmed_text = "第2句"
        self.assertEqual(s1.confirmed_text, "第1句")
        self.assertEqual(s2.confirmed_text, "第2句")

        # 销毁 s1
        engine.finalize_session("sess_001")
        self.assertNotIn("sess_001", engine._sessions)
        self.assertIn("sess_002", engine._sessions)

    def test_engine_flag_before_warmup_does_not_advertise_ready_or_accept_session(self):
        class FakeEngine:
            is_loaded = True  # backend sets this before server-side warm-up completes

            def get_capabilities(self):
                return {"engine": "qwen_2pass", "is_loaded": True}

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.messages = [json.dumps({"action": "start", "session_id": "warmup-test"})]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, message):
                self.sent.append(json.loads(message))

        self.server.engine = FakeEngine()
        self.server.status = "loading_model"
        self.server._ready = False
        ws = FakeWebSocket()
        asyncio.run(self.server.handle_connection(ws))

        hello = next(message for message in ws.sent if message.get("type") == "hello")
        self.assertFalse(hello["ready"])
        self.assertFalse(hello["capabilities"]["is_loaded"])
        error = next(message for message in ws.sent if message.get("type") == "error")
        self.assertEqual(error["error"], "not_ready")

    def test_config_from_relay(self):
        conf_dict = {
            "tools": {
                "voice": {
                    "auto_start": True,
                    "hotkey": "f8",
                    "engine": "sensevoice_offline",
                    "vram_mode": "on_demand_offload",
                    "port": 8405,
                }
            }
        }
        with mock.patch("tools.voice_input.config.open", mock.mock_open(read_data=json.dumps(conf_dict))), \
             mock.patch("pathlib.Path.is_file", return_value=True):
            cfg = VoiceConfig.from_relay_config()
            self.assertTrue(cfg.auto_start)
            self.assertEqual(cfg.hotkey, "f8")
            self.assertEqual(cfg.engine, "sensevoice_offline")
            self.assertEqual(cfg.vram_mode, "on_demand_offload")
            self.assertEqual(cfg.port, 8405)

    def test_websocket_stop_waits_for_pending_pcm_replay(self):
        import threading
        import time

        class FakeWebSocket:
            def __init__(self, messages):
                self.messages = iter(messages)
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.messages)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, message):
                self.sent.append(json.loads(message))

        async def run_protocol():
            cfg = VoiceConfig(
                engine="qwen_2pass", device="cuda:0", vram_mode="on_demand_offload", port=8499
            )
            server = VoiceServer(cfg)
            engine = Qwen2PassEngine(cfg)
            engine._is_loaded = True
            engine._resolved_device = "cuda:0"
            engine._active_device = "cpu"
            engine._resource_state = "cpu_ready"
            engine._gpu_ready.clear()
            server.engine = engine
            server.status = "ready"
            server._ready = True
            server._build_capabilities = lambda: {}
            server._startup_snapshot = lambda: {}

            move_started = threading.Event()
            allow_move = threading.Event()
            finalize_waiting = threading.Event()
            calls = []

            def move_backend(device):
                if device.startswith("cuda"):
                    move_started.set()
                    if not allow_move.wait(2.0):
                        raise TimeoutError("test GPU activation gate timed out")
            engine._move_backends = move_backend
            original_ensure = engine._ensure_gpu_ready

            def ensure_gpu_ready():
                finalize_waiting.set()
                original_ensure()
            engine._ensure_gpu_ready = ensure_gpu_ready
            engine._transcribe_samples = lambda samples: calls.append(np.array(samples, copy=True)) or "ws text"

            session_id = "ws-pending"
            pcm = np.full(5000, 5, dtype=np.int16).tobytes()
            websocket = FakeWebSocket([
                json.dumps({"action": "start", "session_id": session_id}),
                pcm,
                json.dumps({"action": "stop", "session_id": session_id}),
            ])

            with mock.patch("tools.voice_input.asr_engine.torch", None):
                engine._resource_thread = threading.Thread(target=engine._resource_worker, daemon=True)
                engine._resource_thread.start()
                handler = asyncio.create_task(server.handle_connection(websocket))
                self.assertTrue(await asyncio.to_thread(move_started.wait, 1.0))
                self.assertTrue(await asyncio.to_thread(finalize_waiting.wait, 1.0))
                ctx = engine._sessions[session_id]
                self.assertEqual(ctx.total_pcm_len, len(pcm))
                self.assertEqual(list(ctx.pending_pcm_chunks), [pcm])
                self.assertFalse(handler.done())
                allow_move.set()
                await asyncio.wait_for(handler, timeout=3.0)
                deadline = time.monotonic() + 1.0
                while engine._active_device != "cpu" and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)

            final_msgs = [msg for msg in websocket.sent if msg.get("type") == "final"]
            self.assertEqual(len(final_msgs), 1)
            self.assertEqual(final_msgs[0]["text"], "ws text")
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(ctx.pending_pcm_chunks), [])
            self.assertNotIn(session_id, engine._sessions)
            self.assertEqual(engine._active_device, "cpu")
            engine.shutdown()
            server._executor.shutdown(wait=True)

        asyncio.run(run_protocol())


if __name__ == "__main__":
    unittest.main()
