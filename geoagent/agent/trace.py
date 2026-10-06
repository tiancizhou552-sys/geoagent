# -*- coding: utf-8 -*-
"""
运行轨迹记录。

这是评测的数据来源 —— 简历里那句"对比裸 Function Calling 与 ReAct 的完成率差异"，
数字就是从这些 JSONL 里算出来的。所以每一步都要落盘：
模型输出、工具名、参数、成功与否、耗时、错误码、token 用量。

同时收集工具返回的 GeoJSON 图层，交给前端渲染（这些几何不进模型上下文）。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from settings import LOG_DIR


class Tracer:
    def __init__(self, query: str, engine_name: str, model: str):
        self.run_id = uuid.uuid4().hex[:12]
        self.query = query
        self.engine = engine_name
        self.model = model
        self.t0 = time.time()
        self.events: list[dict] = []
        self.geo_layers: list[dict] = []
        self.usage = {'prompt_tokens': 0, 'completion_tokens': 0,
                      'total_tokens': 0, 'reasoning_tokens': 0}
        self.llm_calls = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.retries = 0
        self.answer: str = ''
        self.ok = False
        self.error: str | None = None

    # ── 记录 ────────────────────────────────────────────

    def log(self, kind: str, **kw):
        self.events.append({'t': round(time.time() - self.t0, 3), 'kind': kind, **kw})

    def log_usage(self, usage: dict):
        self.llm_calls += 1
        for k in self.usage:
            self.usage[k] += usage.get(k, 0) or 0

    def log_llm(self, step: int, message: dict, usage: dict):
        self.log_usage(usage)
        tcs = message.get('tool_calls') or []
        self.log('llm', step=step,
                 content=(message.get('content') or '')[:500],
                 tool_calls=[{'name': t['function']['name'],
                              'arguments': t['function']['arguments']} for t in tcs],
                 usage=usage)

    def log_tool(self, step: int, name: str, args: dict, result: dict, elapsed_ms: float):
        self.tool_calls += 1
        if not result.get('ok'):
            self.tool_errors += 1
        self.log('tool', step=step, name=name, args=args,
                 ok=bool(result.get('ok')),
                 error_code=result.get('error_code'),
                 elapsed_ms=round(elapsed_ms, 1),
                 result_brief=_brief(result))

    def log_retry(self, name: str, code: str):
        self.retries += 1
        self.log('retry', name=name, error_code=code)

    def add_geo(self, name: str, geojson: dict | None):
        if geojson:
            self.geo_layers.append({'name': name, 'geojson': geojson})

    # ── 收口 ────────────────────────────────────────────

    def finish(self, answer: str, ok: bool = True, error: str | None = None):
        self.answer = answer
        self.ok = ok
        self.error = error
        self.log('finish', ok=ok, answer_len=len(answer or ''))

    @property
    def elapsed_s(self) -> float:
        return round(time.time() - self.t0, 2)

    def summary(self) -> dict:
        return {
            'run_id': self.run_id,
            'engine': self.engine,
            'model': self.model,
            'query': self.query,
            'ok': self.ok,
            'answer': self.answer,
            'error': self.error,
            'elapsed_s': self.elapsed_s,
            'llm_calls': self.llm_calls,
            'tool_calls': self.tool_calls,
            'tool_errors': self.tool_errors,
            'retries': self.retries,
            'usage': self.usage,
            'geo_layer_count': len(self.geo_layers),
            'steps': [e for e in self.events if e['kind'] == 'tool'],
        }

    def save(self) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime('%Y-%m-%d')
        path = LOG_DIR / f'{day}.jsonl'
        record = {'model': self.model, 'engine': self.engine,
                  'started_at': datetime.now().isoformat(timespec='seconds'),
                  **self.summary(), 'events': self.events}
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        return path


def _brief(result: dict) -> str:
    if not result.get('ok'):
        return f"{result.get('error_code')}: {str(result.get('message'))[:90]}"
    for key in ('count', 'distance_m', 'zone_type'):
        if key in result:
            return f'{key}={result[key]}'
    return 'ok'
