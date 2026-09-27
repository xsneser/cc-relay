#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe, cached update discovery and staging for CLIProxyAPI."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile

REPO = "router-for-me/CLIProxyAPI"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "cc-relay-cpa-updater/1.0"
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_EXE_BYTES = 150 * 1024 * 1024
VERSION_RE = re.compile(r"CLIProxyAPI Version:\s*v?([0-9]+(?:\.[0-9]+)+)")
TAG_RE = re.compile(r"^v?([0-9]+(?:\.[0-9]+)+)$")


def parse_version_output(output):
    match = VERSION_RE.search(output or "")
    return match.group(1) if match else None


def version_tuple(value):
    match = TAG_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _proxy_url(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"^\s*proxy-url:\s*['\"]?([^\s'\"]+)", line)
                if match:
                    value = match.group(1).strip()
                    return "" if value.lower() in ("direct", "none") else value
    except OSError:
        pass
    return ""


def _openers(proxy):
    if not proxy:
        return [urllib.request.build_opener()]
    proxied = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return [proxied, direct]


class CPAUpdater:
    """Owns cached release metadata and safely stages a verified CPA executable."""
    def __init__(self, base, exe_path, config_path, interval=4 * 3600, cache_ttl=2 * 3600):
        self.base = os.path.abspath(base)
        self.exe_path = os.path.abspath(exe_path)
        self.config_path = os.path.abspath(config_path)
        self.codex_dir = os.path.dirname(self.exe_path)
        self.staging_dir = os.path.join(self.codex_dir, ".staging")
        self.interval = interval
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._snapshot = {
            "current_version": None, "latest_version": None, "has_update": False,
            "last_checked": None, "check_error": None, "checking": False,
            "update_state": "idle", "update_error": None, "update_message": None,
            "etag": None, "retry_after": 0.0,
        }
        self._version_fingerprint = None
        self._thread = None

    def _local_version(self, path=None):
        path = os.path.abspath(path or self.exe_path)
        try:
            st = os.stat(path)
            fingerprint = (path, st.st_size, st.st_mtime_ns)
        except OSError:
            fingerprint = (path, None, None)
        with self._lock:
            if fingerprint == self._version_fingerprint and path == self.exe_path:
                return self._snapshot["current_version"]
        if fingerprint[1] is None:
            version = None
        else:
            try:
                result = subprocess.run([path, "-v"], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=8,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                version = parse_version_output((result.stdout or "") + "\n" + (result.stderr or ""))
            except (OSError, subprocess.SubprocessError):
                version = None
        if path == self.exe_path:
            with self._lock:
                self._version_fingerprint = fingerprint
                self._snapshot["current_version"] = version
                self._recompute_locked()
        return version

    def _recompute_locked(self):
        current = version_tuple(self._snapshot.get("current_version"))
        latest = version_tuple(self._snapshot.get("latest_version"))
        self._snapshot["has_update"] = bool(current and latest and latest > current)

    def status(self):
        with self._lock:
            return {k: v for k, v in self._snapshot.items() if k != "etag"}

    def _request(self, url, headers, timeout):
        # All update metadata and assets must stay on official GitHub hosts.
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host not in ("api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"):
            raise ValueError("refusing non-GitHub update URL")
        req = urllib.request.Request(url, headers=headers)
        last_error = None
        proxy = _proxy_url(self.config_path)
        for opener in _openers(proxy):
            try:
                return opener.open(req, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
        raise last_error or RuntimeError("update request failed")

    def check(self, force=False):
        if not self._check_lock.acquire(blocking=False):
            return self.status()
        try:
            self._local_version()
            with self._lock:
                self._snapshot["checking"] = True
                last = self._snapshot.get("last_checked") or 0
                etag = self._snapshot.get("etag")
                retry_after = self._snapshot.get("retry_after") or 0
            if time.time() < retry_after or (not force and time.time() - last < self.cache_ttl):
                return self.status()
            headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
            if etag:
                headers["If-None-Match"] = etag
            try:
                try:
                    response = self._request(RELEASES_URL, headers, 15)
                except urllib.error.HTTPError as exc:
                    if exc.code == 304:
                        with self._lock:
                            self._snapshot.update({"last_checked": time.time(), "check_error": None})
                        return self.status()
                    raise
                with response:
                    payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
                    tag = payload.get("tag_name")
                    if not version_tuple(tag):
                        raise ValueError("GitHub latest release has an invalid version tag")
                    asset = next((a for a in payload.get("assets", [])
                                  if str(a.get("name", "")).lower().endswith("windows_amd64.zip")), None)
                    if not asset or not str(asset.get("browser_download_url", "")).startswith("https://github.com/"):
                        raise ValueError("Windows amd64 release asset was not found")
                    with self._lock:
                        self._snapshot.update({
                            "latest_version": tag.lstrip("v"), "release_url": asset["browser_download_url"],
                            "asset_name": asset.get("name"), "asset_digest": asset.get("digest"),
                            "release_notes": str(payload.get("body") or "")[:4000],
                            "etag": response.headers.get("ETag"), "last_checked": time.time(),
                            "check_error": None,
                        })
                        self._recompute_locked()
            except Exception as exc:
                retry_until = 0.0
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
                    try:
                        retry_until = time.time() + max(60, min(int(retry), 24 * 3600)) if retry else 0.0
                    except (TypeError, ValueError):
                        retry_until = 0.0
                    try:
                        retry_until = max(retry_until, float(reset or 0))
                    except (TypeError, ValueError):
                        pass
                    if not retry_until:
                        retry_until = time.time() + 3600
                with self._lock:
                    self._snapshot["check_error"] = str(exc)[:300]
                    if retry_until:
                        self._snapshot["retry_after"] = retry_until
                        self._snapshot["last_checked"] = time.time()
            finally:
                with self._lock:
                    self._snapshot["checking"] = False
            return self.status()
        finally:
            with self._lock:
                self._snapshot["checking"] = False
            self._check_lock.release()

    def start(self, auto_update=None, installer=None):
        if self._thread and self._thread.is_alive():
            return
        def worker():
            while True:
                try:
                    self.check(force=False)
                    state = self.status()
                    if (auto_update and installer and auto_update() and state.get("has_update")
                            and not state.get("check_error")):
                        self.request_update(installer, automatic=True)
                except Exception:
                    pass
                time.sleep(self.interval)
        self._thread = threading.Thread(target=worker, name="cpa-update-check", daemon=True)
        self._thread.start()

    def request_check(self):
        with self._lock:
            if self._snapshot["checking"]:
                return self.status()
            self._snapshot["checking"] = True
        def run():
            with self._lock:
                self._snapshot["checking"] = False
                self._snapshot["last_checked"] = 0
            self.check(force=True)
        threading.Thread(target=run, name="cpa-manual-check", daemon=True).start()
        return self.status()

    def stage_latest(self):
        if not self._update_lock.acquire(blocking=False):
            raise RuntimeError("update already in progress")
        try:
            with self._lock:
                url = self._snapshot.get("release_url")
                latest = self._snapshot.get("latest_version")
                digest = self._snapshot.get("asset_digest") or ""
            if not url or not latest:
                raise RuntimeError("check for updates first")
            os.makedirs(self.staging_dir, exist_ok=True)
            archive_path = os.path.join(self.staging_dir, "cpa-update.zip")
            staged_path = os.path.join(self.staging_dir, "cli-proxy-api.exe.staged")
            with self._lock:
                self._snapshot.update({"update_state": "downloading", "update_error": None, "update_message": None})
            response = self._request(url, {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}, 60)
            total = 0
            with response, open(archive_path, "wb") as out:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("CPA update archive exceeds size limit")
                    out.write(block)
            if digest:
                import hashlib
                alg, _, expected = digest.partition(":")
                if alg.lower() != "sha256" or hashlib.sha256(open(archive_path, "rb").read()).hexdigest() != expected.lower():
                    raise ValueError("release archive SHA-256 verification failed")
            with zipfile.ZipFile(archive_path) as archive:
                names = [i for i in archive.infolist() if i.filename.replace("\\", "/") == "cli-proxy-api.exe"]
                if len(names) != 1 or names[0].file_size <= 0 or names[0].file_size > MAX_EXE_BYTES:
                    raise ValueError("release archive has invalid executable entry")
                mode = (names[0].external_attr >> 16) & 0o170000
                if mode == 0o120000 or names[0].is_dir():
                    raise ValueError("release executable entry must be a regular file")
                with archive.open(names[0]) as src, open(staged_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            found = self._local_version(staged_path)
            current = self._local_version()
            if found != latest.lstrip("v"):
                raise ValueError(f"staged executable version mismatch (expected {latest}, got {found or 'unknown'})")
            if not current or version_tuple(found) <= version_tuple(current):
                raise ValueError("staged version is not newer than the installed CPA version")
            with self._lock:
                self._snapshot["update_state"] = "staged"
            return staged_path
        except Exception as exc:
            with self._lock:
                self._snapshot.update({"update_state": "failed", "update_error": str(exc)[:400]})
            raise
        finally:
            self._update_lock.release()

    def set_update_state(self, state, message=None, error=None):
        with self._lock:
            self._snapshot.update({"update_state": state, "update_message": message, "update_error": error})
            if state == "complete":
                self._version_fingerprint = None
                self._snapshot["current_version"] = None
        if state == "complete":
            self._local_version()

    def request_update(self, installer, automatic=False):
        with self._lock:
            if self._snapshot.get("update_state") in ("queued", "downloading", "staged", "installing", "rolling_back"):
                return self.status()
            if not self._snapshot.get("has_update"):
                raise RuntimeError("no CPA update is available")
            self._snapshot.update({"update_state": "queued", "update_error": None, "update_message": None})
        def run():
            try:
                staged = self.stage_latest()
                with self._lock:
                    self._snapshot["update_state"] = "installing"
                installer(staged, automatic)
                self.set_update_state("complete", "CPA updated successfully")
            except Exception as exc:
                self.set_update_state("failed", error=str(exc)[:400])
        threading.Thread(target=run, name="cpa-update-install", daemon=True).start()
        return self.status()
