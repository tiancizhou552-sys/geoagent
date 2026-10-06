# -*- coding: utf-8 -*-
"""
基线引擎：**裸 Function Calling**。

用来做对比实验的对照组。它的定义是刻意"朴素"的：
  第 1 轮  给模型工具，允许它发起工具调用（可能并行多个）
  执行    一次性执行这一轮的全部工具调用
  第 2 轮  不给工具，要它直接作答

与 ReAct 引擎的唯一区别：**只有一次工具调用机会**。
拿到工具结果之后没有第二轮推理 —— 不能根据结果追加调用，
也不能在工具报错后修正参数。

这正是要验证的假设：
  单跳任务（"图书馆 300 米内有哪些可停区"）→ 两者应该差不多
  多跳任务（"从宿舍到最近食堂，路上会不会经过违停区"）→ raw 应该大面积失败
"""
from __future__ import annotations

from agent import llm
from agent.exec import execute_tool_calls, extract_answer
from agent.prompts import system_prompt
from agent.registry import openai_tools
from agent.trace import Tracer
from settings import LLM_MODEL_ID

MAX_ROUNDS = 1


def run(query: str, model: str | None = None, save_trace: bool = True) -> dict:
    model = model or LLM_MODEL_ID
    tracer = Tracer(query, 'raw', model)
    tools = openai_tools()

    messages: list[dict] = [
        {'role': 'system', 'content': system_prompt()},
        {'role': 'user', 'content': query},
    ]

    try:
        # 第 1 轮：允许调用工具
        msg, usage = llm.chat(messages, tools=tools)
        tracer.log_llm(1, msg, usage)
        messages.append(msg)

        if msg.get('tool_calls'):
            messages.extend(execute_tool_calls(tracer, msg['tool_calls'], step=1))
            # 第 2 轮：不再给工具，必须作答
            msg2, usage2 = llm.chat(messages, tools=None)
            tracer.log_llm(2, msg2, usage2)
            messages.append(msg2)

        answer = extract_answer(messages)
        tracer.finish(answer, ok=bool(answer))
    except Exception as e:                       # noqa: BLE001
        tracer.finish('', ok=False, error=f'{type(e).__name__}: {e}')

    if save_trace:
        tracer.save()
    return {'answer': tracer.answer, 'ok': tracer.ok,
            'geo_layers': tracer.geo_layers, 'trace': tracer.summary()}
