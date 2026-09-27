#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI front end for cc-relay's protected CPA update API.

Installation is delegated to the live relay so its in-flight request gate,
maintenance lock, staging verification, and rollback policy are shared.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import cc_relay


def api_request(port, path, payload=None, timeout=10):
    origin = f"http://127.0.0.1:{port}"
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        origin + path,
        data=body,
        headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": origin,
            "X-CC-Relay-UI": "1",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Check or update CLIProxyAPI via the running cc-relay")
    parser.add_argument("--check", action="store_true", help="check for updates without installing")
    args = parser.parse_args()
    conf = cc_relay.load_conf()
    port = int(conf.get("ui_port", 8610))

    try:
        state = api_request(port, "/api/cpa/check", {}) if args.check else api_request(port, "/api/cpa/version")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Cannot reach the local cc-relay update service on port {port}: {exc}")
        print("Start cc-relay and use this script again; standalone install is disabled to protect active requests.")
        return 2

    if args.check:
        deadline = time.time() + 25
        while state.get("checking") and time.time() < deadline:
            time.sleep(0.5)
            state = api_request(port, "/api/cpa/version")
    print("Installed:", state.get("current_version") or "unknown")
    print("Latest:   ", state.get("latest_version") or "not checked")
    if state.get("check_error"):
        print("Check failed:", state["check_error"])
        return 1
    if args.check:
        print("Update available:", bool(state.get("has_update")))
        return 0
    if not state.get("has_update"):
        print("CLIProxyAPI is already up to date, or no successful update check is cached.")
        print("Run with --check to query GitHub.")
        return 0

    try:
        api_request(port, "/api/cpa/update", {})
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print("Could not queue update:", exc)
        return 1
    print("Update queued through cc-relay; active Codex requests will drain before restart.")
    while True:
        state = api_request(port, "/api/cpa/version")
        phase = state.get("update_state")
        print("Update state:", phase, state.get("update_message") or state.get("update_error") or "")
        if phase == "complete":
            return 0
        if phase == "failed":
            return 1
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
