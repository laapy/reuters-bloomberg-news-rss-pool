# Reuters / Bloomberg 多平台新闻 RSS 资源池

这是一个独立项目，集中处理 Reuters、Bloomberg 源站 RSS、Yahoo 日本媒体页、公开 RSS
转载平台，以及缺少稳定 RSS 的新闻索引平台。运行时同时输出 RSS、JSON、OPML 和来源健康记录。

## 快速运行

```powershell
cd D:\彭博社_路透社_新闻获取_8_13\reuters_bloomberg_news_rss_pool
python -m unittest discover -s tests -v
python run.py --workers 12
python verify_outputs.py
```

也可执行：

```powershell
.\update.ps1
```

只运行特定来源：

```powershell
python run.py --source yahoo_news_jp_bloomberg --source yahoo_news_jp_reuters
```

`--quick` 仅更新列表和 RSS 元数据，跳过文章页正文增强。

## 常驻 HTTP 服务和动态搜索

后台启动：

```powershell
.\start-http-service.ps1
# 或直接使用 Python
python service_control.py start
```

打开：

```text
http://127.0.0.1:8765/
```

服务默认每 1800 秒更新整个资源池，并提供：

```text
GET  /api/health
GET  /api/sources
GET  /api/search?q=RBI
GET  /api/search?q=India&mode=all&limit=100
GET  /rss/search.xml?q=RBI
GET  /api/live-search?q=RBI
GET  /rss/live-search.xml?q=RBI
POST /api/update
```

- `/api/search`：毫秒级查询最近一次资源池快照；默认排除候选，`mode=all` 包含候选。
- `/rss/search.xml`：把任意关键词查询转换成动态 RSS。
- `/api/live-search`：请求时查询 Google/Bing News，结果缓存 60 秒。
- `/api/update`：从本机触发后台全量更新。

查看状态和停止服务：

```powershell
.\status-http-service.ps1
.\stop-http-service.ps1
# 对应的 Python 命令
python service_control.py status
python service_control.py stop
```

需要观察控制台日志时使用：

```powershell
.\serve-foreground.ps1
```

## 最重要的分类规则

| 分类 | 判定 | 用途 |
|---|---|---|
| `wire_original` | Reuters/Bloomberg 自有端点 | 源站标题、摘要、RSS 正文 |
| `wire_syndication` | Yahoo 出版商专页，或作者/来源精确等于 Reuters/Bloomberg，或保留 wire byline/copyright | 真正转载池 |
| `wire_attribution` | `Reuters reported`、`according to Bloomberg News`、`ブルームバーグによると` 等 | 引用后改写池 |
| `discovery_candidate` | Google/Bing 索引命中，但页面证据仍弱 | 候选池 |

分类器特意把平台品牌、查询关键词和正文署名分开。比如作者是某平台、正文只写
“Bloomberg News reports”的文章进入 `wire_attribution`，而不是 `wire_syndication`。

## 已接入的平台

| 平台 | 获取方式 | 正文/摘要策略 | 输出特性 |
|---|---|---|---|
| Reuters | 当前 Arc outbound RSS | `content:encoded` 正文与摘要 | `wire_original` |
| Bloomberg | 10 个当前分类 RSS | RSS 摘要 | `wire_original` |
| Yahoo!ニュース日本 | `media/reut`、`media/bloom_st` 的 `__PRELOADED_STATE__` | 文章页结构化正文；过滤付费标记 | `wire_syndication` |
| Yahoo!ファイナンス日本 | `news/media/reut` 嵌入状态 | 文章页 paragraphs 正文与 summary | `wire_syndication` |
| Yahoo Finance | 原生 `rssindex` | RSS 元数据 + 有限文章页增强 | 只保留证据命中 |
| CNA | latest/business/world 原生 RSS | JSON-LD、article body、Source credit | 区分转载/自采 |
| Investing.com | 原生 news RSS | RSS author 是主要证据 | 文章页遇到访问限制时保留 RSS 证据 |
| TradingView News | Bing，空结果时回落 Google News | 标题、跳转、索引摘要 | 独立候选 RSS |
| TBS NEWS DIG | Bing/Google News 站内索引 | 日英 attribution 规则 | 独立候选 RSS |
| Bloomberg Línea | Bing/Google News 站内索引 | 页面元数据增强 | 独立候选 RSS |
| MSN | Bing/Google News 站内索引 | 解析 Bing 直达参数 | 独立候选 RSS |
| Google News | 英文 Reuters/Bloomberg + 日文 wire 查询 | 标题、摘要、跳转 | 候选池 |
| 其他 | MINING.com、Mint、Economic Times、Straits Times、Business Standard、ThePrint、SWI、Devdiscourse、ZAWYA、BNN Bloomberg、MarketScreener | 原生 RSS 或站内索引 | 每个平台单独 RSS |

## 输出

- `data/verified_all.xml`：已确认的源站、转载、引用稿。
- `data/wire_original.xml`：Reuters/Bloomberg 自有端点。
- `data/wire_syndication.xml`：明确署名的转载稿。
- `data/wire_attribution.xml`：引用 Reuters/Bloomberg 后写成的平台稿。
- `data/discovery_candidates.xml`：仅有索引证据的待核验条目。
- `data/fulltext.xml`：`body >= 200` 字符的全文子集。
- `data/deduplicated.xml`：跨平台标题归一化去重。
- `data/feeds/<source-id>.xml`：每个平台/端点的单独 RSS。
- `data/resource_pool.json`：正文、摘要、证据、置信度、状态的完整数据池。
- `data/health.json`：端点状态、抓取量、接收量、分类计数。
- `data/resource_pool.opml`：可导入 RSS 阅读器的源清单。

RSS 使用 `wire:` 扩展字段保存：平台、来源 ID、原始发布方、分类、置信度、证据、
摘要来源、正文来源和 canonical URL。正文放在 `content:encoded`。

## 配置与扩展

所有来源都在 `sources.json`。类型包括：

- `rss`：发布方自有 RSS。
- `rss_filter`：第三方 RSS，逐条检查署名和正文证据。
- `yahoo_news_media`：Yahoo!ニュース媒体页。
- `yahoo_finance_media`：Yahoo!ファイナンス媒体页。
- `bing_discovery`：Bing News RSS；零结果时自动转 Google News RSS。
- `google_discovery`：Google News RSS 查询池。

新增原生 RSS 时复制一个 `rss_filter` 条目；新增无 RSS 平台时配置域名和
`bing_discovery`，构建器会生成独立 RSS。

## 上游项目

`research/upstream/` 保存了
[`kidsnz/260518_bloomberg-jp-rss`](https://github.com/kidsnz/260518_bloomberg-jp-rss/tree/main)
的研究快照。上游实现很紧凑：抓取 Yahoo Bloomberg 媒体页、稳健匹配
`window.__PRELOADED_STATE__`、分页取 50 条、跳过 `isPay`，然后每 30 分钟更新
`feed.xml`。本项目保留这条可靠链路，并扩展到多发布方、多平台、正文提取、来源证据和分类 RSS。

完整审计见 `docs/PROJECT_AND_PLATFORM_AUDIT_2026-08-18.md`。
