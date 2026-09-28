"""ASR 引擎抽象与 2-Pass 流式因果识别实现：
- BaseStreamingASR 统一接口
- FunASR 2-Pass 引擎 (在线增量 Partial + 离线全局纠错 Final + 标点恢复)
- SenseVoice 离线保底引擎 (SenseVoiceOfflineEngine)
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
        """结束会话：is_final=True 尾部提取 -> 2-pass 离线重算与标点 -> 销毁 Cache"""
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


def _find_local_model_dir(model_id: str) -> Optional[str]:
    """优先查找用户目录下的模型缓存，若本地不存在则返回 None (绝不隐式联网下载)"""
    user_cache = Path(os.path.expanduser("~/.cache/modelscope/hub/models")) / model_id
    repo_models = Path(__file__).resolve().parent / "models" / model_id
    candidates = [user_cache, repo_models]
    for c in candidates:
        if c.is_dir() and ((c / "model.pt").is_file() or (c / "model.onnx").is_file()):
            return str(c.resolve())
    return None


class FunASR2PassEngine(BaseStreamingASR):
    """基于 FunASR 的 2-Pass 流式因果引擎：
    1. Online: 使用 Streaming Paraformer 进行增量前向推理，吐出 Partial 临时结果
    2. Offline: 句末使用 Offline Paraformer 或 SenseVoice 做上下文整句校正
    3. Punctuation: 调用 CT-Transformer 恢复中文标点
    4. Session Cache 隔离: 遵循官方规范，单会话独占 cache，完毕即销毁
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._is_loaded = False
        self._fallback_engine: Optional[BaseStreamingASR] = None
        self._lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._sessions: Dict[str, SessionContext] = {}

        # 内部模型对象
        self._streaming_model = None
        self._offline_model = None
        self._punc_model = None

        # 步长设置: 默认 600ms = 9600 samples = 19200 bytes
        self.stride_bytes = int(self.config.sample_rate * 2 * 0.6)

    def get_capabilities(self) -> Dict[str, Any]:
        if self._fallback_engine is not None:
            return self._fallback_engine.get_capabilities()
        return {
            "engine": "paraformer_streaming_2pass",
            "is_loaded": self._is_loaded,
            "streaming_available": bool(
                self._streaming_model is not None and self._streaming_model is not self._offline_model
            ),
            "offline_available": bool(self._offline_model is not None),
            "punc_available": bool(self._offline_model is not None),
            "device": self.config.device,
        }

    @property
    def is_loaded(self) -> bool:
        if self._fallback_engine is not None:
            return self._fallback_engine.is_loaded
        return self._is_loaded

    def load(self) -> None:
        """加载模型常驻内存"""
        with self._lock:
            if self._is_loaded:
                return

            # 严格限制 PyTorch / OpenMP CPU 运算线程数，防止模型加载与 warm-up 满载导致系统卡顿
            threads = max(1, min(int(self.config.num_threads or 2), os.cpu_count() or 2))
            os.environ.setdefault("OMP_NUM_THREADS", str(threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(threads))
            try:
                import torch
                torch.set_num_threads(threads)
                if hasattr(torch, "set_num_interop_threads"):
                    try:
                        torch.set_num_interop_threads(max(1, min(2, threads)))
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                from funasr import AutoModel
            except ImportError as e:
                raise RuntimeError("funasr 库未安装，请在 Python 环境中安装 funasr") from e

            t0 = time.monotonic()
            device = self.config.device

            # 解析本地模型路径 (绝对保证仅使用本地缓存，绝不触发联网)
            offline_id = _find_local_model_dir("iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
            if not offline_id:
                local_dir = self.config.models_dir / "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
                if local_dir.is_dir() and (local_dir / "model.pt").is_file():
                    offline_id = str(local_dir.resolve())
                else:
                    raise FileNotFoundError("未在本地找到 Paraformer 模型文件，请检查模型目录")

            vad_id = _find_local_model_dir("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
            punc_id = _find_local_model_dir("iic/punc_ct-transformer_cn-en-common-vocab471067-large")

            # 载入离线纠错与标点模型
            try:
                self._offline_model = AutoModel(
                    model=offline_id,
                    vad_model=vad_id,
                    punc_model=punc_id,
                    device=device,
                    disable_update=True,
                )
            except Exception:
                # 尝试纯离线模型 (无附加组件)
                self._offline_model = AutoModel(model=offline_id, device=device, disable_update=True)

            # 流式模型检查: 仅当本地明确存在在线模型时才加载，否则无缝复用离线模型 (绝不隐式联网)
            online_id = _find_local_model_dir("iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online")
            if online_id:
                try:
                    self._streaming_model = AutoModel(
                        model=online_id,
                        device=device,
                        disable_update=True,
                    )
                except Exception:
                    self._streaming_model = self._offline_model
            else:
                self._streaming_model = self._offline_model

            # Warm-up: 1 帧静音推理，消除冷启动 JIT 延迟
            try:
                dummy_pcm = np.zeros(16000, dtype=np.int16).tobytes()
                _ = self._offline_model.generate(input=dummy_pcm, batch_size_s=300)
            except Exception:
                pass

            self._is_loaded = True
            cost = (time.monotonic() - t0) * 1000
            print(f"[ASR] [OK] FunASR 2-Pass 引擎初始化就绪 (耗时: {cost:.1f}ms, device: {device})")

    def create_session(self, session_id: str) -> SessionContext:
        with self._lock:
            ctx = SessionContext(session_id=session_id)
            self._sessions[session_id] = ctx
            return ctx

    def feed_chunk(self, session_id: str, pcm_bytes: bytes) -> Tuple[str, str]:
        """将音频字节累加进会话并在满足 stride 时触发流式前向推断"""
        ctx = self._sessions.get(session_id)
        if ctx is None or not ctx.is_active:
            return ("", "")

        ctx.accumulated_pcm.extend(pcm_bytes)
        ctx.chunk_buffer.extend(pcm_bytes)

        # 未达到单次 inference 步长，保持当前 partial
        if len(ctx.chunk_buffer) < self.stride_bytes:
            return (ctx.confirmed_text, ctx.current_partial)

        # 满足 stride, 取出数据送入流式推理
        chunk_to_infer = bytes(ctx.chunk_buffer)
        ctx.chunk_buffer.clear()
        ctx.seq += 1

        try:
            with self._inference_lock:
                if self._streaming_model is not self._offline_model and hasattr(self._streaming_model, "generate"):
                    res = self._streaming_model.generate(
                        input=chunk_to_infer,
                        cache=ctx.cache,
                        is_final=False,
                        chunk_size=[0, 10, 5],
                    )
                    if res and len(res) > 0:
                        text = res[0].get("text", "").strip()
                        if text:
                            ctx.current_partial = text
                elif hasattr(self._offline_model, "generate"):
                    res = self._offline_model.generate(input=bytes(ctx.accumulated_pcm), batch_size_s=300)
                    if res and len(res) > 0:
                        text = res[0].get("text", "").strip()
                        if text:
                            ctx.current_partial = text
        except Exception:
            pass

        return (ctx.confirmed_text, ctx.current_partial)

    def finalize_session(self, session_id: str) -> str:
        """收尾：提取尾部文字，执行 2-pass 全局离线纠错与标点恢复，销毁 Cache"""
        with self._lock:
            ctx = self._sessions.pop(session_id, None)

        if ctx is None:
            return ""

        ctx.is_active = False

        # 1. 尾部刷新 (is_final=True)
        try:
            with self._inference_lock:
                if hasattr(self._streaming_model, "generate") and ctx.cache:
                    _ = self._streaming_model.generate(
                        input=bytes(ctx.chunk_buffer),
                        cache=ctx.cache,
                        is_final=True,
                    )
        except Exception:
            pass
        finally:
            # 严格销毁 cache，释放会话资源
            ctx.cache.clear()

        # 2. 2-Pass 离线纠错与标点
        final_text = ""
        full_pcm = bytes(ctx.accumulated_pcm)
        if len(full_pcm) >= int(self.config.sample_rate * 2 * self.config.min_recording_seconds):
            try:
                with self._inference_lock:
                    res = self._offline_model.generate(input=full_pcm, batch_size_s=300)
                if res and len(res) > 0:
                    final_text = res[0].get("text", "").strip()
            except Exception as e:
                print(f"[ASR] 2-Pass 离线纠错异常: {e}")
                final_text = ctx.current_partial or ctx.confirmed_text
        else:
            final_text = ctx.current_partial or ctx.confirmed_text

        return final_text

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
            if ctx is not None:
                ctx.is_active = False
                ctx.cache.clear()


class SenseVoiceOfflineEngine(BaseStreamingASR):
    """SenseVoice 单句离线保底引擎 (兼容原有极速轻量模式，内置 FunASR 2-Pass 平滑回退)"""

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
            "offline_available": self._is_loaded,
            "punc_available": True,
            "device": self.config.device,
        }

    def load(self) -> None:
        with self._lock:
            if self._is_loaded:
                return

            # 1. 优先尝试使用 sherpa_onnx 运行 SenseVoice ONNX
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

            # 2. 智能平滑回退：若本地已具备 FunASR 且存在已缓存的 Paraformer 2-Pass 模型
            try:
                if _find_local_model_dir("iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"):
                    import funasr
                    print("[*] SenseVoice (sherpa-onnx) 未就绪，检测到本地已安装 FunASR 2-Pass 模型，自动平滑切换至 Paraformer 引擎！")
                    self._fallback_engine = FunASR2PassEngine(self.config)
                    self._fallback_engine.load()
                    self._is_loaded = True
                    return
            except Exception:
                pass

            # 3. 尝试使用 FunASR 直接加载 SenseVoice
            try:
                from funasr import AutoModel
                self._recognizer = AutoModel(model="SenseVoiceSmall", device=self.config.device, disable_update=True)
                self._is_loaded = True
                return
            except Exception:
                pass

            # 4. 若两者均不可用，给出清晰指引
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

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


def create_engine(config: VoiceConfig) -> BaseStreamingASR:
    """引擎工厂方法"""
    if config.engine == "paraformer_streaming_2pass":
        return FunASR2PassEngine(config)
    elif config.engine == "sensevoice_offline":
        return SenseVoiceOfflineEngine(config)
    else:
        # 默认使用 2-pass
        return FunASR2PassEngine(config)
