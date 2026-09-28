"""一键初始化脚本：使用 Python 保证全平台中文与环境搭建 100% 稳定运行"""

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

# 设置 Windows 控制台为 UTF-8
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
VENV_DIR = BASE_DIR / ".venv"
REQ_FILE = BASE_DIR / "requirements.txt"


def get_venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def main():
    print("=" * 65)
    print("   Claude Code CLI 中文语音输入伴侣 - 一键环境与模型配置")
    print("=" * 65)

    # 1. 检查 Python 版本
    py_ver = sys.version_info
    print(f"[*] 当前系统 Python 版本: {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    if py_ver < (3, 8):
        print("[-] Python 版本过低，请升级至 Python 3.8 或更高版本。")
        sys.exit(1)

    # 准备环境变量 (开启 UTF-8 支持与继承系统代理)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    # 2. 创建独立虚拟环境
    print("\n[*] 正在准备专属独立虚拟环境 (.venv)...")
    venv_py = get_venv_python()
    if not venv_py.is_file():
        print(f"    -> 正在创建虚拟环境: {VENV_DIR}")
        try:
            venv.create(VENV_DIR, with_pip=True)
            print("    [+] 虚拟环境创建成功！")
        except Exception as e:
            print(f"    [-] 创建虚拟环境失败: {e}")
            sys.exit(1)
    else:
        print(f"    [+] 虚拟环境已存在: {VENV_DIR}")

    # 3. 安装依赖包 (直连官方源 + 支持代理/信任主机)
    print("\n[*] 正在专属虚拟环境中安装必要依赖...")
    print("    -> 包含: sherpa-onnx, sounddevice, numpy, pynput, pywin32")
    print("    -> 这完全隔离在 .venv 中，不会影响 cc-relay 主服务。")

    pip_cmd = [
        str(venv_py),
        "-m",
        "pip",
        "install",
        "-r",
        str(REQ_FILE),
        "--trusted-host",
        "pypi.org",
        "--trusted-host",
        "files.pythonhosted.org",
    ]
    res = subprocess.run(pip_cmd, env=env)
    if res.returncode != 0:
        print("    [!] 标准 pip 安装遇到异常，正在尝试增加 --no-cache-dir 重试...")
        res = subprocess.run(pip_cmd + ["--no-cache-dir"], env=env)
        if res.returncode != 0:
            print("    [-] 依赖安装失败，请检查网络代理设置。")
            sys.exit(1)

    print("    [+] 依赖安装成功！")

    # 4. 下载 SenseVoice 离线模型 (官方 Hugging Face 直连)
    print("\n[*] 正在检查并下载 SenseVoice-Small 离线 ASR 模型 (约 239MB)...")
    download_cmd = [
        str(venv_py),
        "-m",
        "tools.voice_input",
        "download",
        "--source",
        "huggingface",
    ]
    res = subprocess.run(download_cmd, cwd=str(REPO_ROOT), env=env)
    if res.returncode != 0:
        print("    [!] Hugging Face 直连异常，尝试国内镜像备用源...")
        download_cmd[-1] = "hf-mirror"
        res = subprocess.run(download_cmd, cwd=str(REPO_ROOT), env=env)
        if res.returncode != 0:
            print("    [!] 国内镜像下载异常，尝试 ModelScope 魔搭社区源...")
            download_cmd[-1] = "modelscope"
            res = subprocess.run(download_cmd, cwd=str(REPO_ROOT), env=env)

    # 5. 检查模型是否成功落地
    check_cmd = [str(venv_py), "-m", "tools.voice_input.model_download", "--check"]
    model_ready = (subprocess.run(check_cmd, cwd=str(REPO_ROOT), env=env, capture_output=True).returncode == 0)

    # 6. 运行诊断
    print("\n[*] 正在执行环境自检诊断 (Doctor)...")
    doctor_cmd = [str(venv_py), "-m", "tools.voice_input", "doctor"]
    subprocess.run(doctor_cmd, cwd=str(REPO_ROOT), env=env, check=False)

    print("\n" + "=" * 65)
    if model_ready:
        print("  [+] 恭喜！语音伴侣已全部配置就绪。")
        print("  您可以直接双击运行根目录下的 start_voice.bat 启动伴侣！")
    else:
        print("  [!] 依赖安装完成，但离线模型尚未下载完毕。")
        print("  请检查网络后重新运行本脚本，或执行:")
        print(f"    {venv_py} -m tools.voice_input download --source modelscope")
    print("=" * 65)


if __name__ == "__main__":
    main()
