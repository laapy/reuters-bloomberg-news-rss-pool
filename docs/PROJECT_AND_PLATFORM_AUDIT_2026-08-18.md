# 上游项目与 Reuters/Bloomberg 平台资源池审计

审计日期：2026-08-18（Asia/Shanghai）

## 1. 上游仓库逐文件结论

研究对象：`kidsnz/260518_bloomberg-jp-rss`，本地快照位于 `research/upstream/`。

| 文件 | 作用 | 结论 |
|---|---|---|
| `fetch_and_build.py` | Yahoo Bloomberg 日语媒体页转 RSS | 核心实现；共约 7 KB，标准库即可运行 |
| `feed.xml` | 生成结果 | 最多 50 条，链接到 Yahoo 文章页 |
| `.github/workflows/update.yml` | 每 30 分钟更新 | 带 concurrency 和 push retry，避免并发提交冲突 |
| `archive/fetch_and_build_googlenews.py` | 旧 Google News 方案 | Bloomberg 原站跳转；现作为历史参考 |
| `index.html` | GitHub Pages 入口 | 极简 RSS 订阅页面 |
| `README.md` / `CHANGELOG.md` | 使用与变更记录 | 描述了从 Google News 迁到 Yahoo 的原因 |

### 1.1 抓取逻辑

1. 请求 `https://news.yahoo.co.jp/media/bloom_st`。
2. 用花括号深度、字符串和转义状态匹配 `window.__PRELOADED_STATE__`，这比一段贪婪正则稳健。
3. 读取 `mediaArticleList.list`。
4. 使用 `headline`、`newsLink`、`dateString`、`timeString` 和缩略图日期。
5. 跳过 `isPay=true`，每页 25 条，最多两页 50 条。
6. 生成 RSS 2.0，条目链接保留为 Yahoo URL。

### 1.2 时间处理

优先从 `/amd-img/YYYYMMDD-...` 提取年月日，再组合 `timeString`；缺少缩略图日期时从
`dateString` 推断年份，并处理跨年。这个设计对 Yahoo 列表字段较实用。

### 1.3 上游边界

- 只有 Bloomberg 日语单源。
- RSS 条目只有标题、链接和时间，缺少摘要、正文、作者、来源证据。
- `__PRELOADED_STATE__` 属于页面内嵌结构，字段变化后需要同步适配。
- 50 条上限适合订阅流，不适合长期历史库。
- 文章删除后旧 Yahoo 链接可能失效，所以本项目另外保存 JSON 快照和正文状态。

## 2. 本项目扩展

本项目没有直接修改上游快照，所有实现位于新文件夹根目录。核心改动：

1. Yahoo Bloomberg 与 Yahoo Reuters 共用媒体页解析器。
2. 新增 Yahoo Finance Japan Reuters 页面解析器。
3. 支持 RSS 2.0、Atom、`content:encoded`、`dc:creator`。
4. 接入 Reuters Arc 和 Bloomberg 10 个分类 RSS。
5. 接入 CNA、Yahoo Finance、Investing.com 等原生 RSS。
6. 为 TradingView、TBS、Bloomberg Línea、MSN 等生成搜索索引型 RSS。
7. 页面增强优先读 Yahoo state、JSON-LD、meta，再提取 article 元素。
8. 输出四级分类和每条 evidence/confidence。

## 3. “转载稿”与“引用后改写”判定

### 3.1 强证据

- 发布方自有 RSS/Arc endpoint。
- Yahoo 的 `mediaId=reut` / `mediaId=bloom_st` 专页。
- RSS `author`/`dc:creator` 或页面 author 精确为 Reuters、Bloomberg。
- `Source: Reuters`、`© Reuters`、`Reporting by ...; Editing by ...`。
- 日文 `提供：ロイター`、`配信：ブルームバーグ` 等来源行。

这些进入 `wire_original` 或 `wire_syndication`。

### 3.2 弱证据

- `Reuters reported` / `according to Reuters`。
- `Bloomberg News reports` / `according to Bloomberg`。
- `ロイターによると` / `ブルームバーグが報じた`。

这些进入 `wire_attribution`。查询词、平台品牌、标题尾部的站名不单独构成转载证据。

### 3.3 索引证据

Google/Bing 搜索命中但文章页未呈现强/弱证据时进入 `discovery_candidate`，与已确认池隔离。

## 4. 端点实测

2026-08-18 的完整构建覆盖 39 个 source entries。实测活跃端点包括：

- Reuters Arc outbound MSN RSS。
- Bloomberg business、markets、economics、industries、technology、politics、wealth、opinion、crypto、businessweek RSS。
- Yahoo!ニュース `media/reut`、`media/bloom_st`。
- Yahoo!ファイナンス `news/media/reut`。
- Yahoo Finance `news/rssindex`。
- CNA latest、business、world RSS。
- Investing.com news RSS。
- MINING.com、Mint、Economic Times、Straits Times RSS。
- Google News RSS 和 Bing News `format=rss`。

TradingView、TBS NEWS DIG、Bloomberg Línea、MSN、MarketScreener 的常见 `/rss` 或 `/feed`
路径在本轮检查中没有返回可用新闻 XML，因此项目采用 Bing→Google News 的索引回落并仍输出
平台专属 feed。

## 5. 正文和摘要

| 路径 | 摘要 | 正文 |
|---|---|---|
| Reuters Arc | RSS description | RSS `content:encoded` |
| Bloomberg RSS | RSS description | 通常只有摘要 |
| Yahoo News Japan | 文章 lead / extractive lead | `articleDetail.paragraphs` |
| Yahoo Finance Japan | state summary | `mainNewsArticleDetail.paragraphs[].body` |
| CNA 等 | RSS description / JSON-LD description | JSON-LD articleBody 或 article 元素 |
| Google/Bing | 索引摘要 | 页面增强成功时写入；否则保留候选状态 |

每条均写入 `summary_source`、`body_source` 和 `has_full_text`，因此上层程序可按正文质量过滤。

## 6. 自动化和故障隔离

- 同时抓取多个来源，单源异常写入 `health.json`，其余来源继续产出。
- 文章页增强有独立数量上限，减少每轮耗时。
- 写文件采用同目录临时文件 + `os.replace`。
- GitHub Actions 每 30 分钟运行单元测试、构建和 XML/JSON 校验。
- 更新 workflow 复用了上游的串行调度、远端重置和 push retry 思路。

## 7. 实测产物

完整统计写入 `data/health.json`。对应构建会生成 7 个聚合 RSS、1 个去重 RSS、
每个来源一个 RSS、完整 JSON 和 OPML。`verify_outputs.py` 会解析所有 XML，并检查每条分类字段。
