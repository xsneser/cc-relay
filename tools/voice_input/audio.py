"""音频采集与切片模块：支持 20ms Frame、WebRTC VAD、Ring Buffer Pre-roll 与实时流式分块"""

import collections
import queue
import threading
import time
from typing import Callable, Deque, Generator, List, Optional, Tuple

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None


class VADDetector:
    """WebRTC VAD 包装器，支持备用能量检测 (Energy-based fallback)"""

    def __init__(self, mode: int = 2, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.mode = mode
        self._vad = None
        if webrtcvad is not None:
            try:
                self._vad = webrtcvad.Vad(mode)
            except Exception:
                self._vad = None

    def is_speech_bytes(self, pcm_bytes: bytes) -> bool:
        """输入 16-bit Mono 16kHz PCM 字节，长度必须为 10/20/30ms"""
        if self._vad is not None:
            try:
                return self._vad.is_speech(pcm_bytes, self.sample_rate)
            except Exception:
                pass
        # 降级：简易能量检测 (RMS 阈值)
        if len(pcm_bytes) >= 2:
            data = np.frombuffer(pcm_bytes, dtype=np.int16)
            rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
            return bool(rms > 400.0)
        return False

    def compute_audio_level(self, pcm_bytes: bytes) -> float:
        """计算音频归一化 RMS 音量 (0.0 ~ 1.0)，用于 UI 动效展示"""
        if len(pcm_bytes) < 2:
            return 0.0
        data = np.frombuffer(pcm_bytes, dtype=np.int16)
        rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
        # 常见语音 RMS 范围约 200~8000
        level = min(1.0, float(rms) / 5000.0)
        return level


class AudioRecorder:
    """麦克风音频流式录制器：
    - 采集 16,000Hz 单声道 16-bit PCM
    - 20ms 基础音频帧 (320 samples / 640 bytes)
    - Ring Buffer Pre-roll (约 240ms) 防止截断开头首音节
    - WebRTC VAD 实时语音检测与静音超时自动截断 (Silence Auto-Stop)
    - 提供流式 chunk 获取与松键后整段音频汇总 (供 2-pass 终态纠错)
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        pre_roll_ms: int = 240,
        max_duration: float = 30.0,
        vad_mode: int = 2,
        silence_timeout_seconds: float = 1.2,
        device: Optional[int] = None,
        on_silence_timeout: Optional[Callable[[], None]] = None,
        on_audio_level: Optional[Callable[[float], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self.frame_samples = int(sample_rate * frame_duration_ms / 1000)  # 320
        self.frame_bytes = self.frame_samples * 2  # 640 bytes for int16
        self.max_duration = max_duration
        self.silence_timeout_seconds = silence_timeout_seconds
        self.device = device
        self.on_silence_timeout = on_silence_timeout
        self.on_audio_level = on_audio_level

        self.vad = VADDetector(mode=vad_mode, sample_rate=sample_rate)

        # Pre-roll ring buffer: 保存触发前最近的若干帧
        self.pre_roll_frames_count = max(1, pre_roll_ms // frame_duration_ms)
        self._pre_roll_buffer: Deque[bytes] = collections.deque(maxlen=self.pre_roll_frames_count)

        self._stream_queue: queue.Queue = queue.Queue()
        self._all_pcm_chunks: List[bytes] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        self._start_time: float = 0.0
        self._lock = threading.Lock()

        # VAD 状态跟踪
        self.speech_detected = False
        self._last_speech_time: float = 0.0
        self._trailing_silence_seconds: float = 0.0

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time_info, status):
        """sounddevice 回调函数：将接收到的 float32 音频转换为 int16 PCM 字节"""
        if not self._recording:
            return

        # indata 形状为 (frames, 1)，类型 float32
        scaled = np.clip(indata[:, 0], -1.0, 1.0) * 32767.0
        pcm_bytes = scaled.astype(np.int16).tobytes()

        # 计算并派发音量电平
        if self.on_audio_level is not None:
            lvl = self.vad.compute_audio_level(pcm_bytes)
            try:
                self.on_audio_level(lvl)
            except Exception:
                pass

        # 分割为 20ms 基础帧入队列
        step = self.frame_bytes
        now = time.monotonic()

        for i in range(0, len(pcm_bytes), step):
            frame = pcm_bytes[i : i + step]
            if len(frame) == step:
                # 检查 VAD 人声
                is_speech = self.vad.is_speech_bytes(frame)
                if is_speech:
                    self.speech_detected = True
                    self._last_speech_time = now

                # 如果已经检测到说话，检查静音是否超时
                if self.speech_detected and self.silence_timeout_seconds > 0:
                    silence_duration = now - self._last_speech_time
                    if silence_duration >= self.silence_timeout_seconds:
                        if self.on_silence_timeout is not None:
                            try:
                                self.on_silence_timeout()
                            except Exception:
                                pass

                self._stream_queue.put(frame)
                self._all_pcm_chunks.append(frame)

    def start(self) -> None:
        """开始录音，将 pre-roll 缓存打入流队列头部"""
        if sd is None:
            raise RuntimeError("sounddevice 未安装，请安装相关依赖")

        with self._lock:
            if self._recording:
                return

            # 清空队列与历史块
            while not self._stream_queue.empty():
                try:
                    self._stream_queue.get_nowait()
                except queue.Empty:
                    break
            self._all_pcm_chunks.clear()

            # 重置 VAD 跟踪
            self.speech_detected = False
            self._last_speech_time = time.monotonic()

            # 将 pre-roll 帧预注入队列与完整音频归档
            for frame in self._pre_roll_buffer:
                self._stream_queue.put(frame)
                self._all_pcm_chunks.append(frame)

            self._recording = True
            self._start_time = time.monotonic()

            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=self.frame_samples,
                    device=self.device,
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as e:
                self._recording = False
                raise RuntimeError(f"无法启动麦克风录音设备: {e}") from e

    def get_chunk(self, timeout: float = 0.1) -> Optional[bytes]:
        """流式消费者获取下一个 20ms PCM 帧"""
        if not self._recording and self._stream_queue.empty():
            return None
        try:
            return self._stream_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_chunks(self) -> List[bytes]:
        """排空当前流队列中所有未消费的帧"""
        chunks = []
        while not self._stream_queue.empty():
            try:
                chunks.append(self._stream_queue.get_nowait())
            except queue.Empty:
                break
        return chunks

    def stop(self) -> bytes:
        """停止录音并返回整段累计的 16kHz PCM16 完整音频字节 (无重复帧)"""
        with self._lock:
            if not self._recording:
                return b"".join(self._all_pcm_chunks)

            self._recording = False

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            # 注意：_all_pcm_chunks 在 _callback 中已同步追加，严禁再次追加，防止重复
            full_audio = b"".join(self._all_pcm_chunks)
            return full_audio


def list_input_devices() -> List[Tuple[int, str]]:
    """列出系统中所有可用的麦克风/音频输入设备"""
    if sd is None:
        return []
    devices = []
    try:
        all_devs = sd.query_devices()
        for idx, dev in enumerate(all_devs):
            if dev.get("max_input_channels", 0) > 0:
                devices.append((idx, dev.get("name", "Unknown")))
    except Exception:
        pass
    return devices
