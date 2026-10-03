"""CLI 入口模块：支持 service, listen, doctor, devices, download, normalize 子命令"""

import argparse
import os
import sys
import time
from pathlib import Path

_BOOT_MONOTONIC = time.monotonic()
_STARTUP_ATTEMPT_ID = os.environ.get("CC_VOICE_START_ATTEMPT_ID", "")

def _startup_log(phase: str, **details) -> None:
    if not _STARTUP_ATTEMPT_ID:
        return
    import json
    elapsed = round(time.monotonic() - _BOOT_MONOTONIC, 3)
    event = {"attempt_id": _STARTUP_ATTEMPT_ID, "phase": phase, "child_elapsed_seconds": elapsed, **details}
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())}] [VOICE_STARTUP] {json.dumps(event, ensure_ascii=False)}", flush=True)

_startup_log("child_python_entry", executable=sys.executable, pid=os.getpid())

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from .audio import list_input_devices
from .config import VoiceConfig
from .normalize import normalize

_startup_log("cli_imports_complete")


def cmd_doctor():
    """全面诊断当前环境依赖与硬件状态"""
    print("=" * 60)
    print(" Claude Code 语音输入环境诊断 (Doctor)")
    print("=" * 60)

    cfg = VoiceConfig.from_relay_config()
    all_pass = True

    # 1. Python 环境
    print(f"[1] Python 版本: {sys.version.split()[0]} ({sys.executable})")

    # 2. 检查关键依赖库
    print("\n[2] 检查核心依赖库:")
    deps = [
        ("numpy", "numpy (音频矩阵运算)", True),
        ("sounddevice", "sounddevice (麦克风音频采集)", True),
        ("pynput", "pynput (全局热键监听)", True),
        ("websockets", "websockets (双向流式 RPC 通信)", True),
    ]
    if sys.platform == "win32":
        deps.append(("win32gui", "pywin32 (Win32 焦点检测与安全注入)", True))
        try:
            from .tk_runtime import validate_tk_runtime, find_tcl_tk_dirs
            tcl_dir, _ = find_tcl_tk_dirs()
            if validate_tk_runtime():
                deps.append(("tkinter", f"tkinter (桌面免激活悬浮胶囊界面，Tcl 资源: {tcl_dir.parent.name if tcl_dir else '系统'})", True))
            else:
                deps.append(("tkinter", "tkinter (桌面免激活悬浮胶囊界面，缺少 Tcl 脚本资源)", True))
        except Exception:
            deps.append(("tkinter", "tkinter (桌面免激活悬浮胶囊界面)", True))

    if cfg.engine in ("qwen_2pass", "qwen_asr", "qwen_offline", "paraformer_streaming_2pass"):
        deps.append(("torch", "PyTorch (Qwen ASR 1.7B 推理后端)", True))
        deps.append(("transformers", "transformers (Qwen ASR 1.7B 模型管道)", True))
        deps.append(("webrtcvad", "webrtcvad (WebRTC VAD 语音活动检测)", False))
    elif cfg.engine == "sherpa_2pass":
        deps.append(("sherpa_onnx", "sherpa-onnx (Zipformer + SenseVoice 统一 2-Pass 引擎)", True))
        deps.append(("webrtcvad", "webrtcvad (WebRTC VAD 语音活动检测)", False))
    else:
        deps.append(("sherpa_onnx", "sherpa-onnx (SenseVoice 本地轻量 CPU 引擎)", True))
        deps.append(("webrtcvad", "webrtcvad (WebRTC VAD，未安装时自动回退至内置 RMS 检测)", False))

    for mod_name, desc, is_required in deps:
        try:
            __import__(mod_name)
            print(f"    [+] {desc}: 已就绪")
        except ImportError:
            if is_required:
                print(f"    [-] {desc}: 未安装 (必须，请运行 setup_voice.bat 安装)")
                all_pass = False
            else:
                print(f"    [*] {desc}: 未安装 (可选，系统已启用平滑回退)")

    # 3. 检查模型状态
    print("\n[3] 检查模型与配置状态:")
    print(f"    [*] 当前配置引擎: {cfg.engine}")
    print(f"    [*] 服务监听端口: {cfg.host}:{cfg.port}")
    print(f"    [*] 对讲触发热键: {cfg.hotkey}")
    if cfg.engine in ("qwen_2pass", "qwen_asr", "qwen_offline", "paraformer_streaming_2pass"):
        from .asr_engine import _find_local_qwen_dir
        qwen_dir = _find_local_qwen_dir(cfg.qwen_model_id)
        if qwen_dir:
            print(f"    [+] Qwen ASR 1.7B 模型: 已就绪 ({qwen_dir})")
        else:
            print(f"    [-] Qwen ASR 1.7B 模型: 未在本地找到 (请运行 python -m tools.voice_input download --model qwen)")
            all_pass = False
    elif cfg.engine == "sherpa_2pass":
        if cfg.is_sensevoice_installed():
            print(f"    [+] SenseVoice 离线模型: 已就绪 ({cfg.sensevoice_model_path.name})")
        else:
            print(f"    [-] SenseVoice 离线模型: 未下载")
            all_pass = False
        if cfg.is_streaming_model_installed():
            print(f"    [+] Zipformer 流式模型: 已就绪")
        else:
            print(f"    [*] Zipformer 流式模型: 未下载 (将自动降级为 SenseVoice 离线单 Pass)")
    else:
        if cfg.is_sensevoice_installed():
            print(f"    [+] SenseVoice 离线模型: 已就绪 ({cfg.sensevoice_model_path.name})")
        else:
            print(f"    [-] SenseVoice 离线模型: 未下载 (请运行 setup_voice.bat 或 python -m tools.voice_input download)")
            all_pass = False

    # 4. 检查麦克风设备
    print("\n[4] 检查可用音频输入设备:")
    devices = list_input_devices()
    if devices:
        for idx, dev_name in devices:
            print(f"    [+] 设备 #{idx}: {dev_name}")
    else:
        print("    [-] 未检测到可用的音频输入设备，请检查麦克风连接或权限。")
        all_pass = False

    print("\n" + "=" * 60)
    if all_pass:
        print("[✓] 诊断通过！所有依赖、硬件与引擎配置就绪。")
    else:
        print("[!] 诊断发现部分项未就绪，请参考上述提示进行配置。")
    print("=" * 60)


def cmd_devices():
    """列出可用麦克风"""
    print("可用的音频输入设备列表:")
    devices = list_input_devices()
    if not devices:
        print("[-] 未找到输入设备")
        return
    for idx, name in devices:
        print(f"  [{idx}] {name}")


def cmd_normalize(text: str):
    """测试文本规范化转换"""
    res = normalize(text)
    print(f"原始输入: {text}")
    print(f"规范输出: {res}")


def cmd_service(hotkey: str = None, engine: str = None, device: str = None, port: int = None, headless: bool = False):
    """启动语音服务（包含后台 WebSocket RPC 服务、桌面悬浮胶囊与全局 PTT 对讲热键）"""
    if sys.platform == "win32":
        try:
            import ctypes
            # 设置自身为 BELOW_NORMAL_PRIORITY_CLASS (0x00004000) 确保不影响系统输入响应与前台调度
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        except Exception:
            pass

        if not headless:
            try:
                from .tk_runtime import setup_tk_environment
                setup_tk_environment()
            except Exception:
                pass

    _startup_log("reading_config")
    cfg = VoiceConfig.from_relay_config()
    if hotkey:
        cfg.hotkey = hotkey
    if engine:
        cfg.engine = engine
    if device:
        cfg.device = device
        cfg.qwen_device = device
    if port:
        cfg.port = port

    _startup_log("importing_daemon")
    from .daemon import VoiceInputDaemon
    _startup_log("constructing_daemon")
    daemon = VoiceInputDaemon(cfg)
    _startup_log("daemon_constructed")
    try:
        _startup_log("daemon_starting")
        daemon.start(headless=headless)
    finally:
        daemon.stop()


def main():
    parser = argparse.ArgumentParser(description="Claude Code CLI 中文语音输入伴侣工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # service / listen
    p_service = subparsers.add_parser("service", help="启动全功能语音伴侣服务（含 RPC 与全局对讲）")
    p_service.add_argument("--hotkey", help="触发热键 (mouse_x1, mouse_x2, f8, caps_lock)")
    p_service.add_argument("--engine", help="ASR 引擎模式 (qwen_2pass, qwen_offline, sherpa_2pass, sensevoice_offline)")
    p_service.add_argument("--device", help="推理计算设备 (auto, cpu, cuda:0, cuda:1)")
    p_service.add_argument("--port", type=int, help="RPC 服务端口 (默认 8401)")
    p_service.add_argument("--headless", action="store_true", help="无头后台运行 (不弹出桌面悬浮胶囊)")

    p_listen = subparsers.add_parser("listen", help="启动语音对讲监听（兼容别名）")
    p_listen.add_argument("--hotkey", help="触发热键")
    p_listen.add_argument("--headless", action="store_true", help="无头后台运行")

    # download
    p_dl = subparsers.add_parser("download", help="下载 ASR 离线模型")
    p_dl.add_argument("--model", choices=["sensevoice", "qwen", "all"], default="sensevoice", help="模型类型 (sensevoice, qwen, all)")
    p_dl.add_argument("--source", default="hf-mirror", help="模型下载源")
    p_dl.add_argument("--check", action="store_true", help="仅检查模型是否已就绪")
    p_dl.add_argument("--force", action="store_true", help="强制重新下载已有模型文件")

    # doctor
    subparsers.add_parser("doctor", help="诊断环境依赖、硬件与模型状态")

    # devices
    subparsers.add_parser("devices", help="列出当前系统所有音频输入麦克风设备")

    # normalize
    p_norm = subparsers.add_parser("normalize", help="测试命令规范化规则")
    p_norm.add_argument("text", help="待测试转化的口语文案")

    args = parser.parse_args()

    if args.command == "doctor":
        cmd_doctor()
    elif args.command == "devices":
        cmd_devices()
    elif args.command == "normalize":
        cmd_normalize(args.text)
    elif args.command == "download":
        from .model_download import ensure_models, ensure_qwen_model
        cfg = VoiceConfig.from_relay_config()
        if args.check:
            sv_ok = cfg.is_sensevoice_installed()
            qw_ok = cfg.is_qwen_installed()
            print(f"[*] SenseVoice 模型就绪状态: {'[+] 已安装' if sv_ok else '[-] 未安装'}")
            print(f"[*] Qwen ASR 1.7B 就绪状态: {'[+] 已安装' if qw_ok else '[-] 未安装'}")
            sys.exit(0 if (sv_ok or qw_ok) else 1)
        all_ok = True
        if args.model in ("sensevoice", "all"):
            if not ensure_models(cfg, source=args.source, force=args.force):
                all_ok = False
        if args.model in ("qwen", "all"):
            q_src = "modelscope" if args.source == "huggingface" else args.source
            if not ensure_qwen_model(cfg, source=q_src, force=args.force):
                all_ok = False
        sys.exit(0 if all_ok else 1)
    elif args.command in ("service", "listen") or args.command is None:
        hotkey = getattr(args, "hotkey", None)
        engine = getattr(args, "engine", None)
        device = getattr(args, "device", None)
        port = getattr(args, "port", None)
        headless = getattr(args, "headless", False)
        cmd_service(hotkey=hotkey, engine=engine, device=device, port=port, headless=headless)


if __name__ == "__main__":
    main()
