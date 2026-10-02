# cc-relay 实操录屏与实机演示操作指南

本指南供需要配合成片进行实机演示录屏、直播展示或二次剪辑的开发者使用。
已验证所有命令与 cc-relay 当前端口（8400、8610、8401）及最新配置项 100% 吻合。

---

## 1. 录屏环境与软件推荐配置

- **录屏软件**：OBS Studio 或 Windows 自带屏幕录制 (Win + Alt + R) / 剪映专业版
- **画面分辨率**：1920 x 1080 (16:9) 或 3840 x 2160 (4K)
- **帧率**：60 fps（优先）或 30 fps
- **终端推荐**：Windows Terminal + PowerShell 7 / Git Bash
  - 主题：One Dark / Dracula / Night Owl
  - 字体：JetBrains Mono / Cascadia Code，字号 14-16pt
- **浏览器**：Chrome / Edge 全屏访问 `http://127.0.0.1:8610`

---

## 2. 5大核心场景实操步骤

### 场景一：Subagent 5档模型动态智能分配实机抓包

**目的**：演示当终端派生 Plan 或 Explore 代理时，请求如何被分类并路由至不同上游。

1. **启动中转与仪表盘**：
   ```bash
   python cc_relay.py
   ```
2. **打开 Web 控制台**：
   浏览器访问 `http://127.0.0.1:8610`，在顶部选择「混合分流 (Hybrid)」模式；
   展示 5 档卡片：
   - `main`: 设置为 `deepseek-chat`
   - `agent` (Plan代理): 设置为 `codex / gpt-5.6-sol`，思考强度调至 `high`
   - `opus` (Explore代理): 设置为 `codex / gpt-5.6`
   - `fast` (命名代理): 设置为 `claude-3-5-haiku`
3. **在终端发起任务**：
   ```bash
   claude "为整个项目编写详细的架构规划并搜索全部测试用例"
   ```
4. **观察仪表盘抓包**：
   - 控制台「实时调用记录」中，规划请求的 `Role` 显式标记为 `agent`，目标模型为 `gpt-5.6-sol`；
   - 搜索请求的 `Role` 显式标记为 `opus`；
   - 对话结束后命名请求标记为 `fast`；
   - 直观印证不同子任务各司其职，算力精准分配。

---

### 场景二：流量暂停闸门 (Traffic Pause Gate) 与 SSE 保活

**目的**：展示中途换模型/改 Key 时，终端绝不断连或超时的黑科技。

1. **在终端中向 Claude Code 发送复杂长任务**；
2. **在请求刚发出时，立刻在 Web 控制台右上角点击「暂停流量 (Pause Traffic)」**：
   - 顶部状态变为橙色警报：`PAUSED (1 Waiting)`；
3. **切换到终端观察**：
   - 终端并未崩溃，光标静止等待（因为网关正在每 3 秒发送一次 `event: ping` SSE 心跳 chunk）；
4. **在控制台中切换上游模型**（例如将主模型切换为 Gemini 2.5 Pro）；
5. **点击「恢复通行 (Resume Traffic)」**：
   - 状态瞬间恢复为绿色 `RUNNING`；
   - 终端瞬间收到响应并开始流畅打字输出！

---

### 场景三：CLI 语音对讲伴侣实操 (免激活悬浮胶囊)

**目的**：展示不用鼠标切窗口、不抢焦点、按住侧键说话即输入的极客体验。

1. **启动语音伴侣**：
   ```bash
   # 双击根目录 start_voice.bat
   # 或在 Web 控制台顶栏点击「启动伴侣」
   ```
2. **观察桌面**：
   - 屏幕右上角出现半透明圆角麦克风胶囊；
3. **无感对讲输入**：
   - 将光标定位在 Windows Terminal 内；
   - **大拇指按住鼠标后侧键 (`mouse_x1`)**，对麦克风清晰说道：
     > “斜杠 cost 以及查看当前 git status”
   - 松开鼠标按键；
4. **观察效果**：
   - 麦克风胶囊呈现动态绿色音频波峰；
   - 终端光标处瞬间输入：`/cost 以及查看当前 git status`；
   - 口播“斜杠 cost”自动转换为 `/cost`，末尾中文标点自动被清除；
   - 终端光标始终保持活跃，焦点完全没有丢失！

---

### 场景四：CC 指纹剥离与 Gemini (Antigravity Tools) 完美适配

**目的**：展示原/净/自三态修剪与 Prompt Cache 断点无损继承。

1. 在 Web 控制台「修剪器」选项中切换为「净 (Builtin)」模式；
2. 将主模型设置为 `gemini-2.5-pro`（通过 Antigravity Tools 端口 8045）；
3. 在终端发送一条带有复杂业务规则的长 Prompt；
4. 在控制台查看「请求详情」：
   - 原文中的 `You are Claude Code...` 与 `x-anthropic-billing-header` 已被净模式彻底滤除；
   - 原文被删块上的 `cache_control`（`ephemeral`）被精准迁移到下一个幸存文本块；
   - 下游 Gemini 响应速度极快，Prompt Cache 命中率维持在 90% 以上。

---

### 场景五：一键配置双端 (CLI 与 Claude Desktop)

**目的**：展示小白式极速部署。

1. 打开新终端输入：
   ```bash
   python apply_settings.py --all
   ```
2. 控制台输出：
   - `Claude Code CLI: 已配置 ANTHROPIC_BASE_URL="http://127.0.0.1:8400"`
   - `Claude Desktop: 配置文件已自动注入 (3P Gateway)`
3. 分别启动 Claude Desktop 桌面客户端与终端 CLI，两端均已无缝纳管于 cc-relay 中枢之下。
