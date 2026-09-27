"""运行环境与 Python 解释器智能探测器：
- 严格验证候选解释器的可用性 (验证能够 import numpy)，杜绝未装包的空虚拟环境导致闪退
- 优先选择功能完备的解释器，自动平滑回退
- 提供毫秒级高效依赖诊断报告 (importlib.util.find_spec)
- 提供全自动依赖自愈与静默安装 (ensure_voice_dependencies)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _probe_interpreter(py_path: str) -> bool:
    """验证目标解释器是否能正常启动并具备基础运行库 (numpy)"""
    if not py_path:
        return False
    try:
        # 执行微型标准库探针，验证 numpy 可用且解释器无缺失运行时
        res = subprocess.run(
            [py_path, "-c", "import numpy"],
            capture_output=True,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def find_voice_python(repo_root: Optional[Path] = None) -> str:
    """按优先级智能寻找能够正常运行语音伴侣的 Python 解释器路径"""
    if repo_root is None:
        repo_root = get_repo_root()

    candidates = []

    # 1. 优先检查环境变量显式覆盖
    env_py = os.environ.get("CC_VOICE_PYTHON")
    if env_py:
        candidates.append(env_py)

    # 2. 检查 tools/voice_input/.venv 专属独立虚拟环境
    venv_py_win = repo_root / "tools" / "voice_input" / ".venv" / "Scripts" / "python.exe"
    if venv_py_win.is_file():
        candidates.append(str(venv_py_win.resolve()))

    venv_py_nix = repo_root / "tools" / "voice_input" / ".venv" / "bin" / "python"
    if venv_py_nix.is_file():
        candidates.append(str(venv_py_nix.resolve()))

    # 3. 检查当前主进程的 sys.executable (非打包环境)
    if not getattr(sys, "frozen", False) and sys.executable:
        candidates.append(sys.executable)

    # 4. 检查全局 python 命令
    candidates.append("python")

    # 逐一探针测试候选者
    for cand in candidates:
        if _probe_interpreter(cand):
            return cand

    # 若所有候选探针都未完全通过，回退至当前 sys.executable 或 python
    if not getattr(sys, "frozen", False) and sys.executable:
        return sys.executable
    return "python"


def diagnose_python_environment(python_exe: Optional[str] = None) -> Dict[str, bool]:
    """探测指定解释器中各项核心语音依赖的就绪状态 (毫秒级 find_spec 探测)"""
    py_exe = python_exe or find_voice_python()
    modules = [
        "funasr",
        "torch",
        "torchaudio",
        "webrtcvad",
        "websockets",
        "sounddevice",
        "numpy",
        "pynput",
        "win32gui",
        "tkinter",
    ]

    code = (
        "import importlib.util, json\n"
        "mods = ['funasr', 'torch', 'torchaudio', 'webrtcvad', 'websockets', "
        "'sounddevice', 'numpy', 'pynput', 'win32gui', 'tkinter']\n"
        "res = {m: importlib.util.find_spec(m) is not None for m in mods}\n"
        "print('__JSON_START__' + json.dumps(res))\n"
    )

    try:
        res = subprocess.run(
            [py_exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and "__JSON_START__" in res.stdout:
            json_part = res.stdout.split("__JSON_START__")[-1].strip()
            return json.loads(json_part)
    except Exception:
        pass

    # 若子进程探测失败，直接返回全部 False，绝不混淆宿主环境
    return {m: False for m in modules}


def check_deps_ready(python_exe: Optional[str] = None) -> bool:
    """快速检查必要桌面语音依赖是否全部就绪"""
    diag = diagnose_python_environment(python_exe)
    required = ["numpy", "sounddevice", "pynput", "websockets", "win32gui"]
    return all(diag.get(m, False) for m in required)


def ensure_voice_dependencies(python_exe: Optional[str] = None, timeout: int = 120) -> bool:
    """自动自检并静默补齐语音伴侣缺失的桌面依赖 (sounddevice / pynput)

    - 幂等执行：若所有必要依赖已就绪，立即返回 True，零开销；
    - 智能适配系统代理，确保 pip 下载顺畅；
    - 仅在确实缺失依赖时触发安装。
    """
    py_exe = python_exe or find_voice_python()
    if check_deps_ready(py_exe):
        return True

    repo_root = get_repo_root()
    req_file = repo_root / "tools" / "voice_input" / "requirements-desktop.txt"
    if not req_file.is_file():
        req_file = repo_root / "tools" / "voice_input" / "requirements.txt"
    if not req_file.is_file():
        return False

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # 自适应代理配置
    if "127.0.0.1:7900" in os.environ.get("HTTP_PROXY", "") or "127.0.0.1:7900" in os.environ.get("ALL_PROXY", ""):
        env["HTTP_PROXY"] = "http://127.0.0.1:7900"
        env["HTTPS_PROXY"] = "http://127.0.0.1:7900"

    cmd = [
        py_exe,
        "-m",
        "pip",
        "install",
        "-r",
        str(req_file),
    ]

    try:
        res = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
        if res.returncode == 0:
            return check_deps_ready(py_exe)
    except Exception:
        pass
    return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Voice Runtime Manager")
    parser.add_argument("--ensure-deps", action="store_true", help="Ensure desktop voice dependencies are installed")
    args = parser.parse_args()
    if args.ensure_deps:
        ok = ensure_voice_dependencies()
        sys.exit(0 if ok else 1)
    else:
        p = find_voice_python()
        print("Resolved Python:", p)
        print("Diagnostics:", json.dumps(diagnose_python_environment(p), indent=2))
