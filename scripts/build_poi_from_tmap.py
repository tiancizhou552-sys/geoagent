# -*- coding: utf-8 -*-
"""
build_poi_from_tmap.py — 把腾讯抓取的 POI 原始数据生成为校园 POI 图层与别名表

输入：data/raw_tmap/tmap_poi_raw.json（由 fetch_poi_tmap.py 抓取并缓存）
输出：data/campus_poi.geojson   标准化 POI 点图层（**全量替换** OSM 版）
      config/poi_alias.json    别名 → poi_id 查找表

输出格式与 build_poi.py（OSM 版）**完全一致**，下游 loader/engine 无需改动：
      properties = poi_id / name / category / category_cn / aliases
                   / merged_count / osm_ref
      （osm_ref 在腾讯版里存 tmap:<id>，字段名保留以兼容 loader）

设计原则（与 OSM 版一脉相承）：
  1. 类别映射沿用同一套优先级，保证 category 取值集合不变
  2. 别名只做「可推导变体」（数字格式、第X教学楼→X教）+ 5 组人工口语别名
  3. 同名且相距 <50m 合并
  4. 抓到啥用啥，不臆造；边界外的点已在 fetch 阶段剔除
"""
import json
import os
import re
import math
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw_tmap', 'tmap_poi_raw.json')
OUT_POI = os.path.join(ROOT, 'data', 'campus_poi.geojson')
OUT_ALIAS = os.path.join(ROOT, 'config', 'poi_alias.json')

# 噪声名单：与 OSM 版一致，另外补几条腾讯会返回的无关/重复项
BLACKLIST_EXACT = {
    '#1', '#2', '动物房', '泵房(废弃)', '空调机房',
    '湖北生物科技职业学院', '华中师大一附中光谷汤逊湖学校（北校区）',
    '武汉市光谷汤逊湖高级中学', '华中农业大学附属学校',
}
BLACKLIST_SUBSTR = ('旧城改造', '项目部', '指挥部')

# 停车类一律不进 POI 层：停车/禁停已由 parking.geojson / noparking.geojson 独立成层，
# 腾讯返回的「XX停车场」是**重名批次**（「内部地面停车场」一次就 12 条同名），
# 且名字里带「楼」会被误判成「学院与科研」。它们不是地标，只会污染地名解析。
PARKING_SUBSTR = ('停车场', '停车位', '停车点', '泊车', '地库', '车库')

# 口语化别名词典：不可推导，需人工维护（与 OSM 版保持一致）
CURATED_ALIAS = {
    '图书馆': ['华农图书馆', '校图书馆', '老图书馆'],
    '校医院': ['华农医院', '华中农业大学医院'],
    '大学生活动中心': ['大活'],
    '逸夫楼': ['逸夫教学楼'],
    '博物馆': ['华中农业大学博物馆'],
}

CN = '零一二三四五六七八九十'

# 宿舍苑区：全覆盖（含「南苑小区」这种带「小区」中缀的写法）
PRECINCTS = ('荟园', '北苑', '南苑', '博园', '宝积苑', '西苑')
DORM_RE = re.compile(r'(%s)(?:小区)?-?\d+' % '|'.join(PRECINCTS))
DORM_TAIL_RE = re.compile(
    r'^(%s)(?:小区)?-?(\d+)(?:栋|号楼)?([AB])?$' % '|'.join(PRECINCTS))


def cn_num(n: int) -> str:
    if n <= 10:
        return CN[n]
    if n < 20:
        return '十' + CN[n - 10]
    return CN[n // 10] + '十' + (CN[n % 10] if n % 10 else '')


def distance_m(a, b):
    dx = (a[0] - b[0]) * 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = (a[1] - b[1]) * 110574
    return math.hypot(dx, dy)


def _strip_campus_prefix(name: str) -> str:
    """腾讯标题常带学校前缀，如「华中农业大学第四教学楼」→「第四教学楼」。
    这样别名规则（第X教学楼→X教）才能命中；但别名字典里仍保留原名。"""
    for p in ('华中农业大学', '华中农大', '华农'):
        if name.startswith(p) and len(name) > len(p) + 1:
            return name[len(p):]
    return name


def categorize(name, category, code):
    """按优先级把腾讯分类映射到面向 Agent 的类别。
    先看腾讯自带的 category/category_code（更权威），再看名称关键词兜底。
    返回值集合与 OSM 版一致，保证前端/工具不受影响。"""
    c = category or ''
    code_s = str(code or '')
    has = lambda *ks: any(k in name for k in ks)

    if '图书馆' in c or has('图书馆'):
        return 'library', '图书馆'
    if '食堂' in c or has('食堂'):
        return 'canteen', '食堂'
    if c.startswith('美食') or '餐厅' in c or '餐饮' in c or has('餐厅', '饭店', '小吃', '奶茶', '咖啡'):
        return 'dining', '餐饮'
    if '充电' in c or has('充电站', '充电桩'):
        return 'charging', '充电站'
    # 宿舍：腾讯的苑区写法有「荟园10栋」「南苑小区10栋」「博园-12栋」「宝积苑4栋」等，
    # 必须覆盖全部 6 个苑区 + 可选的「小区」中缀 + 可选的连字符。
    if '宿舍' in c or '住宅' in c or DORM_RE.search(name) \
            or re.fullmatch(r'\d+栋', name) or has('公寓', '宿舍'):
        return 'dorm', '宿舍'
    if is_teaching(name) or '教学楼' in c or has('景园楼', '人文社科楼', '创业楼', '综合楼'):
        return 'teaching', '教学楼'
    if '医院' in c or '诊所' in c or '医疗' in c or has('医院', '门诊', '急诊', '住院', '校医院'):
        return 'medical', '医疗'
    if '银行' in c or '金融' in c or 'ATM' in c or has('银行', '储蓄', 'ATM'):
        return 'bank', '银行'
    if '物流' in c or '快递' in c or has('驿站', '快递', '菜鸟', '顺丰', '京东'):
        return 'express', '快递驿站'
    if '超市' in c or '便利店' in c or '购物' in c or has('超市', '教超', '罗森', '书店', '便利店'):
        return 'shop', '超市便利店'
    if '体育' in c or '健身' in c or '运动' in c or has('体育', '活动中心', '游泳', '健身', '操场'):
        return 'sports', '体育健身'
    if '科研' in c or '学院' in c or has('学院', '实验室', '研究中心', '研究所', '实验楼', '训练中心',
                                          '楼', '中心'):
        return 'academic', '学院与科研'
    if '政府' in c or '行政' in c or has('行政', '派出所', '街道', '幼儿园', '学校', '驾校',
                                          '打印', '博物馆', '艺术馆', '邮政', '电信', '移动', '联通',
                                          '通讯社', '开水房'):
        return 'service', '行政服务'
    return 'other', '其他'


def is_teaching(name: str) -> bool:
    return any(k in name for k in ('教学楼', '逸夫楼', '景园楼', '人文社科楼', '创业楼', '综合楼'))


def make_aliases(name, cat):
    al = {name}
    base = _strip_campus_prefix(name)
    if base != name:
        al.add(base)      # 去掉「华中农业大学」前缀后的短名，如「图书馆」「第四教学楼A区-东门」

    # 宿舍逐栋别名：腾讯的写法很杂 —— 「华中农业大学荟园10栋」「华农荟园-10栋」
    # 「华中农业大学南苑小区10栋」「华中农业大学博园-B栋」「荟园21栋A」。
    # 统一抽出 (苑区, 序号, 单元) 再生成变体，学生口语「荟十」「荟10」「荟园10」都要命中。
    m = DORM_TAIL_RE.match(base)
    if m:
        pre, num, suffix = m.group(1), int(m.group(2)), (m.group(3) or '')
        short = {'荟园': '荟', '北苑': '北', '南苑': '南', '博园': '博',
                 '宝积苑': '宝', '西苑': '西'}[pre]
        for s in (pre, short):
            for tail in ('%d栋' % num, '%s栋' % cn_num(num), '%d' % num, cn_num(num)):
                al.add('%s%s%s' % (s, tail, suffix))
            if suffix:
                al.add('%s%d栋%s' % (s, num, suffix))
    # 兼容旧格式（无苑区前缀的「荟园10栋」已由上一条覆盖）
    m = re.match(r'^(荟园|北苑)(\d+)栋$', base)
    if m:
        pre, num = m.group(1), int(m.group(2))
        short = '荟' if pre == '荟园' else '北'
        for s in (pre, short):
            al.add('%s%d栋' % (s, num))
            al.add('%s%s栋' % (s, cn_num(num)))
            al.add('%s%d' % (s, num))
            al.add('%s%s' % (s, cn_num(num)))

    # 第X教学楼 → X教 / 教X （用户点名要的「四教」就靠这条）
    for src in (base, name):
        m = re.match(r'^第([一二三四五六七八九十\d]+)教学楼', src)
        if m:
            c = m.group(1)
            al.add('%s教' % c)
            al.add('教%s' % c)
            al.add('第%s教学楼' % c)
        m2 = re.match(r'^([一二三四五六七八九十\d]+)教$', src)
        if m2:
            al.add('第%s教学楼' % m2.group(1))

    # 「食堂」与「餐厅」双向：腾讯多写「XX餐厅」，学生口语说「XX食堂」
    for src in (base, name):
        if src.endswith('食堂'):
            al.add(src[:-2] + '餐厅')
        elif src.endswith('餐厅'):
            al.add(src[:-2] + '食堂')
    al.update(CURATED_ALIAS.get(base, []))
    al.update(CURATED_ALIAS.get(name, []))
    return sorted(al)


def main():
    if not os.path.exists(RAW):
        sys.exit('缺少 %s，请先运行 scripts/fetch_poi_tmap.py' % RAW)
    raw = json.load(open(RAW, encoding='utf-8'))
    results = raw.get('results', [])
    if not results:
        sys.exit('原始数据为空，请重新抓取。')

    pts = []
    for it in results:
        name = (it.get('title') or '').strip()
        if not name:
            continue
        if name in BLACKLIST_EXACT or any(s in name for s in BLACKLIST_SUBSTR):
            continue
        if any(s in name for s in PARKING_SUBSTR):
            continue
        lon, lat = it.get('lng_wgs'), it.get('lat_wgs')
        if lon is None or lat is None:
            continue
        cat, cat_cn = categorize(name, it.get('category'), it.get('category_code'))
        pts.append({'name': name, 'lon': lon, 'lat': lat, 'cat': cat, 'cat_cn': cat_cn,
                    'ref': 'tmap:%s' % (it.get('id') or '')})

    # 同名 <50m 合并（与 OSM 版同逻辑）
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
                           'merged': len(cluster), 'ref': cluster[0]['ref']})

    merged.sort(key=lambda p: (p['cat'], p['name']))
    features, alias, collisions = [], {}, []
    for idx, p in enumerate(merged, 1):
        pid = 'P%04d' % idx
        al = make_aliases(p['name'], p['cat'])
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point',
                         'coordinates': [round(p['lon'], 7), round(p['lat'], 7)]},
            'properties': {
                'poi_id': pid, 'name': p['name'],
                'category': p['cat'], 'category_cn': p['cat_cn'],
                'aliases': al, 'merged_count': p['merged'], 'osm_ref': p['ref'],
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
                   'source': 'Tencent Maps WebService (scripts/fetch_poi_tmap.py)',
                   'crs': {'type': 'name',
                           'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}},
                   'features': features}, f, ensure_ascii=False, indent=1)
    with open(OUT_ALIAS, 'w', encoding='utf-8') as f:
        json.dump(alias, f, ensure_ascii=False, indent=1, sort_keys=True)

    print('腾讯原始 %d 条 → 有效 %d 个 POI（合并掉 %d 条重复）'
          % (len(results), len(features), dropped))
    print('别名表条目: %d' % len(alias))
    if collisions:
        print('⚠ 别名冲突 %d 条（保留先注册者）：' % len(collisions))
        for a, kept, skipped in collisions[:10]:
            print('   %-12s 保留 %s，忽略 %s' % (a, kept, skipped))
    print()
    by = collections.Counter(f['properties']['category_cn'] for f in features)
    print('类别分布:')
    for c, n in by.most_common():
        print('   %-10s %d' % (c, n))
    print()
    # 自检：四教是否进来了
    for kw in ('四教', '3教', '三教'):
        if kw in alias:
            print('✓ 别名命中: %s → %s' % (kw, alias[kw]))
    print()
    print('写入:', OUT_POI)
    print('写入:', OUT_ALIAS)


if __name__ == '__main__':
    main()
