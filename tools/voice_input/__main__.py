"""CLI 入口模块：支持 service, listen, doctor, devices, download, normalize 子命令"""

import argparse
import sys
from pathlib import Path

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
    print("\n[2] 检查核心与流式依赖库:")
    deps = [
        ("funasr", "funasr (2-Pass 流式因果 ASR 引擎)"),
        ("webrtcvad", "webrtcvad (WebRTC 语音活动检测 VAD)"),
        ("websockets", "websockets (双向流式 RPC 通信)"),
        ("sounddevice", "sounddevice (麦克风音频采集)"),
        ("numpy", "numpy (音频矩阵运算)"),
        ("pynput", "pynput (全局热键监听)"),
        ("win32gui", "pywin32 (Win32 焦点检测与安全注入)"),
    ]
    for mod_name, desc in deps:
        try:
            __import__(mod_name)
            print(f"    [+] {desc}: 已就绪")
        except ImportError:
            print(f"    [-] {desc}: 未安装 (请在环境中运行 pip install -r requirements.txt)")
            all_pass = False

    # 3. 检查模型状态
    print("\n[3] 检查模型与配置状态:")
    print(f"    [*] 当前配置引擎: {cfg.engine}")
    print(f"    [*] 服务监听端口: {cfg.host}:{cfg.port}")
    print(f"    [*] 对讲触发热键: {cfg.hotkey}")

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


def cmd_service(hotkey: str = None, engine: str = None, port: int = None, headless: bool = False):
    """启动语音服务（包含后台 WebSocket RPC 服务、桌面悬浮胶囊与全局 PTT 对讲热键）"""
    cfg = VoiceConfig.from_relay_config()
    if hotkey:
        cfg.hotkey = hotkey
    if engine:
        cfg.engine = engine
    if port:
        cfg.port = port

    from .daemon import VoiceInputDaemon
    daemon = VoiceInputDaemon(cfg)
    daemon.start(headless=headless)


def main():
    parser = argparse.ArgumentParser(description="Claude Code CLI 中文语音输入伴侣工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # service / listen
    p_service = subparsers.add_parser("service", help="启动全功能语音伴侣服务（含 RPC 与全局对讲）")
    p_service.add_argument("--hotkey", help="触发热键 (mouse_x1, mouse_x2, f8, caps_lock)")
    p_service.add_argument("--engine", help="ASR 引擎模式 (paraformer_streaming_2pass, sensevoice_offline)")
    p_service.add_argument("--port", type=int, help="RPC 服务端口 (默认 8401)")
    p_service.add_argument("--headless", action="store_true", help="无头后台运行 (不弹出桌面悬浮胶囊)")

    p_listen = subparsers.add_parser("listen", help="启动语音对讲监听（兼容别名）")
    p_listen.add_argument("--hotkey", help="触发热键")
    p_listen.add_argument("--headless", action="store_true", help="无头后台运行")

    # download
    p_dl = subparsers.add_parser("download", help="下载 ASR 离线模型")
    p_dl.add_argument("--source", default="hf-mirror", help="模型下载源")

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
        from .model_download import download_models
        download_models(source=args.source)
    elif args.command in ("service", "listen") or args.command is None:
        hotkey = getattr(args, "hotkey", None)
        engine = getattr(args, "engine", None)
        port = getattr(args, "port", None)
        headless = getattr(args, "headless", False)
        cmd_service(hotkey=hotkey, engine=engine, port=port, headless=headless)


if __name__ == "__main__":
    main()
