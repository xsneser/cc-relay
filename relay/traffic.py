# -*- coding: utf-8 -*-
"""cc-relay 流量闸门、排队计数与 Codex 在处理请求跟踪"""
import threading
import time

from relay.config import load_conf


def traffic_paused(conf):
    """返回流量闸门状态: True 表示用户已手动暂停所有上游流量."""
    return (conf or {}).get("traffic_paused", False) is True


_TRAFFIC_UNPAUSED_EVENT = threading.Event()
_PAUSED_WAITING_COUNT = 0
_PAUSED_WAITING_LOCK = threading.Lock()

_CODEX_MAINTENANCE = threading.Condition()
_CODEX_ACTIVE_REQUESTS = 0
_CODEX_MAINTENANCE_ACTIVE = False
_CODEX_LAST_REQUEST_TS = 0.0


def _paused_waiting_inc():
    global _PAUSED_WAITING_COUNT
    with _PAUSED_WAITING_LOCK:
        _PAUSED_WAITING_COUNT += 1


def _paused_waiting_dec():
    global _PAUSED_WAITING_COUNT
    with _PAUSED_WAITING_LOCK:
        if _PAUSED_WAITING_COUNT > 0:
            _PAUSED_WAITING_COUNT -= 1


def _paused_waiting_count():
    with _PAUSED_WAITING_LOCK:
        return _PAUSED_WAITING_COUNT


def _sync_traffic_unpaused_event(conf=None):
    if conf is None:
        try:
            conf = load_conf()
        except Exception:
            conf = None
    if traffic_paused(conf):
        _TRAFFIC_UNPAUSED_EVENT.clear()
    else:
        _TRAFFIC_UNPAUSED_EVENT.set()


def _codex_request_enter():
    global _CODEX_ACTIVE_REQUESTS
    with _CODEX_MAINTENANCE:
        if _CODEX_MAINTENANCE_ACTIVE:
            return False
        _CODEX_ACTIVE_REQUESTS += 1
        return True


def _codex_request_leave():
    global _CODEX_ACTIVE_REQUESTS, _CODEX_LAST_REQUEST_TS
    with _CODEX_MAINTENANCE:
        _CODEX_ACTIVE_REQUESTS = max(0, _CODEX_ACTIVE_REQUESTS - 1)
        _CODEX_LAST_REQUEST_TS = time.time()
        _CODEX_MAINTENANCE.notify_all()


# 初始化全局事件
_sync_traffic_unpaused_event()
