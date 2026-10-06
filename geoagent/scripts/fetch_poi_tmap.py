# -*- coding: utf-8 -*-
"""
fetch_poi_tmap.py — 用腾讯地图 WebService「关键词搜索」抓取校园 POI 原始数据

用途：build_poi_from_tmap.py 的上游。把腾讯返回的 GCJ-02 结果**转成 WGS84**
      后缓存到 data/raw_tmap/，使 POI 生成过程可离线复现。

为什么要替换 OSM：
    OSM 在校园里只有 8 个具名教学楼，华中农业大学第四教学楼（四教）根本不存在，
    而四教是学生口语里最常用的地名之一。这是**数据源缺失**，不是抓取配置问题，
    换任何 Overpass 查询都补不上。

边界怎么定（用户已确认：完全贴合、零缓冲）：
    1. 粗筛 —— 请求时给腾讯 rectangle(lat,lng,lat,lng)（西南角 → 东北角），
       用校园边界的外接矩形先把候选压下来，省请求量；
    2. 精筛 —— 拿到结果后逐点转 WGS84，再用 data/campus_boundary.geojson 的
       多边形做 polygon.contains(point)，严格取交集。零 buffer。

鉴权：Key 来自项目根目录 settings.py 的 TMAP_KEY（值读自 .env）。
      腾讯 WebService 需在控制台把「WebServiceAPI」打开，域名白名单可留空。
      注意：必须用项目 venv 运行（settings.py 依赖 python-dotenv）。

重跑：python scripts/fetch_poi_tmap.py            （已有缓存则跳过）
      python scripts/fetch_poi_tmap.py --force    （强制重新联网）
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RAW_DIR = os.path.join(ROOT, 'data', 'raw_tmap')
BOUNDARY = os.path.join(ROOT, 'data', 'campus_boundary.geojson')

SEARCH_URL = 'https://apis.map.qq.com/ws/place/v1/search'
PAGE_SIZE = 20          # 腾讯硬上限
MAX_PAGES = 3           # 单关键词最多取 20*3 = 60 条，总硬上限 200
SLEEP = 0.4             # 关键词之间的间隔，避免触发频控

# 抓取关键词（校园语境）。每个关键词独立翻页，结果按 poi_id 去重。
#
# ⚠ 教训：腾讯的「关键词搜索」按相关度排序、单关键词硬上限 200 条、
#   实测只返回 ≤60 条。"教学楼" 这种泛词几乎抓不到东西（实测只回 1 条），
#   而学生口语里的「四教」也抓不全。必须**逐个编号楼点名**才可靠：
#   "第四教学楼" → 6 条（含 A区/B座/C区/B区），"第一教学楼" → 6 条。
KEYWORDS = [
    # —— 编号教学楼：逐个点名，这是「四教」能进来的唯一可靠方式 ——
    '第一教学楼', '第二教学楼', '第三教学楼', '第四教学楼', '第五教学楼',
    '第一综合楼', '第二综合楼', '第三综合楼', '第四综合楼',
    # —— 具名楼栋 ——
    '逸夫楼', '景园楼', '人文社科楼', '创业楼', '园林楼', '新工科楼',
    # —— 公共设施 ——
    '图书馆', '博物馆', '校史馆', '活动中心', '行政楼', '办公楼',
    # —— 生活服务 ——
    '食堂', '餐厅', '美食城', '超市', '便利店', '银行', 'ATM',
    '快递', '驿站', '打印', '理发', '咖啡', '水果',
    # —— 体育与医疗 ——
    '体育场', '体育馆', '操场', '校医院', '医院',
    # —— 学院与住宿 ——
    # ⚠ 宿舍必须**按苑区点名**才能逐栋抓全。泛词「宿舍/公寓」只会返回「荟园大学生公寓区」
    #   这种整块区域，拿不到「荟园10栋」——而学生口语天天说「荟十」。
    #   实测「华中农业大学荟园」→ 荟园1~21栋（含21A/21B）、「…北苑」→ 北苑1~15栋，
    #   南苑 1~32 栋、博园 1~16 栋、宝积苑 1~23 栋、西苑 33~71 栋（教工）。
    '华中农业大学荟园', '华中农业大学北苑', '华中农业大学南苑',
    '华中农业大学博园', '华中农业大学宝积苑', '华中农业大学西苑',
    '华农荟园', '华农北苑', '华农南苑', '华农博园', '华农西苑',
    '学院', '实验楼',
    # —— 交通 ——
    '校门', '广场',
]


def _load_settings_key() -> str:
    """从项目根 settings.py 读 TMAP_KEY（依赖 .env，需用项目 venv 运行）"""
    try:
        from settings import TMAP_KEY  # noqa: WPS433
    except Exception as e:
        sys.exit('无法导入 settings.TMAP_KEY（%s）。请用项目 venv 运行本脚本：'
                 r'geoagent\.venv\Scripts\python.exe scripts\fetch_poi_tmap.py' % e)
    if not TMAP_KEY:
        sys.exit('TMAP_KEY 为空。请在项目根目录 .env 里配置 TMAP_KEY=xxx。')
    return TMAP_KEY


def _load_boundary():
    """读校园边界多边形，返回 (shapely_polygon, bbox_south_west_north_east)"""
    from shapely.geometry import shape
    gj = json.load(open(BOUNDARY, encoding='utf-8'))
    poly = shape(gj['features'][0]['geometry'])
    if not poly.is_valid:
        poly = poly.buffer(0)
    minx, miny, maxx, maxy = poly.bounds
    return poly, (miny, minx, maxy, maxx)   # south, west, north, east


def search(keyword: str, rect, key: str, page_index: int):
    """单次搜索请求。rect = (lat1,lng1,lat2,lng2) 西南→东北"""
    boundary = 'rectangle(%s,%s,%s,%s)' % (
        round(rect[0], 6), round(rect[1], 6), round(rect[2], 6), round(rect[3], 6))
    params = {
        'key': key,
        'keyword': keyword,
        'boundary': boundary,
        'page_size': PAGE_SIZE,
        'page_index': page_index,
        'added_fields': 'category_code',   # 带回分类编码，便于 build 阶段归类
        'output': 'json',
    }
    url = SEARCH_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'GeoAgent/0.1'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_keyword(keyword: str, rect, key: str):
    """抓一个关键词的全部分页，返回 data 列表"""
    out = []
    for page in range(1, MAX_PAGES + 1):
        try:
            res = search(keyword, rect, key, page)
        except Exception as e:
            print('   ! %s 第 %d 页异常: %s' % (keyword, page, type(e).__name__))
            break
        status = res.get('status')
        if status != 0:
            # 0=成功；其它都当失败处理并打印腾讯原文，便于定位鉴权/参数问题
            print('   ! %s 第 %d 页 status=%s %s'
                  % (keyword, page, status, res.get('message')))
            break
        data = res.get('data') or []
        out.extend(data)
        if len(data) < PAGE_SIZE:       # 没满一页 = 最后一页
            break
        time.sleep(0.2)
    return out


def main():
    from shapely.geometry import Point
    from gis.gcj import gcj02_to_wgs84

    force = '--force' in sys.argv
    os.makedirs(RAW_DIR, exist_ok=True)
    out_path = os.path.join(RAW_DIR, 'tmap_poi_raw.json')
    if os.path.exists(out_path) and not force:
        n = len(json.load(open(out_path, encoding='utf-8')).get('results', []))
        print('已有缓存 %d 条，跳过。要重新抓取请加 --force' % n)
        print('接下来运行: python scripts/build_poi_from_tmap.py')
        return

    key = _load_settings_key()
    poly, rect = _load_boundary()
    print('校园边界: 外接矩形 S,W,N,E = %s' % (rect,))
    print('关键词 %d 个，每词最多 %d 页 × %d 条\n' % (len(KEYWORDS), MAX_PAGES, PAGE_SIZE))

    seen = {}
    for kw in KEYWORDS:
        hits = fetch_keyword(kw, rect, key)
        new = 0
        for it in hits:
            loc = it.get('location') or {}
            lat_g, lng_g = loc.get('lat'), loc.get('lng')
            if lat_g is None or lng_g is None:
                continue
            # 腾讯给的是 GCJ-02 → 先转 WGS84 再判边界（边界本身是 WGS84）
            lng_w, lat_w = gcj02_to_wgs84(lng_g, lat_g)
            if not poly.contains(Point(lng_w, lat_w)):
                continue
            uid = it.get('id') or '%s|%.6f,%.6f' % (
                it.get('title'), lng_w, lat_w)
            if uid in seen:
                seen[uid]['keywords'].append(kw)
                continue
            seen[uid] = {
                'id': it.get('id'),
                'title': it.get('title'),
                'address': it.get('address'),
                'category': it.get('category'),
                'category_code': (it.get('ad_info') or {}).get('category_code')
                                 or it.get('category_code'),
                'lat_wgs': round(lat_w, 7),
                'lng_wgs': round(lng_w, 7),
                'lat_gcj': lat_g,
                'lng_gcj': lng_g,
                'keywords': [kw],
            }
            new += 1
        print('%-8s 命中 %2d 条 → 累计有效 %d（本词新增 %d）'
              % (kw, len(hits), len(seen), new))
        time.sleep(SLEEP)

    results = sorted(seen.values(), key=lambda x: (x['category'] or '', x['title'] or ''))
    payload = {
        'source': 'Tencent Maps WebService place/v1/search',
        'coord_note': 'lat_wgs/lng_wgs 已由 GCJ-02 转换；lat_gcj/lng_gcj 为腾讯原始值',
        'boundary': 'campus_boundary.geojson (polygon.contains, 零缓冲)',
        'count': len(results),
        'results': results,
    }
    json.dump(payload, open(out_path, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('\n写入: %s（%d 条）' % (out_path, len(results)))
    print('接下来运行: python scripts/build_poi_from_tmap.py')


if __name__ == '__main__':
    main()
