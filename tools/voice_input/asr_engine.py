"""ASR 引擎抽象与识别实现：
- BaseStreamingASR 统一接口
- QwenASREngine (Qwen3-ASR-1.7B 原生流式分块 + 全音频终审高精引擎，零异构依赖)
- SenseVoiceOfflineEngine (轻量极速 SenseVoice ONNX 单 Pass 引擎)
- Sherpa2PassEngine (sherpa-onnx 统一轻量 2-Pass 引擎)
- 严格的 Session Cache 生命周期隔离与防串流机制
"""

import os
import threading
from collections import deque
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

try:
    import torch
except Exception:
    torch = None

try:
    import qwen_asr
except Exception:
    qwen_asr = None

from .audio import VADDetector
from .config import VoiceConfig


@dataclass
class SessionContext:
    session_id: str
    cache: Dict[str, Any] = field(default_factory=dict)
    confirmed_text: str = ""
    current_partial: str = ""
    completed_segments: List[Dict[str, Any]] = field(default_factory=list)
    pending_pcm_chunks: Deque[bytes] = field(default_factory=deque, repr=False)
    hypothesis_history: Deque[str] = field(default_factory=deque, repr=False)
    vad_boundaries: Deque[Dict[str, Any]] = field(default_factory=deque, repr=False)
    lock: Any = field(default_factory=threading.RLock, repr=False)
    accumulated_pcm: bytearray = field(default_factory=bytearray)
    buffer: bytearray = field(default_factory=bytearray)
    chunk_id: int = 0
    _raw_decoded: str = ""
    total_pcm_len: int = 0
    audio_base_offset: int = 0
    last_inferred_pcm_len: int = 0
    last_speech_offset: int = 0
    last_inferred_audio_offset: int = 0
    is_draining: bool = False
    trailing_silence_bytes: int = 0
    speech_seen: bool = False
    pause_candidate_pending: bool = False
    pause_candidate_active: bool = False
    segment_inference_seconds: float = 0.0
    segment_inference_count: int = 0
    seq: int = 0
    created_at: float = field(default_factory=time.monotonic)
    is_active: bool = True
    online_stream: Any = None


class BaseStreamingASR(ABC):
    """统一流式 ASR 抽象基类"""

    @abstractmethod
    def load(self) -> None:
        """模型载入常驻内存并执行 1 帧 Warm-up"""
        pass

    @abstractmethod
    def create_session(self, session_id: str) -> SessionContext:
        """创建独立的流式识别会话（独立 Cache）"""
        pass

    @abstractmethod
    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        """送入 PCM 数据块，返回 (confirmed_text, partial_text)"""
        pass

    @abstractmethod
    def finalize_session(self, session_id: str) -> str:
        """结束会话：尾部提取 -> 离线重算与标点 -> 销毁 Cache"""
        pass

    def begin_drain(self, session_id: str) -> None:
        """标记会话进入排空阶段，抑制中间投机性推理"""
        pass

    @abstractmethod
    def cancel_session(self, session_id: str) -> None:
        """取消会话并回收资源"""
        pass

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        pass

    @abstractmethod
    def get_capabilities(self) -> Dict[str, Any]:
        """返回引擎实际能力集 (如是否支持真正流式、是否支持标点恢复等)"""
        pass


def _find_local_qwen_dir(model_id: str = "Qwen/Qwen3-ASR-1.7B") -> Optional[str]:
    """优先查找本地已缓存的 Qwen ASR 1.7B 模型权重目录 (绝不隐式联网下载)"""
    # 1. 显式配置或下载的本地模型目录
    repo_model = Path(__file__).resolve().parent / "models" / "Qwen3-ASR-1.7B"
    if repo_model.is_dir() and ((repo_model / "config.json").is_file() or any(repo_model.glob("*.safetensors"))):
        return str(repo_model.resolve())

    repo_model_sub = Path(__file__).resolve().parent / "models" / model_id
    if repo_model_sub.is_dir() and ((repo_model_sub / "config.json").is_file() or any(repo_model_sub.glob("*.safetensors"))):
        return str(repo_model_sub.resolve())

    # 2. ModelScope 默认缓存路径
    ms_candidates = [
        Path(os.path.expanduser("~/.cache/modelscope/hub/models")) / model_id,
        Path(os.path.expanduser("~/.cache/modelscope/hub")) / model_id,
        Path(os.path.expanduser("~/.cache/modelscope/hub/models/Qwen")) / "Qwen3-ASR-1.7B",
        Path(os.path.expanduser("~/.cache/modelscope/hub/Qwen")) / "Qwen3-ASR-1.7B",
    ]
    for c in ms_candidates:
        if c.is_dir() and ((c / "config.json").is_file() or any(c.glob("*.safetensors")) or any(c.glob("*.bin"))):
            return str(c.resolve())

    # 3. Hugging Face 默认缓存路径
    hf_cache = Path(os.path.expanduser("~/.cache/huggingface/hub")) / f"models--{model_id.replace('/', '--')}"
    if hf_cache.is_dir():
        snapshots = hf_cache / "snapshots"
        if snapshots.is_dir():
            snaps = [s for s in snapshots.iterdir() if s.is_dir()]
            if snaps:
                latest = max(snaps, key=lambda p: p.stat().st_mtime)
                return str(latest.resolve())
    return None


class QwenASREngine(BaseStreamingASR):
    """Qwen3-ASR with rolling partials and stable-prefix retirement.

    Partials re-decode the active PCM window. Text is committed only when it is
    stable across recent hypotheses, has a VAD-backed audio boundary, and is older
    than the trailing-audio guard. There is no fixed-duration segment cut.
    VRAM residency mode changes placement, not the decode path.
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._is_loaded = False
        self._qwen_model = None
        self._pipe = None
        self._model = None
        self._processor = None
        self._resolved_device = "cpu"
        self._active_device = "cpu"
        self._model_dtype = None
        self._lock = threading.RLock()
        self._inference_lock = threading.RLock()
        self._sessions: Dict[str, SessionContext] = {}
        self._active_sessions = set()
        self._resource_cond = threading.Condition(self._lock)
        self._resource_thread = None
        self._resource_stop = False
        self._resource_state = "not_loaded"
        self._resource_error = ""
        self._resource_generation = 0
        self._failed_generation = None
        self._gpu_ready = threading.Event()
        self._vad = VADDetector(mode=self.config.vad_mode, sample_rate=self.config.sample_rate)
        self._frame_bytes = max(1, int(self.config.sample_rate * 2 * self.config.frame_duration_ms / 1000))
        self._holdback_bytes = int(self.config.sample_rate * 2 * self.config.qwen_trailing_audio_guard_seconds)
        self._stable_hypothesis_count = max(2, int(self.config.qwen_stable_hypothesis_count))
        self._segment_silence_bytes = int(self.config.sample_rate * 2 * self.config.qwen_segment_silence_seconds)

        # Partial updates still re-decode the active rolling audio window.
        self.stride_bytes = int(self.config.sample_rate * 2 * 0.8)

    def get_capabilities(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "engine": self.config.engine or "qwen_asr",
                "is_loaded": self._is_loaded,
                "streaming_available": True,
                "offline_available": True,
                "punc_available": True,
                "device": self._resolved_device,
                "model_device": self._active_device,
                "gpu_resident": self._active_device.startswith("cuda"),
                "vram_mode": self.config.vram_mode,
                "residency_state": self._resource_state,
                "residency_error": self._resource_error,
            }

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def _is_on_demand_gpu(self) -> bool:
        return (
            self.config.vram_mode == "on_demand_offload"
            and self._resolved_device.startswith("cuda")
        )

    def _on_demand_gpu_ready(self) -> bool:
        with self._lock:
            return (
                self._active_device.startswith("cuda")
                and self._resource_state == "gpu_ready"
                and not self._resource_error
            )

    def _resource_worker(self) -> None:
        """Move weights between CPU and the target GPU as sessions acquire/release them."""
        while True:
            with self._resource_cond:
                self._resource_cond.wait_for(
                    lambda: self._resource_stop
                    or (
                        self._is_loaded
                        and self._is_on_demand_gpu()
                        and self._failed_generation != self._resource_generation
                        and (bool(self._active_sessions) != self._active_device.startswith("cuda"))
                    )
                )
                if self._resource_stop:
                    return
                want_gpu = bool(self._active_sessions)
                target = self._resolved_device if want_gpu else "cpu"
                if want_gpu:
                    self._resource_state = "activating_gpu"
                    self._resource_error = ""
                else:
                    self._resource_state = "offloading_cpu"
                    self._gpu_ready.clear()

            try:
                with self._inference_lock:
                    with self._lock:
                        # A new session may arrive while the worker waits for inference to finish.
                        want_gpu = bool(self._active_sessions)
                        target = self._resolved_device if want_gpu else "cpu"
                        current = self._active_device
                        if target == current:
                            self._resource_state = "gpu_ready" if target.startswith("cuda") else "cpu_ready"
                            if target.startswith("cuda"):
                                self._gpu_ready.set()
                            self._resource_cond.notify_all()
                            continue
                        self._resource_state = "activating_gpu" if want_gpu else "offloading_cpu"
                        if not want_gpu:
                            self._gpu_ready.clear()
                    self._move_backends(target)
                    if torch is not None and target.startswith("cuda"):
                        torch.cuda.synchronize(target)
                    elif torch is not None and self._active_device.startswith("cuda"):
                        torch.cuda.synchronize(self._active_device)
                        torch.cuda.empty_cache()
                with self._resource_cond:
                    self._active_device = target
                    self._resource_state = "gpu_ready" if target.startswith("cuda") else "cpu_ready"
                    self._resource_error = ""
                    self._failed_generation = None
                    if target.startswith("cuda"):
                        self._gpu_ready.set()
                    self._resource_cond.notify_all()
            except Exception as exc:
                recovery_error = ""
                try:
                    with self._inference_lock:
                        self._move_backends("cpu")
                        if torch is not None and torch.cuda.is_available():
                            torch.cuda.empty_cache()
                except Exception as recovery_exc:
                    recovery_error = f"; CPU 恢复失败: {recovery_exc}"
                with self._resource_cond:
                    self._active_device = "cpu" if not recovery_error else "unknown"
                    self._resource_state = "error"
                    self._resource_error = f"模型显存迁移失败: {exc}{recovery_error}"
                    self._failed_generation = self._resource_generation
                    self._gpu_ready.set()
                    self._resource_cond.notify_all()
                    print(f"[ASR] {self._resource_error}")

    def _move_model_tensor_caches(self, model, device: str) -> None:
        """Move known non-buffer Qwen tensors that Module.to() does not manage."""
        if torch is None or not hasattr(model, "modules"):
            return
        for module in model.modules():
            for name in ("rope_deltas", "original_inv_freq"):
                value = getattr(module, name, None)
                if torch.is_tensor(value) and value.device != torch.device(device):
                    setattr(module, name, value.to(device))

    def _move_backends(self, device: str) -> None:
        """Move whichever Qwen/Transformers backend was loaded to one device."""
        if self._qwen_model is not None:
            model = getattr(self._qwen_model, "model", None)
            if model is None or not hasattr(model, "to"):
                raise RuntimeError("当前 Qwen 包装器不支持运行时设备迁移")
            model.to(device)
            self._move_model_tensor_caches(model, device)
            if hasattr(self._qwen_model, "device"):
                self._qwen_model.device = torch.device(device) if torch is not None else device
            if hasattr(self._qwen_model, "dtype") and model is not None:
                try:
                    self._qwen_model.dtype = next(model.parameters()).dtype
                except Exception:
                    pass
            return

        if self._pipe is not None:
            model = getattr(self._pipe, "model", None)
            if model is None or not hasattr(model, "to"):
                raise RuntimeError("当前 Transformers pipeline 不支持运行时设备迁移")
            model.to(device)
            self._move_model_tensor_caches(model, device)
            if hasattr(self._pipe, "device"):
                self._pipe.device = torch.device(device) if torch is not None else device
            return

        if self._model is not None and hasattr(self._model, "to"):
            self._model.to(device)
            self._move_model_tensor_caches(self._model, device)
            return

        raise RuntimeError("未找到可迁移的 Qwen 模型后端")

    def _ensure_gpu_ready(self, timeout: float = 45.0) -> None:
        if not self._is_on_demand_gpu():
            return
        if not self._gpu_ready.wait(timeout):
            raise TimeoutError("等待语音模型载入显存超时")
        with self._lock:
            if self._resource_error:
                raise RuntimeError(self._resource_error)
            if not self._active_device.startswith("cuda"):
                raise RuntimeError("语音模型尚未进入 GPU")

    def shutdown(self) -> None:
        """Stop residency management and release model weights from CUDA."""
        with self._resource_cond:
            self._resource_stop = True
            self._resource_cond.notify_all()
            worker = self._resource_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5.0)
        if self._active_device.startswith("cuda"):
            try:
                with self._inference_lock:
                    self._move_backends("cpu")
                    if torch is not None:
                        torch.cuda.empty_cache()
                self._active_device = "cpu"
            except Exception as exc:
                print(f"[ASR] 退出时释放 CUDA 模型失败: {exc}")

    def load(self) -> None:
        """载入 Qwen ASR 1.7B 模型并执行单帧 Warm-up"""
        with self._lock:
            if self._is_loaded:
                return

            t0 = time.monotonic()
            try:
                import torch
            except Exception:
                torch = None

            local_dir = _find_local_qwen_dir(self.config.qwen_model_id)
            if not local_dir:
                raise FileNotFoundError("未在本地检测到 Qwen ASR 1.7B 离线模型，无法启动。请在 Web 控制台点击【一键自动下载】")

            model_path = local_dir

            # 设备与精度解析
            cfg_dev = str(self.config.device or self.config.qwen_device or "auto").strip().lower()
            if cfg_dev == "cpu":
                self._resolved_device = "cpu"
                torch_dtype = torch.float32
            elif cfg_dev.startswith("cuda"):
                if torch is not None and torch.cuda.is_available():
                    self._resolved_device = cfg_dev if ":" in cfg_dev else "cuda:0"
                    torch_dtype = torch.float16
                else:
                    print(f"[!] 显式请求了 {cfg_dev}，但当前 PyTorch 未支持 CUDA，回退为 CPU")
                    self._resolved_device = "cpu"
                    torch_dtype = torch.float32
            else:
                # "auto": 优先 GPU
                if torch is not None and torch.cuda.is_available():
                    self._resolved_device = "cuda:0"
                    torch_dtype = torch.float16
                else:
                    self._resolved_device = "cpu"
                    torch_dtype = torch.float32

            self._model_dtype = torch_dtype
            on_demand_gpu = self._is_on_demand_gpu()
            load_device = "cpu" if on_demand_gpu else self._resolved_device

            # 仅在纯 CPU 推理模式下受控限制线程数；按需模式只在 CPU 待机，不执行 CPU 推理。
            if self._resolved_device == "cpu" and torch is not None:
                threads = max(1, min(int(self.config.num_threads or 2), os.cpu_count() or 2))
                os.environ.setdefault("OMP_NUM_THREADS", str(threads))
                os.environ.setdefault("MKL_NUM_THREADS", str(threads))
                try:
                    torch.set_num_threads(threads)
                except Exception:
                    pass

            # 1. 优先使用官方 Qwen3-ASR 模型包装器
            try:
                from qwen_asr import Qwen3ASRModel
                model_kwargs = {"dtype": torch_dtype}
                if load_device != "cpu":
                    model_kwargs["device_map"] = load_device
                self._qwen_model = Qwen3ASRModel.from_pretrained(model_path, **model_kwargs)
            except Exception as ex_qwen:
                if "out of memory" in str(ex_qwen).lower():
                    raise RuntimeError("GPU 显存不足 (CUDA Out of Memory)！请关闭占用显存的应用或切换为纯 CPU 模式") from ex_qwen
                # 2. 备选：通过 transformers 加载
                self._qwen_model = None
                try:
                    from transformers import pipeline
                    pipe_kwargs = {
                        "task": "automatic-speech-recognition",
                        "model": model_path,
                        "device": load_device,
                    }
                    if torch_dtype is not None:
                        pipe_kwargs["torch_dtype"] = torch_dtype
                    self._pipe = pipeline(**pipe_kwargs)
                except Exception as ex:
                    try:
                        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
                        self._processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
                        self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
                            model_path,
                            local_files_only=True,
                            torch_dtype=torch_dtype,
                        )
                        if load_device != "cpu" and torch is not None:
                            self._model = self._model.to(load_device)
                    except Exception as ex2:
                        raise RuntimeError(f"Qwen ASR 1.7B 本地模型载入失败 (模型路径: {model_path}): {ex_qwen}; {ex}; {ex2}")

            self._is_loaded = True
            self._active_device = load_device
            self._resource_state = "cpu_ready" if load_device == "cpu" else "gpu_ready"

            # 按需模式不预热 CPU 或 CUDA，避免启动时触发额外计算和显存分配。
            if not on_demand_gpu:
                try:
                    dummy_pcm = np.zeros(1600, dtype=np.float32)
                    self._transcribe_samples(dummy_pcm)
                except Exception:
                    pass

            if self._is_on_demand_gpu():
                self._gpu_ready.clear()
                self._resource_thread = threading.Thread(
                    target=self._resource_worker,
                    name="QwenResidency",
                    daemon=True,
                )
                self._resource_thread.start()
            else:
                self._gpu_ready.set()

            cost = (time.monotonic() - t0) * 1000
            print(
                f"[ASR] [OK] Qwen ASR 1.7B 引擎初始化就绪 "
                f"(耗时: {cost:.1f}ms, target: {self._resolved_device}, resident: {self._active_device})"
            )

    def _transcribe_samples(self, samples: np.ndarray, context: str = "") -> str:
        with self._inference_lock:
            return self._transcribe_samples_unlocked(samples, context=context)

    def _transcribe_samples_unlocked(self, samples: np.ndarray, context: str = "") -> str:
        if getattr(self, "_qwen_model", None) is not None:
            try:
                kwargs = {"audio": (samples, self.config.sample_rate)}
                if context:
                    kwargs["context"] = context
                try:
                    res = self._qwen_model.transcribe(**kwargs)
                except TypeError:
                    kwargs.pop("context", None)
                    res = self._qwen_model.transcribe(**kwargs)
                if res and len(res) > 0:
                    return getattr(res[0], "text", "").strip()
            except Exception:
                pass
            return ""
        elif self._pipe is not None:
            res = self._pipe({"raw": samples, "sampling_rate": self.config.sample_rate})
            if isinstance(res, dict):
                return res.get("text", "").strip()
            elif isinstance(res, list) and len(res) > 0 and isinstance(res[0], dict):
                return res[0].get("text", "").strip()
            return str(res).strip()
        elif self._model is not None and self._processor is not None:
            import torch
            inputs = self._processor(samples, sampling_rate=self.config.sample_rate, return_tensors="pt")
            if str(self._resolved_device).startswith("cuda") and torch.cuda.is_available():
                inputs = {k: v.to(self._resolved_device) for k, v in inputs.items()}
            with torch.no_grad():
                gen_ids = self._model.generate(**inputs, max_new_tokens=512)
            return self._processor.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
        return ""

    def create_session(self, session_id: str) -> SessionContext:
        with self._resource_cond:
            ctx = SessionContext(session_id=session_id)
            ctx.hypothesis_history = deque(maxlen=self._stable_hypothesis_count)
            self._sessions[session_id] = ctx
            if self._is_on_demand_gpu():
                if not self._active_sessions:
                    self._resource_generation += 1
                    self._resource_error = ""
                    self._failed_generation = None
                self._active_sessions.add(session_id)
                if not self._active_device.startswith("cuda") or self._resource_state == "offloading_cpu":
                    self._gpu_ready.clear()
                else:
                    self._gpu_ready.set()
                self._resource_cond.notify_all()
            else:
                self._active_sessions.add(session_id)
            return ctx

    def _streaming_decode_step(self, ctx: SessionContext, chunk_pcm: bytes = b"") -> str:
        """更新当前有界音频段的完整 partial；绝不把已提交段的音频再次送入模型。"""
        raw_bytes = bytes(ctx.accumulated_pcm)
        if not raw_bytes:
            return ctx.current_partial

        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        inference_started = time.monotonic()
        text_out = self._transcribe_samples(samples)
        ctx.segment_inference_seconds += time.monotonic() - inference_started
        ctx.segment_inference_count += 1
        ctx.buffer.clear()

        if text_out:
            try:
                from qwen_asr.inference.utils import detect_and_fix_repetitions
                text_out = detect_and_fix_repetitions(text_out)
            except Exception:
                pass
            ctx.current_partial = text_out
            ctx._raw_decoded = text_out
            ctx.last_inferred_pcm_len = len(raw_bytes)
            ctx.last_inferred_audio_offset = ctx.audio_base_offset + len(raw_bytes)
            ctx.hypothesis_history.append(text_out)

        ctx.chunk_id += 1
        return ctx.current_partial

    def _refresh_confirmed_text(self, ctx: SessionContext) -> None:
        text = ""
        for segment in ctx.completed_segments:
            text = self._join_transcript_parts(text, segment.get("text", ""))
        ctx.confirmed_text = text

    def _resolve_deferred_segments(self, ctx: SessionContext) -> None:
        resolved_text = ""
        for segment in ctx.completed_segments:
            raw = segment.get("pcm")
            text = segment.get("text", "")
            if raw is not None:
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                result = self._transcribe_samples(samples)
                if result:
                    try:
                        from qwen_asr.inference.utils import detect_and_fix_repetitions
                        result = detect_and_fix_repetitions(result)
                    except Exception:
                        pass
                    text = result
                elif not text:
                    print("[ASR] Qwen segment retry returned no text; using any available partial")
                segment["text"] = text
                segment["pcm"] = None
            resolved_text = self._join_transcript_parts(resolved_text, text)
        ctx.confirmed_text = resolved_text

    @staticmethod
    def _join_transcript_parts(left: str, right: str) -> str:
        left = (left or "").rstrip()
        right = (right or "").lstrip()
        if not left:
            return right
        if not right:
            return left
        if right[0].isascii() and right[0].isalnum():
            return f"{left} {right}"
        return left + right

    @staticmethod
    def _common_text_prefix(texts: List[str]) -> str:
        if not texts:
            return ""
        prefix = texts[0]
        for text in texts[1:]:
            limit = min(len(prefix), len(text))
            index = 0
            while index < limit and prefix[index] == text[index]:
                index += 1
            prefix = prefix[:index]
            if not prefix:
                break
        return prefix

    def _stable_prefix(self, ctx: SessionContext) -> str:
        if len(ctx.hypothesis_history) < self._stable_hypothesis_count:
            return ""
        return self._common_text_prefix(list(ctx.hypothesis_history))

    def _maybe_commit_vad_boundary(self, ctx: SessionContext) -> None:
        stable_prefix = self._stable_prefix(ctx)
        if not stable_prefix or not ctx.current_partial:
            return

        processed_end = ctx.audio_base_offset + len(ctx.accumulated_pcm)
        while ctx.vad_boundaries:
            candidate = ctx.vad_boundaries[0]
            boundary_offset = int(candidate["offset"])
            candidate_text = str(candidate["text"])
            if not candidate_text or processed_end - boundary_offset < self._holdback_bytes:
                return
            if not stable_prefix.startswith(candidate_text) or not ctx.current_partial.startswith(candidate_text):
                return

            trim_bytes = boundary_offset - ctx.audio_base_offset
            if trim_bytes <= 0 or trim_bytes > len(ctx.accumulated_pcm):
                ctx.vad_boundaries.popleft()
                continue

            window_bytes = len(ctx.accumulated_pcm)
            old_inferred_bytes = ctx.last_inferred_pcm_len
            remaining_hypotheses = []
            for hypothesis in ctx.hypothesis_history:
                if hypothesis.startswith(candidate_text):
                    remaining_hypotheses.append(hypothesis[len(candidate_text):])
            if len(remaining_hypotheses) != len(ctx.hypothesis_history):
                return

            ctx.completed_segments.append({"pcm": None, "text": candidate_text})
            self._refresh_confirmed_text(ctx)
            del ctx.accumulated_pcm[:trim_bytes]
            ctx.audio_base_offset = boundary_offset
            ctx.last_inferred_pcm_len = max(0, old_inferred_bytes - trim_bytes)
            ctx.current_partial = ctx.current_partial[len(candidate_text):]
            ctx._raw_decoded = ctx.current_partial

            history_maxlen = ctx.hypothesis_history.maxlen or self._stable_hypothesis_count
            ctx.hypothesis_history = deque(remaining_hypotheses, maxlen=history_maxlen)
            ctx.vad_boundaries.popleft()
            rebased = deque()
            for later in ctx.vad_boundaries:
                text = str(later["text"])
                if later["offset"] > boundary_offset and text.startswith(candidate_text):
                    rebased.append({"offset": later["offset"], "text": text[len(candidate_text):]})
            ctx.vad_boundaries = rebased

            if ctx.segment_inference_count:
                print(
                    f"[ASR] Qwen stable prefix committed: text_chars={len(candidate_text)}, "
                    f"trimmed_audio={trim_bytes / (self.config.sample_rate * 2):.2f}s, "
                    f"window={window_bytes / (self.config.sample_rate * 2):.2f}s, "
                    f"inference={ctx.segment_inference_count}, "
                    f"total={ctx.segment_inference_seconds:.2f}s"
                )
            ctx.segment_inference_seconds = 0.0
            ctx.segment_inference_count = 0
            stable_prefix = self._common_text_prefix(remaining_hypotheses)
            processed_end = ctx.audio_base_offset + len(ctx.accumulated_pcm)

    def _feed_qwen_frame(self, ctx: SessionContext, frame: bytes) -> None:
        ctx.accumulated_pcm.extend(frame)
        ctx.buffer.extend(frame)

        previous_silence = ctx.trailing_silence_bytes
        is_speech = len(frame) == self._frame_bytes and self._vad.is_speech_bytes(frame)
        if is_speech:
            ctx.speech_seen = True
            ctx.trailing_silence_bytes = 0
            ctx.pause_candidate_pending = False
            ctx.pause_candidate_active = False
            ctx.last_speech_offset = ctx.audio_base_offset + len(ctx.accumulated_pcm)
        elif ctx.speech_seen:
            ctx.trailing_silence_bytes += len(frame)

        pause_reached = (
            ctx.speech_seen
            and not ctx.pause_candidate_active
            and not ctx.pause_candidate_pending
            and previous_silence < self._segment_silence_bytes <= ctx.trailing_silence_bytes
        )
        if pause_reached:
            ctx.pause_candidate_pending = True

        should_update = len(ctx.buffer) >= self.stride_bytes

        # Re-decode active PCM periodically. At a pause, force a hypothesis through
        # the pause boundary so its text can be tied to an exact VAD sample offset.
        # When draining, suppress speculative periodic decodes; finalization will decide.
        if not ctx.is_draining and (should_update or pause_reached):
            acquired = self._inference_lock.acquire(blocking=pause_reached)
            if acquired:
                try:
                    if len(ctx.accumulated_pcm) > ctx.last_inferred_pcm_len:
                        self._streaming_decode_step(ctx)
                    if (
                        ctx.pause_candidate_pending
                        and ctx.current_partial
                        and ctx.last_inferred_pcm_len == len(ctx.accumulated_pcm)
                    ):
                        ctx.vad_boundaries.append({
                            "offset": ctx.audio_base_offset + len(ctx.accumulated_pcm),
                            "text": ctx.current_partial,
                        })
                        ctx.pause_candidate_pending = False
                        ctx.pause_candidate_active = True
                finally:
                    self._inference_lock.release()

        self._maybe_commit_vad_boundary(ctx)

    def _feed_qwen_pcm(self, ctx: SessionContext, pcm_bytes: bytes) -> None:
        """Split one original PCM chunk into frames and feed the shared resident path."""
        for offset in range(0, len(pcm_bytes), self._frame_bytes):
            self._feed_qwen_frame(ctx, pcm_bytes[offset : offset + self._frame_bytes])

    def _drain_pending_pcm(self, ctx: SessionContext) -> None:
        """Replay buffered PCM chunks in arrival order once the model is GPU-ready."""
        while ctx.pending_pcm_chunks:
            pcm_bytes = ctx.pending_pcm_chunks.popleft()
            self._feed_qwen_pcm(ctx, pcm_bytes)

    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        """Queue during GPU activation, then use the same ordered frame path in either mode."""
        with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            return ("", "")

        with ctx.lock:
            if not ctx.is_active:
                return ("", "")

            if not pcm_bytes:
                if self._is_on_demand_gpu() and self._on_demand_gpu_ready():
                    self._drain_pending_pcm(ctx)
                return (ctx.confirmed_text, ctx.current_partial or ("正在说话..." if not ctx.confirmed_text else ""))

            pcm_bytes = bytes(pcm_bytes)
            ctx.total_pcm_len += len(pcm_bytes)

            # Keep activation-time audio separate from decoder/VAD state. Once the GPU
            # is ready, drain it before accepting this newer chunk.
            if self._is_on_demand_gpu() and not self._on_demand_gpu_ready():
                ctx.pending_pcm_chunks.append(pcm_bytes)
                return (ctx.confirmed_text, ctx.current_partial)

            if self._is_on_demand_gpu():
                self._drain_pending_pcm(ctx)
            self._feed_qwen_pcm(ctx, pcm_bytes)

            return (ctx.confirmed_text, ctx.current_partial or ("正在说话..." if not ctx.confirmed_text else ""))

    def finalize_session(self, session_id: str) -> str:
        """Replay any activation-time audio, then finalize the active bounded segment."""
        with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            return ""

        with ctx.lock:
            if not ctx.is_active:
                return ""
            # This is the atomic input cutoff: later feed/cancel calls cannot consume
            # or append PCM while finalization waits for model residency.
            ctx.is_active = False
            try:
                if self._is_on_demand_gpu() and ctx.total_pcm_len:
                    self._ensure_gpu_ready()
                    if not self._on_demand_gpu_ready():
                        raise RuntimeError("语音模型尚未进入目标 GPU，无法完成识别")

                # Replay buffered chunks before minimum-duration checks and final decoding,
                # matching any resident-mode partial/segment boundaries already reached.
                self._drain_pending_pcm(ctx)
                min_bytes = int(self.config.sample_rate * 2 * self.config.min_recording_seconds)
                if ctx.total_pcm_len < min_bytes:
                    return self._join_transcript_parts(ctx.confirmed_text, ctx.current_partial)

                self._resolve_deferred_segments(ctx)

                # Reuse the current partial if it already covers all accepted audio,
                # or if all audio accumulated after the last decode is silence.
                needs_decode = False
                if len(ctx.accumulated_pcm) > ctx.last_inferred_pcm_len:
                    can_reuse_partial = (
                        ctx.last_inferred_pcm_len > 0
                        and bool(ctx.current_partial)
                        and not ctx.pause_candidate_pending
                        and ctx.last_speech_offset <= ctx.last_inferred_audio_offset
                    )
                    if not can_reuse_partial:
                        needs_decode = True

                with self._inference_lock:
                    if needs_decode:
                        self._streaming_decode_step(ctx)
                self._maybe_commit_vad_boundary(ctx)

                if ctx.accumulated_pcm and ctx.segment_inference_count:
                    audio_seconds = len(ctx.accumulated_pcm) / (self.config.sample_rate * 2)
                    print(
                        f"[ASR] Qwen segment finished: audio={audio_seconds:.2f}s, "
                        f"inference={ctx.segment_inference_count}, "
                        f"total={ctx.segment_inference_seconds:.2f}s"
                    )
                return self._join_transcript_parts(ctx.confirmed_text, ctx.current_partial)
            finally:
                ctx.pending_pcm_chunks.clear()
                with self._resource_cond:
                    if self._sessions.get(session_id) is ctx:
                        self._sessions.pop(session_id, None)
                    self._active_sessions.discard(session_id)
                    if not self._active_sessions and self._is_on_demand_gpu():
                        self._gpu_ready.clear()
                    self._resource_cond.notify_all()

    def begin_drain(self, session_id: str) -> None:
        """标记会话进入排空阶段，抑制中间投机性推理"""
        with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is not None:
            ctx.is_draining = True

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            with self._resource_cond:
                self._active_sessions.discard(session_id)
                if not self._active_sessions and self._is_on_demand_gpu():
                    self._gpu_ready.clear()
                self._resource_cond.notify_all()
            return

        with ctx.lock:
            if not ctx.is_active:
                return
            ctx.is_active = False
            ctx.pending_pcm_chunks.clear()
            with self._resource_cond:
                if self._sessions.get(session_id) is ctx:
                    self._sessions.pop(session_id, None)
                self._active_sessions.discard(session_id)
                if not self._active_sessions and self._is_on_demand_gpu():
                    self._gpu_ready.clear()
                self._resource_cond.notify_all()


# 兼容类名别名
Qwen2PassEngine = QwenASREngine
QwenOfflineEngine = QwenASREngine


class SenseVoiceOfflineEngine(BaseStreamingASR):
    """SenseVoice 单句离线保底引擎 (轻量极速，纯 CPU)"""

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._is_loaded = False
        self._recognizer = None
        self._fallback_engine: Optional[BaseStreamingASR] = None
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionContext] = {}

    @property
    def is_loaded(self) -> bool:
        if self._fallback_engine is not None:
            return self._fallback_engine.is_loaded
        return self._is_loaded

    def get_capabilities(self) -> Dict[str, Any]:
        if self._fallback_engine is not None:
            return self._fallback_engine.get_capabilities()
        return {
            "engine": "sensevoice_offline",
            "is_loaded": self._is_loaded,
            "streaming_available": False,
            "offline_available": True,
            "punc_available": True,
            "device": "cpu",
        }

    def load(self) -> None:
        with self._lock:
            if self._is_loaded:
                return

            has_sherpa = False
            try:
                import sherpa_onnx
                has_sherpa = True
            except ImportError:
                has_sherpa = False

            if has_sherpa and self.config.is_sensevoice_installed():
                try:
                    self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=str(self.config.sensevoice_model_path),
                        tokens=str(self.config.sensevoice_tokens_path),
                        num_threads=self.config.num_threads,
                        use_itn=self.config.use_itn,
                        language=self.config.language,
                        debug=False,
                    )
                    self._is_loaded = True
                    return
                except Exception as ex:
                    print(f"[!] sherpa-onnx 加载 SenseVoice 异常: {ex}")

            if not has_sherpa:
                raise RuntimeError("SenseVoice 依赖缺失 (未安装 sherpa-onnx，请运行 setup_voice.bat 安装)")
            raise FileNotFoundError(f"SenseVoice 离线模型文件不存在 (请运行 setup_voice.bat 下载): {self.config.sensevoice_model_path}")

    def create_session(self, session_id: str) -> SessionContext:
        if self._fallback_engine is not None:
            return self._fallback_engine.create_session(session_id)
        with self._lock:
            ctx = SessionContext(session_id=session_id)
            self._sessions[session_id] = ctx
            return ctx

    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        if self._fallback_engine is not None:
            return self._fallback_engine.feed_chunk(session_id, pcm_bytes)
        ctx = self._sessions.get(session_id)
        if ctx is not None and ctx.is_active:
            ctx.accumulated_pcm.extend(pcm_bytes)
        return ("", "正在说话...")

    def finalize_session(self, session_id: str) -> str:
        if self._fallback_engine is not None:
            return self._fallback_engine.finalize_session(session_id)
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
        if ctx is None or len(ctx.accumulated_pcm) == 0:
            return ""

        raw_bytes = bytes(ctx.accumulated_pcm)
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        if hasattr(self._recognizer, "create_stream"):
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self.config.sample_rate, samples)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()
        elif hasattr(self._recognizer, "generate"):
            res = self._recognizer.generate(input=raw_bytes)
            if res and len(res) > 0:
                return res[0].get("text", "").strip()
        return ""

    def cancel_session(self, session_id: str) -> None:
        if self._fallback_engine is not None:
            self._fallback_engine.cancel_session(session_id)
            return
        with self._lock:
            self._sessions.pop(session_id, None)


class Sherpa2PassEngine(BaseStreamingASR):
    """基于 sherpa-onnx 的统一 2-Pass 流式引擎：
    - Pass 1 (在线流式 Partial): 使用 sherpa-onnx Streaming Zipformer ONNX 模型
    - Pass 2 (离线全局终审 Final): 句末使用 SenseVoice-Small INT8 执行全音频离线终审
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._is_loaded = False
        self._online_recognizer = None
        self._offline_recognizer = None
        self._fallback_engine: Optional[BaseStreamingASR] = None
        self._lock = threading.Lock()
        self._inference_lock = threading.RLock()
        self._sessions: Dict[str, SessionContext] = {}

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "engine": "sherpa_2pass",
            "is_loaded": self._is_loaded,
            "streaming_available": bool(self._online_recognizer is not None),
            "offline_available": bool(self._offline_recognizer is not None),
            "punc_available": True,
            "device": "cpu",
        }

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def load(self) -> None:
        with self._lock:
            if self._is_loaded:
                return

            try:
                import sherpa_onnx
            except ImportError as e:
                raise RuntimeError("sherpa-onnx 未安装，请在专属虚拟环境中安装 requirements.txt") from e

            # 1. 加载 SenseVoice-Small 离线模型 (用于 Pass 2 终审与标点)
            if self.config.is_sensevoice_installed():
                try:
                    self._offline_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=str(self.config.sensevoice_model_path),
                        tokens=str(self.config.sensevoice_tokens_path),
                        num_threads=self.config.num_threads,
                        use_itn=self.config.use_itn,
                        language=self.config.language,
                        debug=False,
                    )
                except Exception as e:
                    print(f"[ASR] 加载 SenseVoice 离线模型失败: {e}")

            # 2. 尝试加载流式模型 (用于 Pass 1 实时流式出字)
            if hasattr(self.config, "is_streaming_model_installed") and self.config.is_streaming_model_installed():
                d = self.config.streaming_model_dir
                encoder = next(d.glob("encoder*.onnx"), None)
                decoder = next(d.glob("decoder*.onnx"), None)
                joiner = next(d.glob("joiner*.onnx"), None)
                tokens = d / "tokens.txt"
                if encoder and decoder and joiner and tokens.is_file():
                    try:
                        self._online_recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                            encoder=str(encoder),
                            decoder=str(decoder),
                            joiner=str(joiner),
                            tokens=str(tokens),
                            num_threads=self.config.num_threads,
                            sample_rate=self.config.sample_rate,
                            feature_dim=80,
                            decoding_method="greedy_search",
                            debug=False,
                        )
                    except Exception as e:
                        print(f"[ASR] 加载 Zipformer 在线流式模型失败: {e}")

            if self._offline_recognizer is None and self._online_recognizer is None:
                raise FileNotFoundError(
                    f"未找到可用的 ASR 模型文件！SenseVoice: {self.config.sensevoice_model_path}, Zipformer: {self.config.streaming_model_dir}"
                )

            self._is_loaded = True

    def create_session(self, session_id: str) -> SessionContext:
        with self._lock:
            ctx = SessionContext(session_id=session_id)
            if self._online_recognizer is not None:
                try:
                    ctx.online_stream = self._online_recognizer.create_stream()
                except Exception:
                    ctx.online_stream = None
            self._sessions[session_id] = ctx
            return ctx

    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        ctx = self._sessions.get(session_id)
        if ctx is None or not ctx.is_active or len(pcm_bytes) == 0:
            return ("", "")

        ctx.accumulated_pcm.extend(pcm_bytes)

        if self._online_recognizer is not None and ctx.online_stream is not None:
            try:
                samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                ctx.online_stream.accept_waveform(self.config.sample_rate, samples)
                with self._inference_lock:
                    while self._online_recognizer.is_ready(ctx.online_stream):
                        self._online_recognizer.decode_stream(ctx.online_stream)
                text = self._online_recognizer.get_result(ctx.online_stream).text.strip()
                if text:
                    ctx.current_partial = text
            except Exception:
                pass
            return (ctx.confirmed_text, ctx.current_partial)
        else:
            return ("", "正在说话...")

    def finalize_session(self, session_id: str) -> str:
        with self._lock:
            ctx = self._sessions.pop(session_id, None)

        if ctx is None:
            return ""

        ctx.is_active = False
        full_pcm = bytes(ctx.accumulated_pcm)
        if len(full_pcm) < int(self.config.sample_rate * 2 * self.config.min_recording_seconds):
            return ctx.current_partial or ctx.confirmed_text

        if self._offline_recognizer is not None:
            try:
                samples = np.frombuffer(full_pcm, dtype=np.int16).astype(np.float32) / 32768.0
                stream = self._offline_recognizer.create_stream()
                stream.accept_waveform(self.config.sample_rate, samples)
                with self._inference_lock:
                    self._offline_recognizer.decode_stream(stream)
                final_text = stream.result.text.strip()
                if final_text:
                    return final_text
            except Exception as e:
                print(f"[ASR] Pass 2 离线终审异常: {e}")

        if self._online_recognizer is not None and ctx.online_stream is not None:
            try:
                return self._online_recognizer.get_result(ctx.online_stream).text.strip()
            except Exception:
                pass

        return ctx.current_partial or ctx.confirmed_text

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
            if ctx is not None:
                ctx.is_active = False
                ctx.cache.clear()
                ctx.online_stream = None


def create_engine(config: VoiceConfig) -> BaseStreamingASR:
    """引擎工厂方法：
    - "qwen_2pass" / "qwen_asr": Qwen ASR 1.7B 原生流式分块高精度引擎 (无第三方拼接)
    - "sensevoice_offline": 离线单 Pass 极速引擎 (轻量纯 CPU)
    - 向后兼容别名: "paraformer_streaming_2pass", "qwen_offline" 均映射至 QwenASREngine
    """
    if config.engine in ("qwen_2pass", "qwen_asr", "qwen_offline", "paraformer_streaming_2pass"):
        return QwenASREngine(config)
    elif config.engine == "sensevoice_offline":
        return SenseVoiceOfflineEngine(config)
    elif config.engine == "sherpa_2pass":
        return Sherpa2PassEngine(config)
    else:
        return QwenASREngine(config)
