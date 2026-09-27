"""运行环境与 Python 解释器智能探测器：
- 优先发现 tools/voice_input/.venv/Scripts/python.exe 专属虚拟环境
- 保证全局热键 (pynput)、麦克风 (sounddevice) 及流式 ASR (funasr) 环境隔离且完整
- 提供快速依赖探测与能力报告
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def find_voice_python(repo_root: Optional[Path] = None) -> str:
    """按优先级智能寻找最适宜运行语音伴侣的 Python 解释器路径"""
    if repo_root is None:
        repo_root = get_repo_root()

    # 1. 优先检查环境变量显式覆盖
    env_py = os.environ.get("CC_VOICE_PYTHON")
    if env_py and os.path.isfile(env_py):
        return env_py

    # 2. 优先检查 tools/voice_input/.venv 专属独立虚拟环境
    venv_py_win = repo_root / "tools" / "voice_input" / ".venv" / "Scripts" / "python.exe"
    if venv_py_win.is_file():
        return str(venv_py_win.resolve())

    venv_py_nix = repo_root / "tools" / "voice_input" / ".venv" / "bin" / "python"
    if venv_py_nix.is_file():
        return str(venv_py_nix.resolve())

    # 3. 回退至当前运行的 sys.executable (非打包环境)
    if not getattr(sys, "frozen", False) and sys.executable:
        return sys.executable

    # 4. 默认系统 python
    return "python"


def diagnose_python_environment(python_exe: Optional[str] = None) -> Dict[str, bool]:
    """探测指定解释器中各项核心语音依赖的就绪状态"""
    py_exe = python_exe or find_voice_python()
    modules = [
        "funasr",
        "webrtcvad",
        "websockets",
        "sounddevice",
        "numpy",
        "pynput",
        "win32gui",
        "tkinter",
    ]

    code = "import json; " + "; ".join(
        [
            f"try:\n __import__('{m}')\n r_{m}=True\nexcept Exception:\n r_{m}=False"
            for m in modules
        ]
    ) + "; print(json.dumps({" + ", ".join([f"'{m}': r_{m}" for m in modules]) + "}))"

    try:
        res = subprocess.run(
            [py_exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            import json
            return json.loads(res.stdout.strip())
    except Exception:
        pass

    # 降级：检查当前进程内模块导入
    status = {}
    for m in modules:
        try:
            __import__(m)
            status[m] = True
        except Exception:
            status[m] = False
    return status
