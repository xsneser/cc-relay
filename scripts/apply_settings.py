#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude 配置管理工具 (cc-relay)
- 桌面端 (推荐): 为 Claude Desktop 配置官方 3P 推理网关策略 (HKCU\\SOFTWARE\\Policies\\Claude)
- CLI 端: 增量合并 settings.json 的 env 节点 (需显式指定 --cli)
- 支持 --dry-run 预览与 --remove-desktop 恢复官方默认
"""
import os
import sys
import json
import argparse

# 确保在 Windows 控制台输出中文不乱码
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = os.environ.get("CC_RELAY_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_KEY_PATH = r"SOFTWARE\Policies\Claude"


def load_relay_conf(conf_path=None):
    path = conf_path or os.path.join(BASE, "config.json")
    if not os.path.exists(path):
        path = os.path.join(BASE, "config.example.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 读取配置文件异常 ({e})")
    return {}


def is_loopback_host(host):
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "ip6-localhost")


def format_base_url(host, port):
    h = str(host or "127.0.0.1").strip()
    if ":" in h and not h.startswith("["):
        return f"http://[{h}]:{port}"
    return f"http://{h}:{port}"


def _sanitize_dict_for_preview(val):
    if isinstance(val, dict):
        out = {}
        sensitive_substrings = ("key", "token", "secret", "password", "credential")
        for k, v in val.items():
            lower_k = str(k).lower()
            if any(s in lower_k for s in sensitive_substrings):
                out[k] = "<redacted>" if v else ""
            else:
                out[k] = _sanitize_dict_for_preview(v)
        return out
    elif isinstance(val, list):
        return [_sanitize_dict_for_preview(x) for x in val]
    return val


def get_desktop_3p_dirs():
    """获取 Claude Desktop 3P 用户数据目录 (支持原生路径与 Windows Store/MSIX 虚拟化缓存)"""
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    target_dirs = [os.path.join(local_app_data, "Claude-3p")]

    msix_parent = os.path.join(local_app_data, "Packages", "Claude_pzs8sxrjxfjjc", "LocalCache", "Local")
    if os.path.exists(msix_parent):
        target_dirs.append(os.path.join(msix_parent, "Claude-3p"))

    return target_dirs


def apply_desktop_3p_policy(host, port, fake_key, dry_run=False):
    """为 Claude Desktop 写入本地 3P 配置文件与 configLibrary，启用官方 3P Gateway 推理网关"""
    base_url = format_base_url(host, port)
    models = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]
    profile_id = "00000000-0000-0000-0000-000000000001"

    profile_data = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayApiKey": fake_key,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": models,
    }

    meta_data = {
        "appliedId": profile_id,
        "entries": [
            {
                "id": profile_id,
                "name": "cc-relay",
            }
        ],
    }

    desktop_config = {
        "deploymentMode": "3p",
    }

    target_dirs = get_desktop_3p_dirs()

    if dry_run:
        print("=== [DRY RUN] Claude Desktop 官方 3P 配置预览 ===")
        print(f"目标目录: {target_dirs}")
        sanitized_profile = _sanitize_dict_for_preview(profile_data)
        print("Profile 文件内容:")
        print(json.dumps(sanitized_profile, ensure_ascii=False, indent=2))
        return

    try:
        for p_dir in target_dirs:
            os.makedirs(p_dir, exist_ok=True)
            cfg_path = os.path.join(p_dir, "claude_desktop_config.json")

            # 保持已有配置并注入 deploymentMode: 3p
            existing_cfg = {}
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        existing_cfg = json.load(f)
                except Exception:
                    existing_cfg = {}
            existing_cfg["deploymentMode"] = "3p"
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(existing_cfg, f, ensure_ascii=False, indent=2)

            # 写入 configLibrary
            lib_dir = os.path.join(p_dir, "configLibrary")
            os.makedirs(lib_dir, exist_ok=True)

            meta_path = os.path.join(lib_dir, "_meta.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)

            prof_path = os.path.join(lib_dir, f"{profile_id}.json")
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump(profile_data, f, ensure_ascii=False, indent=2)

        print("=" * 64)
        print("[OK] 已成功为 Claude Desktop 配置官方 3P 推理网关:")
        for p_dir in target_dirs:
            print(f"     已配置目录: {p_dir}")
        print(f"     中转端点: {base_url}")
        print(f"     认证方式: bearer (<redacted>)")
        print(f"     可用模型: {models}")
        print("     提示: 打开或重启 Claude 桌面客户端即可自动进入 3P 推理模式！")
        print("=" * 64)
    except Exception as e:
        print(f"[ERROR] 写入 3P 配置失败: {e}")
        sys.exit(1)


def remove_desktop_3p_policy(dry_run=False):
    """清理 Claude Desktop 的 3P 配置，恢复官方 1P 默认"""
    target_dirs = get_desktop_3p_dirs()

    if dry_run:
        print("=== [DRY RUN] 将清理 Claude Desktop 3P 配置 ===")
        for p_dir in target_dirs:
            print(f"     目标: {p_dir}")
        return

    try:
        cleaned_any = False
        for p_dir in target_dirs:
            cfg_path = os.path.join(p_dir, "claude_desktop_config.json")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        cfg = json.load(f)
                    if cfg.get("deploymentMode") == "3p":
                        cfg["deploymentMode"] = "1p"
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        cleaned_any = True
                except Exception:
                    pass

        print("=" * 64)
        print("[OK] 已成功切换 Claude Desktop 部署模式为官方 1P 默认。")
        print("     重启客户端即可恢复官方账号登录模式。")
        print("=" * 64)
    except Exception as e:
        print(f"[ERROR] 清理 3P 配置失败: {e}")
        sys.exit(1)


def apply_cli_settings(host, port, fake_key, settings_path, dry_run=False):
    """安全增量写入 ~/.claude/settings.json (仅当显式传入 --cli 时执行)"""
    os.makedirs(os.path.dirname(os.path.abspath(settings_path)), exist_ok=True)
    base_url = format_base_url(host, port)

    data = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
        except Exception as e:
            print(f"[ERROR] 无法解析已有 CLI 配置文件 ({e})，为防止损坏数据已终止操作。")
            sys.exit(1)

    env_node = data.setdefault("env", {})
    if not isinstance(env_node, dict):
        env_node = {}
        data["env"] = env_node

    relay_env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": fake_key,
        "ANTHROPIC_MODEL": "relay-main[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "OPUS_MODEL[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "SONNET_MODEL[1m]",
        "ANTHROPIC_SMALL_FAST_MODEL": "FAST_MODEL[1m]",
    }

    env_node.update(relay_env)
    output_str = json.dumps(data, ensure_ascii=False, indent=2)

    if dry_run:
        print("=== [DRY RUN] Claude Code CLI 配置预览 ===")
        print(f"目标路径: {settings_path}")
        sanitized = _sanitize_dict_for_preview(data)
        print(json.dumps(sanitized, ensure_ascii=False, indent=2))
        return

    tmp_path = settings_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(output_str + "\n")
    os.replace(tmp_path, settings_path)

    print("=" * 64)
    print("[OK] 已成功将 cc-relay 环境变量写入 Claude CLI 配置文件:")
    print(f"     {settings_path}")
    print(f"     中转地址: {base_url}")
    print("=" * 64)


def main():
    parser = argparse.ArgumentParser(description="Configure cc-relay for Claude Desktop or Claude Code CLI")
    parser.add_argument("--config", default=None, help="Path to cc-relay config.json")
    parser.add_argument("--desktop", action="store_true", help="Apply 3P inference gateway policy to Claude Desktop (default)")
    parser.add_argument("--remove-desktop", action="store_true", help="Remove 3P policy from Claude Desktop (revert to 1P)")
    parser.add_argument("--cli", action="store_true", help="Apply environment variables to Claude Code CLI (~/.claude/settings.json)")
    parser.add_argument("--settings", default=None, help="Custom path for CLI settings.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying file or registry")
    args = parser.parse_args()

    # 1. 优先处理恢复操作
    if args.remove_desktop:
        remove_desktop_3p_policy(dry_run=args.dry_run)
        return

    # 2. 读取 cc-relay 本地配置
    conf = load_relay_conf(args.config)
    host = conf.get("listen_host", "127.0.0.1")
    if not is_loopback_host(host):
        print(f"[ERROR] 拒绝将非本地回环地址 ({host}) 注入配置，保护本地安全。")
        sys.exit(1)
    port = conf.get("listen_port", 8400)
    fake_key = conf.get("fake_api_key", "sk-relay-local-0000")

    # 3. 如果显式指定了 --cli，才修改 CLI 配置
    if args.cli:
        settings_path = args.settings or os.path.expanduser(r"~/.claude/settings.json")
        apply_cli_settings(host, port, fake_key, settings_path, dry_run=args.dry_run)
        return

    # 4. 默认/显式 --desktop: 配置桌面客户端 3P 策略 (完全不影响 CLI)
    apply_desktop_3p_policy(host, port, fake_key, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
