"""语音 WebSocket 与 HTTP 探针服务模块：
- 监听 127.0.0.1:8401 (拒绝 0.0.0.0)
- Origin 白名单校验 (限制 127.0.0.1:* 与 localhost:*)
- HTTP /health 健康探针与 Hello 协议握手
- /ws/voice 双向流式 ASR 通道 (支持 session 隔离、Partial / Final 双结果输出)
- 独立推理工作线程池 (不阻塞 asyncio 事件循环)
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .asr_engine import BaseStreamingASR, create_engine
from .config import VoiceConfig

logger = logging.getLogger("voice_server")


class VoiceServer:
    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self.config.validate()

        self.engine: BaseStreamingASR = create_engine(self.config)
        self.coordinator = None
        self.widget = None
        self.status = "starting"
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="VoiceInference")
        self._stop_event = asyncio.Event()

        # 客户端会话映射: connection -> session_id
        self._conn_sessions: Dict[ServerConnection, str] = {}

    def is_allowed_origin(self, origin: Optional[str]) -> bool:
        """Origin 安全白名单校验"""
        if origin is None:
            # 允许无 Origin 的非浏览器客户端 (如桌面 CLI/对讲伴侣)
            return True
        try:
            parsed = urlparse(origin)
            host = parsed.hostname
            if host in ("127.0.0.1", "localhost"):
                return True
        except Exception:
            pass
        return False

    def _build_capabilities(self) -> Dict[str, Any]:
        caps = dict(self.engine.get_capabilities()) if hasattr(self.engine, "get_capabilities") else {}
        try:
            import sounddevice
            caps["microphone"] = True
        except Exception:
            caps["microphone"] = False
        try:
            import pynput
            caps["hotkey"] = True
        except Exception:
            caps["hotkey"] = False
        return caps

    def process_request(self, conn: ServerConnection, req: Any):
        """处理 HTTP 探测请求 (如 /health)"""
        origin = req.headers.get("Origin")
        if not self.is_allowed_origin(origin):
            resp = conn.respond(403, "Forbidden Origin\n")
            resp.headers["Content-Type"] = "text/plain"
            return resp

        if req.path in ("/health", "/api/health"):
            caps = self._build_capabilities()
            coord_state = self.coordinator.state.value if self.coordinator else "unknown"
            body = json.dumps(
                {
                    "service": "voice",
                    "protocol_version": "1.1",
                    "ready": self.engine.is_loaded,
                    "status": self.status,
                    "engine": self.config.engine,
                    "capabilities": caps,
                    "session_state": coord_state,
                    "pid": os.getpid(),
                },
                ensure_ascii=False,
            )
            resp = conn.respond(200, body)
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
            if origin:
                resp.headers["Access-Control-Allow-Origin"] = origin
            return resp
        return None

    async def handle_connection(self, websocket: ServerConnection):
        """处理 WebSocket /ws/voice 双向长连接"""
        # 1. 握手阶段下发 Hello
        caps = self._build_capabilities()
        hello_msg = {
            "type": "hello",
            "service": "voice",
            "protocol_version": "1.1",
            "ready": self.engine.is_loaded,
            "status": self.status,
            "engine": self.config.engine,
            "capabilities": caps,
        }
        await websocket.send(json.dumps(hello_msg, ensure_ascii=False))

        current_session: Optional[str] = None
        loop = asyncio.get_running_loop()

        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    # 二进制音频小块流 (PCM16 16kHz)
                    if current_session is None:
                        continue
                    # 放入工作线程池异步推理，避免卡顿网络事件循环
                    confirmed, partial = await loop.run_in_executor(
                        self._executor, self.engine.feed_chunk, current_session, message
                    )
                    if partial or confirmed:
                        reply = {
                            "type": "partial",
                            "session_id": current_session,
                            "confirmed_text": confirmed,
                            "partial_text": partial,
                        }
                        await websocket.send(json.dumps(reply, ensure_ascii=False))

                elif isinstance(message, str):
                    try:
                        data = json.loads(message)
                    except Exception:
                        continue

                    action = data.get("action")
                    if action == "hello":
                        await websocket.send(json.dumps(hello_msg, ensure_ascii=False))

                    elif action in ("show_capsule", "hide_capsule", "toggle_capsule"):
                        if self.widget and self.widget.root:
                            if action == "show_capsule":
                                self.widget.root.after(0, self.widget.show)
                            elif action == "hide_capsule":
                                self.widget.root.after(0, self.widget.hide)
                            elif action == "toggle_capsule":
                                if self.widget.root.winfo_viewable():
                                    self.widget.root.after(0, self.widget.hide)
                                else:
                                    self.widget.root.after(0, self.widget.show)
                        await websocket.send(
                            json.dumps({"type": "capsule_ack", "action": action}, ensure_ascii=False)
                        )

                    elif action == "start":
                        sid = data.get("session_id") or f"sess_{int(time.time()*1000)}"
                        current_session = sid
                        self._conn_sessions[websocket] = sid
                        # 开启独立会话隔离 Cache
                        await loop.run_in_executor(self._executor, self.engine.create_session, sid)
                        await websocket.send(
                            json.dumps(
                                {"type": "state", "session_id": sid, "state": "recording"},
                                ensure_ascii=False,
                            )
                        )

                    elif action == "stop":
                        sid = data.get("session_id") or current_session
                        if sid:
                            t0 = time.monotonic()
                            # 触发 2-Pass 离线纠错与尾部提取
                            final_text = await loop.run_in_executor(
                                self._executor, self.engine.finalize_session, sid
                            )
                            cost_ms = (time.monotonic() - t0) * 1000
                            reply = {
                                "type": "final",
                                "session_id": sid,
                                "text": final_text,
                                "metrics": {"duration_ms": cost_ms},
                            }
                            await websocket.send(json.dumps(reply, ensure_ascii=False))
                        current_session = None
                        self._conn_sessions.pop(websocket, None)

                    elif action == "cancel":
                        sid = data.get("session_id") or current_session
                        if sid:
                            await loop.run_in_executor(self._executor, self.engine.cancel_session, sid)
                        current_session = None
                        self._conn_sessions.pop(websocket, None)

                    elif action == "ping":
                        await websocket.send(json.dumps({"type": "pong"}, ensure_ascii=False))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if current_session:
                try:
                    await loop.run_in_executor(self._executor, self.engine.cancel_session, current_session)
                except Exception:
                    pass
            self._conn_sessions.pop(websocket, None)

    async def start(self):
        """服务主入口：即时监听 8401 端口，后台异步预载 ASR 模型"""
        self.status = "loading_model"
        print(f"[*] 语音服务正在启动并监听: ws://{self.config.host}:{self.config.port}/ws/voice")

        # 1. 率先启动 WebSocket 与 HTTP 综合服务 (端口立即打开并可响应 /health!)
        server = await serve(
            self.handle_connection,
            host=self.config.host,
            port=self.config.port,
            process_request=self.process_request,
        )
        print(f"[+] 语音服务端口已就绪: {self.config.host}:{self.config.port}")

        # 2. 异步在工作线程中载入 ASR 模型
        loop = asyncio.get_running_loop()

        def _load_task():
            print(f"[*] 正在后台载入 ASR 模型 ({self.config.engine})...")
            try:
                self.engine.load()
                self.status = "ready"
                print("[+] ASR 模型载入成功，引擎完全就绪！")
            except Exception as e:
                self.status = "error"
                print(f"[-] ASR 模型载入失败: {e}")

        # 异步触发，不阻塞 serve
        loop.run_in_executor(self._executor, _load_task)

        try:
            await self._stop_event.wait()
        finally:
            server.close()
            await server.wait_closed()
            self._executor.shutdown(wait=False)

    def stop(self):
        self._stop_event.set()


def run_server(config: Optional[VoiceConfig] = None):
    """同步启动服务器方法"""
    cfg = config or VoiceConfig.from_relay_config()
    server = VoiceServer(cfg)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    run_server()
