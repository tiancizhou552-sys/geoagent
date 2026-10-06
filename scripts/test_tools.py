# -*- coding: utf-8 -*-
"""
P1 验收测试：工具层必须能**脱离 Agent 独立跑通**。

如果工具只能在 Agent 跑起来之后才能调，说明耦合错了。
运行：python scripts/test_tools.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.tools  # noqa: F401  —— import 即完成注册
from agent.registry import REGISTRY, compact_for_llm, call_tool, openai_tools
from settings import LLM_MODEL_ID

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('  %s %s%s' % ('✓' if cond else '✗', name, ('  ' + detail) if detail else ''))


def main():
    print('=' * 74)
    print('工具注册情况')
    print('=' * 74)
    for n, t in REGISTRY.items():
        print('  %-18s 参数 %d 个  %s' % (n, len(t.params.model_fields),
                                          t.description.strip().splitlines()[0][:34]))
    print('  共 %d 个工具' % len(REGISTRY))

    tools = openai_tools()
    blob = json.dumps(tools, ensure_ascii=False)
    print('  OpenAI 工具定义总字符数: %d（约 %d token）' % (len(blob), len(blob) // 3))
    check('工具定义能生成合规 JSON Schema', all(
        'name' in t['function'] and 'parameters' in t['function'] for t in tools))

    print()
    print('=' * 74)
    print('正常路径')
    print('=' * 74)

    # 注意：断言用「包含」而非「等于」—— POI 名称随数据源而变
    # （OSM 叫「图书馆」，腾讯叫「华农图书馆」），语义命中才是验收标准。
    r = call_tool('geocode', {'place': '图书馆'})
    check('geocode 命中图书馆', r['ok'] and r['count'] >= 1 and '图书馆' in r['candidates'][0]['name'],
          str(r['candidates'][0]['lon']) if r['ok'] and r['count'] else '')

    r = call_tool('geocode', {'place': '荟十'})
    check('geocode 解析口语简称「荟十」',
          r['ok'] and r['count'] >= 1 and r['candidates'][0]['name'].endswith('荟园10栋'),
          r['candidates'][0]['name'] if r['ok'] and r['count'] else '')

    r = call_tool('list_features', {'kind': 'zone', 'zone_type': 'parking'})
    check('list_features 列出 133 个可停区', r['ok'] and r['count'] == 133, 'count=%d' % r.get('count', -1))

    # 食堂数量随数据源而变（OSM=6，腾讯=3：竹苑/桃园/橘园）。只断言「有且分类正确」。
    r = call_tool('list_features', {'kind': 'poi', 'category': 'canteen'})
    check('list_features 按类别查食堂', r['ok'] and r['count'] >= 3, 'count=%d' % r.get('count', -1))

    r = call_tool('buffer_query', {'place': '图书馆', 'radius_m': 300, 'layer': 'parking'})
    check('buffer_query 图书馆 300m 命中可停区', r['ok'] and r['count'] > 0,
          'count=%d' % r.get('count', -1))
    first = r['items'][0] if r.get('items') else {}
    check('  结果带距离与可读位置', 'distance_m' in first and 'location_label' in first,
          '%s / %s' % (first.get('distance_m'), first.get('location_label')))
    check('  返回 GeoJSON 供前端渲染', r['geojson']['type'] == 'FeatureCollection')

    r = call_tool('nearest_facility', {'place': '荟十', 'layer': 'poi', 'k': 3, 'category': 'canteen'})
    check('nearest_facility 找最近 3 个食堂', r['ok'] and r['count'] == 3,
          ' / '.join(i['poi_name'] for i in r.get('items', [])))

    # check_zone：必须用**落在多边形内部**的点来测。
    # 用 centroid 是错的 —— 凹多边形/多部件的质心可能落在自身之外（详见 data/DATA_NOTES.md 第 2 项）
    from gis.loader import get_store
    st = get_store()
    pk = st.zones['parking'].geometry.iloc[0].representative_point()
    r = call_tool('check_zone', {'lon': pk.x, 'lat': pk.y})
    check('check_zone 判定可停区内部点', r['ok'] and r['zone_type'] == 'parking', r.get('zone_type', ''))

    tp = st.zones['tempparking'].geometry.iloc[0].representative_point()
    r = call_tool('check_zone', {'lon': tp.x, 'lat': tp.y})
    check('check_zone 判定可暂停区内部点', r['ok'] and r['zone_type'] == 'tempparking',
          r.get('zone_type', ''))

    r = call_tool('check_zone', {'place': '荟园食堂'})
    check('check_zone 对食堂判定为禁停（语义正确）',
          r['ok'] and r['zone_type'] == 'noparking', r.get('zone_type', ''))
    check('  禁停区结果附带语义说明', bool(r.get('_notice')))

    r = call_tool('route_plan', {'from_place': '荟园10栋', 'to_place': '图书馆'})
    check('route_plan 规划出路径', r['ok'] and not r.get('fallback', True),
          '%.0f m / %.1f min' % (r.get('distance_m', 0), r.get('duration_min', 0)))

    # —— 回归守卫：四教 ——
    # 这是切换数据源（OSM → 腾讯）的**原因**：OSM 里根本没有四教，
    # 「四教附近的停车区」会返回 PLACE_NOT_FOUND。此断言防它再退化。
    r = call_tool('geocode', {'place': '四教'})
    check('geocode 命中「四教」口语简称', r['ok'] and r['count'] >= 1,
          r['candidates'][0]['name'] if r['ok'] and r['count'] else '')
    r = call_tool('buffer_query', {'place': '四教', 'radius_m': 300, 'layer': 'parking'})
    check('buffer_query 四教 300m 命中可停区', r['ok'] and r.get('count', 0) > 0,
          'count=%d' % r.get('count', -1))

    print()
    print('=' * 74)
    print('错误路径 —— 必须返回结构化错误，绝不抛异常')
    print('=' * 74)

    r = call_tool('geocode', {'place': '埃菲尔铁塔'})
    check('不存在的地名 → PLACE_NOT_FOUND', r['ok'] and r['count'] == 0 and r.get('_notice'))
    r2 = call_tool('buffer_query', {'place': '埃菲尔铁塔', 'radius_m': 100, 'layer': 'parking'})
    check('buffer_query 用不存在地名 → 结构化错误',
          r2['ok'] is False and r2['error_code'] == 'PLACE_NOT_FOUND', r2.get('error_code', ''))
    check('  错误里带 hint 指引', bool(r2.get('hint')), (r2.get('hint') or '')[:46] + '…')

    r = call_tool('buffer_query', {'lon': 116.397, 'lat': 39.908, 'radius_m': 200, 'layer': 'parking'})
    check('北京坐标 → OUT_OF_BOUNDS', r['ok'] is False and r['error_code'] == 'OUT_OF_BOUNDS',
          (r.get('message') or '')[:40])

    r = call_tool('buffer_query', {'place': '图书馆', 'radius_m': 99999, 'layer': 'parking'})
    check('半径超上限 → ARG_INVALID', r['ok'] is False and r['error_code'] == 'ARG_INVALID',
          (r.get('message') or '')[:56])

    r = call_tool('check_zone', {})
    check('既不给地名也不给坐标 → ARG_INVALID', r['ok'] is False and r['error_code'] == 'ARG_INVALID')

    r = call_tool('route_plan', {'from_place': '图书馆', 'to_place': '荟园食堂', 'mode': 'fly'})
    check('非法枚举值 mode=fly → ARG_INVALID', r['ok'] is False and r['error_code'] == 'ARG_INVALID')

    r = call_tool('buffer_query', '{"place": "图书馆", "radius_m": 300,')
    check('残缺 JSON → ARG_NOT_JSON', r['ok'] is False and r['error_code'] == 'ARG_NOT_JSON')

    r = call_tool('no_such_tool', {})
    check('不存在的工具 → TOOL_NOT_FOUND', r['ok'] is False and r['error_code'] == 'TOOL_NOT_FOUND')

    print()
    print('=' * 74)
    print('紧凑视图 —— 几何不能进模型上下文')
    print('=' * 74)
    full = call_tool('buffer_query', {'place': '图书馆', 'radius_m': 300, 'layer': 'parking'})
    compact = compact_for_llm(full, max_items=5)
    full_s = json.dumps(full, ensure_ascii=False)
    comp_s = json.dumps(compact, ensure_ascii=False)
    check('紧凑视图体积显著减小', len(comp_s) < len(full_s) * 0.35,
          '%d → %d 字符（压缩 %.0f%%）' % (len(full_s), len(comp_s),
                                        100 * (1 - len(comp_s) / len(full_s))))
    check('紧凑视图不含 geometry', 'geometry' not in comp_s and 'geojson' not in comp_s)
    check('紧凑视图保留关键信息', 'distance_m' in comp_s and 'location_label' in comp_s)
    check('超长列表被截断并注明', 'items_note' in compact, compact.get('items_note', ''))

    print()
    print('=' * 74)
    print('结果：%d 通过 / %d 失败' % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print('  ✗ %s' % f)
    print('=' * 74)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
