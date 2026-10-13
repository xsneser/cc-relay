# -*- coding: utf-8 -*-
"""cc-relay 审计日志追加轮转、用量/缓存指标计算与统计快照"""
import json
import os
import re
import sys
import threading
import time

try:
    from relay.upstreams import antigravity_updater
except ImportError:
    import antigravity_updater
from relay.config import load_conf
from relay.constants import LOCK, _IDX, _PROCESS_INSTANCE_ID, _UP, get_records_path
from relay.modifier import MODIFIER_FILE, _MOD_MGR
from relay.router import DEFAULT_GEMINI_MAP, EFFORT_VALUES, TIER_KEYS, _strip_flags
from relay.traffic import _paused_waiting_count, traffic_paused
from relay.upstreams import (
    _get_cpa_updater,
    _get_relay_updater,
    antigravity_auto_start_enabled,
    antigravity_owned,
    antigravity_up,
    codex_up,
    get_upstream_health,
    live_models,
)
from relay.voice_bridge import voice_owned, voice_status_dict, voice_up

MAX_RECORD_BYTES = 50 * 1024 * 1024   # 50 MB 上限
KEEP_RATIO = 0.5                      # 超限时保留约 25 MB
_last_check = [0.0]


def _rotate_if_needed():
    """超过 MAX_RECORD_BYTES 时删除旧记录，保留最近约 25 MB (整行边界)"""
    records_file = get_records_path()
    try:
        size = os.path.getsize(records_file)
    except Exception:
        return
    if size <= MAX_RECORD_BYTES:
        return
    keep_bytes = int(MAX_RECORD_BYTES * KEEP_RATIO)
    try:
        with open(records_file, "rb") as f:
            f.seek(size - keep_bytes)
            rest = f.read()
        nl = rest.find(b"\n")
        if nl >= 0:
            rest = rest[nl + 1:]
        tmp = records_file + ".tmp"
        with open(tmp, "wb") as f:
            f.write(rest)
        os.replace(tmp, records_file)
    except Exception:
        pass


def _sanitize_headers_for_record(headers):
    if not isinstance(headers, dict):
        return headers
    clean = {}
    for k, v in headers.items():
        if str(k).lower() in ("authorization", "x-api-key"):
            clean[k] = "<redacted>"
        else:
            clean[k] = v
    return clean


def record(entry):
    records_file = get_records_path()
    with LOCK:
        _IDX[0] += 1
        entry["idx"] = _IDX[0]
        if "headers" in entry and isinstance(entry["headers"], dict):
            entry["headers"] = _sanitize_headers_for_record(entry["headers"])
        with open(records_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if time.time() - _last_check[0] > 2.0:
            _last_check[0] = time.time()
            _rotate_if_needed()


def read_records():
    records_file = get_records_path()
    out = []
    if os.path.exists(records_file):
        with open(records_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    return out


def _resp_model(resp_body):
    """从响应体解析上游真实模型名"""
    if not resp_body:
        return None
    try:
        s = resp_body
        if s.lstrip().startswith("event:") or "\ndata:" in s[:200] or s.lstrip().startswith("data:"):
            for line in s.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        j = json.loads(line[5:].strip())
                        m = (j.get("message") or {}).get("model") or j.get("model")
                        if m:
                            return m
                    except Exception:
                        pass
            mm = re.search(r'"model"\s*:\s*"([^"]+)"', s)
            return mm.group(1) if mm else None
        j = json.loads(s)
        return j.get("model")
    except Exception:
        mm = re.search(r'"model"\s*:\s*"([^"]+)"', resp_body)
        return mm.group(1) if mm else None


def _resp_cache_usage(resp_body):
    """从普通 JSON 或 Anthropic SSE 响应提取 prompt-cache usage。"""
    if not resp_body:
        return None

    merged = {}
    saw_usage = False

    def token_count(value):
        if isinstance(value, bool):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def merge_usage(usage):
        nonlocal saw_usage
        if not isinstance(usage, dict):
            return
        fields = ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "output_tokens")
        found = False
        cache_found = False
        for name in fields:
            if name in usage:
                value = token_count(usage.get(name))
                if value is not None:
                    merged[name] = value
                    found = True
                    cache_found = cache_found or (name != "input_tokens" and name != "output_tokens")
        if "completion_tokens" in usage and "output_tokens" not in merged:
            c_val = token_count(usage.get("completion_tokens"))
            if c_val is not None:
                merged["output_tokens"] = c_val
                found = True
        if "cache_creation_input_tokens" not in merged:
            breakdown = usage.get("cache_creation")
            if isinstance(breakdown, dict):
                parts = [token_count(breakdown.get(k)) or 0 for k in
                         ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
                if any(k in breakdown for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")):
                    merged["cache_creation_input_tokens"] = sum(parts)
                    found = True
                    cache_found = True
        saw_usage = saw_usage or cache_found or (found and "output_tokens" in merged)

    def walk(value):
        if isinstance(value, dict):
            merge_usage(value.get("usage"))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    text = str(resp_body)
    try:
        walk(json.loads(text))
    except Exception:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        if "usage" not in payload and "cache" not in payload:
            continue
        try:
            walk(json.loads(payload))
        except Exception:
            pass
    if not saw_usage:
        return None

    input_tokens = merged.get("input_tokens", 0)
    cache_read = merged.get("cache_read_input_tokens", 0)
    cache_creation = merged.get("cache_creation_input_tokens", 0)
    output_tokens = merged.get("output_tokens", 0)
    total = input_tokens + cache_read + cache_creation
    return {
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cache_total_tokens": total,
        "cache_hit_rate": (cache_read / total) if total else None,
        "output_tokens": output_tokens,
    }


def _iter_records_tail(max_records=400, max_bytes=500_000_000):
    """从文件末尾逆序向前读, 拿够 max_records 条或到 max_bytes 为止"""
    records_file = get_records_path()
    if not os.path.exists(records_file):
        return []
    try:
        size = os.path.getsize(records_file)
    except OSError:
        return []
    if size == 0:
        return []

    chunk_size = 512_000
    pos = size
    records = []
    trailing = b""
    bytes_read = 0

    with open(records_file, "rb") as f:
        while pos > 0 and len(records) < max_records and bytes_read < max_bytes:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            bytes_read += read_size
            buf = chunk + trailing
            lines = buf.split(b"\n")
            if pos > 0:
                trailing = lines[0]
                complete_lines = lines[1:]
            else:
                trailing = b""
                complete_lines = lines

            for raw_line in reversed(complete_lines):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    records.append(json.loads(raw_line.decode("utf-8", "replace")))
                    if len(records) >= max_records:
                        break
                except Exception:
                    pass

        if trailing and len(records) < max_records:
            t = trailing.strip()
            if t:
                try:
                    records.append(json.loads(t.decode("utf-8", "replace")))
                except Exception:
                    pass

    records.reverse()
    return records


def _find_record_by_idx(target_idx, max_bytes=800_000_000):
    """高效逆序定位单个历史抓包详情, 支持从海量大记录中精准抓取"""
    records_file = get_records_path()
    if not os.path.exists(records_file):
        return None
    target_idx_str = str(target_idx)
    target_pattern = f'"idx": {target_idx_str}'.encode("utf-8")
    target_pattern2 = f'"idx":{target_idx_str}'.encode("utf-8")
    target_pattern3 = f'"idx": "{target_idx_str}"'.encode("utf-8")

    try:
        size = os.path.getsize(records_file)
    except OSError:
        return None
    if size == 0:
        return None

    chunk_size = 4_000_000
    pos = size
    trailing = b""
    bytes_read = 0

    with open(records_file, "rb") as f:
        while pos > 0 and bytes_read < max_bytes:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            bytes_read += read_size
            buf = chunk + trailing
            lines = buf.split(b"\n")
            if pos > 0:
                trailing = lines[0]
                complete_lines = lines[1:]
            else:
                trailing = b""
                complete_lines = lines

            for raw_line in reversed(complete_lines):
                if target_pattern in raw_line or target_pattern2 in raw_line or target_pattern3 in raw_line:
                    try:
                        r = json.loads(raw_line.decode("utf-8", "replace"))
                        if str(r.get("idx")) == target_idx_str:
                            return r
                    except Exception:
                        pass

        if trailing and (target_pattern in trailing or target_pattern2 in trailing or target_pattern3 in trailing):
            try:
                r = json.loads(trailing.decode("utf-8", "replace"))
                if str(r.get("idx")) == target_idx_str:
                    return r
            except Exception:
                pass

    return None


_STATUS_TAIL_BYTES = 100_000_000
_CALLS_TAIL_BYTES = 600_000_000
_CALLS_CACHE_LIMIT = 8
_STATS_CACHE = {"fingerprint": None, "rows": None, "total": 0, "overall_avg_speed": None}
_CALLS_CACHE = {}
_STATS_LOCK = threading.Lock()
_CALLS_LOCK = threading.Lock()


def _records_fingerprint():
    """On-disk cache key; intentionally independent of this process's _IDX."""
    records_file = get_records_path()
    try:
        stat = os.stat(records_file)
    except OSError:
        return (os.path.abspath(records_file), None, None, None, None)
    return (os.path.abspath(records_file), getattr(stat, "st_dev", None),
            getattr(stat, "st_ino", None), stat.st_mtime_ns, stat.st_size)


def stats_snapshot():
    """Cache record aggregates; configuration and model state remain live per call."""
    status_started = time.perf_counter()
    status_timings = {}
    stats_lock_started = time.perf_counter()
    records_rebuild_ms = 0.0

    mod = sys.modules.get("cc_relay")
    _iter_tail_fn = getattr(mod, "_iter_records_tail", _iter_records_tail) if mod else _iter_records_tail
    _load_conf_fn = getattr(mod, "load_conf", load_conf) if mod else load_conf
    _live_models_fn = getattr(mod, "live_models", live_models) if mod else live_models
    _codex_up_fn = getattr(mod, "codex_up", codex_up) if mod else codex_up
    _anti_up_fn = getattr(mod, "antigravity_up", antigravity_up) if mod else antigravity_up
    _voice_dict_fn = getattr(mod, "voice_status_dict", voice_status_dict) if mod else voice_status_dict
    _voice_up_fn = getattr(mod, "voice_up", voice_up) if mod else voice_up
    _get_health_fn = getattr(mod, "get_upstream_health", get_upstream_health) if mod else get_upstream_health

    with _STATS_LOCK:
        status_timings["stats_lock_wait_ms"] = (time.perf_counter() - stats_lock_started) * 1000
        fingerprint = _records_fingerprint()
        if _STATS_CACHE["fingerprint"] == fingerprint and _STATS_CACHE["rows"] is not None:
            status_timings["stats_cache_hit"] = True
            rows = [dict(row) for row in _STATS_CACHE["rows"]]
            total_records = _STATS_CACHE["total"]
        else:
            status_timings["stats_cache_hit"] = False
            records_started = time.perf_counter()
            recs = _iter_tail_fn(max_records=400, max_bytes=_STATUS_TAIL_BYTES)
            records_rebuild_ms = (time.perf_counter() - records_started) * 1000
            by = {}
            total_duration_all = 0.0
            total_output_tokens_all = 0
            for r in recs:
                real_model = _resp_model(r.get('resp_body')) or r.get('sent_model') or r.get('orig_model')
                key = (r.get('route'), real_model)
                b = by.setdefault(key, {'route': r.get('route'), 'model': real_model,
                                        'sent': r.get('sent_model'), 'req': 0, 'ok': 0, 'err': 0, 'last': 0,
                                        'input_tokens': 0, 'cache_read_tokens': 0,
                                        'cache_creation_tokens': 0, 'cache_requests': 0,
                                        'output_tokens': 0, 'total_duration': 0.0, 'speed_count': 0})
                usage = r.get('cache') if r.get('cache') is not None else _resp_cache_usage(r.get('resp_body'))
                if usage:
                    b['input_tokens'] += usage.get('cache_total_tokens', 0) or 0
                    b['cache_read_tokens'] += usage.get('cache_read_tokens', 0) or 0
                    b['cache_creation_tokens'] += usage.get('cache_creation_tokens', 0) or 0
                    b['cache_requests'] += 1

                dur = r.get('duration')
                out_tok = r.get('output_tokens')
                if out_tok is None and usage:
                    out_tok = usage.get('output_tokens')
                spd = r.get('speed')
                if spd is None and dur and out_tok and dur > 0 and out_tok > 0:
                    spd = round(out_tok / dur, 1)

                if dur and out_tok and dur > 0 and out_tok > 0:
                    b['output_tokens'] += out_tok
                    b['total_duration'] += dur
                    b['speed_count'] += 1
                    total_output_tokens_all += out_tok
                    total_duration_all += dur

                b['req'] += 1
                st = r.get('resp_status') or 0
                if st and st < 400:
                    b['ok'] += 1
                else:
                    b['err'] += 1
                b['last'] = r.get('idx', 0)
            for b in by.values():
                total = b['input_tokens']
                b['cache_hit_rate'] = (b['cache_read_tokens'] / total) if total else None
                if b['total_duration'] > 0 and b['output_tokens'] > 0:
                    b['avg_speed'] = round(b['output_tokens'] / b['total_duration'], 1)
                else:
                    b['avg_speed'] = None
            overall_avg_speed = round(total_output_tokens_all / total_duration_all, 1) if (total_duration_all > 0 and total_output_tokens_all > 0) else None
            rows = sorted(by.values(), key=lambda x: -x['last'])
            total_records = len(recs)
            _STATS_CACHE.update({"fingerprint": fingerprint, "rows": rows, "total": total_records, "overall_avg_speed": overall_avg_speed})
            rows = [dict(row) for row in rows]
            records_rebuild_ms = (time.perf_counter() - records_started) * 1000
    status_timings["records_rebuild_ms"] = records_rebuild_ms

    component_started = time.perf_counter()
    conf = _load_conf_fn()
    rt = conf.get('router') or {}
    lm = _live_models_fn(conf)
    status_timings["config_models_ms"] = (time.perf_counter() - component_started) * 1000
    route = rt.get('route', 'hybrid')
    if route == 'gemini':
        route = 'antigravity'
    component_started = time.perf_counter()
    codex_available = _codex_up_fn(conf)
    antigravity_available = _anti_up_fn(conf)
    v_stat = _voice_dict_fn(conf)
    voice_available = (
        v_stat.get("ready", False)
        or v_stat.get("status") in ("starting", "loading_model", "recording", "ready")
        or _voice_up_fn(conf)
    )
    status_timings["service_probes_ms"] = (time.perf_counter() - component_started) * 1000

    component_started = time.perf_counter()
    cx_health = _get_health_fn(conf, "codex", running=codex_available)
    gm_health = _get_health_fn(conf, "antigravity", running=antigravity_available)
    vc_health = _get_health_fn(conf, "voice", running=voice_available)
    status_timings["health_ms"] = (time.perf_counter() - component_started) * 1000

    component_started = time.perf_counter()
    cpa_status = _get_cpa_updater(conf).status()
    relay_update_status = _get_relay_updater(conf).status()
    ag_update_status = antigravity_updater.get_antigravity_updater(conf).status()
    status_timings["updater_status_ms"] = (time.perf_counter() - component_started) * 1000
    cps = conf.get("custom_providers") or {}
    custom_all = []
    for cp in cps.values():
        if isinstance(cp, dict) and cp.get("models"):
            custom_all.extend(cp["models"])

    res = {
        'route': route, 'mode': route,
        'traffic_paused': traffic_paused(conf),
        'paused_waiting_requests': _paused_waiting_count(),
        'antigravity_auto_start': antigravity_auto_start_enabled(conf),
        'instance_id': _PROCESS_INSTANCE_ID,
        'model': rt.get('model', ''),
        'models_ds': lm['ds'], 'models_codex': lm['cx'], 'models_gemini': lm['gm'],
        'deepseek_models': lm['ds'], 'codex_models': lm['cx'], 'gemini_models': lm['gm'],
        'custom_providers': cps,
        'models_custom': {cpid: list(cp.get('models') or []) for cpid, cp in cps.items() if isinstance(cp, dict)},
        'all_models': lm['ds'] + lm['cx'] + lm['gm'] + custom_all,
        'settings_model': rt.get('model', '') or '(自动)',
        'env_base': 'http://127.0.0.1:8400',
        'env_model': rt.get('model', '') or route,
        'codex_default_model': 'gpt-5.6-sol',
        'hybrid_codex_model': (rt.get('hybrid_codex_model') or rt.get('model') or '').strip() or 'gpt-5.6-sol',
        'hybrid_deepseek_model': (rt.get('hybrid_deepseek_model') or '').strip() or 'deepseek-flash',
        'hybrid_antigravity_model': (rt.get('hybrid_antigravity_model') or '').strip() or DEFAULT_GEMINI_MAP,
        'tiers': {
            'main':   (rt.get('tiers') or {}).get('main')   or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
            'opus':   (rt.get('tiers') or {}).get('opus')   or (rt.get('hybrid_codex_model') or 'gpt-5.6-sol'),
            'sonnet': (rt.get('tiers') or {}).get('sonnet') or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
            'fast':   (rt.get('tiers') or {}).get('fast')   or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
            'agent':  (rt.get('tiers') or {}).get('agent')  or (rt.get('hybrid_deepseek_model') or 'deepseek-flash'),
        },
        'tier_env': {
            'main': 'ANTHROPIC_MODEL',
            'opus': 'ANTHROPIC_DEFAULT_OPUS_MODEL',
            'sonnet': 'ANTHROPIC_DEFAULT_SONNET_MODEL',
            'fast': 'ANTHROPIC_SMALL_FAST_MODEL',
            'agent': 'ANTHROPIC_MODEL·plan代理',
        },
        'efforts': list(EFFORT_VALUES),
        'effort': (rt.get('effort') or 'medium'),
        'strip_cc_banner': _strip_flags(rt),
        'modifier_mode': (rt.get('modifier_mode') or conf.get('modifier_mode') or 'custom').strip().lower(),
        'tier_modifier': {k: str(((rt.get('tier_modifier') or {}).get(k)) or '').strip().lower() for k in TIER_KEYS},
        'gemini_modifier': str(rt.get('gemini_modifier') or '').strip().lower(),
        'modifier_file_exists': os.path.exists(MODIFIER_FILE),
        'modifier_error': _MOD_MGR._err,
        'tier_efforts': {
            'main':   ('low' if (rt.get('tier_efforts') or {}).get('main') == 'instant' else ((rt.get('tier_efforts') or {}).get('main') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
            'opus':   ('low' if (rt.get('tier_efforts') or {}).get('opus') == 'instant' else ((rt.get('tier_efforts') or {}).get('opus') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
            'sonnet': ('low' if (rt.get('tier_efforts') or {}).get('sonnet') == 'instant' else ((rt.get('tier_efforts') or {}).get('sonnet') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
            'fast':   ('low' if (rt.get('tier_efforts') or {}).get('fast') == 'instant' else ((rt.get('tier_efforts') or {}).get('fast') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
            'agent':  ('low' if (rt.get('tier_efforts') or {}).get('agent') == 'instant' else ((rt.get('tier_efforts') or {}).get('agent') or ('low' if rt.get('effort') == 'instant' else rt.get('effort')) or 'medium')),
        },
        'deepseek_profile': {'default_model': 'deepseek-flash'},
        'hybrid_pins': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'gpt-5.6-sol'},
        'proxy_running': codex_available,
        'rows': rows, 'total': total_records,
        'overall_avg_speed': _STATS_CACHE.get('overall_avg_speed'),
        'codex_up': codex_available, 'antigravity_up': antigravity_available,
        'antigravity_owned': antigravity_owned(),
        'voice_up': voice_available,
        'voice_owned': voice_owned(conf),
        'voice_status': v_stat,
        'codex_state': cx_health["state"],
        'codex_reason': cx_health["reason"],
        'antigravity_state': gm_health["state"],
        'antigravity_reason': gm_health["reason"],
        'voice_state': vc_health["state"],
        'voice_reason': vc_health["reason"],
        'cpa_version': cpa_status.get('current_version'),
        'cpa_latest_version': cpa_status.get('latest_version'),
        'cpa_has_update': cpa_status.get('has_update', False),
        'cpa_update_checking': cpa_status.get('checking', False),
        'cpa_update_check_error': cpa_status.get('check_error'),
        'cpa_update_checked_at': cpa_status.get('last_checked'),
        'cpa_update_state': cpa_status.get('update_state', 'idle'),
        'cpa_update_message': cpa_status.get('update_message'),
        'cpa_update_error': cpa_status.get('update_error'),
        'cpa_release_notes': cpa_status.get('release_notes', ''),
        'cpa_download_progress': cpa_status.get('download_progress', 0.0),
        'cpa_downloaded_bytes': cpa_status.get('downloaded_bytes', 0),
        'cpa_total_bytes': cpa_status.get('total_bytes', 0),
        'relay_update': relay_update_status,
        'antigravity_version': ag_update_status.get('current_version'),
        'antigravity_latest_version': ag_update_status.get('latest_version'),
        'antigravity_has_update': ag_update_status.get('has_update', False),
        'antigravity_update': ag_update_status,
        'upstreams_status': {
            'deepseek': {'available': True, 'state': 'online'},
            'codex': {
                'available': cx_health["state"] == "online",
                'running': codex_available,
                'state': cx_health["state"],
                'reason': cx_health["reason"],
                'managed': True,
            },
            'gemini': {
                'available': gm_health["state"] == "online",
                'running': antigravity_available,
                'state': gm_health["state"],
                'reason': gm_health["reason"],
                'managed': antigravity_owned(),
            },
            'voice': {
                'available': vc_health["state"] == "online",
                'running': voice_available,
                'state': vc_health["state"],
                'reason': vc_health["reason"],
                'status': v_stat.get('status', 'stopped'),
                'managed': voice_owned(conf),
                'hotkey': v_stat.get('hotkey', 'mouse_x1'),
                'engine': v_stat.get('engine', 'sensevoice_offline'),
            },
        },
        'last_up': _UP['last'], 'last_model': _UP['last_model']
    }
    total_ms = (time.perf_counter() - status_started) * 1000
    if total_ms >= 750:
        details = " ".join(f"{key}={value:.1f}" if isinstance(value, float) else f"{key}={value}"
                            for key, value in status_timings.items())
        print(f"[ui-status] /api/status slow total_ms={total_ms:.1f} {details}", file=sys.stderr, flush=True)
    return res


def _calls_snapshot(n):
    """Return cached, body-free call viewer summaries for one requested range."""
    mod = sys.modules.get("cc_relay")
    _iter_tail_fn = getattr(mod, "_iter_records_tail", _iter_records_tail) if mod else _iter_records_tail
    with _CALLS_LOCK:
        fingerprint = _records_fingerprint()
        key = (fingerprint, n)
        cached = _CALLS_CACHE.get(key)
        if cached is not None:
            calls, total = cached
            return {"calls": [dict(call) for call in calls], "total": total}
        recs = _iter_tail_fn(max_records=n, max_bytes=_CALLS_TAIL_BYTES)
        calls = []
        for r in recs:
            body = r.get("body") or {}
            headers = {k.lower(): v for k, v in (r.get("headers") or {}).items()}
            cache = r.get("cache") if r.get("cache") is not None else _resp_cache_usage(r.get("resp_body"))
            duration = r.get("duration")
            out_tok = r.get("output_tokens")
            if out_tok is None and isinstance(cache, dict):
                out_tok = cache.get("output_tokens")
            speed = r.get("speed")
            if speed is None and duration and out_tok and duration > 0 and out_tok > 0:
                speed = round(out_tok / duration, 1)
            calls.append({
                "idx": r.get("idx"),
                "ts": r.get("ts"),
                "path": r.get("path"),
                "orig_model": r.get("orig_model"),
                "sent_model": r.get("sent_model"),
                "route": r.get("route"),
                "route_reason": r.get("route_reason"),
                "status": r.get("resp_status"),
                "agent_id": (headers.get("x-claude-code-agent-id") or "")[:12],
                "session": (headers.get("x-claude-code-session-id") or "")[:8],
                "msgs": len(body.get("messages") or []),
                "tools": len(body.get("tools") or []),
                "stream": body.get("stream"),
                "cache": cache,
                "resp_error": r.get("resp_error"),
                "has_custom_prompt": bool(r.get("custom_prompt")),
                "stripped_banner": bool(r.get("stripped_banner")),
                "traffic_paused": bool(r.get("traffic_paused") or r.get("route") == "paused"),
                "paused_wait_sec": r.get("paused_wait_sec"),
                "duration": duration,
                "output_tokens": out_tok or 0,
                "speed": speed,
                "purpose": headers.get("x-cc-relay-purpose") or "",
                "app": headers.get("x-app") or "",
            })
        if len(_CALLS_CACHE) >= _CALLS_CACHE_LIMIT:
            _CALLS_CACHE.clear()
        _CALLS_CACHE[key] = (calls, len(calls))
        return {"calls": [dict(call) for call in calls], "total": len(calls)}
