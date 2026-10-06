# -*- coding: utf-8 -*-
"""
数据加载与缓存。

进程内**只加载一次**：图层、路网图、POI、别名表。
（原 campus_parking 在每次路径请求时重建路网图——452 条线、1931 条边，
  属于无意义的重复劳动。这里改为启动时构建一次，之后只读。）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache

import geopandas as gpd
import networkx as nx
import numpy as np
from shapely.geometry import Point

from settings import CONFIG_DIR, CRS_COMPUTE, CRS_STORAGE, DATA_DIR

ZONE_LAYERS = {
    'parking': ('parking.geojson', '可停区'),
    'tempparking': ('tempparking.geojson', '可暂停区'),
    'noparking': ('noparking.geojson', '禁停区'),
}


@dataclass
class DataStore:
    zones: dict[str, gpd.GeoDataFrame] = field(default_factory=dict)
    zones_compute: dict[str, gpd.GeoDataFrame] = field(default_factory=dict)
    roads: gpd.GeoDataFrame | None = None
    roads_compute: gpd.GeoDataFrame | None = None
    graph: nx.Graph | None = None
    nodes_xy: np.ndarray | None = None       # (N,2) 投影平面坐标，用于最近节点向量化查找
    nodes_lonlat: np.ndarray | None = None   # (N,2) 经纬度
    pois: gpd.GeoDataFrame | None = None
    pois_compute: gpd.GeoDataFrame | None = None
    pois_xy: np.ndarray | None = None        # (N,2) POI 投影坐标，用于最近 POI 向量化查找
    pois_names: list[str] = field(default_factory=list)
    alias: dict[str, str] = field(default_factory=dict)
    study: dict = field(default_factory=dict)

    def zone_names(self) -> dict[str, str]:
        return {k: v[1] for k, v in ZONE_LAYERS.items()}


def _read_geojson(path, name: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_STORAGE)
    gdf = gdf.to_crs(CRS_STORAGE)
    # 生成稳定内部 ID：源数据的 Id 字段不可用（全部为 0 / null）
    gdf['_zid'] = [f'{name[:2].upper()}-{i:04d}' for i in range(1, len(gdf) + 1)]
    gdf['_seq'] = range(1, len(gdf) + 1)
    return gdf


def _build_graph(roads: gpd.GeoDataFrame) -> tuple[nx.Graph, np.ndarray, np.ndarray]:
    """从路网构建无向图。

    权重用**投影平面欧氏距离（米）**，不用经纬度 —— 后者会把经度方向压扁，
    导致东西向路径被系统性低估。
    数据中 oneway 全部为空，故使用无向图，不为方向性做过度设计。
    """
    g = nx.Graph()
    node_xy: dict[tuple, tuple] = {}

    roads_c = roads.to_crs(CRS_COMPUTE)
    for geom in roads_c.geometry:
        if geom is None or geom.is_empty:
            continue
        lines = geom.geoms if geom.geom_type == 'MultiLineString' else [geom]
        for line in lines:
            coords = list(line.coords)
            for i in range(len(coords) - 1):
                (x1, y1), (x2, y2) = coords[i], coords[i + 1]
                if (x1, y1) == (x2, y2):
                    continue
                d = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                g.add_edge((x1, y1), (x2, y2), weight=d)
                node_xy[(x1, y1)] = (x1, y1)

    # 反投影回经纬度，供对外输出
    from pyproj import Transformer
    inv = Transformer.from_crs(CRS_COMPUTE, CRS_STORAGE, always_xy=True)
    nodes = list(g.nodes)
    xy = np.array(nodes, dtype=float)
    lonlat = np.array([inv.transform(x, y) for x, y in nodes], dtype=float)
    return g, xy, lonlat


@lru_cache(maxsize=1)
def get_store() -> DataStore:
    st = DataStore()

    st.study = json.loads((CONFIG_DIR / 'study_area.json').read_text(encoding='utf-8'))

    for key, (fn, _cn) in ZONE_LAYERS.items():
        gdf = _read_geojson(DATA_DIR / fn, key)
        st.zones[key] = gdf
        st.zones_compute[key] = gdf.to_crs(CRS_COMPUTE)

    st.roads = _read_geojson(DATA_DIR / 'road_network.geojson', 'road')
    st.graph, st.nodes_xy, st.nodes_lonlat = _build_graph(st.roads)
    st.roads_compute = st.roads.to_crs(CRS_COMPUTE)

    poi_path = DATA_DIR / 'campus_poi.geojson'
    if poi_path.exists():
        st.pois = _read_geojson(poi_path, 'poi')
        st.pois_compute = st.pois.to_crs(CRS_COMPUTE)
        st.pois_xy = np.array(
            [[g.x, g.y] for g in st.pois_compute.geometry], dtype=float)
        st.pois_names = list(st.pois['name'])
    alias_path = CONFIG_DIR / 'poi_alias.json'
    if alias_path.exists():
        st.alias = json.loads(alias_path.read_text(encoding='utf-8'))

    return st


def origin() -> Point:
    """投影平面的假原点，仅用于类型提示场景"""
    return Point(0, 0)
