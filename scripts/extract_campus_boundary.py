# -*- coding: utf-8 -*-
"""
extract_campus_boundary.py — 从现有数据中提取华中农业大学校园矢量边界

【为什么不需要联网抓取】
校园边界本来就已经在项目里了 —— 它藏在 `data/noparking.geojson` 的几何中：

  noparking 是 1 个 MultiPolygon、2 个部件：
    · 部件 0：4 个点的小矩形（0.001 km²），无关紧要的碎屑
    · 部件 1：**224 个顶点的外环** + 153 个内环
        - 外环 = 校园矢量边界（不是矩形！形状占 bbox 的 65.7%）
        - 内环 = 被挖掉的 133 个停车区 + 其他空洞
  属性 `fclass=university, name=华中农业大学, osm_id=10811687` —— 即 OSM 里
  「华中农业大学」这个大学面要素，被当作"禁停兜底面"复用了。

所以本脚本是**离线提取**，不依赖任何网络与 Key。

用法：
    python scripts/extract_campus_boundary.py

输出：
    data/campus_boundary.geojson   校园矢量边界（单多边形，无洞）
    data/campus_boundary.wkt       边界的 WKT 文本（便于外部粘贴使用）

说明：外环上的点直接来自 OSM，未做简化（保留原始精度）。
      如需简化，取消 SIMPLIFY 相关代码注释。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'data' / 'noparking.geojson'
OUT_GEOJSON = ROOT / 'data' / 'campus_boundary.geojson'
OUT_WKT = ROOT / 'data' / 'campus_boundary.wkt'

# 设为非 None 则做几何简化（单位：度）。0.00005 度 ≈ 5 米，肉眼无差别。
SIMPLIFY_TOLERANCE = None


def pick_boundary(geometry: dict) -> list[list[float]]:
    """从 MultiPolygon 里挑出校园边界外环。

    策略：取「顶点数最多的那个部件」的外环 —— 校园边界必然比碎屑复杂。
    同时校验它确实是多边形（至少 4 个点、首尾闭合）。
    """
    assert geometry['type'] == 'MultiPolygon', \
        '预期 noparking 是 MultiPolygon，实际是 %s' % geometry['type']

    best = None
    for idx, part in enumerate(geometry['coordinates']):
        outer = part[0]          # 外环
        holes = part[1:]         # 内环
        print('  部件 %d：外环 %d 点，内环 %d 个' % (idx, len(outer), len(holes)))
        if best is None or len(outer) > len(best[1]):
            best = (idx, outer, len(holes))

    idx, outer, n_holes = best
    print('  → 选用部件 %d（外环最长，原含 %d 个内环）' % (idx, n_holes))

    if len(outer) < 4:
        sys.exit('外环点数不足（%d），不是有效多边形' % len(outer))
    if outer[0] != outer[-1]:
        outer = outer + [outer[0]]      # 补齐闭合
    return outer


def ring_area_deg2(ring: list[list[float]]) -> float:
    """鞋带公式算平面面积（度²），仅用于自检比较"""
    s = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def main() -> int:
    if not SRC.exists():
        sys.exit('找不到 %s —— 请先确认数据文件存在' % SRC)

    src = json.loads(SRC.read_text(encoding='utf-8'))
    feat = src['features'][0]
    props = feat.get('properties') or {}

    print('源文件：%s' % SRC.name)
    print('  属性：name=%s  fclass=%s  osm_id=%s'
          % (props.get('name'), props.get('fclass'), props.get('osm_id')))
    print('  几何类型：%s（%d 个部件）'
          % (feat['geometry']['type'], len(feat['geometry']['coordinates'])))
    print()

    ring = pick_boundary(feat['geometry'])

    if SIMPLIFY_TOLERANCE:
        from shapely.geometry import Polygon
        p = Polygon(ring).simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
        ring = [[round(x, 7), round(y, 7)] for x, y in p.exterior.coords]
        print('  已简化：%d 顶点' % (len(ring) - 1))

    # 自检
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    bbox_w, bbox_s, bbox_e, bbox_n = min(xs), min(ys), max(xs), max(ys)
    area_deg = ring_area_deg2(ring)
    bbox_area_deg = (bbox_e - bbox_w) * (bbox_n - bbox_s)

    print()
    print('边界统计：')
    print('  顶点数（不含闭合点）：%d' % (len(ring) - 1))
    print('  bbox：%.6f, %.6f ~ %.6f, %.6f' % (bbox_w, bbox_s, bbox_e, bbox_n))
    print('  形状紧凑度：%.4f（外环面积 / bbox 面积；=1 为矩形）'
          % (area_deg / bbox_area_deg))

    # 写 GeoJSON
    out = {
        'type': 'FeatureCollection',
        'name': 'campus_boundary',
        'crs': {'type': 'name',
                'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}},
        'source': {
            'derived_from': 'data/noparking.geojson (MultiPolygon 部件外环)',
            'osm_id': props.get('osm_id'),
            'osm_name': props.get('name'),
            'note': '校园矢量边界；由 scripts/extract_campus_boundary.py 离线提取，未联网',
        },
        'features': [{
            'type': 'Feature',
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
            'properties': {
                'name': props.get('name') or '华中农业大学',
                'osm_id': props.get('osm_id'),
                'fclass': props.get('fclass'),
                'vertex_count': len(ring) - 1,
            },
        }],
    }
    OUT_GEOJSON.write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')

    # 写 WKT（方便直接粘进 PostGIS / 其它工具）
    wkt = 'POLYGON((' + ', '.join('%.7f %.7f' % (x, y) for x, y in ring) + '))'
    OUT_WKT.write_text(wkt, encoding='utf-8')

    print()
    print('✓ 写入 %s' % OUT_GEOJSON)
    print('✓ 写入 %s（%.1f KB）' % (OUT_WKT, len(wkt) / 1024))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
