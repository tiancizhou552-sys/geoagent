# -*- coding: utf-8 -*-
"""
6 个工具的实现与注册。

每个工具的 description 是这个项目里最重要的"提示词"：
模型看不到实现代码，只能读描述。所以每一句都在回答三个问题——
  ① 单位是什么？② 坐标系是什么？③ 什么时候该用、什么时候不该用？
"""
from __future__ import annotations

from agent.registry import register, resolve_point
from agent.schemas import (BufferQueryParams, CheckZoneParams, GeocodeParams,
                           ListFeaturesParams, NearestFacilityParams, RoutePlanParams)
from gis import engine

# ══════════════════════════════════════════════════════════
# 1) geocode
# ══════════════════════════════════════════════════════════

def _geocode(p: GeocodeParams) -> dict:
    cands = engine.geocode(p.place, top_k=p.top_k)
    return {'query': p.place, 'count': len(cands), 'candidates': cands}


def _post_geocode(data: dict) -> str | None:
    if data.get('count') == 0:
        return ('未匹配到任何地点。请勿猜测坐标 —— 可改用 list_features 查看可用地点清单，'
                '或直接告知用户该地点不在校园数据范围内。')
    cands = data.get('candidates') or []
    if len(cands) > 1 and cands[0].get('confidence', 1) < 0.9:
        return '存在多个候选，若非精确匹配，建议在回答中向用户确认具体是哪一个。'
    return None


register(
    'geocode',
    """
把校园内的地名解析成 WGS84 经纬度坐标，返回匹配置信度与匹配方式。

什么时候用：不确定用户说的地点位置时；用户用的名称可能是口语简称或错别字时；
在调用其它需要地点参数的工具之前，想先确认地点是否存在时。

支持三种写法：
  · 正式名称 —— 图书馆、荟园10栋、桃园食堂
  · 数字/中文数字变体 —— 荟十、荟10、荟园十栋 指同一个地点
  · 口语简称 —— 大活（大学生活动中心）、一教（第一教学楼）、华农图书馆（图书馆）

返回 candidates 数组，按置信度排序；confidence 越接近 1 越可靠。
matched_by 说明命中方式：alias_exact（别名精确）、name_exact（名称精确）、
substring（子串包含）、fuzzy（模糊匹配，可能是错别字）。

如果返回 0 个候选，说明该名称不在校园地点清单中 —— 此时**不要猜测坐标**，
应改用 list_features 查看可用地点，或直接告知用户无法识别。
""",
    GeocodeParams, _geocode, _post_geocode,
)

# ══════════════════════════════════════════════════════════
# 2) list_features
# ══════════════════════════════════════════════════════════

def _list_features(p: ListFeaturesParams) -> dict:
    if p.kind == 'zone':
        items = engine.list_zones(p.zone_type)
        return {'kind': 'zone', 'zone_type': p.zone_type,
                'count': len(items), 'items': items}
    pois = engine.list_pois(p.category, p.keyword, p.limit)
    return {'kind': 'poi', 'category': p.category, 'keyword': p.keyword,
            'count': len(pois), 'items': pois,
            'available_categories': engine.overview()['poi_categories']}


def _post_list(data: dict) -> str | None:
    if data.get('count') == 0:
        if data.get('kind') == 'poi':
            cats = '、'.join(f'{k}({v})' for k, v in
                             (data.get('available_categories') or {}).items())
            return f'没有符合条件的兴趣点。可用类别：{cats}'
        return '没有符合条件的停车区。'
    return None


register(
    'list_features',
    """
浏览校园数据清单。回答"有哪些…""有多少个…"这类问题前，先用它确认可用的数据。

kind=zone：列出停车区。zone_type 可选 all / parking（可停区）/ tempparking（可暂停区，限时）
/ noparking（禁停区）。返回每个区的面积、中心坐标和可读位置描述 —— 原始数据里停车区
没有名称，所以位置描述是用"距最近地标的方位和距离"生成的。

kind=poi：列出兴趣点。可用 category 按类别筛选，或 keyword 按名称关键词筛选
（如 keyword="食堂"、"充电"）。返回可选类别清单 available_categories。

典型用途：用户问"学校有哪些食堂""有多少个充电站""停车区一共多少个"；
或者某个地名解析失败后，用它查看正确写法。
""",
    ListFeaturesParams, _list_features, _post_list,
)

# ══════════════════════════════════════════════════════════
# 3) buffer_query
# ══════════════════════════════════════════════════════════

def _buffer_query(p: BufferQueryParams) -> dict:
    lon, lat, label = resolve_point(p.place, p.lon, p.lat, tag='中心点')
    out = engine.buffer_query(lon, lat, p.radius_m, p.layer, p.predicate, p.category)
    out['center_label'] = label
    return out


def _post_buffer(data: dict) -> str | None:
    if data.get('count') == 0:
        return (f"半径 {data.get('radius_m'):.0f} 米内没有命中任何 "
                f"{data.get('layer')} 要素。可以尝试扩大半径或更换图层；"
                '若确实不存在，请如实告知用户"没有"。')
    if data.get('count', 0) > 12:
        return (f"命中 {data['count']} 个要素，数量较多。回答时不必逐条念出，"
                '应归纳数量并挑最近的几个说明。')
    return None


register(
    'buffer_query',
    """
以某个地点为中心生成缓冲区，返回与缓冲区满足空间关系的要素。这是"范围查询"的主力工具。

【单位】radius_m 的单位是**米**，取值 10~5000。不要传公里，也不要传"度"。
【坐标系】place 或 lon/lat 一律使用标准 WGS84 经纬度。内部会自动投影到
  EPSG:4547（CGCS2000 3 度带中央经线 114°E）后再做缓冲与相交判断，
  所以**不要自己换算成米或其它投影坐标**，直接把经纬度传进来即可。
【空间关系】predicate=intersects（默认）表示与缓冲区有交集即可；
  predicate=within 表示目标必须完全落在缓冲区内。

layer 可选：parking（可停区）/ tempparking（可暂停区）/ noparking（禁停区）/
road（路网）/ poi（兴趣点）/ all（全部停车区）。

category 仅在 layer=poi 时生效，用于把范围查询限定到某个类别 ——
例如问"荟园食堂 500 米内有哪些充电站"，应传 layer=poi、category=charging。

返回按距离升序排列，每项含 distance_m（到中心的距离，米）与 location_label
（可读的位置描述，例如"图书馆东侧约 39 米"）。

典型用途："图书馆 300 米内有哪些可停区"、"荟园食堂 500 米内有几个充电站"、
"这个范围里有没有违停区"。
""",
    BufferQueryParams, _buffer_query, _post_buffer,
)

# ══════════════════════════════════════════════════════════
# 4) nearest_facility
# ══════════════════════════════════════════════════════════

def _nearest_facility(p: NearestFacilityParams) -> dict:
    lon, lat, label = resolve_point(p.place, p.lon, p.lat, tag='起点')
    out = engine.nearest_facility(lon, lat, p.layer, p.k, p.category)
    out['origin_label'] = label
    return out


def _post_nearest(data: dict) -> str | None:
    if data.get('count') == 0:
        return '没有找到任何候选设施，可能是该类别在本项目中无数据。请如实告知用户。'
    items = data.get('items') or []
    if items and items[0].get('inside'):
        return '最近的目标就在起点内部（距离为 0），回答时可直接说明"当前位置就在其中"。'
    return None


register(
    'nearest_facility',
    """
找出离某个地点最近的一个或多个设施，返回距离（米）与相对方位。

与 buffer_query 的区别：buffer_query 问"半径内有哪些"，可能一个都没有；
本工具问"最近的是哪一个"，不设半径上限，**一定会返回结果**。

layer=parking 找最近的停车区（会标明是可停区/可暂停区/禁停区）。
layer=poi 配合 category 找最近的特定设施：category=canteen（最近食堂）、
category=charging（最近充电站）、category=dorm（最近宿舍）、category=shop（最近超市）。

k 控制返回几个（1~10），结果按距离升序。每项含 distance_m（米）、
bearing（相对起点的方位，如"西北"）、location_label（可读位置描述）。
若某项 distance_m 为 0 且 inside=true，表示起点就在该设施范围内。

典型用途："最近的食堂在哪""离荟十最近的充电站在哪""我附近 3 个可停区"。
""",
    NearestFacilityParams, _nearest_facility, _post_nearest,
)

# ══════════════════════════════════════════════════════════
# 5) check_zone
# ══════════════════════════════════════════════════════════

def _check_zone(p: CheckZoneParams) -> dict:
    lon, lat, label = resolve_point(p.place, p.lon, p.lat, tag='待判定位置')
    out = engine.check_zone(lon, lat)
    out['location_label'] = label
    return out


def _post_check(data: dict) -> str | None:
    if data.get('zone_type') == 'noparking':
        return ('该位置属于禁停区。注意：禁停区 = 校园范围扣除所有停车区之后的剩余部分，'
                '所以校园里大部分位置都会落在禁停区，这是数据本身的定义，不是异常。'
                '回答时宜表述为"该位置不在任何停车区内，属于禁停范围"，'
                '不要说得像发现了什么特殊问题。')
    if data.get('zone_type') == 'normal':
        return '该位置既不在停车区也不在禁停区内（可能落在数据覆盖范围之外），请如实说明。'
    return None


register(
    'check_zone',
    """
判断某个地点落在哪类区域内：parking（可停区）/ tempparking（可暂停区）/
noparking（禁停区）/ normal（普通区域）。

判定优先级为"可停区 > 可暂停区 > 禁停区" —— 数据明确标注为可停区的地方就是可停区。

【重要语义】禁停区 = 校园范围扣除所有停车区之后的剩余部分，占研究区约 64%。
校园内大多数地点都会判为禁停区，这是数据定义使然，不是异常。
回答"这里能停车吗"时若结果是禁停区，应表述为"该位置不在任何停车区内，属于禁停范围"。

典型用途："图书馆门口能停车吗"、"荟园食堂属于哪类区域"、"我所在的位置算违停吗"。
""",
    CheckZoneParams, _check_zone, _post_check,
)

# ══════════════════════════════════════════════════════════
# 6) route_plan
# ══════════════════════════════════════════════════════════

def _route_plan(p: RoutePlanParams) -> dict:
    a_lon, a_lat, a_label = resolve_point(p.from_place, p.from_lon, p.from_lat, tag='起点')
    b_lon, b_lat, b_label = resolve_point(p.to_place, p.to_lon, p.to_lat, tag='终点')
    out = engine.route_plan(a_lon, a_lat, b_lon, b_lat, p.mode)
    out['from']['label'] = a_label
    out['to']['label'] = b_label
    return out


def _post_route(data: dict) -> str | None:
    if data.get('fallback'):
        return ('起终点落在路网的两个不连通部分，距离已降级为直线距离，'
                '**不是真实可走路径**。回答时必须如实说明这一限制。')
    snap_max = max(data.get('snap_from_m', 0), data.get('snap_to_m', 0))
    if snap_max > 150:
        return (f'起终点与路网的吸附距离较大（最大 {snap_max:.0f} 米），'
                '说明该地点离路网较远，路径长度仅供参考。')
    return None


register(
    'route_plan',
    """
在校园路网上规划两点之间的最短路径（基于 1636 个节点 / 1931 条边的路网图）。

【单位】distance_m 是**米**，duration_min 是分钟。
【坐标系】起终点用 WGS84 经纬度或校园地名，内部自动投影后计算。

mode=walk 按 72 米/分钟（约 1.2 m/s）估算耗时；
mode=bike 按 250 米/分钟（约 15 km/h）估算。

起终点会先吸附到最近的**路网节点**，返回的 snap_from_m / snap_to_m 是吸附距离。
该值超过约 150 米时说明该地点离路网较远，路径仅供参考。

若返回 fallback=true，说明起终点落在路网的两个不连通部分上，
距离已降级为直线距离 —— 这时必须在回答中说明这不是真实可走路径。

返回 geometry 为 GeoJSON LineString，可直接交地图渲染。

典型用途："从荟园10栋到图书馆怎么走""骑车去最近的食堂要多久"。
""",
    RoutePlanParams, _route_plan, _post_route,
)
