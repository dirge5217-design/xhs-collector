# 小红书零浏览器采集器

> ⚠️ **私有仓库 / 仅供学习研究。** 本项目包含对目标站点请求签名逻辑的逆向实现，
> 请保持仓库私有，遵守目标站点服务条款与所在地法律，不要用于大规模抓取或商业用途。
> `_xhs_cookie.txt`（登录态）、`xhs.db` 与所有 `*.csv`（采集数据，含他人昵称/评论/IP）
> 都由 `.gitignore` 排除，**不要手动 `git add -f`**。`_eval_captured.js` 是站点运行时
> 下发的第三方脚本，请勿公开分发。

**状态：全链路已打通（2026-11-07 实测）**。签名在本地 Node 生成，采集走 `requests`，
运行期不启动任何浏览器。四个核心接口全部 `code:0`：

| 接口 | 结果 |
|---|---|
| `POST /api/sns/web/v1/homefeed` 推荐流 | 20 条 `code:0` |
| `POST /api/sns/web/v1/search/notes` 关键词搜索 | 22 条 `code:0` |
| `POST /api/sns/web/v1/feed` 笔记详情 | 正文 / 图片 / 赞藏评 / 标签齐全 |
| `GET /api/sns/web/v2/comment/page` 评论 | `code:0`，实测抓到 11 条 |

另有一键体检脚本，站点改版后先跑它判断断点在哪。

---

## 1. 快速开始

```powershell
cd xhs_collector            # 换成你 clone 到本地的实际路径

# 0. 首次配置：装依赖 + 填登录 Cookie
npm install
copy _xhs_cookie.example.txt _xhs_cookie.txt
#    把 _xhs_cookie.txt 内容替换成自己浏览器登录后的完整 Cookie（单行），
#    至少包含 a1 与 web_session；该文件已被 .gitignore 排除

# 窗口 A：启动常驻签名服务（保持开着）
.\start_signer.bat          # Linux/macOS: node xhs_signer_server.js 18080

# 窗口 B：体检（退出码 0 = 闭环）
python xhs_check.py

# 采集：搜索「咖啡」前 3 页，抓详情 + 每篇 2 页评论，导出 CSV
python xhs_collect.py --mode search --keyword 咖啡 --pages 3 ^
    --detail --comments 2 --db xhs.db --csv 咖啡.csv --rate 1.5
```

要求：Python 3.10+ 且装了 `requests`（`pip install requests`）；Node 18+；
本机代理 `127.0.0.1:7897` 可用（可用 `--proxy ""` 关闭）。
`node_modules` 不入库，clone 后需自行 `npm install`。

**网络注意**：这台机器直连 `edith.xiaohongshu.com` 常读超时，走 7897 才稳。
换机器时可用 `--proxy ""` 关掉代理。

---

## 2. 目录结构

| 文件 | 作用 |
|---|---|
| `xhs_collect.py` | 采集器：四种模式、SQLite 落库去重、断点续跑、限速重试、CSV 导出 |
| `xhs_sign.js` | 签名算法实现，暴露 `sign(url, data)` → `{x-s, x-t}` |
| `_eval_captured.js` | **288KB 运行时载荷**，含字节码 VM 与 `mnsv2` 定义（关键资产） |
| `xhs_signer_server.js` | 常驻 HTTP 服务：只加载一次载荷，之后 1~3ms/次 |
| `xhs_check.py` | 一键体检：签名服务 → 登录态 → 推荐流 → 搜索 |
| `_xhs_cookie.txt` | 登录 Cookie（**不入库 / 不外发**），本地自行创建 |
| `_xhs_cookie.example.txt` | Cookie 占位模板，复制后填自己的值 |
| `.gitignore` | 排除凭据、采集数据、依赖与缓存 |
| `start_signer.bat` | Windows 一键启动签名服务 |
| `package.json` | `jsdom` 依赖声明 |

签名服务接口：

```
GET  /health                              -> {ok, pid, uptime_s}
POST /sign        {url, data, a1}          -> {x-s, x-t, x-s-common, cost_ms}
POST /sign_batch  {items:[{url,data,a1}]} -> {results:[...]}
```

实测吞吐：50 次签名 393ms，平均 **7.9ms/次，约 127 次/秒**，远高于采集限速需求。
单次冷启动加载载荷约 300ms，所以必须常驻，不能每次 `node xhs_sign.js`。

---

## 3. 采集器参数

```
--mode homefeed|search|user|note    必填
--keyword TEXT      search 模式的关键词
--user-id TEXT      user 模式的用户 ID（24 位十六进制）
--notes FILE        note 模式的文件，每行 "<noteId> [xsec_token]"
--pages N           列表最多翻几页（默认 5）
--detail            对每条笔记抓详情（补正文全文 / 图片数 / 标签）
--comments N        抓评论，N = 每篇最多翻几页（0 = 不抓）
--sub-comments      连二级评论一起抓
--skip-done-comments  已抓过评论的笔记不再请求接口（省请求）
--db FILE           SQLite 文件（默认 xhs.db）
--csv FILE          额外导出 CSV（同时生成 *_comments.csv）
--csv-all           导出整库；默认只导出本次运行拿到的笔记
--rate SEC          请求间隔秒数，默认 1.5，带 ±20% 随机抖动
--proxy URL         默认 http://127.0.0.1:7897
--limit N           只处理前 N 条（调试用）
```

示例：

```powershell
# 推荐流 5 页，只建索引
python xhs_collect.py --mode homefeed --pages 5

# 某个博主的全部笔记，抓详情
python xhs_collect.py --mode user --user-id 5b5efac56b58b71d9f1d4b1b --pages 20 --detail

# 已有 noteId 列表，补齐详情和评论
python xhs_collect.py --mode note --notes ids.txt --comments 5 --sub-comments
```

### 日志怎么读

每篇笔记固定打印一行，四个字段用 `|` 分隔：

```
[60/66] 6aeb0da6... | 详情 OK 赞17 总评7 | 入库评论 +3（一级3 · 楼中楼4 未抓） | 📚文博爱好者的"电子榨菜"
```

| 字段 | 含义 |
|---|---|
| `详情 OK 赞 N 总评 M` | 详情抓成功；`总评` 是笔记展示的**评论总数（含楼中楼回复）** |
| `入库评论 +N（...）` | 本次**去重后真正写库**的新增行数，括号里说明差额在哪 |
| 末尾标题 | 详情里的标题（或正文前 30 字） |

括号里的量：

| 片段 | 含义 |
|---|---|
| `一级X` | 本次 `comment/page` 返回的一级评论条数（去重前） |
| `楼中楼Y` | 开了 `--sub-comments`，实际抓到的回复条数 |
| `楼中楼Y 未抓` | 没开 `--sub-comments`，按各一级评论的 `sub_comment_count` 累加出的**待抓回复数（估算）** |
| `无楼中楼` | 一级评论下没有回复 |
| `去重Z` | 接口返回了但库里已存在，被 `INSERT OR IGNORE` 忽略的条数 |

**`总评` 和 `+N` 不相等是正常的**，最常见原因就是楼中楼没抓：上面这行里
3 条一级评论 + 4 条楼中楼回复 = 7，与 `总评7` 完全吻合。加 `--sub-comments`
后会变成 `入库评论 +7（一级3 · 楼中楼4）`。其它原因：`--comments 1` 只翻一页、
评论被折叠或审核不可见、`+N` 是新增行数而非库内总数（同一篇笔记重跑会显示 0）。

`入库评论 0` 现在会明确区分三种情况：

| 日志 | 含义 |
|---|---|
| `入库评论 0（库内已有 3 条，全为重复）` | 之前已抓过，数据没丢，只是没有新增 |
| `入库评论 0（接口未返回评论）` | 接口 `code:0` 但评论列表为空（无评论 / 评论不可见 / token 过期） |
| `评论 已抓过(跳过，库内 3 条)` | 加了 `--skip-done-comments`，直接跳过接口请求 |

因为默认 `xhs.db` 是多关键词共用且按 ID 去重，**同一篇笔记出现在不同关键词的
结果里时，第二轮必然显示「全为重复」**。想省请求就加 `--skip-done-comments`
（请求数减半，但捕捉不到笔记后来新增的评论）；想复查就保持默认。
失败会打 `详情 失败(...)` / `评论 失败(...)`，不会静默跳过，且失败**不会**
被标记为已完成，下次会重试。

每 20 条打一次进度行；结束汇总里「评论新入库」是去重后的总增量，紧接着一行
`本次接口返回：一级 N 条 / 楼中楼 N 条` 说明本次实际拿到了什么，若还有
`另有 N 条楼中楼未展开` 就是差额来源；「库内合计」是整库累计（可能包含历史关键词）。

**断点续跑**：同一 `xhs.db` 重跑即可。笔记按 `id` 去重（UPSERT），评论按 `id` 去重
（`INSERT OR IGNORE`）；`detail_done` / `comment_done` 两个标记避免重复抓。
按 `Ctrl+C` 中断不会丢已落库数据。

**CSV 导出范围**：默认**只导出本次运行拿到的笔记**（`--mode search` 时同时按
关键词过滤），评论按同一批 noteId 过滤，不会串进别的话题。想导出整个库用
`--csv-all`。数据库本身是多关键词共用的（靠 `keyword` 列区分、按 id 去重），
所以如果想让每个话题的物理文件也彻底独立，给每次任务指定不同的 `--db`，例如
`--db 上海博物馆.db`。

**输出字段**：笔记表含 id / 作者 / 标题 / 正文 / 赞藏评享 / 图片数 / 标签 /
IP 属地 / 发布时间 / 带 token 的原文链接 / `xsec_token` / 来源 / 关键词；评论表含
评论 ID / 所属笔记 / 父评论 / 昵称 / 内容 / 点赞 / 时间 / 是否二级。
`1.2万` 这类数字统一转成整数。

---

## 4. 逆向要点（A 路线全过程）

### 4.1 签名算法（`vendor-dynamic.js` 里的明文）

```js
c = url + (data 为 object ? JSON.stringify(data) : data 为 string ? data : "")
u = md5(c);  p = md5(url)
x3 = window.mnsv2(c, u, p)
S  = {x0:"4.4.3", x1:"xhs-pc-web", x2:"Windows",
      x3, x4: typeof data, x5:u}          // 无 ssk 时没有 x6/x7
X-s = "XYS_" + base64_自定义字母表( utf8(JSON.stringify(S)) )
```

自定义字母表（不是 lz-string，曾踩过坑）：

```
ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5
```

`X-S-Common` 同法，结构为
`{s0:5,s1:"",x0:"1",x1:"4.4.3",x2:"Windows",x3:"xhs-pc-web",x4:"6.60.2",
x5:a1_cookie,x6:"",x7:"",x8:null,x9:-931954833,x10:0,x11:"normal",
x12:"null;"+时间戳}`。`x9` 在 x6/x7 空、x8 为 null 时是常量。

**只带 `X-s`/`X-t` 不带 `X-S-Common`，高风险接口会返回
`code:300011 当前账号存在异常`**。同账号浏览器对照返回 200，证明是缺头而不是账号被封。

### 4.2 `mnsv2` 是怎么跑起来的

| 尝试 | 结果 |
|---|---|
| `as/v1/3e44` 的 `ds.js`（60KB）丢进 Node `vm` | eval 通过，但 `mnsv2` 不挂载 |
| `as/v2/ds` 的 `v2ds.js`（62KB） | `undefined is not a constructor` |
| 补齐构造器 / `chrome` / `MouseEvent` / `eval` / `InstallTrigger` | 仍不挂载 |
| **抓 `signV2Init` 里 `eval` 的真实实参** | **288KB，与本地文件相似度仅 0.31** —— 之前一直在跑不完整的脚本 |
| 完整载荷 + Node 裸 `vm` | 不挂载；同载荷在真实浏览器里能挂载 → 纯宿主环境差异 |
| 完整载荷 + jsdom | `TypeError: Failed to execute 'removeChild' on 'Node'` |
| **jsdom 真实 DOM + 放宽 removeChild 参数校验** | **成功，`mnsv2` 返回 `mns0101_...`** |

关键结论：字节码 VM 会**真的创建 DOM 节点、插入、再移除**做环境探测，
手写假 `document` 过不了真实 DOM 的类型校验；但也不能用 jsdom 原生的严格校验，
因为它故意传非法参数试探。所以采用「真实节点 + 宽容的 removeChild」。

用 Proxy 记录全局访问还确认了：VM 会查 `cdc_adoQpo...`、`__webdriverFuncgeb`、
`_Selenium_IDE_Recorder` 等 webdriver 检测项，这些**不存在反而是对的**。

### 4.3 其它坑

- `mnsv2` 是确定性纯函数；浏览器对假参数返回的 `mns0301_0` 是错误值，
  真实调用返回的是 `mns0101_`。加密带随机 IV，所以同一输入两次结果不同。
- 详情 / 评论必须带 **`xsec_token`**，它只由列表接口下发，笔记 ID 本身不够。
  评论接口要放在 **query** 里，详情接口放在 **body** 里。
- `__INITIAL_STATE__` 不是标准 JSON（含 `undefined`、裸值），正则和括号扫描都会失败；
  不要用解析 SSR 这条路，直接调接口更干净。
- `user_posted` 返回**扁平结构**（`display_title` / `note_id` / `cover` /
  `interact_info` / 毫秒时间戳），与首页、搜索的 `note_card` 嵌套结构不同，
  采集器已分别适配。

---

## 5. 风控与使用边界（务必读）

1. **登录期零浏览器不成立**。小红书登录强制滑块 + `a1` 设备指纹，
   纯 HTTP 过不去。当前 `_xhs_cookie.txt` 来自一次人工登录。
   运行期完全无浏览器，但 cookie 过期后仍需人工再登一次。

2. **单账号一天稳定在千级请求**。十万级持续跑一定触发风控，需要账号矩阵 +
   代理池，且每个账号都要一份 cookie。这不是优化项，是必需品。

3. **`--rate` 不要低于 1**。默认 1.5 秒带抖动。搜索是风控最严的接口，
   别用高并发。脚本遇到 `300011`（账号异常）/ `300012`（登录失效）会**立即中止**
   而不是重试，避免把账号彻底打没。

4. **载荷会轮换**。`_eval_captured.js` 是运行时下发的安全脚本，
   服务端换版本后签名可能失效。届时先跑 `xhs_check.py` 定位断点；
   若确认是签名失效，需要用 CDP/Playwright 从浏览器重新抓一次
   `signV2Init` 的 eval 实参（取证用的 `grab_eval.py` 等探针属本地临时素材，未纳入本仓库）。

5. **Cookie 是凭据**。`_xhs_cookie.txt` 等价于账号密码，别提交、别外发、
   别打包进 exe。本仓库已由 `.gitignore` 排除；更稳妥的做法是改用环境变量
   `XHS_COOKIE`（`load_cookie()` 在找不到文件时会读它）。

6. 只采集公开可见数据，遵守目标站点的服务条款与当地法律，别碰用户隐私字段。

---

## 6. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `签名服务未启动` | 先在窗口 A 跑 `start_signer.bat`，或 `node xhs_signer_server.js 18080` |
| 签名服务请求超时 | 本地 127.0.0.1 被系统代理劫持。脚本已设 `trust_env=False`，别改回去 |
| `code:-100 签名被拒` | 载荷轮换或 `webBuild` 版本变了，重新抓 `_eval_captured.js` |
| `code:300011 账号异常` | 缺 `X-S-Common`，或账号被标记。先用 `xhs_check.py` 对照 |
| `code:300031 / HTTP 461` | 缺 `xsec_token`，或 token 过期。重新跑列表接口取新 token |
| `code:300012 登录失效` | 重新导出 Cookie 覆盖 `_xhs_cookie.txt` |
| 首页/搜索返回空但 code=0 | 关键词无结果，或该账号被降权；换关键词试 |
| 网络读超时 | 换走 7897 代理；`--proxy ""` 可关闭 |
| `user` 模式标题为空 | 已修复（扁平结构适配），确认用最新版 `xhs_collect.py` |
| CSV 里混进别的关键词数据 | 早期版本导出整库所致。现已默认按本次运行范围过滤；旧文件可用 `Store('xhs.db').export_csv('x.csv', keyword='关键词')` 重新导出 |
| `url` 列带 `?xsec_token=...` | 正常。这是列表接口下发的短期访问令牌，没有它打不开笔记详情；令牌会过期，届时用 `id` 重新跑列表取新 token |

---

## 7. 逆向素材归档

以下取证素材属本地临时产物（体积大、与账号/会话相关），**未纳入本仓库**，
仅记录清单供站点改版后在本机复测：

- 前端 bundle：`index.js` / `vendor.js` / `vendor-dynamic.js`
- 安全脚本：`ds.js` / `v2ds.js` / `sd1.js` / `sd2.js` / `f218.js` / `v2fp.js`
- 真实签名样本：`_trace.json`（20 条）、`_s_samples.json`
- 浏览器探针：`trace_def.py`、`hook_mnsv2.py`、`hook_S.py`、`grab_eval.py`、
  `ab_browser.py` / `ab2_browser.py` / `ab3_browser.py`
- Node 诊断：`diag1.js` ~ `diag7.js`（含 Proxy 全局访问记录）
