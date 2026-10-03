"""守护进程装配模块 (Daemon)：
- 统一运行 ASR 引擎单例 (支持 2-Pass 流式因果识别与离线备用)
- 启动 WebSocket / HTTP RPC 服务 (:8401)，支持 Web UI 与 /health 探针
- 托管系统级全局对讲热键监听 (mouse_x1, F8, CapsLock 等)
- 托管桌面免抢焦点悬浮胶囊客户端 (DesktopVoiceWidget)
- 由 SessionCoordinator 驱动，统一流式音频采集 -> Partial 实时出字 -> 2-Pass 全局纠错 -> 自动无感注入
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .asr_engine import BaseStreamingASR, create_engine
from .config import VoiceConfig
from .desktop import DesktopVoiceWidget
from .hotkeys import HotkeyController
from .inject import WindowsInjector
from .server import VoiceServer
from .session import SessionCoordinator, SessionState


class VoiceInputDaemon:
    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig.from_relay_config()
        self.config.validate()

        self.engine: BaseStreamingASR = create_engine(self.config)
        self.injector = WindowsInjector(
            restore_clipboard=self.config.restore_clipboard,
            restore_delay=self.config.restore_clipboard_delay,
            auto_reactivate_target=getattr(self.config, "auto_reactivate_target", True),
            restore_switched_focus=getattr(self.config, "restore_switched_focus", True),
        )

        # 核心会话协调器 (仲裁所有输入源)
        self.coordinator = SessionCoordinator(
            config=self.config,
            engine=self.engine,
            injector=self.injector,
        )

        # 桌面悬浮胶囊 (若环境支持 Tkinter)
        self.widget: Optional[DesktopVoiceWidget] = None
        try:
            self.widget = DesktopVoiceWidget(self.coordinator, on_close=self.stop)
        except Exception as e:
            print(f"[!] 初始化桌面悬浮窗组件跳过: {e}")

        self._is_running = False
        self._lock = threading.Lock()

        # 全局热键控制器 (绑定至 coordinator)
        self.hotkey_ctrl = HotkeyController(
            hotkey_name=self.config.hotkey,
            threshold_seconds=self.config.caps_lock_threshold_seconds,
            on_start_record=self._on_hotkey_start,
            on_stop_record=self._on_hotkey_stop,
            on_cancel=self.coordinator.cancel_session,
        )

        # 内嵌 WebSocket / HTTP 探针服务
        self.server = VoiceServer(self.config)
        self.server.engine = self.engine
        self.server.coordinator = self.coordinator
        self.server.widget = self.widget
        self.server.on_ready = self._on_engine_ready
        self.server.on_error = self._on_engine_error
        self._server_thread: Optional[threading.Thread] = None

        # 注册终端控制台输出观察者
        self.coordinator.add_partial_listener(self._on_console_partial)
        self.coordinator.add_final_listener(self._on_console_final)

    def _on_engine_ready(self):
        """ASR 模型可接收会话后，安全启动全局对讲热键监听。"""
        if not self._is_running:
            return
        startup_id = os.environ.get("CC_VOICE_START_ATTEMPT_ID", "")
        started = time.monotonic()
        if self.server:
            self.server.input_status = {"state": "activating", "ready": False, "error": ""}
        print(f"[*] ASR 引擎已就绪，正在激活全局对讲监听: {self.hotkey_ctrl.get_display_name()}...", flush=True)
        try:
            caps = self.engine.get_capabilities()
            if caps.get("vram_mode") == "on_demand_offload" and str(caps.get("device", "")).startswith("cuda"):
                print("[*] Qwen 模型已在系统内存就绪，将在按键识别时载入显存。", flush=True)
        except Exception:
            pass
        try:
            self.hotkey_ctrl.start()
            elapsed = round(time.monotonic() - started, 2)
            if self.server:
                self.server.input_status = {"state": "ready", "ready": True, "error": ""}
            print(f"[+] 全局对讲热键已就绪！(激活耗时 {elapsed}s)", flush=True)
            if startup_id:
                print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())}] [VOICE_STARTUP] "
                      f"{json.dumps({'attempt_id': startup_id, 'phase': 'input_ready', 'duration_seconds': elapsed}, ensure_ascii=False)}",
                      flush=True)
        except Exception as e:
            elapsed = round(time.monotonic() - started, 2)
            if self.server:
                self.server.input_status = {"state": "error", "ready": False, "error": str(e)}
            print(f"[!] 全局热键监听未就绪 ({e})，激活耗时 {elapsed}s。", flush=True)

        if self.widget:
            self.widget.set_ready()

    def _on_engine_error(self, err_msg: str):
        if self.server:
            self.server.input_status = {"state": "error", "ready": False, "error": str(err_msg)}
        if self.widget:
            self.widget.set_error(err_msg)

    @property
    def _pid_file(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / ".voice.pid"

    def _write_pid_file(self):
        """记录自身 PID 至根目录 .voice.pid 文件，方便 Relay 管理"""
        try:
            pid_path = self._pid_file
            tmp = str(pid_path) + f".tmp_{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(),
                    "create_time": time.time(),
                    "cmdline_marker": "tools.voice_input",
                }, f)
            os.replace(tmp, str(pid_path))
        except Exception:
            pass

    def _cleanup_pid_file(self):
        """退出时仅在当前记录仍属于自身时清理 PID 文件"""
        try:
            pid_path = self._pid_file
            if pid_path.is_file():
                with open(pid_path, "r", encoding="utf-8") as f:
                    pinfo = json.load(f)
                if pinfo.get("pid") == os.getpid():
                    os.remove(str(pid_path))
        except Exception:
            pass

    def _play_feedback(self, frequency: int = 1000, duration_ms: int = 50):
        if not self.config.beep_feedback:
            return
        try:
            import winsound
            winsound.Beep(frequency, duration_ms)
        except Exception:
            pass

    def _on_hotkey_start(self, target_snapshot=None):
        """按下热键触发录音"""
        if target_snapshot is None:
            try:
                from .inject import capture_target_snapshot
                target_snapshot = capture_target_snapshot()
            except Exception:
                target_snapshot = None
        self._play_feedback(1200, 40)
        self.coordinator.start_session(
            source="hotkey",
            mode="ptt",
            output_mode="inject",
            target_snapshot=target_snapshot,
        )

    def _on_hotkey_stop(self):
        """松开热键结束录音并触发 2-Pass 纠错与注入"""
        self._play_feedback(800, 40)
        self.coordinator.stop_session(source="hotkey")
        self.hotkey_ctrl.set_idle()

    def _on_console_partial(self, confirmed: str, partial: str):
        """控制台实时出字打印"""
        sys.stdout.write(f"\r[● 说话中] {confirmed} >> {partial}")
        sys.stdout.flush()

    def _on_console_final(self, text: str, res):
        """控制台最终结果打印"""
        sys.stdout.write("\r" + " " * 70 + "\r")
        if text:
            print(f"[✓ 识别完成] 注入文本: \"{text}\" ({res.outcome.value})")
        else:
            print(f"[!] 未检测到有效文字")

    def _run_server_thread(self):
        """在后台线程运行 WebSocket / HTTP 探针服务"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.server.start())
        except Exception as e:
            print(f"[-] 语音伴侣 RPC 服务异常: {e}")

    def start(self, headless: bool = False):
        """启动伴侣守护进程"""
        print("=" * 60)
        print(" Claude Code CLI 中文语音输入伴侣 (客户端级免复制自动输入)")
        print("=" * 60)
        print(f"[*] 引擎模式: {self.config.engine}")
        print(f"[*] 触发方式: 桌面悬浮麦克风点击 + {self.hotkey_ctrl.get_display_name()}")
        print(f"[*] 内部服务: ws://{self.config.host}:{self.config.port}/ws/voice")

        self._is_running = True
        self._write_pid_file()

        # 1. 启动 RPC 探针与 WebSocket 服务后台线程
        self._server_thread = threading.Thread(target=self._run_server_thread, daemon=True)
        self._server_thread.start()

        # 2. 检查引擎就绪状态：若已载入直接激活热键，否则在 ASR 加载完毕后由 server.on_ready 唤醒
        if getattr(self.engine, "is_loaded", False):
            self._on_engine_ready()
        else:
            print("[*] 伴侣网络端口已就绪，正在后台并发预载 ASR 模型（加载完成后自动激活对讲热键与悬浮输入）...")

        # 3. 运行桌面悬浮胶囊界面
        if not headless and self.widget is not None:
            try:
                self.widget.start()
            except Exception as e:
                print(f"[!] 桌面悬浮窗异常退出 ({e})，切换至后台无头服务模式。")
                headless = True
                if self.server:
                    self.server.widget = None

        if headless or self.widget is None:
            try:
                while self._is_running:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        """安全退出并清理资源"""
        print("\n[*] 正在退出语音伴侣守护进程...")
        self._is_running = False
        self.coordinator.cancel_session()
        try:
            self.injector.close()
        except Exception:
            pass
        self.hotkey_ctrl.stop()
        if self.widget:
            self.widget.stop()
        self.server.stop()
        self._cleanup_pid_file()
        print("[+] 退出完成。")
