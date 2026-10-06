# -*- coding: utf-8 -*-
"""
工具执行器 —— ReAct 编排与裸 Function Calling 基线共用同一段代码。

共用是刻意的：对比实验里两组必须用**完全相同的工具执行逻辑**，
否则差异就分不清是"编排方式"带来的，还是"工具调用实现"带来的。
"""
from __future__ import annotations

import json
import time

from agent.registry import call_tool, compact_for_llm
from agent.trace import Tracer


def safe_args(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {'_raw': raw}


def execute_tool_calls(tracer: Tracer, tool_calls: list[dict], step: int,
                       max_items: int = 8) -> list[dict]:
    """执行一轮里的所有工具调用（可能并行多个），返回对应的 tool 消息列表。

    实测 deepseek-flash 会对"图书馆 300 米内有哪些可停区"一次返回两个并行调用，
    所以这里必须遍历而不是只取第一个。
    """
    outs: list[dict] = []
    for tc in tool_calls:
        name = tc['function']['name']
        raw_args = tc['function']['arguments']

        t0 = time.time()
        result = call_tool(name, raw_args)
        elapsed = (time.time() - t0) * 1000

        tracer.log_tool(step, name, safe_args(raw_args), result, elapsed)
        if not result.get('ok'):
            tracer.log_retry(name, result.get('error_code', 'UNKNOWN'))

        collect_geo(tracer, name, result)

        outs.append({
            'role': 'tool',
            'tool_call_id': tc['id'],
            'name': name,
            'content': json.dumps(compact_for_llm(result, max_items=max_items),
                                  ensure_ascii=False),
        })
    return outs


def collect_geo(tracer: Tracer, tool_name: str, result: dict):
    """几何数据只给前端渲染用，不进模型上下文"""
    if not result.get('ok'):
        return
    if result.get('geojson'):
        tracer.add_geo(f'{tool_name}:result', result['geojson'])
    if result.get('buffer_geojson'):
        tracer.add_geo(f'{tool_name}:buffer', {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'geometry': result['buffer_geojson'],
                          'properties': {'kind': 'buffer'}}],
        })
    if result.get('geometry'):
        tracer.add_geo(f'{tool_name}:path', {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'geometry': result['geometry'],
                          'properties': {'kind': 'route'}}],
        })


def extract_answer(messages: list[dict]) -> str:
    """取最后一条有正文的 assistant 消息"""
    for m in reversed(messages):
        if m.get('role') == 'assistant' and (m.get('content') or '').strip():
            return m['content'].strip()
    return ''
