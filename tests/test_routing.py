#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 cc-relay 路由逻辑、中转占位模型识别以及子代理 (Plan 代理) 识别
"""
import unittest
from cc_relay import (
    _strip_model_suffix,
    is_relay_placeholder,
    _is_subagent,
    pick_route,
)


class TestRoutingAndSubagent(unittest.TestCase):

    def setUp(self):
        self.conf = {
            "router": {
                "route": "hybrid",
                "hybrid_codex_model": "gpt-5.6-sol",
                "hybrid_deepseek_model": "deepseek-flash",
                "tiers": {
                    "main": "deepseek-flash",
                    "opus": "gpt-5.6-sol",
                    "sonnet": "deepseek-chat",
                    "fast": "deepseek-flash",
                    "agent": "gpt-5.6-sol",
                },
                "model_routes": {
                    "gpt-5.6-sol": "codex",
                    "deepseek-flash": "deepseek",
                    "deepseek-chat": "deepseek",
                },
            }
        }

    def test_strip_model_suffix(self):
        self.assertEqual(_strip_model_suffix("relay-main[1m]"), ("relay-main", "1m"))
        self.assertEqual(_strip_model_suffix("OPUS_MODEL[1m]"), ("OPUS_MODEL", "1m"))
        self.assertEqual(_strip_model_suffix("本地中转[1m]"), ("本地中转", "1m"))
        self.assertEqual(_strip_model_suffix("deepseek-chat"), ("deepseek-chat", ""))
        self.assertEqual(_strip_model_suffix(""), ("", ""))

    def test_is_relay_placeholder(self):
        # 常见占位符与变体
        self.assertTrue(is_relay_placeholder(""))
        self.assertTrue(is_relay_placeholder(None))
        self.assertTrue(is_relay_placeholder("relay-main"))
        self.assertTrue(is_relay_placeholder("relay-main[1m]"))
        self.assertTrue(is_relay_placeholder("Relay-Main"))
        self.assertTrue(is_relay_placeholder("本地中转"))
        self.assertTrue(is_relay_placeholder("本地中转[1m]"))
        self.assertTrue(is_relay_placeholder("relay"))
        self.assertTrue(is_relay_placeholder("local"))
        self.assertTrue(is_relay_placeholder("cc-relay"))
        self.assertTrue(is_relay_placeholder("main"))
        self.assertTrue(is_relay_placeholder("relay_test"))

        # 真实/具体模型名绝不可被误认为占位符
        self.assertFalse(is_relay_placeholder("gpt-5.6-sol"))
        self.assertFalse(is_relay_placeholder("deepseek-chat"))
        self.assertFalse(is_relay_placeholder("OPUS_MODEL"))
        self.assertFalse(is_relay_placeholder("SONNET_MODEL"))
        self.assertFalse(is_relay_placeholder("FAST_MODEL"))
        self.assertFalse(is_relay_placeholder("claude-opus-5"))

    def test_is_subagent_header_case_insensitive(self):
        # 全小写
        self.assertTrue(_is_subagent({"x-claude-code-agent-id": "agent-123"}, {}))
        # 首字母大写 (Title Case)
        self.assertTrue(_is_subagent({"X-Claude-Code-Agent-Id": "agent-456"}, {}))
        # 全大写
        self.assertTrue(_is_subagent({"X-CLAUDE-CODE-AGENT-ID": "agent-789"}, {}))
        # 空值
        self.assertFalse(_is_subagent({"x-claude-code-agent-id": ""}, {}))

    def test_is_subagent_prompts(self):
        # Claude Code Plan 代理特征词
        plan_prompt = (
            "You are a software architect and planning specialist for Claude Code. "
            "Your role is to explore the codebase and design implementation plans."
        )
        self.assertTrue(_is_subagent({}, {"system": plan_prompt}))

        # List 格式的 system
        self.assertTrue(_is_subagent({}, {"system": [{"type": "text", "text": plan_prompt}]}))

        # 传统 Agent SDK 特征词
        self.assertTrue(_is_subagent({}, {"system": "Prefix... cc_is_subagent=true ...suffix"}))
        self.assertTrue(_is_subagent({}, {"system": "Built on Claude Agent SDK"}))
        self.assertTrue(_is_subagent({}, {"system": "You are a Claude agent"}))

        # 主会话 CLI 提示词 (不应被识别为子代理)
        cli_prompt = "You are Claude Code, Anthropic's official CLI for Claude."
        self.assertFalse(_is_subagent({}, {"system": cli_prompt}))
        self.assertFalse(_is_subagent({}, {}))

    def test_pick_route_relay_main_subagent(self):
        # relay-main[1m] + 子代理请求头 -> 应命中 hybrid:agent 并选用 agent 档模型
        headers = {"x-claude-code-agent-id": "plan-agent-001"}
        body = {"model": "relay-main[1m]", "system": "test"}
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:agent")
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(up, "codex")

    def test_pick_route_relay_main_plan_system_prompt(self):
        # relay-main[1m] + Plan 代理 system prompt (即使无 header) -> 命中 hybrid:agent
        headers = {}
        body = {
            "model": "relay-main[1m]",
            "system": "You are a software architect and planning specialist for Claude Code.",
        }
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:agent")
        self.assertEqual(model, "gpt-5.6-sol")

    def test_pick_route_relay_main_normal_request(self):
        # relay-main[1m] + 主会话请求 -> 应命中 hybrid:main
        headers = {}
        body = {
            "model": "relay-main[1m]",
            "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        }
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:main")
        self.assertEqual(model, "deepseek-flash")
        self.assertEqual(up, "deepseek")

    def test_pick_route_ben_di_zhong_zhuan_compat(self):
        # 兼容旧占位符 本地中转[1m]
        headers = {"X-Claude-Code-Agent-Id": "agent-002"}
        body = {"model": "本地中转[1m]"}
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:agent")
        self.assertEqual(model, "gpt-5.6-sol")

    def test_pick_route_opus_and_other_tiers(self):
        # 轨道一：CLI 占位符 OPUS_MODEL 始终走 opus 档
        headers = {"x-claude-code-agent-id": "explore-agent-003"}
        body = {"model": "OPUS_MODEL[1m]"}
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:opus")
        self.assertEqual(model, "gpt-5.6-sol")

        # 轨道二：官方模型名 claude-opus-5 在主循环时，依据 system 提示词正确命中 main 档 (而不是 opus 档！)
        headers = {}
        body = {
            "model": "claude-opus-5",
            "system": "You are an interactive agent that helps users with software engineering tasks.",
        }
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:main")
        self.assertEqual(model, "deepseek-flash")

        # 轨道二：官方模型名 claude-opus-5 在主循环无特定提示词时，默认进入 main 档
        body = {"model": "claude-opus-5"}
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:main")
        self.assertEqual(model, "deepseek-flash")

        # 轨道二：官方模型名 claude-opus-5 下 Plan 代理依据提示词特征正确命中 agent 档
        headers = {"x-claude-code-agent-id": "plan-agent-004"}
        body = {
            "model": "claude-opus-5",
            "system": "You are a software architect and planning specialist for Claude Code.",
        }
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:agent")
        self.assertEqual(model, "gpt-5.6-sol")

        # 轨道二：官方模型名 claude-opus-5 下 Explore 代理依据提示词特征正确命中 opus 档
        body = {
            "model": "claude-opus-5",
            "system": "You are a file search specialist for Claude Code.",
        }
        up, model, reason = pick_route(self.conf, headers, body)
        self.assertEqual(reason, "hybrid:opus")
        self.assertEqual(model, "gpt-5.6-sol")

        # 轨道二：官方模型名下 安全审查等任务命中 sonnet 档
        body = {
            "model": "claude-opus-5",
            "system": "You are a security monitor for autonomous AI coding agents.",
        }
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:sonnet")
        self.assertEqual(model, "deepseek-chat")

        # 轨道一：CLI 占位符 SONNET_MODEL
        body = {"model": "SONNET_MODEL[1m]"}
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:sonnet")
        self.assertEqual(model, "deepseek-chat")

        # 轨道一：CLI 占位符 FAST_MODEL
        body = {"model": "FAST_MODEL[1m]"}
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:fast")
        self.assertEqual(model, "deepseek-flash")

        # 轨道二：官方模型名 会话命名任务命中 fast 档
        body = {
            "model": "claude-opus-5",
            "system": "You are naming a coding session so the user can pick it out.",
        }
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:fast")
        self.assertEqual(model, "deepseek-flash")

        # 轨道二：官方长版本号模型 claude-haiku-4-5-20251001 命中 fast 档
        body = {"model": "claude-haiku-4-5-20251001"}
        up, model, reason = pick_route(self.conf, {}, body)
        self.assertEqual(reason, "hybrid:fast")
        self.assertEqual(model, "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
