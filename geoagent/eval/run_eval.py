# -*- coding: utf-8 -*-
"""
对比评测：裸 Function Calling（raw） vs LangGraph ReAct（react）。

同一套工具定义、同一个模型、同一份评测集，**只换编排方式** ——
这样差异才能归因到"编排"，而不是"工具实现"或"模型"。

判定规则（刻意做成规则化的，保证可复现，不靠人看）：
  · 必须调用了 expect_tools_any 中的至少一个工具
  · 必须调用了 expect_tools_all 中的每一个工具
  · 回答必须匹配 expect_patterns 中的**全部**正则
  · 以上三条同时满足才算完成

运行：
  python eval/run_eval.py                 # 跑全部 20 条 × 2 组
  python eval/run_eval.py --limit 4       # 先跑 4 条试水
  python eval/run_eval.py --engine react  # 只跑一组
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import graph as react_engine          # noqa: E402
from agent import raw_engine                     # noqa: E402
from settings import LLM_MODEL_ID                # noqa: E402

EVAL_DIR = ROOT / 'eval'
CASES = EVAL_DIR / 'cases.jsonl'
REPORT_DIR = EVAL_DIR / 'reports'

ENGINES = {'raw': raw_engine, 'react': react_engine}


def load_cases(limit: int | None = None):
    cases = []
    for line in CASES.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            cases.append(json.loads(line))
    return cases[:limit] if limit else cases


def score(case: dict, result: dict) -> tuple[bool, str]:
    trace = result['trace']
    called = [s['name'] for s in trace.get('steps', [])]
    ok_calls = [s['name'] for s in trace.get('steps', []) if s.get('ok')]

    reasons = []

    want_any = case.get('expect_tools_any') or []
    if want_any and not (set(want_any) & set(called)):
        reasons.append(f"未调用任一期望工具（期望 {want_any}，实际 {called}）")

    want_all = case.get('expect_tools_all') or []
    missing = [t for t in want_all if t not in ok_calls]
    if missing:
        reasons.append(f"缺少必需工具 {missing}（成功调用 {ok_calls}）")

    answer = result.get('answer') or ''
    if not answer.strip():
        reasons.append('未产出回答')
    for pat in case.get('expect_patterns') or []:
        if not re.search(pat, answer):
            reasons.append(f'回答未匹配 /{pat}/')

    return (not reasons), '；'.join(reasons)


def run_one(case: dict, engine_name: str) -> dict:
    t0 = time.time()
    res = ENGINES[engine_name].run(case['query'])
    passed, why = score(case, res)
    tr = res['trace']
    return {
        'case_id': case['id'], 'category': case['category'], 'query': case['query'],
        'engine': engine_name, 'passed': passed, 'fail_reason': why,
        'answer': res['answer'],
        'tool_calls': tr['tool_calls'], 'tool_errors': tr['tool_errors'],
        'retries': tr['retries'], 'llm_calls': tr['llm_calls'],
        'elapsed_s': tr['elapsed_s'],
        'tokens': tr['usage']['total_tokens'],
        'geo_layers': tr['geo_layer_count'],
        'called_tools': [s['name'] for s in tr.get('steps', [])],
        'wall_s': round(time.time() - t0, 2),
    }


def summarize(rows: list[dict]) -> dict:
    out = {}
    for eng in sorted({r['engine'] for r in rows}):
        rs = [r for r in rows if r['engine'] == eng]
        n = len(rs)
        out[eng] = {
            'cases': n,
            'passed': sum(1 for r in rs if r['passed']),
            'completion_rate': round(100 * sum(1 for r in rs if r['passed']) / n, 1) if n else 0,
            'avg_llm_calls': round(sum(r['llm_calls'] for r in rs) / n, 2) if n else 0,
            'avg_tool_calls': round(sum(r['tool_calls'] for r in rs) / n, 2) if n else 0,
            'avg_tokens': round(sum(r['tokens'] for r in rs) / n) if n else 0,
            'avg_elapsed_s': round(sum(r['elapsed_s'] for r in rs) / n, 2) if n else 0,
        }
        for cat in ('single', 'multi', 'invalid', 'irrelevant'):
            cs = [r for r in rs if r['category'] == cat]
            if cs:
                out[eng][cat] = '%d/%d' % (sum(1 for r in cs if r['passed']), len(cs))
    return out


def print_report(rows: list[dict], summ: dict):
    W = 74
    print('=' * W)
    print('逐条结果')
    print('=' * W)
    print('%-5s %-11s %-7s %-7s %s' % ('用例', '类别', 'raw', 'react', '说明'))
    print('-' * W)
    by_id: dict[str, dict] = {}
    for r in rows:
        by_id.setdefault(r['case_id'], {})[r['engine']] = r
    for cid, pair in by_id.items():
        cat = next(iter(pair.values()))['category']
        marks = []
        for eng in ('raw', 'react'):
            r = pair.get(eng)
            marks.append('-' if not r else ('通过' if r['passed'] else '失败'))
        note = ''
        for eng in ('react', 'raw'):
            if pair.get(eng) and not pair[eng]['passed']:
                note = pair[eng]['fail_reason'][:46]
                break
        print('%-5s %-11s %-7s %-7s %s' % (cid, cat, marks[0], marks[1], note))

    print()
    print('=' * W)
    print('汇总对比')
    print('=' * W)
    print('%-22s %-16s %-16s' % ('指标', 'raw（裸FunctionCalling）', 'react（LangGraph）'))
    print('-' * W)
    keys = [('completion_rate', '任务完成率 %'), ('single', '  单跳'),
            ('multi', '  多跳'), ('invalid', '  越界'), ('irrelevant', '  无关'),
            ('avg_llm_calls', '平均 LLM 调用'), ('avg_tool_calls', '平均工具调用'),
            ('avg_tokens', '平均 token'), ('avg_elapsed_s', '平均耗时 s')]
    for k, label in keys:
        a = summ.get('raw', {}).get(k, '-')
        b = summ.get('react', {}).get(k, '-')
        print('%-22s %-16s %-16s' % (label, a, b))

    ra, rb = summ.get('raw', {}).get('completion_rate', 0), summ.get('react', {}).get('completion_rate', 0)
    print()
    if ra or rb:
        delta = rb - ra
        print('结论：ReAct 相对基线完成率 %+0.1f 个百分点（%.1f%% → %.1f%%）' % (delta, ra, rb))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--engine', choices=['raw', 'react', 'both'], default='both')
    p.add_argument('--model', default=None)
    p.add_argument('--no-save', action='store_true')
    args = p.parse_args()

    cases = load_cases(args.limit)
    engines = ['raw', 'react'] if args.engine == 'both' else [args.engine]
    print(f'模型 {args.model or LLM_MODEL_ID} | 用例 {len(cases)} 条 | 引擎 {engines}')
    print(f'总计 {len(cases) * len(engines)} 次运行，请稍候…\n')

    rows = []
    for eng in engines:
        print(f'── 引擎 {eng} ──')
        for c in cases:
            r = run_one(c, eng)
            rows.append(r)
            print('  %-5s %s  %5.1fs  %6d token  %s'
                  % (r['case_id'], '✓通过' if r['passed'] else '✗失败',
                     r['elapsed_s'], r['tokens'], r['fail_reason'][:40]))

    print()
    summ = summarize(rows)
    print_report(rows, summ)

    if not args.no_save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        path = REPORT_DIR / f'eval-{stamp}.json'
        path.write_text(json.dumps(
            {'model': args.model or LLM_MODEL_ID, 'summary': summ, 'rows': rows},
            ensure_ascii=False, indent=1), encoding='utf-8')
        print('\n报告已保存：%s' % path)


if __name__ == '__main__':
    main()
