"""安装包暂存目录组装脚本 (prepare_staging.py)
将构建产物、ASR 离线运行时、模型及伴侣服务资源精确组装至 installer_build/staging/
排除源码开发环境冗余文件 (.venv, __pycache__, 日志文件等)
"""

import fnmatch
import os
import shutil
import sys
from pathlib import Path

# 控制台 UTF-8 支持
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = REPO_ROOT / "installer_build" / "staging"


def copy_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  [+] {src.relative_to(REPO_ROOT)} -> {dst.relative_to(REPO_ROOT)}")


def copy_tree_filtered(src_dir: Path, dst_dir: Path, ignore_patterns=None):
    if not src_dir.is_dir():
        print(f"  [-] 目录不存在，跳过: {src_dir}")
        return

    dst_dir.mkdir(parents=True, exist_ok=True)

    def should_ignore(path: Path):
        name = path.name
        if ignore_patterns:
            for pat in ignore_patterns:
                if fnmatch.fnmatch(name, pat):
                    return True
        return False

    count = 0
    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel_root

        # 过滤目录
        dirs[:] = [d for d in dirs if not should_ignore(Path(d))]

        for f in files:
            file_path = Path(root) / f
            if should_ignore(file_path):
                continue
            target_file = target_root / f
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target_file)
            count += 1

    print(f"  [+] 复制目录 {src_dir.name}/ -> {count} 个文件")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CC Relay 安装暂存目录组装工具")
    parser.add_argument("--allow-missing-exe", action="store_true", help="允许 cc-relay.exe 缺失 (用于调试资源排布)")
    args = parser.parse_args()

    print("=" * 65)
    print("   CC Relay - 正在组装安装包发布暂存目录 (staging)")
    print("=" * 65)

    if STAGING_DIR.exists():
        print(f"[*] 清理旧的暂存目录: {STAGING_DIR}")
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 核心根目录文件
    root_files = [
        "dist/cc-relay.exe",
        "ui.html",
        "config.example.json",
        "stop_relay.bat",
        "start_relay.bat",
        "THIRD_PARTY_NOTICES.md",
    ]
    print("\n[*] 复制主程序与控制台资源...")
    for rf in root_files:
        p = REPO_ROOT / rf
        if p.is_file():
            dst = STAGING_DIR / p.name
            copy_file(p, dst)
        else:
            if rf == "dist/cc-relay.exe":
                if args.allow_missing_exe:
                    print(f"  [!] 调试模式: 暂时跳过未编译的 {p}")
                else:
                    print(f"[-] 严重错误: {p} 不存在！请先执行 PyInstaller 编译！")
                    return 1

    # 1.1 静态固化版本与 Git 信息至 build_info.json (让无 .git 的安装版依然能展示 short_sha)
    print("\n[*] 固化版本与 Commit 信息至 build_info.json ...")
    import json
    import subprocess
    from datetime import datetime, timezone

    commit_sha = ""
    short_sha = ""
    branch_name = "master"
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            commit_sha = r.stdout.strip()
            short_sha = commit_sha[:7]
        r_br = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5)
        if r_br.returncode == 0 and r_br.stdout.strip():
            branch_name = r_br.stdout.strip()
    except Exception:
        pass

    build_info = {
        "version": "2.4.10",
        "commit": commit_sha,
        "short_sha": short_sha,
        "branch": branch_name,
        "build_time": datetime.now(timezone.utc).isoformat(),
    }
    with open(STAGING_DIR / "build_info.json", "w", encoding="utf-8") as f:
        json.dump(build_info, f, indent=2, ensure_ascii=False)
    print(f"  [+] build_info.json -> commit={short_sha or 'unknown'}, branch={branch_name}")

    # 2. 复制 codex-proxy 目录 (排除日志、更新暂存与本地私有配置)
    print("\n[*] 复制 Codex (CPA) 组件资源...")
    codex_src = REPO_ROOT / "codex-proxy"
    codex_dst = STAGING_DIR / "codex-proxy"
    codex_ignores = [".staging", "*.log", "*.bak", "__pycache__", ".proxy_key", "config.yaml"]
    copy_tree_filtered(codex_src, codex_dst, codex_ignores)

    # 3. 复制 wrapper 目录
    print("\n[*] 复制 Claude CLI Wrapper 资源...")
    wrapper_src = REPO_ROOT / "wrapper"
    wrapper_dst = STAGING_DIR / "wrapper"
    copy_tree_filtered(wrapper_src, wrapper_dst, ["__pycache__"])

    # 4. 复制 tools/voice_input 完整目录 (排除源码开发环境的 .venv 与缓存)
    print("\n[*] 复制 语音伴侣与 ASR 运行环境 (含 runtime 与 models)...")
    voice_src = REPO_ROOT / "tools" / "voice_input"
    voice_dst = STAGING_DIR / "tools" / "voice_input"
    voice_ignores = [".venv", ".cache", "__pycache__", "*.pyc", "*.part", "Qwen*"]
    copy_tree_filtered(voice_src, voice_dst, voice_ignores)

    # 校验关键文件是否存在
    required_checks = [
        (STAGING_DIR / "cc-relay.exe", not args.allow_missing_exe),
        (STAGING_DIR / "ui.html", True),
        (STAGING_DIR / "config.example.json", True),
        (STAGING_DIR / "codex-proxy" / "cli-proxy-api.exe", True),
        (STAGING_DIR / "tools" / "voice_input" / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17" / "model.int8.onnx", True),
        (STAGING_DIR / "tools" / "voice_input" / "runtime" / "python.exe", True),
    ]

    print("\n[*] 正在校验 staging 暂存文件完整性...")
    missing = False
    for req, is_mandatory in required_checks:
        if req.is_file():
            size_mb = req.stat().st_size / (1024 * 1024)
            print(f"  [OK] {req.name} ({size_mb:.1f} MB)")
        else:
            if is_mandatory:
                print(f"  [FAIL] 缺失关键依赖文件: {req}")
                missing = True
            else:
                print(f"  [SKIP] 暂未就绪 (将在流水线编译后注入): {req.name}")

    if missing:
        print("\n[-] 暂存目录文件不完整！请检查上方缺失项。")
        return 1

    print("\n" + "=" * 65)
    print("[SUCCESS] staging 发布目录组装成功，准备调用 Inno Setup 进行封装！")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
