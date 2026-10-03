# cc-relay

> 🌐 [English](README.en.md) | 简体中文

<p align="center">
  <b>零依赖 · 高性能 · 多上游智能路由 · Claude Code 本地流量调度与特征修剪中枢</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/Dependencies-Zero%20(Pure%20Stdlib)-success.svg" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-orange.svg" alt="Platform">
</p>

---

## 📌 项目概述

`cc-relay` 是专为 **Claude Code (CC)** 以及兼容 Anthropic Messages 协议的客户端（如 Claude Desktop、IDE 插件等）设计的轻量级本地中转网关。

它通过统一的标准接口，实现对 **DeepSeek**、**Codex (CLIProxyAPI)** 与 **Gemini (Antigravity Tools)** 三大上游的无缝汇聚与按需分流，并支持基于模型档位、子代理（Subagent）特征以及思考预算的五档位智能调度。同时内置 Web 可视化仪表盘、实时抓包分析、Prompt 缓存命中率监控与 3 态客户端指纹修剪机制。

---

## 🚀 核心特性

- ⚡ **核心中转零第三方依赖 (Zero Dependencies)**：根目录 `cc_relay.py` 均由纯 Python 3.8+ 标准库编写，无需 `pip install` 任何第三方包，克隆即可秒级启动。
- 🔀 **3 大主流上游汇聚**：
  - **DeepSeek**：官方 Anthropic 兼容端点直连，极致性价比。
  - **Codex (CLIProxyAPI)**：桥接官方 GPT 模型（GPT-5.6 / GPT-6 等），支持 Reasoning Effort 预算转换。
  - **Gemini (Antigravity Tools)**：桥接 Google Gemini 模型；默认只使用已运行的 sidecar，自动拉起需显式开启。
- 🎯 **5 档位智能路由体系 (5-Tier Routing)**：
  - `main`：日常主对话模型
  - `opus`：复杂架构设计与深度推理（对应 Plan 模式 / Opus 占位符）
  - `sonnet`：中等复杂度任务与分析（对应 Sonnet 占位符）
  - `fast`：高频后台小调用、状态检测（对应 Haiku / Fast 占位符）
  - `agent`：子代理调用识别（精准捕获 `x-claude-code-agent-id`、Billing 标记及 Agent SDK 特征）
- 🛡️ **3 态 CC 特征修剪机制 (3-State Modifier Mode)**：
  - `original`：原汁原味纯透传，不改动任何 System 提示词。
  - `builtin`：智能剥离 Claude Code 身份标记与计费头，**严格无损继承 Prompt Cache 断点**。
  - `custom`：支持 Web 界面在线编辑 System 提示词，或热重载本地 `custom_modifier.py` Python 钩子。
- 📊 **可视化 Web 控制台 & 抓包审计器**：
  - 实时吞吐监控、Token 消耗统计、Prompt Cache 命中率可视化分析。
  - 完整请求/响应报文抓包查看器，支持多维过滤、单条调用深度回溯。
  - 支持在 Web 端一键切换全局路由、微调各档位模型、配置思考强度与指纹修剪开关。
- 🔄 **全自动生命周期看门狗 (Lifecycle Automation)**：
  - 提供轻量 Windows Wrapper 与看门狗服务，伴随 `claude` 命令按需秒起，并在所有 Claude 实例退出后自动休眠回收资源。
- 📦 **Windows 一键安装包与协同卸载向导 (Installer & Uninstaller)**：
  - **自包含 ASR 离线环境**：集成免配置 Python 3.10 便携运行时与 SenseVoice INT8 语音大模型，用户电脑无需预装 Python，安装即享全局悬浮胶囊语音打字。
  - **双引擎与大模型上下文语义校正 (v2.4.10)**：支持 SenseVoice 极速离线与 Qwen 1.7B 高精度双引擎；深度对齐 Claude CLI 终端上下文进行同音词与代码标识符智能校正，并支持自主拖动滑块调节推理思考强度。
  - **伴侣组件协同卸载**：卸载程序原生弹窗询问用户是否同步卸载 Codex (CPA)、Gemini (拉起 Antigravity Tools 官方卸载 exe) 及 ASR 离线大模型，一键彻底清理。
  - **双模在线静默升级**：安装版客户端点击「检查更新」时自动流式下载最新安装包并执行 `/SILENT` 静默升级与自动重启，绝对保护现有 API Keys。

---

## 🏗️ 架构设计

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        Claude Code CLI / Desktop                        │
│               (ANTHROPIC_BASE_URL = http://127.0.0.1:8400)              │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTP POST /v1/messages
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           cc-relay (Port: 8400)                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ 1. 下游认证鉴权 (fake_api_key 常量时间校验)                         │  │
│  │ 2. 请求特征嗅探 (识别子代理 Header / Agent SDK 提示词)              │  │
│  │ 3. 五档位智能路由匹配 (main / opus / sonnet / fast / agent)       │  │
│  │ 4. 思考预算注入 (Thinking Budget / Reasoning Effort)              │  │
│  │ 5. 3 态特征修剪与 Prompt 替换 (original / builtin / custom)       │  │
│  │ 6. 流量审计与持久化轮转 (records.jsonl / 缓存统计)                │  │
│  └─────────────────────────────────┬─────────────────────────────────┘  │
└───────┬────────────────────────────┼────────────────────────────┬───────┘
        │ 直连                       │ 本地 HTTP 代理             │ 本地 HTTP 代理
        ▼                            ▼                            ▼
┌──────────────┐             ┌──────────────┐             ┌──────────────┐
│   DeepSeek   │             │ Codex Proxy  │             │ Antigravity  │
│  (Anthropic  │             │ (CLIProxyAPI │             │ Tools        │
│  兼容接口)   │             │  Port: 8317) │             │  Port: 8045) │
└──────────────┘             └──────────────┘             └──────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         Web 控制台 (Port: 8610)                          │
│     流量大盘 / 缓存分析 / 报文抓包 / 在线提示词修改 / 上游探针与热切流   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ 快速上手

### 方式 1：Windows 一键安装包（推荐，开箱即用）

1. 下载最新的 `CC-Relay-Setup-v2.4.10.exe`（内置便携 ASR 语音环境与 SenseVoice 模型，免 Python 配置）；
2. 运行安装包，按照向导完成安装，默认安装至 `%LOCALAPPDATA%\Programs\CC-Relay`；
3. 安装完成后自动启动中转并在浏览器打开 Web 监控大盘 **[http://127.0.0.1:8610](http://127.0.0.1:8610)**；
4. 开发者如需从源码构建安装包，双击运行根目录下批处理即可：
   ```cmd
   build_installer.bat
   ```

### 方式 2：源码多平台运行（开发者模式）

#### 1. 克隆与初始化配置

```bash
git clone https://github.com/xsneser/cc-relay.git
cd cc-relay

# 从模板创建本地配置文件
cp config.example.json config.json
```

编辑 `config.json`，至少填入你的真实密钥（例如 DeepSeek API Key）：
```json
{
  "real_deepseek_key": "sk-your-real-deepseek-key",
  "fake_api_key": "sk-relay-local-0000"
}
```

#### 2. 启动服务

```bash
# 前台运行（包含核心 Relay 8400 与 Web UI 8610）
python cc_relay.py serve
```

访问浏览器控制台：**[http://127.0.0.1:8610](http://127.0.0.1:8610)**

#### 3. 配置客户端连接

#### A. Claude 桌面客户端 (GUI) 官方 3P 推理网关（推荐）
运行配置工具一键接入桌面客户端（完全隔离写入独立的本地 3P 配置文件，绝不污染 CLI）：
```bash
# 一键启用 Claude Desktop 官方 3P 网关
python apply_settings.py --desktop
```
重启 Claude Desktop 客户端即可直接使用本地中转！如需切回官方账号登录，执行 `python apply_settings.py --remove-desktop`。

#### B. Claude Code CLI 配置
运行注入工具增量合并至 `~/.claude/settings.json`：
```bash
python apply_settings.py --cli
```

或手动在终端设置环境变量：
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8400"
export ANTHROPIC_AUTH_TOKEN="sk-relay-local-0000"
export ANTHROPIC_MODEL="relay-main[1m]"
export ANTHROPIC_DEFAULT_OPUS_MODEL="OPUS_MODEL[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="SONNET_MODEL[1m]"
export ANTHROPIC_SMALL_FAST_MODEL="FAST_MODEL[1m]"
```

现在直接运行 `claude`，所有流量即可畅享智能分流与监控！

> 💡 详细的桌面版 3P 网关、Windows 一键集成、Codex / Gemini 上游配置、后台服务化教程请参阅 **[INSTALL.md](INSTALL.md)**。

---

## 🧩 路由与分流机制

`cc-relay` 提供四种顶层路由模式（可在 Web 端随时热切换）：

| 模式 | 描述 | 适用场景 |
| :--- | :--- | :--- |
| `hybrid` (推荐) | **混合多档智能分流**：根据模型类型与子代理标识分派至不同上游 | 日常高强度主力开发，兼顾推理能力与响应成本 |
| `deepseek` | **全量 DeepSeek 模式**：所有请求统一走 DeepSeek | 专注性价比、低延迟场景 |
| `codex` | **全量 Codex 模式**：所有请求经 CLIProxyAPI 转发至 GPT 模型 | 需要极强代码生成与长文本推理 |
| `antigravity` | **全量 Gemini 模式**：所有请求经 Antigravity Tools 转发至 Gemini | 测试 Gemini 系列或多模态任务 |

### 5 档位匹配优先级（Hybrid 模式）

1. **OPUS 档**：捕获 `OPUS_MODEL` / `claude-opus-*` 占位符，默认派发至高推理模型（如 `gpt-5.6-sol`）。
2. **SONNET 档**：捕获 `SONNET_MODEL` / `claude-sonnet-*` 占位符。
3. **FAST 档**：捕获 `FAST_MODEL` / `claude-haiku` 占位符。
4. **显式模型直连**：
   - 命中 `model_routes` 显式规则，优先按规则派发。
   - `gpt-*` 模型自动归属于 `codex` 上游。
   - `gemini-*` 模型自动归属于 `antigravity` 上游。
   - `deepseek-*` 模型自动归属于 `deepseek` 上游。
5. **AGENT 子代理档**：请求头包含 `x-claude-code-agent-id` 或 System 提示中包含子代理标记时，单独走 `agent` 档位。
6. **MAIN 主模型档**：其他常规交互请求默认走 `main` 档位。

---

## 🛡️ CC 指纹修剪与 Modifier 工作流

部分非官方上游对 Claude Code 客户端的默认 System 提示词与 Billing Header 存在检测限制。`cc-relay` 支持按档位精细控制修剪模式：

- **`original` (原始透传)**：不做任何修剪。
- **`builtin` (内置清理)**：
  - 自动剥离 `You are Claude Code, Anthropic's official CLI...` 与 `x-anthropic-billing-header:` 指纹。
  - **断点顺延算法**：若被删除的块带有 `cache_control` 标记，中转会自动将其顺延至紧随其后的有效块上，**绝不丢失 Prompt Cache 加速特性**。
- **`custom` (自定义模式)**：
  - 优先使用在 Web 控制台在线编辑并启用的对应档位提示词。
  - 若未指定，可热加载项目目录下的 `custom_modifier.py` 中的 `modify_headers` 与 `modify_body` 函数。

---

## 📡 RESTful API 接口参考

Web 控制台基于以下标准 JSON 接口通信，亦可用于第三方自动化集成：

| 方法 | 路径 | 功能说明 |
| :--- | :--- | :--- |
| `GET` | `/api/status` | 获取运行时状态快照（当前路由模式、各上游健康状态、各档位模型映射、历史调用指标汇总） |
| `GET` | `/api/config` | 获取脱敏的配置视图（上游列表、代理地址等，不暴露明文 Secret） |
| `POST`| `/api/config` | 保存基础配置更新（支持在线修改上游 Base URL、Proxy 设置及密钥） |
| `POST`| `/api/route` | 动态切换路由模式（`hybrid` / `deepseek` / `codex` / `antigravity`）、各档位模型、思考强度与修剪模式 |
| `GET` | `/api/probe` | 主动探测指定上游端点的可用性及可用模型列表（参数 `?name=antigravity|codex|deepseek`） |
| `POST`| `/api/upstream` | 管理外部 Sidecar 进程（启动/停止 Codex 或 Antigravity 代理） |
| `GET` | `/api/calls` | 获取历史请求摘要列表（支持 `?n=100`，包含 Token 消耗与缓存命中率） |
| `GET` | `/api/call` | 获取单条抓包详情（参数 `?idx=<id>`，包含原始 Headers、请求 Body 及响应 Body） |
| `GET` | `/api/prompts`| 获取各档位抓取到的原始 System 提示词与自定义提示词 |
| `POST`| `/api/prompts`| 保存并启用/禁用某档位的自定义 System 提示词 |
| `POST`| `/api/reset` | 清空本地运行流量记录 (`records.jsonl`) |

---

## 🔄 Antigravity Tools 更新

当前项目不会在请求或 `claude` wrapper 启动时自动拉起 Antigravity Tools。已有的 `8045` 实例仍可正常使用；需要启动时，请从本地 Web UI 明确执行 `/api/upstream` 的启动操作，或手动启动应用。顶栏中的 Codex 与 Gemini 启停按钮彼此独立；Gemini 停止操作只会停止当前 cc-relay 启动并跟踪的实例，手动启动或可执行路径不匹配的进程会被保留。可通过独立更新器从官方 GitHub Release 获取 Windows x64 安装包：

```bash
# 只检查版本（不下载、不修改安装）
python update_antigravity.py check

# 下载并校验官方 GitHub asset（不安装）
python update_antigravity.py download

# 下载后启动官方安装器；安装器本身仍由用户确认
python update_antigravity.py install
```

默认来源为 [Antigravity-Manager Releases](https://github.com/lbjlaq/Antigravity-Manager/releases)。更新器会严格选择 Windows x64 setup asset，并将 SHA-256 与 GitHub asset digest 比对；也可通过 `--proxy http://127.0.0.1:端口` 使用本机代理，或通过 `--download-base` 显式指定可信加速前缀。安装器启动前不会删除账号数据；下载失败或校验不通过时不会运行安装器。

---

## ⚙️ 配置字典 (`config.json`)

```json
{
  "listen_host": "127.0.0.1",          // 监听地址（生产环境请务必保持 127.0.0.1）
  "listen_port": 8400,                 // Relay 主服务端口
  "ui_port": 8610,                     // Web 控制台端口
  "fake_api_key": "sk-relay-local-0000",// 下游连接本中转使用的 Token
  "real_deepseek_key": "sk-...",       // DeepSeek 真实 API Key
  "codex_proxy_key": "...",            // Codex 代理端点访问 Key
  "antigravity_key": "",               // Antigravity 访问 Key（通常为空）
  "codex_exe": "",                     // CLIProxyAPI 可执行文件绝对路径（用于按需自启动）
  "codex_config": "",                  // CLIProxyAPI 配置文件绝对路径
  "antigravity_exe": "",               // Antigravity 可执行文件路径
  "tools": {
    "antigravity": {
      "auto_start": false                // 默认不隐式拉起；显式 API 启动仍可用
    }
  },
  "max_body_capture": 2000000,         // 单条请求抓包截断上限字节数 (2MB)
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

## 🔒 安全模型与隐私须知

1. **本地回环绑定 (Loopback Only)**：默认 `listen_host` 为 `127.0.0.1`。除非你在绝对受信任的私有网络中，否则**切勿**将监听地址开放为 `0.0.0.0`，以免 Web 控制台及密钥信息暴露在公网。
2. **下游鉴权校验**：Relay 对下游 `/v1/messages` 及 `/v1/models` 请求强制使用安全常量时间比对 (`hmac.compare_digest`)，防止非法客户端接入。
3. **数据隐私与轮转**：`records.jsonl` 与 `prompts.json` 会记录完整的交互提示词与工具调用报文（用于抓包审计与调试）。本项目 `.gitignore` 已默认将其严格排除，请妥善保管本地机器，切勿将包含敏感业务代码的记录文件分发。

---

## 🎙️ Claude Code CLI 中文语音输入伴侣 (可选扩展)

针对 Windows 终端下使用 Claude Code CLI 时 `Win + H` 体验差、官方 `/voice` 依赖海外网络且不支持第三方代理的问题，本项目在 `tools/voice_input/` 提供了基于 **SenseVoice-Small + sherpa-onnx** 的独立中文语音伴侣：
- **两步极速上手**：首次双击根目录 `setup_voice.bat` 自动完成环境与模型初始化，之后双击 `start_voice.bat`（或在 Web 控制台一键启动）即可使用；
- **对讲输入**：在任意终端或编辑器中按住热键说话（默认鼠标后侧键 `mouse_x1`，支持 `F8`、`CapsLock` 等），松开即在毫秒内转写并安全填入光标位置；
- **纯本地 CPU 离线推理**：开箱即用，无需 Visual C++ 编译器或 PyTorch，极低延迟，中英文代码术语友好；
- **口述斜杠命令智能转换**：例如“*斜杠 cost*”自动转为 `/cost`；
- **独立虚拟环境隔离**：完全隔离在 `tools/voice_input/.venv` 中，绝不侵入主服务零 pip 依赖架构。

使用指南与进阶配置详见：[tools/voice_input/README.md](tools/voice_input/README.md)

---

## 📄 开源许可证与协议说明

- **本项目代码 (`cc-relay`)**：基于 [MIT License](LICENSE) 开源发布。纯 Python 标准库实现，商业与非商业均可自由使用。
- 项目中涉及的第三方桥接工具（如 `CLIProxyAPI`）遵循其各自独立的开源协议与版权声明。
