# -*- coding: utf-8 -*-
"""Web 层冒烟测试：起服务 → 打接口 → 关服务"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.app import app   # noqa: E402

PORT = 5099
BASE = f'http://127.0.0.1:{PORT}'


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, r.read().decode('utf-8')


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:          # 4xx 也要能拿到状态码
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, {}


def main():
    t = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=PORT,
                                                debug=False, use_reloader=False),
                         daemon=True)
    t.start()
    time.sleep(2.5)

    ok = fail = 0

    def check(name, cond, detail=''):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
        print('  %s %s%s' % ('✓' if cond else '✗', name, ('  ' + detail) if detail else ''))

    print('=' * 70)
    print('静态与元数据接口')
    print('=' * 70)
    st, html = get('/')
    check('GET / 返回页面', st == 200 and 'GeoAgent' in html)
    check('  页面含引擎切换', 'data-e="raw"' in html and 'data-e="react"' in html)
    check('  未配 TMAP_KEY 时给出配置指引', '需要先配置合规底图的 Key' in html)
    check('  页面无硬编码 key', 'key=' not in html.split('map.qq.com')[0][-200:])

    st, body = get('/api/overview')
    ov = json.loads(body)
    check('GET /api/overview', st == 200 and ov['poi_total'] == 146,
          'POI %d' % ov['poi_total'])

    print()
    print('=' * 70)
    print('图层接口与坐标系')
    print('=' * 70)
    st, body = get('/api/layers/parking')
    gj = json.loads(body)
    check('GET /api/layers/parking 返回 133 个面', st == 200 and len(gj['features']) == 133)
    gcj0 = gj['features'][0]['geometry']

    st2, body2 = get('/api/layers/parking?crs=wgs84')
    raw = json.loads(body2)
    wgs0 = raw['features'][0]['geometry']
    check('  默认返回 GCJ-02（带标记）', gj.get('_display_crs') == 'GCJ-02')
    check('  ?crs=wgs84 返回未转换的原始坐标', '_display_crs' not in raw)
    check('  wgs84 与 gcj02 坐标确实不同', wgs0['coordinates'] != gcj0['coordinates'])

    import math
    from shapely.geometry import shape
    from gis.crs import to_compute
    w = shape(wgs0).centroid
    g = shape(gcj0).centroid
    ax, ay = to_compute(w.x, w.y)
    bx, by = to_compute(g.x, g.y)
    off = math.hypot(bx - ax, by - ay)
    check('  实测坐标偏移量级合理（数百米）', 300 < off < 900, '偏移 %.0f 米' % off)

    print()
    print('=' * 70)
    print('对话接口')
    print('=' * 70)
    st, d = post('/api/chat', {'query': '图书馆 300 米内有哪些可停区？', 'engine': 'react'})
    check('POST /api/chat（react）', st == 200 and d['ok'] is True)
    check('  返回回答', bool(d.get('answer')))
    check('  返回几何图层', len(d.get('geo_layers') or []) >= 1,
          '%d 个图层' % len(d.get('geo_layers') or []))
    check('  标注显示坐标系', d.get('display_crs') == 'GCJ-02')
    tr = d.get('trace') or {}
    check('  返回轨迹', len(tr.get('steps') or []) >= 1,
          '工具 %d 次 / LLM %d 次' % (tr.get('tool_calls', 0), tr.get('llm_calls', 0)))
    check('  轨迹含耗时与结果摘要',
          bool(tr['steps'][0].get('elapsed_ms')) and 'result_brief' in tr['steps'][0])

    st, d2 = post('/api/chat', {'query': '今天天气怎么样？', 'engine': 'raw'})
    check('POST /api/chat（raw 引擎）', st == 200 and d2['ok'] is True)

    st, d3 = post('/api/chat', {'query': '', 'engine': 'react'})
    check('空 query 返回 400', st == 400)

    st, d4 = post('/api/chat', {'query': 'x', 'engine': 'nope'})
    check('未知引擎返回 400', st == 400)

    print()
    print('=' * 70)
    print('评测接口')
    print('=' * 70)
    st, body = get('/api/eval/latest')
    ev = json.loads(body)
    check('GET /api/eval/latest 读到报告', st == 200 and ev.get('available') is True,
          ev.get('file', ''))
    if ev.get('available'):
        s = ev['summary']
        check('  报告含两组完成率',
              'completion_rate' in s.get('raw', {}) and 'completion_rate' in s.get('react', {}),
              'raw %.0f%% → react %.0f%%' % (s['raw']['completion_rate'],
                                             s['react']['completion_rate']))

    print()
    print('=' * 70)
    print('结果：%d 通过 / %d 失败' % (ok, fail))
    print('=' * 70)
    return 1 if fail else 0


if __name__ == '__main__':
    raise SystemExit(main())
