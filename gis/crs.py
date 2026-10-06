# -*- coding: utf-8 -*-
"""
CRS 转换。

设计原则：**转换只在这里发生**。上层（工具、Agent）一律用 WGS84 经纬度说话，
所有涉及"米"的计算先落到 EPSG:4547 再做，算完转回 WGS84 对外输出。

为什么必须这样：
  EPSG:4326 的单位是"度"，直接量距离会得到数量级错误的结果。
  1 度纬度 ≈ 111 km，1 度经度在武汉（30.47°N）≈ 95.7 km。
  把 300 米当成 300 度，缓冲区会覆盖整个中国。
"""
from functools import lru_cache

from pyproj import Transformer
from shapely.ops import transform as shapely_transform

from settings import CRS_COMPUTE, CRS_STORAGE


@lru_cache(maxsize=1)
def _fwd() -> Transformer:
    return Transformer.from_crs(CRS_STORAGE, CRS_COMPUTE, always_xy=True)


@lru_cache(maxsize=1)
def _inv() -> Transformer:
    return Transformer.from_crs(CRS_COMPUTE, CRS_STORAGE, always_xy=True)


def to_compute(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 经纬度 → 投影平面坐标（米）"""
    return _fwd().transform(lon, lat)


def to_wgs(x: float, y: float) -> tuple[float, float]:
    """投影平面坐标（米）→ WGS84 经纬度"""
    return _inv().transform(x, y)


def geom_to_compute(geom):
    """Shapely 几何：WGS84 → 投影平面"""
    return shapely_transform(lambda x, y, z=None: _fwd().transform(x, y), geom)


def geom_to_wgs(geom):
    """Shapely 几何：投影平面 → WGS84"""
    return shapely_transform(lambda x, y, z=None: _inv().transform(x, y), geom)


def length_m(geom) -> float:
    """几何长度（米）。输入应为 WGS84 几何。"""
    return geom_to_compute(geom).length


def area_m2(geom) -> float:
    """几何面积（㎡）。输入应为 WGS84 几何。"""
    return geom_to_compute(geom).area
