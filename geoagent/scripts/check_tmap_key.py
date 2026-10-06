# -*- coding: utf-8 -*-
"""一键检查腾讯地图 Key 的鉴权状态（不需要打开浏览器）。

用法：
    python scripts/check_tmap_key.py

原理：腾讯地图 GL JS 内部会用 key 去请求
    https://apikey.map.qq.com/mkey/index.php/mkey/check
本脚本直接打同一个接口，用官方文档里的示例 key 做对照，
所以能区分「你的 key 有问题」和「接口本身挑参数」。

判读：
    ✅ 鉴权通过            —— key 可用，底图能正常渲染
    ❌ 当前产品鉴权失败    —— key 有效但**没授权当前产品**，去控制台修
    ❌ 未找到 TMAP_KEY     —— 没配 key

边界说明：本脚本用 urllib 直接请求，虽然带了 Referer 头，但不等同于浏览器的
Referer/Origin 语义。所以可能出现「脚本说通过、网页仍报鉴权失败」——
那种情况基本就是**域名白名单**没放行 localhost / 127.0.0.1。

控制台修复路径（记不住就看页面上的提示框）：
    登录 https://lbs.qq.com/ → 控制台 → 应用管理 → 我的应用 → 找到该 Key → 设置
      1. 「启用产品」勾上 **JavaScript API GL**（有的版本叫 SDK / 地图 SDK）
      2. 「域名白名单」加 localhost 和 127.0.0.1（本地调试），或留空不限制
      3. 关掉「签名校验（SN）」—— 那是给 WebService 用的，前端 GL 用不了
      4. 确认 Key 状态是「启用中」
    保存后等 1~2 分钟再跑一次本脚本。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import TMAP_KEY   # noqa: E402

CHECK_URL = "https://apikey.map.qq.com/mkey/index.php/mkey/check"
DEMO_KEY = 'OB4BZ-D4W3U-B7VVO-4PJWW-6TKDJ-WPB77'   # 官方文档 hello world 的示例 key
REFERER = 'http://127.0.0.1:5001/'


def check(key: str) -> tuple[bool, str]:
    """调用腾讯鉴权接口。

    返回 (是否鉴权通过, 说明文字)。
    响应是 JSONP：mkeycb&&mkeycb({...})，成功时给 detail.cfg，
    失败时给 info.error + info.msg。注意**不能**简单地搜字符串 "error"——
    成功的响应体里也有 error 字段（值为 0）。
    """
    url = CHECK_URL + '?appid=jsapi_v3&key=' + urllib.parse.quote(key)
    req = urllib.request.Request(url, headers={'Referer': REFERER,
                                               'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode('utf-8', 'replace')

    m = re.search(r'\((.*)\)\s*$', body, re.S)
    if not m:
        return False, '响应格式异常：' + body[:160]
    try:
        data = json.loads(m.group(1))
    except Exception as e:                       # noqa: BLE001
        return False, 'JSON 解析失败(%s)：%s' % (e, body[:160])

    info = data.get('info') or {}
    if info.get('error'):
        return False, 'error=%s  %s' % (info.get('error'), info.get('msg', ''))
    if data.get('detail'):
        return True, 'detail.cfg 已下发'
    return False, '响应既无 detail 也无 info.error：' + body[:160]


def main() -> int:
    if not TMAP_KEY:
        print('❌ 未找到 TMAP_KEY —— 请在项目根目录 .env 里填上 TMAP_KEY=你的key')
        return 1
    print('TMAP_KEY 已配置（长度 %d），正在检查…' % len(TMAP_KEY))
    print()

    # ① 对照：官方文档里的示例 key。它要是也失败，说明是网络/接口问题，不是你的 key 的问题。
    try:
        demo_ok, demo_msg = check(DEMO_KEY)
    except Exception as e:                       # noqa: BLE001
        print('⚠️ 连不上腾讯鉴权接口，先检查网络：%s: %s' % (type(e).__name__, e))
        return 2
    print('对照 · 官方示例 key ：%s（%s）' % ('鉴权通过 ✅' if demo_ok else '失败 ❌', demo_msg))

    # ② 本项目的 key
    try:
        mine_ok, mine_msg = check(TMAP_KEY)
    except Exception as e:                       # noqa: BLE001
        print('❌ 请求失败：%s: %s' % (type(e).__name__, e))
        return 2
    print('本项目 · TMAP_KEY  ：%s（%s）' % ('鉴权通过 ✅' if mine_ok else '失败 ❌', mine_msg))
    print()

    if mine_ok:
        print('→ key 可用，底图能正常渲染。')
        return 0

    print('→ key 本身有效，但**没有授权当前产品（JavaScript API GL）**。')
    print('  按文件顶部的「控制台修复路径」改配置，改完再跑一次本脚本，')
    print('  看到「鉴权通过」再去刷新网页。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
