"""ASR 识别模块：基于 sherpa-onnx 离线 SenseVoice-Small 纯 CPU 推理"""

import os
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .config import VoiceConfig

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None


class SenseVoiceRecognizer:
    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self._recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None
        self._is_loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def load(self) -> None:
        """加载 SenseVoice ONNX 模型与词表文件"""
        if sherpa_onnx is None:
            raise RuntimeError("sherpa-onnx 未安装，请在专属虚拟环境中安装 requirements.txt")

        if not self.config.model_path.is_file():
            raise FileNotFoundError(
                f"SenseVoice 模型文件不存在: {self.config.model_path}\n"
                f"请运行 `python -m tools.voice_input.model_download` 进行自动下载"
            )

        if not self.config.tokens_path.is_file():
            raise FileNotFoundError(
                f"SenseVoice tokens 词表文件不存在: {self.config.tokens_path}\n"
                f"请运行 `python -m tools.voice_input.model_download` 进行自动下载"
            )

        start_t = time.monotonic()
        try:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self.config.model_path),
                tokens=str(self.config.tokens_path),
                num_threads=self.config.num_threads,
                use_itn=self.config.use_itn,
                language=self.config.language,
                debug=False,
            )
            self._is_loaded = True
            load_cost = (time.monotonic() - start_t) * 1000
            print(f"[+] SenseVoice-Small 模型载入就绪 (耗时: {load_cost:.1f}ms)")
        except Exception as e:
            self._is_loaded = False
            raise RuntimeError(f"加载 SenseVoice 模型失败: {e}") from e

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """对传入的一维 float32 音频数据执行识别"""
        if not self._is_loaded or self._recognizer is None:
            raise RuntimeError("SenseVoiceRecognizer 尚未初始化或加载")

        if samples is None or len(samples) == 0:
            return ""

        # 确保为 1 维 float32
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        start_t = time.monotonic()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        text = stream.result.text
        cost_ms = (time.monotonic() - start_t) * 1000
        # 统计调试
        # print(f"[*] ASR 转写耗时: {cost_ms:.1f}ms, 文本: {text}")
        return text
