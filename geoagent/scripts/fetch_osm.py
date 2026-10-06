# -*- coding: utf-8 -*-
"""
fetch_osm.py — 从 OpenStreetMap Overpass API 抓取校园 POI 原始数据

用途：build_poi.py 的上游。抓取结果缓存到 data/raw/，使 POI 生成过程可离线复现。
重跑：python scripts/fetch_osm.py            （覆盖缓存）
      python scripts/fetch_osm.py --force    （强制重新联网抓取）

注意：Overpass 是公共免费服务，请勿高频调用。缓存存在时默认跳过联网。
"""
import json, os, sys, urllib.request, urllib.parse, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, 'data', 'raw')
STUDY = os.path.join(ROOT, 'config', 'study_area.json')

study = json.load(open(STUDY, encoding='utf-8'))
b = study['bbox']
BBOX = '%s,%s,%s,%s' % (b['south'], b['west'], b['north'], b['east'])

ENDPOINTS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
]

JOBS = {
    'overpass_amenity.json': f"""
[out:json][timeout:100];
(
  node["amenity"]({BBOX});
  way["amenity"]({BBOX});
  node["building"="dormitory"]({BBOX});
  way["building"="dormitory"]({BBOX});
  node["leisure"]({BBOX});
  way["leisure"]({BBOX});
  node["shop"]({BBOX});
);
out center tags;
""",
    'overpass_building.json': f"""
[out:json][timeout:100];
(
  way["building"]["name"]({BBOX});
  node["amenity"="library"]({BBOX});
  way["amenity"="library"]({BBOX});
  node["amenity"="canteen"]({BBOX});
  way["amenity"="canteen"]({BBOX});
  way["amenity"="university"]({BBOX});
  node["amenity"="university"]({BBOX});
  way["building"="dormitory"]({BBOX});
);
out center tags;
""",
}


def fetch(query, retries=2):
    last = None
    for ep in ENDPOINTS:
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    ep,
                    data=urllib.parse.urlencode({'data': query}).encode('utf-8'),
                    headers={'User-Agent': 'GeoAgent/0.1 (campus accessibility project)'})
                with urllib.request.urlopen(req, timeout=150) as r:
                    return json.loads(r.read().decode('utf-8')), ep
            except Exception as e:
                last = '%s @ %s' % (type(e).__name__, ep)
                time.sleep(3)
    raise RuntimeError('全部端点失败：%s' % last)


def main():
    force = '--force' in sys.argv
    os.makedirs(RAW_DIR, exist_ok=True)
    for fn, q in JOBS.items():
        out = os.path.join(RAW_DIR, fn)
        if os.path.exists(out) and not force:
            n = len(json.load(open(out, encoding='utf-8')).get('elements', []))
            print('跳过（已有缓存 %d 要素）: %s' % (n, fn))
            continue
        print('抓取:', fn, flush=True)
        data, ep = fetch(q)
        json.dump(data, open(out, 'w', encoding='utf-8'), ensure_ascii=False)
        print('  ✓ %d 要素  (来源 %s)' % (len(data.get('elements', [])), ep))
        time.sleep(2)
    print('\n完成。接下来运行: python scripts/build_poi.py')


if __name__ == '__main__':
    main()
