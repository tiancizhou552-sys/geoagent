# -*- coding: utf-8 -*-
"""
build_poi.py — 从 OSM 原始数据生成校园 POI 图层与别名表

输入：data/raw/overpass_*.json（由 fetch_osm.py 抓取并缓存）
输出：data/campus_poi.geojson   标准化 POI 点图层
      config/poi_alias.json    别名 → poi_id 查找表

设计原则：
  1. 别名只做「可推导的变体」（数字格式、规范简称），不做臆测映射
  2. 同名且相距 <50m 的 POI 合并，避免「菜鸟驿站」出现三条
  3. 研究区外的 POI 一律剔除（相邻学校、跨区地物）
"""
import json, os, re, math, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, 'data', 'raw')
OUT_POI = os.path.join(ROOT, 'data', 'campus_poi.geojson')
OUT_ALIAS = os.path.join(ROOT, 'config', 'poi_alias.json')
STUDY = os.path.join(ROOT, 'config', 'study_area.json')

study = json.load(open(STUDY, encoding='utf-8'))
B = study['bbox']
W, S, E, N = B['west'], B['south'], B['east'], B['north']

# 噪声名单：无意义名称 / 相邻学校 / 施工临时设施
BLACKLIST_EXACT = {
    '#1', '#2', 'Lanhuayuan', '动物房', '泵房(废弃)', '空调机房',
    '湖北生物科技职业学院', '华中师大一附中光谷汤逊湖学校（北校区）',
    '武汉市光谷汤逊湖高级中学', '华中农业大学附属学校',
}
BLACKLIST_SUBSTR = ('旧城改造', '项目部', '指挥部')

# 口语化别名词典：不可推导，需人工维护（宁缺勿滥，避免歧义）
CURATED_ALIAS = {
    '图书馆': ['华农图书馆', '校图书馆', '老图书馆'],
    '校医院': ['华农医院', '华中农业大学医院'],
    '大学生活动中心': ['大活'],
    '逸夫楼': ['逸夫教学楼'],
    '博物馆': ['华中农业大学博物馆'],
}

CN = '零一二三四五六七八九十'


def cn_num(n: int) -> str:
    if n <= 10:
        return CN[n]
    if n < 20:
        return '十' + CN[n - 10]
    return CN[n // 10] + '十' + (CN[n % 10] if n % 10 else '')


def distance_m(a, b):
    """经纬度近似距离（米），校园尺度足够"""
    dx = (a[0] - b[0]) * 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = (a[1] - b[1]) * 110574
    return math.hypot(dx, dy)


def load_raw():
    els = {}
    if not os.path.isdir(RAW_DIR):
        sys.exit('缺少 %s，请先运行 scripts/fetch_osm.py' % RAW_DIR)
    for fn in sorted(os.listdir(RAW_DIR)):
        if not fn.endswith('.json'):
            continue
        d = json.load(open(os.path.join(RAW_DIR, fn), encoding='utf-8'))
        for e in d.get('elements', []):
            els[(e['type'], e['id'])] = e
    return els


def categorize(name, tags):
    """按优先级把 OSM 标签映射到面向 Agent 的类别（顺序即优先级）"""
    am = tags.get('amenity', '')
    le = tags.get('leisure', '')
    sh = tags.get('shop', '')
    bd = tags.get('building', '')
    has = lambda *ks: any(k in name for k in ks)

    if am == 'library' or has('图书馆'):
        return 'library', '图书馆'
    if has('食堂'):
        return 'canteen', '食堂'
    if am in ('restaurant', 'fast_food', 'cafe') or sh in ('butcher', 'seafood', 'greengrocer'):
        return 'dining', '餐饮'
    if am == 'charging_station' or has('充电站', '充电桩'):
        return 'charging', '充电站'
    if (bd == 'dormitory' or re.search(r'(荟园|北苑)\d+', name)
            or re.fullmatch(r'\d+栋', name) or has('公寓')):
        return 'dorm', '宿舍'
    if has('教学楼', '逸夫楼', '景园楼', '人文社科楼', '创业楼', '综合楼'):
        return 'teaching', '教学楼'
    if am in ('clinic', 'hospital', 'veterinary') or has('医院', '门诊', '急诊', '住院'):
        return 'medical', '医疗'
    if am in ('bank', 'atm') or has('银行', '储蓄'):
        return 'bank', '银行'
    if am in ('post_office', 'post_box') or has('驿站', '快递'):
        return 'express', '快递驿站'
    if am in ('supermarket', 'convenience') or has('超市', '教超', '罗森', '书店'):
        return 'shop', '超市便利店'
    if am == 'fitness_centre' or le == 'fitness_centre' or has('体育', '活动中心', '游泳', '健身'):
        return 'sports', '体育健身'
    if am in ('college', 'research_institute') or has('学院', '实验室', '研究中心', '研究所',
                                                      '实验楼', '训练中心', '楼', '中心'):
        return 'academic', '学院与科研'
    if (am in ('townhall', 'police', 'public_building', 'office') or bd == 'office'
            or has('行政', '派出所', '街道', '幼儿园', '学校', '驾校', '打印', '博物馆', '艺术馆')):
        return 'service', '行政服务'
    return 'other', '其他'


def make_aliases(name, cat):
    """生成别名：可推导的变体 + 少量人工维护的口语别名"""
    al = {name}
    m = re.match(r'^(荟园|北苑)(\d+)栋$', name)
    if m:
        pre, num = m.group(1), int(m.group(2))
        short = '荟' if pre == '荟园' else '北'
        for s in (pre, short):
            al.add('%s%d栋' % (s, num))
            al.add('%s%s栋' % (s, cn_num(num)))
            al.add('%s%d' % (s, num))
            al.add('%s%s' % (s, cn_num(num)))
    m = re.match(r'^第(.+)教学楼$', name)
    if m:
        c = m.group(1)
        al.add('%s教' % c)
        al.add('教%s' % c)
    if name.endswith('食堂'):
        al.add(name[:-2] + '餐厅')
    al.update(CURATED_ALIAS.get(name, []))
    return sorted(al)


def main():
    els = load_raw()
    pts = []
    for e in els.values():
        tags = e.get('tags') or {}
        name = (tags.get('name') or '').strip()
        if not name:
            continue
        if name in BLACKLIST_EXACT or any(s in name for s in BLACKLIST_SUBSTR):
            continue
        if e['type'] == 'node':
            lat, lon = e.get('lat'), e.get('lon')
        else:
            c = e.get('center') or {}
            lat, lon = c.get('lat'), c.get('lon')
        if lat is None or lon is None:
            continue
        if not (W <= lon <= E and S <= lat <= N):
            continue
        cat, cat_cn = categorize(name, tags)
        pts.append({'name': name, 'lon': lon, 'lat': lat, 'cat': cat,
                    'cat_cn': cat_cn, 'osm': '%s/%s' % (e['type'], e['id'])})

    # 同名 <50m 合并
    groups = collections.defaultdict(list)
    for p in pts:
        groups[p['name']].append(p)
    merged, dropped = [], 0
    for name, group in groups.items():
        used = [False] * len(group)
        for i, p in enumerate(group):
            if used[i]:
                continue
            cluster = [p]
            used[i] = True
            for j in range(i + 1, len(group)):
                if not used[j] and distance_m((p['lon'], p['lat']),
                                              (group[j]['lon'], group[j]['lat'])) < 50:
                    cluster.append(group[j])
                    used[j] = True
            dropped += len(cluster) - 1
            lon = sum(c['lon'] for c in cluster) / len(cluster)
            lat = sum(c['lat'] for c in cluster) / len(cluster)
            merged.append({**p, 'lon': lon, 'lat': lat,
                           'merged': len(cluster), 'osm': cluster[0]['osm']})

    merged.sort(key=lambda p: (p['cat'], p['name']))
    features, alias, collisions = [], {}, []
    for idx, p in enumerate(merged, 1):
        pid = 'P%04d' % idx
        al = make_aliases(p['name'], p['cat'])
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [round(p['lon'], 7), round(p['lat'], 7)]},
            'properties': {
                'poi_id': pid, 'name': p['name'],
                'category': p['cat'], 'category_cn': p['cat_cn'],
                'aliases': al, 'merged_count': p['merged'], 'osm_ref': p['osm'],
            },
        })
        for a in al:
            if a in alias and alias[a] != pid:
                collisions.append((a, alias[a], pid))
                continue
            alias[a] = pid

    with open(OUT_POI, 'w', encoding='utf-8') as f:
        json.dump({'type': 'FeatureCollection',
                   'name': 'campus_poi',
                   'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}},
                   'features': features}, f, ensure_ascii=False, indent=1)
    with open(OUT_ALIAS, 'w', encoding='utf-8') as f:
        json.dump(alias, f, ensure_ascii=False, indent=1, sort_keys=True)

    print('原始候选 %d 个 → 合并同名近邻后 %d 个 POI（合并掉 %d 条重复）'
          % (len(pts), len(features), dropped))
    print('别名表条目: %d' % len(alias))
    if collisions:
        print('⚠ 别名冲突 %d 条（保留先注册者，后续需靠模糊匹配消歧）：' % len(collisions))
        for a, kept, skipped in collisions[:10]:
            print('   %-12s 保留 %s，忽略 %s' % (a, kept, skipped))
    print()
    by = collections.Counter(f['properties']['category_cn'] for f in features)
    print('类别分布:')
    for c, n in by.most_common():
        print('   %-10s %d' % (c, n))
    print()
    print('前 12 个 POI:')
    for f in features[:12]:
        p = f['properties']
        c = f['geometry']['coordinates']
        print('   %-6s %-10s %-16s %.6f, %.6f' % (p['poi_id'], p['category_cn'], p['name'], c[0], c[1]))
    print()
    print('写入:', OUT_POI)
    print('写入:', OUT_ALIAS)


if __name__ == '__main__':
    main()
