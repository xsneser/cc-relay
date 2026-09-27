"""会话调度与状态机模块 (SessionCoordinator)：
- 统一仲裁来自桌面悬浮胶囊 (Widget)、全局热键 (Hotkey) 与 Web 控制台的语音输入
- 维护严格的状态机流转 (IDLE -> STARTING -> RECORDING -> DRAINING -> FINALIZING -> INJECTING -> IDLE)
- 驱动音频流式推送、Partial 实时出字派发、VAD 静音自动截断与 2-Pass 终态纠错
- 确保注入安全 (至多一次注入、目标焦点防串流、取消安全作废)
"""

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .asr_engine import BaseStreamingASR
from .audio import AudioRecorder
from .config import VoiceConfig
from .inject import (
    InjectionOutcome,
    InjectionResult,
    TargetWindowSnapshot,
    WindowsInjector,
    capture_target_snapshot,
)
from .normalize import normalize

logger = logging.getLogger("voice_session")


class SessionState(enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RECORDING = "RECORDING"
    DRAINING = "DRAINING"
    FINALIZING = "FINALIZING"
    INJECTING = "INJECTING"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass
class SessionInfo:
    session_id: str
    source: str                    # "widget" | "hotkey" | "web"
    mode: str                      # "toggle" (点击开关) | "ptt" (按住说话)
    output_mode: str               # "inject" (自动输入) | "preview" (仅预览)
    target: TargetWindowSnapshot
    created_at: float = field(default_factory=time.monotonic)
    state: SessionState = SessionState.IDLE
    raw_text: str = ""
    normalized_text: str = ""
    injection_result: Optional[InjectionResult] = None


class SessionCoordinator:
    def __init__(
        self,
        config: VoiceConfig,
        engine: BaseStreamingASR,
        injector: WindowsInjector,
        recorder: Optional[AudioRecorder] = None,
    ):
        self.config = config
        self.engine = engine
        self.injector = injector

        self._state = SessionState.IDLE
        self._current_session: Optional[SessionInfo] = None
        self._lock = threading.Lock()

        # 音频流式推送线程
        self._streaming_thread: Optional[threading.Thread] = None
        self._stream_active = False

        # 录制器实例化 (绑定 VAD 静音超时与电平回调)
        self.recorder = recorder or AudioRecorder(
            sample_rate=self.config.sample_rate,
            channels=self.config.channels,
            frame_duration_ms=self.config.frame_duration_ms,
            pre_roll_ms=self.config.pre_roll_ms,
            max_duration=self.config.max_recording_seconds,
            vad_mode=self.config.vad_mode,
            silence_timeout_seconds=1.2,
            on_silence_timeout=self._handle_silence_timeout,
            on_audio_level=self._handle_audio_level,
        )

        # 观察者回调列表
        self.state_listeners: List[Callable[[SessionState, Optional[SessionInfo]], None]] = []
        self.partial_listeners: List[Callable[[str, str], None]] = []
        self.final_listeners: List[Callable[[str, InjectionResult], None]] = []
        self.audio_level_listeners: List[Callable[[float], None]] = []

        # 缓存最近一次识别完成的内容（供焦点丢失时手动补录）
        self.last_transcript: str = ""
        self.last_target: Optional[TargetWindowSnapshot] = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_recording(self) -> bool:
        return self._state in (SessionState.STARTING, SessionState.RECORDING)

    def add_state_listener(self, cb: Callable[[SessionState, Optional[SessionInfo]], None]) -> None:
        self.state_listeners.append(cb)

    def add_partial_listener(self, cb: Callable[[str, str], None]) -> None:
        self.partial_listeners.append(cb)

    def add_final_listener(self, cb: Callable[[str, InjectionResult], None]) -> None:
        self.final_listeners.append(cb)

    def add_audio_level_listener(self, cb: Callable[[float], None]) -> None:
        self.audio_level_listeners.append(cb)

    def _set_state(self, new_state: SessionState) -> None:
        self._state = new_state
        if self._current_session:
            self._current_session.state = new_state
        for cb in list(self.state_listeners):
            try:
                cb(new_state, self._current_session)
            except Exception:
                pass

    def _handle_audio_level(self, level: float) -> None:
        for cb in list(self.audio_level_listeners):
            try:
                cb(level)
            except Exception:
                pass

    def _handle_silence_timeout(self) -> None:
        """VAD 检测到用户说完话且停顿达到设定阈值 -> 自动触发静音停止上屏"""
        with self._lock:
            # 仅对点击模式 (toggle) 自动截断，PTT 模式由物理松键掌控
            if self._state == SessionState.RECORDING and self._current_session and self._current_session.mode == "toggle":
                logger.info("[Session] VAD 静音超时截断触发，自动收尾并注入文本。")
                threading.Thread(target=self.stop_session, kwargs={"source": "vad"}, daemon=True).start()

    def _stream_pump_loop(self, session_id: str):
        """音频块分发流循环：持续从麦克风拉取 PCM 帧并喂给流式推理模型"""
        while self._stream_active:
            chunk = self.recorder.get_chunk(timeout=0.05)
            if chunk:
                confirmed, partial = self.engine.feed_chunk(session_id, chunk)
                if partial or confirmed:
                    for cb in list(self.partial_listeners):
                        try:
                            cb(confirmed, partial)
                        except Exception:
                            pass

    def start_session(
        self,
        source: str = "widget",
        mode: str = "toggle",
        output_mode: str = "inject",
    ) -> bool:
        """启动一次新的语音听写会话"""
        with self._lock:
            # 如果当前正在录音，且来自同一个 toggle 触发源，则视为“点击停止”
            if self.is_recording:
                if self._current_session and self._current_session.source == source and mode == "toggle":
                    # 异步触发停止
                    threading.Thread(target=self.stop_session, kwargs={"source": source}, daemon=True).start()
                    return True
                return False

            # 1. 捕获前台输入目标快照
            target = capture_target_snapshot()
            sid = f"sess_{int(time.time() * 1000)}"

            self._current_session = SessionInfo(
                session_id=sid,
                source=source,
                mode=mode,
                output_mode=output_mode,
                target=target,
            )
            self._set_state(SessionState.STARTING)

            # 2. 创建独立隔离的流式推理会话
            try:
                self.engine.create_session(sid)
            except Exception as e:
                logger.error(f"[Session] 创建 ASR 会话失败: {e}")
                self._set_state(SessionState.ERROR)
                self._current_session = None
                return False

            # 3. 启动麦克风硬件录音
            try:
                self.recorder.start()
            except Exception as e:
                logger.error(f"[Session] 启动麦克风录音设备失败: {e}")
                self.engine.cancel_session(sid)
                self._set_state(SessionState.ERROR)
                self._current_session = None
                return False

            # 4. 启动音频泵线程
            self._stream_active = True
            self._streaming_thread = threading.Thread(
                target=self._stream_pump_loop,
                args=(sid,),
                daemon=True,
            )
            self._streaming_thread.start()
            self._set_state(SessionState.RECORDING)
            return True

    def stop_session(self, source: Optional[str] = None) -> None:
        """结束录音会话：排空剩余帧 -> 2-Pass 离线纠错 -> 文本规范化 -> 自动注入"""
        with self._lock:
            if not self.is_recording or not self._current_session:
                return

            sess = self._current_session
            self._set_state(SessionState.DRAINING)
            self._stream_active = False

        # 1. 停止硬件录音并排空残留帧
        if self._streaming_thread:
            self._streaming_thread.join(timeout=0.4)

        # 排空流队列中尚未被 pump 消费的尾部帧
        tail_frames = self.recorder.drain_chunks()
        for f in tail_frames:
            self.engine.feed_chunk(sess.session_id, f)

        # 获取整段累积完整音频
        _ = self.recorder.stop()

        with self._lock:
            self._set_state(SessionState.FINALIZING)

        # 2. 2-Pass 离线纠错与标点恢复
        raw_text = ""
        try:
            raw_text = self.engine.finalize_session(sess.session_id)
        except Exception as e:
            logger.error(f"[Session] 终态纠错异常: {e}")

        # 3. 智能命令与标点规范化
        cleaned_text = normalize(raw_text)
        sess.raw_text = raw_text
        sess.normalized_text = cleaned_text

        # 缓存最后结果
        self.last_transcript = cleaned_text
        self.last_target = sess.target

        # 4. 安全注入到目标窗口光标处
        injection_res = InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "无有效文字")
        if cleaned_text:
            if sess.output_mode == "inject":
                with self._lock:
                    self._set_state(SessionState.INJECTING)
                injection_res = self.injector.inject_text(
                    text=cleaned_text,
                    target_hwnd=sess.target.hwnd,
                    target_pid=sess.target.pid,
                )
            else:
                injection_res = InjectionResult(True, InjectionOutcome.SUCCESS, "预览模式未注入")

        sess.injection_result = injection_res

        # 派发完成通知
        for cb in list(self.final_listeners):
            try:
                cb(cleaned_text, injection_res)
            except Exception:
                pass

        with self._lock:
            self._set_state(SessionState.IDLE)
            self._current_session = None

    def cancel_session(self) -> None:
        """安全取消当前会话，丢弃音频并不注入任何内容"""
        with self._lock:
            if not self.is_recording and self._state == SessionState.IDLE:
                return

            sess = self._current_session
            self._stream_active = False
            self._set_state(SessionState.CANCELLED)

        self.recorder.stop()
        if sess:
            self.engine.cancel_session(sess.session_id)

        with self._lock:
            self._current_session = None
            self._set_state(SessionState.IDLE)

    def retry_last_injection(self) -> InjectionResult:
        """用户在焦点切换后，点击浮窗重试向当前激活窗口注入上一次的识别结果"""
        if not self.last_transcript:
            return InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "无历史识别结果")
        # 捕获当前前台窗口并直接注入
        curr = capture_target_snapshot()
        return self.injector.inject_text(
            text=self.last_transcript,
            target_hwnd=curr.hwnd,
            target_pid=curr.pid,
        )
