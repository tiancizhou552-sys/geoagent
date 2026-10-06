# -*- coding: utf-8 -*-
"""
大模型客户端。DeepSeek 走 OpenAI 兼容协议。

只做三件事：发请求、把返回规整成纯 dict、把用量带出来。
规整成纯 dict 是刻意的 —— LangGraph 的 state 与 JSONL 轨迹都要求可序列化。
"""
from __future__ import annotations

from typing import Any

from openai import OpenAI

from settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_ID, LLM_TIMEOUT

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not LLM_API_KEY:
            raise RuntimeError('缺少 LLM_API_KEY，请检查项目根目录的 .env 文件')
        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
                         timeout=LLM_TIMEOUT, max_retries=2)
    return _client


def _normalize(msg) -> dict:
    """OpenAI message 对象 → 纯 dict（tool_calls 也要转）"""
    out: dict[str, Any] = {'role': 'assistant', 'content': msg.content or ''}
    if getattr(msg, 'tool_calls', None):
        out['tool_calls'] = [{
            'id': tc.id,
            'type': 'function',
            'function': {'name': tc.function.name, 'arguments': tc.function.arguments},
        } for tc in msg.tool_calls]
    return out


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.0, model: str | None = None) -> tuple[dict, dict]:
    """一次对话补全。返回 (assistant消息, usage)。"""
    kwargs: dict[str, Any] = {
        'model': model or LLM_MODEL_ID,
        'messages': messages,
        'temperature': temperature,
    }
    if tools:
        kwargs['tools'] = tools
        # ⚠️ 只能用 auto。实测 deepseek-flash 是思考模式，传 'required' 会被拒：
        #    "Thinking mode does not support this tool_choice"
        # 所以"强制模型必须先调工具"这条路走不通，只能靠提示词与 schema 引导。
        kwargs['tool_choice'] = 'auto'

    resp = client().chat.completions.create(**kwargs)
    usage = resp.usage
    u = {
        'prompt_tokens': getattr(usage, 'prompt_tokens', 0),
        'completion_tokens': getattr(usage, 'completion_tokens', 0),
        'total_tokens': getattr(usage, 'total_tokens', 0),
    }
    details = getattr(usage, 'completion_tokens_details', None)
    if details is not None:
        u['reasoning_tokens'] = getattr(details, 'reasoning_tokens', 0) or 0
    return _normalize(resp.choices[0].message), u
