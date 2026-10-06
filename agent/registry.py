# -*- coding: utf-8 -*-
"""
工具注册表 + 三层防护。

三层防护：
  第 1 层｜描述约束   —— 单位、坐标系、取值域写进 JSON Schema 的 description / enum / 数值边界
  第 2 层｜执行前校验 —— Pydantic 类型与范围 → 地名解析 → 地理围栏 → 参数语义
  第 3 层｜执行后校验 —— 结果合理性（命中数为 0 / 距离量级异常时给出提示）

失败一律返回**结构化错误**（error_code + message + hint），而不是抛异常。
hint 是给模型的下一步动作建议 —— 这是让重试真正能成功的关键，
只说"参数错误"模型只能瞎猜，说"地名不在清单里，请先用 list_features 查看"
它就能自己修。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from gis import engine

# ══════════════════════════════════════════════════════════
# 结果结构
# ══════════════════════════════════════════════════════════

@dataclass
class ToolOutcome:
    ok: bool
    data: dict | None = None
    error_code: str | None = None
    message: str | None = None
    hint: str | None = None

    def payload(self) -> dict:
        if self.ok:
            return {'ok': True, **(self.data or {})}
        out = {'ok': False, 'error_code': self.error_code, 'message': self.message}
        if self.hint:
            out['hint'] = self.hint
        return out


def err(code: str, message: str, hint: str | None = None) -> ToolOutcome:
    return ToolOutcome(ok=False, error_code=code, message=message, hint=hint)


class ResolveError(Exception):
    """地名/坐标解析失败。由 call_tool 统一转成结构化错误。"""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


# ══════════════════════════════════════════════════════════
# 工具定义
# ══════════════════════════════════════════════════════════

PostCheck = Callable[[dict], str | None]


@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    impl: Callable[[BaseModel], dict]
    post: PostCheck | None = None
    meta: dict = field(default_factory=dict)


REGISTRY: dict[str, Tool] = {}

_loaded = False


def _ensure_loaded():
    """确保工具已完成注册。

    工具注册是 `import agent.tools` 的副作用。任何入口（CLI / 图 / 基线引擎）
    只要漏掉这一句，REGISTRY 就是空的，表现为"模型拿到了 0 个工具、
    于是把调用写进正文" —— 这个 bug 第一次遇到时排查了很久，
    因为报错信息完全没有指向 import。
    """
    global _loaded
    if _loaded and REGISTRY:
        return
    import agent.tools  # noqa: F401  —— 触发注册
    _loaded = True


def register(name: str, description: str, params: type[BaseModel],
             impl: Callable[[BaseModel], dict], post: PostCheck | None = None):
    REGISTRY[name] = Tool(name=name, description=description.strip(),
                          params=params, impl=impl, post=post)
    return impl


# ══════════════════════════════════════════════════════════
# JSON Schema 生成
# ══════════════════════════════════════════════════════════

def _strip_titles(node: Any) -> Any:
    """递归去掉 Pydantic 自动加的 title 字段：无语义，只白烧 token"""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != 'title'}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def openai_tools() -> list[dict]:
    _ensure_loaded()
    out = []
    for t in REGISTRY.values():
        schema = _strip_titles(t.params.model_json_schema())
        schema.pop('$defs', None)
        out.append({
            'type': 'function',
            'function': {
                'name': t.name,
                'description': t.description,
                'parameters': schema,
            },
        })
    return out


# ══════════════════════════════════════════════════════════
# 共用的地名/坐标解析（第 2 层校验的核心）
# ══════════════════════════════════════════════════════════

def resolve_point(place: str | None, lon: float | None, lat: float | None,
                  tag: str = '位置') -> tuple[float, float, str]:
    """地名或坐标 → (lon, lat, 可读标签)。

    失败抛 ResolveError（结构化错误由 call_tool 统一包装）。
    """
    if place:
        cands = engine.geocode(place)
        if not cands:
            raise ResolveError(
                'PLACE_NOT_FOUND',
                f'{tag}地名「{place}」在校园地点清单中不存在。',
                hint='请换用清单中已有的地名（如『图书馆』『荟园食堂』『荟园10栋』），'
                     '或先调用 list_features 查看可用地点，也可以直接提供 lon/lat 坐标。',
            )
        best = cands[0]
        if best['confidence'] < 0.75 and len(cands) > 1:
            alts = '、'.join(f"{c['name']}（{c['category_cn']}）" for c in cands[:3])
            raise ResolveError(
                'PLACE_AMBIGUOUS',
                f'{tag}地名「{place}」无法唯一确定，可能匹配：{alts}。',
                hint='请改用完整名称重新调用，或先用 geocode 让用户确认。',
            )
        return best['lon'], best['lat'], best['name']

    if not engine.in_study_area(lon, lat):
        b = engine.study_bbox()
        raise ResolveError(
            'OUT_OF_BOUNDS',
            f'{tag}坐标 ({lon}, {lat}) 超出研究区范围。',
            hint=f"研究区为华中农业大学狮子山校区，范围 lon {b['west']:.5f}~{b['east']:.5f}、"
                 f"lat {b['south']:.5f}~{b['north']:.5f}。"
                 '若用户问的是校园外的地方，请直接说明本项目只覆盖校园范围。',
        )
    return lon, lat, engine.describe_point(lon, lat)


# ══════════════════════════════════════════════════════════
# 调用入口
# ══════════════════════════════════════════════════════════

def call_tool(name: str, args: str | dict,
              tracer: Callable[[str, dict], None] | None = None) -> dict:
    """执行一个工具。任何失败都转成结构化错误，绝不抛异常。"""
    import json as _json

    _ensure_loaded()
    tool = REGISTRY.get(name)
    if tool is None:
        return err('TOOL_NOT_FOUND', f'不存在名为「{name}」的工具。',
                   hint='可用工具：' + '、'.join(REGISTRY)).payload()

    if isinstance(args, str):
        try:
            args = _json.loads(args) if args.strip() else {}
        except _json.JSONDecodeError as e:
            return err('ARG_NOT_JSON', f'参数不是合法 JSON：{e.msg}（位置 {e.pos}）。',
                       hint='请重新生成 JSON 参数，字符串要用双引号。').payload()

    # —— 第 2 层：类型与范围 ——
    try:
        parsed = tool.params(**args)
    except ValidationError as e:
        details = []
        for item in e.errors()[:4]:
            loc = '.'.join(str(x) for x in item.get('loc', ()))
            details.append(f"{loc}: {item.get('msg')}")
        return err(
            'ARG_INVALID',
            '参数校验未通过 —— ' + '；'.join(details),
            hint='请按下列要求修正后重试：' + _param_hint(tool.params),
        ).payload()

    # —— 执行 ——
    try:
        data = tool.impl(parsed)
    except ResolveError as e:
        return err(e.code, e.message, e.hint).payload()
    except Exception as e:                      # noqa: BLE001 —— 工具内任何异常都要被结构化
        return err('TOOL_EXEC_ERROR', f'工具执行出错：{type(e).__name__}: {e}',
                   hint='这通常说明参数在语义上不成立（例如缓冲区退化、图层无数据），请检查后重试。').payload()

    # —— 第 3 层：结果合理性 ——
    if tool.post:
        note = tool.post(data)
        if note:
            data = dict(data)
            data['_notice'] = note

    outcome = ToolOutcome(ok=True, data=data)
    payload = outcome.payload()
    if tracer:
        tracer(name, payload)
    return payload


def _param_hint(model: type[BaseModel]) -> str:
    lines = []
    for fname, f in model.model_fields.items():
        desc = (f.description or '').split('。')[0]
        extra = ''
        for m in f.metadata:
            if hasattr(m, 'ge') and hasattr(m, 'le'):
                extra = f'（{m.ge}~{m.le}）'
        lines.append(f'{fname}{extra} = {desc}')
    return '；'.join(lines[:6])


# ══════════════════════════════════════════════════════════
# 给模型看的紧凑视图
# ══════════════════════════════════════════════════════════

_GEOM_KEYS = ('geojson', 'buffer_geojson', 'geometry')


def compact_for_llm(payload: dict, max_items: int = 8) -> dict:
    """剥离几何、截断列表后的紧凑结果。

    几何数据只服务于前端渲染，不该进模型上下文 —— 25 个多边形含数百个顶点，
    塞进去会瞬间吃掉几千 token，而模型真正需要的只是"有哪些、在哪、多远"。
    """
    out = {k: v for k, v in payload.items() if k not in _GEOM_KEYS}
    items = out.get('items')
    if isinstance(items, list):
        out['items'] = [{k: v for k, v in it.items() if k != 'geometry'}
                        for it in items[:max_items]]
        if len(items) > max_items:
            out['items_note'] = f'共 {len(items)} 项，此处仅列前 {max_items} 项'
    return out
