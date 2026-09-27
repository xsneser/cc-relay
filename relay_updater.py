#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe, cached update discovery and fast-forward updating for cc-relay.

Checks the official repository (xsneser/cc-relay) master branch on GitHub.
Supports both GitHub API (with ETag & proxy) and native Git fallbacks.
Safeguards local modifications and ensures syntax integrity before restart.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_REPO = "https://github.com/xsneser/cc-relay.git"
DEFAULT_BRANCH = "master"
GITHUB_API_COMMITS_URL = "https://api.github.com/repos/xsneser/cc-relay/commits/master"
USER_AGENT = "cc-relay-updater/1.0"

# Paths that are ignored during dirty check if untracked/modified
IGNORED_DIRTY_PATHS = {
    "prompts.json",
    "sentinel.log",
    "serve.log",
    "_diag.log",
    ".update-status.json",
    ".relay-update-state.json",
    "relay-status.json",
    "config.json",
}


def _run_git(args, cwd, timeout=30):
    """Run git command safely with UTF-8 decoding and timeout."""
    try:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        class _DummyResult:
            returncode = -1
            stdout = ""
            stderr = str(exc)
        return _DummyResult()


def _is_git_repo(base_dir):
    return os.path.isdir(os.path.join(base_dir, ".git"))


def _proxy_url_from_config(config_path):
    if not config_path or not os.path.exists(config_path):
        return ""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            # Check tools.relay_update.proxy_url first, then top-level proxy_url
            tu = (cfg.get("tools") or {}).get("relay_update") or {}
            pu = tu.get("proxy_url") or cfg.get("proxy_url") or ""
            return "" if pu.lower() in ("direct", "none") else pu
    except Exception:
        return ""


def _openers(proxy=""):
    if not proxy:
        return [urllib.request.build_opener()]
    proxied = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return [proxied, direct]


class RelayUpdater:
    """Discovers upstream updates for cc-relay and executes safe fast-forward updates."""

    def __init__(self, base_dir, config_path=None, interval=14400, cache_ttl=7200):
        self.base_dir = os.path.abspath(base_dir)
        self.config_path = os.path.abspath(config_path) if config_path else os.path.join(self.base_dir, "config.json")
        self.interval = interval
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._update_lock = threading.Lock()

        # Capture running commit at startup
        running_info = self._get_local_commit()
        self._running_sha = running_info.get("sha")
        self._running_short = running_info.get("short_sha")

        self._snapshot = {
            "is_git": _is_git_repo(self.base_dir),
            "branch": DEFAULT_BRANCH,
            "local_branch": running_info.get("branch", "unknown"),
            "current_version": self._running_short or "unknown",
            "current_sha": self._running_sha or "",
            "latest_version": self._running_short or "unknown",
            "latest_sha": self._running_sha or "",
            "latest_subject": "",
            "latest_date": "",
            "behind_count": 0,
            "has_update": False,
            "can_update": True,
            "blocked_reason": None,
            "last_checked": None,
            "check_error": None,
            "checking": False,
            "update_state": "idle",  # idle, checking, queued, fetching, applying, success, error
            "update_error": None,
            "update_message": None,
            "etag": None,
            "retry_after": 0.0,
        }
        self._thread = None
        self._stop_event = threading.Event()

    def _get_local_commit(self):
        """Query local git repository for current HEAD info."""
        if not _is_git_repo(self.base_dir):
            return {"sha": None, "short_sha": None, "branch": "non-git"}
        res = _run_git(["rev-parse", "HEAD"], self.base_dir)
        sha = res.stdout.strip() if res.returncode == 0 else None
        res_br = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.base_dir)
        branch = res_br.stdout.strip() if res_br.returncode == 0 else "unknown"
        short = sha[:7] if sha else None
        return {"sha": sha, "short_sha": short, "branch": branch}

    def _check_dirty(self):
        """Check whether there are untracked or modified files that prevent clean updates."""
        if not _is_git_repo(self.base_dir):
            return True, "非 Git 仓库，无法直接更新"
        res = _run_git(["status", "--porcelain", "--untracked-files=all"], self.base_dir)
        if res.returncode != 0:
            return True, "无法检查 Git 状态: " + res.stderr.strip()
        unsafe = []
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            status = line[:2]
            path = line[3:].strip()
            # Allow ignored runtime files
            if status == "??" and (path in IGNORED_DIRTY_PATHS or path.startswith("records")):
                continue
            if path in IGNORED_DIRTY_PATHS:
                continue
            unsafe.append(line_str)
        if unsafe:
            return True, f"本地存在未提交的修改 ({len(unsafe)} 项)，请先提交或备份"
        return False, ""

    def status(self):
        """Return a defensive copy of the current status snapshot."""
        with self._lock:
            snap = dict(self._snapshot)
            snap.pop("etag", None)
            return snap

    def _request_github_api(self, etag=None):
        proxy = _proxy_url_from_config(self.config_path)
        token = os.environ.get("CC_RELAY_GITHUB_TOKEN") or ""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if etag:
            headers["If-None-Match"] = etag

        req = urllib.request.Request(GITHUB_API_COMMITS_URL, headers=headers)
        last_error = None
        for opener in _openers(proxy):
            try:
                resp = opener.open(req, timeout=15)
                new_etag = resp.headers.get("ETag")
                raw = resp.read(512 * 1024)
                data = json.loads(raw.decode("utf-8"))
                return 200, data, new_etag
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return 304, None, etag
                last_error = exc
                if exc.code in (403, 429):
                    # Rate limit or forbidden
                    break
            except Exception as exc:
                last_error = exc
        raise last_error or RuntimeError("GitHub API request failed")

    def _check_via_git_ls_remote(self):
        """Fallback check using git ls-remote when GitHub API is blocked or rate-limited."""
        res = _run_git(["ls-remote", DEFAULT_REPO, f"refs/heads/{DEFAULT_BRANCH}"], self.base_dir, timeout=25)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split()
            if parts:
                sha = parts[0]
                return sha, f"commit {sha[:7]}"
        raise RuntimeError("git ls-remote failed: " + res.stderr.strip()[:200])

    def check(self, force=False):
        """Perform update check against GitHub master branch."""
        if not self._check_lock.acquire(blocking=False):
            return self.status()
        try:
            with self._lock:
                self._snapshot["checking"] = True
                last = self._snapshot.get("last_checked") or 0
                etag = self._snapshot.get("etag")
                retry_after = self._snapshot.get("retry_after") or 0
                current_sha = self._snapshot.get("current_sha")

            now = time.time()
            if now < retry_after or (not force and now - last < self.cache_ttl):
                with self._lock:
                    self._snapshot["checking"] = False
                return self.status()

            remote_sha = None
            subject = ""
            date_str = ""
            new_etag = etag
            check_err = None

            # Try GitHub API first
            try:
                code, data, resp_etag = self._request_github_api(etag if not force else None)
                if code == 304:
                    with self._lock:
                        self._snapshot.update({
                            "last_checked": now,
                            "check_error": None,
                            "checking": False,
                        })
                    return self.status()
                elif code == 200 and isinstance(data, dict):
                    remote_sha = data.get("sha")
                    commit_obj = data.get("commit") or {}
                    subject = (commit_obj.get("message") or "").splitlines()[0][:100]
                    date_str = (commit_obj.get("committer") or {}).get("date") or ""
                    new_etag = resp_etag
            except Exception as exc:
                check_err = str(exc)
                # Fallback to git ls-remote
                try:
                    remote_sha, subject = self._check_via_git_ls_remote()
                    check_err = None
                except Exception as git_exc:
                    check_err = f"API: {check_err}; Git: {git_exc}"

            with self._lock:
                self._snapshot["checking"] = False
                self._snapshot["last_checked"] = now
                if check_err:
                    self._snapshot["check_error"] = check_err
                    return self.status()

                self._snapshot["check_error"] = None
                self._snapshot["etag"] = new_etag

                if remote_sha:
                    self._snapshot["latest_sha"] = remote_sha
                    self._snapshot["latest_version"] = remote_sha[:7]
                    self._snapshot["latest_subject"] = subject
                    self._snapshot["latest_date"] = date_str

                    # Check relationship between local HEAD and remote commit
                    local_info = self._get_local_commit()
                    loc_sha = local_info.get("sha")
                    loc_branch = local_info.get("branch")
                    self._snapshot["current_sha"] = loc_sha or ""
                    self._snapshot["current_version"] = loc_sha[:7] if loc_sha else "unknown"
                    self._snapshot["local_branch"] = loc_branch

                    if not loc_sha or loc_sha == remote_sha:
                        self._snapshot["has_update"] = False
                        self._snapshot["behind_count"] = 0
                        self._snapshot["can_update"] = True
                        self._snapshot["blocked_reason"] = None
                    else:
                        # Test if remote_sha is ancestor of local (local ahead / dev branch)
                        is_anc = _run_git(["merge-base", "--is-ancestor", remote_sha, loc_sha], self.base_dir).returncode == 0
                        if is_anc:
                            # Local already contains remote_sha
                            self._snapshot["has_update"] = False
                            self._snapshot["behind_count"] = 0
                            self._snapshot["can_update"] = True
                            self._snapshot["blocked_reason"] = None
                        else:
                            # Remote has commits not in local HEAD
                            self._snapshot["has_update"] = True
                            behind_res = _run_git(["rev-list", "--count", f"HEAD..{remote_sha}"], self.base_dir)
                            if behind_res.returncode == 0:
                                try:
                                    self._snapshot["behind_count"] = int(behind_res.stdout.strip())
                                except ValueError:
                                    self._snapshot["behind_count"] = 1
                            else:
                                self._snapshot["behind_count"] = 1

                            # Check if local is allowed to auto-update
                            dirty, dirty_msg = self._check_dirty()
                            if dirty:
                                self._snapshot["can_update"] = False
                                self._snapshot["blocked_reason"] = dirty_msg
                            elif loc_branch != DEFAULT_BRANCH:
                                self._snapshot["can_update"] = False
                                self._snapshot["blocked_reason"] = f"当前处于分支 '{loc_branch}'，自动更新仅支持 '{DEFAULT_BRANCH}' 分支"
                            else:
                                self._snapshot["can_update"] = True
                                self._snapshot["blocked_reason"] = None

            return self.status()
        finally:
            self._check_lock.release()

    def request_check(self, force=True):
        """Asynchronously trigger an update check and return current snapshot."""
        threading.Thread(target=self.check, args=(force,), daemon=True).start()
        return self.status()

    def _compile_check(self):
        """Validate python syntax for key entry files before completing update."""
        files = ["cc_relay.py", "lifecycle.py", "relay_updater.py"]
        py_files = [os.path.join(self.base_dir, f) for f in files if os.path.exists(os.path.join(self.base_dir, f))]
        try:
            r = subprocess.run(
                [sys.executable, "-m", "py_compile"] + py_files,
                cwd=self.base_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                return True, ""
            return False, (r.stderr or r.stdout).strip()[:400]
        except Exception as exc:
            return False, str(exc)

    def apply_update(self, restart_callback=None):
        """Safely fetch and fast-forward merge master branch, compile-check, and trigger restart."""
        if not self._update_lock.acquire(blocking=False):
            return False, "已有更新任务正在运行中"
        try:
            with self._lock:
                self._snapshot["update_state"] = "queued"
                self._snapshot["update_error"] = None
                self._snapshot["update_message"] = "准备更新..."

            # 1. Preflight check
            dirty, dirty_msg = self._check_dirty()
            if dirty:
                with self._lock:
                    self._snapshot["update_state"] = "error"
                    self._snapshot["update_error"] = dirty_msg
                return False, dirty_msg

            local_info = self._get_local_commit()
            old_sha = local_info.get("sha")
            if local_info.get("branch") != DEFAULT_BRANCH:
                err = f"当前处于分支 '{local_info.get('branch')}'，无法自动合并到 master"
                with self._lock:
                    self._snapshot["update_state"] = "error"
                    self._snapshot["update_error"] = err
                return False, err

            # 2. Fetch master
            with self._lock:
                self._snapshot["update_state"] = "fetching"
                self._snapshot["update_message"] = "正在从 GitHub 获取更新..."

            fetch_res = _run_git(["fetch", "origin", DEFAULT_BRANCH], self.base_dir, timeout=60)
            if fetch_res.returncode != 0:
                err = "Git fetch 失败: " + fetch_res.stderr.strip()[:200]
                with self._lock:
                    self._snapshot["update_state"] = "error"
                    self._snapshot["update_error"] = err
                return False, err

            # 3. Fast-forward merge
            with self._lock:
                self._snapshot["update_state"] = "applying"
                self._snapshot["update_message"] = "正在应用更新文件..."

            merge_res = _run_git(["merge", "--ff-only", f"origin/{DEFAULT_BRANCH}"], self.base_dir, timeout=30)
            if merge_res.returncode != 0:
                err = "Fast-forward 合并失败: " + merge_res.stderr.strip()[:200]
                with self._lock:
                    self._snapshot["update_state"] = "error"
                    self._snapshot["update_error"] = err
                return False, err

            # 4. Compile check
            ok, compile_err = self._compile_check()
            if not ok:
                # Rollback on syntax error
                _run_git(["reset", "--hard", old_sha], self.base_dir, timeout=30)
                err = f"更新后代码编译失败，已安全回滚: {compile_err}"
                with self._lock:
                    self._snapshot["update_state"] = "error"
                    self._snapshot["update_error"] = err
                return False, err

            # 5. Success - trigger restart
            new_info = self._get_local_commit()
            with self._lock:
                self._snapshot["update_state"] = "success"
                self._snapshot["has_update"] = False
                self._snapshot["current_sha"] = new_info.get("sha") or ""
                self._snapshot["current_version"] = (new_info.get("sha") or "")[:7]
                self._snapshot["update_message"] = "更新已完成，正在重启中转服务..."

            if restart_callback:
                threading.Thread(target=restart_callback, daemon=True).start()

            return True, "更新成功并正在重启"
        finally:
            self._update_lock.release()

    def request_update(self, restart_callback=None):
        """Asynchronously start update job."""
        threading.Thread(target=self.apply_update, args=(restart_callback,), daemon=True).start()
        with self._lock:
            self._snapshot["update_state"] = "queued"
            self._snapshot["update_message"] = "已提交更新任务..."
        return self.status()

    def start(self, auto_check=True):
        """Start periodic background check."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()

            def _worker():
                # Initial check shortly after startup
                time.sleep(3)
                if auto_check and not self._stop_event.is_set():
                    try:
                        self.check(force=False)
                    except Exception:
                        pass
                while not self._stop_event.wait(self.interval):
                    if auto_check:
                        try:
                            self.check(force=False)
                        except Exception:
                            pass

            self._thread = threading.Thread(target=_worker, daemon=True, name="relay-updater")
            self._thread.start()

    def stop(self):
        """Stop background check worker."""
        self._stop_event.set()


# Module-level singleton
_RELAY_UPDATER = None
_RELAY_UPDATER_LOCK = threading.Lock()


def get_relay_updater(conf=None):
    global _RELAY_UPDATER
    with _RELAY_UPDATER_LOCK:
        if _RELAY_UPDATER is None:
            base = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(base, "config.json")
            tu = ((conf or {}).get("tools") or {}).get("relay_update") or {}
            interval = int(tu.get("check_interval_seconds") or 14400)
            _RELAY_UPDATER = RelayUpdater(base, config_path=config_path, interval=interval)
        return _RELAY_UPDATER
