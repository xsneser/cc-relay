#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for voice input text normalization rules."""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tools.voice_input.normalize import clean_sensevoice_tags, normalize


class TestVoiceNormalize(unittest.TestCase):
    def test_clean_sensevoice_tags(self):
        # 验证 SenseVoice 输出中的特殊标签被清理
        raw = "<|zh|><|NEUTRAL|><|Speech|>你好，世界！<|laughter|>"
        cleaned = clean_sensevoice_tags(raw)
        self.assertEqual(cleaned, "你好，世界！")

    def test_slash_commands_extraction(self):
        # 验证常见口语斜杠指令转换为 CLI 命令且不带句号
        cases = [
            ("斜杠 cost", "/cost"),
            ("斜杠 cost。", "/cost"),
            ("斜杠compact", "/compact"),
            ("反斜杠 compact。", "/compact"),
            ("正斜杠 clear", "/clear"),
            ("slash review", "/review"),
            ("杠 help", "/help"),
            ("斜杠考斯特", "/cost"),
            ("斜杠 帮助", "/help"),
            ("斜杠 清屏", "/clear"),
        ]
        for spoken, expected in cases:
            with self.subTest(spoken=spoken):
                self.assertEqual(normalize(spoken), expected)

    def test_slash_command_with_arguments(self):
        # 验证带参数的斜杠指令，末尾多余标点去除
        self.assertEqual(normalize("斜杠 help git。"), "/help git")
        self.assertEqual(normalize("slash review --diff"), "/review --diff")

    def test_shell_command_punctuation_cleanup(self):
        # 针对常规单行 CLI 指令，自动去除末尾中文句号
        self.assertEqual(normalize("git status。"), "git status")
        self.assertEqual(normalize("python main.py。"), "python main.py")
        self.assertEqual(normalize("ls -la。"), "ls -la")

    def test_natural_language_preserves_punctuation(self):
        # 普通自然语言对话保留中文句号与问号
        self.assertEqual(
            normalize("请帮我重构这个 Python 类的代码结构。"),
            "请帮我重构这个 Python 类的代码结构。",
        )
        self.assertEqual(
            normalize("这个 API 接口返回的是 JSON 格式吗？"),
            "这个 API 接口返回的是 JSON 格式吗？",
        )

    def test_tech_terms_replacement(self):
        # 常见技术专有名词口语修正
        self.assertEqual(
            normalize("使用 派森 编写一个 吉特 钩子脚本"),
            "使用 Python 编写一个 Git 钩子脚本",
        )
        self.assertEqual(
            normalize("用 皮普 安装依赖"),
            "用 pip 安装依赖",
        )

    def test_newline_safety(self):
        # 严禁在转写结果中保留任何换行符，防止自动回车提交
        raw = "查询当前状态\n并且重试\r\n"
        self.assertNotIn("\n", normalize(raw))
        self.assertNotIn("\r", normalize(raw))

    def test_empty_or_whitespace_handling(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")
        self.assertEqual(normalize("<|zh|><|NEUTRAL|>"), "")


if __name__ == "__main__":
    unittest.main()
