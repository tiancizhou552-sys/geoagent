# 环境与踩坑记录

README 讲项目本身，这份文件讲「怎么让它在你机器上真的跑起来」。
内容都是实测踩出来的，不是通用建议。

---

## 一、Windows 上装依赖报 UnicodeDecodeError

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0xa1 in position 15
```

pip 读 `requirements.txt` 时用的是**系统区域编码**（简体中文 Windows 是 GBK），
只要文件里出现任何非 ASCII 字符 —— 包括中文注释、`──` 这类制表符 —— 就会在开始安装前
直接崩掉，**跟依赖本身无关**。

本项目已把 `requirements.txt` 固定为**纯 ASCII**（`#` 注释一律用英文），请勿在其中加中文注释。

如果将来遇到同类报错（其他项目也适用），两个通用解法：

- **临时绕过**：`set PYTHONUTF8=1` 后重跑（UTF-8 模式下 pip 按 UTF-8 解析）
- **根治**：把该依赖文件另存为 ASCII 或 UTF-8 无 BOM，并去掉中文注释

> 这个坑对中文环境是通用的：**凡是命令行工具读取的配置/清单文件**
> （`requirements.txt`、`.txt` 清单、部分工具的 `.cfg`），都比业务代码更容易栽在编码上。
> 业务代码里的中文没问题，因为这些文件是我们自己用 UTF-8 显式读写的。

---

## 二、不要升级 venv 里的 pip

**症状**：`pip install --upgrade pip` 之后，整个环境坏掉，报 `No module named pip`。

**原因**：pip 升级自己时要先把 `site-packages\pip` 改名挪走。这一步在 Windows 上失败后，
回滚也失败，结果是**整个 `Lib\site-packages` 被清空**。

**处置**：原地修复，**不用重建 venv**：

```bash
.venv\Scripts\python.exe -m ensurepip --upgrade
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

venv 自带的 pip（24.3.1）装本项目依赖完全够用，不需要升级。

---

## 三、安装被中途打断，留下"半成品"包

**症状**：安装没报明显错误，但导入某个包失败；`site-packages` 里出现以 `~` 开头的目录
（如 `~anggraph-checkpoint-4.2.0.dist-info`）；或 `pip list` 显示已装但 `import` 报错。

**原因**：pip 覆盖已存在的文件时会先 `os.unlink`。如果运行环境装了文件删除防护类的钩子
（部分沙箱 / 企业管控环境、安全软件会做这件事），在大量删除时会中断进程，而 pip 已经写了一半。
**这不是依赖本身的问题。**

**处置**（pip 是幂等的，重跑即可自愈）：

```bash
# 1. 再次安装，补齐被中断的部分
python -m pip install -r requirements.txt

# 2. 若提示某个包不完整，单独补装
python -m pip install --force-reinstall --no-deps <包名>

# 3. 清掉以 ~ 开头的残骸目录（这些是 pip 中断留下的标记）
#    Windows: 手动删除 site-packages 下所有 ~ 开头的目录
#    macOS/Linux:
find .venv/lib/*/site-packages -maxdepth 1 -name '~*' -exec rm -rf {} +
```

**第 4 步：文件完整性校验**（比 `pip check` 更严格，能发现半成品包）

```python
import os, csv
sp = os.path.join('.venv', 'Lib', 'site-packages')      # macOS/Linux: lib/python3.x/site-packages
for d in sorted(os.listdir(sp)):
    if not d.endswith('.dist-info'): continue
    rec = os.path.join(sp, d, 'RECORD')
    if not os.path.exists(rec): continue
    missing = [r[0] for r in csv.reader(open(rec, encoding='utf-8', newline=''))
               if r and not os.path.exists(os.path.join(sp, r[0].replace('/', os.sep)))]
    if missing: print(d, '缺', len(missing), '个文件', missing[:2])
```

本项目当前状态：**59 个 distribution 全部文件齐全**。

> `pip check` 检查的是依赖版本约束，**半成品包也能过**。要确认"装全了"必须查 RECORD。

> 💡 **建议在 PyCharm 的 Terminal 或系统命令行里装依赖**，不要在有文件删除防护的
> 沙箱 / Agent 终端里装 —— 那里的守卫可能把 pip 的正常覆盖动作当成批量删除而中断它。

---

## 四、PyCharm 里第三方库报红波浪线，但面板显示已安装

**症状**：`Settings → Python Interpreter` 面板能正常显示包（面板是实时查磁盘），
但代码里 `import shapely` 仍然红波浪线。

**根因**：**解释器路径含非 ASCII 字符时，PyCharm 静默失败，不把 `site-packages`
加进 SDK classPath**（面板走磁盘查询，代码提示走 classPath，两者数据源不同）。

本机 7 个 SDK 实测相关性 100%：路径含中文的 2 个（`agent开发\geoagent\.venv`、
`水体处理\.venv`）`site-packages` 类路径根 **0 个**；路径全 ASCII 的 5 个都有。

**诊断**：读 `%APPDATA%\JetBrains\PyCharm<版本>\options\jdk.table.xml`，
看每个 `<jdk>` 的 `<roots><classPath>` 里有没有 `site-packages` 那一行
（正常应同时有 venv 根 + site-packages 根）。

**修复（按代价递增）**：

1. `Settings → Python Interpreter → 齿轮 → Reload interpreter paths`
2. `Show All… → 选中 SDK → Show Interpreter Paths → +` 手动补上两个根
3. **根治：路径去掉中文**（项目或解释器放到纯英文路径）

> **结论：新建项目 / 虚拟环境一律用纯英文路径。**

---

## 五、环境从零重建

当解释器 / 依赖状态乱了、想彻底重来时。**顺序很重要**：先关 IDE，再动文件。

```bash
# ① 先关闭 PyCharm（否则 .venv / .idea 被占用，删不干净）

# ② 把旧环境挪走（重命名而非删除，可回退）
cd geoagent
ren .venv .venv_old
ren .idea .idea_old          # .idea 是 IDE 配置，会自动重建，删了不影响代码

# ③ 用系统 Python 建全新环境（标准库 venv；不要加 --upgrade-deps）
C:\python\python.exe -m venv .venv

# ④ 装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

# ⑤ 自检
.venv\Scripts\python.exe scripts/test_tools.py

# ⑥ 确认正常后再删旧目录（进回收站，可恢复）
```

再打开 PyCharm → `Add Interpreter → Existing` → 选 `.venv\Scripts\python.exe`。

- **为什么用 `python -m venv` 而不是 `virtualenv`**：标准库够用，少一层依赖；
  旧版 `virtualenv` 建出的环境在 pip 自升级时更容易出问题。
- **同一项目只保留一个 venv**。出现 `.venv1`、`.venv_new` 这类嵌套 venv
  （尤其是从另一个 venv 的 python 里创建出来的）会让 IDE 和命令行各自指向不同环境，
  是"明明装了却提示找不到包"最常见的原因。

---

## 六、修改代码后要重启服务

`web/app.py` 里 `app.run(..., debug=False)`，**代码和 Jinja 模板都缓存**。
改完 `web/app.py` 或 `web/templates/` 后必须重启，否则看到的还是旧页面。

想在别的端口验证：先设 `PORT=5002` 再启动（python-dotenv 不覆盖已存在的环境变量）。

---

## 七、前端调试（没有浏览器可用时的替代方案）

前端脚本一旦顶层抛错，**其后的代码全部不执行** —— 表现为"页面能打开但所有按钮都没反应
且底图空白"，一个根因三种表现。在没有浏览器可以做验证的环境里，按这个顺序排查：

1. **抽内联 `<script>` 跑 `node --check`**（只解析不执行，能确证语法）。
   注意 Jinja 的 `{{ }}` 会导致 `SyntaxError: Unexpected token {`，
   要先替换成占位符再检查。
2. **服务端渲染断言**：`app.test_client().get('/')` 拿到渲染结果，检查关键片段是否存在。
3. **底图鉴权可服务端直检**，不用开浏览器：
   `https://apikey.map.qq.com/mkey/index.php/mkey/check?appid=jsapi_v3&key=XXX`
   （见 README 底图 Key 一节，注意要解析 JSON 而不是搜字符串）。

两个已经踩过、值得单独记住的坑：

- **模板往 JS 注入数据必须是合法 JS 字面量。** `render_template(center=(lat, lon))`
  会渲染成 `const CENTER = (30.471547, 114.355029);` —— JS 按**逗号运算符**求值，
  `CENTER` 变成最后一个数字，`CENTER[0]` 是 `undefined`，腾讯 GL 的
  `new TMap.LatLng(NaN, NaN)` 直接 throw，整个 `<script>` 中断。**必须传 `list`。**
- **`.catch(() => {})` 是隐患。** 它把"加载失败"变成"元素静默消失"，排查时毫无线索。
  改成 `.catch(e => console.error(...))` 才能看见。本项目因此浪费了一轮排查。

> 推论：**任何可能抛错的初始化（地图 / 图表 / 第三方 SDK）都要 try/catch，
> 或把 UI 事件绑定放到它前面。**
