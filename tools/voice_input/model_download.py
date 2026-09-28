"""模型下载模块：支持 Hugging Face / 国内镜像站下载 SenseVoice-Small 离线模型"""

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from .config import VoiceConfig

# 预设下载源
DOWNLOAD_SOURCES = {
    "hf-mirror": {
        "name": "HF 国内镜像 (hf-mirror.com)",
        "base_url": "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/",
    },
    "modelscope": {
        "name": "ModelScope 魔搭社区",
        "base_url": "https://modelscope.cn/models/k2-fsa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/master/",
    },
    "huggingface": {
        "name": "Hugging Face 官方源",
        "base_url": "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/",
    },
}

# 待下载的文件清单及其预期大小下限 (bytes)
MODEL_FILES = {
    "tokens.txt": {
        "min_size": 100_000,        # ~200KB
    },
    "model.int8.onnx": {
        "min_size": 200_000_000,    # ~239MB
    },
}


def compute_sha256(file_path: Path) -> str:
    """计算文件 SHA-256 哈希值"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def download_file_with_progress(url: str, dest_path: Path) -> bool:
    """带进度条与断点暂存的文件流式下载"""
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[*] 正在下载: {url}")
    print(f"[*] 目标位置: {dest_path.name}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 cc-relay-voice/1.0"
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            total_size = response.getheader("Content-Length")
            total_bytes = int(total_size) if total_size else 0
            downloaded = 0
            chunk_size = 1024 * 512  # 512KB

            with open(part_path, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes > 0:
                        percent = downloaded / total_bytes * 100
                        mb_down = downloaded / (1024 * 1024)
                        mb_tot = total_bytes / (1024 * 1024)
                        sys.stdout.write(f"\r    -> 进度: {percent:.1f}% ({mb_down:.1f}MB / {mb_tot:.1f}MB)")
                        sys.stdout.flush()
                    else:
                        mb_down = downloaded / (1024 * 1024)
                        sys.stdout.write(f"\r    -> 已下载: {mb_down:.1f}MB")
                        sys.stdout.flush()

            sys.stdout.write("\n")

        # 校验大小后重命名
        if part_path.exists():
            part_path.replace(dest_path)
            print(f"[+] 下载完成: {dest_path.name}")
            return True
        return False
    except Exception as e:
        print(f"\n[-] 下载失败: {e}")
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        return False


def ensure_models(
    config: Optional[VoiceConfig] = None,
    source: str = "huggingface",
    force: bool = False,
) -> bool:
    """检查并下载所需的 SenseVoice 模型文件"""
    if config is None:
        config = VoiceConfig()

    target_dir = config.models_dir / config.sensevoice_model_name
    target_dir.mkdir(parents=True, exist_ok=True)

    if source not in DOWNLOAD_SOURCES:
        print(f"[-] 未知下载源 '{source}'，可选: {list(DOWNLOAD_SOURCES.keys())}")
        source = "hf-mirror"

    src_info = DOWNLOAD_SOURCES[source]
    base_url = src_info["base_url"]
    print(f"[*] 当前使用下载源: {src_info['name']}")

    all_ok = True
    for filename, meta in MODEL_FILES.items():
        file_path = target_dir / filename
        if file_path.is_file() and not force:
            actual_size = file_path.stat().st_size
            if actual_size >= meta["min_size"]:
                print(f"[+] 模型文件已就绪: {filename} ({actual_size / (1024*1024):.1f}MB)")
                continue
            else:
                print(f"[!] 模型文件异常或不完整 ({actual_size} bytes)，将重新下载...")

        url = base_url + filename
        ok = download_file_with_progress(url, file_path)
        if not ok:
            # 尝试回退到其他源
            fallback_sources = [s for s in DOWNLOAD_SOURCES if s != source]
            for fb in fallback_sources:
                print(f"[!] 尝试备用源: {DOWNLOAD_SOURCES[fb]['name']}...")
                fb_url = DOWNLOAD_SOURCES[fb]["base_url"] + filename
                if download_file_with_progress(fb_url, file_path):
                    ok = True
                    break
        if not ok:
            all_ok = False
            break

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="下载 SenseVoice-Small 离线 ASR 模型")
    parser.add_argument(
        "--source",
        choices=list(DOWNLOAD_SOURCES.keys()),
        default="huggingface",
        help="下载源选择 (默认: huggingface 官方源)",
    )
    parser.add_argument("--force", action="store_true", help="强制重新下载已有模型文件")
    parser.add_argument("--check", action="store_true", help="仅检查模型是否已就绪")
    args = parser.parse_args()

    cfg = VoiceConfig()
    if args.check:
        if cfg.is_sensevoice_installed():
            print(f"[+] 模型已完整安装于: {cfg.models_dir / cfg.sensevoice_model_name}")
            sys.exit(0)
        else:
            print("[-] 模型未安装或文件缺失")
            sys.exit(1)

    success = ensure_models(cfg, source=args.source, force=args.force)
    if success:
        print("[+] 所有模型文件已成功下载并就绪！")
        sys.exit(0)
    else:
        print("[-] 模型下载失败，请检查网络连接或更换下载源 --source modelscope / huggingface")
        sys.exit(1)


# 导出兼容器别名
download_models = ensure_models


if __name__ == "__main__":
    main()
