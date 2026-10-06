# -*- coding: utf-8 -*-
"""
服务层：Flask。

  /                        交互页面（腾讯地图 GL JS + 对话 + 执行轨迹）
  /api/chat                一次问答，返回回答 + 几何图层 + 轨迹
  /api/layers/<name>       底图图层（可停区 / 可暂停区 / 禁停区 / 路网 / POI），默认转 GCJ-02
  /api/layers/<name>?crs=wgs84  取原始 WGS84 坐标
  /api/eval/latest         最近一次评测报告摘要（给页面上的对比卡片用）

启动：python web/app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import graph as react_engine        # noqa: E402
from agent import raw_engine                   # noqa: E402
from gis import engine                         # noqa: E402
from gis.gcj import layers_to_gcj02            # noqa: E402
from settings import EVAL_DIR, HOST, PORT, TMAP_KEY   # noqa: E402

app = Flask(__name__, template_folder=str(ROOT / 'web' / 'templates'),
            static_folder=str(ROOT / 'web' / 'static'))

LAYER_FILES = {
    'parking': 'parking.geojson',
    'tempparking': 'tempparking.geojson',
    'noparking': 'noparking.geojson',
    'road': 'road_network.geojson',
    'poi': 'campus_poi.geojson',
}

ENGINES = {'react': react_engine, 'raw': raw_engine}


@app.route('/')
def index():
    b = engine.study_bbox()
    clon, clat = (b['west'] + b['east']) / 2, (b['south'] + b['north']) / 2
    from gis.gcj import wgs84_to_gcj02
    glon, glat = wgs84_to_gcj02(clon, clat)   # 底图用 GCJ-02
    # center 必须是 list（渲染成 JS 数组 [lat, lon]）——不能用 tuple：
    # Jinja 会把 tuple 渲染成 "(30.47, 114.35)"，在 JS 里那是**逗号运算符**，
    # 结果是取最后一个值，于是 CENTER[0]/CENTER[1] 全变 undefined。
    return render_template('index.html', tmap_key=TMAP_KEY,
                           has_key=bool(TMAP_KEY),
                           center=[round(glat, 6), round(glon, 6)])


@app.route('/api/overview')
def api_overview():
    return jsonify(engine.overview())


@app.route('/api/layers/<name>')
def api_layer(name):
    """底图图层。

    返回的是 **GCJ-02** 坐标 —— 因为国内合规底图（腾讯/高德）用的是 GCJ-02，
    而我们的数据是 WGS84。不做这一步转换，图元会整体偏移约 590 米。
    转换只发生在渲染边界，数据层始终是 WGS84。
    """
    fn = LAYER_FILES.get(name)
    if not fn:
        return jsonify({'error': f'未知图层 {name}'}), 404
    path = ROOT / 'data' / fn
    if not path.exists():
        return jsonify({'type': 'FeatureCollection', 'features': []})

    data = json.loads(path.read_text(encoding='utf-8'))
    # /api/layers/<name>?crs=wgs84 可取原始坐标，用于核对偏移
    if request.args.get('crs', 'gcj02').lower() == 'wgs84':
        return jsonify(data)

    from gis.gcj import geom_wgs84_to_gcj02
    feats = [{**f, 'geometry': geom_wgs84_to_gcj02(f.get('geometry'))}
             for f in data.get('features', [])]
    return jsonify({**data, 'features': feats, '_display_crs': 'GCJ-02'})


@app.route('/api/chat', methods=['POST'])
def api_chat():
    body = request.get_json(silent=True) or {}
    query = (body.get('query') or '').strip()
    engine_name = body.get('engine') or 'react'
    if not query:
        return jsonify({'ok': False, 'error': '缺少 query'}), 400
    if engine_name not in ENGINES:
        return jsonify({'ok': False, 'error': f'未知引擎 {engine_name}'}), 400

    try:
        result = ENGINES[engine_name].run(query)
    except Exception as e:                      # noqa: BLE001
        return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 500

    # 渲染边界转换：轨迹与数据层保持 WGS84，只有交给底图的几何转 GCJ-02
    display_crs = (body.get('display_crs') or 'gcj02').lower()
    layers = result['geo_layers']
    if display_crs == 'gcj02':
        layers = layers_to_gcj02(layers)

    return jsonify({
        'ok': result['ok'],
        'answer': result['answer'],
        'geo_layers': layers,
        'display_crs': 'GCJ-02' if display_crs == 'gcj02' else 'WGS-84',
        'trace': result['trace'],
    })


@app.route('/api/eval/latest')
def api_eval_latest():
    reports = sorted((EVAL_DIR / 'reports').glob('eval-*.json'))
    if not reports:
        return jsonify({'available': False})
    data = json.loads(reports[-1].read_text(encoding='utf-8'))
    return jsonify({'available': True, 'file': reports[-1].name,
                    'model': data.get('model'), 'summary': data.get('summary')})


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(str(ROOT / 'web' / 'static'), 'favicon.ico') \
        if (ROOT / 'web' / 'static' / 'favicon.ico').exists() else ('', 204)


if __name__ == '__main__':
    ov = engine.overview()
    print('=' * 62)
    print('GeoAgent 已启动')
    print('  研究区   %s' % ov['study_area'])
    print('  数据     %d 个停车区 / %d 个 POI / 路网 %d 节点'
          % (sum(ov['zone_counts'].values()), ov['poi_total'], ov['road']['nodes']))
    print('  访问     http://%s:%d' % (HOST, PORT))
    print('=' * 62)
    app.run(host=HOST, port=PORT, debug=False)
