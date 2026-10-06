# -*- coding: utf-8 -*-
"""
编排层：LangGraph StateGraph 实现的 ReAct 循环。

    START → agent ──(有 tool_calls 且未超步)──→ tools ──┐
              ↑                                          │
              └──────────────────────────────────────────┘
              │
              └──(无 tool_calls 或 步数用尽)──→ END

三个刻意的设计决定：

1. **步数用尽时不给工具，而不是直接终止。** 否则最后一条消息会是工具结果，
   用户拿到的是原始 JSON。强制收口那一轮把 tools 传 None，模型只能产出文字回答。

2. **支持单轮多工具调用。** 实测 deepseek-flash 对"图书馆 300 米内有哪些可停区"
   一次返回两个并行 tool_call（parking + tempparking）。假设"一轮只调一个工具"
   会直接漏掉一半结果。

3. **几何数据不进消息，只进 state。** 工具返回的 GeoJSON 交给前端渲染，
   发给模型的是 compact_for_llm 压缩后的版本（实测压缩 96%）。
   带几何跑多轮，上下文会被顶点坐标迅速吃光。
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import llm
from agent.exec import execute_tool_calls, extract_answer
from agent.prompts import final_answer_prompt, system_prompt
from agent.registry import openai_tools
from agent.trace import Tracer
from settings import AGENT_MAX_STEPS, LLM_MODEL_ID


def _append(left, right):
    """累积型 reducer：节点只返回增量，不返回全量"""
    return (left or []) + (right or [])


class AgentState(TypedDict, total=False):
    messages: Annotated[list, _append]
    geo: Annotated[list, _append]
    step: int


# ══════════════════════════════════════════════════════════
# 节点
# ══════════════════════════════════════════════════════════

def _make_agent_node(tracer: Tracer, tools_schema: list[dict]):
    def agent_node(state: AgentState) -> dict:
        step = state.get('step', 0)
        force_answer = step >= AGENT_MAX_STEPS

        messages = list(state['messages'])
        if force_answer:
            messages.append({'role': 'user', 'content': final_answer_prompt()})

        msg, usage = llm.chat(messages, tools=None if force_answer else tools_schema)
        tracer.log_llm(step + 1, msg, usage)
        if force_answer:
            tracer.log('force_finish', step=step + 1)

        return {'messages': [msg], 'step': step + 1}

    return agent_node


def _make_tools_node(tracer: Tracer):
    def tools_node(state: AgentState) -> dict:
        last = state['messages'][-1]
        return {'messages': execute_tool_calls(
            tracer, last.get('tool_calls') or [], state.get('step', 0))}

    return tools_node


def _route_after_agent(state: AgentState) -> str:
    last = state['messages'][-1]
    if state.get('step', 0) > AGENT_MAX_STEPS:
        return END
    return 'tools' if last.get('tool_calls') else END


# ══════════════════════════════════════════════════════════
# 图
# ══════════════════════════════════════════════════════════

def build_graph(tracer: Tracer):
    g = StateGraph(AgentState)
    g.add_node('agent', _make_agent_node(tracer, openai_tools()))
    g.add_node('tools', _make_tools_node(tracer))
    g.add_edge(START, 'agent')
    g.add_conditional_edges('agent', _route_after_agent, {'tools': 'tools', END: END})
    g.add_edge('tools', 'agent')
    return g.compile()


def run(query: str, model: str | None = None, save_trace: bool = True) -> dict:
    """跑一次完整的 ReAct 对话。返回 {answer, ok, geo_layers, trace}"""
    model = model or LLM_MODEL_ID
    tracer = Tracer(query, 'react', model)

    try:
        graph = build_graph(tracer)
        init: AgentState = {
            'messages': [
                {'role': 'system', 'content': system_prompt()},
                {'role': 'user', 'content': query},
            ],
            'step': 0,
            'geo': [],
        }
        final = graph.invoke(init, config={'recursion_limit': 40})
        answer = extract_answer(final['messages'])
        tracer.finish(answer, ok=bool(answer))
    except Exception as e:                       # noqa: BLE001
        tracer.finish('', ok=False, error=f'{type(e).__name__}: {e}')

    if save_trace:
        tracer.save()
    return {'answer': tracer.answer, 'ok': tracer.ok,
            'geo_layers': tracer.geo_layers, 'trace': tracer.summary()}
