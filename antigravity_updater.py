#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe, cached update discovery for Antigravity Tools (antitools).

Antigravity Tools is an upstream desktop tool (Tauri + WebView2) running on
127.0.0.1:8045.  This module checks for updates from the official GitHub
releases (lbjlaq/Antigravity-Manager) and compares against the locally running
version reported by GET http://127.0.0.1:8045/health.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

REPO = "lbjlaq/Antigravity-Manager"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DEFAULT_UPSTREAM_URL = "http://127.0.0.1:8045"
USER_AGENT = "cc-relay-antigravity-updater/1.0"
VERSION_RE = re.compile(r"^v?([0-9]+(?:\.[0-9]+)+)")
DEFAULT_EXE = os.path.expandvars(
    r"%LOCALAPPDATA%\Antigravity Tools\antigravity-tools.exe"
)

_UPDATER_INSTANCE = None
_UPDATER_LOCK = threading.Lock()


def parse_version_tuple(value):
    """Convert version string like '4.8.4' or 'v4.8.4' to integer tuple (4, 8, 4)."""
    if not value:
        return None
    match = VERSION_RE.search(str(value).strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def clean_release_notes(body):
    """Extract clean changelog notes from release markdown."""
    if not body:
        return ""
    # Strip HTML comments
    s = re.sub(r"<!--.*?-->", "", str(body), flags=re.DOTALL)
    # Strip Full Changelog link
    s = re.sub(r"\*\*Full\s+Changelog\*\*:[^\n]+", "", s)
    lines = []
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("## Changelog", "## What's Changed", "## Updates")):
            continue
        if line.startswith(("- ", "* ")):
            line = re.sub(r"\s*\([0-9a-f]{7,40}\)", "", line)
            lines.append(line)
        elif not line.startswith("#"):
            lines.append(line)
    return "\n".join(lines).strip()[:2000]


def _proxy_url_from_conf(conf):
    """Extract proxy URL from relay configuration for tools/antigravity or global."""
    if not conf:
        return ""
    tools = conf.get("tools") or {}
    ag_tool = tools.get("antigravity") or {}
    if ag_tool.get("proxy_url"):
        p = ag_tool["proxy_url"].strip()
        return "" if p.lower() in ("direct", "none") else p
    upstreams = conf.get("upstreams") or {}
    ag_up = upstreams.get("antigravity") or {}
    if ag_up.get("proxy_url"):
        p = ag_up["proxy_url"].strip()
        return "" if p.lower() in ("direct", "none") else p
    if conf.get("proxy_url"):
        p = conf["proxy_url"].strip()
        return "" if p.lower() in ("direct", "none") else p
    return ""


def _openers(proxy):
    if not proxy:
        return [urllib.request.build_opener()]
    proxied = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return [proxied, direct]


class AntigravityUpdater:
    """Discovers Antigravity Tools updates via GitHub and checks local version."""

    def __init__(self, upstream_url=DEFAULT_UPSTREAM_URL, exe_path=None, proxy_provider=None,
                 interval=4 * 3600, cache_ttl=2 * 3600):
        self.upstream_url = upstream_url.rstrip("/")
        self.exe_path = exe_path or DEFAULT_EXE
        self.proxy_provider = proxy_provider or (lambda: "")
        self.interval = interval
        self.cache_ttl = cache_ttl

        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._local_probe_lock = threading.Lock()
        self._local_probe_running = False
        self._local_probe_last = 0.0
        self._etag = None
        self._snapshot = {
            "current_version": None,
            "latest_version": None,
            "has_update": False,
            "last_checked": None,
            "check_error": None,
            "checking": False,
            "local_version_loading": False,
            "local_version_error": None,
            "local_version_checked_at": None,
            "release_url": f"https://github.com/{REPO}/releases/latest",
            "release_notes": "",
        }
        self._thread = None
        self._stop_event = threading.Event()

    def _query_local_version(self):
        """Query http://127.0.0.1:8045/health for version."""
        url = f"{self.upstream_url}/health"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        # Health check must stay strictly direct / loopback with short timeout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=2.5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    raw_ver = data.get("version")
                    if raw_ver:
                        m = VERSION_RE.search(str(raw_ver).strip())
                        if m:
                            return m.group(1)
        except Exception:
            pass
        return None

    def _refresh_local_version_background(self):
        try:
            version = self._query_local_version()
            checked_at = time.time()
            with self._lock:
                self._snapshot["local_version_loading"] = False
                self._snapshot["local_version_checked_at"] = checked_at
                if version:
                    self._snapshot["current_version"] = version
                    self._snapshot["local_version_error"] = None
                else:
                    self._snapshot["local_version_error"] = "local health unavailable"
                self._recompute_locked()
        finally:
            with self._local_probe_lock:
                self._local_probe_last = time.monotonic()
                self._local_probe_running = False

    def _schedule_local_version_refresh(self):
        with self._local_probe_lock:
            if self._local_probe_running:
                return
            self._local_probe_running = True
        with self._lock:
            self._snapshot["local_version_loading"] = True
            self._snapshot["local_version_error"] = None
        thread = threading.Thread(
            target=self._refresh_local_version_background,
            name="AntigravityLocalVersion",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                self._snapshot["local_version_loading"] = False
                self._snapshot["local_version_error"] = str(exc)
            with self._local_probe_lock:
                self._local_probe_running = False

    def status(self):
        now = time.monotonic()
        with self._lock:
            self._recompute_locked()
            snapshot = dict(self._snapshot)
            needs_probe = (
                self._snapshot.get("current_version") is None
                or now - self._local_probe_last >= 15.0
            )
        if needs_probe:
            self._schedule_local_version_refresh()
            with self._lock:
                snapshot = dict(self._snapshot)
        return snapshot

    def _recompute_locked(self):
        cur = parse_version_tuple(self._snapshot.get("current_version"))
        lat = parse_version_tuple(self._snapshot.get("latest_version"))
        if cur and lat:
            self._snapshot["has_update"] = lat > cur
        else:
            self._snapshot["has_update"] = False

    def check(self, force=False):
        """Perform update check against GitHub releases."""
        with self._lock:
            now = time.time()
            last_checked = self._snapshot.get("last_checked") or 0
            if not force and (now - last_checked < self.cache_ttl) and self._snapshot.get("latest_version"):
                cur = self._query_local_version()
                if cur:
                    self._snapshot["current_version"] = cur
                self._recompute_locked()
                return dict(self._snapshot)

        if not self._check_lock.acquire(blocking=False):
            with self._lock:
                return dict(self._snapshot)

        try:
            with self._lock:
                self._snapshot["checking"] = True
                self._snapshot["check_error"] = None

            cur_ver = self._query_local_version()
            proxy = self.proxy_provider() if callable(self.proxy_provider) else ""

            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            }
            if not force and self._etag:
                headers["If-None-Match"] = self._etag

            req = urllib.request.Request(RELEASES_URL, headers=headers)
            release_data = None
            new_etag = None
            last_err = None

            for opener in _openers(proxy):
                try:
                    with opener.open(req, timeout=15) as resp:
                        new_etag = resp.headers.get("ETag")
                        body = resp.read().decode("utf-8", errors="replace")
                        release_data = json.loads(body)
                        break
                except urllib.error.HTTPError as he:
                    if he.code == 304:
                        # Cached release is still current
                        with self._lock:
                            if cur_ver:
                                self._snapshot["current_version"] = cur_ver
                            self._snapshot["last_checked"] = time.time()
                            self._snapshot["checking"] = False
                            self._recompute_locked()
                            return dict(self._snapshot)
                    last_err = he
                    break
                except Exception as e:
                    last_err = e
                    continue

            with self._lock:
                if release_data:
                    tag = str(release_data.get("tag_name") or "").strip()
                    m = VERSION_RE.search(tag)
                    latest_ver = m.group(1) if m else tag.lstrip("v")
                    notes = clean_release_notes(release_data.get("body") or "")
                    html_url = release_data.get("html_url") or f"https://github.com/{REPO}/releases/latest"

                    self._etag = new_etag or self._etag
                    if cur_ver:
                        self._snapshot["current_version"] = cur_ver
                    self._snapshot["latest_version"] = latest_ver
                    self._snapshot["release_url"] = html_url
                    self._snapshot["release_notes"] = notes
                    self._snapshot["last_checked"] = time.time()
                    self._snapshot["check_error"] = None
                    self._recompute_locked()
                elif last_err:
                    if cur_ver:
                        self._snapshot["current_version"] = cur_ver
                    self._snapshot["check_error"] = str(last_err)
                self._snapshot["checking"] = False
                return dict(self._snapshot)
        finally:
            with self._lock:
                self._snapshot["checking"] = False
            self._check_lock.release()

    def request_check(self, force=True):
        """Asynchronously trigger update check and return snapshot."""
        t = threading.Thread(target=self.check, args=(force,), daemon=True)
        t.start()
        with self._lock:
            snap = dict(self._snapshot)
            snap["checking"] = True
            return snap

    def start(self, auto_check=True):
        """Start background polling thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, args=(auto_check,), daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run_loop(self, auto_check):
        if auto_check:
            # Short delay at startup so service finishes initializing
            time.sleep(3.0)
            self.check(force=False)
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.interval):
                break
            self.check(force=False)


def get_antigravity_updater(conf=None):
    """Singleton getter for AntigravityUpdater."""
    global _UPDATER_INSTANCE
    with _UPDATER_LOCK:
        if _UPDATER_INSTANCE is None:
            c = conf or {}
            upstreams = c.get("upstreams") or {}
            ag_up = upstreams.get("antigravity") or {}
            base_url = ag_up.get("base") or DEFAULT_UPSTREAM_URL
            exe = (c.get("antigravity_exe") or "").strip() or DEFAULT_EXE
            proxy_getter = lambda: _proxy_url_from_conf(conf)
            _UPDATER_INSTANCE = AntigravityUpdater(
                upstream_url=base_url,
                exe_path=exe,
                proxy_provider=proxy_getter,
            )
        return _UPDATER_INSTANCE
