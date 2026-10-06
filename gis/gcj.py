# -*- coding: utf-8 -*-
"""
WGS84 ⇄ GCJ-02 坐标转换。

为什么需要它：本项目所有 GIS 数据是 WGS84（EPSG:4326），而国内合规底图
（腾讯 / 高德）使用的是 GCJ-02（俗称火星坐标）。把 WGS84 坐标直接画在
GCJ-02 底图上，**在中国境内会整体偏移几十到几百米** —— 校园尺度下
足以让"图书馆东侧 39 米"的可停区跑到另一栋楼上去。

处理原则：
  · WGS84 是**内部与接口的唯一标准**（空间计算、工具返回、轨迹记录都用它）
  · 只在**渲染边界**做一次转换，转成 GCJ-02 交给底图
  · 绝不把 GCJ-02 存回数据层 —— 那会污染空间计算，且不可逆（该转换有偏移算法，
    不是精确可逆的数学变换，往返会有米级残差）

算法为国家测绘局公布的公开偏移算法（非线性加密），
在中国境外不生效，此时原样返回。
"""
from __future__ import annotations

import math

_A = 6378245.0                      # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323        # 偏心率平方


def _out_of_china(lon: float, lat: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """近似反解：迭代逼近。残差在 1e-7 度（约 1 厘米）量级。"""
    if _out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(5):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon += lon - glon
        wlat += lat - glat
    return wlon, wlat


def geom_wgs84_to_gcj02(geom: dict) -> dict:
    """GeoJSON 几何：WGS84 → GCJ-02（仅用于渲染，不得写回数据层）"""

    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            x, y = wgs84_to_gcj02(coords[0], coords[1])
            return [round(x, 7), round(y, 7)]
        return [walk(c) for c in coords]

    if not geom:
        return geom
    return {'type': geom['type'], 'coordinates': walk(geom['coordinates'])}


def layers_to_gcj02(layers: list[dict]) -> list[dict]:
    out = []
    for layer in layers:
        gj = layer.get('geojson') or {}
        feats = []
        for f in gj.get('features', []):
            feats.append({**f, 'geometry': geom_wgs84_to_gcj02(f.get('geometry'))})
        out.append({**layer, 'geojson': {**gj, 'features': feats}})
    return out
