# Reuters / Bloomberg 多平台新闻 RSS 资源池

这是一个独立项目，集中处理 Reuters、Bloomberg 源站 RSS、Yahoo 日本媒体页、公开 RSS
转载平台，以及缺少稳定 RSS 的新闻索引平台。所有入口统一为一份文章数据池，主要只输出
去重全集、Reuters、Bloomberg、全文四条 RSS；每个资源点的 RSS 留作内部诊断。

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
GET  /api/search?q=India&publisher=Reuters&relation=repost&content_level=full
GET  /rss/search.xml?q=RBI
GET  /api/live-search?q=RBI
GET  /rss/live-search.xml?q=RBI
POST /api/update
```

- `/api/search`：毫秒级查询最近一次资源池快照；默认返回原稿与明确转载，`mode=all` 包含引用和待核验条目。
- 搜索过滤字段：`publisher`、`found_at`、`relation`、`content_level`；旧的 `classification` 参数继续兼容已有调用方。
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

## 统一文章字段

每篇文章对 API、JSON 和 RSS 暴露四个核心维度：

| 字段 | 值 | 含义 |
|---|---|---|
| `publisher` | `Reuters` / `Bloomberg` / 空 | 新闻机构 |
| `found_at` | 来源 ID，如 `yahoo_news_jp_reuters` | 从哪个资源点发现 |
| `relation` | `original` / `repost` / `mention` / `unknown` | 原稿、明确转载、仅引用、待核验 |
| `content_level` | `full` / `summary` / `link_only` | 正文、摘要、仅链接 |

详细证据和旧 `classification` 继续保存在 JSON 中，用于置信度计算和兼容读取；它们不再拆成多条主要 RSS。

## 已接入的平台

| 平台 | 获取方式 | 正文/摘要策略 | 输出特性 |
|---|---|---|---|
| Reuters | 当前 Arc outbound RSS | `content:encoded` 正文与摘要 | `publisher=Reuters, relation=original` |
| Bloomberg | 10 个当前分类 RSS | RSS 摘要 | `publisher=Bloomberg, relation=original` |
| Yahoo!ニュース日本 | `media/reut`、`media/bloom_st` 的 `__PRELOADED_STATE__` | 文章页结构化正文；过滤付费标记 | `relation=repost` |
| Yahoo!ファイナンス日本 | `news/media/reut` 嵌入状态 | 文章页 paragraphs 正文与 summary | `relation=repost` |
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

- `data/deduplicated.xml`：全部接受条目的跨平台去重视图。
- `data/reuters.xml`：已确认的 Reuters 原稿和明确转载。
- `data/bloomberg.xml`：已确认的 Bloomberg 原稿和明确转载。
- `data/fulltext.xml`：已确认且 `body >= 200` 字符的正文子集。
- `data/feeds/<source-id>.xml`：按采集入口拆分的诊断 RSS。
- `data/resource_pool.json`：正文、摘要、证据、置信度、状态的完整数据池。
- `data/health.json`：端点状态、抓取量、接收量、内容关系和正文等级计数。
- `data/last_attempt.json`：最近一次刷新是发布还是因质量门槛被拒绝。
- `data/resource_pool.opml`：可导入 RSS 阅读器的源清单。

RSS 使用 `wire:` 扩展字段保存：发布机构、发现位置、平台、内容关系、正文等级、
置信度、证据、摘要来源、正文来源和 canonical URL。正文放在 `content:encoded`。

构建器在全部来源失败、条目为零，或相对上一版出现“来源大面积失效且条目骤降”时拒绝发布，
继续保留上一版数据，并把失败详情写到 `last_attempt.json`。

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
