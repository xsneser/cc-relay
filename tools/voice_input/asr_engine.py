"""ASR 引擎抽象与识别实现：
- BaseStreamingASR 统一接口
- QwenASREngine (Qwen3-ASR-1.7B 原生流式分块 + 全音频终审高精引擎，零异构依赖)
- SenseVoiceOfflineEngine (轻量极速 SenseVoice ONNX 单 Pass 引擎)
- Sherpa2PassEngine (sherpa-onnx 统一轻量 2-Pass 引擎)
- 严格的 Session Cache 生命周期隔离与防串流机制
"""

import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import VoiceConfig


@dataclass
class SessionContext:
    session_id: str
    cache: Dict[str, Any] = field(default_factory=dict)
    confirmed_text: str = ""
    current_partial: str = ""
    accumulated_pcm: bytearray = field(default_factory=bytearray)
    chunk_buffer: bytearray = field(default_factory=bytearray)
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
    """基于 Qwen3-ASR-1.7B 的原生语音识别引擎：
    - 采用阿里巴巴开源 Qwen3-ASR-1.7B 大模型，支持 52+ 多语种与高精度代码、专有名词理解
    - 原生流式分块出字 (Chunk-based Streaming)：录音期间基于动态音频窗口增量吐字 (Partial)
    - 句末终审 (Final)：录音结束时对完整音频执行全句高精解码与标点生成
    - 纯血 Qwen，完全独立，无任何第三方异构流式模型拼接 (零 Zipformer 依赖)
    - 自动检测 GPU (CUDA) 加速与 CPU 线程控制
    - 单会话独占缓冲区与隔离
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._is_loaded = False
        self._pipe = None
        self._model = None
        self._processor = None
        self._resolved_device = "cpu"
        self._lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._sessions: Dict[str, SessionContext] = {}

        # 流式增量出字步长: 800ms = 12800 samples = 25600 bytes
        self.stride_bytes = int(self.config.sample_rate * 2 * 0.8)
        self._last_partial_time: Dict[str, float] = {}

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "engine": self.config.engine or "qwen_asr",
            "is_loaded": self._is_loaded,
            "streaming_available": True,
            "offline_available": True,
            "punc_available": True,
            "device": self._resolved_device,
        }

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def load(self) -> None:
        """载入 Qwen ASR 1.7B 模型并执行单帧 Warm-up"""
        with self._lock:
            if self._is_loaded:
                return

            t0 = time.monotonic()
            # 限制 CPU 线程数防满载卡顿
            threads = max(1, min(int(self.config.num_threads or 2), os.cpu_count() or 2))
            os.environ.setdefault("OMP_NUM_THREADS", str(threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(threads))

            try:
                import torch
                torch.set_num_threads(threads)
            except Exception:
                torch = None

            model_path = _find_local_qwen_dir(self.config.qwen_model_id) or self.config.qwen_model_id

            # 设备与精度解析
            cfg_dev = str(self.config.qwen_device or "auto").lower()
            if cfg_dev == "cuda" or (cfg_dev == "auto" and torch is not None and torch.cuda.is_available()):
                self._resolved_device = "cuda"
            else:
                self._resolved_device = "cpu"

            try:
                from transformers import pipeline
                torch_dtype = None
                if torch is not None:
                    cfg_dtype = str(self.config.qwen_torch_dtype or "auto").lower()
                    if cfg_dtype == "bfloat16" and hasattr(torch, "bfloat16"):
                        torch_dtype = torch.bfloat16
                    elif cfg_dtype == "float16":
                        torch_dtype = torch.float16
                    elif cfg_dtype == "float32":
                        torch_dtype = torch.float32
                    elif self._resolved_device == "cuda" and hasattr(torch, "bfloat16"):
                        torch_dtype = torch.bfloat16

                pipe_kwargs = {
                    "task": "automatic-speech-recognition",
                    "model": model_path,
                    "device": self._resolved_device,
                }
                if torch_dtype is not None:
                    pipe_kwargs["torch_dtype"] = torch_dtype

                self._pipe = pipeline(**pipe_kwargs)
            except Exception as ex:
                # 尝试通过 AutoModelForSpeechSeq2Seq / AutoProcessor 加载
                try:
                    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
                    self._processor = AutoProcessor.from_pretrained(model_path)
                    self._model = AutoModelForSpeechSeq2Seq.from_pretrained(model_path)
                    if self._resolved_device == "cuda" and torch is not None:
                        self._model = self._model.cuda()
                except Exception as ex2:
                    raise RuntimeError(f"Qwen ASR 1.7B 模型载入失败 (模型路径: {model_path}): {ex}; {ex2}")

            # 1 帧零采样 Warm-up
            try:
                dummy_pcm = np.zeros(1600, dtype=np.float32)
                self._transcribe_samples(dummy_pcm)
            except Exception:
                pass

            self._is_loaded = True
            cost = (time.monotonic() - t0) * 1000
            print(f"[ASR] [OK] Qwen ASR 1.7B 引擎初始化就绪 (耗时: {cost:.1f}ms, device: {self._resolved_device})")

    def _transcribe_samples(self, samples: np.ndarray) -> str:
        with self._inference_lock:
            return self._transcribe_samples_unlocked(samples)

    def _transcribe_samples_unlocked(self, samples: np.ndarray) -> str:
        if self._pipe is not None:
            res = self._pipe({"raw": samples, "sampling_rate": self.config.sample_rate})
            if isinstance(res, dict):
                return res.get("text", "").strip()
            elif isinstance(res, list) and len(res) > 0 and isinstance(res[0], dict):
                return res[0].get("text", "").strip()
            return str(res).strip()
        elif self._model is not None and self._processor is not None:
            import torch
            inputs = self._processor(samples, sampling_rate=self.config.sample_rate, return_tensors="pt")
            if self._resolved_device == "cuda":
                inputs = {k: v.cuda() for k, v in inputs.items()}
            with torch.no_grad():
                gen_ids = self._model.generate(**inputs)
            return self._processor.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
        return ""

    def create_session(self, session_id: str) -> SessionContext:
        with self._lock:
            ctx = SessionContext(session_id=session_id)
            self._sessions[session_id] = ctx
            self._last_partial_time[session_id] = time.monotonic()
            return ctx

    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        """送入 PCM 数据块：累积数据并以非阻塞方式触发 Qwen 自身分块实时出字"""
        ctx = self._sessions.get(session_id)
        if ctx is None or not ctx.is_active:
            return ("", "")
        if len(pcm_bytes) == 0:
            return ("", "正在说话...")

        ctx.accumulated_pcm.extend(pcm_bytes)
        ctx.chunk_buffer.extend(pcm_bytes)

        # 录音时长超过 0.5s 且自上次出字经过了 stride_bytes (约 0.8s) 时，尝试流式输出
        now = time.monotonic()
        last_t = self._last_partial_time.get(session_id, 0.0)
        min_bytes = int(self.config.sample_rate * 2 * 0.5)

        if len(ctx.accumulated_pcm) >= min_bytes and len(ctx.chunk_buffer) >= self.stride_bytes and (now - last_t >= 0.5):
            ctx.chunk_buffer.clear()
            self._last_partial_time[session_id] = now
            # 非阻塞尝试推演，不阻塞实时音频采集线程
            if self._inference_lock.acquire(blocking=False):
                try:
                    raw_bytes = bytes(ctx.accumulated_pcm)
                    samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    partial_text = self._transcribe_samples_unlocked(samples)
                    if partial_text:
                        ctx.current_partial = partial_text
                except Exception:
                    pass
                finally:
                    self._inference_lock.release()

        return (ctx.confirmed_text, ctx.current_partial or "正在说话...")

    def finalize_session(self, session_id: str) -> str:
        """收尾：全音频由 Qwen 执行高精度最终解码与标点输出"""
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
            self._last_partial_time.pop(session_id, None)

        if ctx is None or len(ctx.accumulated_pcm) == 0:
            return ""

        ctx.is_active = False
        raw_bytes = bytes(ctx.accumulated_pcm)
        if len(raw_bytes) < int(self.config.sample_rate * 2 * self.config.min_recording_seconds):
            return ctx.current_partial or ""

        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            final_text = self._transcribe_samples(samples)
            if final_text:
                return final_text
        except Exception as e:
            print(f"[ASR] Qwen 终审异常: {e}")

        return ctx.current_partial or ""

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._last_partial_time.pop(session_id, None)


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
        self._inference_lock = threading.Lock()
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
