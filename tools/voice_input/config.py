"""配置模块：语音伴侣工具运行参数与校验"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class VoiceConfig:
    # 路径配置
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    models_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "models")
    sensevoice_model_name: str = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    sensevoice_model_filename: str = "model.int8.onnx"
    sensevoice_tokens_filename: str = "tokens.txt"

    # 服务网络配置
    host: str = "127.0.0.1"
    port: int = 8401
    auto_start: bool = False
    relay_host: str = "127.0.0.1"
    relay_port: int = 8400
    relay_ui_port: int = 8610
    fake_api_key: str = "sk-relay-local-0000"
    semantic_correction_enabled: bool = False
    semantic_correction_model: str = ""
    semantic_correction_timeout_seconds: float = 60.0
    semantic_correction_context_chars: int = 2500

    # 引擎模式: "qwen_2pass" (推荐高精流式: Zipformer + Qwen3-ASR-1.7B) | "qwen_offline" (Qwen 1.7B 离线单 Pass) | "sherpa_2pass" (统一 2-Pass：流式 Zipformer + 离线 SenseVoice) | "sensevoice_offline" (轻量极速，纯 CPU) | "paraformer_streaming_2pass" (向后兼容别名)
    engine: str = "sherpa_2pass"

    # Qwen ASR 1.7B 配置
    qwen_model_id: str = "Qwen/Qwen3-ASR-1.7B"
    qwen_model_dir_name: str = "Qwen3-ASR-1.7B"
    qwen_device: str = "auto"          # "auto" | "cuda" | "cpu"
    qwen_torch_dtype: str = "auto"     # "auto" | "bfloat16" | "float16" | "float32"
    qwen_trailing_audio_guard_seconds: float = 2.0
    qwen_stable_hypothesis_count: int = 3
    qwen_segment_silence_seconds: float = 0.6
    vram_mode: str = "resident"        # "resident" | "on_demand_offload"

    # 音频参数
    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 20         # 20ms 一帧 (320 samples)
    vad_mode: int = 2                  # WebRTC VAD 灵敏度 0~3
    pre_roll_ms: int = 240             # Ring buffer 预录时长 (防首音节被截断)
    min_recording_seconds: float = 0.25
    max_recording_seconds: float = 30.0

    # 触发按键配置
    # 支持鼠标侧键: "mouse_x1", "mouse_x2"
    # 键盘按键: "f8", "f9", "caps_lock", "ctrl_r"
    hotkey: str = "mouse_x1"
    caps_lock_threshold_seconds: float = 0.25

    # ASR 推理与线程
    num_threads: int = 2
    device: str = "auto"               # "auto" | "cpu" | "cuda:0" | "cuda:1"
    language: str = "auto"
    use_itn: bool = True

    # 注入与剪贴板
    restore_clipboard: bool = True
    restore_clipboard_delay: float = 0.15
    beep_feedback: bool = False
    auto_reactivate_target: bool = True
    restore_switched_focus: bool = True

    @property
    def model_name(self) -> str:
        return self.sensevoice_model_name

    @property
    def sensevoice_model_path(self) -> Path:
        return self.models_dir / self.sensevoice_model_name / self.sensevoice_model_filename

    @property
    def sensevoice_tokens_path(self) -> Path:
        return self.models_dir / self.sensevoice_model_name / self.sensevoice_tokens_filename

    @property
    def model_path(self) -> Path:
        """向后兼容属性别名"""
        return self.sensevoice_model_path

    @property
    def tokens_path(self) -> Path:
        """向后兼容属性别名"""
        return self.sensevoice_tokens_path

    @property
    def qwen_model_dir(self) -> Path:
        return self.models_dir / self.qwen_model_dir_name

    def is_qwen_installed(self) -> bool:
        """检查 Qwen ASR 1.7B 模型是否在本地就绪 (优先 repo/models，其次 modelscope/hf 缓存)"""
        d = self.qwen_model_dir
        if d.is_dir() and ((d / "config.json").is_file() or any(d.glob("*.safetensors")) or any(d.glob("*.bin"))):
            return True

        # 检查 ModelScope 默认缓存
        ms_candidates = [
            Path(os.path.expanduser("~/.cache/modelscope/hub/models")) / self.qwen_model_id,
            Path(os.path.expanduser("~/.cache/modelscope/hub")) / self.qwen_model_id,
        ]
        for c in ms_candidates:
            if c.is_dir() and ((c / "config.json").is_file() or any(c.glob("*.safetensors")) or any(c.glob("*.bin"))):
                return True

        # 检查 Hugging Face 默认缓存
        hf_cache = Path(os.path.expanduser("~/.cache/huggingface/hub")) / f"models--{self.qwen_model_id.replace('/', '--')}"
        if hf_cache.is_dir():
            snapshots = hf_cache / "snapshots"
            if snapshots.is_dir() and any(snapshots.iterdir()):
                return True
        return False

    @property
    def streaming_model_dir(self) -> Path:
        return self.models_dir / "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"

    def is_streaming_model_installed(self) -> bool:
        d = self.streaming_model_dir
        if not d.is_dir():
            return False
        has_encoder = any(d.glob("encoder*.onnx"))
        has_decoder = any(d.glob("decoder*.onnx"))
        has_joiner = any(d.glob("joiner*.onnx"))
        has_tokens = (d / "tokens.txt").is_file()
        return has_encoder and has_decoder and has_joiner and has_tokens

    def is_sensevoice_installed(self) -> bool:
        return self.sensevoice_model_path.is_file() and self.sensevoice_tokens_path.is_file()

    def is_model_installed(self) -> bool:
        return self.is_sensevoice_installed()

    def validate(self) -> None:
        """检查关键配置是否合法"""
        if self.sample_rate != 16000:
            raise ValueError(f"语音识别仅支持 16000Hz 采样率，当前配置: {self.sample_rate}")
        if self.vad_mode not in (0, 1, 2, 3):
            raise ValueError(f"vad_mode 必须为 0~3，当前: {self.vad_mode}")
        if self.min_recording_seconds <= 0 or self.max_recording_seconds <= self.min_recording_seconds:
            raise ValueError("录音时长阈值非法")
        if self.qwen_trailing_audio_guard_seconds <= 0 or self.qwen_segment_silence_seconds <= 0:
            raise ValueError("Qwen 音频保留与静音边界阈值必须大于 0")
        if self.qwen_stable_hypothesis_count < 2:
            raise ValueError("Qwen 稳定前缀至少需要 2 次成功识别")
        valid_engines = ("qwen_2pass", "qwen_offline", "sherpa_2pass", "sensevoice_offline", "paraformer_streaming_2pass")
        if self.engine not in valid_engines:
            raise ValueError(f"不支持的引擎: {self.engine}")
        valid_dev = str(self.device or "auto").strip().lower()
        if not (valid_dev in ("auto", "cpu") or valid_dev.startswith("cuda")):
            raise ValueError(f"不支持的设备配置: {self.device}")
        self.vram_mode = str(self.vram_mode or "resident").strip().lower()
        if self.vram_mode not in ("resident", "on_demand_offload"):
            raise ValueError(f"不支持的显存策略: {self.vram_mode}")
        if not (1024 <= self.port <= 65535):
            raise ValueError(f"端口超出范围: {self.port}")
        if not (1024 <= self.relay_ui_port <= 65535):
            raise ValueError(f"Relay UI 端口超出范围: {self.relay_ui_port}")
        if not (1.0 <= self.semantic_correction_timeout_seconds <= 120.0):
            raise ValueError("语义校正超时必须在 1~120 秒之间")
        if not (512 <= self.semantic_correction_context_chars <= 12000):
            raise ValueError("语义校正上下文长度必须在 512~12000 字符之间")
        if self.num_threads <= 0:
            self.num_threads = 2

    @classmethod
    def from_relay_config(cls, repo_root: Optional[Path] = None) -> "VoiceConfig":
        """从根目录 config.json 中的 tools.voice 节点加载配置"""
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent.parent
        config_path = repo_root / "config.json"
        cfg = cls()
        if not config_path.is_file():
            return cfg

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("ui_port"), int):
                cfg.relay_ui_port = data["ui_port"]
            if isinstance(data.get("listen_port"), int):
                cfg.relay_port = data["listen_port"]
            if isinstance(data.get("listen_host"), str) and data["listen_host"].strip():
                cfg.relay_host = data["listen_host"].strip()
            if isinstance(data.get("fake_api_key"), str) and data["fake_api_key"].strip():
                cfg.fake_api_key = data["fake_api_key"].strip()
            v_conf = data.get("tools", {}).get("voice", {})
            if not isinstance(v_conf, dict):
                return cfg

            if "auto_start" in v_conf:
                cfg.auto_start = bool(v_conf["auto_start"])
            if "semantic_correction_enabled" in v_conf:
                cfg.semantic_correction_enabled = bool(v_conf["semantic_correction_enabled"])
            if "semantic_correction_model" in v_conf and isinstance(v_conf["semantic_correction_model"], str):
                cfg.semantic_correction_model = v_conf["semantic_correction_model"].strip()
            timeout = v_conf.get("semantic_correction_timeout_seconds")
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                cfg.semantic_correction_timeout_seconds = float(timeout)
            context_chars = v_conf.get("semantic_correction_context_chars")
            if isinstance(context_chars, int) and not isinstance(context_chars, bool):
                cfg.semantic_correction_context_chars = context_chars
            if "hotkey" in v_conf and isinstance(v_conf["hotkey"], str):
                cfg.hotkey = v_conf["hotkey"].strip().lower()
            if "engine" in v_conf and isinstance(v_conf["engine"], str):
                cfg.engine = v_conf["engine"].strip()
            if "device" in v_conf and isinstance(v_conf["device"], str):
                cfg.device = v_conf["device"].strip().lower()
                cfg.qwen_device = cfg.device
            if "qwen_model_id" in v_conf and isinstance(v_conf["qwen_model_id"], str):
                cfg.qwen_model_id = v_conf["qwen_model_id"].strip()
            if "qwen_device" in v_conf and isinstance(v_conf["qwen_device"], str):
                cfg.qwen_device = v_conf["qwen_device"].strip()
                if "device" not in v_conf:
                    cfg.device = cfg.qwen_device
            if "qwen_torch_dtype" in v_conf and isinstance(v_conf["qwen_torch_dtype"], str):
                cfg.qwen_torch_dtype = v_conf["qwen_torch_dtype"].strip()
            if isinstance(v_conf.get("qwen_trailing_audio_guard_seconds"), (int, float)):
                cfg.qwen_trailing_audio_guard_seconds = float(v_conf["qwen_trailing_audio_guard_seconds"])
            if isinstance(v_conf.get("qwen_stable_hypothesis_count"), int):
                cfg.qwen_stable_hypothesis_count = int(v_conf["qwen_stable_hypothesis_count"])
            if isinstance(v_conf.get("qwen_segment_silence_seconds"), (int, float)):
                cfg.qwen_segment_silence_seconds = float(v_conf["qwen_segment_silence_seconds"])
            if "vram_mode" in v_conf and isinstance(v_conf["vram_mode"], str):
                cfg.vram_mode = v_conf["vram_mode"].strip().lower()
            if "port" in v_conf and isinstance(v_conf["port"], int):
                cfg.port = v_conf["port"]
            if "vad_mode" in v_conf and isinstance(v_conf["vad_mode"], int):
                cfg.vad_mode = v_conf["vad_mode"]
            if "num_threads" in v_conf and isinstance(v_conf["num_threads"], int) and v_conf["num_threads"] > 0:
                cfg.num_threads = v_conf["num_threads"]
            if "restore_clipboard" in v_conf:
                cfg.restore_clipboard = bool(v_conf["restore_clipboard"])
            if "beep_feedback" in v_conf:
                cfg.beep_feedback = bool(v_conf["beep_feedback"])
            if "auto_reactivate_target" in v_conf:
                cfg.auto_reactivate_target = bool(v_conf["auto_reactivate_target"])
            if "restore_switched_focus" in v_conf:
                cfg.restore_switched_focus = bool(v_conf["restore_switched_focus"])
        except Exception:
            pass

        return cfg
