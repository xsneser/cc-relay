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
from typing import Callable, Dict, Optional, Tuple


_SILENT_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _probe_interpreter(py_path: str) -> bool:
    """验证目标解释器是否能正常启动并具备基础运行库 (numpy)"""
    if not py_path:
        return False
    try:
        res = subprocess.run(
            [py_path, "-c", "import numpy"],
            capture_output=True,
            timeout=3,
            creationflags=_SILENT_FLAGS,
        )
        return res.returncode == 0
    except Exception:
        return False


def _probe_desktop_capability(py_path: str) -> bool:
    """静态验证目标解释器包含 Tkinter 与 Tcl/Tk 脚本资源，不创建临时 GUI 窗口"""
    if not py_path or sys.platform != "win32":
        return True
    code = (
        "import tkinter\n"
        "from tools.voice_input.tk_runtime import find_tcl_tk_dirs\n"
        "tcl_dir, tk_dir = find_tcl_tk_dirs()\n"
        "assert tcl_dir and (tcl_dir / 'init.tcl').is_file()\n"
        "assert tk_dir and (tk_dir / 'tk.tcl').is_file()\n"
        "print('TK_STATIC_OK')\n"
    )
    try:
        res = subprocess.run(
            [py_path, "-c", code],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=_SILENT_FLAGS,
        )
        return res.returncode == 0 and "TK_STATIC_OK" in res.stdout
    except Exception:
        return False


def _score_interpreter(py_path: str) -> int:
    """评估候选解释器的语音核心依赖就绪度（得分越高越完备，优先选择可用依赖最多的环境）"""
    if not py_path:
        return -1
    code = (
        "import importlib.util\n"
        "mods = ['numpy', 'sounddevice', 'pynput', 'websockets', 'sherpa_onnx', 'torch', 'transformers']\n"
        "print(sum(1 for m in mods if importlib.util.find_spec(m) is not None))\n"
    )
    try:
        res = subprocess.run(
            [py_path, "-c", code],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=_SILENT_FLAGS,
        )
        if res.returncode == 0 and res.stdout.strip().isdigit():
            return int(res.stdout.strip())
    except Exception:
        pass
    return -1


def _check_module_in_interpreter(py_path: str, module_name: str) -> bool:
    """快速探测解释器中是否安装了指定模块"""
    if not py_path:
        return False
    code = f"import importlib.util; exit(0 if importlib.util.find_spec('{module_name}') is not None else 1)"
    try:
        res = subprocess.run(
            [py_path, "-c", code],
            capture_output=True,
            timeout=2,
            creationflags=_SILENT_FLAGS,
        )
        return res.returncode == 0
    except Exception:
        return False


def find_voice_python(repo_root: Optional[Path] = None, engine: Optional[str] = None, require_desktop: bool = False) -> str:
    """按依赖完备度与目标引擎诉求智能寻找最匹配能够运行该 ASR 引擎的 Python 解释器路径"""
    if repo_root is None:
        repo_root = get_repo_root()

    if engine is None:
        try:
            cfg_file = repo_root / "config.json"
            if cfg_file.is_file():
                import json
                with open(cfg_file, "r", encoding="utf-8") as f:
                    conf = json.load(f)
                engine = conf.get("tools", {}).get("voice", {}).get("engine")
        except Exception:
            pass

    target_mod = None
    if engine:
        eng_lower = str(engine).lower()
        if "qwen" in eng_lower or "paraformer" in eng_lower:
            target_mod = "transformers"
        elif "sensevoice" in eng_lower or "sherpa" in eng_lower:
            target_mod = "sherpa_onnx"

    candidates = []

    # 1. 优先检查环境变量显式覆盖
    env_py = os.environ.get("CC_VOICE_PYTHON")
    if env_py:
        candidates.append(env_py)

    # 2. 检查安装包内置的自包含便携运行时 (tools/voice_input/runtime/python.exe)
    bundled_py_win = repo_root / "tools" / "voice_input" / "runtime" / "python.exe"
    if bundled_py_win.is_file():
        candidates.append(str(bundled_py_win.resolve()))

    # 3. 检查 tools/voice_input/.venv 专属独立虚拟环境 (开发调试环境)
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

    # 去重保留顺序
    unique_candidates = []
    seen = set()
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    # Memoize candidate results within this resolution pass; fallback passes reuse probes.
    interpreter_results: Dict[str, bool] = {}
    module_results: Dict[Tuple[str, str], bool] = {}
    desktop_results: Dict[str, bool] = {}

    def interpreter_ok(candidate: str) -> bool:
        if candidate not in interpreter_results:
            interpreter_results[candidate] = _probe_interpreter(candidate)
        return interpreter_results[candidate]

    def module_ok(candidate: str, module: str) -> bool:
        key = (candidate, module)
        if key not in module_results:
            module_results[key] = _check_module_in_interpreter(candidate, module)
        return module_results[key]

    def desktop_ok(candidate: str) -> bool:
        if candidate not in desktop_results:
            desktop_results[candidate] = _probe_desktop_capability(candidate)
        return desktop_results[candidate]

    # 优先匹配：满足目标引擎核心模块 + 桌面静态能力（若需要）
    if target_mod:
        if require_desktop:
            for cand in unique_candidates:
                if interpreter_ok(cand) and module_ok(cand, target_mod) and desktop_ok(cand):
                    return cand
        for cand in unique_candidates:
            if interpreter_ok(cand) and module_ok(cand, target_mod):
                return cand

    # 其次按优先级探针测试候选者
    if require_desktop:
        for cand in unique_candidates:
            if interpreter_ok(cand) and desktop_ok(cand):
                return cand

    for cand in unique_candidates:
        if interpreter_ok(cand):
            return cand

    # 最终保底
    if not getattr(sys, "frozen", False) and sys.executable:
        return sys.executable
    return "python"


_DIAGNOSTIC_MODULES = (
    "sherpa_onnx", "transformers", "torch", "torchaudio", "modelscope", "webrtcvad",
    "websockets", "sounddevice", "numpy", "pynput", "win32gui", "tkinter", "PIL",
)
_REQUIRED_DESKTOP_MODULES = ("numpy", "sounddevice", "pynput", "websockets", "win32gui")


def _probe_dependency_report(python_exe: str, timeout: float = 5.0) -> Tuple[Optional[Dict[str, bool]], Optional[str]]:
    """静态探测依赖；不创建 Tk 窗口。返回 None report 表示探测进程本身失败。"""
    mods = repr(list(_DIAGNOSTIC_MODULES))
    code = (
        "import importlib.util, json\n"
        f"mods = {mods}\n"
        "res = {m: importlib.util.find_spec(m) is not None for m in mods}\n"
        "print('__JSON_START__' + json.dumps(res))\n"
    )
    try:
        res = subprocess.run(
            [python_exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_SILENT_FLAGS,
        )
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or f"probe exited {res.returncode}").strip()[-1200:]
            return None, detail
        if "__JSON_START__" not in res.stdout:
            return None, "dependency probe returned no result marker"
        data = json.loads(res.stdout.split("__JSON_START__")[-1].strip())
        if not isinstance(data, dict):
            return None, "dependency probe returned invalid data"
        return {name: bool(data.get(name, False)) for name in _DIAGNOSTIC_MODULES}, None
    except Exception as exc:
        return None, str(exc)


def diagnose_python_environment(python_exe: Optional[str] = None) -> Dict[str, bool]:
    """静态探测解释器中的模块可用性，不会创建临时 Tk 窗口。"""
    py_exe = python_exe or find_voice_python()
    report, _error = _probe_dependency_report(py_exe)
    return report if report is not None else {name: False for name in _DIAGNOSTIC_MODULES}


def check_deps_ready(python_exe: Optional[str] = None) -> bool:
    """快速检查必要桌面语音依赖是否全部就绪。"""
    diag = diagnose_python_environment(python_exe)
    return all(diag.get(name, False) for name in _REQUIRED_DESKTOP_MODULES)


def ensure_voice_dependencies(
    python_exe: Optional[str] = None,
    timeout: int = 120,
    on_progress: Optional[Callable[[str, Dict[str, object]], None]] = None,
) -> bool:
    """检查并按需修复桌面依赖；报告探测/安装阶段，保留有界 pip 超时。"""
    py_exe = python_exe or find_voice_python()

    def report(phase: str, **details) -> None:
        if on_progress:
            try:
                on_progress(phase, details)
            except Exception:
                pass

    report("checking_dependencies", interpreter=py_exe)
    diag, probe_error = _probe_dependency_report(py_exe)
    if diag is None:
        report("dependency_probe_failed", error=probe_error or "unknown probe error")
        return False

    missing = [name for name in _REQUIRED_DESKTOP_MODULES if not diag.get(name, False)]
    if not missing:
        report("dependencies_ready", missing=[])
        return True

    report("installing_dependencies", missing=missing)
    repo_root = get_repo_root()
    req_file = repo_root / "tools" / "voice_input" / "requirements-desktop.txt"
    if not req_file.is_file():
        req_file = repo_root / "tools" / "voice_input" / "requirements.txt"
    if not req_file.is_file():
        report("dependency_install_failed", missing=missing, error="requirements file not found")
        return False

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if "127.0.0.1:7900" in os.environ.get("HTTP_PROXY", "") or "127.0.0.1:7900" in os.environ.get("ALL_PROXY", ""):
        env["HTTP_PROXY"] = "http://127.0.0.1:7900"
        env["HTTPS_PROXY"] = "http://127.0.0.1:7900"

    cmd = [py_exe, "-m", "pip", "install", "-r", str(req_file)]
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=timeout,
            creationflags=_SILENT_FLAGS,
        )
    except Exception as exc:
        report("dependency_install_failed", missing=missing, error=str(exc))
        return False

    if result.returncode != 0:
        raw_detail = result.stderr or result.stdout or f"pip exited {result.returncode}"
        detail = raw_detail.decode("utf-8", errors="replace") if isinstance(raw_detail, bytes) else str(raw_detail)
        report("dependency_install_failed", missing=missing, error=detail[-1200:])
        return False

    diag, probe_error = _probe_dependency_report(py_exe)
    if diag is None:
        report("dependency_recheck_failed", missing=missing, error=probe_error or "unknown probe error")
        return False
    remaining = [name for name in _REQUIRED_DESKTOP_MODULES if not diag.get(name, False)]
    if remaining:
        report("dependency_install_incomplete", missing=remaining)
        return False
    report("dependencies_ready", installed=missing)
    return True


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
