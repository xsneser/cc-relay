# cc-relay

> 🌐 English | [简体中文](README.md)

<p align="center">
  <b>Zero dependencies · High performance · Multi-upstream smart routing · Local traffic dispatch and fingerprint-trimming hub for Claude Code</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/Dependencies-Zero%20(Pure%20Stdlib)-success.svg" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-orange.svg" alt="Platform">
</p>

---

## 📌 Overview

`cc-relay` is a lightweight local relay gateway built for **Claude Code (CC)** and any client that speaks the Anthropic Messages protocol (Claude Desktop, IDE plugins, and so on).

Through one unified endpoint it aggregates and dispatches to three upstreams — **DeepSeek**, **Codex (CLIProxyAPI)** and **Gemini (Antigravity Tools)** — and supports five-tier smart routing driven by model tier, subagent markers and thinking budget. It ships with a web dashboard, live packet capture, prompt-cache hit-rate monitoring and a 3-state client fingerprint trimmer.

---

## 🚀 Core Features

- ⚡ **Zero third-party dependencies for root relay**: root `cc_relay.py` is written entirely against the Python 3.8+ standard library. No `pip install` needed — clone it and it starts in seconds.
- 🔀 **Three first-class upstreams**:
  - **DeepSeek** — direct connection to the official Anthropic-compatible endpoint, best cost/performance.
  - **Codex (CLIProxyAPI)** — bridges official GPT models (GPT-5.6 / GPT-6 and friends) with Reasoning Effort budget translation.
  - **Gemini (Antigravity Tools)** — bridges Google Gemini models; existing sidecars are used, while implicit startup is opt-in.
- 🎯 **Five-tier routing**:
  - `main`: everyday main conversation model
  - `opus`: complex architecture work and deep reasoning (Plan mode / Opus placeholder)
  - `sonnet`: medium-complexity tasks and analysis (Sonnet placeholder)
  - `fast`: high-frequency background calls and status checks (Haiku / Fast placeholder)
  - `agent`: subagent call detection (precisely catches `x-claude-code-agent-id`, billing markers and Agent SDK signatures)
- 🛡️ **3-state CC fingerprint trimming**:
  - `original`: pure pass-through, no System prompt is touched.
  - `builtin`: strips the Claude Code identity markers and billing header, **while preserving Prompt Cache breakpoints losslessly**.
  - `custom`: edit the System prompt online in the web UI, or hot-reload the local `custom_modifier.py` Python hook.
- 📊 **Visual web console & capture auditor**:
  - Live throughput monitoring, token accounting, prompt-cache hit-rate visualisation.
  - Full request/response packet viewer with multi-dimensional filtering and per-call deep dive.
  - Switch the global route, tune each tier's model, and configure thinking effort and trimming switches from the browser.
- 🔄 **Fully automatic lifecycle watchdog**:
  - A lightweight Windows wrapper and watchdog service spin the relay up alongside `claude` in seconds, and put it back to sleep once every Claude instance has exited.
- 📦 **Windows One-Click Installer & Cooperative Uninstaller (v2.4.10)**:
  - **Self-contained offline ASR**: Bundles an embedded portable Python 3.10 runtime and SenseVoice INT8 offline model (~228MB), delivering instant desktop capsule voice typing with zero prerequisites.
  - **Dual ASR Engines & Contextual Semantic Correction**: Supports SenseVoice ultra-fast CPU offline ASR and Qwen 1.7B high-precision ASR; automatically matches foreground terminal Claude CLI context to align code identifiers and homophones, with adjustable reasoning effort slider.
  - **Cooperative Uninstallation**: Interactive uninstaller wizard allowing selective cleanup of Codex proxy, Gemini Antigravity, and ASR models.
  - **Online Silent Updates**: Check and upgrade in-place seamlessly without losing user API keys and configurations.

---

## 🏗️ Architecture

```text
Claude Code CLI / Desktop
  ANTHROPIC_BASE_URL = http://127.0.0.1:8400
        │  HTTP POST /v1/messages
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        cc-relay  (port 8400)                         │
│  1. downstream auth      constant-time fake_api_key check            │
│  2. request sniffing     subagent header / Agent SDK markers         │
│  3. five-tier routing    main / opus / sonnet / fast / agent         │
│  4. budget injection     thinking budget / reasoning effort          │
│  5. 3-state trimming     original / builtin / custom                 │
│  6. audit persistence    records.jsonl, rotated at 1 GB              │
└──────────────────────────────────────────────────────────────────────┘
        │
        ├── direct ──────────▶ DeepSeek            api.deepseek.com/anthropic
        ├── local HTTP proxy ▶ Codex Proxy         CLIProxyAPI :8317
        └── local HTTP proxy ▶ Antigravity Tools   :8045

┌──────────────────────────────────────────────────────────────────────┐
│                    Web console  (port 8610)                          │
│  traffic board / cache analysis / packet capture / prompt editing /  │
│  upstream probes and live hot-switching                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Quick Start

### 1. Clone and initialise the config

```bash
git clone https://github.com/xsneser/cc-relay.git
cd cc-relay

# create your local config from the template
cp config.example.json config.json
```

Edit `config.json` and fill in at least your real key (for example a DeepSeek API key):

```json
{
  "real_deepseek_key": "sk-your-real-deepseek-key",
  "fake_api_key": "sk-relay-local-0000"
}
```

### 2. Start the service

```bash
# foreground run (core relay on 8400 plus web UI on 8610)
python cc_relay.py serve
```

Open the console at **[http://127.0.0.1:8610](http://127.0.0.1:8610)**.

### 3. Connect your clients

#### A. Claude Desktop (GUI) 3P Inference Gateway (Recommended)
Configure Claude Desktop in one step (writes to an isolated local 3P profile, never touches your CLI):
```bash
# configure Claude Desktop official 3P gateway
python apply_settings.py --desktop
```
Restart Claude Desktop to start using the local relay! To switch back to official account login, run `python apply_settings.py --remove-desktop`.

#### B. Claude Code CLI
Run the bundled safe injection tool (incremental merge into `~/.claude/settings.json`):

```bash
python apply_settings.py --cli
```

Or set the variables manually in your terminal:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8400"
export ANTHROPIC_AUTH_TOKEN="sk-relay-local-0000"
export ANTHROPIC_MODEL="relay-main[1m]"
export ANTHROPIC_DEFAULT_OPUS_MODEL="OPUS_MODEL[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="SONNET_MODEL[1m]"
export ANTHROPIC_SMALL_FAST_MODEL="FAST_MODEL[1m]"
```

Now just run `claude` and all of its traffic gets smart dispatch and monitoring.

> 💡 For the full Desktop 3P gateway guide, Windows one-click integration, Codex / Gemini upstream setup and background service instructions, see **[INSTALL.en.md](INSTALL.en.md)**.

---

## 🧩 Routing and Dispatch

`cc-relay` offers four top-level routing modes (hot-switchable from the web UI at any time):

| Mode | Description | When to use |
| :--- | :--- | :--- |
| `hybrid` (recommended) | **Mixed multi-tier dispatch**: requests are dispatched to different upstreams based on model type and subagent markers | Day-to-day heavy development, balancing reasoning quality against cost |
| `deepseek` | **DeepSeek only**: every request goes to DeepSeek | Cost- and latency-sensitive work |
| `codex` | **Codex only**: every request goes through CLIProxyAPI to GPT models | When you need the strongest code generation and long-context reasoning |
| `antigravity` | **Gemini only**: every request goes through Antigravity Tools to Gemini | Testing the Gemini family or multimodal tasks |

### Five-tier match priority (hybrid mode)

1. **OPUS tier**: catches `OPUS_MODEL` / `claude-opus-*` placeholders, dispatched to a high-reasoning model by default (such as `gpt-5.6-sol`).
2. **SONNET tier**: catches `SONNET_MODEL` / `claude-sonnet-*` placeholders.
3. **FAST tier**: catches `FAST_MODEL` / `claude-haiku` placeholders.
4. **Explicit model routing**:
   - An explicit `model_routes` rule wins and is dispatched as written.
   - `gpt-*` models belong to the `codex` upstream.
   - `gemini-*` models belong to the `antigravity` upstream.
   - `deepseek-*` models belong to the `deepseek` upstream.
5. **AGENT (subagent) tier**: when the request header carries `x-claude-code-agent-id`, or the System prompt carries subagent markers, the request uses the `agent` tier on its own.
6. **MAIN tier**: every other regular interactive request falls back to the `main` tier.

---

## 🛡️ CC Fingerprint Trimming and the Modifier Workflow

Some unofficial upstreams inspect the default System prompt and billing header that the Claude Code client sends. `cc-relay` lets you control the trimming mode per tier:

- **`original` (pass-through)**: no trimming at all.
- **`builtin` (built-in cleanup)**:
  - Automatically strips the `You are Claude Code, Anthropic's official CLI...` and `x-anthropic-billing-header:` fingerprints.
  - **Breakpoint carry-forward**: if a removed block carried a `cache_control` marker, the relay carries it forward to the next surviving block, so the **Prompt Cache acceleration is never lost**.
- **`custom`**:
  - Prefers the tier prompt you edited and enabled in the web console.
  - Otherwise it can hot-load `modify_headers` and `modify_body` from `custom_modifier.py` in the project directory.

---

## 📡 RESTful API Reference

The web console talks to the relay over these plain JSON endpoints, which are equally usable for third-party automation:

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Runtime status snapshot (current route, upstream health, per-tier model mapping, aggregated call metrics) |
| `GET` | `/api/config` | Redacted config view (upstream list, proxy addresses; no plaintext secrets) |
| `POST`| `/api/config` | Persist config updates (edit upstream Base URL, proxy settings and keys online) |
| `POST`| `/api/route` | Hot-switch the route (`hybrid` / `deepseek` / `codex` / `antigravity`), per-tier models, thinking effort and trimming mode |
| `GET` | `/api/probe` | Actively probe one upstream's availability and model list (`?name=antigravity|codex|deepseek`) |
| `POST`| `/api/upstream` | Manage external sidecar processes (start/stop the Codex or Antigravity proxy) |
| `GET` | `/api/calls` | Recent request summaries (`?n=100`, including token usage and cache hit rate) |
| `GET` | `/api/call` | One capture in full (`?idx=<id>`: raw headers, request body and response body) |
| `GET` | `/api/prompts`| Captured original System prompts plus custom prompts, per tier |
| `POST`| `/api/prompts`| Save and enable/disable a tier's custom System prompt |
| `POST`| `/api/reset` | Clear the local traffic log (`records.jsonl`) |

---

## 🔄 Antigravity Tools Updates

cc-relay does not implicitly launch Antigravity Tools during a request or `claude` wrapper startup. An already-running `8045` sidecar remains usable; start it explicitly from the local UI when needed. The header provides independent Codex and Gemini start/stop controls; Gemini stop only terminates an instance launched and tracked by the current cc-relay process, leaving manually started or mismatched executables untouched. To avoid the application's slow self-download, use the standalone updater:

```bash
python update_antigravity.py check
python update_antigravity.py download
python update_antigravity.py install
```

The updater selects the Windows x64 installer from the official [Antigravity-Manager release](https://github.com/lbjlaq/Antigravity-Manager/releases) and verifies its SHA-256 against the GitHub asset digest. Use `--proxy http://127.0.0.1:PORT` for a local HTTP proxy or explicitly provide a trusted acceleration prefix with `--download-base`. Downloading stages the installer without changing the current installation; only `install` launches the official installer.

---

## ⚙️ Config Dictionary (`config.json`)

```json
{
  "listen_host": "127.0.0.1",          // listen address (always keep 127.0.0.1 in production)
  "listen_port": 8400,                 // relay main port
  "ui_port": 8610,                     // web console port
  "fake_api_key": "sk-relay-local-0000",// token downstream clients present to this relay
  "real_deepseek_key": "sk-...",       // real DeepSeek API key
  "codex_proxy_key": "...",            // access key of the Codex proxy endpoint
  "antigravity_key": "",               // Antigravity access key (usually empty)
  "codex_exe": "",                     // absolute path to the CLIProxyAPI executable (for auto-start)
  "codex_config": "",                  // absolute path to the CLIProxyAPI config file
  "antigravity_exe": "",               // path to the Antigravity executable
  "tools": {
    "antigravity": {
      "auto_start": false                // no implicit launch; explicit UI start remains available
    }
  },
  "max_body_capture": 2000000,         // per-request capture truncation limit in bytes (2 MB)
  "upstreams": {
    "deepseek": {
      "base": "https://api.deepseek.com/anthropic",
      "key_env": "real_deepseek_key",
      "proxy_url": "direct"
    },
    "codex": {
      "base": "http://127.0.0.1:8317",
      "key_env": "codex_proxy_key",
      "proxy_url": "direct"
    },
    "antigravity": {
      "base": "http://127.0.0.1:8045",
      "key_env": "antigravity_key",
      "proxy_url": "direct"
    }
  },
  "router": {
    "route": "hybrid",
    "tiers": {
      "main": "deepseek-flash",
      "opus": "gpt-5.6-sol",
      "sonnet": "deepseek-flash",
      "fast": "deepseek-flash",
      "agent": "deepseek-flash"
    },
    "tier_efforts": {
      "main": "medium",
      "opus": "high",
      "sonnet": "medium",
      "fast": "low",
      "agent": "medium"
    },
    "tier_modifier": {
      "main": "builtin",
      "opus": "original",
      "sonnet": "original",
      "fast": "builtin",
      "agent": "original"
    }
  },
  "model_routes": {
    "gemini-exp-*": "antigravity"
  }
}
```

---

## 🔒 Security Model and Privacy

1. **Loopback binding only**: `listen_host` defaults to `127.0.0.1`. Unless you are on a fully trusted private network, **never** open it up to `0.0.0.0`, or the web console and your keys become reachable from the internet.
2. **Downstream authentication**: the relay checks downstream `/v1/messages` and `/v1/models` requests with a constant-time comparison (`hmac.compare_digest`) so unauthorised clients cannot get in.
3. **Data privacy and rotation**: `records.jsonl` and `prompts.json` capture complete prompts and tool-call payloads (for auditing and debugging). The project `.gitignore` excludes them by default; keep the machine safe and never distribute capture files that contain sensitive projects.

---

## 🎙️ Claude Code CLI Voice Input Companion (Optional Extension)

For Windows terminal users dealing with unreliable `Win + H` dictation or the official `/voice` command requiring direct network connections incompatible with local proxies, `cc-relay` provides a standalone offline speech-to-text companion in `tools/voice_input/` powered by **SenseVoice-Small + sherpa-onnx**:
- **Two-Step Quick Start**: Run `setup_voice.bat` once to auto-configure the isolated virtual environment and download the ~239MB offline model, then run `start_voice.bat` (or click Start in the Web dashboard);
- **Push-to-Talk**: Hold hotkey (default mouse back button `mouse_x1`, configurable to `F8` or `CapsLock`), speak, and release to inject transcribed text into your active terminal/editor cursor without losing window focus;
- **Pure Local CPU Offline Inference**: Zero external API calls, extremely low latency, no Visual C++ compiler or PyTorch required out of the box;
- **Spoken Slash Commands**: Voice phrases like "*slash cost*" are automatically converted to `/cost`;
- **Isolated Environment**: Dependencies live strictly in `tools/voice_input/.venv`, preserving `cc-relay`'s core zero-pip-dependency architecture.

See [tools/voice_input/README.md](tools/voice_input/README.md) for full configuration and documentation.

---

## 📄 License and Terms

- **Project code (`cc-relay`)**: Released under the [MIT License](LICENSE). Pure Python standard library implementation, free for both commercial and non-commercial use.
- Third-party bridging tools referenced by this project (such as `CLIProxyAPI`) remain under their own licenses and copyrights.
