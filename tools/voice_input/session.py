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
from .correction import SemanticCorrectionClient
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
    context_snapshot: Optional[Dict[str, Any]] = None
    context_thread: Optional[Any] = None
    context_error: str = ""
    correction_generation: int = 0
    cancelled: bool = False


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
        getattr(self.injector, "prewarm", lambda: None)()

        self._state = SessionState.IDLE
        self._current_session: Optional[SessionInfo] = None
        self._lock = threading.Lock()
        self._correction_client = (
            SemanticCorrectionClient(config)
            if getattr(config, "semantic_correction_enabled", False)
            else None
        )
        self._correction_lock = threading.Lock()
        self._correction_generation = 0
        self._pending_correction = None
        self._correction_threads = set()

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
        self.correction_listeners: List[Callable[[str, str, str], None]] = []
        self.audio_level_listeners: List[Callable[[float], None]] = []

        # 缓存最近一次识别完成的内容（供焦点丢失时手动补录）
        self.last_transcript: str = ""
        self.last_target: Optional[TargetWindowSnapshot] = None
        self.last_error: str = ""
        self.last_correction_error: str = ""

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

    def add_correction_listener(self, cb: Callable[[str, str, str], None]) -> None:
        self.correction_listeners.append(cb)

    def _notify_correction(self, status: str, text: str = "", message: str = "") -> None:
        for cb in list(self.correction_listeners):
            try:
                cb(status, text, message)
            except Exception:
                pass

    def _is_session_current(self, sess: SessionInfo) -> bool:
        with self._lock:
            return self._current_session is sess and not sess.cancelled

    def _invalidate_pending_correction(self) -> None:
        with self._correction_lock:
            self._correction_generation += 1
            pending = self._pending_correction
            self._pending_correction = None
        if pending and pending.get("lease") is not None:
            try:
                self.injector.end_input_lease(pending["lease"])
            except Exception:
                pass

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
        target_snapshot: Optional[TargetWindowSnapshot] = None,
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
            if self._state != SessionState.IDLE:
                return False

            self._invalidate_pending_correction()

            # 0. 校验 ASR 引擎是否已完成载入
            if self.engine is not None and not getattr(self.engine, "is_loaded", True):
                logger.warning("[Session] ASR 模型尚未载入就绪，忽略录音请求。")
                self.last_error = "ASR模型加载中，请稍候"
                return False

            # 1. 捕获前台输入目标快照 (优先使用热键按下瞬间捕获的高精快照)
            target = target_snapshot or capture_target_snapshot()
            sid = f"sess_{int(time.time() * 1000)}"

            self.last_error = ""
            self._current_session = SessionInfo(
                session_id=sid,
                source=source,
                mode=mode,
                output_mode=output_mode,
                target=target,
                correction_generation=self._correction_generation,
            )
            self._set_state(SessionState.STARTING)

            # Asynchronously fetch CLI context so recording hardware starts with zero delay
            if self._correction_client and output_mode == "inject":
                sess_ref = self._current_session
                client = self._correction_client

                def _bg_capture():
                    try:
                        snap = client.capture_context(target)
                    except Exception:
                        from .correction import default_context_snapshot
                        snap = default_context_snapshot()
                    with self._lock:
                        if not sess_ref.cancelled:
                            sess_ref.context_snapshot = snap

                t = threading.Thread(target=_bg_capture, name=f"VoiceContextFetch-{sid}", daemon=True)
                self._current_session.context_thread = t
                t.start()

            # 2. 创建独立隔离的流式推理会话
            try:
                self.engine.create_session(sid)
            except Exception as e:
                logger.error(f"[Session] 创建 ASR 会话失败: {e}")
                self.last_error = f"ASR创建失败: {e}"
                self._set_state(SessionState.ERROR)
                self._current_session = None
                return False

            # 3. 启动麦克风硬件录音
            try:
                self.recorder.start()
            except Exception as e:
                logger.error(f"[Session] 启动麦克风录音设备失败: {e}")
                self.last_error = f"麦克风错误: {e}"
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
        """Finalize ASR, paste immediately, then correct asynchronously when enabled."""
        with self._lock:
            if not self.is_recording or not self._current_session:
                return
            sess = self._current_session
            stream_thread = self._streaming_thread
            self._set_state(SessionState.DRAINING)
            self._stream_active = False

        begin_drain = getattr(self.engine, "begin_drain", None)
        if callable(begin_drain):
            try:
                begin_drain(sess.session_id)
            except Exception as e:
                logger.warning(f"[Session] begin_drain 异常: {e}")

        # Stop production before joining the pump so finalization has a stable tail.
        _ = self.recorder.stop()
        if stream_thread:
            stream_thread.join()
        if not self._is_session_current(sess):
            return

        for frame in self.recorder.drain_chunks():
            if not self._is_session_current(sess):
                return
            self.engine.feed_chunk(sess.session_id, frame)

        with self._lock:
            if self._current_session is not sess or sess.cancelled:
                return
            self._set_state(SessionState.FINALIZING)

        raw_text = ""
        try:
            raw_text = self.engine.finalize_session(sess.session_id)
        except Exception as e:
            logger.error(f"[Session] 终态识别异常: {e}")
            self.last_error = f"ASR识别失败: {e}"
        if not self._is_session_current(sess):
            return

        cleaned_text = normalize(raw_text)
        sess.raw_text = raw_text
        sess.normalized_text = cleaned_text
        self.last_transcript = cleaned_text
        self.last_target = sess.target

        injection_res = InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "无有效文字")
        correction_lease = None
        if cleaned_text:
            if sess.output_mode == "inject":
                if self._correction_client and sess.context_snapshot:
                    begin_lease = getattr(self.injector, "begin_input_lease", None)
                    if callable(begin_lease):
                        try:
                            correction_lease = begin_lease(sess.target.hwnd, sess.target.pid)
                        except Exception:
                            correction_lease = None
                with self._lock:
                    if self._current_session is not sess or sess.cancelled:
                        if correction_lease is not None:
                            self.injector.end_input_lease(correction_lease)
                        return
                    self._set_state(SessionState.INJECTING)
                injection_res = self.injector.inject_text(
                    text=cleaned_text,
                    target_hwnd=sess.target.hwnd,
                    target_pid=sess.target.pid,
                )
            else:
                injection_res = InjectionResult(True, InjectionOutcome.SUCCESS, "预览模式未注入")

        sess.injection_result = injection_res
        for cb in list(self.final_listeners):
            try:
                cb(cleaned_text, injection_res)
            except Exception:
                pass

        with self._lock:
            still_current = self._current_session is sess and not sess.cancelled
            if still_current:
                self._set_state(SessionState.IDLE)
                self._current_session = None
        if not still_current:
            if correction_lease is not None:
                self.injector.end_input_lease(correction_lease)
            return

        if self._correction_client and cleaned_text and sess.output_mode == "inject":
            ctx_thread = getattr(sess, "context_thread", None)
            if ctx_thread and ctx_thread.is_alive():
                ctx_thread.join(timeout=0.5)
            if not sess.context_snapshot:
                from .correction import default_context_snapshot
                sess.context_snapshot = default_context_snapshot()

            if injection_res.success:
                self._schedule_correction(sess, cleaned_text, correction_lease)
                correction_lease = None
            else:
                if correction_lease is not None:
                    self.injector.end_input_lease(correction_lease)
                self._notify_correction("skipped", "", "原文未成功粘贴，已跳过语义校正")
        elif correction_lease is not None:
            self.injector.end_input_lease(correction_lease)

    def _schedule_correction(self, sess: SessionInfo, original_text: str, lease) -> None:
        with self._correction_lock:
            if sess.correction_generation != self._correction_generation:
                if lease is not None:
                    self.injector.end_input_lease(lease)
                return
            job = {
                "generation": sess.correction_generation,
                "session": sess,
                "original": original_text,
                "lease": lease,
                "context": dict(sess.context_snapshot or {}),
                "scheduled_at": time.monotonic(),
            }
            self._pending_correction = job
        has_context = bool((sess.context_snapshot or {}).get("context"))
        pending_msg = "原文已输入，正在结合对话语义校正" if has_context else "原文已输入，正在语义校正"
        self._notify_correction("pending", "", pending_msg)
        worker = threading.Thread(
            target=self._run_correction,
            args=(job,),
            name="VoiceSemanticCorrection",
            daemon=True,
        )
        self._correction_threads.add(worker)
        worker.start()

    def _correction_job_current(self, job) -> bool:
        with self._correction_lock:
            return (
                self._pending_correction is job
                and self._correction_generation == job["generation"]
            )

    @staticmethod
    def _format_correction_failure_message(result: dict, reason: str) -> str:
        error_type = str(result.get("error_type") or "").strip().lower()
        reason_lower = str(reason or "").strip().lower()
        if error_type == "timeout" or "timeout" in reason_lower or "timed out" in reason_lower:
            return "校正超时，已保留原文"
        if error_type == "connection_refused" or "10061" in reason_lower or "refused" in reason_lower:
            return "中转未启动 (8400端口)"
        if "http_error_502" in reason_lower or "http_error_503" in reason_lower:
            return "校正上游服务暂不可用 (502/503)"
        if "http_error_429" in reason_lower:
            return "校正上游请求超限 (429)"
        if "http_error_401" in reason_lower or "http_error_403" in reason_lower:
            return "校正服务鉴权失败"
        if reason in ("multiline_text", "invalid_correction_text") or error_type == "multiline_text":
            return "模型返回多行，已保留原文"
        if reason == "empty_correction_text" or error_type == "invalid_text":
            return "模型返回空文本，已保留原文"
        if reason == "correction_too_long" or error_type == "too_long":
            return "校正输出超长，已保留原文"
        if "unsupported_stop_reason_max_tokens" in reason:
            return "模型输出截断，已保留原文"
        if reason == "traffic_paused":
            return "流量已暂停，已跳过校正"
        if reason_lower.startswith("http_error_"):
            return f"校正HTTP错误 ({reason})"
        return f"校正失败 ({reason[:16]})"

    def _run_correction(self, job) -> None:
        sess = job["session"]
        lease = job.get("lease")
        try:
            if not self._correction_job_current(job):
                return
            result = self._correction_client.correct(sess.target, job["context"], job["original"])
            if not self._correction_job_current(job):
                return
            if not result.get("ok"):
                reason = str(result.get("reason") or "语义校正请求失败")
                user_msg = self._format_correction_failure_message(result, reason)
                self.last_correction_error = f"{user_msg} ({reason})"
                self._notify_correction("failed", "", user_msg)
                return

            candidate = str(result.get("text") or "").strip()
            if not candidate:
                self.last_correction_error = "模型返回了空校正文本"
                self._notify_correction("failed", "", "模型返回空文本，已保留原文")
                return
            if candidate == job["original"]:
                self._notify_correction("unchanged", candidate, "未发现需要校正的内容")
                return
            if not self._correction_job_current(job):
                return

            replace = getattr(self.injector, "replace_pasted_text", None)
            if lease is not None and callable(replace):
                applied = replace(
                    original_text=job["original"],
                    corrected_text=candidate,
                    target_hwnd=sess.target.hwnd,
                    target_pid=sess.target.pid,
                    activity_lease=lease,
                )
                if applied and getattr(applied, "success", False):
                    with self._correction_lock:
                        if self._pending_correction is job and self._correction_generation == job["generation"]:
                            self.last_transcript = candidate
                    self._notify_correction("applied", candidate, "已替换为语义校正文本")
                    return
                reason = getattr(applied, "message", "目标输入已变化")
            else:
                reason = "无法监测目标输入状态，未自动替换"
            self._notify_correction("suggestion", candidate, f"未自动替换：{reason}")
        except Exception as e:
            logger.warning("[Session] 语义校正失败: %s", e)
            if self._correction_job_current(job):
                self._notify_correction("failed", "", "语义校正服务暂不可用，已保留原文")
        finally:
            if lease is not None:
                try:
                    self.injector.end_input_lease(lease)
                except Exception:
                    pass
            with self._correction_lock:
                if self._pending_correction is job:
                    self._pending_correction = None
            self._correction_threads.discard(threading.current_thread())

    def cancel_session(self) -> None:
        """取消识别或尚未应用的校正结果，避免迟到任务改写输入。"""
        with self._lock:
            if not self.is_recording and self._state == SessionState.IDLE and not self._current_session:
                cancel_pending_only = True
                sess = None
                stream_thread = None
            else:
                cancel_pending_only = False
                sess = self._current_session
                if sess:
                    sess.cancelled = True
                stream_thread = self._streaming_thread
                self._stream_active = False
                self._set_state(SessionState.CANCELLED)

        self._invalidate_pending_correction()
        if cancel_pending_only:
            return

        self.recorder.stop()
        if stream_thread:
            stream_thread.join()
        if sess:
            self.engine.cancel_session(sess.session_id)

        with self._lock:
            if self._current_session is sess:
                self._set_state(SessionState.IDLE)
                self._current_session = None

    def retry_last_injection(self) -> InjectionResult:
        """用户在焦点切换后，点击浮窗重试向当前激活窗口或原目标窗口注入上一次的识别结果"""
        if not self.last_transcript:
            return InjectionResult(False, InjectionOutcome.EMPTY_TEXT, "无历史识别结果")
        # 优先向原目标窗口补录（若有效，依托 WindowsInjector 自动恢复原窗口焦点）
        target_hwnd = self.last_target.hwnd if self.last_target and self.last_target.hwnd else 0
        target_pid = self.last_target.pid if self.last_target and self.last_target.pid else 0
        try:
            from .inject import is_window_valid
            if not target_hwnd or not is_window_valid(target_hwnd):
                curr = capture_target_snapshot()
                target_hwnd = curr.hwnd
                target_pid = curr.pid
        except Exception:
            pass
        return self.injector.inject_text(
            text=self.last_transcript,
            target_hwnd=target_hwnd,
            target_pid=target_pid,
        )
