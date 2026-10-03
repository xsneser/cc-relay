# cc-relay 部署与实战安装手册

> 🌐 [English](INSTALL.en.md) | 简体中文

本手册提供 `cc-relay` 从零开始的完整安装、跨平台配置、三大上游接入、Claude Code 注入以及常见故障排查指引。

---

## 📋 目录

1. [前置环境要求](#1-前置环境要求)
2. [Windows 一键安装包与协同卸载 (推荐)](#2-windows-一键安装包与协同卸载-推荐)
   - [2.1 安装包特性 (内置 ASR 离线环境)](#21-安装包核心特性)
   - [2.2 卸载向导与三组件协同清理 (CPA / Anti / Voice)](#22-卸载向导与三组件协同清理)
   - [2.3 开发者自行打包构建流程](#23-开发者自行打包构建流程)
3. [获取源码与初始配置 (开发者模式)](#3-获取源码与初始配置-开发者模式)
4. [三大上游对接实操](#4-三大上游对接实操)
   - [4.1 DeepSeek 上游直连](#41-deepseek-上游直连)
   - [4.2 Codex (CLIProxyAPI) 桥接](#42-codex-cliproxyapi-桥接)
   - [4.3 Gemini (Antigravity Tools) 桥接](#43-gemini-antigravity-tools-桥接)
5. [客户端配置接入 (Desktop 3P & CLI)](#5-客户端配置接入)
   - [5.1 Claude 桌面客户端 (GUI) 3P 推理网关配置](#51-claude-桌面客户端-gui-3p-推理网关配置推荐)
   - [5.2 Claude Code CLI 配置注入](#52-claude-code-cli-配置注入)
6. [Windows 无感自动化体验 (Wrapper & Lifecycle)](#6-windows-无感自动化体验-wrapper--lifecycle)
7. [macOS / Linux 守护与服务化](#7-macos--linux-守护与服务化)
8. [日常运维与诊断命令](#8-日常运维与诊断命令)
9. [常见故障排查 FAQ](#9-常见故障排查-faq)

---

## 1. 前置环境要求

- **Windows 一键安装包模式**：
  - Windows 10 / Windows 11 (64位)
  - **无需安装 Python、无需配置 C++ 运行环境、无需预装 Git**，安装包自包含完整便携运行时与 SenseVoice INT8 离线模型。
- **源码开发运行模式**：
  - **Python 3.8 或更高版本**（必须已加入系统 PATH）
    - 验证命令：`python --version` 或 `python3 --version`
    - *注：cc-relay 核心中转采用纯标准库构建，无需安装任何 pip 扩展包。*
  - **Claude Code CLI**（官方已正常安装可用）
    - 验证命令：`claude --version`
  - **Git**
    - 验证命令：`git --version`

---

## 2. Windows 一键安装包与协同卸载 (推荐)

### 2.1 安装包核心特性
- **开箱即用**：采用 Inno Setup 6 封装为标准安装包 `CC-Relay-Setup-v2.4.9.exe`，安装路径默认位于 `%LOCALAPPDATA%\Programs\CC-Relay`，**无需管理员提权**即可完成安装。
- **集成离线 ASR 语音环境**：内置独立的便携式 Python 3.10 运行时、`sherpa-onnx` 依赖库以及 **228MB SenseVoice INT8 语音大模型**。用户即使在无网络或无系统 Python 的纯净机上，双击安装即可立即使用全局语音打字与悬浮胶囊。
- **环境自动配置**：安装向导自动将 `{app}\wrapper` 目录追加到当前用户的 `PATH` 环境变量中，并实时广播 `WM_SETTINGCHANGE` 消息，用户打开任何终端直接输入 `claude` 即可无感直通本地中转。
- **用户资产保护**：升级安装时严格保护已存在的 `config.json`，用户填写的各上游 API Key、路由规则及历史记录绝不被覆盖。

### 2.2 卸载向导与三组件协同清理
当在 Windows 11 设置「已安装的应用」或控制面板中卸载 `CC Relay` 时，卸载程序 (`unins000.exe`) 会原生弹出交互对话框，针对 UI 顶栏常态托管的三大伴侣组件进行智能协同检测与询问：
1. **[X] 语音伴侣 (Voice ASR) 离线运行环境与 SenseVoice 模型 (~380MB)** *(默认勾选)*：强制终止端口 8401 进程，彻底清理便携 Python 运行时与模型缓存，为用户释放磁盘空间。
2. **[ ] Codex 代理网关 (CLIProxyAPI :8317)** *(默认不勾选)*：优先检测是否存在独立卸载程序并拉起执行；便携绿色版则安全清空目录与登录凭据。
3. **[ ] Gemini 伴侣 (拉起 Antigravity Tools 官方卸载程序)** *(默认不勾选)*：自动探测注册表与本地安装路径，**直接拉起 Antigravity Tools 官方原生的 `uninstall.exe`** 执行独立卸载。
4. **[ ] 彻底清除用户个人配置文件 (包含 config.json、历史记录与 API 密钥)** *(默认不勾选)*。
5. **环境变量还原**：卸载完成后自动从当前用户的 `PATH` 环境变量中干净剔除 wrapper 路径，绝无任何系统残留。

### 2.3 开发者自行打包构建流程
如果需要基于当前源码重新生成安装包，仅需在根目录下运行一键构建流水线：
```cmd
build_installer.bat
```
构建脚本将全自动执行：
1. 环境探针：检查 Inno Setup 6 编译器 `ISCC.exe`（缺失时支持通过 `winget` 自动安装）；
2. 主程序编译：通过 PyInstaller 根据 `cc-relay.spec` 编译生成后台静默版 `dist\cc-relay.exe`；
3. ASR 运行时封装：调用 `tools\voice_input\bundle_runtime.py` 自动化组装便携 Python 运行时并验证模型；
4. 静态元数据固化：调用 `installer_build\prepare_staging.py` 写入 Git Commit Hash 至 `build_info.json`；
5. 生成安装包：调用 Inno Setup 进行 LZMA2 高压缩打包，产物生成在 `output\CC-Relay-Setup-v2.4.9.exe`。

---

## 3. 获取源码与初始配置 (开发者模式)

### 3.1 克隆仓库

```bash
# 克隆仓库至本地
git clone https://github.com/xsneser/cc-relay.git
cd cc-relay
```

### 3.2 生成本地配置文件

```bash
# Windows (CMD / PowerShell)
copy config.example.json config.json

# macOS / Linux
cp config.example.json config.json
```

> ⚠️ **安全警告**：`config.json` 包含你的真实上游 API Key，严禁上传或提交至公开代码仓库！项目自带的 `.gitignore` 已默认忽略该文件。

---

## 3. 三大上游对接实操

### 3.1 DeepSeek 上游直连

1. 前往 [DeepSeek 开放平台](https://platform.deepseek.com/) 获取 API Key。
2. 打开 `config.json`，填写 `real_deepseek_key`：
   ```json
   {
     "real_deepseek_key": "sk-your-actual-deepseek-api-key"
   }
   ```
3. 默认配置下，DeepSeek 上游端点为 `https://api.deepseek.com/anthropic`，网络直连即可使用。

---

### 3.2 Codex (CLIProxyAPI) 桥接

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 可以将 OpenAI Codex / GPT 系列模型转换为 Anthropic Messages 兼容格式。

1. **下载与放置**：
   - 从 CLIProxyAPI 官方 Release 页面下载对应系统的二进制文件。
   - Windows 用户将 `cli-proxy-api.exe` 放入本项目的 `codex-proxy/` 目录下。
2. **生成与配置 `config.yaml`**：
   - 复制模板：
     ```bash
     cd codex-proxy
     copy config.example.yaml config.yaml
     ```
   - 编辑 `codex-proxy/config.yaml`，按需配置监听端口（默认 `8317`）与代理。
3. **完成 OAuth / Token 登录**：
   - 运行 `start_proxy.bat` 或手动执行 `cli-proxy-api.exe` 完成账号授权。
   - 验证服务正常响应：访问 `http://127.0.0.1:8317/v1/models`。
4. **绑定至 `config.json`**：
   ```json
   {
     "codex_proxy_key": "your-proxy-key",
     "codex_exe": "C:\\path\\to\\cc-relay\\codex-proxy\\cli-proxy-api.exe",
     "codex_config": "C:\\path\\to\\cc-relay\\codex-proxy\\config.yaml"
   }
   ```
   *配置 `codex_exe` 路径后，当路由命中 Codex 且代理未启动时，cc-relay 会自动在后台静默拉起该进程。*

---

### 3.3 Gemini (Antigravity Tools) 桥接

1. 安装并按需手动启动本地 Antigravity Tools，使其监听 `8045` 端口（提供 Anthropic 兼容端点）。cc-relay 默认不会隐式拉起它；已运行的实例仍会被正常使用。Web 控制台顶栏提供独立的 Codex/Gemini 启停按钮；Gemini 停止只针对当前 cc-relay 启动并跟踪的实例，手动启动或路径不匹配的进程不会被终止。
2. 在 `config.json` 中配置：
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
3. 如需恢复隐式启动，必须显式加入：
   ```json
   {"tools": {"antigravity": {"auto_start": true}}}
   ```
   默认值为 `false`。也可以在本地 UI 通过受保护的 `/api/upstream` 启动操作明确启动，不受该开关限制。
4. 验证端点连通性：在 Web 仪表盘点击 **Gemini 探针** 或调用 `GET /api/probe?name=antigravity`。

### 3.4 加速更新 Antigravity Tools

项目提供独立更新器，避免使用 Antigravity Tools 自身的慢速下载：

```bash
python update_antigravity.py check
python update_antigravity.py download --proxy http://127.0.0.1:7890
python update_antigravity.py install --proxy http://127.0.0.1:7890
```

默认从官方 `lbjlaq/Antigravity-Manager` Release 选择 Windows x64 安装包，并校验 GitHub asset 的 SHA-256 digest。`download` 只暂存，不修改当前安装；`install` 才会启动官方安装器。若使用第三方下载加速前缀，请明确指定 `--download-base` 并确认其信任边界。

---

## 4. 客户端配置接入

### 4.1 Claude 桌面客户端 (GUI) 3P 推理网关配置（推荐）

Claude 官方桌面客户端（Claude Desktop）内置了官方 **3P（第三方推理网关 / Inference Gateway）** 模式。通过配置本地 3P 配置文件，客户端将直接将推理流量交给 `cc-relay` (8400 端口)，无需登录官方账号，也不受官方账号计费或限制。

> 📌 **重要特性**：该配置完全写入独立的用户目录（`%LOCALAPPDATA%\Claude-3p`），**与本地 Claude Code CLI 彻底隔离，绝不影响或改动终端 CLI 的既有配置**。

#### 一键启用 3P 模式：
```bash
# 自动读取 config.json 并配置 Claude Desktop 3P 网关
python apply_settings.py --desktop

# 或者预览将要写入的配置
python apply_settings.py --desktop --dry-run
```
*(Windows 用户也可直接双击运行 `apply_settings.bat`)*

写入成功后，**完全退出并重启 Claude Desktop** 客户端即可生效！
- 客户端将自动进入 3P 推理网关模式，发出的聊天请求将直连 `cc-relay` (8400 端口)。
- 可在 Web 控制台 (`http://127.0.0.1:8610`) 查看发出的请求报文。
- **说明**：3P 模式下客户端断开与 claude.ai 云端同步，所有聊天记录均妥善保存在本地设备上。

#### 一键恢复官方 1P 默认：
如果需要切回官方原生登录模式（通过 Google/Apple/邮箱登录官方账号），只需执行：
```bash
python apply_settings.py --remove-desktop
```
重启 Claude Desktop 即可恢复官方账号登录界面。

---

### 4.2 Claude Code CLI 配置注入

如果你同时希望配置终端的 Claude Code CLI 走 `cc-relay`：

#### 方式 A：自动化写入脚本（需显式指定 `--cli`）
```bash
# 预览即将写入 CLI 的环境变量（安全只读）
python apply_settings.py --cli --dry-run

# 执行安全增量合并至 ~/.claude/settings.json
python apply_settings.py --cli
```

`apply_settings.py` 会动态读取当前 `config.json` 的监听端口与 `fake_api_key`，并将以下变量合并至 `settings.json` 的 `env` 节点中，**绝不丢失或覆盖已有的其他自定义环境变量**：

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

#### 方式 B：终端临时环境变量

如果不希望更改全局配置文件，可在各终端会话中单独注入：

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

## 5. Windows 无感自动化体验 (Wrapper & Lifecycle)

为了让使用体验如丝般顺滑，项目内置了 Windows 环境下的包装器（Wrapper）与后台自动化机制：

### 5.1 设置 Wrapper 优先执行

1. 将 `C:\path\to\cc-relay\wrapper` 目录添加至系统环境变量 `PATH`，且**排在 npm 全局目录之前**。
2. 当你在任何控制台输入 `claude` 时：
   - `wrapper/claude.cmd` 会被优先调用；
   - 自动执行 `python lifecycle.py autostart` 秒级唤醒中转服务；
   - 自动拉起后台 `lifecycle.py watch` 守护进程；
   - 无缝转接执行原生的 Claude Code。
3. 当所有 `claude.exe` 进程退出后，看门狗在检测到空闲超时后会自动停止中转与 Codex 上游，释放系统资源。

### 5.2 Web 监控与控制台访问

服务启动后，随时在浏览器中打开 **[http://127.0.0.1:8610](http://127.0.0.1:8610)**，即可实时查看：
- 当前活跃路由与模型分派情况；
- 桌面客户端与 CLI 产生的每一次请求报文抓包与 Token 吞吐；
- Prompt Cache 命中率与上游健康度。

---

## 6. macOS / Linux 守护与服务化

在 POSIX 环境下，你可以使用 `systemd`（Linux）或 `launchd`（macOS）将 `cc-relay` 常驻为后台用户服务。

### 6.1 Linux Systemd 用户服务配置

创建文件 `~/.config/systemd/user/cc-relay.service`：

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

启用与启动服务：
```bash
systemctl --user daemon-reload
systemctl --user enable --now cc-relay.service
systemctl --user status cc-relay.service
```

---

## 7. 日常运维与诊断命令

| 需求场景 | 执行命令 |
| :--- | :--- |
| **前台调试启动** | `python cc_relay.py serve` |
| **前台无 UI 启动** | `python cc_relay.py serve --no-ui` |
| **查看运行时服务与进程状态** | `python lifecycle.py status` |
| **一键停止中转与所有 Sidecar**| `python lifecycle.py stopall` |
| **查看最近 10 条调用抓包摘要**| `python cc_relay.py last 10` |
| **查看流量统计分布** | `python cc_relay.py stats` |
| **测试配置注入 Dry-Run** | `python apply_settings.py --dry-run` |

---

## 8. 常见故障排查 FAQ

### Q1: 启动时报错 `[Errno 10048]` / `Address already in use` (端口被占用)
- **原因**：8400 (Relay) 或 8610 (UI) 端口被残留进程或其它应用占用。
- **解决**：
  - Windows 执行：`python lifecycle.py stopall` 强制清理残留。
  - 或在 `config.json` 中修改 `"listen_port"` 与 `"ui_port"` 为未占用端口，并重新运行 `python apply_settings.py`。

### Q2: 提示 `401 Unauthorized` 或 `invalid api key`
- **原因**：Claude Code 传入的 Token 与 `config.json` 中的 `fake_api_key` 不匹配。
- **解决**：
  - 检查 `config.json` 中的 `"fake_api_key"`（默认 `sk-relay-local-0000`）。
  - 重新运行 `python apply_settings.py` 刷新 `~/.claude/settings.json`。

### Q3: 切换到 Codex 上游后报错 `502 Bad Gateway`
- **原因**：`codex-proxy` (CLIProxyAPI) 尚未启动，或 OAuth 登录已过期。
- **解决**：
  - 检查 `http://127.0.0.1:8317/v1/models` 是否能正常访问。
  - 手动运行 `codex-proxy/start_proxy.bat` 检查是否有登录或代理报错。

### Q4: 切换到 Gemini 上游提示 `503 No available accounts`
- **原因**：该错误直接源自 Antigravity Tools 本地端点（8045），说明当前绑定的 Google 账号配额耗尽或失效。
- **解决**：打开 Antigravity Tools 客户端刷新/重新登录 Google 账号。

### Q5: 为什么在 Web UI 中看到的 Prompt 缓存命中率较低？
- **原因**：
  1. 上游模型本身未开启或不支持 Prompt Caching；
  2. 连续两次请求的 System Prompt 或历史上下文发生了剧烈变动；
  3. 注意：`cc-relay` 的 `builtin` 修剪模式已具备断点顺延算法，不会破坏缓存连续性。

### Q6: 在 Windows 终端中如何使用中文语音输入 Claude Code？
- **说明**：Windows 自带的 `Win + H` 在命令行中容易丢失焦点且标点混乱，Claude Code 官方 `/voice` 在代理模式下无法工作。
- **推荐方案**：首次使用双击根目录的 `setup_voice.bat`（自动在独立虚拟环境安装轻量依赖并下载 SenseVoice 离线模型，无需 Visual C++ 编译环境或 PyTorch），随后双击 `start_voice.bat`（或在 Web 控制台顶栏一键启动）。在任意终端或编辑器中按住热键（默认鼠标后侧键 `mouse_x1`，可在 `config.json` 或 Web 界面自定义为 `f8`、`caps_lock`）说话，松开即可自动将中文或口述指令（如“*斜杠 cost*”转为 `/cost`）秒速填入光标位置。详见 `tools/voice_input/README.md`。
