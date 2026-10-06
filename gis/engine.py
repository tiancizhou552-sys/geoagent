# -*- coding: utf-8 -*-
"""
空间算子。工具层直接调用这里的函数，不关心 CRS 细节。

约定：
  入参与返回一律 WGS84 经纬度；所有距离、面积、缓冲区在 EPSG:4547 下计算。
  内部函数以 _c 结尾表示操作投影平面坐标。
"""
from __future__ import annotations

import difflib
import math
from typing import Any

import networkx as nx
import numpy as np
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from gis.crs import area_m2, geom_to_compute, geom_to_wgs, to_compute, to_wgs
from gis.loader import ZONE_LAYERS, get_store
from settings import CRS_COMPUTE, GEOFENCE_TOLERANCE_M, WALK_SPEED_M_PER_MIN

# 方位词：8 向
_DIRS = ['东', '东北', '北', '西北', '西', '西南', '南', '东南']


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

def study_bbox() -> dict:
    return get_store().study['bbox']


def bbox_size_m() -> tuple[float, float]:
    b = study_bbox()
    x0, y0 = to_compute(b['west'], b['south'])
    x1, y1 = to_compute(b['east'], b['north'])
    return x1 - x0, y1 - y0


def in_study_area(lon: float, lat: float, tolerance_m: float = GEOFENCE_TOLERANCE_M) -> bool:
    """地理围栏：坐标是否落在研究区内（允许外扩 tolerance_m）"""
    b = study_bbox()
    dlon = tolerance_m / (111320 * math.cos(math.radians((b['south'] + b['north']) / 2)))
    dlat = tolerance_m / 110574
    return (b['west'] - dlon <= lon <= b['east'] + dlon
            and b['south'] - dlat <= lat <= b['north'] + dlat)


def _bearing_word(dx: float, dy: float) -> str:
    """投影平面上的位移 → 方位词（东/东北/北/…）"""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return ''
    ang = (math.degrees(math.atan2(dy, dx)) + 360) % 360
    return _DIRS[int(((ang + 22.5) % 360) // 45)]


def nearest_poi(lon: float, lat: float) -> tuple[str | None, float | None, str]:
    """返回最近 POI 的名称、距离（米）、以及该点相对 POI 的方位词。

    用预计算的投影坐标做向量化距离，替代逐个 POI 调用 pyproj 的写法
    （后者在缓冲区命中 25 个要素时会做 25×146 次坐标转换，约 190 ms）。
    """
    st = get_store()
    if st.pois_xy is None or not len(st.pois_xy):
        return None, None, ''
    x, y = to_compute(lon, lat)
    d2 = (st.pois_xy[:, 0] - x) ** 2 + (st.pois_xy[:, 1] - y) ** 2
    i = int(np.argmin(d2))
    px, py = st.pois_xy[i]
    return st.pois_names[i], float(math.sqrt(d2[i])), _bearing_word(x - px, y - py)


def describe_point(lon: float, lat: float, max_dist_m: float = 400.0) -> str:
    """把坐标描述成人话，用于弥补停车区无名称的问题"""
    name, d, word = nearest_poi(lon, lat)
    if name is None:
        return f'({lon:.5f}, {lat:.5f})'
    if d is None:
        return f'({lon:.5f}, {lat:.5f})'
    if d < 15:
        return f'{name}附近'
    if d > max_dist_m:
        return f'({lon:.5f}, {lat:.5f})，距 {name} 约 {d:.0f} 米'
    return f'{name}{word}侧约 {d:.0f} 米'


# ══════════════════════════════════════════════════════════
# 0) geocode —— 地名 → 坐标
# ══════════════════════════════════════════════════════════

def _poi_record(row, strategy: str, confidence: float) -> dict:
    return {
        'poi_id': row['poi_id'],
        'name': row['name'],
        'category': row['category'],
        'category_cn': row['category_cn'],
        'lon': round(float(row.geometry.x), 7),
        'lat': round(float(row.geometry.y), 7),
        'matched_by': strategy,
        'confidence': round(confidence, 3),
    }


def geocode(place: str, top_k: int = 3) -> list[dict]:
    """三级地名解析：精确别名 → 子串包含 → 模糊匹配。

    刻意不做"猜一个坐标" —— 都未命中就返回空列表，由工具层转成结构化错误。
    空间问答里错误坐标比没有答案更糟。
    """
    st = get_store()
    if st.pois is None or not len(st.pois):
        return []

    q = place.strip()
    if not q:
        return []

    # 1) 精确命中别名表
    pid = st.alias.get(q)
    if pid:
        rows = st.pois[st.pois['poi_id'] == pid]
        if len(rows):
            return [_poi_record(rows.iloc[0], 'alias_exact', 1.0)]

    # 2) 名称精确
    rows = st.pois[st.pois['name'] == q]
    if len(rows):
        return [_poi_record(rows.iloc[0], 'name_exact', 0.98)]

    # 3) 子串包含（双向）
    hits = []
    for _, row in st.pois.iterrows():
        name = row['name']
        if q in name or name in q:
            # 长度差越小越可信
            ratio = min(len(q), len(name)) / max(len(q), len(name))
            hits.append((ratio, row))
    if hits:
        hits.sort(key=lambda t: -t[0])
        return [_poi_record(r, 'substring', 0.6 + 0.3 * ratio) for ratio, r in hits[:top_k]]

    # 4) 模糊匹配（错别字、漏字）
    pool = list(st.pois['name']) + list(st.alias.keys())
    cand = difflib.get_close_matches(q, pool, n=top_k, cutoff=0.6)
    out, seen = [], set()
    for c in cand:
        pid = st.alias.get(c)
        row = None
        if pid:
            rows = st.pois[st.pois['poi_id'] == pid]
            if len(rows):
                row = rows.iloc[0]
        if row is None:
            rows = st.pois[st.pois['name'] == c]
            if len(rows):
                row = rows.iloc[0]
        if row is not None and row['poi_id'] not in seen:
            seen.add(row['poi_id'])
            score = difflib.SequenceMatcher(None, q, c).ratio()
            out.append(_poi_record(row, 'fuzzy', 0.3 + 0.3 * score))
    return out


# ══════════════════════════════════════════════════════════
# 1) find_zones / find_pois —— 让模型先"看一眼地图"
# ══════════════════════════════════════════════════════════

def list_zones(zone_type: str = 'all') -> list[dict]:
    st = get_store()
    keys = list(ZONE_LAYERS) if zone_type == 'all' else [zone_type]
    out = []
    for k in keys:
        gdf = st.zones.get(k)
        if gdf is None:
            continue
        for _, row in gdf.iterrows():
            c = row.geometry.centroid
            out.append({
                'zone_id': row['_zid'],
                'zone_type': k,
                'zone_type_cn': ZONE_LAYERS[k][1],
                'area_m2': round(area_m2(row.geometry), 1),
                'lon': round(c.x, 7),
                'lat': round(c.y, 7),
                'location_label': describe_point(c.x, c.y),
            })
    return out


def list_pois(category: str | None = None, keyword: str | None = None,
              limit: int = 30) -> list[dict]:
    st = get_store()
    if st.pois is None:
        return []
    df = st.pois
    if category:
        df = df[df['category'] == category]
    if keyword:
        df = df[df['name'].str.contains(keyword, na=False)]
    out = []
    for _, row in df.head(limit).iterrows():
        out.append({
            'poi_id': row['poi_id'], 'name': row['name'],
            'category': row['category'], 'category_cn': row['category_cn'],
            'lon': round(row.geometry.x, 7), 'lat': round(row.geometry.y, 7),
        })
    return out


# ══════════════════════════════════════════════════════════
# 2) buffer_query —— 缓冲区分析
# ══════════════════════════════════════════════════════════

def _target_geoms(layer: str):
    """返回 (投影平面几何列表, 元数据列表, 图层中文名)"""
    st = get_store()
    if layer in ZONE_LAYERS:
        gdf = st.zones_compute[layer]
        metas = [{'feature_id': r['_zid'], 'layer': layer,
                  'layer_cn': ZONE_LAYERS[layer][1], 'area_m2': round(geom.area, 1)}
                 for (_, r), geom in zip(gdf.iterrows(), gdf.geometry)]
        return list(gdf.geometry), metas, ZONE_LAYERS[layer][1]
    if layer == 'road':
        metas = [{'feature_id': r['_zid'], 'layer': 'road', 'layer_cn': '路网',
                  'area_m2': None, 'road_name': r.get('name')}
                 for _, r in st.roads.iterrows()]
        return list(st.roads_compute.geometry), metas, '路网'
    if layer == 'poi':
        metas = [{'feature_id': r['poi_id'], 'layer': 'poi', 'layer_cn': '兴趣点',
                  'area_m2': None, 'poi_name': r['name'],
                  'category': r['category'], 'category_cn': r['category_cn']}
                 for _, r in st.pois.iterrows()]
        return list(st.pois_compute.geometry), metas, '兴趣点'
    raise ValueError(f'未知图层: {layer}')


def buffer_query(lon: float, lat: float, radius_m: float, layer: str,
                 predicate: str = 'intersects', category: str | None = None) -> dict:
    """在中心点周围生成缓冲区，返回与缓冲区满足空间关系的目标要素。

    category 仅在 layer='poi' 时生效，用于把范围查询限定到某个类别
    （例如"荟园食堂 500 米内的充电站"）。
    """
    from shapely.geometry import Point

    x, y = to_compute(lon, lat)
    center_c = Point(x, y)
    buf_c = center_c.buffer(radius_m)

    if layer == 'all':
        layers = list(ZONE_LAYERS)
    else:
        layers = [layer]

    hits = []
    for lyr in layers:
        geoms, metas, cn = _target_geoms(lyr)
        for geom, meta in zip(geoms, metas):
            if geom is None or geom.is_empty:
                continue
            if category and lyr == 'poi' and meta.get('category') != category:
                continue
            if predicate == 'within':
                ok = buf_c.contains(geom)
            else:
                ok = buf_c.intersects(geom)
            if not ok:
                continue
            d = geom.distance(center_c)
            item = dict(meta)
            item['distance_m'] = round(d, 1)
            item['interior'] = bool(d == 0)
            item['_geom_c'] = geom
            hits.append(item)

    hits.sort(key=lambda h: h['distance_m'])
    feats = []
    for h in hits:
        g_wgs = geom_to_wgs(h.pop('_geom_c'))
        c = g_wgs.centroid
        h['geometry'] = mapping(g_wgs)
        h['location_label'] = describe_point(c.x, c.y)
        feats.append(h)

    return {
        'center': {'lon': lon, 'lat': lat},
        'center_label': describe_point(lon, lat),
        'radius_m': radius_m,
        'layer': layer,
        'predicate': predicate,
        'count': len(feats),
        'items': feats,
        'geojson': {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'geometry': f['geometry'],
                          'properties': {k: v for k, v in f.items() if k != 'geometry'}}
                         for f in feats],
        },
        'buffer_geojson': mapping(geom_to_wgs(buf_c)),
    }


# ══════════════════════════════════════════════════════════
# 3) nearest_facility —— 最近设施
# ══════════════════════════════════════════════════════════

def nearest_facility(lon: float, lat: float, layer: str = 'parking',
                     k: int = 1, category: str | None = None) -> dict:
    from shapely.geometry import Point

    x, y = to_compute(lon, lat)
    p = Point(x, y)

    if layer == 'poi':
        geoms, metas, _ = _target_geoms('poi')
        if category:
            st = get_store()
            keep = set(st.pois[st.pois['category'] == category]['poi_id'])
            pairs = [(g, m) for g, m in zip(geoms, metas) if m['feature_id'] in keep]
            geoms = [g for g, _ in pairs]
            metas = [m for _, m in pairs]
    elif layer == 'all':
        geoms, metas = [], []
        for k_ in ZONE_LAYERS:
            g_, m_, _ = _target_geoms(k_)
            geoms += g_
            metas += m_
    else:
        geoms, metas, _ = _target_geoms(layer)

    scored = []
    for geom, meta in zip(geoms, metas):
        if geom is None or geom.is_empty:
            continue
        scored.append((geom.distance(p), geom, meta))
    scored.sort(key=lambda t: t[0])

    items = []
    for d, geom, meta in scored[:k]:
        anchor = geom.centroid
        item = dict(meta)
        item['distance_m'] = round(d, 1)
        item['inside'] = bool(d == 0)
        lon_c, lat_c = to_wgs(anchor.x, anchor.y)
        item['lon'] = round(lon_c, 7)
        item['lat'] = round(lat_c, 7)
        item['bearing'] = _bearing_word(anchor.x - x, anchor.y - y)
        item['location_label'] = describe_point(lon_c, lat_c)
        item['geometry'] = mapping(geom_to_wgs(geom))
        items.append(item)

    return {
        'origin': {'lon': lon, 'lat': lat},
        'origin_label': describe_point(lon, lat),
        'layer': layer,
        'k': k,
        'count': len(items),
        'items': items,
    }


# ══════════════════════════════════════════════════════════
# 4) check_zone —— 点在哪个区（优先级已修正）
# ══════════════════════════════════════════════════════════

# ⚠️ 顺序即优先级：可停 > 可暂停 > 禁停。
# noparking 是"校园范围扣除所有可停区"的兜底面（占研究区 64%），
# 若先判禁停，会把 8 个可停区误判为禁停（详见 data/DATA_NOTES.md）。
ZONE_PRIORITY = ['parking', 'tempparking', 'noparking']


def check_zone(lon: float, lat: float) -> dict:
    from shapely.geometry import Point

    st = get_store()
    x, y = to_compute(lon, lat)
    p = Point(x, y)

    for key in ZONE_PRIORITY:
        gdf = st.zones_compute[key]
        for (_, row), geom in zip(gdf.iterrows(), gdf.geometry):
            if geom is None or geom.is_empty:
                continue
            if geom.contains(p) or geom.distance(p) <= 0.5:   # 0.5 m 容差
                return {
                    'zone_type': key,
                    'zone_type_cn': ZONE_LAYERS[key][1],
                    'zone_id': row['_zid'],
                    'area_m2': round(geom.area, 1),
                    'location_label': describe_point(lon, lat),
                    'lon': lon, 'lat': lat,
                }

    return {
        'zone_type': 'normal',
        'zone_type_cn': '普通区域（非停车区）',
        'zone_id': None,
        'area_m2': None,
        'location_label': describe_point(lon, lat),
        'lon': lon, 'lat': lat,
    }


# ══════════════════════════════════════════════════════════
# 5) route_plan —— 最短路径
# ══════════════════════════════════════════════════════════

def _nearest_node(x: float, y: float) -> tuple[int, float]:
    """向量化的最近节点查找（替代原来的 O(n) Python 循环）"""
    st = get_store()
    arr = st.nodes_xy
    d2 = ((arr[:, 0] - x) ** 2 + (arr[:, 1] - y) ** 2)
    idx = int(np.argmin(d2))
    return idx, float(math.sqrt(d2[idx]))


def route_plan(from_lon: float, from_lat: float, to_lon: float,
               to_lat: float, mode: str = 'walk') -> dict:
    st = get_store()
    g = st.graph

    fx, fy = to_compute(from_lon, from_lat)
    tx, ty = to_compute(to_lon, to_lat)

    si, sd = _nearest_node(fx, fy)
    ti, td = _nearest_node(tx, ty)
    snode = tuple(st.nodes_xy[si])
    tnode = tuple(st.nodes_xy[ti])

    result = {
        'from': {'lon': from_lon, 'lat': from_lat, 'label': describe_point(from_lon, from_lat)},
        'to': {'lon': to_lon, 'lat': to_lat, 'label': describe_point(to_lon, to_lat)},
        'snap_from_m': round(sd, 1),
        'snap_to_m': round(td, 1),
        'mode': mode,
        'fallback': False,
    }

    try:
        path = nx.shortest_path(g, snode, tnode, weight='weight')
        dist = sum(g[path[i]][path[i + 1]]['weight'] for i in range(len(path) - 1))
        coords = []
        from pyproj import Transformer
        inv = Transformer.from_crs('EPSG:4547', 'EPSG:4326', always_xy=True)
        for x, y in path:
            lon_, lat_ = inv.transform(x, y)
            coords.append([round(lon_, 7), round(lat_, 7)])
        result.update({
            'distance_m': round(dist, 1),
            'duration_min': round(dist / WALK_SPEED_M_PER_MIN, 1),
            'node_count': len(path),
            'geometry': {'type': 'LineString', 'coordinates': coords},
        })
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        # 两端吸附到不同连通分量：**显式降级**，不静默返回假路径
        dist = math.hypot(tx - fx, ty - fy)
        result.update({
            'distance_m': round(dist, 1),
            'duration_min': round(dist / WALK_SPEED_M_PER_MIN, 1),
            'node_count': 0,
            'fallback': True,
            'fallback_reason': '起终点落在路网的两个不连通分量上，已降级为直线距离',
            'geometry': {'type': 'LineString',
                         'coordinates': [[round(from_lon, 7), round(from_lat, 7)],
                                         [round(to_lon, 7), round(to_lat, 7)]]},
        })

    return result


# ══════════════════════════════════════════════════════════
# 概览
# ══════════════════════════════════════════════════════════

def overview() -> dict:
    """数据概览，用于系统提示中给模型一个"地图印象" """
    st = get_store()
    w, h = bbox_size_m()
    zone_counts = {k: len(st.zones[k]) for k in ZONE_LAYERS}
    cat_counts: dict[str, int] = {}
    if st.pois is not None:
        for _, row in st.pois.iterrows():
            cat_counts[row['category_cn']] = cat_counts.get(row['category_cn'], 0) + 1
    return {
        'study_area': st.study['name'],
        'size_km': {'width': round(w / 1000, 2), 'height': round(h / 1000, 2)},
        'bbox': study_bbox(),
        'zone_counts': zone_counts,
        'poi_total': int(len(st.pois)) if st.pois is not None else 0,
        'poi_categories': cat_counts,
        'road': {'nodes': st.graph.number_of_nodes(),
                 'edges': st.graph.number_of_edges()},
    }
