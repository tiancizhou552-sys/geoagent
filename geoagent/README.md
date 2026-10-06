# GeoAgent · 校园空间问答智能体

> 用一句自然语言完成「空间查询 → 分析 → 出图」。
> 把 GIS 的空间分析能力封装成大模型可调用的工具，让模型负责理解意图、让算法负责算出答案。

**能回答的问题**

- 「图书馆 300 米内有哪些可停区？」→ 25 个，最近的在图书馆东侧 19 米
- 「从荟园10栋到最近的食堂怎么走，要走多久？」→ 桃园食堂，沿路网 237 米，约 3.3 分钟
- 「荟十附近最近的充电站在哪？走过去要多久？」→ 荟园12北侧充电站，165 米，2.3 分钟
- 「荟园食堂门口能停车吗？」→ 不能，最近的**可停区**在东南 138 米
- 「荟十是哪儿？」→ 华中农业大学荟园10栋，园区东部

和普通问答机器人的区别：**这里的答案不是查表得来的，是实时算出来的。**
这句话决定了项目必须有工具层、必须有坐标系处理、必须有参数校验 —— 也正是我认为
LLM 应用落在地上比留在 demo 更值得做的部分。

---

## 目录

- [为什么做这个](#为什么做这个)
- [评测：编排方式值 30 个百分点](#评测编排方式值-30-个百分点)
- [架构](#架构)
- [坐标系：本项目最容易出错的地方](#坐标系本项目最容易出错的地方)
- [六个工具](#六个工具)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [API](#api)
- [数据](#数据)
- [设计取舍](#设计取舍)

---

## 为什么做这个

通用大模型的空间推理是不可靠的。你问它「图书馆 300 米内有哪些停车区」，它要么
编一个听起来很合理的答案，要么把 300 米理解成 300 度。这两个失败模式都源于同一件事：
**几何计算不该由语言模型来做。**

所以整个项目的设计出发点只有一句：**让模型只做它擅长的事（理解意图、编排步骤、组织语言），
把一切"算"的部分交给确定性的空间算子。** 具体落到三个约束上：

1. 模型只看到地名的**坐标**，看不到几何 —— 25 个多边形含数百顶点，实测压缩 96%（27426 → 1069 字符）
2. 模型只知道工具**要什么参数**，不知道工具怎么实现 —— 距离单位一律「米」，坐标系一律 WGS84
3. 模型算不出结果时**不允许猜** —— 工具返回结构化错误（含 `hint`），让它读到原因再自己纠正

研究区选了华中农业大学狮子山校区：约 3.2 km × 2.15 km，有真实的停车管理问题，
也有足够丰富的 POI 与路网，拿来验证一个空间 Agent 刚好。

---

## 评测：编排方式值 30 个百分点

同一套工具定义、同一个模型（`deepseek-flash`）、同一份 20 条评测集，**只换编排方式**：

| 指标 | raw（裸 Function Calling） | react（LangGraph ReAct） |
|---|---|---|
| **任务完成率** | **70.0%**（14/20） | **100.0%**（20/20） |
| 单跳（6 条） | 6/6 | 6/6 |
| **多跳（6 条）** | **1/6** | **6/6** |
| 参数越界（4 条） | 3/4 | 4/4 |
| 无关问题（4 条） | 4/4 | 4/4 |
| 平均 LLM 调用 | 1.8 | 2.2 |
| 平均工具调用 | 1.1 | 1.5 |
| 平均 token | 6055 | 11565 |
| 平均耗时 | 2.73 s | 3.42 s |

**ReAct 编排把完成率从 70% 提到 100%（+30 个百分点），代价是 token 约 1.9 倍、耗时约 1.25 倍。**

差别集中在**多跳任务**上。基线只有一次工具调用机会：它能"找到最近的食堂"，
但拿不到结果后再去规划路径 —— 5 个双跳用例全部失败，而且失败方式很典型 ——
模型知道该调 `route_plan`，把调用当成文本吐了出来（评测报告里能看到 `fail_reason`
和答案里残留的调用标记）。单跳任务两者持平（都 6/6），说明差距确实来自编排而非工具实现。

这个取舍值得，因为多跳任务在基线里是**完全不可用**的，而 token 翻倍只是成本问题。

复现：`python eval/run_eval.py`　报告：`eval/reports/*.json`（含 40 条逐案回答原文）

---

## 架构

```
① 交互层   腾讯地图 GL JS 地图 + 对话面板 + 执行轨迹时间线
② 服务层   Flask  /api/chat  /api/layers/<name>  /api/eval/latest
③ 编排层   LangGraph StateGraph：agent ⇄ tools（ReAct 循环）
   ─────────────────── 唯一稳定的契约面 ───────────────────
④ 工具层   geocode · list_features · buffer_query ·
           nearest_facility · check_zone · route_plan
⑤ 空间引擎 Shapely · pyproj(WGS84 ⇄ EPSG:4547) · NetworkX · GeoPandas
⑥ 数据层   GeoJSON 直读  →  PostGIS（进阶，工具契约不变）
```

**核心设计主张**：③ 与 ④ 之间是唯一稳定的契约面。
上层只认工具的名字、描述、参数格式，下层怎么实现它完全不管。这个边界带来两个白得的能力：

1. 换后端（GeoJSON → PostGIS）不改上层 Agent 代码
2. 换编排（裸 Function Calling ↔ ReAct）共用同一套工具定义 —— **这是上面那个对比实验能成立的前提**

工具定义由 Pydantic 模型单一来源产出：同一份 `schemas.py` 既生成给模型看的 JSON Schema，
又做运行时参数校验。加一个参数只会加在一个地方。

---

## 坐标系：本项目最容易出错的地方

这是空间应用最容易出错的地方，也是 GIS 背景真正派上用场的地方。项目里同时存在三套坐标：

| 用途 | 坐标系 | 说明 |
|---|---|---|
| 数据存储与接口 | **WGS84 (EPSG:4326)** | GeoJSON 标准，所有工具输入输出都是它 |
| 空间计算 | **EPSG:4547** | CGCS2000 / 3° 高斯-克吕格 CM 114°E，单位是米 |
| 地图渲染 | **GCJ-02** | 国内合规底图使用 |

三个必须注意的点：

1. **距离必须投影后再算。** EPSG:4326 的单位是"度"，1 度纬度 ≈ 111 km。
   把 300 米当成 300 度，缓冲区会覆盖整个中国。所以所有涉及"米"的计算先落到 EPSG:4547，
   算完转回 WGS84 对外输出 —— 转换只发生在 `gis/crs.py`。
   实测 300 m 量测偏差 0.78 m（尺度误差 0.26%）。

2. **WGS84 不能直接画在腾讯/高德底图上。** 实测**偏移 590 米** —— 接近校园宽度
   （3.19 km）的五分之一，足以让"图书馆东侧 39 米"的可停区跑到另一栋楼上。
   渲染前必须做 WGS84 → GCJ-02 转换（`gis/gcj.py`），且**不得写回数据层**
   （该转换不是精确可逆的数学变换）。

3. **工具描述里必须写明坐标系与单位。** 模型看不到实现代码，你不写它就会猜。
   六个工具的 description 都显式声明了"单位是米""内部会自行投影，不要自行换算"。

> 底图合规说明：**不得使用 Leaflet + OpenStreetMap 直连瓦片 / Mapbox / Google /
> 海外版 Bing**。允许的只有腾讯地图、高德、百度、天地图。本项目用腾讯地图 GL JS。
> 这也意味着 POI 数据需要从合规渠道获取（见 [数据](#数据)）。

---

## 六个工具

工具的 `description` 是这个项目里最重要的"提示词" —— 模型看不到实现代码，只能读描述。
所以每一段描述都在回答三个问题：**① 单位是什么？② 坐标系是什么？③ 什么时候该用、什么时候不该用？**

| 工具 | 干什么 | 关键设计 |
|---|---|---|
| `geocode` | 地名 → WGS84 坐标 | 支持正式名、数字变体（荟十/荟10）、口语简称（大活/四教）；返回置信度与命中方式；**0 命中时明确禁止模型猜坐标** |
| `list_features` | 列出停车区或 POI | 附带"可用类别清单"，让模型知道有哪些合法取值 |
| `buffer_query` | 圆形范围查询 | 半径 10–5000 m 硬边界；返回面积与最近距离 |
| `nearest_facility` | 最近的 N 个某类设施 | 支持"只要可停区/可暂停区"过滤 |
| `check_zone` | 点 → 区域归属判定 | 优先级 **可停区 > 可暂停区 > 禁停区**（防御性设计，理由见 `data/DATA_NOTES.md` 第 2 项） |
| `route_plan` | 路网上最短路径 | 无向图；起终点吸附到最近路网节点并回报吸附距离，路径不可靠时如实说明 |

工具层的三层防护：

1. **Pydantic 参数校验** —— 越界的半径/数目在进算法前就被拦下
2. **地理围栏** —— 坐标必须落在研究区 bbox 外扩 200 m 以内
3. **失败结构化返回** —— 返回 `{ok: false, error, hint}` 而不抛异常，让模型读到原因自我纠正

另有两个值得一提的实现细节：

- **几何不进上下文。** 工具返回结果时剥离几何体，只给名称、距离、面积，
  并顺带返回一份可渲染的 `geo_layers` 给前端 —— 模型和地图各拿各的，互不干扰。
- **步数用尽时不给工具。** 最后一轮把工具列表撤掉，强制模型产出文字回答。
  否则最后一条消息是原始 JSON，用户拿到的是一堆结构化数据而不是答案。

---

## 快速开始

```bash
# 1. 建独立环境
python -m venv .venv
.venv\Scripts\activate                    # Windows
# source .venv/bin/activate               # macOS / Linux

# 2. 装依赖（直接装即可，不要先升级 pip）
python -m pip install -r requirements.txt

# 3. 配置密钥
cp .env.example .env          # 填 LLM_API_KEY（必需）、TMAP_KEY（底图，可选）

# 4. 跑起来
python cli.py ask "图书馆 300 米内有哪些可停区？" --show-trace
python web/app.py             # 打开 http://127.0.0.1:5001
```

> ⚠️ **不要在 venv 里跑 `pip install --upgrade pip`。** 实测踩过一次：pip 自我升级时
> 先把 `site-packages\pip` 改名挪走，改名失败后回滚也没成功，结果**整个 `Lib\site-packages`
> 被清空**，报 `No module named pip`。venv 自带的 pip 装本项目依赖完全够用。
> 万一已经坏了，原地修复即可，不用重建 venv：`.venv\Scripts\python.exe -m ensurepip --upgrade`

自测：

```bash
python scripts/test_tools.py  # 工具层测试，脱离 Agent 独立跑
python scripts/test_web.py    # Web 层冒烟测试
python eval/run_eval.py       # 40 次运行的对比评测
```

`requirements.txt` 刻意保持 **纯 ASCII**：pip 按系统区域编码（简中 Windows 是 GBK）读取该文件，
任何非 ASCII 字符（包括中文注释）都会导致 `UnicodeDecodeError`，且发生在安装开始之前。

其他环境相关的坑（PyCharm 解释器、半成品包校验、Windows 编码）记在
[`docs/NOTES.md`](docs/NOTES.md)。

### 运行环境

| | 版本 |
|---|---|
| Python | 3.13 |
| langgraph | 1.2.12 |
| langchain-core | 1.6.6 |
| openai | 3.23.0 |
| pydantic | 2.13.5 |
| shapely / pyproj / networkx | 2.1.2 / 3.8.0 / 3.7 |
| geopandas / pandas | 1.2.0 / 3.0.6 |
| flask | 3.1.3 |

> 代码按 **LangGraph 1.x** 的 API 写（`StateGraph` / `START` / `END`），
> 0.x 的老写法（`AgentExecutor` 那一套）不适用。

### 底图 Key

腾讯地图 GL JS 需要在 <https://lbs.qq.com/> 申请一个免费 Key（控制台 → 应用管理 →
创建应用 → 添加 Key，类型选 **JavaScript GL**），并在该 Key 的域名白名单里加上
`127.0.0.1` 与 `localhost`，然后填入 `.env` 的 `TMAP_KEY=`。

Key 存在服务端、由模板注入，不硬编码进前端 JS，且已被 `.gitignore` 排除。
未配置时对话功能正常，只是底图不显示（页面会给出配置指引）。

> 域名白名单这一步很容易漏。腾讯 GL 库内部用的鉴权接口是
> `https://apikey.map.qq.com/mkey/index.php/mkey/check?appid=jsapi_v3&key=XXX`，
> 可以用它做服务端直检（`scripts/check_tmap_key.py`）。注意判断成败必须**解析 JSON 看
> `info.error` 字段**，不能字符串搜 `"error"` —— 成功响应里也有这个字段（值为 0）。

---

## 目录结构

```
geoagent/
├── settings.py             全局配置（路径、密钥、CRS、运行时参数）
├── cli.py                  命令行入口（overview / tools / ask）
├── config/
│   ├── study_area.json     研究区边界、投影基准、图层口径
│   └── poi_alias.json      别名 → poi_id 查找表（1800 条，生成物）
├── data/
│   ├── parking.geojson         可停区 133
│   ├── tempparking.geojson     可暂停区 51
│   ├── noparking.geojson       禁停区 1（带 153 个内环）
│   ├── road_network.geojson    路网 452 条 / 1636 节点
│   ├── campus_boundary.geojson 校园矢量边界（224 顶点）
│   ├── campus_poi.geojson      POI 350（生成物）
│   ├── DATA_NOTES.md           ★ 数据说明与已发现的问题，动手前先读
│   └── raw_tmap/               腾讯地图原始缓存（可离线复现）
├── gis/
│   ├── crs.py              WGS84 ⇄ EPSG:4547
│   ├── gcj.py              WGS84 ⇄ GCJ-02（仅渲染用）
│   ├── loader.py           数据加载与缓存（进程内单例）
│   └── engine.py           空间算子：缓冲区 / 最近设施 / 区域判定 / 路径
├── agent/
│   ├── schemas.py          Pydantic 参数契约（同时产出 JSON Schema 与校验）
│   ├── registry.py         工具注册表 + 三层防护 + 紧凑视图
│   ├── tools.py            6 个工具的实现与描述
│   ├── llm.py              DeepSeek 客户端（OpenAI 兼容）
│   ├── prompts.py          系统提示（数据规模从数据动态读取，不写死）
│   ├── exec.py             工具执行器（两个引擎共用）
│   ├── graph.py            ReAct 编排（LangGraph）
│   ├── raw_engine.py       基线：裸 Function Calling
│   └── trace.py            运行轨迹（JSONL）
├── eval/
│   ├── cases.jsonl         20 条评测集（单跳/多跳/越界/无关）
│   ├── run_eval.py         对比评测
│   └── reports/            评测报告
├── web/
│   ├── app.py              Flask
│   └── templates/index.html
└── scripts/
    ├── extract_campus_boundary.py  从禁停区外环提取校园边界
    ├── fetch_poi_tmap.py     抓取腾讯地图 POI 原始数据
    ├── build_poi_from_tmap.py 生成 POI 图层与别名表
    ├── check_tmap_key.py     底图 Key 服务端直检
    └── test_tools.py / test_web.py
```

---

## API

| 端点 | 说明 |
|---|---|
| `GET /` | 交互页面 |
| `POST /api/chat` | 一次问答。入参 `{query, engine, display_crs}`，返回 `{ok, answer, geo_layers, trace}` |
| `GET /api/layers/<name>` | 底图图层（`parking` / `tempparking` / `noparking` / `road` / `poi`），默认转 GCJ-02 |
| `GET /api/layers/<name>?crs=wgs84` | 取原始 WGS84 坐标，用于核对偏移 |
| `GET /api/overview` | 数据概览 |
| `GET /api/eval/latest` | 最近一次评测报告摘要 |

`POST /api/chat` 的返回里 `trace` 含每一步工具调用的参数、耗时、结果摘要和 token 用量 ——
前端把它折叠进「执行详情」面板，默认不显示，需要时能看到模型到底调了什么。
这份轨迹也是 `eval/run_eval.py` 判定任务是否完成的依据：评测不比对答案文本，
而是**检查必需工具是否被成功调用过**，避免"答案碰巧说对了"。

---

## 数据

| 图层 | 要素数 | 来源 | 坐标系 |
|---|---|---|---|
| 可停区 | 133 | OpenStreetMap | WGS84 |
| 可暂停区（限时） | 51 | OpenStreetMap | WGS84 |
| 禁停区 | 1（2 部件 / 153 内环） | OpenStreetMap | WGS84 |
| 校园路网 | 452 条（1636 节点 / 1931 边 / 75.85 km） | OpenStreetMap | WGS84 |
| 校园边界 | 1（224 顶点） | 由禁停区外环离线提取 | WGS84 |
| **POI** | **350** | **腾讯地图 WebService** | WGS84 |

停车三图层与路网来自 OpenStreetMap（Overpass API），© OpenStreetMap contributors，ODbL。
已缓存于 `data/raw/`，全流程可离线复现。

**POI 单独走腾讯地图**，这是一个刻意的例外，原因是 OSM 在校园尺度上的覆盖不足：
它只有 8 个具名建筑，**没有「第四教学楼」**（学生口语里的「四教」），
导致「四教附近的停车区」直接返回"未找到该地点"。这是数据源缺失，换任何 Overpass 查询都补不上。

换成腾讯地图后：四教（含 A区/B座/C区）、六个苑区逐栋宿舍（荟园 1–21 栋、北苑 1–15 栋、
南苑 1–32 栋、博园 1–16 栋、宝积苑 1–23 栋、西苑 33–71 栋）、步行街餐饮都已收录，
别名表扩展到 1800 条。抓取流程 `fetch_poi_tmap.py` → `build_poi_from_tmap.py` 两级过滤
（外接矩形粗筛 + `polygon.contains()` 精筛，零缓冲），转换 GCJ-02 → WGS84 后缓存，
**同样可离线复现**。

`data/DATA_NOTES.md` 记录了全部已知数据问题，包括一个我自己推翻的结论：
最初用**质心**核查禁停区判定顺序，得出"8 个可停区被误判"；改用
`representative_point()` 复查后发现误判为 0 —— 有 8 个可停区的质心根本落在自身多边形之外
（凹多边形 / 多部件），质心法测出的是假阳性。**验证空间数据不要用质心。**

---

## 设计取舍

| 约束 | 原因 |
|---|---|
| 工具参数单位一律用**米** | 人类的距离语义是米；模型看到的必须是地理坐标 |
| 工具输入输出一律 WGS84；CRS 转换收在工具层内部 | 但必须在 description 里声明，否则模型会自作聪明去换算 |
| 禁停区判定优先级低于可停区 | 语义上"明确可停优先于兜底禁停"，防御性设计 |
| 路网用**无向图** | 数据中 oneway 全为空（0/452），没有方向信息，不为方向性过度设计 |
| 失败返回结构化错误（含 `hint`），不抛异常 | 让模型读到原因并自我纠正参数 |
| 几何数据不进模型上下文 | 压缩 96%（27426 → 1069 字符） |
| 步数用尽时**不给工具**而非直接终止 | 否则最后一条消息是原始 JSON，用户拿不到文字回答 |
| 路网图启动时构建一次并缓存 | 原实现每次请求重建 452 条线的图（约几十毫秒），纯属浪费 |
| 最近节点查找用 NumPy 向量化 | 1636 个节点，去掉 O(n) Python 循环 |

---

## License

MIT
