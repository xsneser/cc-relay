# cc-relay Deployment and Installation Guide

> 🌐 English | [简体中文](INSTALL.md)

This guide walks through a complete `cc-relay` setup from scratch: installation, cross-platform configuration, hooking up the three upstreams, injecting the Claude Code configuration, and troubleshooting common failures.

---

## 📋 Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Getting the Source and Initial Configuration](#2-getting-the-source-and-initial-configuration)
3. [Wiring Up the Three Upstreams](#3-wiring-up-the-three-upstreams)
   - [3.1 DeepSeek Direct](#31-deepseek-direct)
   - [3.2 Codex (CLIProxyAPI) Bridge](#32-codex-cliproxyapi-bridge)
   - [3.3 Gemini (Antigravity Tools) Bridge](#33-gemini-antigravity-tools-bridge)
4. [Client Configuration (Desktop 3P & CLI)](#4-client-configuration)
   - [4.1 Claude Desktop (GUI) 3P Inference Gateway](#41-claude-desktop-gui-3p-inference-gateway-recommended)
   - [4.2 Injecting the Claude Code CLI Configuration](#42-injecting-the-claude-code-cli-configuration)
5. [Seamless Automation on Windows (Wrapper & Lifecycle)](#5-seamless-automation-on-windows-wrapper--lifecycle)
6. [Daemonization on macOS / Linux](#6-daemonization-on-macos--linux)
7. [Daily Operations and Diagnostics](#7-daily-operations-and-diagnostics)
8. [Troubleshooting FAQ](#8-troubleshooting-faq)

---

## 1. Prerequisites

- **Python 3.8 or newer** (must be on your system `PATH`)
  - Check with: `python --version` or `python3 --version`
  - *Note: cc-relay is built purely on the standard library — no pip packages are required.*
- **Claude Code CLI** (installed and working)
  - Check with: `claude --version`
- **Git**
  - Check with: `git --version`

---

## 2. Getting the Source and Initial Configuration

### 2.1 Clone the repository

```bash
git clone https://github.com/xsneser/cc-relay.git
cd cc-relay
```

### 2.2 Create your local config file

```bash
# Windows (CMD / PowerShell)
copy config.example.json config.json

# macOS / Linux
cp config.example.json config.json
```

> ⚠️ **Security warning**: `config.json` holds your real upstream API keys. Never upload or commit it to a public repository. The bundled `.gitignore` already excludes it.

---

## 3. Wiring Up the Three Upstreams

### 3.1 DeepSeek Direct

1. Get an API key from the [DeepSeek open platform](https://platform.deepseek.com/).
2. Open `config.json` and fill in `real_deepseek_key`:
   ```json
   {
     "real_deepseek_key": "sk-you…-key"
   }
   ```
3. With the default configuration the DeepSeek endpoint is `https://api.deepseek.com/anthropic` and works over a direct connection.

---

### 3.2 Codex (CLIProxyAPI) Bridge

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) converts OpenAI Codex / GPT models into the Anthropic Messages format.

1. **Download and place it**:
   - Download the binary for your platform from the CLIProxyAPI release page.
   - On Windows, put `cli-proxy-api.exe` into this project's `codex-proxy/` directory.
2. **Create and configure `config.yaml`**:
   - Copy the template:
     ```bash
     cd codex-proxy
     copy config.example.yaml config.yaml
     ```
   - Edit `codex-proxy/config.yaml` and set the listen port (default `8317`) and proxy as needed.
3. **Complete the OAuth / token login**:
   - Run `start_proxy.bat`, or launch `cli-proxy-api.exe` manually, to authorise the account.
   - Verify the service responds at `http://127.0.0.1:8317/v1/models`.
4. **Bind it into `config.json`**:
   ```json
   {
     "codex_proxy_key": "your-proxy-key",
     "codex_exe": "C:\\path\\to\\cc-relay\\codex-proxy\\cli-proxy-api.exe",
     "codex_config": "C:\\path\\to\\cc-relay\\codex-proxy\\config.yaml"
   }
   ```
   *Once `codex_exe` is configured, cc-relay will silently start the proxy in the background whenever a request routes to Codex and the proxy is not running.*

---

### 3.3 Gemini (Antigravity Tools) Bridge

1. Install and start Antigravity Tools manually when needed; it should listen on port `8045` (it exposes an Anthropic-compatible endpoint). cc-relay does not implicitly launch it by default; an already-running instance remains usable. The web console header provides independent Codex/Gemini start and stop controls; Gemini stop only targets an instance launched and tracked by the current cc-relay process, leaving manually started or mismatched executables untouched.
2. Configure it in `config.json`:
   ```json
   {
     "antigravity_key": "",
     "antigravity_exe": "C:\\path\\to\\antigravity-tools.exe",
     "upstreams": {
       "antigravity": {
         "base": "http://127.0.0.1:8045",
         "key_env": "antigravity_key",
         "proxy_url": "direct"
       }
     }
   }
   ```
3. To opt back into implicit startup, explicitly add `{"tools":{"antigravity":{"auto_start":true}}}`. The default is `false`. The protected local UI `/api/upstream` start action remains available for explicit manual startup.
4. Verify connectivity: click the **Gemini probe** in the web dashboard, or call `GET /api/probe?name=antigravity`.

### 3.4 Faster Antigravity Tools updates

Use the standalone updater instead of the application's slow self-download:

```bash
python update_antigravity.py check
python update_antigravity.py download --proxy http://127.0.0.1:7890
python update_antigravity.py install --proxy http://127.0.0.1:7890
```

It selects the Windows x64 installer from the official `lbjlaq/Antigravity-Manager` release and verifies the GitHub asset SHA-256 digest. `download` only stages the installer; `install` launches the official installer. A third-party acceleration prefix must be supplied explicitly with `--download-base` and should only be used if trusted.

---

## 4. Client Configuration

### 4.1 Claude Desktop (GUI) 3P Inference Gateway (Recommended)

The official Claude Desktop application features a native **3P (Third-Party Inference Gateway)** deployment mode. By applying the local 3P profile, Claude Desktop will route all chat inferences directly through `cc-relay` (port 8400), without requiring a claude.ai subscription or login credentials.

> 📌 **Key Feature**: This configuration writes to an isolated local profile directory (`%LOCALAPPDATA%\Claude-3p`). It is **completely decoupled from Claude Code CLI and will never touch or modify your CLI configuration**.

#### Enable 3P Mode in One Step:
```bash
# automatically reads config.json and configures Claude Desktop 3P gateway
python apply_settings.py --desktop

# or preview the profile before writing
python apply_settings.py --desktop --dry-run
```
*(Windows users can also double-click `apply_settings.bat`)*

Once applied, **completely exit and restart Claude Desktop**:
- Claude Desktop will start in 3P gateway mode and send all chat requests to `cc-relay` (`http://127.0.0.1:8400`).
- You can monitor live requests and tokens in the Web Dashboard (`http://127.0.0.1:8610`).
- **Note**: In 3P mode, the application disconnects from the claude.ai cloud account; all conversation histories are stored safely on your local machine.

#### Revert to Official 1P Default:
To switch back to official claude.ai cloud account login:
```bash
python apply_settings.py --remove-desktop
```
Restart Claude Desktop to return to the standard account login screen.

---

### 4.2 Injecting the Claude Code CLI Configuration

If you also wish to configure the terminal Claude Code CLI to use `cc-relay`:

#### Method A: automated injection script (requires `--cli`)

```bash
# preview what would be written (safe, read-only)
python apply_settings.py --cli --dry-run

# perform the safe incremental merge into ~/.claude/settings.json
python apply_settings.py --cli
```

`apply_settings.py` reads the current `config.json` for the listen port and `fake_api_key`, then merges the following variables into the `env` node of `settings.json` — **without ever losing or overwriting your other custom environment variables**:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8400",
    "ANTHROPIC_AUTH_TOKEN": "sk-relay-local-0000",
    "ANTHROPIC_MODEL": "relay-main[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "OPUS_MODEL[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "SONNET_MODEL[1m]",
    "ANTHROPIC_SMALL_FAST_MODEL": "FAST_MODEL[1m]"
  }
}
```

#### Method B: per-terminal environment variables

If you would rather not touch the global config file, inject them per terminal:

**Windows PowerShell:**
```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8400"
$env:ANTHROPIC_AUTH_TOKEN="sk-relay-local-0000"
$env:ANTHROPIC_MODEL="relay-main[1m]"
$env:ANTHROPIC_DEFAULT_OPUS_MODEL="OPUS_MODEL[1m]"
$env:ANTHROPIC_DEFAULT_SONNET_MODEL="SONNET_MODEL[1m]"
$env:ANTHROPIC_SMALL_FAST_MODEL="FAST_MODEL[1m]"
claude
```

**macOS / Linux Bash / Zsh:**
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8400"
export ANTHROPIC_AUTH_TOKEN="sk-relay-local-0000"
export ANTHROPIC_MODEL="relay-main[1m]"
export ANTHROPIC_DEFAULT_OPUS_MODEL="OPUS_MODEL[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="SONNET_MODEL[1m]"
export ANTHROPIC_SMALL_FAST_MODEL="FAST_MODEL[1m]"
claude
```

---

## 5. Seamless Automation on Windows (Wrapper & Lifecycle)

To keep the experience friction-free, the project ships a Windows wrapper and background automation:

### 5.1 Make the wrapper take priority

1. Add `C:\path\to\cc-relay\wrapper` to the system `PATH` **ahead of** the npm global directory.
2. From then on, typing `claude` in any console:
   - calls `wrapper/claude.cmd` first;
   - runs `python lifecycle.py autostart` to wake the relay in under a second;
   - starts the background `lifecycle.py watch` watchdog;
   - then hands over to the real Claude Code.
3. Once every `claude.exe` process has exited, the watchdog notices the idle timeout and stops the relay and the Codex upstream, freeing system resources.

### 5.2 Web Dashboard & Monitoring

With the relay running, open **[http://127.0.0.1:8610](http://127.0.0.1:8610)** in your browser at any time to inspect:
- Active routing modes and upstream models;
- Live packet capture and token usage from both Desktop and CLI clients;
- Prompt Cache hit rate and upstream health probes.

---

## 6. Daemonization on macOS / Linux

On POSIX systems you can run `cc-relay` as a persistent per-user background service with `systemd` (Linux) or `launchd` (macOS).

### 6.1 Linux systemd user service

Create `~/.config/systemd/user/cc-relay.service`:

```ini
[Unit]
Description=Claude Code Multi-Upstream Relay
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/your-user/cc-relay
ExecStart=/usr/bin/python3 cc_relay.py serve
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

Enable and start it:
```bash
systemctl --user daemon-reload
systemctl --user enable --now cc-relay.service
systemctl --user status cc-relay.service
```

---

## 7. Daily Operations and Diagnostics

| Goal | Command |
| :--- | :--- |
| **Foreground debug run** | `python cc_relay.py serve` |
| **Foreground run without the UI** | `python cc_relay.py serve --no-ui` |
| **Check service and process status** | `python lifecycle.py status` |
| **Stop the relay and all sidecars** | `python lifecycle.py stopall` |
| **Inspect the last 10 captured calls** | `python cc_relay.py last 10` |
| **Traffic statistics** | `python cc_relay.py stats` |
| **Dry-run the config injection** | `python apply_settings.py --dry-run` |

---

## 8. Troubleshooting FAQ

### Q1: `[Errno 10048]` / `Address already in use` on startup
- **Cause**: port 8400 (relay) or 8610 (UI) is taken by a leftover process or another application.
- **Fix**:
  - On Windows run `python lifecycle.py stopall` to force-clean leftovers.
  - Or change `"listen_port"` and `"ui_port"` in `config.json` to free ports, then re-run `python apply_settings.py`.

### Q2: `401 Unauthorized` or `invalid api key`
- **Cause**: the token Claude Code sends does not match `fake_api_key` in `config.json`.
- **Fix**:
  - Check `"fake_api_key"` in `config.json` (default `sk-rel…0000`).
  - Re-run `python apply_settings.py` to refresh `~/.claude/settings.json`.

### Q3: `502 Bad Gateway` after switching to the Codex upstream
- **Cause**: `codex-proxy` (CLIProxyAPI) is not running, or its OAuth login has expired.
- **Fix**:
  - Check that `http://127.0.0.1:8317/v1/models` is reachable.
  - Run `codex-proxy/start_proxy.bat` manually and look for login or proxy errors.

### Q4: `503 No available accounts` after switching to the Gemini upstream
- **Cause**: this error comes straight from the local Antigravity Tools endpoint (8045), meaning the currently bound Google account is out of quota or has become invalid.
- **Fix**: open the Antigravity Tools client and refresh or re-login the Google account.

### Q5: Why is the prompt-cache hit rate so low in the web UI?
- **Cause**:
  1. the upstream model does not enable or support prompt caching;
  2. the System prompt or conversation history changed drastically between two consecutive requests;
  3. note: `cc-relay`'s `builtin` trimming mode carries cache breakpoints forward, so it does not break cache continuity.
