# -*- coding: utf-8 -*-
"""cc-relay 8400 端口 Relay 请求处理类 (Anthropic 协议网关、协议适配与 SSE 流式中继)"""
import hmac
import json
import logging
import os
import re
import select
import socket
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

log = logging.getLogger("cc_relay.proxy")

try:
    from relay.upstreams import relay_updater
except ImportError:
    import relay_updater

try:
    from relay.adapters.openai_adapter import (
        OpenAIToAnthropicStreamAdapter,
        anthropic_to_openai_request,
        openai_to_anthropic_response,
    )
except ImportError:
    from openai_adapter import (
        OpenAIToAnthropicStreamAdapter,
        anthropic_to_openai_request,
        openai_to_anthropic_response,
    )
from relay.audit import _resp_cache_usage, _resp_model, record
from relay.config import CONF_LOCK, load_conf
from relay.constants import _UP
from relay.modifier import _MOD_MGR, _PROMPT_MGR, _strip_cc_fingerprint
from relay.router import (
    REASONING_MAP,
    _is_subagent,
    _key_for,
    _strip_flags,
    _tier_from_reason,
    _upstream_conf,
    get_configured_upstreams,
    is_relay_placeholder,
    pick_route,
)
from relay.traffic import (
    _CODEX_MAINTENANCE,
    _CODEX_MAINTENANCE_ACTIVE,
    _TRAFFIC_UNPAUSED_EVENT,
    _codex_request_enter,
    _codex_request_leave,
    _paused_waiting_dec,
    _paused_waiting_inc,
    traffic_paused,
)
from relay.upstreams import (
    antigravity_auto_start_enabled,
    antigravity_start,
    antigravity_up,
    codex_start,
    codex_up,
    live_models,
    record_upstream_health,
)
try:
    from relay.voice_context import VOICE_CONTEXTS, tcp_client_pid
except ImportError:
    from voice_context import VOICE_CONTEXTS, tcp_client_pid


def _send_sse_error(wfile, err_payload, is_anthropic=True):
    """向已建立 200 chunked SSE 连接的客户端下发协议级错误事件并正常关闭分块流."""
    try:
        if is_anthropic:
            chunk = f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n".encode("utf-8")
        else:
            chunk = f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n\ndata: [DONE]\n\n".encode("utf-8")
        wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
        wfile.write(b"0\r\n\r\n")
        wfile.flush()
    except Exception:
        pass


def _is_client_socket_closed(sock):
    """检测下游客户端 TCP 连接是否已断开 (FIN / RST / 异常)."""
    if not sock:
        return False
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            peek = sock.recv(1, socket.MSG_PEEK)
            if not peek:
                return True
    except Exception:
        return True
    return False


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"cc-relay/{getattr(relay_updater, 'RELAY_VERSION', '2.4.9')}"

    def log_message(self, *a):
        pass

    def _do(self, method):
        try:
            if hasattr(self.server, "reload_conf") and callable(self.server.reload_conf):
                conf = self.server.reload_conf()
            else:
                conf = load_conf()
            self.server.conf = conf
        except Exception:
            conf = getattr(self.server, "conf", None) or load_conf()

        fake_key = (conf.get("fake_api_key") or "").strip()
        if fake_key and not self.path.startswith("/api/"):
            auth_hdr = self.headers.get("Authorization", "").strip()
            bearer_tok = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
            x_api_key = self.headers.get("x-api-key", "").strip()
            valid = False
            if bearer_tok and hmac.compare_digest(bearer_tok, fake_key):
                valid = True
            elif x_api_key and hmac.compare_digest(x_api_key, fake_key):
                valid = True
            if not valid:
                msg = json.dumps({"error": {"type": "authentication_error", "message": "invalid api key"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(msg)
                return

        if self.path.split("?")[0] == "/v1/models" and method == "GET":
            lm = live_models(conf)
            custom_models = []
            for cp in (conf.get("custom_providers") or {}).values():
                if isinstance(cp, dict) and cp.get("models"):
                    custom_models.extend(cp["models"])
            names = sorted(set(lm['ds'] + lm['cx'] + lm['gm'] + custom_models +
                               ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]))
            body = json.dumps({"data": [{"id": m, "object": "model", "owned_by": "relay"} for m in names],
                               "object": "list"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path

        body_json = None
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype and raw:
            try:
                body_json = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                body_json = None

        is_anthropic = path.startswith("/v1/messages") or "anthropic" in self.headers.get("anthropic-version", "").lower()

        is_voice_correction = (self.headers.get("x-cc-relay-purpose") == "voice-correction")
        voice_context_token = None
        if method == "POST" and path.split("?", 1)[0] == "/v1/messages" and isinstance(body_json, dict) and not is_voice_correction:
            session_id = self.headers.get("x-claude-code-session-id", "")
            client_pid = tcp_client_pid(self.connection, self.client_address)
            try:
                is_main_session = not _is_subagent(self.headers, body_json)
                voice_context_token = VOICE_CONTEXTS.observe_request(
                    session_id=session_id,
                    client_pid=client_pid,
                    model=body_json.get("model", ""),
                    messages=body_json.get("messages") or [],
                    is_main_session=is_main_session,
                )
            except Exception:
                voice_context_token = None

        is_stream_early = False
        t_pause_wait = 0.0
        if traffic_paused(conf):
            is_stream = False
            if isinstance(body_json, dict) and body_json.get("stream") is True:
                is_stream = True
            elif "text/event-stream" in self.headers.get("Accept", "").lower():
                is_stream = True

            t_pause_start = time.time()
            _paused_waiting_inc()
            client_aborted = False

            try:
                if is_stream:
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache, no-transform")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("X-CC-Relay-Traffic-Paused", "1")
                        self.end_headers()
                    except Exception:
                        client_aborted = True

                    if is_anthropic:
                        ping_payload = b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
                    else:
                        ping_payload = b": keep-alive\n\n"

                    if not client_aborted:
                        try:
                            self.wfile.write(f"{len(ping_payload):x}\r\n".encode("ascii") + ping_payload + b"\r\n")
                            self.wfile.flush()
                        except Exception:
                            client_aborted = True

                    last_ping_t = time.time()
                    while not client_aborted:
                        if _is_client_socket_closed(self.connection):
                            client_aborted = True
                            break
                        if _TRAFFIC_UNPAUSED_EVENT.wait(timeout=0.25):
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                break
                        else:
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                _TRAFFIC_UNPAUSED_EVENT.set()
                                break

                        now = time.time()
                        if now - last_ping_t >= 3.0:
                            last_ping_t = now
                            try:
                                self.wfile.write(f"{len(ping_payload):x}\r\n".encode("ascii") + ping_payload + b"\r\n")
                                self.wfile.flush()
                            except Exception:
                                client_aborted = True
                                break

                    is_stream_early = not client_aborted
                else:
                    while True:
                        if _is_client_socket_closed(self.connection):
                            client_aborted = True
                            break
                        if _TRAFFIC_UNPAUSED_EVENT.wait(timeout=0.25):
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                break
                        else:
                            with CONF_LOCK:
                                conf = load_conf()
                            if not traffic_paused(conf):
                                _TRAFFIC_UNPAUSED_EVENT.set()
                                break
            finally:
                _paused_waiting_dec()

            t_pause_wait = round(time.time() - t_pause_start, 2)

            if client_aborted:
                self.close_connection = True
                try:
                    record({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "method": method, "path": path,
                        "client": self.client_address[0] if self.client_address else "127.0.0.1",
                        "headers": {k: v for k, v in self.headers.items()},
                        "body": body_json,
                        "body_raw": raw.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                        "route": "paused", "route_reason": "traffic_paused_aborted",
                        "orig_model": (body_json or {}).get("model") if isinstance(body_json, dict) else None,
                        "sent_model": None,
                        "stripped_banner": 0, "custom_prompt": False,
                        "upstream": None, "resp_status": 499,
                        "resp_body": "client disconnected while traffic was paused",
                        "cache": None,
                        "resp_error": "client_aborted",
                        "duration": t_pause_wait,
                        "paused_wait_sec": t_pause_wait,
                        "traffic_paused": True,
                    })
                except Exception:
                    pass
                return

        up_name, map_model, reason = pick_route(conf, self.headers, body_json)
        if up_name == "codex":
            with _CODEX_MAINTENANCE:
                if _CODEX_MAINTENANCE_ACTIVE:
                    msg = json.dumps({"error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}).encode()
                    if is_stream_early:
                        _send_sse_error(self.wfile, {"type": "error", "error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}, is_anthropic=is_anthropic)
                    else:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(msg)))
                        self.send_header("Retry-After", "3")
                        self.end_headers()
                        if method != "HEAD":
                            self.wfile.write(msg)
                    return

        if up_name == "codex" and not codex_up():
            codex_start(conf)
        elif (up_name == "antigravity" and antigravity_auto_start_enabled(conf)
              and not antigravity_up(conf)):
            antigravity_start(conf)

        up = _upstream_conf(conf, up_name)
        base = (up.get("base") or "").strip()
        if not base:
            configured = get_configured_upstreams(conf)
            if configured and up_name not in configured:
                alt_up = "deepseek" if "deepseek" in configured else ("antigravity" if "antigravity" in configured else next(iter(configured)))
                log.warning("[relay] upstream %s not configured, emergency fallback to %s", up_name, alt_up)
                up_name = alt_up
                up = _upstream_conf(conf, up_name)
                base = (up.get("base") or "").strip()

        if not base:
            msg = json.dumps({"error": {"type": "relay_error", "message": "upstream is not configured: " + up_name}}).encode()
            if is_stream_early:
                _send_sse_error(self.wfile, {"type": "error", "error": {"type": "relay_error", "message": "upstream is not configured: " + up_name}}, is_anthropic=is_anthropic)
            else:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(msg)
            return
        codex_request_active = False
        is_custom_provider = up_name in (conf.get("custom_providers") or {})
        provider_proto = str(up.get("protocol") or "openai").strip().lower() if is_custom_provider else "anthropic"

        if is_custom_provider and provider_proto == "openai":
            base_clean = base.rstrip("/")
            upstream = base_clean + "/chat/completions"
        else:
            upstream = base.rstrip("/") + path

        orig_model = (body_json or {}).get("model") if isinstance(body_json, dict) else None
        sent_model = orig_model
        if map_model and isinstance(body_json, dict) and body_json.get("model") != map_model:
            body_json = dict(body_json)
            body_json["model"] = map_model
            sent_model = map_model
            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        router_cfg = conf.get("router") or {}
        tier_hit = None if is_voice_correction else _tier_from_reason(reason)
        if is_voice_correction:
            v_conf = (conf.get("tools") or {}).get("voice") or {}
            v_effort = str(v_conf.get("semantic_correction_effort") or "").strip().lower()
            if v_effort in REASONING_MAP:
                effort = v_effort
            else:
                effort = "off" if (up_name in ("deepseek", "antigravity") or "deepseek" in str(sent_model or "").lower()) else "low"
        else:
            effort = (router_cfg.get("effort") or "medium").strip()
            te = router_cfg.get("tier_efforts") or {}
            if tier_hit:
                effort = (te.get(tier_hit) or effort)

        if effort in REASONING_MAP and up_name != "antigravity" and isinstance(body_json, dict):
            body_json = dict(body_json)
            body_json["thinking"] = REASONING_MAP[effort]
            if REASONING_MAP[effort].get("type") == "enabled":
                b_tokens = REASONING_MAP[effort].get("budget_tokens", 1024)
                cur_max = int(body_json.get("max_tokens") or 0)
                if cur_max <= b_tokens:
                    body_json["max_tokens"] = b_tokens + 512
            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        if tier_hit and isinstance(body_json, dict):
            _PROMPT_MGR.capture(tier_hit, body_json)

        custom_prompt_applied = False
        if tier_hit and isinstance(body_json, dict):
            _tm = str((router_cfg.get("tier_modifier") or {}).get(tier_hit) or "").strip().lower()
            if _tm == "custom":
                body_json, custom_prompt_applied = _PROMPT_MGR.apply_custom(tier_hit, body_json)
                if custom_prompt_applied:
                    raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        fwd = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection", "authorization", "x-api-key", "accept-encoding", "x-cc-relay-purpose"):
                continue
            fwd[k] = v
        key = _key_for(conf, up, up_name)
        if key:
            fwd["x-api-key"] = key
            fwd["authorization"] = "Bearer " + key
        fwd["Accept-Encoding"] = "identity"

        mod_mode = (router_cfg.get("modifier_mode") or conf.get("modifier_mode") or "custom").strip().lower()
        tier_strip_on = bool(tier_hit and _strip_flags(router_cfg).get(tier_hit, False))
        tier_mod_map = router_cfg.get("tier_modifier") or {}
        eff_mode = "original" if is_voice_correction else mod_mode
        per_tier_explicit = False
        if tier_hit:
            _tm = str(tier_mod_map.get(tier_hit) or "") if isinstance(tier_mod_map, dict) else ""
            _tm = _tm.strip().lower()
            if _tm in ("original", "builtin", "custom"):
                eff_mode = _tm
            else:
                eff_mode = "original"
            per_tier_explicit = True
        elif up_name == "antigravity":
            _gm = str(router_cfg.get("gemini_modifier") or "").strip().lower()
            eff_mode = _gm if _gm in ("original", "builtin") else "builtin"
            per_tier_explicit = True
        stripped_n = 0

        mod_ctx = {
            "upstream_name": up_name,
            "tier": tier_hit,
            "orig_model": orig_model,
            "target_model": sent_model,
            "path": path,
            "method": method,
            "is_subagent": _is_subagent(self.headers, body_json),
            "tier_strip_on": tier_strip_on,
            "modifier_mode": eff_mode,
        }

        if eff_mode == "original":
            stripped_n = 0
        elif eff_mode == "builtin":
            if isinstance(body_json, dict):
                body_json, stripped_n = _strip_cc_fingerprint(body_json)
                if stripped_n:
                    raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        else:
            if per_tier_explicit:
                stripped_n = 0
            else:
                mod = _MOD_MGR.get_modifier()
                if mod:
                    try:
                        if hasattr(mod, "modify_body"):
                            nb_json, nb_raw, sn = mod.modify_body(body_json, raw, mod_ctx)
                            if nb_raw is not None:
                                raw = nb_raw
                                if nb_json is not None:
                                    body_json = nb_json
                            elif nb_json is not None:
                                body_json = nb_json
                                raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                            stripped_n = int(sn or 0)
                    except Exception as e:
                        print(f"[WARN] custom_modifier.modify_body 执行失败: {e}", file=sys.stderr)
                        if tier_strip_on and isinstance(body_json, dict):
                            body_json, stripped_n = _strip_cc_fingerprint(body_json)
                            if stripped_n:
                                raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

                    try:
                        if hasattr(mod, "modify_headers"):
                            new_fwd = mod.modify_headers(fwd, mod_ctx)
                            if isinstance(new_fwd, dict):
                                fwd = new_fwd
                    except Exception as e:
                        print(f"[WARN] custom_modifier.modify_headers 执行失败: {e}", file=sys.stderr)
                else:
                    if tier_strip_on and isinstance(body_json, dict):
                        body_json, stripped_n = _strip_cc_fingerprint(body_json)
                        if stripped_n:
                            raw = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

        if is_custom_provider and provider_proto == "openai" and isinstance(body_json, dict):
            openai_body = anthropic_to_openai_request(body_json, target_model=sent_model)
            raw = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
            fwd["Content-Type"] = "application/json"

        data = raw if raw else None
        if up_name == "codex":
            if not _codex_request_enter():
                msg = json.dumps({"error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}).encode()
                if is_stream_early:
                    _send_sse_error(self.wfile, {"type": "error", "error": {"type": "api_error", "message": "Codex proxy is updating; retry shortly"}}, is_anthropic=is_anthropic)
                else:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(msg)))
                    self.send_header("Retry-After", "3")
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(msg)
                return
            codex_request_active = True

        status = 502
        rbody = b""
        rheaders = {}
        err = None
        t0 = time.time()
        try:
            req = urllib.request.Request(upstream, data=data, method=method)
            for k, v in fwd.items():
                req.add_header(k, v)
            if up.get("proxy_url") and up["proxy_url"] != "direct":
                op = urllib.request.build_opener(urllib.request.ProxyHandler(
                    {"http": up["proxy_url"], "https": up["proxy_url"]}))
            elif conf.get("proxy_url") and conf["proxy_url"] != "direct" and up.get("proxy_url") == "direct":
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            else:
                op = urllib.request.build_opener()
            resp = op.open(req, timeout=900)
            status = resp.status
            rheaders = dict(resp.headers)
            if is_custom_provider and provider_proto == "openai":
                is_stream_req = bool(body_json.get("stream")) if isinstance(body_json, dict) else False
                if is_stream_req:
                    if not is_stream_early:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.end_headers()
                        is_stream_early = True
                    stream_adapter = OpenAIToAnthropicStreamAdapter(model_name=sent_model)
                    rbody_chunks = []
                    for line in resp:
                        for chunk_bytes in stream_adapter.process_chunk(line.decode("utf-8", "replace")):
                            rbody_chunks.append(chunk_bytes)
                            try:
                                self.wfile.write(chunk_bytes)
                                self.wfile.flush()
                            except Exception:
                                break
                        if line.strip() == b"data: [DONE]":
                            break
                    for chunk_bytes in stream_adapter.finish():
                        rbody_chunks.append(chunk_bytes)
                        try:
                            self.wfile.write(chunk_bytes)
                            self.wfile.flush()
                        except Exception:
                            pass
                    self.close_connection = True
                    rbody = b"".join(rbody_chunks)
                else:
                    raw_resp = resp.read()
                    try:
                        openai_json = json.loads(raw_resp.decode("utf-8", "replace"))
                        anthropic_json = openai_to_anthropic_response(openai_json, model_name=sent_model)
                        rbody = json.dumps(anthropic_json, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        rbody = raw_resp
                    rheaders["Content-Type"] = "application/json"
            elif is_stream_early:
                rbody_chunks = []
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    rbody_chunks.append(chunk)
                    try:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                self.close_connection = True
                rbody = b"".join(rbody_chunks)
            else:
                rbody = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            rbody = e.read()
            rheaders = dict(e.headers) if e.headers else {}
            if is_custom_provider and provider_proto == "openai":
                try:
                    err_json = json.loads(rbody.decode("utf-8", "replace"))
                    err_msg = (err_json.get("error") or {}).get("message") or str(err_json)
                except Exception:
                    err_msg = rbody.decode("utf-8", "replace")[:500]
                rbody = json.dumps({"type": "error", "error": {"type": "api_error", "message": err_msg}}).encode()
                rheaders["Content-Type"] = "application/json"
            if is_stream_early:
                self.close_connection = True
                try:
                    err_json = json.loads(rbody.decode("utf-8", "replace"))
                except Exception:
                    err_json = {"type": "error", "error": {"type": "upstream_error", "message": rbody.decode("utf-8", "replace")[:500]}}
                _send_sse_error(self.wfile, err_json, is_anthropic=is_anthropic)
        except Exception as e:
            err = repr(e)
            rbody = json.dumps({"error": {"type": "relay_error", "message": err}}).encode()
            rheaders = {"Content-Type": "application/json"}
            if is_stream_early:
                self.close_connection = True
                err_json = {"type": "error", "error": {"type": "relay_error", "message": err}}
                _send_sse_error(self.wfile, err_json, is_anthropic=is_anthropic)
        finally:
            if codex_request_active:
                _codex_request_leave()

        if voice_context_token:
            try:
                VOICE_CONTEXTS.observe_response(voice_context_token, rbody)
            except Exception:
                pass

        duration = max(0.001, round(time.time() - t0, 2))
        cache_data = _resp_cache_usage(rbody.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)])
        out_tok = (cache_data.get("output_tokens") if isinstance(cache_data, dict) else 0) or 0
        speed = round(out_tok / duration, 1) if (out_tok > 0 and duration > 0) else None

        try:
            record({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": method, "path": path,
                "client": self.client_address[0],
                "headers": {k: v for k, v in self.headers.items()},
                "body": body_json, "body_raw": raw.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                "route": up_name, "route_reason": reason,
                "orig_model": orig_model, "sent_model": sent_model,
                "stripped_banner": stripped_n,
                "custom_prompt": custom_prompt_applied,
                "upstream": upstream, "resp_status": status,
                "resp_body": rbody.decode("utf-8", "replace")[:conf.get("max_body_capture", 2000000)],
                "cache": cache_data,
                "resp_error": err,
                "duration": duration,
                "paused_wait_sec": t_pause_wait if t_pause_wait > 0 else None,
                "traffic_paused": bool(t_pause_wait > 0),
                "output_tokens": out_tok,
                "speed": speed,
            })
        except Exception:
            pass

        if up_name in ("codex", "antigravity", "gemini", "deepseek"):
            target_up = "antigravity" if up_name == "gemini" else up_name
            if status and status < 400:
                record_upstream_health(target_up, True)
            elif status and (400 <= status < 500) and status not in (401, 403):
                pass
            else:
                err_str = ""
                if rbody:
                    try:
                        err_str = rbody.decode("utf-8", "replace")
                    except Exception:
                        err_str = ""
                if not err_str and err:
                    err_str = str(err)
                reason_text = "API报错"
                m_dial = re.search(r'dial tcp [^:\s]+:(\d+)', err_str)
                if m_dial and "dial HTTP proxy failed" in err_str:
                    reason_text = f"代理未运行 (:{m_dial.group(1)})"
                elif "dial HTTP proxy failed" in err_str:
                    reason_text = "代理连接失败"
                elif status in (401, 403):
                    reason_text = f"鉴权失败 ({status})"
                elif status and status >= 500:
                    reason_text = f"上游报错 ({status})"
                elif err:
                    reason_text = "网络超时"
                record_upstream_health(target_up, False, reason_text)

        _UP["last"] = up_name
        _UP["last_model"] = sent_model or ""
        _UP["last_ts"] = time.time()

        if not is_stream_early:
            try:
                self.send_response(status)
                self.send_header("Content-Type", rheaders.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(rbody)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(rbody)
            except Exception:
                pass

    def do_GET(self): self._do("GET")
    def do_POST(self): self._do("POST")
    def do_HEAD(self): self._do("HEAD")
    def do_DELETE(self): self._do("DELETE")
