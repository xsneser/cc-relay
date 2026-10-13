# -*- coding: utf-8 -*-
"""cc-relay 5 档分层路由引擎、子代理特征嗅探与模型提供商仲裁"""
import re

from relay.config import UPSTREAM_ALLOWED_KEYS, _canonical_key_field, _safe_key_env_name

CODEX_MODELS = []
DEEPSEEK_MODELS = []
GEMINI_MODELS = []
DEFAULT_CODEX_MAP = "gpt-5.6-sol"
DEFAULT_DS_MAP = "deepseek-flash"
DEFAULT_GEMINI_MAP = "gemini-3.7-flash-low"

REASONING_MAP = {
    "off":     {"type": "disabled"},
    "on":      {"type": "enabled"},
    "instant": {"type": "enabled", "budget_tokens": 1024},
    "low":     {"type": "enabled", "budget_tokens": 1024},
    "medium":  {"type": "enabled", "budget_tokens": 8192},
    "high":    {"type": "enabled", "budget_tokens": 16384},
    "xhigh":   {"type": "enabled", "budget_tokens": 32768},
    "max":     {"type": "enabled", "budget_tokens": 65536},
}
EFFORT_VALUES = tuple(REASONING_MAP)


def _strip_model_suffix(model):
    """剥离 CC 附加的后缀: deepseek-chat[1m] / relay-main[1m] / 本地中转[1m] -> 基名"""
    if not model:
        return "", ""
    m = re.match(r'^\s*(.+?)\s*\[([^\]]+)\]\s*$', model)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return model.strip(), ""


RELAY_MAIN_PLACEHOLDERS = {
    "本地中转",
    "relay",
    "local",
    "relay-main",
    "cc-relay",
    "main",
}


def is_relay_placeholder(model: str) -> bool:
    """判断是否为中转占位模型名 (不区分大小写，支持常见变体与前缀)"""
    if not model:
        return True
    m = str(model).strip().lower()
    if m in RELAY_MAIN_PLACEHOLDERS:
        return True
    if m.startswith(("relay-main", "relay_", "local-", "local_")):
        return True
    if m in ("relay", "local") or "本地中转" in m:
        return True
    return False


def is_cli_placeholder(model: str) -> bool:
    """判断是否为 CLI 占位符模型名 (如 relay-main, OPUS_MODEL, SONNET_MODEL, FAST_MODEL 等)"""
    if not model:
        return True
    m = str(model).strip().lower()
    if is_relay_placeholder(m):
        return True
    if m in ("opus_model", "sonnet_model", "fast_model", "relay-opus", "relay-sonnet", "relay-fast"):
        return True
    return False


def _classify_request_role(headers, body_json, requested_model=""):
    """统一角色分流函数：无论是占位符 relay-main 还是官方模型名 (claude-*)，
    统一根据请求头与 system 提示词内容区分角色分流：
    1. 优先子代理检测与角色细分：
       - Plan 代理 -> agent 档 (架构设计与长程规划)
       - Explore 代理 -> opus 档 (代码搜索与大上下文探索)
       - 安全审查 / 状态行 / Guide 等特定辅助代理 -> sonnet 档
       - 其他子代理 (携带 Agent ID 或 cc_is_subagent) -> agent 档 (默认主力智能体)
    2. 辅助轻量任务：
       - 会话命名 / 快速任务 (naming a coding session 等) -> fast 档
       - 未知纯 Haiku 请求 -> fast 档
    3. 主会话循环 (CLI 默认、Desktop 默认等) -> main 档
    """
    sys_field = (body_json or {}).get("system")
    txt = ""
    if isinstance(sys_field, str):
        txt = sys_field
    elif isinstance(sys_field, list):
        txt = " ".join((c.get("text") or "") for c in sys_field if isinstance(c, dict))

    # 1. Plan 规划代理 (优先级最高，确保精准走 agent)
    if any(sig in txt for sig in (
        "software architect and planning specialist",
        "planning specialist for Claude Code",
        "software architect agent for designing implementation plans",
    )):
        return "agent"

    # 2. Explore 搜索代理 (走 opus 档)
    if any(sig in txt for sig in (
        "file search specialist for Claude Code",
        "thoroughly navigating and exploring codebases",
    )):
        return "opus"

    # 3. 安全审查 / 状态行 / Guide 等特定辅助代理
    if any(sig in txt for sig in (
        "security monitor for autonomous AI coding agents",
        "status line setup agent for Claude Code",
        "You are the Claude guide agent",
        "claude-code-guide",
    )):
        return "sonnet"

    # 4. 会话命名 / 快速任务
    if any(sig in txt for sig in (
        "naming a coding session",
        "short noun phrase of two to five words",
    )):
        return "fast"

    # 5. 明确带有子代理标记 (有 agent-id 或 cc_is_subagent=true 或 Claude Agent SDK) -> 走 agent 档
    if _is_subagent(headers, body_json):
        return "agent"

    # 6. 未知纯 Haiku 请求 -> 走 fast 档
    if str(requested_model or "").lower().startswith("claude-haiku"):
        return "fast"

    # 7. 其余（包括带 <application_details> 的 Desktop 主循环等）一律视为主循环 -> main
    return "main"


# 保留别名以保持兼容
_route_by_system_prompt = _classify_request_role


def _is_subagent(headers, body_json):
    """识别子代理: 请求头带 x-claude-code-agent-id (大小写不敏感), 或 system 里 billing header
    含 cc_is_subagent=true, 或提示含 'Claude Agent SDK' / 'You are a Claude agent'
    / 'software architect and planning specialist' 等 (Plan 代理特定特征)"""
    try:
        hdrs = headers or {}
        if hasattr(hdrs, "items"):
            for k, v in hdrs.items():
                if str(k).strip().lower() == "x-claude-code-agent-id" and v:
                    return True
        sys_field = (body_json or {}).get("system")
        txt = ""
        if isinstance(sys_field, str):
            txt = sys_field
        elif isinstance(sys_field, list):
            txt = " ".join((c.get("text") or "") for c in sys_field if isinstance(c, dict))
        signatures = (
            "cc_is_subagent=true",
            "Claude Agent SDK",
            "You are a Claude agent",
            "software architect and planning specialist",
            "planning specialist for Claude Code",
            "software architect agent for designing implementation plans",
            "file search specialist for Claude Code",
            "thoroughly navigating and exploring codebases",
            "You are a software engineer for Claude Code",
        )
        return any(sig in txt for sig in signatures)
    except Exception:
        return False


CC_ID_PAT = (r"(?:You are Claude Code,?\s*Anthropic's official CLI for Claude\.?"
             r"|You are a Claude agent,\s*built on Anthropic's Claude Agent SDK\.?)")
CC_BILLING_PREFIX = "x-anthropic-billing-header:"

TIER_KEYS = ("main", "opus", "sonnet", "fast", "agent")
LEGACY_STRIP_DEFAULTS = {"main": True, "opus": False, "sonnet": False, "fast": True, "agent": False}


def _strip_flags(router):
    """开关归一化 -> 五键布尔对象 (读接口 / 生效判断 / 写入合并的唯一入口)"""
    v = router.get("strip_cc_banner")
    if isinstance(v, dict):
        return {k: v.get(k) is True for k in TIER_KEYS}
    if v is True:
        return dict(LEGACY_STRIP_DEFAULTS)
    return {k: False for k in TIER_KEYS}


def _tier_from_reason(reason):
    """reason -> 内部档位键; 兼容新旧名称, 非档位(hybrid:gpt-direct / route:*)返回 None"""
    if not isinstance(reason, str) or not reason.startswith("hybrid:"):
        return None
    t = reason[len("hybrid:"):]
    t = {"explore": "opus", "plan": "agent"}.get(t, t)
    return t if t in TIER_KEYS else None


def _upstream_conf(conf, name):
    """读取 provider 配置, 兼容旧配置里的 antigravity upstream 命名, 以及自定义供应商"""
    ups = conf.get("upstreams") or {}
    if name == "antigravity":
        return ups.get("gemini") or ups.get("antigravity") or {}
    if name in ups:
        return ups[name]
    cps = conf.get("custom_providers") or {}
    if name in cps and isinstance(cps[name], dict):
        return cps[name]
    return {}


def is_upstream_configured(conf, name):
    """检查指定上游是否配置了有效的基础 URL。"""
    up = _upstream_conf(conf, name)
    return bool(isinstance(up, dict) and (up.get("base") or "").strip())


def get_configured_upstreams(conf):
    """返回所有配置了有效 base URL 的上游名称集合。"""
    res = set()
    ups = conf.get("upstreams") or {}
    for name in ("deepseek", "codex", "antigravity"):
        if is_upstream_configured(conf, name):
            res.add(name)
    for name, u in ups.items():
        if isinstance(u, dict) and (u.get("base") or "").strip():
            res.add(name)
    cps = conf.get("custom_providers") or {}
    for cpid, cp in cps.items():
        if isinstance(cp, dict) and (cp.get("base") or "").strip():
            res.add(cpid)
    return res


def _key_for(conf, up, name=None):
    if not isinstance(up, dict):
        return ""
    if "key" in up and str(up.get("key") or "").strip():
        return str(up.get("key")).strip()
    field = up.get("key_env")
    if name:
        field = _safe_key_env_name(name, field) or _canonical_key_field(name)
    else:
        all_allowed = set().union(*UPSTREAM_ALLOWED_KEYS.values())
        if field not in all_allowed:
            return ""
    return conf.get(field, "") or ""


def provider_for_model(model, conf=None):
    """显式模型归属优先, 前缀仅作兼容回退; 未知模型返回 None。"""
    model = (model or "").strip()
    if not model:
        return None
    conf = conf or {}
    routes = conf.get("model_routes") or {}
    if model in routes:
        p = routes[model]
        return "antigravity" if p == "gemini" else p
    cps = conf.get("custom_providers") or {}
    for cpid, cp in cps.items():
        if not isinstance(cp, dict):
            continue
        if model in (cp.get("models") or []):
            return cpid
    for p, values in (("deepseek", DEEPSEEK_MODELS), ("codex", CODEX_MODELS),
                      ("antigravity", GEMINI_MODELS)):
        if model in values:
            return p
    if model.startswith("gpt-"):
        return "codex"
    if model.startswith("deepseek-"):
        return "deepseek"
    if model.startswith("gemini-"):
        return "antigravity"
    for cpid in cps:
        if model.startswith(cpid + "/"):
            return cpid
    return None


def pick_route(conf, headers, body_json):
    """统一路由: 返回 (upstream_name, map_model|None, reason)
    router.route = "hybrid" | "codex" | "deepseek" | "antigravity"
    router.model = 全量路由的指定模型(可空=用默认)
    """
    def _resolve():
        router = conf.get("router") or {}
        route = (router.get("route") or "hybrid").strip().lower()
        if route == "gemini":
            route = "antigravity"
        forced = (router.get("model") or "").strip()
        raw_model = ""
        if isinstance(body_json, dict):
            raw_model = body_json.get("model") or ""
        model, _suffix = _strip_model_suffix(raw_model)
        if is_relay_placeholder(model):
            model = ""

        if route == "codex":
            m = forced if provider_for_model(forced, conf) == "codex" else DEFAULT_CODEX_MAP
            return "codex", m, "route:codex"
        if route == "deepseek":
            m = forced or DEFAULT_DS_MAP
            return "deepseek", m, "route:deepseek"
        if route == "antigravity":
            m = forced or (router.get("hybrid_antigravity_model") or "").strip() or DEFAULT_GEMINI_MAP
            return "antigravity", m, "route:antigravity"
        cps = conf.get("custom_providers") or {}
        if route in cps:
            cp_models = cps[route].get("models") or []
            m = forced or (cp_models[0] if cp_models else "")
            return route, m, f"route:{route}"

        hv = (router.get("hybrid_codex_model") or router.get("model") or "").strip() or DEFAULT_CODEX_MAP
        dd = (router.get("hybrid_deepseek_model") or "").strip() or DEFAULT_DS_MAP
        tier = router.get("tiers") or {}

        def _up(m):
            return provider_for_model(m, conf) or "deepseek"

        raw_base, _ = _strip_model_suffix(raw_model)
        m_lower = raw_base.lower()

        # 阶段一：显式专属占位符
        if m_lower in ("opus_model", "relay-opus"):
            m = (tier.get("opus") or hv).strip()
            return _up(m), m, "hybrid:explore"
        if m_lower in ("sonnet_model", "relay-sonnet"):
            m = (tier.get("sonnet") or dd).strip()
            return _up(m), m, "hybrid:sonnet"
        if m_lower in ("fast_model", "relay-fast"):
            m = (tier.get("fast") or dd).strip()
            return _up(m), m, "hybrid:fast"

        # 阶段二：直连已知 provider 模型
        p_direct = provider_for_model(model, conf)
        if p_direct == "codex":
            return "codex", model, "hybrid:gpt-direct"
        if p_direct == "antigravity":
            return "antigravity", model, "hybrid:antigravity-direct"
        if p_direct and p_direct != "deepseek":
            return p_direct, model, f"hybrid:{p_direct}-direct"

        # 阶段三：统一智能角色分流
        role = _classify_request_role(headers, body_json, requested_model=raw_base)
        if role == "agent":
            m = (tier.get("agent") or dd).strip()
            return _up(m), m, "hybrid:plan"
        elif role == "opus":
            m = (tier.get("opus") or hv).strip()
            return _up(m), m, "hybrid:explore"
        elif role == "sonnet":
            m = (tier.get("sonnet") or dd).strip()
            return _up(m), m, "hybrid:sonnet"
        elif role == "fast":
            m = (tier.get("fast") or dd).strip()
            return _up(m), m, "hybrid:fast"
        else:
            m = (tier.get("main") or dd).strip()
            return _up(m), m, "hybrid:main"

    up_name, map_model, reason = _resolve()

    # 兜底阶段：上游可用性防 502 降级检查
    configured = get_configured_upstreams(conf)
    if configured and up_name not in configured:
        router = conf.get("router") or {}
        tier = router.get("tiers") or {}
        dd = (router.get("hybrid_deepseek_model") or "").strip() or DEFAULT_DS_MAP
        main_m = (tier.get("main") or dd).strip()
        main_up = provider_for_model(main_m, conf) or "deepseek"

        fallback_up = None
        if main_up in configured:
            fallback_up = main_up
            map_model = main_m
        elif "deepseek" in configured:
            fallback_up = "deepseek"
            map_model = router.get("hybrid_deepseek_model") or DEFAULT_DS_MAP
        elif "antigravity" in configured:
            fallback_up = "antigravity"
            map_model = router.get("hybrid_antigravity_model") or DEFAULT_GEMINI_MAP
        else:
            fallback_up = next(iter(configured))
            if fallback_up == "codex":
                map_model = router.get("hybrid_codex_model") or DEFAULT_CODEX_MAP
            elif fallback_up == "deepseek":
                map_model = router.get("hybrid_deepseek_model") or DEFAULT_DS_MAP
            elif fallback_up == "antigravity":
                map_model = router.get("hybrid_antigravity_model") or DEFAULT_GEMINI_MAP
            else:
                cps = conf.get("custom_providers") or {}
                cp = cps.get(fallback_up) or {}
                map_model = (cp.get("models") or [""])[0] or fallback_up
        reason = f"fallback:{up_name}->{fallback_up} ({reason})"
        up_name = fallback_up

    return up_name, map_model, reason
