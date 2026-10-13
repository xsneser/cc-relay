# -*- coding: utf-8 -*-
"""cc-relay CC 指纹清理、提示词抓包与动态修改器管理"""
import json
import os
import re
import sys
import threading

from relay.constants import BASE, RECORDS, get_prompts_path
from relay.router import CC_BILLING_PREFIX, CC_ID_PAT, TIER_KEYS, _tier_from_reason


def _strip_cc_fingerprint(body):
    """删除 system 里的 CC 身份句与 billing 指纹块, 返回 (新 body, 删除块数)
    - 整块命中 -> 整块丢弃; 句子混在别的文本里 -> 只摘句子
    - 被删块的 cache_control 顺延给其后第一个没有该标记的幸存块, 免得白白丢 prompt-cache 断点
    """
    sysv = body.get("system")
    if sysv is None:
        return body, 0

    # 客户端直接发字符串形式
    if isinstance(sysv, str):
        ns = re.sub(CC_ID_PAT, "", sysv)
        if ns == sysv:
            return body, 0
        nb = dict(body)
        if ns.strip():
            nb["system"] = ns
        else:
            nb.pop("system", None)
        return nb, 1

    if not isinstance(sysv, list):
        return body, 0

    kept = []
    carried = []   # (被删块本该落到的下标, cache_control)
    removed = 0
    for blk in sysv:
        txt = blk.get("text") if isinstance(blk, dict) else None
        if isinstance(txt, str):
            t = txt.strip()
            if t.startswith(CC_BILLING_PREFIX) or re.fullmatch(CC_ID_PAT, t):
                removed += 1
                if blk.get("cache_control"):
                    carried.append((len(kept), blk["cache_control"]))
                continue
            if re.search(CC_ID_PAT, txt):
                nb_blk = dict(blk)
                nb_blk["text"] = re.sub(CC_ID_PAT, "", txt)
                removed += 1
                kept.append(nb_blk)
                continue
        kept.append(blk)

    if not removed:
        return body, 0

    # 缓存断点顺延: 优先落给其后最近的幸存块; 该处已有断点则不动;
    # 被删的是尾块时向前落到最近的幸存块
    for pos, cc in carried:
        placed = False
        for i in range(pos, len(kept)):
            blk = kept[i]
            if not isinstance(blk, dict):
                continue
            if blk.get("cache_control"):
                placed = True
                break
            nb_blk = dict(blk)
            nb_blk["cache_control"] = cc
            kept[i] = nb_blk
            placed = True
            break
        if placed:
            continue
        for i in range(min(pos, len(kept)) - 1, -1, -1):
            blk = kept[i]
            if not isinstance(blk, dict):
                continue
            if blk.get("cache_control"):
                break
            nb_blk = dict(blk)
            nb_blk["cache_control"] = cc
            kept[i] = nb_blk
            break

    nb = dict(body)
    if kept:
        nb["system"] = kept
    else:
        nb.pop("system", None)
    return nb, removed


def builtin_strip_cc_fingerprint(headers, body):
    """供外部修改器调用的公共指纹清理入口"""
    cleaned_body, _ = _strip_cc_fingerprint(body)
    cleaned_headers = dict(headers or {})
    for k in list(cleaned_headers.keys()):
        if str(k).lower().startswith("x-anthropic-billing"):
            cleaned_headers.pop(k, None)
    return cleaned_headers, cleaned_body


MODIFIER_FILE = os.path.join(BASE, "custom_modifier.py")


class ModifierManager:
    """管理 custom_modifier.py 的热重载与安全调用"""
    def __init__(self):
        self._mod = None
        self._mtime = 0.0
        self._err = None

    def get_modifier(self):
        """检查 custom_modifier.py 修改时间，有变更时自动热重载"""
        try:
            if not os.path.exists(MODIFIER_FILE):
                return None
            mtime = os.path.getmtime(MODIFIER_FILE)
            if mtime != self._mtime or self._mod is None:
                import importlib.util
                spec = importlib.util.spec_from_file_location("custom_modifier", MODIFIER_FILE)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    self._mod = mod
                    self._mtime = mtime
                    self._err = None
        except Exception as e:
            self._err = repr(e)
            print(f"[WARN] 加载 custom_modifier.py 失败: {e}", file=sys.stderr)
        return self._mod


_MOD_MGR = ModifierManager()

PROMPTS_FILE = get_prompts_path()
PROMPTS_LOCK = threading.RLock()


class PromptManager:
    """管理各档位 System 提示词的抓包捕获、在线编辑与请求体替换"""
    def __init__(self):
        self._data = {
            "captured": {k: "" for k in TIER_KEYS},
            "custom": {k: "" for k in TIER_KEYS},
            "enabled": {k: False for k in TIER_KEYS},
        }
        self.load()

    def load(self):
        with PROMPTS_LOCK:
            p_file = get_prompts_path()
            if os.path.exists(p_file):
                try:
                    with open(p_file, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        for section in ("captured", "custom", "enabled"):
                            if isinstance(d.get(section), dict):
                                self._data[section].update(d[section])
                except Exception:
                    pass

    def save(self):
        with PROMPTS_LOCK:
            p_file = get_prompts_path()
            tmp = p_file + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p_file)
            except Exception:
                pass

    def get_data(self):
        with PROMPTS_LOCK:
            return {
                "captured": dict(self._data["captured"]),
                "custom": dict(self._data["custom"]),
                "enabled": dict(self._data["enabled"]),
                "counts": {
                    k: {
                        "captured_len": len(self._data["captured"].get(k, "")),
                        "custom_len": len(self._data["custom"].get(k, "")),
                        "enabled": bool(self._data["enabled"].get(k, False)),
                    }
                    for k in TIER_KEYS
                }
            }

    def update_tier(self, tier, custom_text=None, enabled=None):
        if tier not in TIER_KEYS:
            return False
        with PROMPTS_LOCK:
            if custom_text is not None:
                self._data["custom"][tier] = str(custom_text)
            if enabled is not None:
                self._data["enabled"][tier] = bool(enabled)
        self.save()
        return True

    def capture(self, tier, body_json):
        """运行时抓包: 记录进入中转的原版 system 提示词"""
        if tier not in TIER_KEYS or not isinstance(body_json, dict):
            return
        sysv = body_json.get("system")
        if not sysv:
            return
        if isinstance(sysv, list):
            txt = "\n\n".join(blk.get("text", "") for blk in sysv if isinstance(blk, dict) and blk.get("text"))
        else:
            txt = str(sysv)
        txt = txt.strip()
        if not txt:
            return
        if self._data["captured"].get(tier) != txt:
            with PROMPTS_LOCK:
                self._data["captured"][tier] = txt
            self.save()

    def apply_custom(self, tier, body_json):
        """若开启了自定义提示词，将修改后的提示词注入/替换到 body_json 中，并保留 cache_control"""
        if tier not in TIER_KEYS or not isinstance(body_json, dict):
            return body_json, False
        if not self._data["enabled"].get(tier):
            return body_json, False
        custom_txt = (self._data["custom"].get(tier) or "").strip()
        if not custom_txt:
            return body_json, False

        orig_sys = body_json.get("system")
        nb = dict(body_json)
        if isinstance(orig_sys, list) and orig_sys:
            cc = None
            for blk in orig_sys:
                if isinstance(blk, dict) and blk.get("cache_control"):
                    cc = blk["cache_control"]
                    break
            new_blk = {"type": "text", "text": custom_txt}
            if cc:
                new_blk["cache_control"] = cc
            nb["system"] = [new_blk]
        else:
            nb["system"] = custom_txt
        return nb, True


_PROMPT_MGR = PromptManager()
