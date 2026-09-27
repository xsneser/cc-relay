"""文本规范化模块 (Normalizer)：将口语化转写处理为终端与 Claude Code 友好指令"""

import re
from typing import Set

# 已知 Claude Code / CLI 常用斜杠命令白名单
KNOWN_SLASH_COMMANDS: Set[str] = {
    "cost",
    "compact",
    "clear",
    "help",
    "init",
    "doctor",
    "review",
    "login",
    "logout",
    "status",
    "config",
    "pr",
    "bug",
    "test",
    "model",
    "memory",
    "fast",
    "verbose",
    "loop",
    "simplify",
    "dataviz",
}

# 常见口语同音词别名映射
SLASH_ALIASES = {
    "考斯特": "cost",
    "帮助": "help",
    "清屏": "clear",
    "审查": "review",
    "紧凑": "compact",
    "配置": "config",
    "状态": "status",
    "登录": "login",
    "登出": "logout",
    "初始化": "init",
}

# 英文边界守卫 (在包含中文字符的上下文中，\b 无法跨越中文边界，使用字符集 lookaround)
_L = r"(?<![a-zA-Z0-9])"
_R = r"(?![a-zA-Z0-9])"

# 常见技术专有名词口语修正与中英混杂映射 (严格按长词优先排序)
TECH_TERMS_REPLACEMENTS = [
    # 1. Claude 系列 (长词优先)
    (re.compile(rf"{_L}(?:cloud\s*code|cloudcode){_R}|(?:克劳德|阔劳德|克劳得)\s*(?:code|Code|代码)", re.IGNORECASE), "Claude Code"),
    (re.compile(rf"{_L}claude{_R}|(?:克劳德|阔劳德|克劳得|克劳特)", re.IGNORECASE), "Claude"),

    # 2. ChatGPT & GPT 系列
    (re.compile(rf"{_L}(?:chad\s*gpt|chat\s*gpt|chatgpt){_R}|(?:查德|柴特|差的|插的)\s*GPT", re.IGNORECASE), "ChatGPT"),
    (re.compile(rf"{_L}(?:g\s*p\s*t|gpt){_R}|(?:吉皮提|鸡皮提|急皮提)", re.IGNORECASE), "GPT"),

    # 3. Codex 系列
    (re.compile(rf"{_L}(?:code\s*x|codex){_R}|(?:扣德斯|科德斯|扣的克斯|扣德克斯|可德斯)", re.IGNORECASE), "Codex"),

    # 4. Token & Tokens 系列 (复数优先)
    (re.compile(rf"{_L}tokens{_R}|(?:透肯斯|投肯斯|token斯)", re.IGNORECASE), "tokens"),
    (re.compile(rf"{_L}token{_R}|(?:投肯|透肯|偷肯|头肯)", re.IGNORECASE), "token"),

    # 5. CC Relay & CC 系列
    (re.compile(rf"{_L}cc\s*relay{_R}|(?:西西|CC)\s*(?:中转|relay|Relay)", re.IGNORECASE), "CC Relay"),
    (re.compile(r"(?:西西)(?=[的之与和及中转])"), "CC"),

    # 6. DeepSeek 系列
    (re.compile(rf"{_L}(?:deep\s*seek|deepseek){_R}|(?:深度求索|地普西克|地普\s*seek|滴普\s*seek|递普西克)", re.IGNORECASE), "DeepSeek"),

    # 7. Gemini & Antigravity
    (re.compile(rf"{_L}gemini{_R}|(?:杰米奈|吉米尼)", re.IGNORECASE), "Gemini"),
    (re.compile(r"(?:反重力)(?=工具|服务|上游|代理|tools)", re.IGNORECASE), "Antigravity"),

    # 8. API & API Key
    (re.compile(rf"{_L}api\s*key{_R}|(?:A批I|A皮I|诶皮爱|a\s*p\s*i)\s*(?:key|Key|密钥|秘钥)", re.IGNORECASE), "API Key"),
    (re.compile(rf"{_L}(?:a\s*p\s*i|api){_R}|(?:A批I|A皮I|诶皮爱)", re.IGNORECASE), "API"),

    # 9. Prompt
    (re.compile(rf"{_L}prompts?{_R}|(?:普朗普特|泼朗谱特|庞普特)", re.IGNORECASE), "Prompt"),

    # 10. 编程开发词汇
    (re.compile(rf"{_L}github{_R}|(?:吉特哈布|基特哈布|计特哈布)", re.IGNORECASE), "GitHub"),
    (re.compile(r"(?:吉特|记特)"), "Git"),
    (re.compile(rf"{_L}python{_R}|(?:派森|拍森)", re.IGNORECASE), "Python"),
    (re.compile(r"(?:皮普|批普)"), "pip"),
    (re.compile(rf"{_L}json{_R}|(?:杰森)", re.IGNORECASE), "JSON"),
    (re.compile(rf"{_L}c\s*l\s*i{_R}|(?:西艾勒爱|西里)", re.IGNORECASE), "CLI"),
]

# SenseVoice 特殊标记清理
SENSE_VOICE_TAG_PATTERN = re.compile(r"<\|[^|>]+(?:\|>|$)")

# 句首斜杠识别模式
SLASH_PREFIX_PATTERN = re.compile(
    r"^(?:斜杠|反斜杠|正斜杠|杠|slash)\s*([a-zA-Z0-9_一-龥\-]+)(.*)$",
    re.IGNORECASE,
)

# 尾部标点符号集合
TRAILING_PUNCTUATION_PATTERN = re.compile(r"[。！？!?，,；;.]+$")


def clean_sensevoice_tags(text: str) -> str:
    """去除 SenseVoice 产生的特殊富文本标记如 <|zh|> <|NEUTRAL|> 等"""
    if "<|" in text:
        text = SENSE_VOICE_TAG_PATTERN.sub("", text)
    return text.strip()


def format_cjk_spacing(text: str) -> str:
    """在中文字符与英文单词/数字之间增加优雅的排版空格 (中英混排规范)"""
    text = re.sub(r"([一-龥])([a-zA-Z0-9])", r"\1 \2", text)
    text = re.sub(r"([a-zA-Z0-9])([一-龥])", r"\1 \2", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def normalize(text: str) -> str:
    """规范化识别文本，转为 CLI / 编程友好的输入

    1. 清理 SenseVoice 标签与多余首尾空白
    2. 识别句首口述斜杠命令并转换 (/command)
    3. 规范常用大模型、AI 与编程技术专有名词
    4. 优雅中英混排智能空格
    5. 移除任何换行符，确保单行安全
    """
    if not text:
        return ""

    # 1. 清理模型标记与换行符
    text = clean_sensevoice_tags(text)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""

    # 2. 检查是否为口述斜杠命令
    match = SLASH_PREFIX_PATTERN.match(text)
    if match:
        cmd_candidate = match.group(1).lower().strip()
        remainder = match.group(2).strip()

        # 检查是否在别名中
        cmd_candidate = SLASH_ALIASES.get(cmd_candidate, cmd_candidate)

        # 只要是字母数字组合或属于已知命令，就格式化为 /cmd
        if cmd_candidate in KNOWN_SLASH_COMMANDS or cmd_candidate.isalnum():
            if remainder:
                remainder = TRAILING_PUNCTUATION_PATTERN.sub("", remainder)
                text = f"/{cmd_candidate} {remainder}"
            else:
                text = f"/{cmd_candidate}"
            return text.strip()

    # 如果文本整体是常见 shell 单命令（如 git status, ls -la 等），保留原生命令，仅去除末尾中文句号
    if re.match(r"^[a-zA-Z0-9_\-\.\s/]+[。.]+$", text):
        return TRAILING_PUNCTUATION_PATTERN.sub("", text).strip()

    # 3. 常见技术专有名词口语修正
    for pattern, replacement in TECH_TERMS_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    # 4. 如果是普通文本但以已有的 "/" 开头，去除尾部多余标点
    if text.startswith("/"):
        text = TRAILING_PUNCTUATION_PATTERN.sub("", text)
    else:
        # 对自然语言应用中英混排优雅排版空格
        text = format_cjk_spacing(text)

    return text.strip()
