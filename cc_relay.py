#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CC 统一中转 (unified relay) - 顶层门面与命令行入口
- 纯标准库实现，零第三方依赖
- 5 档分层路由仲裁与多上游智能调度:
    deepseek     -> https://api.deepseek.com/anthropic (直连)
    codex        -> CLIProxyAPI 127.0.0.1:8317 (Codex 额度, 随需自动拉起与热更新)
    antigravity  -> Antigravity Tools 127.0.0.1:8045 (Gemini, 随需自动拉起)
    custom       -> 用户自定义 OpenAI/Anthropic 兼容模型网关
- 实时请求审计流、Token 消耗与提示词缓存统计
- 语音伴侣 (Voice Companion) 进程管理与活跃终端上下文语义纠错

用法:
    python cc_relay.py serve
    python cc_relay.py stats | last [n] | dump <idx>
    python cc_relay.py startproxy | stopproxy
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay.upstreams import antigravity_updater, relay_updater
from relay import *
from relay.voice_bridge import _monitor_voice_startup


def serve(a):
    conf = load_conf()
    _sync_traffic_unpaused_event(conf)
    _get_cpa_updater(conf).start(
        auto_update=lambda: _cpa_auto_update_enabled() and _cpa_auto_update_idle(),
        installer=_install_cpa_update,
    )
    ru_conf = (conf.get("tools") or {}).get("relay_update") or {}
    if ru_conf.get("auto_check", True):
        _get_relay_updater(conf).start(auto_check=True)
    ag_conf = (conf.get("tools") or {}).get("antigravity") or {}
    if ag_conf.get("auto_check", True):
        antigravity_updater.get_antigravity_updater(conf).start(auto_check=True)
    host = conf.get("listen_host", "127.0.0.1")
    if not is_loopback_host(host):
        raise ValueError(f"Refusing to bind relay to non-loopback host '{host}'.")
    port = conf.get("listen_port", 8400)
    srv = create_exclusive_server((host, port), Relay, max_wait_sec=15.0)
    srv.reload_conf = lambda: load_conf()

    _rotate_if_needed()
    threading.Thread(target=stats_snapshot, name="WarmupStats", daemon=True).start()

    if not a.no_ui:
        ui_port = conf.get("ui_port", 8610)
        try:
            ui_srv = create_exclusive_server((host, ui_port), UIHandler, max_wait_sec=15.0)
        except Exception as e:
            srv.server_close()
            raise RuntimeError(f"Failed to bind UI server on {host}:{ui_port}: {e}") from e
        t = threading.Thread(target=ui_srv.serve_forever, daemon=True)
        t.start()
    srv.conf = conf
    print(f"cc-relay v2 on http://{host}:{port}")
    for n, u in (conf.get("upstreams") or {}).items():
        print(f"  [{n}] -> {u.get('base')}")
    print(f"  records: {get_records_path()}")
    fake_key = conf.get("fake_api_key")
    masked_key = _mask_config_key(fake_key) if fake_key else "<none>"
    print(f"  CC: ANTHROPIC_BASE_URL=http://{host}:{port}  ANTHROPIC_AUTH_TOKEN={masked_key}")
    srv.serve_forever()


def stats(a):
    recs = read_records()
    print(f"总记录: {len(recs)}")
    by_route = {}
    by_model = {}
    for r in recs:
        by_route[r.get("route", "?")] = by_route.get(r.get("route", "?"), 0) + 1
        by_model[r.get("orig_model", "?")] = by_model.get(r.get("orig_model", "?"), 0) + 1
    print("路由分布:", by_route)
    print("原始 model 分布:", by_model)
    print("\n最近 12 条:")
    for r in recs[-12:]:
        print(f"  #{r.get('idx')} {r.get('ts')} {r.get('orig_model')} -> {r.get('sent_model')} "
              f"[{r.get('route')}] {r.get('path')[:24]} {r.get('resp_status')}")


def last(a):
    n = a.n or 5
    for r in read_records()[-n:]:
        b = r.get("body") or {}
        print("=" * 70)
        print(f"#{r.get('idx')} {r.get('ts')} {r.get('method')} {r.get('path')} -> {r.get('resp_status')}")
        print(f"route={r.get('route')} ({r.get('route_reason')}) | model {r.get('orig_model')} -> {r.get('sent_model')}")
        h = r.get("headers", {})
        print("agent-id:", h.get("x-claude-code-agent-id"), "| session:", (h.get("X-Claude-Code-Session-Id") or "")[:8])
        print("msgs:", len(b.get("messages") or []), "tools:", len(b.get("tools") or []))


def _redact_record_for_cli(rec):
    if not isinstance(rec, dict):
        return rec
    r = json.loads(json.dumps(rec, ensure_ascii=False))
    headers = r.get("headers")
    if isinstance(headers, dict):
        for k in list(headers.keys()):
            if k.lower() in ("authorization", "x-api-key"):
                headers[k] = "<redacted>"
    return r


__all__ = [k for k in list(globals().keys()) if not k.startswith("__")]


def dump(a):
    recs = read_records()
    if a.which == "all":
        for r in recs[-20:]:
            print(json.dumps(_redact_record_for_cli(r), ensure_ascii=False, indent=1)[:4000])
    else:
        for r in recs:
            if str(r.get("idx")) == a.which:
                print(json.dumps(_redact_record_for_cli(r), ensure_ascii=False, indent=1))
                return
        print("未找到 #%s" % a.which)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve").add_argument("--no-ui", action="store_true")
    sub.add_parser("ui")
    sub.add_parser("stats")
    l = sub.add_parser("last")
    l.add_argument("n", type=int, nargs="?", default=5)
    d = sub.add_parser("dump")
    d.add_argument("which")
    sub.add_parser("startproxy")
    sub.add_parser("stopproxy")
    sub.add_parser("proxycheck")
    a = ap.parse_args()
    os.makedirs(BASE, exist_ok=True)
    if a.cmd == "serve":
        serve(a)
    elif a.cmd == "ui":
        serve_ui(load_conf())
    elif a.cmd == "stats":
        stats(a)
    elif a.cmd == "last":
        last(a)
    elif a.cmd == "dump":
        dump(a)
    elif a.cmd == "startproxy":
        print(codex_start(load_conf()))
    elif a.cmd == "stopproxy":
        print(codex_stop())
    elif a.cmd == "proxycheck":
        print("up" if codex_up() else "down")
    else:
        ap.print_help()
