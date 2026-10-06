# -*- coding: utf-8 -*-
"""
命令行入口。

  python cli.py overview                     查看数据概览
  python cli.py tools                        列出已注册工具
  python cli.py ask "图书馆 300 米内有哪些可停区"
  python cli.py ask "..." -e raw             用基线引擎跑同一问题
  python cli.py ask "..." --show-trace       打印完整执行轨迹
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import AGENT_MAX_STEPS, AGENT_MAX_TOOL_RETRIES, LLM_MODEL_ID


def cmd_overview(_args):
    from gis import engine
    ov = engine.overview()
    print(json.dumps(ov, ensure_ascii=False, indent=2))


def cmd_tools(_args):
    import agent.tools  # noqa: F401
    from agent.registry import REGISTRY, openai_tools
    for name, t in REGISTRY.items():
        print('─' * 72)
        print(f'● {name}   参数：{"、".join(t.params.model_fields)}')
        print(t.description.strip())
    blob = json.dumps(openai_tools(), ensure_ascii=False)
    print('─' * 72)
    print(f'共 {len(REGISTRY)} 个工具，工具定义合计 {len(blob)} 字符（约 {len(blob) // 3} token）')


def cmd_ask(args):
    if args.engine == 'raw':
        from agent import raw_engine as engine_mod
    else:
        from agent import graph as engine_mod

    print(f'引擎 {args.engine} | 模型 {args.model or LLM_MODEL_ID} | '
          f'最大步数 {AGENT_MAX_STEPS} | 重试上限 {AGENT_MAX_TOOL_RETRIES}')
    print(f'问题：{args.query}')
    print('─' * 72)

    r = engine_mod.run(args.query, model=args.model)

    if args.show_trace:
        print('执行轨迹：')
        for ev in r['trace'].get('steps', []):
            brief = ev.get('result_brief', '')
            mark = '✓' if ev.get('ok') else '✗'
            print(f"  [{ev['t']:>6.2f}s] {mark} {ev['name']}"
                  f"({json.dumps(ev.get('args', {}), ensure_ascii=False)[:70]})"
                  f"  {ev['elapsed_ms']:.0f}ms  {brief[:60]}")
        print('─' * 72)

    print(r['answer'] or '（未产出回答）')
    print('─' * 72)
    s = r['trace']
    print(f"状态 {'成功' if s['ok'] else '失败'} | 耗时 {s['elapsed_s']}s | "
          f"LLM {s['llm_calls']} 次 | 工具 {s['tool_calls']} 次"
          f"（失败 {s['tool_errors']}） | 重试 {s['retries']}")
    print(f"token：总 {s['usage']['total_tokens']}"
          f"（提示 {s['usage']['prompt_tokens']} / 生成 {s['usage']['completion_tokens']}"
          f" / 其中思考 {s['usage'].get('reasoning_tokens', 0)}）"
          f" | 几何图层 {s['geo_layer_count']} 个")
    if s.get('error'):
        print(f"错误：{s['error']}")
    return 0 if s['ok'] else 1


def main():
    p = argparse.ArgumentParser(description='GeoAgent 命令行')
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('overview', help='打印数据概览').set_defaults(func=cmd_overview)
    sub.add_parser('tools', help='列出已注册工具').set_defaults(func=cmd_tools)

    a = sub.add_parser('ask', help='问一个问题')
    a.add_argument('query')
    a.add_argument('-e', '--engine', choices=['react', 'raw'], default='react')
    a.add_argument('--model', default=None)
    a.add_argument('--show-trace', action='store_true')
    a.set_defaults(func=cmd_ask)

    args = p.parse_args()
    return args.func(args) or 0


if __name__ == '__main__':
    raise SystemExit(main())
