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

    # 引擎模式: "paraformer_streaming_2pass" | "sensevoice_offline"
    engine: str = "paraformer_streaming_2pass"

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
    device: str = "cpu"                # "cpu" | "cuda:0"
    language: str = "auto"
    use_itn: bool = True

    # 注入与剪贴板
    restore_clipboard: bool = True
    restore_clipboard_delay: float = 0.15
    beep_feedback: bool = False

    @property
    def sensevoice_model_path(self) -> Path:
        return self.models_dir / self.sensevoice_model_name / self.sensevoice_model_filename

    @property
    def sensevoice_tokens_path(self) -> Path:
        return self.models_dir / self.sensevoice_model_name / self.sensevoice_tokens_filename

    def is_sensevoice_installed(self) -> bool:
        return self.sensevoice_model_path.is_file() and self.sensevoice_tokens_path.is_file()

    def validate(self) -> None:
        """检查关键配置是否合法"""
        if self.sample_rate != 16000:
            raise ValueError(f"语音识别仅支持 16000Hz 采样率，当前配置: {self.sample_rate}")
        if self.vad_mode not in (0, 1, 2, 3):
            raise ValueError(f"vad_mode 必须为 0~3，当前: {self.vad_mode}")
        if self.min_recording_seconds <= 0 or self.max_recording_seconds <= self.min_recording_seconds:
            raise ValueError("录音时长阈值非法")
        if self.engine not in ("paraformer_streaming_2pass", "sensevoice_offline"):
            raise ValueError(f"不支持的引擎: {self.engine}")
        if not (1024 <= self.port <= 65535):
            raise ValueError(f"端口超出范围: {self.port}")

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
            v_conf = data.get("tools", {}).get("voice", {})
            if not isinstance(v_conf, dict):
                return cfg

            if "auto_start" in v_conf:
                cfg.auto_start = bool(v_conf["auto_start"])
            if "hotkey" in v_conf and isinstance(v_conf["hotkey"], str):
                cfg.hotkey = v_conf["hotkey"].strip().lower()
            if "engine" in v_conf and isinstance(v_conf["engine"], str):
                cfg.engine = v_conf["engine"].strip()
            if "port" in v_conf and isinstance(v_conf["port"], int):
                cfg.port = v_conf["port"]
            if "vad_mode" in v_conf and isinstance(v_conf["vad_mode"], int):
                cfg.vad_mode = v_conf["vad_mode"]
            if "restore_clipboard" in v_conf:
                cfg.restore_clipboard = bool(v_conf["restore_clipboard"])
            if "beep_feedback" in v_conf:
                cfg.beep_feedback = bool(v_conf["beep_feedback"])
        except Exception:
            pass

        return cfg
