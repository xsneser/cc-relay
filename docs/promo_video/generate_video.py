"""
cc-relay 宣传视频全自动渲染引擎
生成 1080p 30fps 高清成片 (含 AI 旁白配音、中文字幕、动效卡片、终端模拟)
"""
import os
import sys
import json
import math
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FFMPEG_PATH = r"C:\Users\lenovo\AppData\Roaming\bilibili\ffmpeg\ffmpeg.exe"
AUDIO_DIR = "docs/promo_video/audio"
OUTPUT_VIDEO_PATH = "docs/promo_video/cc_relay_promo.mp4"
TEMP_RAW_VIDEO = "docs/promo_video/temp_raw.mp4"

WIDTH, HEIGHT = 1920, 1080
FPS = 30

# 载入中文字体
FONT_PATH_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_PATH_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_PATH_MONO = r"C:\Windows\Fonts\consola.ttf"

def get_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

FONT_TITLE = get_font(FONT_PATH_BOLD, 36)
FONT_SUBTITLE = get_font(FONT_PATH_BOLD, 26)
FONT_BODY = get_font(FONT_PATH_REG, 22)
FONT_BODY_BOLD = get_font(FONT_PATH_BOLD, 22)
FONT_SMALL = get_font(FONT_PATH_REG, 17)
FONT_MONO = get_font(FONT_PATH_MONO, 19)
FONT_MONO_SMALL = get_font(FONT_PATH_MONO, 15)
FONT_SUB_TEXT = get_font(FONT_PATH_BOLD, 31)

# 配色盘 (Dark Cyber Glassmorphism)
BG_TOP = (8, 12, 22)
BG_BOTTOM = (12, 20, 36)
ACCENT_CYAN = (0, 240, 255)
ACCENT_VIOLET = (147, 92, 255)
ACCENT_GREEN = (16, 185, 129)
ACCENT_AMBER = (245, 158, 11)
ACCENT_RED = (244, 63, 94)
TEXT_WHITE = (248, 250, 252)
TEXT_MUTED = (148, 163, 184)
CARD_BG = (15, 23, 42, 235)
CARD_BORDER = (40, 60, 95)

def draw_gradient_background(draw, t):
    # 绘制高质感微光背景
    for y in range(0, HEIGHT, 4):
        ratio = y / HEIGHT
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.rectangle([0, y, WIDTH, y + 4], fill=(r, g, b))

    # 科技感网格微光点
    grid_spacing = 80
    dot_color = (25, 40, 70)
    for x in range(40, WIDTH, grid_spacing):
        for y in range(40, HEIGHT, grid_spacing):
            draw.point((x, y), fill=dot_color)

def draw_top_nav(draw, act_num, act_title, t):
    # 顶栏装饰
    draw.rectangle([0, 0, WIDTH, 64], fill=(10, 15, 28, 220))
    draw.line([0, 64, WIDTH, 64], fill=(30, 45, 75), width=1)

    # Logo
    logo_pulse = int(220 + 35 * math.sin(t * 4))
    draw.text((60, 16), "⚡ cc-relay", font=FONT_TITLE, fill=(0, logo_pulse, 255))
    draw.text((250, 23), "Claude Code 本地流量调度与特征修剪中枢", font=FONT_SMALL, fill=TEXT_MUTED)

    # Act 胶囊
    act_text = f"ACT 0{act_num} · {act_title}"
    act_bbox = draw.textbbox((0, 0), act_text, font=FONT_SUBTITLE)
    act_w = act_bbox[2] - act_bbox[0] + 36
    act_x = (WIDTH - act_w) // 2
    draw.rounded_rectangle([act_x, 14, act_x + act_w, 50], radius=18, fill=(20, 30, 55), outline=ACCENT_CYAN, width=1)
    draw.text((act_x + 18, 18), act_text, font=FONT_SUBTITLE, fill=ACCENT_CYAN)

    # 右侧状态
    status_x = WIDTH - 380
    draw.text((status_x, 23), "PORT: 8400", font=FONT_MONO_SMALL, fill=ACCENT_GREEN)
    draw.text((status_x + 120, 23), "WEB: 8610", font=FONT_MONO_SMALL, fill=ACCENT_CYAN)
    draw.ellipse([status_x + 230, 27, status_x + 240, 37], fill=ACCENT_GREEN)
    draw.text((status_x + 250, 22), "ONLINE", font=FONT_MONO_SMALL, fill=TEXT_WHITE)

def draw_subtitle_bar(draw, text, t):
    if not text:
        return
    # 底部发光字幕胶囊
    sub_bbox = draw.textbbox((0, 0), text, font=FONT_SUB_TEXT)
    sub_w = sub_bbox[2] - sub_bbox[0]
    box_w = max(sub_w + 100, 700)
    box_x = (WIDTH - box_w) // 2
    box_y = HEIGHT - 110
    box_h = 68

    # 半透明磨砂底框
    draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=34, fill=(12, 18, 32, 230), outline=(0, 200, 240, 180), width=2)

    # 模拟麦克风活动声波
    wave_x = box_x + 30
    for i in range(5):
        h = int(10 + 16 * abs(math.sin(t * 8 + i * 0.8)))
        draw.line([wave_x + i * 8, box_y + 34 - h // 2, wave_x + i * 8, box_y + 34 + h // 2], fill=ACCENT_CYAN, width=3)

    # 字幕文本
    text_x = box_x + 85
    draw.text((text_x, box_y + 14), text, font=FONT_SUB_TEXT, fill=TEXT_WHITE)

def draw_card(draw, rect, title, border_color=CARD_BORDER, bg_color=(15, 23, 42, 220)):
    x1, y1, x2, y2 = rect
    draw.rounded_rectangle([x1, y1, x2, y2], radius=14, fill=bg_color, outline=border_color, width=2)
    if title:
        draw.text((x1 + 24, y1 + 18), title, font=FONT_SUBTITLE, fill=TEXT_WHITE)
        draw.line([x1 + 20, y1 + 56, x2 - 20, y1 + 56], fill=(35, 50, 80), width=1)

# ==================== 各场景渲染逻辑 ====================

def render_act_1(draw, progress, t, sentence_idx):
    # Scene 1: 痛点引入与架构概览
    # 左侧：Claude Code 疯狂派生 Subagent 终端
    draw_card(draw, [100, 100, 980, 890], "终端模拟 · Claude Code 运行状态", border_color=(45, 65, 105))

    # 终端模拟内容
    term_lines = [
        ("you@devbox:~/project$ ", TEXT_MUTED),
        ("claude \"重构整个鉴权模块并遍历全部 50 个文件\"", ACCENT_CYAN),
        ("", TEXT_WHITE),
        ("╭── Claude Code CLI v2.0 ─────────────────────────────╮", (70, 95, 140)),
        ("│  [Subagent 启动] 深度遍历代码库并设计实施方案...      │", TEXT_WHITE),
        ("│   > Agent: Explore (文件搜索) ... 消耗 42,000 Tokens│", ACCENT_AMBER),
        ("│   > Agent: Plan    (架构规划) ... 消耗 38,500 Tokens│", ACCENT_AMBER),
        ("│   > Agent: Guide   (安全审查) ... 消耗 15,200 Tokens│", ACCENT_AMBER),
        ("│   > Agent: Naming  (会话命名) ... 消耗  1,200 Tokens│", ACCENT_AMBER),
        ("│                                                     │", TEXT_WHITE),
        ("│  [警报] 当前会话累计 Token 消耗已达 96,900 Tokens!   │", ACCENT_RED),
        ("╰─────────────────────────────────────────────────────╯", (70, 95, 140)),
        ("", TEXT_WHITE),
        ("正在等待主模型返回下一轮响应...", TEXT_MUTED)
    ]

    ty = 180
    visible_lines = min(len(term_lines), int(3 + progress * (len(term_lines) - 2)))
    for line, color in term_lines[:visible_lines]:
        draw.text((130, ty), line, font=FONT_MONO, fill=color)
        ty += 34

    # 光标闪烁
    if int(t * 3) % 2 == 0:
        draw.rectangle([130 + len("正在等待主模型返回下一轮响应...") * 11, ty - 32, 130 + len("正在等待主模型返回下一轮响应...") * 11 + 10, ty - 10], fill=ACCENT_CYAN)

    # 右侧上卡片：额度刺客警报
    draw_card(draw, [1030, 100, 1820, 470], "⚠️ 痛点一：Subagent 额度消耗失控", border_color=ACCENT_RED)
    draw.text((1060, 180), "所有后台子任务默认全走顶级主模型", font=FONT_BODY_BOLD, fill=ACCENT_RED)
    draw.text((1060, 220), "• 架构规划、文件搜索、简单审查无差别调度", font=FONT_BODY, fill=TEXT_WHITE)
    draw.text((1060, 260), "• 每次后台遍历触发数十万 Token 计费", font=FONT_BODY, fill=TEXT_WHITE)
    draw.text((1060, 300), "• 个人与团队月度 API 配额极速见底", font=FONT_BODY, fill=TEXT_WHITE)

    # 进度条
    gauge_fill = min(1.0, 0.4 + progress * 0.58)
    draw.rounded_rectangle([1060, 370, 1780, 400], radius=8, fill=(30, 40, 60))
    bar_w = int((1780 - 1060) * gauge_fill)
    draw.rounded_rectangle([1060, 370, 1060 + bar_w, 400], radius=8, fill=ACCENT_RED)
    draw.text((1060, 415), f"月度额度消耗率: {int(gauge_fill * 100)}% (额度告急)", font=FONT_SMALL, fill=ACCENT_RED)

    # 右侧下卡片：第三方拦截
    draw_card(draw, [1030, 500, 1820, 890], "❌ 痛点二：第三方模型因指纹遭遇拦截", border_color=ACCENT_AMBER)
    draw.text((1060, 580), "HTTP 403 / 400 协议解析与身份校验失败", font=FONT_BODY_BOLD, fill=ACCENT_AMBER)

    # 模拟错误代码块
    draw.rounded_rectangle([1060, 630, 1780, 830], radius=8, fill=(8, 12, 20), outline=(60, 40, 40), width=1)
    err_text = [
        "POST https://api.upstream.com/v1/messages",
        "Header: x-anthropic-billing-header: cc_is_subagent=true",
        "System: \"You are Claude Code, Anthropic's official CLI...\"",
        "--> Response: 403 Forbidden",
        "--> Error: \"Unknown billing header or client identity rejected\""
    ]
    ey = 650
    for eline in err_text:
        c = ACCENT_RED if "403" in eline or "Error" in eline else TEXT_MUTED
        draw.text((1080, ey), eline, font=FONT_MONO_SMALL, fill=c)
        ey += 32

def render_act_2(draw, progress, t, sentence_idx):
    # Scene 2: Subagent 5档模型动态智能分配
    draw.text((100, 85), "cc-relay 5-Tier 角色识别与智能路由矩阵", font=FONT_TITLE, fill=ACCENT_CYAN)
    draw.text((100, 130), "自动深度解析 System Prompt 与请求头特征 · 针对性分派最适配模型", font=FONT_BODY, fill=TEXT_MUTED)

    tiers = [
        {
            "tier": "main",
            "name": "主交互模型",
            "icon": "💬",
            "desc": "常规命令行对话",
            "model": "deepseek-chat / flash",
            "gear": "Low Effort",
            "metric": "极速响应 · 极致性价比",
            "color": ACCENT_GREEN
        },
        {
            "tier": "agent",
            "name": "Plan 代理",
            "icon": "📐",
            "desc": "架构长远规划",
            "model": "codex / gpt-5.6-sol",
            "gear": "High (满血推理)",
            "metric": "深度推理 · 架构设计",
            "color": ACCENT_VIOLET
        },
        {
            "tier": "opus",
            "name": "Explore 代理",
            "icon": "🔍",
            "desc": "全库文件探索",
            "model": "codex / gpt-5.6",
            "gear": "Medium Effort",
            "metric": "广域搜索 · 精准定位",
            "color": ACCENT_CYAN
        },
        {
            "tier": "sonnet",
            "name": "审查 / Guide 代理",
            "icon": "🛡️",
            "desc": "代码审查与指引",
            "model": "claude-3-7-sonnet",
            "gear": "Low Effort",
            "metric": "质量把关 · 稳健可靠",
            "color": (56, 189, 248)
        },
        {
            "tier": "fast",
            "name": "会话命名代理",
            "icon": "⚡",
            "desc": "提取 2-5 词会话名",
            "model": "claude-3-5-haiku",
            "gear": "Instant",
            "metric": "微型秒出 · 零额度消耗",
            "color": ACCENT_AMBER
        }
    ]

    card_w = 320
    card_gap = 25
    start_x = 100
    cy = 180
    card_h = 630

    for i, tr in enumerate(tiers):
        cx = start_x + i * (card_w + card_gap)

        # 激活高亮动效
        active = (i == int(progress * 5) % 5)
        border_col = tr["color"] if active else CARD_BORDER
        card_bg = (20, 32, 58, 240) if active else CARD_BG

        draw.rounded_rectangle([cx, cy, cx + card_w, cy + card_h], radius=14, fill=card_bg, outline=border_col, width=3 if active else 1)

        # 顶部角标
        draw.rounded_rectangle([cx + 15, cy + 20, cx + 110, cy + 50], radius=8, fill=(10, 16, 30))
        draw.text((cx + 25, cy + 24), tr["tier"].upper(), font=FONT_MONO_SMALL, fill=tr["color"])

        # 图标与名称
        draw.text((cx + 120, cy + 22), tr["icon"], font=FONT_BODY, fill=TEXT_WHITE)
        draw.text((cx + 20, cy + 70), tr["name"], font=FONT_SUBTITLE, fill=TEXT_WHITE)
        draw.text((cx + 20, cy + 110), tr["desc"], font=FONT_SMALL, fill=TEXT_MUTED)

        draw.line([cx + 20, cy + 145, cx + card_w - 20, cy + 145], fill=(35, 50, 80), width=1)

        # 绑定模型卡片
        draw.text((cx + 20, cy + 165), "映射上游模型:", font=FONT_SMALL, fill=TEXT_MUTED)
        draw.rounded_rectangle([cx + 20, cy + 195, cx + card_w - 20, cy + 250], radius=8, fill=(10, 16, 28), outline=tr["color"], width=1)
        draw.text((cx + 30, cy + 210), tr["model"], font=FONT_MONO_SMALL, fill=TEXT_WHITE)

        # 推理强度档位 (Reasoning Gear)
        draw.text((cx + 20, cy + 275), "思考预算 (Reasoning Gear):", font=FONT_SMALL, fill=TEXT_MUTED)
        draw.rounded_rectangle([cx + 20, cy + 305, cx + card_w - 20, cy + 355], radius=8, fill=(12, 20, 36))
        draw.text((cx + 30, cy + 320), f"⚡ {tr['gear']}", font=FONT_BODY_BOLD, fill=tr["color"])

        # 核心优势
        draw.text((cx + 20, cy + 380), "算力匹配收益:", font=FONT_SMALL, fill=TEXT_MUTED)
        draw.text((cx + 20, cy + 415), tr["metric"], font=FONT_BODY, fill=TEXT_WHITE)

        # 动态流量流向光标
        if active:
            draw.text((cx + 20, cy + 560), "▶ 实时数据流调度中...", font=FONT_BODY_BOLD, fill=tr["color"])
            # 光粒子
            for p in range(4):
                px = cx + 40 + p * 60
                py = cy + 595
                draw.ellipse([px, py, px + 10, py + 10], fill=tr["color"])
        else:
            draw.text((cx + 20, cy + 560), "✔ 规则监听就绪", font=FONT_SMALL, fill=TEXT_MUTED)

    # 底部总结横幅
    draw.rounded_rectangle([100, 840, 1820, 895], radius=10, fill=(14, 22, 40), outline=ACCENT_CYAN, width=1)
    draw.text((140, 855), "💡 价值亮点: 重算力精准投喂架构规划，轻算力支撑高频辅助，综合调用成本直降 70%！", font=FONT_BODY_BOLD, fill=ACCENT_CYAN)

def render_act_3(draw, progress, t, sentence_idx):
    # Scene 3: 本地控制枢纽 · 零依赖网关与流量暂停闸门
    draw.text((100, 85), "零依赖本地网关 & 独创流量暂停闸门", font=FONT_TITLE, fill=ACCENT_CYAN)
    draw.text((100, 130), "100% Python 标准库构建 · 零 pip 外部依赖 · 微秒级审计 · SSE 心跳防断连", font=FONT_BODY, fill=TEXT_MUTED)

    # 左侧：网关拓扑与审计仪表盘
    draw_card(draw, [100, 180, 880, 880], "本地透明代理与数据包审计", border_color=(45, 65, 105))

    # 端口信息
    draw.text((130, 260), "代理监听端口: http://127.0.0.1:8400 (Anthropic API)", font=FONT_BODY, fill=TEXT_WHITE)
    draw.text((130, 305), "控制台仪表盘: http://127.0.0.1:8610 (REST API / UI)", font=FONT_BODY, fill=TEXT_WHITE)

    draw.line([130, 360, 850, 360], fill=(35, 50, 80), width=1)

    # 4 项指标方块
    metrics = [
        ("吞吐速率", f"{int(78 + 12 * math.sin(t*3))} Tokens/s", ACCENT_GREEN),
        ("转发延迟", "11.4 ms", ACCENT_CYAN),
        ("Prompt Cache", "92.8% 命中", ACCENT_VIOLET),
        ("外部依赖", "0 (标准库)", ACCENT_AMBER)
    ]
    for mi, (mlabel, mval, mcol) in enumerate(metrics):
        mx = 130 + (mi % 2) * 360
        my = 390 + (mi // 2) * 140
        draw.rounded_rectangle([mx, my, mx + 330, my + 110], radius=10, fill=(10, 16, 28), outline=(35, 50, 80), width=1)
        draw.text((mx + 20, my + 18), mlabel, font=FONT_SMALL, fill=TEXT_MUTED)
        draw.text((mx + 20, my + 52), mval, font=FONT_SUBTITLE, fill=mcol)

    draw.rounded_rectangle([130, 710, 850, 830], radius=10, fill=(10, 16, 30))
    draw.text((150, 730), "✔ 纯标准库 ExclusiveThreadingHTTPServer", font=FONT_BODY, fill=ACCENT_GREEN)
    draw.text((150, 770), "✔ 独占端口绑定，避免 Windows 孤儿进程冲突", font=FONT_BODY, fill=TEXT_WHITE)

    # 右侧：流量暂停闸门核心演示
    draw_card(draw, [920, 180, 1820, 880], "独创特色：流量暂停闸门 (Traffic Pause Gate)", border_color=ACCENT_AMBER)

    # 模拟暂停切换状态
    paused = (int(t * 0.8) % 2 == 1)
    status_box_col = ACCENT_AMBER if paused else ACCENT_GREEN
    status_text = "PAUSED (1 请求挂起等待中)" if paused else "NORMAL (流量正常通行)"

    draw.rounded_rectangle([960, 260, 1780, 330], radius=10, fill=(10, 16, 28), outline=status_box_col, width=2)
    draw.ellipse([990, 285, 1010, 305], fill=status_box_col)
    draw.text((1030, 280), f"闸门状态: {status_text}", font=FONT_SUBTITLE, fill=status_box_col)

    # 核心心跳波形演示
    draw.text((960, 360), "SSE 心跳保活模拟机制 (Keep-Alive):", font=FONT_BODY_BOLD, fill=TEXT_WHITE)
    draw.text((960, 395), "请求暂停期间，网关每 3 秒主动向 CLI 客户端发送 SSE ping 数据块", font=FONT_BODY, fill=TEXT_MUTED)

    # 心跳波形盒子
    draw.rounded_rectangle([960, 440, 1780, 600], radius=10, fill=(8, 12, 20), outline=(40, 55, 85), width=1)

    # 绘制脉冲心跳
    hw_y = 520
    draw.line([980, hw_y, 1100, hw_y], fill=(40, 70, 100), width=2)
    # 脉冲峰
    pulse_phase = (t * 4) % 4
    for px in range(1100, 1760, 140):
        draw.line([px, hw_y, px + 20, hw_y - 45], fill=ACCENT_CYAN, width=3)
        draw.line([px + 20, hw_y - 45, px + 40, hw_y + 45], fill=ACCENT_CYAN, width=3)
        draw.line([px + 40, hw_y + 45, px + 60, hw_y], fill=ACCENT_CYAN, width=3)
        draw.line([px + 60, hw_y, px + 140, hw_y], fill=(40, 70, 100), width=2)

    draw.text((980, 620), "代码级传输示范:", font=FONT_SMALL, fill=TEXT_MUTED)
    draw.text((980, 650), "event: ping\\ndata: {\"type\": \"ping\"}\\n\\n  (保持 HTTP 200 chunked 通道活跃)", font=FONT_MONO, fill=ACCENT_CYAN)

    draw.rounded_rectangle([960, 710, 1780, 830], radius=10, fill=(14, 24, 42), outline=ACCENT_GREEN, width=1)
    draw.text((980, 735), "⚡ 开发者收益: 任务中途随意切模型、改 API Key、查报文", font=FONT_BODY_BOLD, fill=ACCENT_GREEN)
    draw.text((980, 775), "终端绝不报错超时，修改后一键 Resume，立即唤醒继续流式输出！", font=FONT_BODY, fill=TEXT_WHITE)

def render_act_4(draw, progress, t, sentence_idx):
    # Scene 4: CLI 语音输入伴侣
    draw.text((100, 85), "CLI 语音输入伴侣 (Voice Input Companion)", font=FONT_TITLE, fill=ACCENT_CYAN)
    draw.text((100, 130), "Win32 免激活悬浮胶囊 · 鼠标侧键推讲 · 智能命令规范化 · 纯 CPU 离线毫秒级", font=FONT_BODY, fill=TEXT_MUTED)

    # 左侧：实录终端交互仿真
    draw_card(draw, [100, 180, 1000, 880], "Windows Terminal · 语音免复制直接输入", border_color=(45, 65, 105))

    # 模拟对讲触发按键
    draw.rounded_rectangle([130, 260, 970, 320], radius=8, fill=(20, 40, 70), outline=ACCENT_CYAN, width=2)
    draw.text((150, 275), "🖱️ 鼠标后侧键 [mouse_x1] · 按住推讲中 (Push-to-Talk)", font=FONT_BODY_BOLD, fill=ACCENT_CYAN)

    draw.text((130, 360), "you@workstation:~/cc-relay$ ", font=FONT_MONO, fill=TEXT_MUTED)

    spoken_text = "斜杠 cost 以及查看当前 git status"
    typed_cmd = "/cost 以及查看当前 git status"

    draw.text((130, 410), f"实时语音转写: \"{spoken_text}\"", font=FONT_BODY, fill=ACCENT_AMBER)

    # 自动命令规范化对比
    draw.rounded_rectangle([130, 470, 970, 620], radius=10, fill=(8, 14, 24), outline=(40, 60, 95), width=1)
    draw.text((150, 490), "智能规范化 (Normalizer) 纠偏:", font=FONT_SMALL, fill=TEXT_MUTED)
    draw.text((150, 525), "• 口播 \"斜杠 cost\"  ==>  自动转为标准命令 /cost", font=FONT_BODY_BOLD, fill=ACCENT_GREEN)
    draw.text((150, 565), "• 自动去除末尾语音识别产生的中文标点句号", font=FONT_BODY, fill=TEXT_WHITE)

    # 注入结果
    draw.text((130, 660), "终端最终直接上屏 (无感注入光标处):", font=FONT_SMALL, fill=TEXT_MUTED)
    draw.rounded_rectangle([130, 695, 970, 765], radius=8, fill=(10, 20, 35), outline=ACCENT_GREEN, width=1)
    draw.text((150, 715), f"$ {typed_cmd}", font=FONT_MONO, fill=ACCENT_GREEN)

    # 安全保障标签
    draw.text((130, 800), "✔ 绝不自动回车 (保留用户回车确认权)  ✔ 自动恢复原系统剪贴板", font=FONT_SMALL, fill=TEXT_MUTED)

    # 右侧：免激活悬浮胶囊特写
    draw_card(draw, [1040, 180, 1820, 880], "Win32 免激活悬浮胶囊 (Desktop Capsule)", border_color=ACCENT_CYAN)

    # 悬浮麦克风胶囊拟态
    cap_x, cap_y, cap_w, cap_h = 1180, 270, 500, 130
    draw.rounded_rectangle([cap_x, cap_y, cap_x + cap_w, cap_y + cap_h], radius=65, fill=(12, 20, 36, 240), outline=ACCENT_CYAN, width=3)

    # 麦克风图标
    draw.ellipse([cap_x + 30, cap_y + 35, cap_x + 90, cap_y + 95], fill=(20, 35, 60))
    draw.text((cap_x + 48, cap_y + 48), "🎙️", font=FONT_SUBTITLE, fill=ACCENT_CYAN)

    # 实时波形
    wave_base_x = cap_x + 130
    for wi in range(16):
        wh = int(12 + 35 * abs(math.sin(t * 10 + wi * 0.6)))
        draw.line([wave_base_x + wi * 18, cap_y + 65 - wh // 2, wave_base_x + wi * 18, cap_y + 65 + wh // 2], fill=ACCENT_GREEN, width=4)

    draw.text((cap_x + 130, cap_y + 95), "REC · SenseVoice INT8 (<200ms)", font=FONT_MONO_SMALL, fill=ACCENT_GREEN)

    # 架构技术细节
    tech_items = [
        ("WS_EX_NOACTIVATE 免激活", "点击或唤醒绝不抢占终端或 VS Code 焦点"),
        ("窗口句柄双重核对", "若说话时切走窗口则自动取消，杜绝误输入"),
        ("SenseVoice 纯 CPU 离线", "239MB 小模型毫秒级推理，无需 GPU/CUDA"),
        ("Qwen 2-Pass 旗舰大模型", "Zipformer 流式预览 + Qwen3-ASR 全文重构")
    ]
    ty = 440
    for tit, dsc in tech_items:
        draw.rounded_rectangle([1080, ty, 1780, ty + 85], radius=10, fill=(12, 18, 30), outline=(35, 50, 75), width=1)
        draw.text((1110, ty + 15), f"⭐ {tit}", font=FONT_BODY_BOLD, fill=ACCENT_CYAN)
        draw.text((1110, ty + 48), dsc, font=FONT_SMALL, fill=TEXT_MUTED)
        ty += 105

def render_act_5(draw, progress, t, sentence_idx):
    # Scene 5: CC 指纹剥离与 Prompt Cache 断点顺延
    draw.text((100, 85), "CC 指纹剥离 & Prompt Cache 断点无损顺延", font=FONT_TITLE, fill=ACCENT_CYAN)
    draw.text((100, 130), "原/净/自三态修剪 · 兼容 Gemini & DeepSeek · 独创断点继承算法保住 90% 缓存折扣", font=FONT_BODY, fill=TEXT_MUTED)

    # 模式切换选择器
    draw.text((100, 180), "修剪器运行模式 (Modifier Mode):", font=FONT_BODY_BOLD, fill=TEXT_WHITE)
    modes = [("原 (Original)", False), ("净 (Builtin · 推荐)", True), ("自 (Custom)", False)]
    for mi, (mname, is_act) in enumerate(modes):
        bx = 420 + mi * 220
        bcol = ACCENT_GREEN if is_act else (30, 45, 70)
        draw.rounded_rectangle([bx, 175, bx + 190, 220], radius=8, fill=(15, 25, 45), outline=bcol, width=2 if is_act else 1)
        draw.text((bx + 20, 185), mname, font=FONT_BODY, fill=ACCENT_GREEN if is_act else TEXT_MUTED)

    # 左侧：原始载荷 (Raw Payload with CC Fingerprints)
    draw_card(draw, [100, 250, 880, 880], "原始载荷 (携带 CC 官方指纹)", border_color=ACCENT_RED)

    draw.text((130, 330), "system: [", font=FONT_MONO, fill=TEXT_MUTED)

    # 被删除的行 (红底删除线)
    del_box_1 = [130, 365, 850, 435]
    draw.rounded_rectangle(del_box_1, radius=6, fill=(40, 15, 20), outline=ACCENT_RED, width=1)
    draw.text((145, 375), "❌ { \"text\": \"You are Claude Code,", font=FONT_MONO_SMALL, fill=ACCENT_RED)
    draw.text((145, 400), "       Anthropic's official CLI...\" }", font=FONT_MONO_SMALL, fill=ACCENT_RED)

    del_box_2 = [130, 455, 850, 525]
    draw.rounded_rectangle(del_box_2, radius=6, fill=(40, 15, 20), outline=ACCENT_RED, width=1)
    draw.text((145, 465), "❌ { \"text\": \"x-anthropic-billing-header:", font=FONT_MONO_SMALL, fill=ACCENT_RED)
    draw.text((145, 490), "       cc_is_subagent=true\" }", font=FONT_MONO_SMALL, fill=ACCENT_RED)

    # 幸存文本块
    surv_box = [130, 545, 850, 680]
    draw.rounded_rectangle(surv_box, radius=6, fill=(10, 20, 35), outline=(40, 60, 90), width=1)
    draw.text((145, 560), "✔ { \"text\": \"项目工程规范与业务规则...\":", font=FONT_MONO_SMALL, fill=TEXT_WHITE)
    draw.text((145, 595), "     // 该块原先没有 cache_control 断点", font=FONT_MONO_SMALL, fill=TEXT_MUTED)
    draw.text((145, 630), "  }", font=FONT_MONO_SMALL, fill=TEXT_WHITE)

    draw.text((130, 700), "]", font=FONT_MONO, fill=TEXT_MUTED)

    draw.text((130, 750), "⚠️ 传统做法若直接删除前置块，将导致", font=FONT_SMALL, fill=ACCENT_AMBER)
    draw.text((130, 785), "   附带的 prompt-cache 断点全部遗失！", font=FONT_SMALL, fill=ACCENT_RED)

    # 右侧：断点顺延与纯净载荷
    draw_card(draw, [920, 250, 1820, 880], "cc-relay 净模式 (指纹清洗 + 缓存断点接力)", border_color=ACCENT_GREEN)

    # 顺延接力动效示意
    draw.rounded_rectangle([960, 330, 1780, 430], radius=10, fill=(14, 28, 48), outline=ACCENT_CYAN, width=2)
    draw.text((990, 350), "⚡ 独创算法: 缓存断点自动顺延 (Cache Carry-Over)", font=FONT_SUBTITLE, fill=ACCENT_CYAN)
    draw.text((990, 390), "被删块的 cache_control 自动顺延给紧随其后的首个幸存文本块！", font=FONT_BODY, fill=TEXT_WHITE)

    # 最终发送给 Gemini / DeepSeek 的净载荷
    draw.rounded_rectangle([960, 460, 1780, 720], radius=10, fill=(8, 14, 24), outline=ACCENT_GREEN, width=1)
    draw.text((990, 485), "下游上游 (如 Gemini 2.5 Pro / Antigravity Tools) 接收到的结构:", font=FONT_SMALL, fill=TEXT_MUTED)

    clean_lines = [
        "system: [",
        "  {",
        "    \"text\": \"项目工程规范与业务规则...\",",
        "    \"cache_control\": { \"type\": \"ephemeral\" }  <-- [完美继承断点!]",
        "  }",
        "]"
    ]
    cy_text = 525
    for cl in clean_lines:
        col = ACCENT_GREEN if "cache_control" in cl or "继承" in cl else TEXT_WHITE
        draw.text((1010, cy_text), cl, font=FONT_MONO, fill=col)
        cy_text += 30

    # 成果指标
    draw.rounded_rectangle([960, 750, 1780, 840], radius=10, fill=(12, 24, 40))
    draw.text((990, 765), "🎉 达成双赢: 既无阻碍接入 Gemini / DeepSeek", font=FONT_BODY_BOLD, fill=ACCENT_GREEN)
    draw.text((990, 800), "同时维持 > 90% 的 Prompt Cache 高额折扣，拒绝重复掏钱！", font=FONT_BODY, fill=ACCENT_CYAN)

def render_act_6(draw, progress, t, sentence_idx):
    # Scene 6: 开源生态与一键接入
    # 居中大 Logo 与标语
    logo_y = 120
    draw.text((720, logo_y), "⚡ cc-relay", font=get_font(FONT_PATH_BOLD, 64), fill=ACCENT_CYAN)
    draw.text((580, logo_y + 80), "Claude Code 本地流量调度与特征修剪中枢", font=FONT_SUBTITLE, fill=TEXT_WHITE)

    # 4 大核心亮点卡片
    highlights = [
        ("🚀 Subagent 5档模型分配", "按角色自省精准分流，长规划与轻任务各得其所"),
        ("🛡️ 纯标准库本地网关", "零 pip 依赖，微秒级转发，独创 SSE 心跳暂停闸门"),
        ("🎙️ CLI 语音输入伴侣", "Win32 免激活悬浮胶囊，鼠标侧键推讲，智能斜杠命令"),
        ("✂️ 指纹剥离与缓存顺延", "完美适配 Gemini 与第三方模型，保留 90% 缓存折扣")
    ]

    for hi, (htit, hdsc) in enumerate(highlights):
        hx = 160 + (hi % 2) * 820
        hy = 260 + (hi // 2) * 160
        draw.rounded_rectangle([hx, hy, hx + 780, hy + 130], radius=12, fill=(14, 22, 38), outline=CARD_BORDER, width=2)
        draw.text((hx + 30, hy + 25), htit, font=FONT_SUBTITLE, fill=ACCENT_CYAN)
        draw.text((hx + 30, hy + 75), hdsc, font=FONT_BODY, fill=TEXT_MUTED)

    # 极简启动命令
    draw_card(draw, [160, 610, 1760, 810], "两步极简上手体验", border_color=ACCENT_GREEN)
    draw.text((200, 680), "步骤 1. 启动本地调度中枢:     python cc_relay.py", font=FONT_MONO, fill=ACCENT_GREEN)
    draw.text((200, 730), "步骤 2. 一键配置 CLI 与桌面端:  python apply_settings.py", font=FONT_MONO, fill=ACCENT_CYAN)

    # 结尾行动呼吁
    call_to_action = "立即前往 GitHub 仓库获取源码 · 开启属于你的本地智能调度之旅！"
    cta_bbox = draw.textbbox((0, 0), call_to_action, font=FONT_SUBTITLE)
    cta_w = cta_bbox[2] - cta_bbox[0]
    draw.text(((WIDTH - cta_w) // 2, 850), call_to_action, font=FONT_SUBTITLE, fill=ACCENT_AMBER)

ACT_RENDERERS = {
    1: render_act_1,
    2: render_act_2,
    3: render_act_3,
    4: render_act_4,
    5: render_act_5,
    6: render_act_6
}

ACT_TITLES = {
    1: "PAIN POINTS & OVERVIEW",
    2: "5-TIER SUBAGENT ALLOCATION",
    3: "LOCAL GATEWAY & PAUSE GATE",
    4: "CLI VOICE INPUT COMPANION",
    5: "FINGERPRINT STRIPPING & CACHE",
    6: "GET STARTED & OPEN SOURCE"
}

def main():
    print("=== 开始生成 cc-relay 宣传视频 ===")

    # 1. 检查并读取音频元数据
    meta_path = os.path.join(AUDIO_DIR, "meta.json")
    if not os.path.exists(meta_path):
        print("错误: 未找到音频 meta.json，请先生成音频！")
        sys.exit(1)

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    print(f"载入音频分段: 共 {len(meta)} 句解说词")

    # 构建句段结构
    timeline = []
    current_time = 0.0

    # 映射表
    sentence_texts = [
        # Act 1
        (1, "在使用 Claude Code 时，你是否也遇到过这样的痛点？"),
        (1, "派生出的各类 Subagent 悄悄吞噬着昂贵的主模型配额；"),
        (1, "第三方大模型因官方指纹屡遭拦截；"),
        (1, "而在终端敲击复杂指令更是繁琐低效。"),
        (1, "今天，cc-relay 为你带来彻底破局的解决方案！"),
        # Act 2
        (2, "cc-relay 首创五档 Subagent 智能路由引擎。"),
        (2, "它能实时自省请求头与意图特征，"),
        (2, "自动将架构规划交由强推理的 Plan 代理，"),
        (2, "广域代码检索派发给 Explore 代理，"),
        (2, "而高频的审查与会话命名则分流给毫秒级极速模型。"),
        (2, "让重算力用在刀刃上，综合调用成本立降百分之七十！"),
        # Act 3
        (3, "整个本地网关采用纯 Python 标准库构建，零 pip 依赖，开箱即用。"),
        (3, "它不仅提供微秒级请求转发与实时 Token 审计，"),
        (3, "更有独创的流量暂停闸门。"),
        (3, "当你在任务中途需要更换模型、调整参数或切换秘钥时，只需一键暂停，"),
        (3, "网关将自动维持 SSE 心跳保活，终端绝不超时断连！"),
        # Act 4
        (4, "针对终端输入痛点，cc-relay 提供了深度定制的语音伴侣。"),
        (4, "采用 Win32 免激活悬浮胶囊，对讲过程绝不抢占终端焦点；"),
        (4, "按住鼠标侧键即可说话，SenseVoice 纯 CPU 离线推理毫秒级响应。"),
        (4, "更能智能识别语音指令，口播斜杠 cost 秒变规范命令，"),
        (4, "让终端交互如行云流水！"),
        # Act 5
        (5, "为了让更多上游模型发挥威力，cc-relay 提供了原、净、自三态修剪器。"),
        (5, "一键去除 Claude Code 身份声明与计费头，让 Gemini 与各类第三方模型无缝接入。"),
        (5, "更独创缓存断点顺延算法，在剥离指纹的同时无损保留缓存标记，"),
        (5, "兼顾模型兼容与高达百分之九十的 Prompt Cache 费用减免！"),
        # Act 6
        (6, "零额外依赖，一键双端接入。"),
        (6, "无论是 Claude Code CLI 还是桌面端，"),
        (6, "cc-relay 都将为你提供掌控全局的自由与极致效率。"),
        (6, "立即前往 GitHub 仓库，开启你的本地智能调度之旅！")
    ]

    total_frames = 0
    for idx, item in enumerate(meta):
        act_num, text = sentence_texts[idx]
        dur = item["duration"]
        start_frame = total_frames
        frames_count = int(dur * FPS)
        end_frame = start_frame + frames_count
        total_frames += frames_count

        timeline.append({
            "act": act_num,
            "text": text,
            "duration": dur,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "frames": frames_count
        })

    print(f"总计算帧数: {total_frames} 帧 (~ {total_frames / FPS:.2f} 秒)")

    # 2. 合并全流程主音频轨
    print("正在合并全流程音频轨道...")
    list_path = os.path.join(AUDIO_DIR, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for item in meta:
            base = os.path.basename(item["file"])
            f.write(f"file '{base}'\n")

    master_audio = os.path.join(AUDIO_DIR, "master_narration.mp3")
    res = subprocess.run([FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0", "-i", "concat_list.txt", "-c", "copy", "master_narration.mp3"], cwd=AUDIO_DIR, capture_output=True, text=True)
    if not os.path.exists(master_audio):
        print("音频合并失败:", res.stderr)
        sys.exit(1)
    print("音频轨道就绪:", master_audio)

    # 3. 初始化 OpenCV VideoWriter 渲染无声视频流
    import cv2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out_video = cv2.VideoWriter(TEMP_RAW_VIDEO, fourcc, FPS, (WIDTH, HEIGHT))

    print("正在逐帧渲染 1080p 高清画面...")

    seg_idx = 0
    for f in range(total_frames):
        while seg_idx < len(timeline) - 1 and f >= timeline[seg_idx]["end_frame"]:
            seg_idx += 1

        cur_seg = timeline[seg_idx]
        act_num = cur_seg["act"]
        seg_progress = (f - cur_seg["start_frame"]) / max(1, cur_seg["frames"])
        t = f / FPS

        # 创建画布
        img = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(img)

        # 背景
        draw_gradient_background(draw, t)

        # 顶栏
        draw_top_nav(draw, act_num, ACT_TITLES[act_num], t)

        # 场景核心动效
        renderer = ACT_RENDERERS.get(act_num, render_act_1)
        renderer(draw, seg_progress, t, seg_idx)

        # 底部发光字幕
        draw_subtitle_bar(draw, cur_seg["text"], t)

        # 写入视频
        frame_np = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        out_video.write(frame_np)

        if f % 150 == 0 or f == total_frames - 1:
            pct = (f + 1) / total_frames * 100
            print(f"渲染进度: {pct:5.1f}% [{f+1}/{total_frames} 帧] Act 0{act_num}")

    out_video.release()
    print("临时视频流渲染完成:", TEMP_RAW_VIDEO)

    # 4. 调用 ffmpeg 合成音视频成片 (H.264 + AAC)
    print("正在进行音视频合成封装...")
    cmd = [
        FFMPEG_PATH, "-y",
        "-i", TEMP_RAW_VIDEO,
        "-i", master_audio,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        OUTPUT_VIDEO_PATH
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(OUTPUT_VIDEO_PATH) and os.path.getsize(OUTPUT_VIDEO_PATH) > 1000000:
        print(f"[OK] Video successfully generated: {OUTPUT_VIDEO_PATH}")
        print(f"File size: {os.path.getsize(OUTPUT_VIDEO_PATH) / (1024*1024):.2f} MB")

        # 清理临时无声视频
        if os.path.exists(TEMP_RAW_VIDEO):
            os.remove(TEMP_RAW_VIDEO)
    else:
        print("合成失败:", res.stderr)

if __name__ == "__main__":
    main()
