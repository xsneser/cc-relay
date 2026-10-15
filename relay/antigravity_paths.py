# -*- coding: utf-8 -*-
"""Antigravity Tools (Gemini 伴侣) 可执行文件定位与路径探测工具。"""

import os
import shutil
import sys
from typing import Any, Dict, List, Optional


def _normalize_candidate(raw_path: Any) -> Optional[str]:
    """清洗并验证可执行文件路径候选值，若为有效文件则返回规范化绝对路径，否则返回 None。"""
    if not isinstance(raw_path, str):
        return None
    s = raw_path.strip()
    if not s:
        return None
    # 去除首尾匹配的双引号或单引号
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if not s or "\x00" in s:
        return None
    try:
        expanded = os.path.expanduser(os.path.expandvars(s))
        abs_p = os.path.abspath(expanded)
        if os.path.isfile(abs_p) or (os.path.exists(abs_p) and not os.path.isdir(abs_p)):
            if sys.platform != "win32" and not os.access(abs_p, os.X_OK):
                return None
            return abs_p
    except Exception:
        pass
    return None


def get_standard_antigravity_candidates() -> List[str]:
    """获取当前操作系统平台下 Antigravity Tools 常见的默认安装路径列表。"""
    candidates: List[str] = []

    if sys.platform == "win32":
        # Windows: LocalAppData (官方默认安装路径)、AppData、Program Files
        roots = [
            os.environ.get("LOCALAPPDATA"),
            os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs"),
            os.environ.get("APPDATA"),
            os.path.join(os.environ.get("APPDATA") or "", "Programs"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ]
        for r in roots:
            if r and os.path.isdir(r):
                candidates.append(os.path.join(r, "Antigravity Tools", "antigravity-tools.exe"))
    elif sys.platform == "darwin":
        # macOS
        candidates.extend([
            "/Applications/Antigravity Tools.app/Contents/MacOS/Antigravity Tools",
            os.path.expanduser("~/Applications/Antigravity Tools.app/Contents/MacOS/Antigravity Tools"),
        ])
    else:
        # Linux / Unix
        candidates.extend([
            os.path.expanduser("~/.local/bin/antigravity-tools"),
            "/usr/local/bin/antigravity-tools",
            "/usr/bin/antigravity-tools",
        ])

    return candidates


def find_antigravity_exe(conf: Optional[Dict[str, Any]] = None, allow_fallback: bool = False) -> str:
    """解析并定位 Antigravity Tools 可执行文件的绝对路径。

    探测优先级:
      1. conf["antigravity_exe"] (顶层显式配置)
      2. conf["tools"]["antigravity"]["exe"] (工具嵌套配置)
      3. 若未显式配置 (或显式配置无效且 allow_fallback=True)，自动探测系统标准安装目录 (%LOCALAPPDATA%\\Antigravity Tools\\...)
      4. 系统环境变量 PATH (antigravity-tools / antigravity-tools.exe)

    若均未匹配到有效可执行文件，返回空字符串 ""。
    """
    res = inspect_antigravity_exe(conf, allow_fallback=allow_fallback)
    return res["path"]


def inspect_antigravity_exe(conf: Optional[Dict[str, Any]] = None, allow_fallback: bool = False) -> Dict[str, Any]:
    """详细探测 Antigravity Tools 路径及来源元数据。"""
    conf = conf or {}
    raw_cfg_top = conf.get("antigravity_exe")
    raw_cfg_nested = None
    tools = conf.get("tools")
    if isinstance(tools, dict):
        ag_tool = tools.get("antigravity")
        if isinstance(ag_tool, dict):
            raw_cfg_nested = ag_tool.get("exe")

    effective_raw_cfg = raw_cfg_top if (raw_cfg_top and str(raw_cfg_top).strip()) else raw_cfg_nested
    cfg_path_str = str(effective_raw_cfg).strip() if effective_raw_cfg else ""

    # 1. 尝试显式配置项
    if raw_cfg_top and str(raw_cfg_top).strip():
        norm = _normalize_candidate(raw_cfg_top)
        if norm:
            return {
                "path": norm,
                "source": "antigravity_exe",
                "configured_path": cfg_path_str,
                "configured_valid": True,
                "is_fallback": False,
            }
        elif not allow_fallback:
            return {
                "path": "",
                "source": "antigravity_exe",
                "configured_path": cfg_path_str,
                "configured_valid": False,
                "is_fallback": False,
            }

    if raw_cfg_nested and str(raw_cfg_nested).strip():
        norm = _normalize_candidate(raw_cfg_nested)
        if norm:
            return {
                "path": norm,
                "source": "tools.antigravity.exe",
                "configured_path": cfg_path_str,
                "configured_valid": True,
                "is_fallback": False,
            }
        elif not allow_fallback:
            return {
                "path": "",
                "source": "tools.antigravity.exe",
                "configured_path": cfg_path_str,
                "configured_valid": False,
                "is_fallback": False,
            }

    # 2. 尝试系统标准安装路径
    for cand in get_standard_antigravity_candidates():
        norm = _normalize_candidate(cand)
        if norm:
            return {
                "path": norm,
                "source": "standard",
                "configured_path": cfg_path_str,
                "configured_valid": False,
                "is_fallback": bool(cfg_path_str),
            }

    # 3. 尝试 PATH 环境变量
    names = ["antigravity-tools.exe", "antigravity-tools"] if sys.platform == "win32" else ["antigravity-tools"]
    for name in names:
        which_path = shutil.which(name)
        if which_path:
            norm = _normalize_candidate(which_path)
            if norm:
                return {
                    "path": norm,
                    "source": "path",
                    "configured_path": cfg_path_str,
                    "configured_valid": False,
                    "is_fallback": bool(cfg_path_str),
                }

    return {
        "path": "",
        "source": "missing",
        "configured_path": cfg_path_str,
        "configured_valid": False,
        "is_fallback": False,
    }
