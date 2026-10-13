# -*- coding: utf-8 -*-
"""cc-relay 核心常量、路径与基础同步原语"""
import os
import sys
import threading
import time

BASE = os.environ.get("CC_RELAY_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("CC_RELAY_DATA_DIR") or os.path.join(BASE, "data")
LOGS_DIR = os.environ.get("CC_RELAY_LOGS_DIR") or os.path.join(BASE, "logs")

CONF = os.path.join(BASE, "config.json")
RECORDS = os.path.join(DATA_DIR, "records.jsonl")
PROMPTS_FILE = os.path.join(DATA_DIR, "prompts.json")
VOICE_PID_FILE = os.path.join(DATA_DIR, ".voice.pid")
WATCH_PID_FILE = os.path.join(DATA_DIR, ".watch.pid")

VOICE_OUT_LOG = os.path.join(LOGS_DIR, "voice.out.log")
VOICE_ERR_LOG = os.path.join(LOGS_DIR, "voice.err.log")
RELAY_LAUNCH_LOG = os.path.join(LOGS_DIR, "relay_launch.log")

LOCK = threading.Lock()
_IDX = [0]
_UP = {"last": "", "last_model": "", "last_ts": 0}
_PROCESS_INSTANCE_ID = f"{os.getpid()}-{time.time_ns()}"

DEFAULT_RELAY_PORT = 8400
DEFAULT_UI_PORT = 8610
DEFAULT_VOICE_PORT = 8401

UPSTREAM_DEGRADED_TTL = 30.0


def ensure_runtime_dirs_and_migrate():
    """确保 data/ 与 logs/ 目录存在，并在启动时无缝迁移根目录下遗留的历史数据与日志文件"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    import shutil

    # 数据文件平滑迁移
    for root_name, target_path in [
        ("records.jsonl", RECORDS),
        ("prompts.json", PROMPTS_FILE),
        ("relay-status.json", os.path.join(DATA_DIR, "relay-status.json")),
        (".update-status.json", os.path.join(DATA_DIR, ".update-status.json")),
        (".voice.pid", VOICE_PID_FILE),
        (".watch.pid", WATCH_PID_FILE),
    ]:
        root_file = os.path.join(BASE, root_name)
        if os.path.isfile(root_file) and os.path.abspath(root_file) != os.path.abspath(target_path):
            try:
                if not os.path.exists(target_path):
                    shutil.move(root_file, target_path)
            except Exception:
                pass

    # 日志文件平滑迁移
    for root_log, target_log in [
        ("voice.out.log", VOICE_OUT_LOG),
        ("voice.err.log", VOICE_ERR_LOG),
        ("relay_launch.log", RELAY_LAUNCH_LOG),
        ("serve.log", os.path.join(LOGS_DIR, "serve.log")),
        ("sentinel.log", os.path.join(LOGS_DIR, "sentinel.log")),
        ("_diag.log", os.path.join(LOGS_DIR, "_diag.log")),
    ]:
        old_log = os.path.join(BASE, root_log)
        if os.path.isfile(old_log) and os.path.abspath(old_log) != os.path.abspath(target_log):
            try:
                if not os.path.exists(target_log):
                    shutil.move(old_log, target_log)
            except Exception:
                pass


ensure_runtime_dirs_and_migrate()


def get_conf_path() -> str:
    """动态获取当前有效的配置文件路径，支持单元测试 monkey-patch cc_relay.CONF"""
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, "CONF"):
        return getattr(mod, "CONF")
    return CONF


def get_records_path() -> str:
    """动态获取当前有效的请求记录路径，支持单元测试 monkey-patch cc_relay.RECORDS"""
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, "RECORDS"):
        return getattr(mod, "RECORDS")
    return RECORDS


def get_prompts_path() -> str:
    """动态获取当前有效的提示词文件路径，支持单元测试 monkey-patch cc_relay.PROMPTS_FILE"""
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, "PROMPTS_FILE"):
        return getattr(mod, "PROMPTS_FILE")
    return PROMPTS_FILE


def _tcp(port, host="127.0.0.1", t=0.6):
    """简单 TCP 端口探测"""
    import socket
    try:
        s = socket.create_connection((host, int(port)), timeout=t)
        s.close()
        return True
    except Exception:
        return False

