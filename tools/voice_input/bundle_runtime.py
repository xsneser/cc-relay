"""ASR 自包含便携运行环境组装与校验脚本
用于在打包前自动化准备 tools/voice_input/runtime/ 独立嵌入式 Python 3.10 环境
以及验证 SenseVoice-Small 离线模型，使最终生成的安装包完全脱离系统 Python 和网络依赖。
"""

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# 控制台 UTF-8 支持
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"
MODELS_DIR = BASE_DIR / "models"
VENV_DIR = BASE_DIR / ".venv"
CACHE_DIR = BASE_DIR / ".cache"

PYTHON_EMBED_URLS = [
    "https://npmmirror.com/mirrors/python/3.10.11/python-3.10.11-embed-amd64.zip",
    "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip",
]


def test_runtime_interpreter(py_exe: Path) -> bool:
    """验证 runtime 中的 Python 解释器是否可用并成功载入语音核心依赖"""
    if not py_exe.is_file():
        return False
    check_code = (
        "import sys, os\n"
        "import numpy\n"
        "import sounddevice\n"
        "import pynput\n"
        "import websockets\n"
        "import sherpa_onnx\n"
        "print('RUNTIME_OK')\n"
    )
    try:
        res = subprocess.run(
            [str(py_exe), "-c", check_code],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(REPO_ROOT),
        )
        return res.returncode == 0 and "RUNTIME_OK" in res.stdout
    except Exception as e:
        print(f"[-] 测试解释器异常: {e}")
        return False


def ensure_embed_python() -> bool:
    """确保 runtime 目录具备基础嵌入式 Python 解释器 (优先复用本地运行时，免外网下载)"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    target_py = RUNTIME_DIR / "python.exe"

    # 1. 优先从本地系统 Python 环境提取运行时文件 (100% 离线，纯本地)
    src_root = Path(sys.base_prefix)
    if src_root.is_dir() and (src_root / "python310.dll").is_file():
        print(f"[*] 正在从本地 Python 环境 ({src_root}) 提取基础运行时文件...")
        for fn in ["python.exe", "pythonw.exe", "python310.dll", "python3.dll", "vcruntime140.dll"]:
            src = src_root / fn
            if src.is_file():
                shutil.copy2(src, RUNTIME_DIR / fn)

        # 同步 DLLs 目录
        src_dlls = src_root / "DLLs"
        dst_dlls = RUNTIME_DIR / "DLLs"
        if src_dlls.is_dir():
            dst_dlls.mkdir(parents=True, exist_ok=True)
            for f in src_dlls.glob("*.pyd"):
                shutil.copy2(f, dst_dlls / f.name)
            for f in src_dlls.glob("*.dll"):
                shutil.copy2(f, dst_dlls / f.name)

        # 构建标准库 python310.zip
        target_zip = RUNTIME_DIR / "python310.zip"
        src_lib = src_root / "Lib"
        if not target_zip.is_file() and src_lib.is_dir():
            print(f"[*] 正在压缩标准库至 {target_zip.name} ...")
            with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(src_lib):
                    # 排除 site-packages 与测试缓存
                    if "site-packages" in root or "__pycache__" in root or "test" in root:
                        continue
                    rel_dir = Path(root).relative_to(src_lib)
                    for file in files:
                        if file.endswith((".py", ".pyc")):
                            zf.write(Path(root) / file, rel_dir / file)

        return (RUNTIME_DIR / "python.exe").is_file()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / "python-3.10.11-embed-amd64.zip"

    # 2. 备用：从本地缓存或镜像源下载
    if not zip_path.is_file() or zip_path.stat().st_size < 5_000_000:
        downloaded = False
        for url in PYTHON_EMBED_URLS:
            try:
                print(f"[*] 正在从 {url} 下载 Python 3.10 嵌入式运行时...")
                urllib.request.urlretrieve(url, zip_path)
                if zip_path.is_file() and zip_path.stat().st_size > 5_000_000:
                    downloaded = True
                    print("[+] 下载成功！")
                    break
            except Exception as e:
                print(f"[-] 下载失败 ({url}): {e}")

        if not downloaded:
            return False

    # 解压 zip
    print(f"[*] 正在解压缩 Python 嵌入式运行时至: {RUNTIME_DIR}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(RUNTIME_DIR)

    return (RUNTIME_DIR / "python.exe").is_file()


def setup_pth_file():
    """配置 python310._pth 定向文件，使嵌入式 Python 支持 site-packages 与项目根目录导入"""
    pth_file = RUNTIME_DIR / "python310._pth"
    pth_content = (
        "python310.zip\n"
        ".\n"
        "DLLs\n"
        "Lib\\site-packages\n"
        "..\\..\\..\n"
        "import site\n"
    )
    with open(pth_file, "w", encoding="utf-8") as f:
        f.write(pth_content)
    print(f"[+] 配置完成: {pth_file}")


def sync_site_packages():
    """将 .venv 中的 pre-built 依赖同步至 runtime/Lib/site-packages"""
    src_site = VENV_DIR / "Lib" / "site-packages"
    dst_site = RUNTIME_DIR / "Lib" / "site-packages"

    if not src_site.is_dir():
        print(f"[-] 警告: 未找到源虚拟环境 site-packages 目录: {src_site}")
        return False

    dst_site.mkdir(parents=True, exist_ok=True)
    print(f"[*] 正在同步依赖项: {src_site} -> {dst_site} ...")

    # 忽略 setuptools / pip 临时安装缓存与测试用例以精简体积
    def ignore_patterns(folder, files):
        ignored = []
        for f in files:
            if f.endswith((".pyc", ".dist-info.tmp")) or f == "__pycache__":
                ignored.append(f)
            elif f in ["pip", "setuptools", "wheel", "pkg_resources"]:
                ignored.append(f)
        return ignored

    for item in src_site.iterdir():
        if item.name in ["pip", "setuptools", "wheel", "pkg_resources", "__pycache__"]:
            continue
        dst_item = dst_site / item.name
        if not dst_item.exists():
            if item.is_dir():
                shutil.copytree(item, dst_item, ignore=ignore_patterns)
            else:
                shutil.copy2(item, dst_item)

    print("[+] 依赖项同步完成。")
    return True


def verify_models():
    """校验 SenseVoice 模型完整性"""
    model_dir = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    model_onnx = model_dir / "model.int8.onnx"
    tokens_txt = model_dir / "tokens.txt"

    if model_onnx.is_file() and tokens_txt.is_file():
        size_mb = model_onnx.stat().st_size / (1024 * 1024)
        print(f"[+] 离线 ASR 模型校验通过: {model_onnx.name} ({size_mb:.1f} MB)")
        return True

    print(f"[-] 离线模型文件缺失，尝试自动触发下载...")
    try:
        from .model_download import download_sensevoice_small
        return download_sensevoice_small(model_dir)
    except Exception as e:
        print(f"[-] 下载 SenseVoice 模型失败: {e}")
        return False


def main():
    print("=" * 65)
    print("      CC Relay - ASR 离线自包含运行环境准备工具")
    print("=" * 65)

    py_exe = RUNTIME_DIR / "python.exe"

    # 若已经可用则直接跳过
    if test_runtime_interpreter(py_exe):
        print(f"[+] 便携运行环境已完整就绪: {py_exe}")
        if verify_models():
            print("[+] 全部 ASR 资源就绪，可以打包！")
            return 0
        return 1

    # 1. 确保基础 Python 嵌入式环境
    if not ensure_embed_python():
        print("[-] 无法准备 Python 基础运行时！")
        return 1

    # 2. 配置 ._pth 导入规则
    setup_pth_file()

    # 3. 复制依赖库
    if not sync_site_packages():
        print("[-] 依赖库同步失败！")
        return 1

    # 4. 再次验证解释器与依赖
    print("[*] 正在验证打包环境可用性...")
    if not test_runtime_interpreter(py_exe):
        print("[-] 嵌入式环境验证未通过，请检查依赖缺失或路径配置！")
        return 1
    print("[+] 嵌入式 Python 解释器及 sherpa_onnx 依赖验证 100% 通过！")

    # 5. 校验模型
    if not verify_models():
        print("[-] 模型文件未就绪！")
        return 1

    print("=" * 65)
    print("[SUCCESS] ASR 离线自包含环境与模型已全部准备完毕！")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
