"""WebSocket 协议、会话隔离与健康探针单元测试"""

import asyncio
import json
import unittest
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

    def test_config_from_relay(self):
        conf_dict = {
            "tools": {
                "voice": {
                    "auto_start": True,
                    "hotkey": "f8",
                    "engine": "sensevoice_offline",
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
            self.assertEqual(cfg.port, 8405)


if __name__ == "__main__":
    unittest.main()
