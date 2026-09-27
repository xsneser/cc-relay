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

# 常见技术专有名词口语修正
TECH_TERMS_REPLACEMENTS = [
    (re.compile(r"\b(吉特|记特)\b", re.IGNORECASE), "Git"),
    (re.compile(r"\b(派森|拍森)\b", re.IGNORECASE), "Python"),
    (re.compile(r"\b(皮普|批普)\b", re.IGNORECASE), "pip"),
    (re.compile(r"\b(杰森)\b", re.IGNORECASE), "JSON"),
]

# SenseVoice 特殊标记清理（如情绪、事件标记）
SENSE_VOICE_TAG_PATTERN = re.compile(r"<\|[^|>]+(?:\|>|$)")

# 句首斜杠识别模式：如 "斜杠 cost", "反斜杠 compact", "slash review", "杠 help"
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


def normalize(text: str) -> str:
    """规范化识别文本，转为 CLI / 编程友好的输入

    1. 清理 SenseVoice 标签与多余首尾空白
    2. 识别句首口述斜杠命令并转换 (/command)
    3. 规范常用编程技术专有名词
    4. 针对命令去除尾部语气标点；自然语言则保留
    5. 移除任何换行符，确保单行安全
    """
    if not text:
        return ""

    # 1. 清理模型标记与非法字符
    text = clean_sensevoice_tags(text)
    # 强制移除非法换行符，防止自动回车提交执行
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
                # 去除 remainder 句末标点
                remainder = TRAILING_PUNCTUATION_PATTERN.sub("", remainder)
                text = f"/{cmd_candidate} {remainder}"
            else:
                text = f"/{cmd_candidate}"
            return text.strip()

    # 3. 常见技术专有名词口语修正
    for pattern, replacement in TECH_TERMS_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    # 4. 如果是普通文本但以已有的 "/" 开头，去除尾部多余标点
    if text.startswith("/"):
        text = TRAILING_PUNCTUATION_PATTERN.sub("", text)

    # 5. 如果文本整体是常见 shell 单命令（如 git status, ls -l 等），去除末尾中文句号
    if re.match(r"^[a-zA-Z0-9_\-\.\s/]+[。.]+$", text):
        text = TRAILING_PUNCTUATION_PATTERN.sub("", text)

    return text.strip()
