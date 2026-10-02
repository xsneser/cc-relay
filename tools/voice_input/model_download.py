"""模型下载模块：支持 Hugging Face / 国内镜像站下载 SenseVoice-Small 离线模型"""

import argparse
import hashlib
import os
import shutil
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


def ensure_qwen_model(
    config: Optional[VoiceConfig] = None,
    source: str = "modelscope",
    force: bool = False,
) -> bool:
    """检查并下载 Qwen ASR 1.7B 模型权重"""
    if config is None:
        config = VoiceConfig()

    # 绕过系统代理对国内镜像站 (ModelScope / hf-mirror) 的 SSL 握手异常
    no_proxy_entries = "modelscope.cn,www.modelscope.cn,hf-mirror.com,127.0.0.1,localhost"
    cur_no_proxy = os.environ.get("NO_PROXY", "")
    os.environ["NO_PROXY"] = f"{cur_no_proxy},{no_proxy_entries}" if cur_no_proxy else no_proxy_entries
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    target_dir = config.qwen_model_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    if not force and config.is_qwen_installed():
        print(f"[+] Qwen ASR 1.7B 模型已就绪: {target_dir}")
        return True

    print(f"[*] 准备下载 Qwen ASR 1.7B 模型 ({config.qwen_model_id}) 至: {target_dir}")
    print(f"[*] 推荐源: {source}")

    # 1. 尝试使用 ModelScope (国内极速通道)
    if source == "modelscope":
        try:
            from modelscope import snapshot_download
            print("[*] 正在通过 ModelScope 高速通道下载...")
            snapshot_download(config.qwen_model_id, local_dir=str(target_dir))
            if config.is_qwen_installed():
                print(f"[+] Qwen ASR 1.7B 下载完成: {target_dir}")
                return True
        except ImportError:
            print("[!] 未安装 modelscope 库，尝试回退到 huggingface_hub / hf-mirror...")
        except Exception as e:
            print(f"[!] ModelScope 下载异常: {e}，尝试备用源...")

    # 2. 尝试使用 huggingface_hub / hf-mirror
    try:
        from huggingface_hub import snapshot_download
        endpoint = "https://hf-mirror.com" if source in ("hf-mirror", "modelscope") else None
        print(f"[*] 正在通过 huggingface_hub (endpoint={endpoint or '官方源'}) 下载...")
        snapshot_download(
            repo_id=config.qwen_model_id,
            local_dir=str(target_dir),
            endpoint=endpoint,
        )
        if config.is_qwen_installed():
            print(f"[+] Qwen ASR 1.7B 下载完成: {target_dir}")
            return True
        else:
            print("[-] 下载未完成或文件不完整")
            return False
    except ImportError:
        print("[-] 未安装 huggingface_hub 或 modelscope，请先运行: pip install modelscope 或 pip install huggingface_hub")
        return False
    except Exception as e:
        print(f"[-] Qwen ASR 1.7B 下载失败: {e}")
        return False


def delete_qwen_model(config: Optional[VoiceConfig] = None) -> Tuple[bool, str]:
    """删除本地 Qwen ASR 1.7B 离线模型文件并释放磁盘空间"""
    if config is None:
        config = VoiceConfig()

    deleted_paths = []
    failed_paths = []

    candidates = [
        config.qwen_model_dir,
        config.models_dir / config.qwen_model_id,
        Path(os.path.expanduser("~/.cache/modelscope/hub/models")) / config.qwen_model_id,
        Path(os.path.expanduser("~/.cache/modelscope/hub")) / config.qwen_model_id,
        Path(os.path.expanduser("~/.cache/huggingface/hub")) / f"models--{config.qwen_model_id.replace('/', '--')}",
    ]

    for p in candidates:
        if p.exists() and p.is_dir():
            try:
                shutil.rmtree(p, ignore_errors=False)
                deleted_paths.append(str(p))
            except Exception as e:
                failed_paths.append(f"{p} ({e})")

    if failed_paths:
        return False, f"部分路径删除失败: {', '.join(failed_paths)}"

    if deleted_paths:
        return True, f"已成功删除 Qwen 1.7B 模型，释放磁盘空间 (已删除: {len(deleted_paths)} 个目录)"
    return True, "未检测到本地 Qwen 1.7B 模型文件 (无需删除)"


def main():
    parser = argparse.ArgumentParser(description="下载 SenseVoice / Qwen ASR 离线 ASR 模型")
    parser.add_argument(
        "--model",
        choices=["sensevoice", "qwen", "all"],
        default="sensevoice",
        help="待下载模型类别 (默认: sensevoice, 可选: qwen, all)",
    )
    parser.add_argument(
        "--source",
        choices=list(DOWNLOAD_SOURCES.keys()),
        default="huggingface",
        help="下载源选择 (默认: huggingface 官方源，Qwen 推荐 modelscope)",
    )
    parser.add_argument("--force", action="store_true", help="强制重新下载已有模型文件")
    parser.add_argument("--check", action="store_true", help="仅检查模型是否已就绪")
    args = parser.parse_args()

    cfg = VoiceConfig()
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
        q_source = "modelscope" if args.source == "huggingface" else args.source
        if not ensure_qwen_model(cfg, source=q_source, force=args.force):
            all_ok = False

    if all_ok:
        print("[+] 选定的模型已全部成功下载并就绪！")
        sys.exit(0)
    else:
        print("[-] 模型下载失败，请检查网络连接或更换下载源")
        sys.exit(1)


# 导出兼容器别名
download_models = ensure_models


if __name__ == "__main__":
    main()
