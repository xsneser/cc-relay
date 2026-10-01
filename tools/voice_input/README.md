# Claude Code CLI 中文语音输入伴侣 (SenseVoice 离线极速 / Qwen ASR 1.7B 高精度 2-Pass)

专为 **Claude Code CLI** 及各类 Windows 终端打造的**本地低延迟、开箱即用、支持随 cc-relay 自启动的对讲输入伴侣**。

---

## ⚡ 快速上手 (3 步搞定)

1. **一键环境与模型初始化**：
   - 双击项目根目录下的 **`setup_voice.bat`**；
   - 脚本将自动创建隔离虚拟环境（`tools/voice_input/.venv`）、安装轻量 CPU 依赖（无需安装 Visual C++ 14.0+），并自动下载 ~239MB 的 SenseVoice 离线模型。
2. **启动伴侣服务**：
   - 双击根目录下的 **`start_voice.bat`**；
   - *或者* 在 cc-relay Web 控制台（`http://127.0.0.1:8610`）顶栏点击「语音伴侣 -> 启动」；
   - 启动后屏幕右上角将显示免激活桌面悬浮麦克风胶囊。
3. **免复制自动输入**：
   - 在任意终端（Windows Terminal、VS Code 终端等）或文本框定位光标；
   - 按住鼠标后侧键（`mouse_x1`）说话，松开按键，文字即在毫秒内自动输入到光标处！

---

## 🌟 核心特性与架构

1. **轻量与多引擎架构**：
   - **默认引擎 (`sensevoice_offline`)**：基于 `sherpa-onnx` 纯 CPU 离线推理，资源占用极小，毫秒级转写，中英文代码术语识别准确率极高；
   - **旗舰高精引擎 (`qwen_2pass`)**：Pass 1 采用 Zipformer ONNX 毫秒级边说边出字，Pass 2 采用开源大模型 **Qwen3-ASR-1.7B** 进行全音频全局重构与终审，编程专有名词及中英混读精准度顶尖；
   - **离线高精引擎 (`qwen_offline`)**：纯 Qwen3-ASR-1.7B 单 Pass 离线高精度转写；
   - **轻量流式引擎 (`sherpa_2pass`)**：基于 sherpa-onnx Zipformer + SenseVoice 统一轻量 2-Pass 引擎。
2. **随 cc-relay 统一生命周期管理**：
   - 支持在 `config.json` 中配置 `"tools": { "voice": { "auto_start": true } }`，在中转启动时一键静默自启；
   - 采用 **Windows Job Object (`KILL_ON_JOB_CLOSE`) + `.voice.pid` 强校验**双重保障，主服务退出或异常崩溃时由操作系统内核自动回收伴侣子进程，彻底消除孤儿进程残留。
3. **Web 控制台仪表盘与在线听写集成 (`ui.html`)**：
   - 顶部 Header 增加语音伴侣状态胶囊（状态点：未启动 / 模型加载中 / 就绪 / 录音中闪烁）；
   - 一键启停控制按钮（联动 `POST /api/upstream` 调度 `name: "voice"`）；
   - 内置**在线实时流式听写抽屉**（基于浏览器 `AudioWorklet` 将原生麦克风 48kHz 音频高保真降采样为 16kHz Int16，通过 WebSocket `ws://127.0.0.1:8401/ws/voice` 实时推流边说边显）。
4. **安全无感注入**：
   - **窗口双重校验**：按键开始时锁定终端窗口句柄，转写注入前再次核对。如果在说话时切换到了浏览器或聊天窗口，会自动取消粘贴，防止误输入。
   - **剪贴板保护**：通过 Win32 Unicode 剪贴板 + `SendInput (Ctrl+V)` 模拟按键，注入后可自动恢复原有剪贴板历史。
   - **防自动提交**：转写内容仅粘贴到输入行供用户审阅确认，**绝不自动附加回车 (Enter)**。
5. **智能命令规范化 (Normalizer)**：
   - 自动将口语“**斜杠 cost**”、“**反斜杠 compact**”、“**slash review**”精确转换为 `/cost`、`/compact`、`/review`。
   - 自动去除命令末尾语音生成的中文句号（如 `git status。` -> `git status`）。

---

## ⚙️ 配置说明 (`config.json`)

在 `config.json` 的 `tools.voice` 节点可进行可视化或文件配置（Web UI 连接配置页亦支持修改保存）：

```json
{
  "tools": {
    "voice": {
      "auto_start": false,
      "hotkey": "mouse_x1",
      "engine": "sensevoice_offline",
      "port": 8401,
      "vad_mode": 2,
      "beep_feedback": false,
      "restore_clipboard": true
    }
  }
}
```

- `auto_start`: 是否随 cc-relay 自动启动伴侣服务。
- `hotkey`: 全局对讲触发键。可选：
  - `mouse_x1`：鼠标后侧键（推荐默认，大拇指顺手）
  - `mouse_x2`：鼠标前侧键
  - `f8`：键盘 F8 键
  - `caps_lock`：大写锁定键（短按切换大写，长按对讲）
- `engine`: 识别引擎模式。可选：
  - `sensevoice_offline`：SenseVoice 离线极速引擎（推荐默认，极低资源占用）
  - `qwen_2pass`：Qwen ASR 1.7B 2-Pass 流式因果识别（推荐旗舰高精度）
  - `qwen_offline`：Qwen ASR 1.7B 纯离线高精终审
  - `sherpa_2pass`：Sherpa Zipformer 2-Pass 流式轻量引擎
- `port`: 语音伴侣 RPC 与 WebSocket 监听端口（默认 8401，绑定 127.0.0.1）。

---

## 🚀 启动与使用方式

### 方式一：随 CC Relay 自动启动（推荐）
在 Web UI（`http://127.0.0.1:8610`）进入「连接配置」->「voice 语音伴侣」勾选「随 CC Relay 自启动」，或直接在顶栏控制区点击语音伴侣的「启动」按钮。伴侣启动后会自动在屏幕右上角唤出**免激活桌面悬浮麦克风胶囊**。

### 方式二：手动运行批处理
双击运行根目录下的 `start_voice.bat`。

### 💡 核心体验：客户端免复制自动输入
1. 在任何软件（Windows Terminal、VS Code、记事本、浏览器、微信等）中将输入光标定位到需要打字的位置；
2. **操作方式 A（鼠标点击胶囊）**：
   - 鼠标单击屏幕上的**桌面悬浮麦克风胶囊**（基于 Win32 `WS_EX_NOACTIVATE` 机制，点击绝对不会抢夺原窗口的光标焦点！）；
   - 对着麦克风说话，胶囊动态展示音量波形并**实时滚动出字预览 (Partial)**；
   - 说话停止约 1.2 秒（VAD 自动截断）或再次点击胶囊，伴侣毫秒级完成 2-Pass 精细纠错与标点，**自动将文字输入到光标处**，全程无需任何复制粘贴！
3. **操作方式 B（全局热键对讲）**：
   - 按住鼠标侧键（X1 / 后退键）或键盘 CapsLock 不放说话；
   - 松开按键，文字立即自动输入到终端光标处。

### 在 Web UI 中在线测试听写
1. 打开 `http://127.0.0.1:8610`；
2. 点击右上角「实时听写」按钮；
3. 点击「开始说话」，浏览器将通过 AudioWorklet 采集音频实时推流，界面展示边说边出字效果。

---

## 🛠️ CLI 诊断与工具命令

在项目根目录下可使用命令行进行诊断与单项测试：

```bash
# 1. 运行系统全项诊断 (检查依赖、模型、麦克风状态)
python -m tools.voice_input doctor

# 2. 列出系统可用麦克风设备
python -m tools.voice_input devices

# 3. 测试文本规则转换效果
python -m tools.voice_input normalize "斜杠 cost。"
# 输出: /cost

# 4. 手动启动全功能语音伴侣服务
python -m tools.voice_input service --hotkey mouse_x1 --port 8401
```

---

## 常见问题 (FAQ)

**Q: 浏览器在线听写提示“麦克风受安全上下文限制”？**
A: 浏览器的 `navigator.mediaDevices.getUserMedia` API 要求必须在 Secure Context（安全上下文）下才能调用麦克风。请务必使用 `http://127.0.0.1:8610` 或 `http://localhost:8610` 访问，避免使用局域网 IP（如 `http://192.168.x.x`）访问。

**Q: 为什么按住热键说话后终端没有任何反应？**
A: 请运行 `python -m tools.voice_input doctor` 检查：
1. 麦克风设备是否被系统权限禁用；
2. 终端窗口是否以管理员（Administrator）权限运行，而语音伴侣以普通权限运行（Windows 完整性级别限制跨权限模拟按键）。如果是，请以相同权限级别运行伴侣程序。

**Q: 录音期间切走窗口会怎样？**
A: 伴侣内置前台焦点句柄校验，若检测到窗口在录音/转写期间切换，会主动放弃粘贴，绝不向错误窗口误发内容。
