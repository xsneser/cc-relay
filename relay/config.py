# -*- coding: utf-8 -*-
"""cc-relay 配置中枢、结构自愈校验与安全脱敏"""
import json
import os
import re
import shutil
import threading
from urllib.parse import urlsplit

from relay.constants import CONF, get_conf_path

CONF_LOCK = threading.Lock()
_CODEX_LOGIN = None
_CODEX_LOGIN_LOCK = threading.Lock()


def get_codex_login():
    global _CODEX_LOGIN
    with _CODEX_LOGIN_LOCK:
        if _CODEX_LOGIN is None:
            try:
                from relay.upstreams.codex_login import CodexLogin
            except ImportError:
                from codex_login import CodexLogin
            from relay.constants import BASE
            _CODEX_LOGIN = CodexLogin(BASE, load_conf, _save_conf, CONF_LOCK)
        return _CODEX_LOGIN

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 8400,
    "ui_port": 8610,
    "fake_api_key": "sk-relay-local-0000",
    "upstreams": {
        "deepseek": {
            "base": "https://api.deepseek.com/anthropic",
            "key_env": "real_deepseek_key",
            "proxy_url": "direct",
        },
        "codex": {
            "base": "http://127.0.0.1:8317",
            "key_env": "codex_proxy_key",
            "proxy_url": "direct",
        },
        "antigravity": {
            "base": "http://127.0.0.1:8045",
            "key_env": "antigravity_key",
            "proxy_url": "direct",
        },
    },
    "router": {
        "route": "hybrid",
        "hybrid_codex_model": "gpt-5.6-sol",
        "hybrid_deepseek_model": "deepseek-flash",
        "hybrid_antigravity_model": "gemini-3.7-flash-low",
        "effort": "high",
        "tiers": {
            "main": "deepseek-flash",
            "opus": "gpt-5.6-luna",
            "sonnet": "gpt-5.6-luna",
            "fast": "gpt-5.6-luna",
            "agent": "gpt-5.6-sol",
        },
        "tier_efforts": {
            "opus": "medium",
            "main": "high",
            "fast": "instant",
            "sonnet": "instant",
            "agent": "medium",
        },
    },
}

UPSTREAM_ALLOWED_KEYS = {
    "deepseek": {"real_deepseek_key", "deepseek_key"},
    "codex": {"codex_proxy_key", "codex_key"},
    "antigravity": {"antigravity_key", "gemini_key"},
    "gemini": {"antigravity_key", "gemini_key"},
}

UPSTREAM_CANONICAL_KEYS = {
    "deepseek": "real_deepseek_key",
    "codex": "codex_proxy_key",
    "antigravity": "antigravity_key",
    "gemini": "antigravity_key",
}

_CONFIG_KEY_MASK = "••••••••"


def _deep_merge_dict(base, patch):
    """递归合并字典, 避免部分更新覆盖同级键或被截断的字典清空关键配置节。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    res = dict(base)
    for k, v in patch.items():
        if k in res and isinstance(res[k], dict) and isinstance(v, dict):
            res[k] = _deep_merge_dict(res[k], v)
        else:
            res[k] = v
    return res


def is_loopback_host(host):
    """Only allow loopback addresses for local security."""
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "ip6-localhost")


def _canonical_key_field(upstream_name):
    return UPSTREAM_CANONICAL_KEYS.get(str(upstream_name or "").strip().lower(), "")


def _safe_key_env_name(upstream_name, value):
    """Return an allowed config field name for the given upstream, or empty string."""
    val = str(value or "").strip()
    allowed = UPSTREAM_ALLOWED_KEYS.get(str(upstream_name or "").strip().lower(), set())
    return val if val in allowed else ""


def _repair_misplaced_upstream_key(conf):
    """Recover a key pasted into upstream.key_env by an older/broken UI."""
    changed = False
    for name, upstream in (conf.get("upstreams") or {}).items():
        if not isinstance(upstream, dict):
            continue
        key_env = str(upstream.get("key_env") or "").strip()
        target = _canonical_key_field(name)
        if not target:
            continue
        safe_field = _safe_key_env_name(name, key_env)
        if safe_field:
            continue
        if key_env and not str(conf.get(target) or "").strip():
            if key_env not in ("fake_api_key", target):
                conf[target] = key_env
        upstream["key_env"] = target
        changed = True
    return changed


def _mask_config_key(value):
    """只向 UI 暴露固定掩码, 不泄露 key 长度或内容。"""
    return _CONFIG_KEY_MASK if value else ""


def _normalize_config_url(value, field, allow_empty=False, allow_direct=False):
    """校验并规范化配置页提交的 URL。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{field} cannot be empty")
    if allow_direct and value.lower() == "direct":
        return "direct"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} contains control characters")
    if "://" not in value:
        if value.startswith(("127.0.0.1", "localhost", "::1")):
            value = "http://" + value
        else:
            value = "https://" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError(f"{field} is invalid")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL")
    if not parsed.hostname:
        raise ValueError(f"{field} must include a host")
    return value.rstrip("/")


def load_conf():
    """从磁盘加载配置，附带防损坏回滚与缺失配置自愈"""
    conf_path = get_conf_path()
    conf = None
    try:
        with open(conf_path, encoding="utf-8") as f:
            conf = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        bak_file = conf_path + ".bak"
        if os.path.isfile(bak_file):
            try:
                with open(bak_file, encoding="utf-8") as f:
                    conf = json.load(f)
            except Exception:
                pass
    if not isinstance(conf, dict) or not conf:
        conf = dict(DEFAULT_CONFIG)

    # 兜底：若缺少核心配置节 (upstreams 或 router)，自动合并默认配置
    if not conf.get("upstreams") or not conf.get("router"):
        conf = _deep_merge_dict(DEFAULT_CONFIG, conf)

    if _repair_misplaced_upstream_key(conf):
        _save_conf(conf)
    return conf


def _save_conf(conf):
    """原子写回配置: 先写 .tmp 再 replace, 免并发丢更新 / 读到半份 JSON。
    同时进行防截断保护: 若传入的字典缺少核心配置节 (upstreams 或 router), 自动与现有文件深合并。"""
    conf_path = get_conf_path()
    try:
        if os.path.isfile(conf_path):
            with open(conf_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                needs_merge = False
                for core_sec in ("upstreams", "router"):
                    if core_sec in existing and (core_sec not in conf or not conf[core_sec]):
                        needs_merge = True
                        break
                if needs_merge:
                    conf = _deep_merge_dict(existing, conf)
    except Exception:
        pass

    try:
        if os.path.isfile(conf_path) and os.path.getsize(conf_path) > 50:
            shutil.copyfile(conf_path, conf_path + ".bak")
    except Exception:
        pass

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, conf_path)


def _dispatch_save_conf(conf):
    """支持测试对 cc_relay._save_conf 的 mock.patch"""
    import sys
    mod = sys.modules.get("cc_relay")
    if mod and hasattr(mod, "_save_conf") and mod._save_conf != _save_conf:
        return mod._save_conf(conf)
    return _save_conf(conf)


def _config_public_view(conf):
    """返回配置页所需的非敏感配置快照。"""
    upstreams = {}
    for name, upstream in (conf.get("upstreams") or {}).items():
        if not isinstance(upstream, dict):
            continue
        key_env = _safe_key_env_name(name, upstream.get("key_env")) or _canonical_key_field(name)
        key = conf.get(key_env, "") if key_env else ""
        upstreams[str(name)] = {
            "base": str(upstream.get("base") or ""),
            "proxy_url": str(upstream.get("proxy_url") or ""),
            "key_env": key_env,
            "key_mask": _mask_config_key(key),
            "has_key": bool(key),
        }
    custom_providers = {}
    for cp_id, cp_val in (conf.get("custom_providers") or {}).items():
        if not isinstance(cp_val, dict):
            continue
        cp_key = str(cp_val.get("key") or "").strip()
        custom_providers[str(cp_id)] = {
            "id": str(cp_id),
            "name": str(cp_val.get("name") or cp_id),
            "base": str(cp_val.get("base") or ""),
            "protocol": str(cp_val.get("protocol") or "openai").strip().lower(),
            "proxy_url": str(cp_val.get("proxy_url") or ""),
            "key_mask": _mask_config_key(cp_key),
            "has_key": bool(cp_key),
            "models": list(cp_val.get("models") or []),
        }

    # 动态获取 CPA 外发代理与语音设备状态，避免硬编码循环依赖
    outbound_proxy = ""
    try:
        from relay.upstreams import _read_codex_outbound_proxy
        outbound_proxy = _read_codex_outbound_proxy(conf)
    except Exception:
        pass

    tools_conf = conf.get("tools") or {}
    codex_view = {
        "auto_update": ((tools_conf.get("codex") or {}).get("auto_update") is True),
        "outbound_proxy": outbound_proxy,
    }

    v_conf = tools_conf.get("voice") or {}
    configured_device = str(v_conf.get("device") or "auto")
    available_devices = []
    device_discovery = {"loading": False, "error": ""}
    try:
        from relay.voice_bridge import get_voice_available_devices, voice_device_discovery_state
        available_devices = get_voice_available_devices(required_device=configured_device)
        device_discovery = voice_device_discovery_state()
    except Exception:
        pass

    voice_view = {
        "auto_start": bool(v_conf.get("auto_start", False)),
        "hotkey": str(v_conf.get("hotkey") or "mouse_x1"),
        "engine": str(v_conf.get("engine") or "sensevoice_offline"),
        "device": configured_device,
        "vram_mode": str(v_conf.get("vram_mode") or "resident"),
        "available_devices": available_devices,
        "available_devices_loading": device_discovery.get("loading", False),
        "available_devices_error": device_discovery.get("error", ""),
        "port": int(v_conf.get("port") or 8401),
        "restore_clipboard": bool(v_conf.get("restore_clipboard", True)),
        "semantic_correction_enabled": bool(v_conf.get("semantic_correction_enabled", False)),
        "semantic_correction_model": str(v_conf.get("semantic_correction_model") or "codex:gpt-5.6-luna"),
        "semantic_correction_effort": str(v_conf.get("semantic_correction_effort", "low")),
        "semantic_correction_timeout_seconds": float(v_conf.get("semantic_correction_timeout_seconds") or 8.0),
        "semantic_correction_context_chars": int(v_conf.get("semantic_correction_context_chars") or 4000),
    }

    return {
        "listen_host": str(conf.get("listen_host") or "127.0.0.1"),
        "listen_port": int(conf.get("listen_port") or 8400),
        "ui_port": int(conf.get("ui_port") or 8610),
        "upstreams": upstreams,
        "router": dict(conf.get("router") or {}),
        "traffic_paused": bool(conf.get("traffic_paused", False)),
        "tools": {
            "codex": codex_view,
            "antigravity": {
                "auto_start": bool((tools_conf.get("antigravity") or {}).get("auto_start", True)),
            },
            "voice": voice_view,
        },
        "custom_providers": custom_providers,
    }


def _apply_config_update(conf, data):
    """校验并合并配置页更新, 返回新的非敏感快照。"""
    if not isinstance(data, dict):
        raise ValueError("request body must be an object")
    candidate = json.loads(json.dumps(conf, ensure_ascii=False))

    updates = data.get("upstreams")
    if updates is not None:
        if not isinstance(updates, dict):
            raise ValueError("upstreams must be an object")
        configured = candidate.get("upstreams") or {}
        unknown = set(updates) - set(configured)
        if unknown:
            raise ValueError("unknown upstream: " + ", ".join(sorted(map(str, unknown))))
        for name, patch in updates.items():
            if not isinstance(patch, dict):
                raise ValueError(f"upstreams.{name} must be an object")
            upstream = configured[name]
            if not isinstance(upstream, dict):
                raise ValueError(f"upstreams.{name} is invalid")
            if "base" in patch:
                upstream["base"] = _normalize_config_url(
                    patch["base"], f"upstreams.{name}.base")
            if "proxy_url" in patch:
                upstream["proxy_url"] = _normalize_config_url(
                    patch["proxy_url"], f"upstreams.{name}.proxy_url",
                    allow_empty=True, allow_direct=True)
            key_env = _safe_key_env_name(name, upstream.get("key_env")) or _canonical_key_field(name)
            if not key_env:
                raise ValueError(f"upstreams.{name} has an invalid key_env")
            upstream["key_env"] = key_env
            if "key" in patch:
                key = patch["key"]
                if not isinstance(key, str):
                    raise ValueError(f"upstreams.{name}.key must be a string")
                if key.strip() and key.strip() != _CONFIG_KEY_MASK:
                    candidate[key_env] = key.strip()

    tools_update = data.get("tools")
    if tools_update is not None:
        if not isinstance(tools_update, dict):
            raise ValueError("tools must be an object")
        tools = candidate.setdefault("tools", {})
        if "codex" in tools_update:
            c_patch = tools_update["codex"]
            if not isinstance(c_patch, dict):
                raise ValueError("tools.codex must be an object")
            if "auto_update" in c_patch:
                if not isinstance(c_patch["auto_update"], bool):
                    raise ValueError("tools.codex.auto_update must be a boolean")
                tools.setdefault("codex", {})["auto_update"] = c_patch["auto_update"]
            if "outbound_proxy" in c_patch:
                raw_proxy = str(c_patch["outbound_proxy"]).strip()
                if raw_proxy and raw_proxy.lower() not in ("direct", "none"):
                    if not (raw_proxy.startswith("http://") or raw_proxy.startswith("https://") or raw_proxy.startswith("socks5://")):
                        raise ValueError("Codex 外部代理必须以 http://、https:// 或 socks5:// 开头，或填 direct/留空")
                try:
                    from relay.upstreams import _write_codex_outbound_proxy
                    _write_codex_outbound_proxy(candidate, raw_proxy)
                except Exception:
                    pass
        if "antigravity" in tools_update:
            ag = tools.setdefault("antigravity", {})
            if "auto_start" in tools_update["antigravity"]:
                ag["auto_start"] = bool(tools_update["antigravity"]["auto_start"])
        if "voice" in tools_update:
            v_patch = tools_update["voice"]
            if not isinstance(v_patch, dict):
                raise ValueError("tools.voice must be an object")
            v_conf = tools.setdefault("voice", {})
            if "auto_start" in v_patch:
                v_conf["auto_start"] = bool(v_patch["auto_start"])
            if "hotkey" in v_patch:
                v_conf["hotkey"] = str(v_patch["hotkey"]).strip().lower()
            if "engine" in v_patch:
                engine = str(v_patch["engine"]).strip()
                if engine not in ("qwen_2pass", "qwen_offline", "sherpa_2pass", "sensevoice_offline", "paraformer_streaming_2pass"):
                    raise ValueError(f"unsupported engine: {engine}")
                v_conf["engine"] = engine
            if "device" in v_patch:
                dev = str(v_patch["device"]).strip().lower()
                if not (dev in ("auto", "cpu") or dev.startswith("cuda")):
                    raise ValueError(f"unsupported device: {dev}")
                v_conf["device"] = dev
            if "vram_mode" in v_patch:
                vram_mode = str(v_patch["vram_mode"]).strip().lower()
                if vram_mode not in ("resident", "on_demand_offload"):
                    raise ValueError(f"unsupported vram_mode: {vram_mode}")
                v_conf["vram_mode"] = vram_mode
            if "port" in v_patch:
                port = int(v_patch["port"])
                if not (1024 <= port <= 65535):
                    raise ValueError("port must be between 1024 and 65535")
                v_conf["port"] = port
            if "restore_clipboard" in v_patch:
                v_conf["restore_clipboard"] = bool(v_patch["restore_clipboard"])
            if "semantic_correction_enabled" in v_patch:
                if not isinstance(v_patch["semantic_correction_enabled"], bool):
                    raise ValueError("tools.voice.semantic_correction_enabled must be a boolean")
                v_conf["semantic_correction_enabled"] = v_patch["semantic_correction_enabled"]
            if "semantic_correction_model" in v_patch:
                v_conf["semantic_correction_model"] = str(v_patch["semantic_correction_model"] or "").strip()
            if "semantic_correction_effort" in v_patch:
                eff = str(v_patch["semantic_correction_effort"] or "").strip().lower()
                if eff and eff not in ("off", "low", "medium", "high", "xhigh", "max"):
                    raise ValueError(f"unsupported semantic_correction_effort: {eff}")
                v_conf["semantic_correction_effort"] = eff
            if "semantic_correction_timeout_seconds" in v_patch:
                timeout = v_patch["semantic_correction_timeout_seconds"]
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= 120):
                    raise ValueError("semantic correction timeout must be between 1 and 120 seconds")
                v_conf["semantic_correction_timeout_seconds"] = float(timeout)
            if "semantic_correction_context_chars" in v_patch:
                context_chars = v_patch["semantic_correction_context_chars"]
                if isinstance(context_chars, bool) or not isinstance(context_chars, int) or not (512 <= context_chars <= 12000):
                    raise ValueError("semantic correction context size must be between 512 and 12000 characters")
                v_conf["semantic_correction_context_chars"] = context_chars

    cp_updates = data.get("custom_providers")
    if cp_updates is not None:
        if not isinstance(cp_updates, dict):
            raise ValueError("custom_providers must be an object")
        existing_cp = candidate.setdefault("custom_providers", {})
        for cp_id, cp_patch in cp_updates.items():
            cp_id_str = str(cp_id).strip()
            if not re.match(r'^[a-zA-Z0-9_\-\.]{1,64}$', cp_id_str):
                raise ValueError(f"invalid custom provider ID: '{cp_id}'")
            if cp_patch is None or (isinstance(cp_patch, dict) and cp_patch.get("deleted") is True):
                existing_cp.pop(cp_id_str, None)
                continue
            if not isinstance(cp_patch, dict):
                raise ValueError(f"custom_providers.{cp_id} must be an object")
            entry = existing_cp.setdefault(cp_id_str, {})
            if "name" in cp_patch:
                entry["name"] = str(cp_patch["name"] or cp_id_str).strip()
            elif "name" not in entry:
                entry["name"] = cp_id_str
            if "protocol" in cp_patch:
                proto = str(cp_patch["protocol"] or "openai").strip().lower()
                if proto not in ("openai", "anthropic"):
                    raise ValueError(f"custom_providers.{cp_id}.protocol must be 'openai' or 'anthropic'")
                entry["protocol"] = proto
            elif "protocol" not in entry:
                entry["protocol"] = "openai"
            if "base" in cp_patch:
                entry["base"] = _normalize_config_url(
                    cp_patch["base"], f"custom_providers.{cp_id}.base", allow_empty=True)
            if "proxy_url" in cp_patch:
                entry["proxy_url"] = _normalize_config_url(
                    cp_patch["proxy_url"], f"custom_providers.{cp_id}.proxy_url",
                    allow_empty=True, allow_direct=True)
            if "key" in cp_patch:
                key_val = str(cp_patch["key"] or "").strip()
                if key_val and key_val != _CONFIG_KEY_MASK:
                    entry["key"] = key_val
            if "models" in cp_patch:
                m_val = cp_patch["models"]
                if isinstance(m_val, list):
                    entry["models"] = [str(x).strip() for x in m_val if str(x).strip()]
                elif isinstance(m_val, str):
                    entry["models"] = [x.strip() for x in m_val.replace(",", "\n").splitlines() if x.strip()]

    _dispatch_save_conf(candidate)
    return _config_public_view(candidate)
