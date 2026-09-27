#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download the official Antigravity Tools Windows x64 release.

This updater deliberately keeps release discovery and installation separate from
cc-relay's own source updater.  ``check`` is read-only; ``download`` stages an
installer without touching the current installation; ``install`` launches the
staged official installer after an explicit confirmation.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

REPO = "lbjlaq/Antigravity-Manager"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
GITHUB_DOWNLOAD_PREFIX = "https://github.com/"
DEFAULT_EXE = os.path.expandvars(
    r"%LOCALAPPDATA%\Antigravity Tools\antigravity-tools.exe"
)
USER_AGENT = "cc-relay-antigravity-updater/1.0"


def _opener(proxy=""):
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    return build_opener(*handlers)


def _get_json(url, proxy="", timeout=30):
    req = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with _opener(proxy).open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_release(proxy=""):
    release = _get_json(API_URL, proxy)
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("latest GitHub release is not a stable release")
    if not release.get("tag_name") or not isinstance(release.get("assets"), list):
        raise RuntimeError("GitHub release metadata is incomplete")
    return release


def select_windows_asset(release):
    candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        lower = name.lower()
        if not name or lower.endswith(".sig") or lower.endswith(".sha256"):
            continue
        if "x64-setup.exe" in lower or "x86_64-setup.exe" in lower:
            candidates.append(asset)
    if len(candidates) != 1:
        names = ", ".join(str(a.get("name")) for a in candidates) or "none"
        raise RuntimeError("expected exactly one Windows x64 setup asset; found: " + names)
    return candidates[0]


def _download_url(url, base):
    if not base:
        return url
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("download base must be an absolute http(s) URL")
    if not url.startswith(GITHUB_DOWNLOAD_PREFIX):
        raise ValueError("refusing to rewrite a non-GitHub download URL")
    return base.rstrip("/") + "/" + url[len(GITHUB_DOWNLOAD_PREFIX):]


def download(url, destination, proxy="", retries=3, expected_digest=""):
    expected = str(expected_digest or "").strip().lower()
    if expected.startswith("sha256:"):
        expected = expected.split(":", 1)[1]
    if expected and (len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected)):
        raise ValueError("release digest is not a valid SHA-256 value")
    last_error = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
            digest = hashlib.sha256()
            size = 0
            with _opener(proxy).open(req, timeout=60) as response, open(destination, "wb") as out:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    size += len(block)
            if size == 0:
                raise RuntimeError("downloaded file is empty")
            actual = digest.hexdigest()
            if expected and actual != expected:
                raise RuntimeError("SHA-256 mismatch: expected %s, got %s" % (expected, actual))
            return size, actual
        except (OSError, HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            try:
                os.unlink(destination)
            except OSError:
                pass
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("download failed: %s" % last_error)


def _authenticode_status(path):
    if os.name != "nt":
        return "unsupported"
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return "unavailable"
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath %s; "
        "Write-Output $s.Status" % _ps_quote(path)
    )
    try:
        result = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip() or "Unknown"
    except Exception:
        return "unavailable"


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _print_release(release, asset, download_url):
    print("Release: %s (%s)" % (release.get("tag_name"), release.get("published_at", "unknown date")))
    print("Asset:   %s (%s bytes)" % (asset.get("name"), asset.get("size", "unknown")))
    print("Source:  %s" % download_url)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Update Antigravity Tools from the official GitHub release")
    parser.add_argument("command", choices=("check", "download", "install"), nargs="?", default="check")
    parser.add_argument("--proxy", default="", help="HTTP(S) proxy for GitHub metadata and downloads")
    parser.add_argument("--download-base", default="", help="HTTPS mirror prefix for GitHub asset downloads")
    parser.add_argument("--output", default="", help="staged installer path (download/install)")
    parser.add_argument("--target", default=DEFAULT_EXE, help="current Antigravity executable, for display only")
    parser.add_argument("--yes", action="store_true", help="launch the installer without an interactive prompt")
    args = parser.parse_args(argv)

    try:
        release = latest_release(args.proxy)
        asset = select_windows_asset(release)
        url = _download_url(str(asset.get("browser_download_url") or ""), args.download_base)
        _print_release(release, asset, url)
        if args.command == "check":
            print("Target:  %s" % args.target)
            print("Action:  no files changed")
            return 0

        output = args.output or os.path.join(tempfile.gettempdir(), str(asset["name"]))
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        expected_digest = str(asset.get("digest") or "").strip()
        if not expected_digest:
            raise RuntimeError("release does not provide an official asset digest; refusing to install")
        size, digest = download(url, output, args.proxy, expected_digest=expected_digest)
        print("Staged:  %s (%d bytes)" % (output, size))
        print("SHA256:  %s (verified against GitHub asset digest)" % digest)
        print("Signer:  %s" % _authenticode_status(output))
        if args.command == "download":
            print("The current installation was not changed.")
            return 0

        if not args.yes:
            answer = input("Launch the official installer now? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Installer staged; current installation was not changed.")
                return 0
        subprocess.Popen([output], cwd=os.path.dirname(output))
        print("Installer launched. Antigravity Tools will update through its official installer.")
        return 0
    except (OSError, ValueError, RuntimeError, HTTPError, URLError) as exc:
        print("Update failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
