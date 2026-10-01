#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anthropic <-> OpenAI API 协议双向适配器
实现 Claude Code (Anthropic Messages API) 与任意标准 OpenAI 兼容服务商的双向格式转换：
1. 请求转换：Anthropic messages 转换为 OpenAI chat completions 请求
2. 非流式响应转换：OpenAI choices 转换为 Anthropic message 格式
3. 流式响应转换：OpenAI SSE 流实时转换为 Anthropic 标准 SSE 事件流 (支持 text 与 tool_calls)
"""

import json
import uuid


def map_stop_reason_openai_to_anthropic(finish_reason: str) -> str:
    """映射 OpenAI finish_reason 到 Anthropic stop_reason"""
    if not finish_reason:
        return "end_turn"
    fr = str(finish_reason).strip().lower()
    if fr == "stop":
        return "end_turn"
    if fr in ("tool_calls", "function_call"):
        return "tool_use"
    if fr == "length":
        return "max_tokens"
    if fr == "content_filter":
        return "stop_sequence"
    return "end_turn"


def anthropic_to_openai_request(anthropic_body: dict, target_model: str = None) -> dict:
    """
    将 Anthropic /v1/messages 请求体转换为标准 OpenAI /v1/chat/completions 请求体。
    """
    if not isinstance(anthropic_body, dict):
        return {}

    openai_messages = []

    # 1. 提取并映射 system 提示词
    sys = anthropic_body.get("system")
    sys_content = ""
    if isinstance(sys, str) and sys.strip():
        sys_content = sys.strip()
    elif isinstance(sys, list):
        parts = []
        for b in sys:
            if isinstance(b, dict) and b.get("type") == "text":
                txt = b.get("text") or ""
                if txt:
                    parts.append(txt)
            elif isinstance(b, str) and b:
                parts.append(b)
        sys_content = "\n\n".join(parts).strip()

    if sys_content:
        openai_messages.append({
            "role": "system",
            "content": sys_content
        })

    # 2. 映射 messages
    input_msgs = anthropic_body.get("messages") or []
    for msg in input_msgs:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")

        if isinstance(content, str):
            openai_messages.append({
                "role": role,
                "content": content
            })
            continue

        if not isinstance(content, list):
            openai_messages.append({
                "role": role,
                "content": ""
            })
            continue

        # 复合 blocks 列表 (text, image, tool_use, tool_result)
        text_parts = []
        multimodal_parts = []
        tool_calls = []
        tool_results = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text") or ""
                text_parts.append(txt)
                multimodal_parts.append({"type": "text", "text": txt})
            elif btype == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64":
                    media_type = src.get("media_type") or "image/png"
                    data_b64 = src.get("data") or ""
                    multimodal_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{data_b64}"
                        }
                    })
            elif btype == "tool_use":
                call_id = block.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                call_name = block.get("name") or ""
                call_input = block.get("input") or {}
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": call_name,
                        "arguments": json.dumps(call_input, ensure_ascii=False)
                    }
                })
            elif btype == "tool_result":
                res_id = block.get("tool_use_id") or ""
                res_content = block.get("content")
                res_str = ""
                if isinstance(res_content, str):
                    res_str = res_content
                elif isinstance(res_content, list):
                    cparts = []
                    for cb in res_content:
                        if isinstance(cb, dict) and cb.get("type") == "text":
                            cparts.append(cb.get("text") or "")
                        elif isinstance(cb, str):
                            cparts.append(cb)
                    res_str = "\n".join(cparts)
                elif res_content is not None:
                    res_str = json.dumps(res_content, ensure_ascii=False)

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": res_id,
                    "content": res_str
                })

        # 先分发 tool_result (OpenAI 要求 tool 角色消息独立位于顶层)
        for tr in tool_results:
            openai_messages.append(tr)

        # 构建本消息的 content
        has_images = any(p.get("type") == "image_url" for p in multimodal_parts)
        assigned_content = multimodal_parts if has_images else "".join(text_parts)

        # 若是 assistant 且有 tool_calls 或 content
        if role == "assistant":
            m_obj = {"role": "assistant"}
            if assigned_content:
                m_obj["content"] = assigned_content
            if tool_calls:
                m_obj["tool_calls"] = tool_calls
            if "content" in m_obj or "tool_calls" in m_obj:
                openai_messages.append(m_obj)
        elif role == "user":
            if assigned_content:
                openai_messages.append({
                    "role": "user",
                    "content": assigned_content
                })

    # 3. 映射 tools 定义
    openai_tools = []
    anthropic_tools = anthropic_body.get("tools") or []
    for t in anthropic_tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not name:
            continue
        fn_obj = {
            "name": name,
            "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}
        }
        openai_tools.append({
            "type": "function",
            "function": fn_obj
        })

    # 4. 构建 OpenAI 请求负载
    payload = {
        "model": target_model or anthropic_body.get("model") or "gpt-3.5-turbo",
        "messages": openai_messages,
    }

    if openai_tools:
        payload["tools"] = openai_tools

    # 映射可选参数
    if "stream" in anthropic_body:
        payload["stream"] = bool(anthropic_body["stream"])
        if payload["stream"]:
            # 部分上游(如 SiliconFlow)支持返回 stream_options 统计 token
            payload["stream_options"] = {"include_usage": True}

    if "temperature" in anthropic_body:
        payload["temperature"] = anthropic_body["temperature"]
    if "top_p" in anthropic_body:
        payload["top_p"] = anthropic_body["top_p"]
    if "max_tokens" in anthropic_body:
        payload["max_tokens"] = anthropic_body["max_tokens"]

    return payload


def openai_to_anthropic_response(openai_resp: dict, model_name: str = None) -> dict:
    """
    将 OpenAI 非流式响应体转换为 Anthropic /v1/messages 格式。
    """
    if not isinstance(openai_resp, dict):
        return {}

    msg_id = openai_resp.get("id") or f"msg_{uuid.uuid4().hex}"
    if not msg_id.startswith("msg_"):
        msg_id = "msg_" + msg_id

    model = model_name or openai_resp.get("model") or "unknown"
    choices = openai_resp.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") or {}
    finish_reason = first_choice.get("finish_reason") or "stop"

    content_blocks = []
    txt_content = message.get("content")
    if txt_content:
        content_blocks.append({
            "type": "text",
            "text": txt_content
        })

    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        cid = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        fn = tc.get("function") or {}
        fname = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            parsed_args = {"_raw": raw_args}

        content_blocks.append({
            "type": "tool_use",
            "id": cid,
            "name": fname,
            "input": parsed_args if isinstance(parsed_args, dict) else {}
        })

    usage_data = openai_resp.get("usage") or {}
    in_tok = usage_data.get("prompt_tokens") or 0
    out_tok = usage_data.get("completion_tokens") or 0

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": map_stop_reason_openai_to_anthropic(finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok
        }
    }


class OpenAIToAnthropicStreamAdapter:
    """
    将 OpenAI SSE 数据流逐块转换为符合 Anthropic 规范的标准 SSE 事件流。
    遵循标准事件时序：
    message_start -> content_block_start -> content_block_delta -> content_block_stop -> message_delta -> message_stop
    """

    def __init__(self, model_name: str = None):
        self.model_name = model_name or "custom-model"
        self.msg_id = f"msg_{uuid.uuid4().hex}"
        self.message_started = False
        self.block_index = 0
        self.active_block_type = None  # "text" | "tool_use" | None
        self.active_tool_id = None
        self.active_tool_name = None
        self.tool_calls_buffer = {}  # index -> {id, name, args_buf}
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.final_stop_reason = "end_turn"

    def process_chunk(self, line: str):
        """
        输入单行 SSE 字符串（如 'data: {"id": ...}'），
        产出 0 到多个 Anthropic SSE 格式的 bytes。
        """
        line = line.strip()
        if not line or not line.startswith("data:"):
            return

        raw_data = line[5:].strip()
        if raw_data == "[DONE]":
            yield from self._finish_stream()
            return

        try:
            chunk = json.loads(raw_data)
        except Exception:
            return

        if not isinstance(chunk, dict):
            return

        # 捕获 usage (部分厂商如 SiliconFlow 在单独 chunk 或最后一个 chunk 返回 usage)
        if "usage" in chunk and isinstance(chunk["usage"], dict):
            u = chunk["usage"]
            if u.get("prompt_tokens"):
                self.total_input_tokens = int(u["prompt_tokens"])
            if u.get("completion_tokens"):
                self.total_output_tokens = int(u["completion_tokens"])

        # 首次触发 message_start
        if not self.message_started:
            if chunk.get("id"):
                self.msg_id = "msg_" + chunk["id"]
            if chunk.get("model") and not self.model_name:
                self.model_name = chunk["model"]

            self.message_started = True
            start_payload = {
                "type": "message_start",
                "message": {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model_name,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": self.total_input_tokens or 1,
                        "output_tokens": 1
                    }
                }
            }
            yield self._encode_event("message_start", start_payload)

        choices = chunk.get("choices") or []
        if not choices:
            return

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            self.final_stop_reason = map_stop_reason_openai_to_anthropic(finish_reason)

        delta = choice.get("delta") or {}

        # 1. 处理文本增量
        text_chunk = delta.get("content")
        if text_chunk:
            self.total_output_tokens += 1
            # 若之前开启了 tool block，先关闭
            if self.active_block_type == "tool_use":
                yield self._close_current_block()

            # 若未开启 text block，先开启
            if self.active_block_type != "text":
                self.active_block_type = "text"
                block_start = {
                    "type": "content_block_start",
                    "index": self.block_index,
                    "content_block": {
                        "type": "text",
                        "text": ""
                    }
                }
                yield self._encode_event("content_block_start", block_start)

            delta_event = {
                "type": "content_block_delta",
                "index": self.block_index,
                "delta": {
                    "type": "text_delta",
                    "text": text_chunk
                }
            }
            yield self._encode_event("content_block_delta", delta_event)

        # 2. 处理工具调用增量
        tool_deltas = delta.get("tool_calls") or []
        for td in tool_deltas:
            if not isinstance(td, dict):
                continue
            t_idx = td.get("index", 0)
            t_id = td.get("id")
            fn = td.get("function") or {}
            t_name = fn.get("name")
            t_args = fn.get("arguments") or ""

            # 首次遇到该工具调用 (或者新工具调用出现)
            if t_idx not in self.tool_calls_buffer:
                # 若当前有开启的 text 或前一个 tool block，先关闭
                if self.active_block_type is not None:
                    yield self._close_current_block()

                actual_id = t_id or f"call_{uuid.uuid4().hex[:12]}"
                actual_name = t_name or ""
                self.tool_calls_buffer[t_idx] = {
                    "id": actual_id,
                    "name": actual_name,
                    "block_index": self.block_index
                }
                self.active_block_type = "tool_use"

                start_tool_event = {
                    "type": "content_block_start",
                    "index": self.block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": actual_id,
                        "name": actual_name,
                        "input": {}
                    }
                }
                yield self._encode_event("content_block_start", start_tool_event)
            else:
                buf = self.tool_calls_buffer[t_idx]
                if t_id and not buf.get("id"):
                    buf["id"] = t_id
                if t_name and not buf.get("name"):
                    buf["name"] = t_name

            # 发送参数 chunk
            if t_args:
                arg_event = {
                    "type": "content_block_delta",
                    "index": self.tool_calls_buffer[t_idx]["block_index"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": t_args
                    }
                }
                yield self._encode_event("content_block_delta", arg_event)

    def _close_current_block(self):
        """关闭当前正在输出的 block 并增加索引"""
        stop_event = {
            "type": "content_block_stop",
            "index": self.block_index
        }
        self.block_index += 1
        self.active_block_type = None
        return self._encode_event("content_block_stop", stop_event)

    def _finish_stream(self):
        """流结束时发送闭合事件序列"""
        if not self.message_started:
            return

        # 1. 关闭任何尚未关闭的 block
        if self.active_block_type is not None:
            yield self._close_current_block()

        # 2. 发送 message_delta (含 stop_reason 与 usage)
        msg_delta = {
            "type": "message_delta",
            "delta": {
                "stop_reason": self.final_stop_reason,
                "stop_sequence": None
            },
            "usage": {
                "output_tokens": max(1, self.total_output_tokens)
            }
        }
        yield self._encode_event("message_delta", msg_delta)

        # 3. 发送 message_stop
        yield self._encode_event("message_stop", {"type": "message_stop"})

    def finish(self):
        """显式结束流（兜底保护）"""
        yield from self._finish_stream()

    @staticmethod
    def _encode_event(event_name: str, data_obj: dict) -> bytes:
        payload = f"event: {event_name}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n"
        return payload.encode("utf-8")
