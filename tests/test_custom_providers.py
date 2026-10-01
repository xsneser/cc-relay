#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
针对自定义供应商 (Custom Providers) 及 OpenAI 协议转换的单元测试
"""

import json
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from unittest import mock
from openai_adapter import (
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    OpenAIToAnthropicStreamAdapter,
    map_stop_reason_openai_to_anthropic,
)
import cc_relay


class TestOpenAIAdapter(unittest.TestCase):

    def test_anthropic_to_openai_request_basic(self):
        req = {
            "model": "deepseek-ai/DeepSeek-V3",
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello world!"}
            ],
            "max_tokens": 1024,
            "temperature": 0.5,
            "stream": True,
        }
        res = anthropic_to_openai_request(req)
        self.assertEqual(res["model"], "deepseek-ai/DeepSeek-V3")
        self.assertTrue(res["stream"])
        self.assertEqual(res["max_tokens"], 1024)
        self.assertEqual(res["temperature"], 0.5)

        msgs = res["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "You are a helpful assistant.")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "Hello world!")

    def test_anthropic_to_openai_request_multimodal_and_system_list(self):
        req = {
            "model": "qwen-vl",
            "system": [
                {"type": "text", "text": "Part 1 of system prompt"},
                {"type": "text", "text": "Part 2 of system prompt"}
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "abc123base64"
                            }
                        }
                    ]
                }
            ]
        }
        res = anthropic_to_openai_request(req)
        msgs = res["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertIn("Part 1 of system prompt\n\nPart 2 of system prompt", msgs[0]["content"])
        user_parts = msgs[1]["content"]
        self.assertEqual(len(user_parts), 2)
        self.assertEqual(user_parts[0]["type"], "text")
        self.assertEqual(user_parts[1]["type"], "image_url")
        self.assertEqual(user_parts[1]["image_url"]["url"], "data:image/jpeg;base64,abc123base64")

    def test_anthropic_to_openai_tools_and_tool_results(self):
        req = {
            "model": "deepseek-v3",
            "messages": [
                {"role": "user", "content": "Run ls command"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Sure, running it now."},
                        {
                            "type": "tool_use",
                            "id": "call_abc123",
                            "name": "Bash",
                            "input": {"command": "ls -la"}
                        }
                    ]
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_abc123",
                            "content": "file1.txt\nfile2.txt"
                        }
                    ]
                }
            ],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Execute bash command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"]
                    }
                }
            ]
        }
        res = anthropic_to_openai_request(req)
        # 校验 tools 格式
        tools = res.get("tools")
        self.assertIsNotNone(tools)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "Bash")
        self.assertEqual(tools[0]["function"]["parameters"]["properties"]["command"]["type"], "string")

        # 校验 messages 中的 tool_calls 与 tool_result
        msgs = res["messages"]
        # msgs[0]: user
        self.assertEqual(msgs[0]["role"], "user")
        # msgs[1]: assistant
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Sure, running it now.")
        self.assertEqual(len(msgs[1]["tool_calls"]), 1)
        tc = msgs[1]["tool_calls"][0]
        self.assertEqual(tc["id"], "call_abc123")
        self.assertEqual(tc["function"]["name"], "Bash")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"command": "ls -la"})
        # msgs[2]: tool
        self.assertEqual(msgs[2]["role"], "tool")
        self.assertEqual(msgs[2]["tool_call_id"], "call_abc123")
        self.assertEqual(msgs[2]["content"], "file1.txt\nfile2.txt")

    def test_openai_to_anthropic_response(self):
        openai_resp = {
            "id": "chatcmpl-test-123",
            "model": "deepseek-ai/DeepSeek-V3",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello from OpenAI compatible endpoint!",
                        "tool_calls": [
                            {
                                "id": "call_999",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": "{\"command\": \"pwd\"}"
                                }
                            }
                        ]
                    },
                    "finish_reason": "tool_calls"
                }
            ],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 25,
                "total_tokens": 40
            }
        }
        res = openai_to_anthropic_response(openai_resp)
        self.assertEqual(res["type"], "message")
        self.assertEqual(res["role"], "assistant")
        self.assertEqual(res["stop_reason"], "tool_use")
        self.assertEqual(res["usage"]["input_tokens"], 15)
        self.assertEqual(res["usage"]["output_tokens"], 25)

        blocks = res["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["text"], "Hello from OpenAI compatible endpoint!")
        self.assertEqual(blocks[1]["type"], "tool_use")
        self.assertEqual(blocks[1]["id"], "call_999")
        self.assertEqual(blocks[1]["name"], "Bash")
        self.assertEqual(blocks[1]["input"], {"command": "pwd"})

    def test_stream_adapter_text(self):
        adapter = OpenAIToAnthropicStreamAdapter(model_name="test-model")
        openai_lines = [
            'data: {"id": "chunk-1", "choices": [{"delta": {"content": "Hello"}, "finish_reason": null}]}',
            'data: {"id": "chunk-2", "choices": [{"delta": {"content": " world!"}, "finish_reason": null}]}',
            'data: {"id": "chunk-3", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 8}}',
            'data: [DONE]'
        ]
        emitted_events = []
        for line in openai_lines:
            for b in adapter.process_chunk(line):
                text = b.decode("utf-8")
                emitted_events.append(text)

        full_stream = "".join(emitted_events)
        self.assertIn("event: message_start", full_stream)
        self.assertIn("event: content_block_start", full_stream)
        self.assertIn("event: content_block_delta", full_stream)
        self.assertIn('"text": "Hello"', full_stream)
        self.assertIn('"text": " world!"', full_stream)
        self.assertIn("event: content_block_stop", full_stream)
        self.assertIn("event: message_delta", full_stream)
        self.assertIn('"stop_reason": "end_turn"', full_stream)
        self.assertIn("event: message_stop", full_stream)

    def test_stream_adapter_tool_calls(self):
        adapter = OpenAIToAnthropicStreamAdapter(model_name="tool-model")
        openai_lines = [
            'data: {"id": "c1", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_xyz", "function": {"name": "Bash", "arguments": "{\\"com"}}]}}]}',
            'data: {"id": "c2", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "mand\\": \\"date\\"}"}}]}}]}',
            'data: {"id": "c3", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}',
            'data: [DONE]'
        ]
        emitted_events = []
        for line in openai_lines:
            for b in adapter.process_chunk(line):
                emitted_events.append(b.decode("utf-8"))

        full_stream = "".join(emitted_events)
        self.assertIn("event: message_start", full_stream)
        self.assertIn("event: content_block_start", full_stream)
        self.assertIn('"type": "tool_use"', full_stream)
        self.assertIn('"name": "Bash"', full_stream)
        self.assertIn('"id": "call_xyz"', full_stream)
        self.assertIn("event: content_block_delta", full_stream)
        self.assertIn('"type": "input_json_delta"', full_stream)
        self.assertIn("event: content_block_stop", full_stream)
        self.assertIn("event: message_delta", full_stream)
        self.assertIn('"stop_reason": "tool_use"', full_stream)
        self.assertIn("event: message_stop", full_stream)


class TestCustomProvidersRelayIntegration(unittest.TestCase):

    def setUp(self):
        self.conf = {
            "upstreams": {
                "deepseek": {"base": "https://api.deepseek.com/anthropic", "key_env": "real_deepseek_key"},
                "codex": {"base": "http://127.0.0.1:8317", "key_env": "codex_proxy_key"},
                "antigravity": {"base": "http://127.0.0.1:8045", "key_env": "antigravity_key"},
            },
            "custom_providers": {
                "siliconflow": {
                    "name": "硅基流动",
                    "base": "https://api.siliconflow.cn/v1",
                    "key": "sk-sf-secret-123456",
                    "protocol": "openai",
                    "proxy_url": "direct",
                    "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"]
                }
            },
            "router": {
                "route": "hybrid",
                "tiers": {
                    "main": "deepseek-ai/DeepSeek-V3",
                    "opus": "gpt-5.6-sol",
                    "sonnet": "deepseek-flash",
                    "fast": "deepseek-flash",
                    "agent": "deepseek-flash"
                }
            }
        }

    def test_provider_for_model_custom(self):
        # 1. 声明在 models 中的模型命中自定义供应商
        self.assertEqual(cc_relay.provider_for_model("deepseek-ai/DeepSeek-V3", self.conf), "siliconflow")
        self.assertEqual(cc_relay.provider_for_model("Qwen/Qwen2.5-72B-Instruct", self.conf), "siliconflow")

        # 2. 传统内置模型命中对应上游
        self.assertEqual(cc_relay.provider_for_model("deepseek-flash", self.conf), "deepseek")
        self.assertEqual(cc_relay.provider_for_model("gpt-5.6-sol", self.conf), "codex")
        self.assertEqual(cc_relay.provider_for_model("gemini-3.7-flash-low", self.conf), "antigravity")

        # 3. 前缀模式回退
        self.assertEqual(cc_relay.provider_for_model("siliconflow/custom-model", self.conf), "siliconflow")

    def test_pick_route_hybrid_custom_tier(self):
        # 主循环请求 (main tier 配置为 deepseek-ai/DeepSeek-V3)
        up_name, map_model, reason = cc_relay.pick_route(self.conf, {}, {"model": "relay-main"})
        self.assertEqual(up_name, "siliconflow")
        self.assertEqual(map_model, "deepseek-ai/DeepSeek-V3")
        self.assertEqual(reason, "hybrid:main")

    def test_pick_route_direct_custom(self):
        # 全量切换到 siliconflow
        conf = dict(self.conf)
        conf["router"] = {"route": "siliconflow"}
        up_name, map_model, reason = cc_relay.pick_route(conf, {}, {"model": "relay-main"})
        self.assertEqual(up_name, "siliconflow")
        self.assertEqual(map_model, "deepseek-ai/DeepSeek-V3")
        self.assertEqual(reason, "route:siliconflow")

    def test_config_public_view_masks_custom_provider_key(self):
        view = cc_relay._config_public_view(self.conf)
        cps = view.get("custom_providers") or {}
        self.assertIn("siliconflow", cps)
        sf = cps["siliconflow"]
        self.assertEqual(sf["key_mask"], cc_relay._CONFIG_KEY_MASK)
        self.assertTrue(sf["has_key"])
        self.assertNotIn("sk-sf-secret", json.dumps(view))

    def test_apply_config_update_custom_providers(self):
        base_conf = {
            "custom_providers": {
                "old_p": {"name": "Old", "base": "https://old.com/v1", "key": "old-key", "protocol": "openai"}
            }
        }
        # 新增/修改 provider
        patch = {
            "custom_providers": {
                "openrouter": {
                    "name": "OpenRouter",
                    "base": "https://openrouter.ai/api",
                    "protocol": "anthropic",
                    "key": "sk-or-new-123",
                    "models": ["anthropic/claude-3.5-sonnet"]
                },
                "old_p": {
                    "name": "Old Renamed",
                    "key": cc_relay._CONFIG_KEY_MASK  # 保持原 Key
                }
            }
        }
        saved_configs = []
        with mock.patch("cc_relay._save_conf", side_effect=lambda c: saved_configs.append(c)):
            view = cc_relay._apply_config_update(base_conf, patch)

        cps = view.get("custom_providers") or {}
        self.assertIn("openrouter", cps)
        self.assertEqual(cps["openrouter"]["name"], "OpenRouter")
        self.assertEqual(cps["openrouter"]["protocol"], "anthropic")
        self.assertTrue(cps["openrouter"]["has_key"])

        # 检查保存的 candidate conf 中 key 是否正确保存或保留
        self.assertTrue(len(saved_configs) > 0)
        saved = saved_configs[-1]
        self.assertEqual(saved["custom_providers"]["openrouter"]["key"], "sk-or-new-123")
        self.assertEqual(saved["custom_providers"]["old_p"]["key"], "old-key")
        self.assertEqual(saved["custom_providers"]["old_p"]["name"], "Old Renamed")

        # 删除 provider
        delete_patch = {
            "custom_providers": {
                "old_p": {"deleted": True}
            }
        }
        with mock.patch("cc_relay._save_conf", side_effect=lambda c: saved_configs.append(c)):
            view2 = cc_relay._apply_config_update(saved, delete_patch)
        self.assertNotIn("old_p", view2.get("custom_providers") or {})
        saved2 = saved_configs[-1]
        self.assertNotIn("old_p", saved2.get("custom_providers") or {})


if __name__ == "__main__":
    unittest.main()
