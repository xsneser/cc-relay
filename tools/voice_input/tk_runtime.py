"""Tcl/Tk 运行时辅助模块：定位、自愈与验证 Tkinter 运行环境。

确保无论在便携式嵌入环境、虚拟环境还是独立安装包中，
桌面悬浮胶囊均能找到完整的 Tcl/Tk 脚本资源。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _is_valid_tcl_dir(p: Path) -> bool:
    """检查是否为包含 init.tcl 的有效 Tcl 脚本目录"""
    try:
        return p.is_dir() and (p / "init.tcl").is_file()
    except Exception:
        return False


def _is_valid_tk_dir(p: Path) -> bool:
    """检查是否为包含 tk.tcl 的有效 Tk 脚本目录"""
    try:
        return p.is_dir() and (p / "tk.tcl").is_file()
    except Exception:
        return False


def _find_pair_in_tcl_root(root: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """在指定的根目录下寻找匹配的 tcl8.x 与 tk8.x 目录对"""
    if not root.is_dir():
        return None, None

    tcl_cand = root / "tcl8.6"
    tk_cand = root / "tk8.6"
    if _is_valid_tcl_dir(tcl_cand) and _is_valid_tk_dir(tk_cand):
        return tcl_cand, tk_cand

    # 模糊匹配版本
    found_tcl = None
    found_tk = None
    try:
        for item in sorted(root.iterdir()):
            if item.is_dir():
                if item.name.startswith("tcl") and _is_valid_tcl_dir(item):
                    found_tcl = item
                elif item.name.startswith("tk") and _is_valid_tk_dir(item):
                    found_tk = item
    except Exception:
        pass

    return found_tcl, found_tk


def find_tcl_tk_dirs(py_exe: Optional[str] = None) -> Tuple[Optional[Path], Optional[Path]]:
    """智能探测并返回 (tcl_dir, tk_dir) 路径对。

    优先级：
    1. py_exe 所在目录及子目录 (runtime/tcl, runtime/lib 等)
    2. 项目内部的 tools/voice_input/runtime/tcl
    3. 当前运行环境 sys.base_prefix / "tcl"
    4. 当前解释器目录 (sys.executable 对应目录)
    5. 虚拟环境 pyvenv.cfg 中指明的 home 目录
    6. Windows 用户目录下的 Python310 安装路径
    """
    candidates_roots: List[Path] = []

    # 1. 目标 py_exe 对应路径
    if py_exe:
        try:
            exe_p = Path(py_exe).resolve()
            exe_dir = exe_p.parent
            candidates_roots.extend([
                exe_dir / "tcl",
                exe_dir / "lib",
                exe_dir,
            ])
        except Exception:
            pass

    # 2. 项目内 tools/voice_input/runtime/tcl
    try:
        repo_runtime_tcl = Path(__file__).resolve().parent / "runtime" / "tcl"
        candidates_roots.append(repo_runtime_tcl)
    except Exception:
        pass

    # 3. sys.base_prefix / "tcl"
    try:
        base_prefix = Path(sys.base_prefix)
        candidates_roots.append(base_prefix / "tcl")
        candidates_roots.append(base_prefix / "lib")
    except Exception:
        pass

    # 4. sys.executable 对应目录
    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates_roots.append(exe_dir / "tcl")
        candidates_roots.append(exe_dir / "lib")
    except Exception:
        pass

    # 5. 检查 pyvenv.cfg 关联的原始 Python 安装目录
    try:
        venv_cfg = Path(sys.prefix) / "pyvenv.cfg"
        if venv_cfg.is_file():
            with open(venv_cfg, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().startswith("home"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            home_p = Path(parts[1].strip())
                            candidates_roots.append(home_p / "tcl")
                            candidates_roots.append(home_p / "lib")
    except Exception:
        pass

    # 6. Windows 标准安装目录扫描
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            py_progs = Path(local_app) / "Programs" / "Python"
            if py_progs.is_dir():
                try:
                    for p in sorted(py_progs.glob("Python3*"), reverse=True):
                        candidates_roots.append(p / "tcl")
                except Exception:
                    pass

        prog_files = os.environ.get("ProgramFiles")
        if prog_files:
            py_prog = Path(prog_files) / "Python310"
            candidates_roots.append(py_prog / "tcl")

    # 去重按序搜索
    seen = set()
    for root in candidates_roots:
        resolved = str(root)
        if resolved in seen:
            continue
        seen.add(resolved)

        tcl_dir, tk_dir = _find_pair_in_tcl_root(root)
        if tcl_dir and tk_dir:
            return tcl_dir, tk_dir

    return None, None


def setup_tk_environment(py_exe: Optional[str] = None) -> Dict[str, str]:
    """自愈配置 TCL_LIBRARY 与 TK_LIBRARY 环境变量。

    若当前已有有效配置则保留，否则寻找可用目录并注入 os.environ。
    返回所设置或已存在的环境变量字典。
    """
    env_vars: Dict[str, str] = {}

    curr_tcl = os.environ.get("TCL_LIBRARY")
    curr_tk = os.environ.get("TK_LIBRARY")

    tcl_ok = curr_tcl and _is_valid_tcl_dir(Path(curr_tcl))
    tk_ok = curr_tk and _is_valid_tk_dir(Path(curr_tk))

    if tcl_ok and tk_ok:
        return {"TCL_LIBRARY": curr_tcl, "TK_LIBRARY": curr_tk}

    tcl_dir, tk_dir = find_tcl_tk_dirs(py_exe)
    if tcl_dir and not tcl_ok:
        os.environ["TCL_LIBRARY"] = str(tcl_dir)
        env_vars["TCL_LIBRARY"] = str(tcl_dir)
    elif curr_tcl:
        env_vars["TCL_LIBRARY"] = curr_tcl

    if tk_dir and not tk_ok:
        os.environ["TK_LIBRARY"] = str(tk_dir)
        env_vars["TK_LIBRARY"] = str(tk_dir)
    elif curr_tk:
        env_vars["TK_LIBRARY"] = curr_tk

    return env_vars


def validate_tk_runtime(py_exe: Optional[str] = None, timeout: float = 4.0) -> bool:
    """验证目标解释器（或当前进程环境）是否能够成功初始化 Tkinter 视窗系统"""
    # 确保本进程已尝试自愈环境变量
    env_overrides = setup_tk_environment(py_exe)

    if py_exe:
        code = (
            "import os, sys\n"
            "from tools.voice_input.tk_runtime import setup_tk_environment\n"
            "setup_tk_environment()\n"
            "import tkinter\n"
            "root = tkinter.Tk()\n"
            "root.withdraw()\n"
            "root.destroy()\n"
            "print('TK_OK')\n"
        )
        try:
            env = dict(os.environ)
            env.update(env_overrides)
            res = subprocess.run(
                [py_exe, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return res.returncode == 0 and "TK_OK" in res.stdout
        except Exception:
            return False
    else:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            root.destroy()
            return True
        except Exception:
            return False
