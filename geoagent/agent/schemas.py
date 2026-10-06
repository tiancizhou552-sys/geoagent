# -*- coding: utf-8 -*-
"""
工具参数契约（Pydantic v2）。

一份定义同时产出两个东西：
  ① 给大模型看的 JSON Schema（含单位、取值域、枚举说明）
  ② 运行时的类型/范围校验

这是"三层防护"的第一层与第二层共用的一份真相来源 —— 手写 jsonschema 迟早会和校验逻辑漂移。

⚠️ 所有 Field 的 description 都会被原样送进模型上下文。写描述时假设读者是一个
   **从没见过这份代码的新同事**：不写单位，它就会猜。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from settings import K_MAX, K_MIN, RADIUS_MAX_M, RADIUS_MIN_M

Layer = Literal['parking', 'tempparking', 'noparking', 'road', 'poi', 'all']
ZoneType = Literal['all', 'parking', 'tempparking', 'noparking']
Predicate = Literal['intersects', 'within']
Mode = Literal['walk', 'bike']

LAYER_DOC = "目标图层：parking=可停区，tempparking=可暂停区(限时)，noparking=禁停区，road=路网，poi=兴趣点，all=全部停车区"


class PlaceMixin(BaseModel):
    """地名与坐标二选一。两个都传或都不传都会被拦下。"""

    place: str | None = Field(
        None,
        description="地名，必须来自校园内已知地点，如『图书馆』『荟园10栋』『荟十』『大活』『一教』。"
                    "不建议同时传 lon/lat。",
    )
    lon: float | None = Field(
        None, ge=-180, le=180,
        description="经度，WGS84 十进制度（本项目研究区约 114.33 ~ 114.37）。",
    )
    lat: float | None = Field(
        None, ge=-90, le=90,
        description="纬度，WGS84 十进制度（本项目研究区约 30.46 ~ 30.48）。",
    )

    @model_validator(mode='after')
    def _exactly_one_form(self):
        has_place = self.place is not None and str(self.place).strip() != ''
        has_lon = self.lon is not None
        has_lat = self.lat is not None
        if not has_place and not (has_lon and has_lat):
            raise ValueError('必须提供 place，或者同时提供 lon 和 lat')
        if has_place and (has_lon or has_lat):
            raise ValueError('place 与 lon/lat 只能二选一，请只提供其中一种')
        if has_lon != has_lat:
            raise ValueError('lon 与 lat 必须同时提供')
        return self


# ══════════════════════════════════════════════════════════
# 1) geocode
# ══════════════════════════════════════════════════════════

class GeocodeParams(BaseModel):
    place: str = Field(
        ...,
        description="要解析的地名，如『图书馆』『三食堂』『荟十』『北3』。"
                    "支持正式名、数字变体和口语简称。",
    )
    top_k: int = Field(
        3, ge=1, le=10,
        description="最多返回几个候选。当名称有歧义时，会返回多个候选供判断。",
    )


# ══════════════════════════════════════════════════════════
# 2) list_features
# ══════════════════════════════════════════════════════════

class ListFeaturesParams(BaseModel):
    kind: Literal['zone', 'poi'] = Field(
        'zone',
        description="kind=zone 列出停车区（可停/可暂停/禁停）；kind=poi 列出兴趣点（食堂、宿舍、教学楼等）。",
    )
    zone_type: ZoneType = Field(
        'all',
        description="仅 kind=zone 时生效：筛选停车区类型。",
    )
    category: str | None = Field(
        None,
        description="仅 kind=poi 时生效：按类别筛选，取值如 canteen(食堂)、dorm(宿舍)、"
                    "library(图书馆)、teaching(教学楼)、shop(超市便利店)、express(快递驿站)、"
                    "medical(医疗)、bank(银行)、charging(充电站)、sports(体育健身)、"
                    "academic(学院与科研)、service(行政服务)、dining(餐饮)。留空则返回全部类别。",
    )
    keyword: str | None = Field(
        None,
        description="仅 kind=poi 时生效：按名称包含的关键词过滤，如『食堂』『充电』。",
    )
    limit: int = Field(30, ge=1, le=200, description="最多返回多少条。")


# ══════════════════════════════════════════════════════════
# 3) buffer_query
# ══════════════════════════════════════════════════════════

class BufferQueryParams(PlaceMixin):
    radius_m: float = Field(
        ..., ge=RADIUS_MIN_M, le=RADIUS_MAX_M,
        description=f"缓冲区半径，单位【米】，取值 {RADIUS_MIN_M:.0f}~{RADIUS_MAX_M:.0f}。"
                    "不要传公里或度。",
    )
    layer: Layer = Field('parking', description=LAYER_DOC)
    predicate: Predicate = Field(
        'intersects',
        description="空间关系：intersects=与缓冲区有交集即可；within=必须完全落在缓冲区内。",
    )
    category: str | None = Field(
        None,
        description="仅 layer=poi 时生效：把范围查询限定到某个 POI 类别，"
                    "取值如 canteen(食堂)、charging(充电站)、dorm(宿舍)、shop(超市)、"
                    "express(快递驿站)、medical(医疗)、bank(银行)、teaching(教学楼)。"
                    "例如问『荟园食堂 500 米内有哪些充电站』时应传 layer=poi、category=charging。",
    )


# ══════════════════════════════════════════════════════════
# 4) nearest_facility
# ══════════════════════════════════════════════════════════

class NearestFacilityParams(PlaceMixin):
    layer: Layer = Field('parking', description=LAYER_DOC)
    k: int = Field(K_MIN, ge=K_MIN, le=K_MAX,
                   description=f"返回最近的前 k 个，取值 {K_MIN}~{K_MAX}。")
    category: str | None = Field(
        None,
        description="仅 layer=poi 时生效：限定 POI 类别，如 canteen、dorm、charging。留空则不限定。",
    )


# ══════════════════════════════════════════════════════════
# 5) check_zone
# ══════════════════════════════════════════════════════════

class CheckZoneParams(PlaceMixin):
    pass


# ══════════════════════════════════════════════════════════
# 6) route_plan
# ══════════════════════════════════════════════════════════

class RoutePlanParams(BaseModel):
    from_place: str | None = Field(None, description="起点地名，如『荟园10栋』。与 from_lon/from_lat 二选一。")
    from_lon: float | None = Field(None, ge=-180, le=180, description="起点经度 WGS84。")
    from_lat: float | None = Field(None, ge=-90, le=90, description="起点纬度 WGS84。")
    to_place: str | None = Field(None, description="终点地名，如『图书馆』。与 to_lon/to_lat 二选一。")
    to_lon: float | None = Field(None, ge=-180, le=180, description="终点经度 WGS84。")
    to_lat: float | None = Field(None, ge=-90, le=90, description="终点纬度 WGS84。")
    mode: Mode = Field(
        'walk',
        description="出行方式：walk=步行（按 72 米/分钟估算）；bike=骑行（按 250 米/分钟估算）。",
    )

    @staticmethod
    def _check_one(tag, place, lon, lat):
        has_place = place is not None and str(place).strip() != ''
        has_lon, has_lat = lon is not None, lat is not None
        if not has_place and not (has_lon and has_lat):
            raise ValueError(f'{tag}：必须提供地名，或同时提供经纬度')
        if has_place and (has_lon or has_lat):
            raise ValueError(f'{tag}：地名与经纬度只能二选一')
        if has_lon != has_lat:
            raise ValueError(f'{tag}：经度与纬度必须同时提供')

    @model_validator(mode='after')
    def _both_ends(self):
        self._check_one('起点', self.from_place, self.from_lon, self.from_lat)
        self._check_one('终点', self.to_place, self.to_lon, self.to_lat)
        return self


# 工具名 → (参数模型, 简介)。简介只用于文档，给模型的描述在 tools.py 里。
PARAM_MODELS: dict[str, type[BaseModel]] = {
    'geocode': GeocodeParams,
    'list_features': ListFeaturesParams,
    'buffer_query': BufferQueryParams,
    'nearest_facility': NearestFacilityParams,
    'check_zone': CheckZoneParams,
    'route_plan': RoutePlanParams,
}
