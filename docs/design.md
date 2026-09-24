# Daily Intelligence 系统设计文档

> 面向独立实现者的完整设计参考。本文档描述一套个人财经情报系统的设计思路、体系结构和实现细节，适合在自有 Claude Code 环境中按需裁剪复用。
>
> **最后更新**：2026-09-24（同步 2026-09-23/24 云端 session 合并的 PR #94–#97：Pass 2 截断正文判为失败 + `max_tokens` 32000 + HTTP 超时 640s（#94）；社交舆情只查非 ETF 个股（#95）；Extract 补齐到同一 credit 档、搜索 `max_results=3`、直链解析日志（#96）；Pass 2 供给事件例外（#97）。同时把第二节流程图、§5.1/§5.1b/§5.2/§5.4、第八节选型表、第十节目录结构改写为 issue #87 PR #89（已合并）之后的情报快照架构；#89 之前的 Search+Extract 三层设计降为 §5.1c 历史记录。另回补仓库版独有的 2026-08-04（#59/PR #61）变更记录，更正 Parallel SDK 版本，标注 §8.3 追问成本表过时。仓库 `docs/design.md` 同日按本版整体同步。详见文末变更记录）

> **本文件与 Obsidian 权威版本的关系**：作者本人的实时权威版本维护在私有 Obsidian vault（`Hermes/Daily Intelligence/Daily_Intel设计文档.md`），Session 初始化规则要求每次开发都先读那份。本仓库这份是手动同步的快照，供不使用 Obsidian 的其他实现者参考——内容一致，但更新可能滞后于 Obsidian 版本一次提交的时间差。

---

## 一、系统定位与设计原则

### 1.1 定位

Daily Intelligence 是一套面向**个人主动投资者**的每日财经情报系统。它不是通用财经资讯聚合，而是围绕特定用户的持仓、投资框架和关注主题，提供**个人化**的情报收集、分析和交互能力。

核心差异点：
- 输出不是"市场发生了什么"，而是"这件事对我的持仓和框架意味着什么"
- 系统主动记忆用户的历史判断和关注演变，而不是每次从零开始
- 用户可以通过自然语言对话调整监控范围、追问细节

### 1.2 设计原则

**隔离性**：系统完全独立于宿主 AI Agent 运行，不共享数据库、不共享 Telegram bot、不共享调度器。依赖的外部资源均只读借用。**API key 完全独立**：所有 DI 脚本只读 `~/Daily_Intelligence/.env`，不 fallback 到 `~/.hermes/.env`；各项目持有独立 key，互不影响（OPENROUTER 两边各一份，未来可分别替换）。

**Fail-open**：所有可选增强（知识库上下文）在不可达时静默跳过，不阻断主流程。

**成本可控**：每个 LLM 调用都有明确的定价意识，避免在不需要推理能力的环节使用昂贵模型。

**可观测**：每次运行的关键决策（skip 原因、Tavily 使用量、LLM token）写入日志，追问记录持久化到 Obsidian。

---

## 二、系统架构总览

```
每日定时任务（launchd）
  AM  5:30 AM PT = 8:30 AM ET   开盘前简报
  PM  5:10 PM PT = 20:10 ET     夜盘动向（NYSE 盘后结束后 10 分钟）
      │
      ▼
run_finance.py —— 主入口：NYSE 交易日检查 → 并发锁 → 防重（月度文件）
      │
      ▼
fetch_prices —— 价格 + 当日异动 + 3/5 日累计涨跌（issue #80 阈值：3 日 ≥15% / 5 日 ≥20%）
      │
      ▼
Pass 0（intel_pass0.build_intel_snapshot，免费，零 LLM / 零 Tavily）
      按标的收集 Finnhub company-news、公司名 Google News RSS、7 RSS + Guardian
      → 别名边界匹配、标的内去重、seen_before 标记；收集整体失败则生成带错误覆盖的应急快照
      │
      ▼
代码层 skip —— 无标的异动、无标的新闻、无命中地缘话题 → 退出（零付费调用）
      │
      ▼ 背景采集
  ├─ memory_context：MemPalace + Obsidian，Layer B 持仓框架
  ├─ Sonar 宏观快照（perplexity/sonar，~$0.005）
  ├─ 社交舆情：Polymarket + Adanos X + Apify Reddit（只查非 ETF 个股，_social_tickers，最多 4 个，PR #95）
  └─ FRED 流动性快照（档位与上次成功报告不同才注入 Pass 2）
      │
      ▼
代码 Pass 1（intel_deepen.deepen_intel_snapshot，无 LLM）
      按绝对涨跌取最多 5 个异动/多日阈值标的；每标的最多 2 条不同落地域名的直链或 Finnhub 302
      无直链才发 `Why is {公司名} stock {up|down}` basic 搜索（max_results=3，SerpApi fallback）
      最多 3 次搜索 + 1 次 Extract（≤10 URL，补齐到同一 credit 档，PR #96），单次运行 ≤5cr
      → 正文片段 + 置信度标签回填快照 → archive_intel_snapshot 原子存档
      │
      ▼
LLM Pass 2 —— openai/gpt-6-luna via OR/OpenAI，reasoning.effort=xhigh，max_tokens 32000（issue #90 / PR #94）
              输入：价格表 + intel_render 渲染的情报快照 + Sonar + 报告涉及标的的社交舆情
                    + 状态变化的背景信号 + 近 5 个交易日已报道内容 + 持仓框架
              Layer A: SYSTEM_PROMPT_P2（Layer_A_Prompt.md）；输出裸 markdown
              空文本 / 异常 / finish_reason=length 截断 → 同模型重试 → fallback → 代码摘要 + TG 告警
      │
      ▼
SAS 候选独立抽取（gemma-4-31b-it，只读情报快照）+ PM 预判校准
      │
      ▼ 输出
  ├─ Obsidian 月度报告 + 月度 Context Log
  ├─ MemPalace per-day drawer
  └─ 邮件 + Telegram 推送（Markdown→HTML，超4096字自动分段）
                        + 独立运行状态消息（build_status_message()）
```

```
Telegram Bot（常驻 long polling，独立 bot token，与宿主 Agent 隔离）
      │ 用户发消息 → 立即回"收到，处理中..."
      ▼
Step 1 —— V4 Flash 统一预处理（via OR/DigitalOcean→Venice）
         意图分类 + 搜索词 + relevant_tickers + framework_focus
      │
      ▼ 按 action 分流（三路）
  ├─ 指令执行（改 watchlist）
  ├─ 状态报告（系统状态）
  └─ 自然语言追问：
        Step 2 —— yfinance 实时行情+新闻（免费，session-aware 三层路由）
              │
              ▼
        Step 3 —— Parallel.ai search+extract（主）→ Sonar（fallback）→ Exa（再 fallback）
              │
              ▼
        Step 4 —— openai/gpt-5.6-luna（非pro）via OR/OpenAI，reasoning.effort=high（主，自管重试，issue #60）
                 持仓框架 + MemPalace + question_intent → 个人化推理
                 fallback: x-ai/grok-4.5（via OR，reasoning.effort=medium）
              │
              ▼
        月度文件 append（## 追问 YYYY-MM-DD HH:MM ET）
```

---

## 三、两层知识体系

系统的核心价值在于能够把**实时外部情报**和**用户历史知识积累**结合起来。这依赖两层知识体系协同工作。

> 原设计含第三层 Knowledge Graph（实体关系图谱），2026-06-12 全面下线，详见文末变更记录。

### 3.1 第一层：Obsidian Vault（结构化文档）

**定位**：人类可读的持久化知识库，用户可直接编辑，系统也可写入。

**关键文件：**

| 文件                                                                     | 内容                  | 写入方                  |
| ---------------------------------------------------------------------- | ------------------- | -------------------- |
| `Hermes/Daily Intelligence/watchlist.md`                               | 监控配置（标的/关键词/阈值/收件人） | 用户手工 或 TG 指令         |
| `Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_YYYYMM.md` | 每月报告 + 追问记录         | 系统 append            |
| `Finance/portfolio_report_latest.md`                                   | 最新持仓快照              | portfolio-agent 覆盖更新 |
| `Finance/金融资产信息.md`                                                    | 投资框架、资产配置策略、历史决策（TG追问流水线 `telegram_commands.py::_load_framework()` 仍在用）    | 用户维护                 |
| `Finance/Investment Operating Manual v1.0.md`                            | 能力边界/Expectation Gap/SAS/Portfolio Construction 完整决策框架（AM/PM 日报 `run_finance.py::_load_framework()` 使用，issue #30，2026-07-08起） | 用户维护                 |
| `Hermes/Daily Intelligence/SAS候选证据日志.md`                            | Pass 2 命中 Manual 第7.4/第6节内部信号时自动 append 的证据队列，不参与自动打分（issue #31，2026-07-08起） | 系统 append           |

**月度报告格式：**
```markdown
# Daily Intelligence 2026-04

## 2026-04-29 开盘前简报
_Tavily: 1/10_

[LLM生成的报告内容]

---

## 追问 2026-04-30 08:31 ET

**追问：** QCOM为什么昨天盘中盘后剧烈上升？
**情报（Sonar，搜索词：QCOM stock surge April 29 2026）：** [Sonar完整简报]
**回答：** [Claude个人化推理]

---
```

### 3.2 第二层：MemPalace（向量语义索引）

**定位**：通过 bridge REST API 提供语义搜索，解决"我之前对这件事怎么看"的问题。

**接入方式：**
- bridge 地址：宿主机 `http://localhost:8765`，容器内 `http://host.lima.internal:8765`
- 核心端点：`POST /mempalace/search`，参数 `{query, wing, room, n_results, max_distance}`
- Daily Intelligence 报告（日报/追问）使用 `wing=paperview, room=hermes`；投资框架笔记在 `room=finance`
- `memory_context_finance.py` 异动 ticker 查询同时搜 hermes（日报历史）+ finance（投资笔记）

**查询时机：**
- 报告生成前：拉取与异动标的相关的历史上下文（fail-open）
- TG 追问前：拉取与问题相关的历史讨论（fail-open）

---

## 四、监控配置管理（watchlist.md）

watchlist.md 是系统唯一配置入口，修改后下次运行自动生效。

```markdown
## 个股与基金
INTC, NVDA, ORCL, QCOM, TSLA, AMKR, QQQM, VOO, EWJ, SGOL

## 商品期货
GC=F, CL=F, ^TNX

## 汇率
USDCNY=X, USDJPY=X, DX-Y.NYB

## 地缘政治关键词
US-Domestic: Trump, tariff, Fed, Federal Reserve, recession
US-Iran: Iran, nuclear, Strait of Hormuz, Middle East

## 异动阈值
stock_pct: 3.0
commodity_pct: 2.0
fx_pct: 1.0
tnx_bps: 10

## 收件人
user@example.com
```

**Telegram 指令修改：**
```
加 MSFT / 删 INTC          → 增删个股
加关键词 US-Iran blockade  → 增删地缘政治关键词
加收件人 x@x.com           → 增删收件人
```

---

## 五、每日情报收集流程

### 5.1 数据采集

**价格（yfinance → Finnhub fallback，slot 感知）**：`fetch_prices(slot=run_slot)` 根据报告时段采用不同的价格基准。必须在宿主机运行——yfinance 依赖 `fc.yahoo.com` 初始化，容器内不可达。

| slot | 主数据源 | price 含义 | change_pct 基准 | 异动触发 |
|---|---|---|---|---|
| `am` | 日线（prev_close/week）+ 1m prepost=True（盘前） | 9:30前最新成交价 | vs 昨收 | 盘前涨跌超阈值 |
| `pm` | 1m prepost=True（今日常规时段收盘/开盘+盘后，issue #69 起主源）+ 日线（仅 prev_close/5日涨幅） | 今日收盘价 | vs 昨收 | 日内超阈值 OR 盘后超阈值 |
| `daily` | 日线（兜底） | 最新日线收盘 | vs 前一日收盘 | 日内涨跌超阈值 |

`PriceRow` 新增字段：`afterhours_price`、`afterhours_pct`、`slot`。`format_price_table(slot)` 按 slot 输出不同列：AM 显示「盘前价/盘前涨跌（vs昨收）」，PM 增加「盘后涨跌」列。

yfinance 失败时 fallback 到 Finnhub `/api/v1/quote`（免费60 req/min）；Finnhub 无盘前/盘后数据，记 warning，返回日线等价数据。商品/FX（`GC=F`、`^TNX`、汇率等）在 Finnhub 免费 tier 无数据则跳过并记录。

日线 `period=8d` 与 AM/PM 盘前盘后 `period=2d` 均经 `_yf_download()`：拉取期间将 yfinance logger 提到 CRITICAL（issue #63 的 `_quiet_yfinance_logs()`，issue #67 扩到主路径），避免库对瞬时空响应打 `possibly delisted` ERROR 触发 Homepage 巡检。日线任一 ticker Close 有效行不足时 sleep 1s 再拉一次，用 `_merge_daily_ohlc()` 只把「第一次缺失、第二次可用」的 Close/Open 叠回第一帧——不整表替换（否则一次更差的 retry 会丢掉 Finnhub 补不上的商品/FX）。取值必须列名或 `Series.name` 等于目标 ticker，防止 yfinance 把多标的 bulk 收成单列时把幸存者价格写到别的 ticker 上。

**[已修复] issue #69（2026-09-03 发现，PR #70 squash `d457455` 已合并至 main，上表 PM 行“日线（今收）”描述已过时，实际主源已改为 intraday）**：上表 PM 行描述的“日线（今收）”这个数据源在 20:10 ET 运行时**不保证**已经落库当天这一行——2026-09-02/09-03 连续两个交易日，Yahoo 后端日线批量在收盘后 4h10min 仍未写入当天数据，PM 分支旧代码在 `_closes_today` 为空时静默回退成“批量表最后一行”（=昨天），导致 `price == prev_close`，18个标的的 `change_pct` 集体归零且**无任何日志信号**——直到用户拿手机 App 真实数据肉眼核对才发现（PLTR 报告写“收盘 $169.46”，真实收盘 $182.53）。修复后 PM“今日收盘/今日开盘”改由同一次运行里已经拉取的 `period=2d, interval=1m, prepost=True` intraday 提供（`_get_pm_prices()` 扩展为同时返回今日开盘，原先只被丢弃的今日常规时段收盘价现被启用），批量日线只保留给 5 日涨幅这类需要多日窗口的计算（`week_change_pct` 相应改为以批量表「今天之前」的最后一行为锚点往前数 5 个交易日，修正了旧代码假设批量表最后一行=今天导致的窗口错位）。若 intraday 也没有今天数据，不允许任何静默兜底，该 ticker 直接从 `rows` 中剔除 + WARNING 日志，由 `run_finance.py` 既有的失败标的检测机制接管，向 Pass1/2 prompt 注入禁引声明（沿用 2026-06-18 那次 yfinance 早间故障已验证的模式，非新发明）。排查记录、方案权衡、设计契约见 issue #69；实现细节、测试、review 与合并记录（已合并 `d457455`）见文末变更记录与开发部署日志。

**IBKR Client Portal Gateway（`ibkr/quotes.py`，隔夜/周末实时数据源）**：IBKR 自有 REST API，通过本地 gateway 代理，覆盖 ATS/OTC 隔夜时段和周末场外报价——这是 yfinance/Polygon 免费 tier 均无法覆盖的窗口。

**`_fetch_realtime_prices()` 三层路由策略（`telegram_commands.py`）：**

| 时段 | 主力 | Fallback 1 | Fallback 2 | 非实时标注 |
|---|---|---|---|
| 工作日 04:00-20:00 ET | yfinance Ticker.info（session-aware） | IBKR gateway | Finnhub | 无 |
| 隔夜 20:00-03:50 ET + 周末 | **IBKR gateway** | yfinance | Finnhub | yfinance/Finnhub 均标注"非实时" |

yfinance 字段按时段选择：`preMarketPrice`（04:00-09:29）/ `regularMarketPrice`（09:30-15:59）/ `postMarketPrice`（16:00-19:59，基准用今收）。`fast_info.last_price` 和 `yf.download(prepost=True)` 均只返回常规收盘，不含盘前/盘后，不可用。

`# FUTURE`：待 IBKR gateway 稳定后翻转优先级，改为 IBKR 全时段主力。代码中已用注释标出两处修改点。

**新闻（RSS 7个源 + Guardian API；issue #85 起停用五个综合新闻源）**：

| 源 | 定位 |
|---|---|
| FT World | 专业财经 |
| CNBC | 快速财经/市场速报 |
| MarketWatch | 市场数据驱动 |
| Foreign Policy | 地缘战略深度 |
| Seeking Alpha | 个股机构分析 |
| Reuters（via Google News RSS） | 综合/财经，<1h 延迟 |
| Digitimes | 台湾/大陆半导体供应链贸易媒体（issue #72） |
| Guardian API | 国际/财经/政治，结构化 JSON，20条/次 |

**停用（issue #85/PR #86，2026-09-23）**：NYT Business/World/Politics、BBC Business/World、Al Jazeera、AP、WSJ（后两者经 Google News RSS）移入 `RSS_FEEDS_DISABLED`，不抓取，URL 保留，挪回 `RSS_FEEDS` 即恢复。实测这五个源占 RSS 总量 57%，与持仓/科技/宏观相关的只有约 10%；在子串匹配打地缘标签、每个话题桶只留最新 5 条的情况下，它们把持仓新闻挤出了 prompt。停用后地缘面由 FT、Reuters、Foreign Policy、Guardian API 和 Sonar 宏观快照覆盖。这是情报收集重构（按持仓组织）的第一步。

过去 24h，按地缘政治关键词分类。Guardian API（`content.guardianapis.com/search`）fail-open，`GUARDIAN_API_KEY` 控制，结果合并进 RSS 统一时间排序。不可达：Politico（403）；Reuters/AP/WSJ 直连受阻，已通过 Google News 代理覆盖。

**Brave News（独立西方搜索引擎，issue #14，2026-07-15；PR #89 起主报告不再调用，`fetch_brave_news()` 与预算文件保留，以下为原设计）**：区别于 Serper/SerpApi 这类 Google 搜索代理，Brave Search API 是独立索引的搜索引擎。`fetch_brave_news()` 按 anomaly ticker + geo topic 逐条查询，最多4条/次，结果并入跨源去重流程后再供 Pass 1/2 使用。Brave Search API 于 2026 年取消免费层，现为 $5/月预付额度用完后按量计费（$0.003-0.005/次），因此加了硬性月度预算上限（`BRAVE_MONTHLY_LIMIT=800`，`finance_brave_budget.json`，到量自动停用不再调用，绝不产生额外扣费）。`BRAVE_API_KEY` 存于 `.env`。

**跨源标题去重（`score_and_filter()`，issue #14，同日；PR #89 起随开放池一起移出主流程，现在的去重是 Pass 0 按标的别名归类后的标的内去重，以下为原设计）**：多个新闻源同时报道同一事件（实测存档验证过真实案例：同一财报事件 4 份变体标题抢占 4/10 名额）时，字符级相似度（`difflib.SequenceMatcher`）实测对同事件不同措辞的标题失效（相似度仅 0.33-0.73），改用 token 重叠度（Jaccard，去停用词），且限定只在标题命中同一个 tracked ticker/geo 关键词时才比较，避免不相关新闻因共享通用财经词被误判重复。

**社交舆情与预测市场情报（`social_sentiment_section` 注入槽，issue #17，2026-06-25 起分三步接入）**：三个数据源按固定顺序拼接为同一个 prompt 注入槽（Polymarket + Adanos + Reddit；FRED 流动性 PR #89 起改为独立的 `liquidity_section`）。PR #89 起该槽经 `filter_social_lines()` 过滤，只保留情报快照中出现的标的，每个标的一行：

1. **Polymarket 预测市场**（`_polymarket_brief()`）：查询与 watchlist 关键词相关的预测市场当前赔率，作为市场对未来事件概率的隐含定价参考，免费公开 API，fail-open。
2. **Adanos X（Twitter）舆情**（`_adanos_x_sentiment()`）：按 ticker 查询社交媒体讨论热度（buzz/bullish/mentions/trend），月度预算 `ADANOS_MONTHLY_LIMIT`（`finance_adanos_budget.json`）。
3. **Reddit 舆情**（`_reddit_sentiment_brief()`，issue #17 第三步，2026-07-16）：官方 Reddit API 免费层限定非商业用途且 100 req/min 达不到本项目需求，改用第三方聚合 actor `benthepythondev/stock-sentiment-intelligence`（Apify 平台，专精 WSB/r-stocks/r-investing 股票情绪聚合，非通用 Reddit 爸虫），按次计费（$0.001/result + $0.00005/run），一次 API 调用批量查询全部待测 ticker（复用 Adanos 同一份异动优先级列表，最多4个），月度预算 `APIFY_MONTHLY_LIMIT=60` runs（`finance_apify_budget.json`）。

**Adanos/Reddit 标的列表只含个股（PR #95，2026-09-24）**：`run_finance._social_tickers()` 只保留 watchlist 个股中不在 `intel_collect.ETFS`（QQQM/VOO/EWJ/SGOL）里的标的，异动个股排在前面，最多 4 个，Adanos 与 Apify Reddit 共用这份列表。此前列表直接取异动标的，商品/FX/ETF 也会被送出：2026-09-23 PM 把 `CL=F` 发给 Adanos 收到 422，而 Adanos 只要收到 HTTP 响应就计入月度额度，这次调用白白消耗了额度。

**第三方字段消毒（issue #38，2026-07-21）**：以上三个数据源返回的字段（Polymarket question/outcome、Adanos buzz/bullish/mentions/trend、Apify ticker/signal/mentions_24h/rank_change）在拼进 markdown 前统一经过 `_sanitize_field()`（折叠空白+剔除控制字符+长度截断）和 `_sanitize_ticker()`（`^[A-Z]{1,5}$` 白名单，Apify 回传的 ticker 因是第三方在响应里给出、不可信，额外做请求集合成员校验）处理——未消毒时第三方响应里的换行符可伪造出假的 `##` markdown 小节边界，误导 Pass 1/2 对 prompt 结构的解读。

**Sonar 宏观快照（step 6c，AM + PM）**：`_sonar_macro_brief(slot, stocks, commodities, fx, geo_topics, now_et, portfolio_snapshot, price_table)` 调用 `perplexity/sonar`，query 从 watchlist 动态构建，随持仓变化自动演化。

| slot | 查询聚焦 |
|---|---|
| AM | 过去 12h 隔夜发展，对今日开盘有何影响 |
| PM | 当日盘面驱动因素 + 盘后/隔夜风险 |

system prompt 注入 portfolio 快照实现个人化。~$0.005/次，fail-open。PR #89 起旧 LLM Pass 1 已删除，Sonar 输出用在三处：Pass 2 正文、PM 预判校准、Context Log。SAS 候选抽取的输入只剩情报快照（#89 之前还能看到 RSS、Finnhub、Sonar、社交舆情和 Tavily），是否把 Sonar 加回 SAS 输入尚未决定，待观察 SAS 候选命中频率。

**防过时/防幻觉加固（issue #24，2026-07-02）**：Sonar 是搜索+合成模型，不是行情 feed，曾在同一份报告中与实时价格直接矛盾（声称 WTI 破 $100，实际价格 $68.58）。三重加固：① OR payload 加 `search_recency_filter: "day"`（实测确认 OpenRouter 会透传给 Perplexity，不会被静默丢弃），限制底层搜索只召回过去24小时发布的源；② 把 pipeline 中已经算好的 `price_table`（fetch_prices 输出）注入 system prompt 作为权威真实数据，要求若搜索结果与之冲突则以注入价格为准并明确标注冲突；③ prompt 要求每条具体断言必须带时间戳，若某话题无近 24 小时更新必须明说，不得拿旧信息冒充当前。`telegram_commands.py` 的 `_sonar_research()`（TG 追问流水线的 Sonar fallback，同模型同风险）同步加了 `search_recency_filter`。

**Finnhub 公司新闻（issue #87 PR #88/#89，已合并）**：主流程改由 `intel_collect.collect()` 对每个个股做一次完整 `/company-news` 拉取，AM 默认 36h、PM 24h，#80 多日阈值标的扩到对应涨跌起点；按别名归入情报快照的实体，记录原始条数和错误，详见 §5.1b。旧 `fetch_finnhub_news()`（每 ticker 最多 5 条、摘要 100 字的 prompt 注入）已退出主报告，仅 `sas_review.py` 仍在调用。Finnhub 302 链接在代码 Pass 1 里用 HEAD 解析成直链再 Extract。

**FRED 流动性水位快照（step 6e，AM+PM，issue #26，2026-07-02）**：`fetch_liquidity_snapshot()` 拉取银行准备金（`WRESBAL`）、SOFR（`SOFR`）、ON RRP 授予利率（`RRPONTSYAWARD`，注意不是 `RRPONTSYD`——后者是隔多逆回购**交易量**不是利率，实测数值差异巨大才发现搭错）、TGA余额（`WTREGEN`），按 `Hermes/Daily Intelligence/市场见顶预警指标.md` 的阈值分类【正常/观察/警戒】，整体取最高档。最初折进 `social_sentiment_section` 注入槽；PR #89 起改为独立的 `liquidity_section`，且只在档位与上次成功报告情报快照的 `context_state` 不同时才注入 Pass 2（`pass2_context.changed_background()`）。SRF用量 FRED 无对应序列，不自动化，留作文档里的人工检查项。选型理由：FRED 是比 Sonar 搜索更可靠的精确数据源（呼应 issue #24 的教训——LLM 搜索对精确数值不可靠，能用结构化权威数据源就不该靠 LLM 猜）。当前 Pass 2 prompt 规定：注入了 FRED 档位变化时允许写仓位小节，但只陈述本次背景变化，不把它当成单独的交易指令（旧版“第⑥条分析要求”随 PR #89 的新 prompt 替换）。

### 5.1b 情报快照与代码深挖（issue #87：PR #88/#89 已合并；PR #96 补齐）

**Pass 0 收集（`intel_pass0.py` / `intel_collect.py`，免费、零 LLM、零 Tavily）**：对 watchlist 个股（排除 QQQM/VOO/EWJ/SGOL）按标的收集 Finnhub company-news（AM 36h、PM 24h，多日阈值标的扩到涨跌起点）、公司名 Google News RSS（每次 HTTP 尝试含重试至少间隔 1s，与 Finnhub 使用独立线程池；链接只作线索，不解码原文）、现有 7 个 RSS 源和 Guardian。标题按别名边界匹配归入实体（中文别名用 ASCII 边界，拉丁别名用词边界）并做标的内去重。别名写在 watchlist `## 实体别名`（如 `INTC: Intel, 英特尔`），缺失时用 Finnhub profile2 补全并缓存到 gitignore 的 `entity_alias_cache.json`。与上一次运行快照归一化标题相同的条目标 `seen_before`。多日异动时 RSS/Guardian 共享抓取窗口扩到最早标的起点，再按各标的窗口分拣；报告与回放共用 `publication_window.py`。每个实体保存移动、覆盖（各来源原始条数与错误）、条目和 `fulltext`。收集整体异常时生成带错误覆盖记录的应急快照，不阻断报告。

**代码 Pass 1（`intel_deepen.deepen_intel_snapshot()`，无 LLM）**：
- **候选**：当日异动或 #80 多日阈值（3 日 ≥15% / 5 日 ≥20%）标的，按触发项的绝对涨跌取最多 5 个。
- **直链优先**：每标的最多 2 条标题命中别名、落地域名不同的 direct / Finnhub 302 链接。302 用 HEAD 解析 Location（不下载正文，最多 3 次尝试），结果在同一次运行内缓存，补齐轮不重复发 HEAD。`_direct_leads()` 每调用一次按标的记一行 INFO：`Deepen direct leads {ticker}: N leads (limit L), Finnhub redirects resolved a/b, X.Xs`（PR #96；背景是 2026-09-23 PM 在 FRED 与第一条 Tavily 之间有 76s 没有任何日志，时间耗在串行 HEAD 上）。并发解析和单标的解析上限暂不实现，先看日志数据。
- **无直链才搜索**：`Why is {公司名} stock {up|down}`，Tavily basic、`max_results=3`（PR #96 由 2 提高，仍是 1cr），SerpApi 作 fallback。发表窗口取多日涨跌起点，否则为报告日前 2 天（AM）/ 1 天（PM）到报告日。每次运行最多 3 次搜索。前 2 条结果进入首轮 Extract，第 3 条留作补齐备选。
- **Extract**：每次运行只调用一次，≤10 个 URL。
- **补齐到同一 credit 档（PR #96，2026-09-24）**：Extract 按 `ceil(URL 数/5)` 计费，2026-09-23 PM 只送 7 个 URL 却付了 2cr，和 10 个一样。首轮后按涨跌幅从强到弱补到下一个 5 的倍数（最多 10）：先取该标的下一条不同落地域名的直链（只限原本就走直链、或因 Extract 名额满被跳过的标的），再取搜索结果的第 3 条。补进来的 URL 排在列表末尾，预算不够时最先被截掉；补齐发生时记 `Deepen Extract top-up: +N URLs to M (same credit tier)`，快照字段 `extract_topup_count`。
- **预算**：最多 3 次 basic 搜索（3cr）+ 一次 Extract（≤10 URL，2cr），单次运行合计 ≤5cr；日上限 `TAVILY_DAILY_LIMIT=25`。Extract 前按剩余额度截断 URL 列表（`remaining × 5`），现有预算 helper 仍做最终预检与记账。
- **回填与存档**：正文片段（每条 ≤1200 字）与 `_source_confidence_tags()` 置信度标签回填实体 `fulltext`；`deepen_status`/`search_jobs`/`search_count`/`extract_url_count`/`extract_topup_count`/`extract_success_count` 写入快照，由 `archive_intel_snapshot()` 原子写入 `archives/YYYYMM/YYYY-MM-DD-{slot}-intel-snapshot.json`。`*-extract.md` 停止新写，历史文件保留。深挖整体异常时保留免费源快照继续出报告。

**Pass 2 输入渲染（`intel_render.py`）**：异动标的最多 25 条（标题、来源、时间、摘要、此前已报道标记、正文与覆盖）；无异动持仓最多 8 个标题；无异动观察标的一行；地缘话题每话题最多 8 条、总数最多 40 条。

**已知结构性取舍**：#89 收窄了输入（没有开放池，地缘话题只有标题没有全文），加上 Pass 2 要求“无进展就省略”，报告信息量可能偏少。这一点待观察几天正常报告后单独评估，PR #94–#97 均未处理。

### 5.1c 历史设计：Search+Extract 三层架构（PR #89 之前的主流程，已移除）

> 本节保留作决策记录：Search vs Extract 的成本比较、“LLM 判断必须在 Extract 之前”、关键词锚定的双精度设计等推理仍有参考价值。其中 LLM Pass 1、`score_and_filter` 开放池、语义过滤、必须解释 ticker 的预留名额（#82）、异动/多日/轮询搜索 job、7 天围栏、Extract 分批“约 20 URL / 4cr”均已随 issue #87 PR #89 移出主流程；`scoring_utils.py` 的置信度打标函数仍被 `intel_deepen.py` 复用。当前实现以 §5.1b 为准。

#### 背景与问题

原始流程：直接调用 Tavily advanced search（2 credits），返回 12 条结果并截断到 250-char 摘要。主要问题：
- **内容截断**：250 字摘要不足以支撑深度分析，LLM 看到的是碎片
- **无筛选层**：多条查询的原始结果直接堆叠，噪音多、token 浪费
- **时间精度**：`days=N` 是粗粒度过滤，Tavily 新增 `start_date`/`end_date` 可达到天级精度

#### 关键分析：Search vs Extract

| 能力 | Tavily Search | Tavily Extract |
|---|---|---|
| 定位 | 全网发现（不知信源在哪） | 已知 URL 的实时内容获取 |
| 返回 | URL + Tavily 算法摘要（200-300字）| 全文 chunk（600+字，基于 query 对齐）|
| 时效 | 依赖 Tavily 索引，可能有索引时滞 | 实时抓取，不受索引时滞影响 |
| 成本 | basic=1cr，advanced=2cr | basic=2cr（批量，最多 10 URLs）|

**核心结论**：`basic search (1cr) × 3 + extract (2cr) = 5cr` 与 `advanced search (2cr) × 2 = 4cr` 的成本相近，但前者拿到的是完整正文 chunk，后者只有截断摘要。对于个人投资者的金融情报场景，正文内容对比摘要有明显价値。

#### 筛选层设计：脚本 vs LLM

40条搜索结果 → 10条 URL，应该用脚本还是 LLM？

**已知可用信号**：Tavily 每条结果自带 `score`（0-1）、`published_date`、`url`、`content`（200-300字摘要）。这些信号已足够成原顺序：

```
综合分 = Tavily score（语义相关性）
         + 可信域名加成（Reuters/Bloomberg/FT/WSJ 等 +0.15）
         + 时效加成（24h内 +0.10，72h内 +0.05）
         + 关键词命中（异动 ticker 或地缘主题出现，+0.05×n）
```

**何时必须用 LLM**：当筛选意图是语义层面的（如“判断这条新闻是否构成实质性监管风险”）——这种意图 keyword 小不覆盖。但对于个人投资情报场景，问题是“这条新闻与我的持仓和地缘主题相关吗”——Tavily score + 可信域名 + ticker 命中就能覆盖大部分情局。

**关键原则：LLM 判断必须在 Extract 之前，不是之后**。先 Extract 40条再判断 = 浪费 2cr×40个 URL 的抓取消耗；先用摘要做分类再 Extract 前 10 = 按需投入。

**语义过滤的非显然价值**：prompt 要求考虑上下游供应链和宏观传导，而非仅 ticker 名字命中。例：TSMC 产能收缩新闻即使不提 INTC，也与 INTC 高度相关。纯脚本关键词匹配覆盖不到这类语义关联。

当前实现（issue #82，2026-09-23）：必须解释的 ticker 先各留一条 Extract URL，不参加开放池排名。开放池才走脚本预筛选（`score_and_filter`，25条）+ `google/gemma-4-31b-it` 语义排序（`_semantic_relevance_filter`，约15条；issue #53/PR #54 前身为 `_haiku_relevance_filter`）。语义过滤仍识别上下游供应链和宏观传导，而不只是 ticker 名字。

#### 三层流程设计

```
Layer 1 — Discovery（basic search × 2-4，每条 1cr）
  全部为 basic（不再使用 advanced，Extract 来补深度）
  合并原始结果 raw_results（20-60 条）

必须解释的名单（issue #82）
  `_must_answer_tickers()` 是唯一组装点。异动 job 与未解释大涨 job 都写
  `_must_answer_ticker`。7 天围栏、开放池 keyword bonus、两次 Extract 重排 query
  只读这份列表，不再各自拼接类别字段。
  每个名单内 ticker 在自己的结果里留一条 URL（域名/时效/视频页，不含 keyword bonus）。
  子集为空则不占名额。选中的 URL 从开放池剔除。

Layer 2a — 开放池脚本预筛（纯脚本，0cr）
  score_and_filter: 开放池 → 25条，去重 + 综合评分
  keyword bonus 的 ticker 来自 must-answer 名单，不是全量异动代码

Layer 2b — google/gemma-4-31b-it 语义过滤（OR，不 pin provider，~$0.0001-0.00014/次，issue #53/PR #54）
  _semantic_relevance_filter: 25条 → 约15条（前身为 _haiku_relevance_filter）
  只排开放池。识别直接催化剂、上下游供应链、宏观传导渠道
  fail-open：主+备均失败则回退到 script top-15

Layer 3 — Extract（1cr/5 URLs；单次仍最多 10 URL = 2cr）
  预留 URL 先调用，query 是整份 must-answer 名单（Tavily 用它重排 chunk）
  开放池再按 10 个一批调用，query 是同一份名单加地缘词（地缘词仍截 80 字）
  满载约 5 预留 + 15 开放 = 20 URL，最多 4cr。日上限仍是 25，本次不上调
  chunks_per_source=2。Pass 2 拿到正文 chunk，不是 250 字摘要
  归档候选标 `预留:TICKER` 或 `开放池`

Layer 3.5 — 信源置信度打标（issue #19，2026-06-30）
  每条 Extract 结果附加 [信源类型 | 发布时间 | 交叉印证] 标签行：
    信源类型：_detect_low_structure() 识别视频聚合页/caption堆叠（无独立时间戳，谨慎）
    发布时间：_lookup_published_date() 从 extract 前的 search 结果池按 URL 反查
              （Tavily /extract 响应本身不带日期字段，只有 /search 有）
    交叉印证：_compute_corroboration() 规则式事实指纹匹配（专有名词短语+日期/数字token+
              build_keyword_set() 提供的关键词锚定），统计候选池中有多少个其他独立域名与本条
              内容重叠——零 API/LLM 成本的启发式，存在假阴性，0 不代表"确认单一信源"而是
              "本规则未找到重叠"
  标签同时写入 Pass 2 prompt（LLM 参考）和本地 Extract Archive（审计留痕）
  背景：Reuters 视频聚合页孤立 caption（无时间戳）曾被 Pass 2 当作确定事实写入报告
  （"签署仪式定于周五"，用户核实后其他信源查无此消息）

  **关键词锚定的双精度设计（PR #46，2026-07-18）**：build_keyword_set(anomaly_tickers,
  geo_keywords, split_phrases) 同时服务两个精度要求不同的下游——score_and_filter() 的
  排序打分/标题去重（split_phrases=True，含 anomaly ticker + 多词地理关键词拆分出的单词，
  如"Middle East"→"middle"/"east"，单个词命中只贡献 +0.05 排序权重，误报代价低）和
  _compute_corroboration() 的跨源印证判定（split_phrases=False，只保留 watchlist.md 字面
  配置的关键词，不拆分、不含 ticker）。最初两者共用同一份宽松关键词表，上线前被 review
  发现真实假阳性：两篇无关报道仅共享拆分词"east"、或两篇同 ticker 不同事件的报道仅共享
  ticker 本身，都被误判为"已交叉印证"——因为"独立信源确认了同一事实"这个判定的误报代价
  远高于排序打分的误报代价。详见 docs/PITFALLS.md#81。
```

**语义过滤设计细节（Layer 2b）：**

输入：开放池预筛后最多 25 条摘要（每条 title + URL[:70] + snippet[:130]）+ 当日异动 ticker + geo 主题 + 持仓 ticker。必须解释的 ticker 不在这 25 条里重复占位，它们已有预留 URL。
输出：JSON 数组，按相关性排序，目标约 15 条。
总 token：~300-500 input + ~40 output = **~$0.000035/次**（DeepSeek 直连，原 Haiku/Bedrock 约 $0.0021，降低 60 倍）。

判断标准（优先级递减）：
1. 直接催化剂（财报、交易、监管行动）
2. 上下游供应链（上游元件提供商、下游 OEM 客户、代工厂）
3. 行业性监管/出口管制（直接解释异动原因）
4. 地缘事件对市场的可量化传导（制裁、冲突升级）
5. 宏观信号与持仓暴露相关（联储/利率/汇率/商品供应冲击）

**budget 触发规则（issue #82 起按批检查，不再为整次 Extract 做一次预检）：**

| 余额 | 行为 |
|---|---|
| 该批 `ceil(n/5)` 够 | 发出这一批。预留批次先于开放池 |
| 该批不够 | 跳过这一批。已经提取的批次保留；搜索摘要仍可作 fallback |
| 搜索阶段 < 1cr | 停止后续 search |
| = 0cr | 跳过搜索，继续生成 Pass 1 基础报告 |

**信用消耗对比：**

| 场景 | 旧流程 | 新流程 |
|---|---|---|
| AM 有异动 | 1 advanced(2) + 3 basic(3) = **5cr** | 4 basic(4) + 1 extract(2) = **6cr**，但全文 |
| PM 有异动 | 4 basic(4) = **4cr** | 前3异动各 1 basic + Pass1 basic + 1 extract；删除 Finnhub 短路后最多增加 **3cr**，Finnhub 仅补充 |
| 仅 geo，无异动 | 3 basic(3) = **3cr** | 2 basic(2) + 1 extract(2) = **4cr** |

Tavily 日预算为 25cr。issue #82 把单次运行的 Extract 从最多 2cr 提高到最多 4cr（预留最多 1cr，开放池 15 个 URL 为 3cr），日上限本次不改，先看真实消耗。

---

### 5.2 报告生成（Pass 2）

**代码层 skip**：`should_report(intel_snapshot)` 在没有标的异动（含 #80 多日阈值）、没有标的新闻、也没有命中地缘话题的宏观条目时退出（日志 `No entity move, company item, or macro item; skipping`），零付费调用。旧 LLM Pass 1（草稿 + `tavily_queries`）、异动/多日/轮询三类搜索 job 均已随 PR #89 删除，其演化过程见 §5.1c 与文末 #72/#76/#80/#82 变更记录。

**Pass 2（有报告材料时必跑，不再依赖是否有付费搜索结果）**：`report_pass2` stage，`openai/gpt-6-luna` + `reasoning.effort=xhigh`（issue #90/PR #91），`max_tokens` 32000（PR #94）。输入：价格表、情报快照段落（§5.1b 的渲染上限）、Sonar 宏观、报告涉及标的的社交舆情（每标的一行）、状态变化的背景信号（FRED 档位、15% 仓位跨越、52 周新高/低，只在与上次成功报告快照 `context_state` 不同时注入）、KB 上下文、AM 校准笔记、近 5 个 NYSE 交易日已报道内容（`recent_coverage.py`：按异动/多日阈值标的抽取实体段落，跨月读取，同档只取首份，排除当前档，每标的最多 600 字）、个人上下文（Layer B）。Layer A 私有文件里旧的“结论必须可操作”句在运行时精确替换，其余个人原则保留。

**提示词规则（`USER_PROMPT_TEMPLATE_P2`）**：
- 归因三状态：已知原因（附来源）/ 线索待核实（单一来源的强断言在句内标一次“未证实”）/ 未找到原因（只在这种情形下简短写来源覆盖）；检索失败且无条目写“未能完成检索”。价格变化本身不是原因，不凭空归因于情绪、资金流或风格轮动，也不因没找到线索就断言没有公司级催化。
- 只写相对“此前已报道”及近五个交易日报告的新增事实；无进展时省略，或一句“延续 MM-DD 已报道的<事件>，今日无新进展”。
- **供给事件例外（PR #97，2026-09-24）**：已排期的供给事件（限售股解禁、增发或 ATM 发行、配售、指数纳入或剔除调整）在生效日之前和之后的报告里都要保留，即使此前已报道也不算“无进展”；写明生效日期、规模，以及它与当日价格或成交的关系。这类事件的价格影响集中在生效日前后，按旧规则可能在生效当天被当作“无进展”省掉。触发案例：2026-09-23 PM 的 SPCX 约 3.28 亿股解禁、AAOI 至多 6 亿美元 ATM 发行。已知局限：规则只保证模型不删，不保证信息找得到——生效日当天如果没有相关新闻，Pass 2 根本看不到这个事件。候选方案是由代码按日期注入的“供给事件日历”，未实现。
- 仓位小节只在认知提升、Alpha 大幅兑现、更高赔率机会、单一仓位被动跨过 15%，或注入了 FRED 档位变化 / 52 周新高新低时出现；否则省略，不逐股声明“无加减仓依据”。
- 正文按主题写自然段，不写检索步骤、证据缺口清单或自我免责（PR #92）；直接输出 Markdown，不要 JSON 或代码围栏。

**失败与降级**：Pass 2 返回空文本、异常，或 `finish_reason=length` 的截断正文（PR #94 起即使正文非空也判为失败），`call_llm()` 依次走同模型重试、`fallback_model`（`google/gemini-3.5-flash` OR flex）；全部失败时 `run_finance.py` 用代码渲染的情报快照摘要照常写出与发送，并发 TG 告警「Pass 2 失败」。SAS 候选抽取（独立 JSON 调用，只读情报快照）和 PM 校准照常运行。

**错误韧性**：`call_llm()` 对网络/5xx/429 自动重试（指数退避），耗尽后进入 OR flex fallback；`parse_json=False` 路径的空文本或截断文本由 `_free_text()` 按解析失败处理、进入同一条重试路径。非流式 HTTP 读超时由 `_http_timeout()` 计算：`max(180, max_tokens // 50)`，32000 对应 640s（此前固定 180s，预算翻倍后会读超时）。`telegram_commands.py` 的 `_deepseek_post()`（沿用旧名，实际按 stage 配置走 OpenRouter）与 `_openrouter_post()` 使用相同的网络/5xx 重试策略。

### 5.3 防重与手动重跑

```python
if f"## {today_et} {slot_label}" in monthly_file_content:
    exit()  # 已存在本 slot 的报告，跳过
```
`FINANCE_FORCE_RUN=1` 或 TG "强制运行" 可绕过。

**手动重跑历史报告**：三个 env var 支持补跑：

| 环境变量 | 作用 |
|---|---|
| `FINANCE_FORCE_DATE=YYYY-MM-DD` | 强制报告日期（同时绕过交易日检查） |
| `FINANCE_FORCE_SLOT=am\|pm` | 强制时段（绕过实时时钟判断） |
| `FINANCE_FORCE_RUN=1` | 绕过防重检查 |

### 5.4 输出

| 输出 | 实现 | 被 mine |
|---|---|---|
| Obsidian 月度报告 `Daily_Intel_report_YYYYMM.md` | append section（step 11） | 是 |
| Obsidian 月度 Context Log `Daily_Intel_context_YYYYMM.md` | append section（step 11b）：价格快照 + 每标的情报快照摘要/覆盖 + Sonar 宏观原文 + 代码搜索任务列表 | 是 |
| 情报快照 `~/Daily_Intelligence/archives/YYYYMM/YYYY-MM-DD-{slot}-intel-snapshot.json`（PR #89 起） | 免费来源条目、覆盖、Extract 正文片段与置信度标签、深挖状态/计数（含 `extract_topup_count`）、供下次比较的 `context_state`；原子写入 | 否（Obsidian 之外） |
| Extract Archive `archives/YYYYMM/YYYY-MM-DD-{slot}-extract.md` | PR #89 起停止新写，历史文件保留 | 否 |
| MemPalace per-day drawer | report_md 推送 bridge，wing=paperview, room=finance | — |
| 邮件 | Gmail API（send+readonly scope） | — |
| Telegram | Markdown → HTML，超 4096 字符自动分段 | — |
| TG 独立运行状态消息 | `build_status_message()`（step 13b）：Tavily/SerpApi 本次用量+剩余、情报源状态（RSS/Guardian/Finnhub/Sonar/Tavily搜索+Extract）、LLM/Provider 清单；与正文分开发送，不进邮件/Obsidian | 否 |

**Context Log 与情报快照的设计分工：**
- Context Log 存 Obsidian → MemPalace 矿化后可语义检索"某日早上市场context是什么"
- 情报快照存本地 → 不污染矿化索引，保留完整免费来源条目和 Extract 片段，用于审计、回放（`intel_pass0.py --replay`）和跨次状态比较（`seen_before`、背景信号变化）
- Extract 全文刻意不进 Obsidian：原始网页抓取含导航/广告碎片，矿化会产生大量低质量向量

### 5.5 AM 预判校准闭环（issue #10，2026-07-02）

**定位**：把原本"盘后对比版本"的设想改造成闭环学习机制——AM 报告输出可验证信号，PM 报告校验并沉淀为知识，知识反过来影响未来的 AM。不新增调度任务，折进现有 PM pipeline（PM 已在盘后跑，已经算好 EOD 价格表）。

**AM 报告新增"可验证信号"清单**：`USER_PROMPT_TEMPLATE_P2`（Pass 2）新增条件性指令常量 `VERIFIABLE_SIGNALS_INSTRUCTION_P2`（最初还有 Pass 1 的 `_P1`，随 PR #89 删除），通过模板变量 `{verifiable_signals_rule}` 注入，仅 `run_slot=="am"` 生效。要求报告结尾固定追加"## 可验证信号"小节，2-4条条件-结果式可核验断言（如"WTI跌破$65→通胀预期继续下修"），不写模糊定性描述。报告主体的自由叙事写法不受影响（呼应 05-21"格式硬约束压制LLM深度"的教训，见踩坑记录#49）。

**PM 校验步骤**：新函数 `evaluate_am_calibration()`，插在报告标题修正后、`write_report()` 之前，仅 PM slot 执行：
1. `_extract_report_section()` 定位当天 AM 报告 section——以下一个日期戳 `## YYYY-MM-DD` 为边界，不被报告内部的 `## 子标题`/`---`分隔符误判（复用 2026-05-04 修复 KG section 截断 bug 时确立的模式，见踩坑记录#25）
2. `_extract_verifiable_signals()` 提取"可验证信号"小节内容
3. `_evaluate_am_predictions()`：一次 `am_calibration` stage 调用（最初为 DeepSeek V4 Flash，issue #59 起 `google/gemma-4-31b-it`，~$0.0005），对照当日实际价格表+新闻上下文（Finnhub+Sonar），逐条判定 hit/miss/inconclusive，提炼一段"知识条目"（不是罗列对错，是可迁移的教训或验证），并判断是否值得展示
4. 若当天 AM 报告没有该小节（历史报告、或该步骤本身失败），静默跳过，不影响主流程——整个函数 fail-open

**知识沉淀与备份（2026-07-02 修正：Obsidian 为主，不依赖 MemPalace）**：`_write_calibration_knowledge()` 写三份：
1. **Obsidian**（源之真实）：追加写入 `Hermes/Daily Intelligence/预判校准记录.md`，一段话式的教训记录，不是数据行
2. **本地备份镜像**：`backups/预判校准记录_backup.md`（项目目录下，已加入 .gitignore，不进代码仓库），与 Obsidian 独立写入相同内容，防 Obsidian 侧丢失（sync 冲突、误删）
3. **MemPalace drawer**（`room=finance`，锰上添花）：仅作为语义检索的可选增强层，不是任何环节的必需依赖

**为什么不依赖 MemPalace**（用户 2026-07-02 提出）：最初设计假设"AM 能通过现有 `get_finance_context()` 的 MemPalace 搜索自动捕到校准知识"——这是个未经验证的假设，那个搜索是通用 query，不是针对校准知识专门设计的，而且对用户描述的"MemPalace finance room 最近已多次全部重建"这种故障零容错。已改为 `_load_recent_calibration_notes()` 直接读 Obsidian——不经 bridge、不经 MemPalace，若 Obsidian 文件缺失/不可读自动 fallback 到本地备份镜像。注入 AM prompt（最初 Pass 1/2，PR #89 起只剩 Pass 2）新模板变量 `{calibration_notes}`，仅 AM slot 生效，取最近 5 条。

**写入安全**：所有写入都是纯 append（`_append_calibration_entry()`），不用 `open(path,'w')` 截断覆盖，符合项目文件写入安全原则。

**验证**：模拟了用户担心的确切故障场景——写入两天数据后删除 Obsidian 文件（模拟 room 重建/文件丢失），确认读取正确 fallback 到本地备份并完整恢复内容；MemPalace 调用失败也确认不会阻断 Obsidian/备份的写入。

**展示克制**：评估 LLM 调用里同时输出 `worth_surfacing` 判断，原则性指导（不是硬规则，观察一段时间再评估是否收紧）：只有高置信度判断被推翻、核心框架逻辑被验证、或存在需要立即警惕的偏差模式时才算"重要"；普通命中/未命中是常态，不展示。若判断为重要，`surface_blurb` 会被机械地追加进 `report_md`（"## 预判校验"小节），随邮件/Obsidian/TG 一并发出；否则报告不受任何影响。

**可回滚性**：全部新增逻辑收在几个自包含函数里，只有一个调用点（`evaluate_am_calibration(...)`），不与周围代码交织，即使未来这个文件的其他部分被修改，这块改动依然能干净地单独 revert。

---

## 六、Telegram 双向交互系统

### 6.1 Bot 配置

- 独立 bot token，与宿主 Agent bot 完全隔离（避免 getUpdates 消息争抢）
- Long polling（服务端 timeout=30），launchd KeepAlive 常驻，响应延迟 < 1s
- 收到消息立即回"收到，处理中..."，再做 LLM 分类（避免用户等待感知延迟；不用 emoji）

**容错层（`scripts/telegram_utils.py::call_telegram()`，2026-07-01/02，issue #20-23）**：`run_finance.py`（发送报告/告警）和 `telegram_commands.py`（轮询）共用同一个底层调用函数，不再各自手写 `httpx.post()`。两层防护：
1. **客户端 timeout 必须长于服务端长轮询等待时长**（issue #20）：早期 `_tg()` 对所有调用统一用 `timeout=10`，但 `getUpdates` 请求体里 `payload.timeout=30` 是告诉 Telegram 服务端最多挂起 30 秒等新消息，客户端比服务端早 20 秒放弃，几乎每次空轮询都自己打断自己（5天四万多条 `ReadTimeout` 日志），还引发 409 Conflict。修复：`getUpdates` 显式传 `timeout=POLL_TIMEOUT+5`。
2. **轮询无状态化，不在单次调用内重试**（issue #22/#23 提出同步重试 → issue #25 简化为无状态）：本机 Shadowrocket TUN 隐道对新建到 `api.telegram.org` 的 TLS 连接有约 25-30% 瞬时失败率（固定~3.2秒内 `ConnectError`，对照测试确认与 Slack/OpenAI 同时段同递道均无此问题，是 Telegram 域名特定，大概率是 Shadowrocket 分流规则把被墙服务单独路由到不稳定节点）。初次方案（`call_telegram()` 对 `ConnectError` 同步重试一次）被用户复查指出"太重"——轮询循环本身每 ~30 秒自然重跑一次，循环节奏就是现成的重试机制，不需要在单次调用内再套一层同步重试。最终方案（`run()` 的 `getUpdates` 调用绕开 `call_telegram()`，直接单次 `httpx.post()`）：失败静默跳过（`sleep(5)` 交给下一轮，不记日志），用 `failing_since` 时间戳追踪连续失败起点，持续失败 ≥ 30 分钟才升级为 `WARNING`（并重置计时器避免每轮重复报警）；恢复时记 `logger.info("getUpdates recovered after Ns")`（真实停机秒数，不是重试次数）。`sendMessage` 类调用（确认消息/最终回复/报告推送）没有"下一轮"天然兑底，仍然走 `call_telegram()` 的同步重试。这个原则适用于所有不稳定外部依赖：**容错要覆盖依赖的全部调用路径（轮询/发送/告警），日志级别反映"是否需要人关注"而非"底层是否发生过一次抱动"**，但对有天然重试循环兼企的调用（如轮询）而言，连循环本身的重跑节奏都算容错，不需要另套同步重试。

### 6.2 统一预处理（单次 V4 Flash）

所有消息经一次 V4 Flash 调用完成意图分类 + followup 上下文提取：

```json
{
  "action": "add_ticker|remove_ticker|add_geo|remove_geo|add_recipient|remove_recipient|status|force_run|followup|unknown",
  "section": "个股与基金",
  "item": "MSFT",
  "query": "QCOM after-hours surge April 29 2026 earnings catalyst",
  "relevant_tickers": ["QCOM", "INTC", "NVDA"],
  "framework_focus": "Dream Bucket INTC thesis",
  "question_intent": "用户想评估QCOM盘后大涨是否影响其Dream Bucket逻辑"
}
```

预处理 prompt 注入今昨日期和持仓快照，确保时间推算和持仓识别正确。所有 prompt 均包含当前时间（`%Y-%m-%d %H:%M %Z`，动态输出 `EDT`/`EST`）并明确要求 LLM 以 NYSE 时区（America/New_York）进行时间推理。

### 6.3 追问四步流水线

```
Step 1  V4 Flash 统一预处理（~$0.0001）
        → 意图分类 + 精准英文搜索词 + relevant_tickers + 框架考量

Step 2  实时行情（三层路由）+ yfinance.news（免费，无配额）
        → _fetch_realtime_prices()：按 ET 时段路由数据源
            工作日 04:00-20:00 ET：yfinance Ticker.info（主）→ IBKR → Finnhub
            隔夜/周末：IBKR gateway（主，ATS/OTC 真实报价）→ yfinance（"非实时"）→ Finnhub（"非实时"）
        → _fetch_yfinance_news()：过去48h内的相关新闻标题+时间（ET）+来源
        → 各有 Finnhub fallback（常规时段价格 + company-news，免费60 req/min）
        进度提示："获取实时行情及新闻（TICKER）..."

Step 3  Parallel.ai search + extract（主，~$0.007-$0.012）
        → _parallel_research(queries: list[str])：
            search(search_queries=queries, objective=queries[0]) → 最多10条结果，单次请求
            dedup by title
            [P2] aggregator URL 优先排序：_AGGREGATOR_DOMAINS（stockanalysis/macrotrends/finviz/
                tipranks/finance.yahoo.com/seekingalpha 等）score=0，其余 score=1，sort 后取 top 3
            extract(urls=top3, objective=query) → full_content（4000字/篇上限）
            段落级去重（跨文章，去除<60字短行/导航/链接）
            剩余3条结果取 excerpt 摘要
        → Step 1 输出 search_queries[2条]：[0]事件角度，[1]量化/技术角度（options IV、历史模式、分析师目标价）
          ⚠️ 历史 bug（2026-05-21 上线→2026-05-23 修复）：_preprocess_question 未传递 search_queries
             字段，导致 _llm_followup 的 ctx.get("search_queries") 始终 None，退化为单条 query。
             修复后两条互补 query 正式生效，日志应显示"情报检索（2条查询）"。
        [P1] 自适应第三条 query（_detect_research_gap()，~$0.0001 + 可能 $0.005）：
            Parallel 成功后，V4 Flash 判断是否存在明显盲区（缺价格路径/市场反应/基本面解释之一）
            有则生成第3条补漏 query 并再次调用 Parallel；无则直接进 Step 4
            触发时进度提示："情报补充（补漏查询）..."；fail-open，不影响主流程
        → 原始全文直接传入 Step 4，无预摘要损耗
        → 失败 fallback：Sonar（重试1次→Exa model="exa"）
        进度提示："情报检索（N条查询）..."

Step 4  DeepSeek V4 Flash via OR/DigitalOcean→Venice（主，~$0.002）
        System: 投资框架（module-level cache）+ NYSE 时区推理要求
                + 禁止对话体开场白（直接进入分析）
        User:   当前时刻(EDT/EST) + yfinance实时行情（价格基准） + yfinance.news
                + Parallel.ai原文情报 + 持仓快照（均价，非现价）+ MemPalace
                + question_intent
                + 推理规则：以yfinance为价格唯一基准；无新催化剂直接声明动量延续
        max_tokens=8000；thinking=disabled；自管3次重试；fallback: x-ai/grok-4.3（via OR）
        输出：自由展开分析（不设字数上限，参考维度：驱动力/持仓含义/待验证信号/信息缺口）
        注：Step 4 绕过 _deepseek_post()，自管重试确保 model_label 精确、Grok fallback 正确触发
        注：DS_OR_PROVIDERS={"order":["DigitalOcean","Venice"],"allow_fallbacks":True}（2026-06-02 起，NovitaAI 不再服务 V4 Flash）；经 OR 路由而非直连 DeepSeek 是全项目统一的隐私考量

总成本：~$0.010/次追问（无 P1 触发）；P1 触发时 ~$0.015（+$0.005 额外 Parallel）
```

**成本对比：**

| 步骤 | 原始方案 | 最终方案 | 差额 |
|---|---|---|---|
| Step 3 情报 | Sonar $0.005（搜索+摘要） | Parallel 2query+3extract $0.007（原始全文） | +$0.002 |
| Step 4 推理 | Claude Sonnet ~$0.019 | DeepSeek V4 Flash ~$0.002 | -$0.017 |
| 合计 | ~$0.025 | ~$0.009 | **-64%** |

**关键设计决策：**
- Gemini 3.5 Flash（中间尝试方案）被否决：输出过于关注格式正确，缺乏深度分析；改回 V4 Flash
- 5节硬格式→参考建议：硬格式迫使 V4 Flash "填格子"，去掉约束后分析深度立即对标 Hermes
- 持仓快照注入 `均价`（成本价），而非 `现价`（报告日市价）——防止 LLM 误以为旧现价是成本

**数据来源优先级：**
- 交易时段：`yfinance（权威）> IBKR gateway > Finnhub`
- 隔夜/周末：`IBKR gateway（权威）> yfinance（非实时）> Finnhub（非实时）`
- 情报：`Parallel.ai原文（全文，当日）> Sonar（摘要合成，fallback）`

追问完成后 append 到月度文件（`## 追问 YYYY-MM-DD HH:MM EDT/EST`）。

### 6.4 状态报告

`状态` 指令返回：当前 watchlist + Tavily 用量 + 最近 AM/PM 报告 + launchd 任务 PID + 推理流水线 LLM 配置。

---

## 七、个人上下文注入设计

### 7.1 投资框架

**TG 追问流水线**（`telegram_commands.py::_load_framework()`）：从 `Finance/金融资产信息.md` 提取：总体构架（目标配置比例）+ Dream Bucket 逻辑（高弹性标的选择标准）。注入 Claude 的 system message，跨调用复用。

**AM/PM 日报**（`run_finance.py::_load_framework()`，2026-07-08 起，issue #30）：改从 `Finance/Investment Operating Manual v1.0.md` 提取三段运行性规则——第2节能力边界、第6节 Portfolio Construction（含认知提升标准/减仓触发情形，2026-07-09 issue #34 起不再用字母代号标注）、第7.4节 Expectation Gap 内部信号清单——按标题正则定位，Manual 编辑后自动同步无需改代码。与 `_get_portfolio_snapshot()` 一同注入 **user message**（Layer B，非 system message）。Pass 2 prompt 同步新增以下分析要求（均为描述性小标题，不用编号，issue #34 一并把互相引用改为内联复述）：能力圈内外标注（圈外驱动因素须显式标注“不构成操作依据”），持仓异动核对（唯一允许给出加减仓建议的依据来源，对照认知提升/减仓具体标准逐条核对，不满足则明确声明不构成依据），SAS候选证据标注（命中7.4内部信号/认知提升标准时输出 `sas_candidates` 字段，见 issue #31）。不自动计算 SAS 分数（仍为人工季度任务，见 issue #32）。

### 7.1b 持仓计算信号（user message，纯计算，零LLM/搜索成本，issue #33）

`_compute_holding_signals()` 将两项计算结果注入 Layer B，与持仓快照、投资框架并列：
- **52周区间百分位+距历史高点回撤**（`fetch_prices.py::fetch_52week_stats()`，yfinance period="1y"；issue #63 起 bulk 缺失时 `Ticker.history` 重试一次，并在拉取期间压低 yfinance ERROR 以免 healthcheck 误报），对应 Manual 7.4 节"股价相对位置"信号，代码算好不让 LLM 从文本自行估算
- **持仓占组合%**（`_get_portfolio_weights()`，市值÷组合总USD市值），对应 Manual 第6节的仓位结构性超载减仓情形（>15%），“持仓异动核对”那条分析要求直接读取这个计算值判断，不再让 LLM 自己从持仓快照文本估算百分比
- 适用范围仅限核心主动个股（排除 QQQM/VOO/EWJ/SGOL/BOXX/CASH），与 Manual 第1节三层结构对齐

**PR #89 起的注入方式**：Pass 2 不再每次注入全量数值。`pass2_context.current_state()` 把 FRED 档位、是否跨过 15%、52 周新高/新低记为 `context_state` 存入情报快照，`changed_background()` 与上次成功报告的快照比较，只把发生变化的项注入 Pass 2 的 Layer B（减少“15%/结构性超载”一类套话）。SAS 候选抽取仍拿全量持仓计算信号。

### 7.2 持仓快照（user message，每次追问刷新）

从 `Finance/portfolio_report_latest.md` 提取 IB 美股持仓（过滤 CASH 和 A 股编号）：
```
IB美股持仓（成本价为均价，浮盈%为报告日数据供参考，实时盈亏请结合yfinance现价计算）：
  AMKR  成本@48.02  报告浮盈+46.5%
  INTC  成本@32.62  报告浮盈+233.5%
  NVDA  成本@190.17  报告浮盈+18.5%
  ...（共10只）
```

**重要：提取 `均价`（持仓成本），不提取 `现价`。** portfolio_report 同时记录均价和现价，现价是报告生成日的市价（可能已数天前），绝对不能当成本注入 LLM。现价由 yfinance 实时提供。`_get_portfolio_snapshot()` 的 regex 捕获 `均价` 字段。

### 7.3 MemPalace 语义上下文（user message，fail-open）

查询与问题相关的历史记录，结果注入 user message。

### 7.4 预处理焦点提示

`relevant_tickers` 和 `framework_focus` 告诉 Claude 本次应重点关联哪些持仓和框架，避免全量扫描。

---

## 八、LLM 选型与成本结构

### 8.1 各环节选型

所有 LLM 调用统一走 **OpenRouter**（`https://openrouter.ai/api/v1/chat/completions`）。不再有任何 DeepSeek 直连。

**选型不再硬编码在各脚本里（issue #11，2026-07-25）**：下表全部来自 `scripts/llm_config.py` 的 stage 定义（当前 8 个：`am_calibration`/`report_pass2`/`sas_candidate_extract`/`macro_brief`/`tg_preprocess`/`tg_gap_detect`/`tg_research`/`tg_followup`；`report_pass1` 与 `semantic_filter` 已随 PR #89 删除），可由项目根目录 `llm_config.json`（**git 追踪，非 gitignore**——最初照搬 `tg_offset.json` 那类运行时状态文件的套路做成 gitignore，后来意识到这是人手改的、有意图的配置决策而非机器写的临时状态，跟 `watchlist.md` 是同一类东西，且不含任何敏感信息，没理由不入库；追踪进 git 不影响"改了立即生效不用走 PR"——那是 loader 每次读文件决定的，git 只是白得一份可追溯的修改历史）在运行时逐字段覆盖，无需改代码/走 PR。`llm_config.py` 内置 DEFAULTS 是唯一最终兜底：配置缺失/损坏/字段非法逐字段回退默认值并记 WARNING，不让流水线崩；新增跨字段校验——`max_tokens` 必须比 `thinking.budget_tokens` 多至少 500，否则两个字段一起回退（防止手改配置复现 issue #53 的预算耗尽）。每处生效覆盖记 INFO 日志（`LLM config override: <stage>.<field>: old -> new`）。仓库内 `llm_config.example.json` 是 schema 说明模板（有测试断言与 DEFAULTS 一致）。**语义过滤（#3）已于 2026-07-23（issue #53/PR #54）切换为非 DeepSeek 模型且不再 pin provider；`tg_gap_detect`（#6b）与 `tg_preprocess`（#5）已于 2026-07-25（issue #11）同样切换，原因是实测发现两处生产 bug（见表后新增注记与文末变更记录）。**

| #   | 调用位置 | 用途 | 主力模型 | Fallback | max_tokens | 成本估算 |
| --- | --- | --- | --- | --- | --- | --- |
| 1b  | `calibration.py::_evaluate_am_predictions()`（stage `am_calibration`） | PM slot：核验AM可验证信号 | `google/gemma-4-31b-it`（OR，provider锁定OpenInference，issue #59——从report_pass1拆分为独立stage） | `google/gemini-3.1-flash-lite` OR flex | 4000 | ~$0.0005 |
| 2   | `run_finance.py` Pass 2（stage `report_pass2`） | 整合情报快照、Sonar 与个人上下文生成最终报告，report_md 直接输出裸 markdown | `openai/gpt-6-luna` via OR/OpenAI（provider 锁定不允许 fallback 到其他 provider；`reasoning={"effort":"xhigh"}`，issue #90/PR #91；此前 issue #60 从 `deepseek-v4-pro`+thinking 换到 `gpt-5.6-luna`/high，原因见文末变更记录）。**截断正文判为失败**（PR #94）：`finish_reason=length` 时即使正文非空，也依次走同模型重试 → fallback → 代码摘要 + TG 告警 | `google/gemini-3.5-flash` OR flex | 32000（PR #94 由 16000 提高；HTTP 读超时 `max(180, max_tokens//50)`=640s） | ~$0.02（未核实精确单价） |
| 2b  | `run_finance.py` Pass 2后（stage `sas_candidate_extract`，issue #60） | 独立提取SAS候选证据（原是Pass 2 JSON的一个字段） | `google/gemma-4-31b-it`（OR，provider锁定OpenInference，9/9真实对抗测试验证） | `google/gemini-3.1-flash-lite` OR flex | 800 | ~$0.0003 |
| 4   | `intel_sources.py::_sonar_macro_brief()`（stage `macro_brief`） | Sonar 宏观快照（AM/PM 各一次） | `perplexity/sonar`（OR，`search_recency_filter="day"`，2026-07-02 加，见 issue #24） | 重试1次(5s) → `””` 空节 | 1500（2026-07-23 起，issue #55；此前 800 会在 finish_reason=length 时静默截断且无日志可查） | ~$0.005（含固定搜索费） |
| 5   | `telegram_commands.py` Step 1（stage `tg_preprocess`） | 统一预处理：意图分类 + 2条 query 生成 | `google/gemma-4-31b-it`（OR，provider锁定OpenInference，issue #11/#60——实测 temperature=0 下 deepseek-v4-flash 对同一条简单指令连续3次调用给出3种不同错误结果，见第8.1节注记） | `google/gemini-3.1-flash-lite` OR flex | 600 | ~$0.0001 |
| 6   | `telegram_commands.py` Step 3 | 追问原文情报（2条 query + P1 可选第3条 + 3 URL extract，P2 聚合 URL 优先） | Parallel.ai SDK `parallel-web==0.6.0` | Sonar（重试1次→Exa） | — | ~$0.007（无P1）/ ~$0.012（P1触发） |
| 6b  | `telegram_commands.py` Step 3 P1（stage `tg_gap_detect`） | gap detection：是否需要第3条 query | `google/gemma-4-31b-it`（OR，provider锁定OpenInference，issue #11/#60——原 deepseek-v4-flash 在 60-token 预算下实测把预算全烧在隐藏推理上，`content=None`，自己的 try/except 静默吞掉异常返回 None，跟"正确判断无需补搜"完全无法区分——这个功能自 2026-05-23 上线起大概率从未真正生效过） | fail-open（不触发即跳过） | 60 | ~$0.0001 |
| 7   | `telegram_commands.py` Step 4（stage `tg_followup`） | 个人化推理 | `openai/gpt-5.6-luna`（非pro）via OR/OpenAI（provider锁定，`reasoning={"effort":"high"}`，issue #60——原deepseek-v4-flash+thinking实测同样会无视budget_tokens软上限烧穿max_tokens） | `x-ai/grok-4.5`（OR，`reasoning={"effort":"medium"}`，max_tokens 8000） | 16000 | ~$0.01（未核实精确单价） |
| 8   | `sas_review.py`（季度手动/自动触发，issue #32） | 直接打 SAS 四维度分（Strategic Space/Execution/Expectation Gap/Alpha Potential） | `~anthropic/claude-sonnet-latest`（OR） | 无（v1 故意不接，观察实际效果后再评估） | 4000 | ~$0.05（OR `usage.cost` 实际读取，无硬编码价格表） |

**已删除的 stage（PR #89）**：#1 `report_pass1`（LLM Pass 1 草稿 + `tavily_queries`，最后为 `gemma-4-31b-it`）与 #3 `semantic_filter`（开放池语义排序，最后为 `gemma-4-31b-it`）随情报快照重构从主流程移除，`llm_config.py` DEFAULTS 与 `llm_config.json` 中已无这两个 stage。Pass 1 现为代码实现（`intel_deepen.py`，见 §5.1b）。表后关于 #3 的注记保留作选型记录。

**OR provider 实测结论（2026-06-02）：**
- **V4 Flash**：DigitalOcean（FP16，高精度）、Venice（US 机房，全精度，实测可用）、StreamLake（OR 默认路由，精度未知，已在 OR 账户排除）可用。NovitaAI 不再服务 V4 Flash。
- **V4 Pro**：2026-06-02 实测时 Together、Fireworks 可用且 DigitalOcean 不服务；**此结论已过时**——实际生产日志（2026-07-15至今）显示 Pass 2（v4-pro，thinking=enabled）绝大多数调用实际路由到 **DigitalOcean** 并成功返回内容，Together/Fireworks 从未在日志中出现过。偶尔 fallback 到 NextBit/Cloudflare/Alibaba（均非 pin 列表内，靠 `allow_fallbacks:True` 自然转移）。必须传 `thinking:enabled+budget_tokens` 的结论仍成立（否则 content=None），但“DigitalOcean 不服务 V4 Pro”这条已不再准确（2026-07-23 核实，未改代码仅修正本行描述）。
- **thinking 参数兼容性**：Flash 不得传 `thinking:disabled`（StreamLake fallback 时会导致 content=None）；Pro 必须传 `thinking:enabled`。
- **BYOK 对 provider pin 的实际影响（2026-07-23，issue #55）**：本账号在 OpenRouter 配置了 BYOK（Bring Your Own Key，DeepSeek 官方 API token）。issue #53 排查时观察到 `DS_OR_PROVIDERS` pin 到 `["DigitalOcean", "Venice"]` 仍有调用被路由到未 pin 的 `Alibaba`，当时归因为“OR provider pin 机制本身不可靠”——这个结论需要修正。更准确的解释是：BYOK 连接的 provider 容量/限流耗尽时，`allow_fallbacks: True` 会让请求自然降级到 pin 列表之外的 provider（如 Alibaba），这是预期行为，不是 pin 机制失效。下次遇到类似“路由到未 pin 的 provider”现象，应先查 BYOK 状态和该 provider 的限流情况，而不是怀疑 OR 路由逻辑本身。

**注：**
- #6 Parallel.ai SDK：DI 宿主机 `requirements.txt` 为 `parallel-web==0.6.0`（2026-09-24 核对；此前文档写的 0.4.2 是 Hermes 容器版本）；search 返回 WebSearchResult，extract 返回 ExtractResult；dedup → P2 聚合 URL 排序 → extract top 3；每篇正文截 4000 字
- #6 fallback 链：Parallel.ai 失败 → Sonar（重试1次5s）→ Exa model=”exa”
- #7 历经 Claude Sonnet → Gemini 3.5 Flash（格式过于机械否决）→ DeepSeek V4 Flash via OR/DigitalOcean → `openai/gpt-5.6-luna`/high（当前，issue #60）；决策依据：Hermes/DI 对比验证”数据质量 > 模型档次”原则
- #4 Sonar 失败：重试1次 → `””` 空节（宏观面由 7 RSS 源 + Guardian 进入情报快照的地缘话题覆盖，不走 Exa）。Sonar 输出用于 Pass 2 正文、PM 校准和 Context Log，不进 SAS 候选抽取
- OR flex 延迟实测约 12-15s，作为应急路径可接受；gemini 模型不接受 `thinking` 参数，fallback 调用自动去掉
- #3 2026-07-22 生产环境真实崩溃：`deepseek-v4-flash` 即使不发 `thinking`/`reasoning` key，仍隐式产生 reasoning token 吃满 `max_tokens=80`，`content` 返回 `null`，`.strip()` 直接抛异常；同时确认 `DS_OR_PROVIDERS` provider pin 并未被 OpenRouter 可靠遵守（该次事故 Pass 1 两次调用都被路由到未 pin 的 Alibaba）。切换到 `google/gemma-4-31b-it`（姊妹项目 `PC611-homepage` 的 LLM-eval 框架测出的唯一 100% 通过模型，用真实 prompt 模板验证 `reasoning_tokens=0`），不再 pin provider，真实 provider 信息改为动态串联进 TG 状态消息（此前硬编码 `"OR/DigitalOcean"` 掩盖了真实路由数月）。函数 `_haiku_relevance_filter()` 同时改名为 `_semantic_relevance_filter()`（原名从未用过 Haiku）。详见 issue #53/PR #54

### 8.2 重要认知

**Perplexity 隐藏搜索费**：所有 Perplexity 模型额外固定收 $0.005/次搜索调用，与 token 量无关。sonar 和 sonar-pro 每次实际成本几乎相同，选"便宜版"省不了多少。

**OR provider 差异**：通过 OpenRouter 调用的模型不附带宿主平台的工具层。Grok 通过 OR 没有 X 实时搜索，这些能力只在各自官方 API 中可用。

**DeepSeek R1 拒绝 2026 日期**：R1 训练截止约 2025 年中，会主动拒绝注入的 2026 日期，回退到训练数据。不适合实时数据合成场景。

**Amazon Bedrock uptime 约 71%**：原锁定 Azure provider，但 Azure 已于 2026-05 放弃 Sonnet 路由，不再适用。

**OR 波浪号前缀 `~model`**：`~anthropic/claude-sonnet-latest` 是 OR 维护的 always-latest alias，始终指向该系列当前最新版本。不带波浪号的 `anthropic/claude-sonnet-latest` 在 OR 是无效 ID（400 Bad Request）。需要固定版本用精确 ID，接受滚动更新用 `~` 前缀。

**Exa.ai 端点与计费**：调用地址 `https://api.exa.ai/chat/completions`（无 `/v1` 前缀，OpenAI 兼容格式）。`model="exa"` 走 Answer 通道（$5/1k = $0.005/次），与 Sonar 成本持平。`/search` 端点走 Search 通道（$7/1k）。两类配额在 dashboard 独立计量，不共享；免费额度含 $20 初始 credit，pay-as-you-go。新鲜度：索引延迟约 4-10h，查询时触发 livecrawl，实测 4h 内事件已可检索。`model="exa"` 搜索+合成一体，无需额外 LLM，作为 Step 3 Sonar fallback 成本中性。

**模型背对背比较（Grok 4.3 vs Claude claude-sonnet-4-6）**：同一输入，Grok 成本约 $0.005（Claude 的 1/4），但框架理解和 context 遵循弱于 Claude。现阶段保持 Claude，持续积累对比样本。

### 8.3 每次追问成本

```
                        原始方案        2026-05-21      2026-05-23（当前）
V4 Flash 预处理：       ~$0.0001        ~$0.0001        ~$0.0001
gap detection（P1）：   —              —               ~$0.0001（fail-open，按需触发）
情报检索：              Sonar $0.0050   Parallel $0.007 Parallel $0.007（无P1）/ $0.012（P1触发）
推理合成：              Claude $0.0190  V4 Flash $0.002 V4 Flash via OR/Novita $0.002
总计（无P1）：          ~$0.025/次      ~$0.009/次      ~$0.009/次（-64%）
总计（P1触发）：         —              —               ~$0.014/次
```

fallback 时成本：Sonar($0.005) + V4 Flash($0.002) ≈ $0.007；Exa($0.005) + Grok($0.005) ≈ $0.010。
注：中间方案 Gemini 3.5 Flash（$0.003）因输出质量不如 V4 Flash 被否决（过于关注格式正确性）。

**表中“当前”列已过时（2026-09-24 标注）**：预处理与 gap detection 自 issue #11/#60 起为 `google/gemma-4-31b-it`，推理合成自 issue #60 起为 `openai/gpt-5.6-luna`/high（fallback `x-ai/grok-4.5`），见 §8.1。换模型后的单次追问成本尚未用 OR 账单核实，上表数字不再代表现状。

---

## 九、调度与运维

### 9.1 launchd（macOS）

报告任务使用**两个独立 plist**（不可合并为一个）：

```
com.daily-intel.finance.am.plist   5:30 AM PT = 8:30 AM ET  开盘前简报
com.daily-intel.finance.pm.plist   5:10 PM PT = 20:10 ET    夜盘动向 + SAS季度复盘扫描
```

**PM plist 串联 sas_review.py（issue #32，2026-07-09）**：`ProgramArguments` 改为 `/bin/bash -c "run_finance.py; sas_review.py"`（分号分隔，前者失败不挡后者），复用同一运行时点，未新增独立 plist。`sas_review.py` 当前 `NOTIFY_ONLY=True`，自动扫描命中触发条件时只发邮件提醒（附手工执行命令），不自动跑分析/不自动花钱，观察几个真实财报季后可改 `False` 切换全自动。

**重要：`StartCalendarInterval` 数组陷阱**

macOS launchd 的 `StartCalendarInterval` 若写成数组（多个时间），只有第一个时间会被注册为 XPC activity，其余静默丢失。2026-05-29 确认此 bug：AM 触发后系统日志只显示一个 activity ID，PM 从未注册过。修复方案是拆成两个独立 plist，各自只含一个时间点。

```xml
<!-- com.daily-intel.finance.am.plist -->
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key><integer>5</integer>
    <key>Minute</key><integer>30</integer>
</dict>
```

**Telegram bot**：`com.daily-intel.finance.telegram.plist`，KeepAlive 常驻，RunAtLoad true（注：本节此处长期误记为 `com.hermes.finance.telegram`，2026-07-02 修正，实际 launchd label 以 `launchctl list | grep finance` 为准）。

### 9.2 日志安全（2026-07-02，issue #21）

`run_finance.py` 和 `telegram_commands.py` 都在 `logging.basicConfig()` 后加了 `logging.getLogger("httpx").setLevel(logging.WARNING)`。原因：httpx 库自带的请求日志会在 INFO 级输出完整请求 URL，而 Telegram Bot API 把 token 编码在 URL 路径里（`https://api.telegram.org/bot<TOKEN>/method`）、Finnhub/Guardian 把 key 放在查询参数（`?token=`/`?api-key=`）——不压低这个 logger 级别，每次 API 调用都会把明文凭据写进 `/tmp` 下世界可读（644）的日志文件。新增外部 API 调用时，默认检查 URL 是否带凭据，带则必须确保对应脚本已压低 `httpx`/`requests` logger 级别，不能依赖默认状态。

### 9.3 NYSE 交易日检查

```python
import exchange_calendars as xcals
nyse = xcals.get_calendar("XNYS")
is_trading = nyse.is_session(today_et_str)  # 非交易日静默退出
```

### 9.4 常用命令

```bash
tail -f /tmp/daily_intelligence.log   # 报告任务日志
tail -f /tmp/finance_telegram.log     # Telegram bot 日志
cat finance_tavily_budget.json        # 今日 Tavily 用量
cat finance_serpapi_budget.json       # 本月 SerpApi 用量

# 手动重跑（补跑历史报告）
FINANCE_FORCE_DATE=2026-05-01 FINANCE_FORCE_SLOT=pm \
HERMES_DATA=~/.hermes OBSIDIAN_PATH="~/..." \
.venv/bin/python scripts/run_finance.py

# Telegram bot 重启（env var 变更后）
launchctl stop com.daily-intel.finance.telegram && launchctl start com.daily-intel.finance.telegram

# IBKR gateway 状态诊断
pgrep -la GatewayStart                          # 确认进程运行
lsof -i :5001                                   # 确认端口监听
curl -sk https://localhost:5001/v1/api/iserver/auth/status | python3 -m json.tool  # 认证状态
tail -f /tmp/ibkr_gateway.log                   # gateway 主日志
tail -f /tmp/ibkr_keepalive.log                 # keepalive（每 5 分钟 auth check）

# IBKR session 恢复（iOS App 踢出 / ~30 天过期 / 进程崩溃）
# 先在 iOS App 退出登录（场景 A），然后：
~/Daily_Intelligence/ibkr/login.sh
# 脚本自动：清理僵尸进程 → 启动干净实例 → 打开浏览器 → 轮询认证状态 → 报告结果
# 最终验证标志：authenticated=true, connected=true, competing=false
```

---

## 十、目录结构参考

```
~/Daily_Intelligence/
├── scripts/
│   ├── run_finance.py              主入口，报告生成流程
│   ├── fetch_prices.py             yfinance 价格拉取
│   ├── fetch_news.py               RSS + Guardian 聚合
│   ├── intel_pass0.py              Pass 0 情报快照入口 build_intel_snapshot()；--replay 回放（issue #87 PR #88/#89）
│   ├── intel_collect.py            按标的收集 Finnhub/Google News/RSS/Guardian、别名匹配、ETFS 常量、archive_intel_snapshot()
│   ├── intel_deepen.py             代码 Pass 1：直链/302 解析、按需搜索、Extract 补齐（PR #96）
│   ├── intel_render.py             情报快照 → Pass 2 输入段落（条数上限）与代码摘要降级
│   ├── pass2_context.py            背景信号 context_state 与变化判断（FRED 档位/15% 仓位/52 周高低）
│   ├── recent_coverage.py          近 5 个 NYSE 交易日已报道内容抽取
│   ├── publication_window.py       报告与回放共用的交易日发表窗口计算
│   ├── eval/                       issue #87 回溯评估集与收集召回验收入口
│   ├── intel_sources.py            Sonar 宏观 / Polymarket / Adanos / Apify Reddit / FRED + 字段消毒（issue #17/#26/#38）；fetch_brave_news()、fetch_finnhub_news() 仍在但主报告不再调用
│   ├── scoring_utils.py            关键词锚定与信源置信度打标（_source_confidence_tags 被 intel_deepen 复用）
│   ├── report_writers.py           报告/状态消息格式化辅助函数（_fmt_llm_meta 等，从 run_finance.py 拆分）
│   ├── calibration.py              AM 预判校准闭环（evaluate_am_calibration 等，issue #10，从 run_finance.py 拆分）
│   ├── llm_client.py               call_llm() 统一 LLM 调用层（重试 + OR flex fallback + parse_llm_json，从 run_finance.py 拆分）
│   ├── llm_config.py               每-stage LLM 选型 + 运行时覆盖加载器（issue #11，可由 llm_config.json 运行时覆盖）
│   ├── budget_trackers.py          各个预算 tracker 对外共享接口（内部委托 quota_store.py，issue #41）
│   ├── quota_store.py              Tavily/SerpApi/Adanos/Apify/Brave 五个 tracker 共享的 load/save/remaining 原子操作（issue #41，叶子模块）
│   ├── memory_context_finance.py   KB 上下文注入
│   ├── telegram_commands.py        TG bot + 追问流水线
│   ├── telegram_utils.py           call_telegram() 共享容错层（run_finance.py 与 telegram_commands.py 共用，issue #20-23）
│   ├── finance_email.py            Resend 邮件客户端
│   ├── sas_review.py               季度 SAS 深度复盘（issue #32，2026-07-09），已接入 PM launchd 串联运行（第九节9.1），详见第十二节
│   ├── sec_edgar_utils.py          封装 edgartools：Form 4 内部人买入过滤 + 10-K risk factors 取值
│   ├── backfill_drawers.py         MemPalace 历史 drawer 一次性回填（已完成，保留供参考）
│   ├── migrate_reports.py          旧格式迁移（一次性）
│   └── test_*.py                   无 pytest 依赖的回归测试（2026-09-24 共 13 个；含 test_issue87_pass0/pass2/switch、test_llm_config、test_intel_sources_sanitize、test_telegram_followup_reason 等；#72/#80/#82 与语义过滤测试随 PR #89 删除）
├── .venv/
├── llm_config.example.json         LLM 选型 schema 与默认值说明模板（git 追踪，issue #11）
├── llm_config.json                 实际生效的 LLM 选型覆盖（git 追踪，目前内容与 DEFAULTS 完全一致——没有覆盖，可直接编辑，不需 PR）
├── sas_tracked_tickers.json        SAS 永久追踪标的列表（原子写入，人工才能移除）
├── sas_review.lock                 sas_review.py 并发锁（运行时产生，非代码仓库内容）
├── finance_tavily_budget.json      Tavily 每日计数
├── finance_serpapi_budget.json     SerpApi 月度计数（首次使用时自动创建）
├── finance_brave_budget.json       Brave News 月度计数（issue #14）
├── finance_adanos_budget.json      Adanos X 舆情月度计数（issue #17）
├── finance_apify_budget.json       Apify Reddit 舆情月度计数（issue #17）
├── tg_offset.json                  TG getUpdates offset
├── entity_alias_cache.json         Finnhub profile2 补全的实体别名缓存（gitignore）
├── archives/                       情报快照与历史 Extract 存档（Obsidian 之外，不被 mine）
│   └── YYYYMM/
│       ├── YYYY-MM-DD-{slot}-intel-snapshot.json   PR #89 起每次运行原子写入
│       └── YYYY-MM-DD-{slot}-extract.md            PR #89 起停止新写，历史保留
└── backups/                        本地备份镜像（gitignore，不进代码仓库）
    └── 预判校准记录_backup.md      与 Obsidian 预判校准记录.md 同步写入，防 Obsidian 侧丢失

注：`run_finance.py` 自 2026-07 起逐步拆分出上述叶子模块（budget_trackers/quota_store/intel_sources/scoring_utils/report_writers/calibration/llm_client/telegram_utils），自身仅保留主流程编排与调用胶结，具体拆分过程见 Daily Intelligence 开发部署日志各对应 issue。

Obsidian Vault/
├── Hermes/Daily Intelligence/
│   ├── watchlist.md                唯一配置入口
│   ├── Daily_Intel设计文档.md      本文档
│   ├── Daily Reports/
│   │   ├── Daily_Intel_report_YYYYMM.md   月度报告+追问（被 mine）
│   │   └── Daily_Intel_context_YYYYMM.md  月度 Context Log（被 mine）
│   └── 预判校准记录.md                    AM 预判校准知识日志（追加式，issue #10）
└── Finance/
    ├── 金融资产信息.md              投资框架（用户维护，TG追问流水线仍在用）
    ├── Investment Operating Manual v1.0.md  能力边界/SAS/Portfolio Construction完整决策框架（AM/PM日报+sas_review.py共用，issue #30，2026-07-08起）
    ├── portfolio_report_latest.md  持仓快照（agent 更新）
    └── SAS_Review/
        └── {TICKER}.md             每 ticker 一份，季度 SAS 深度复盘历史记录（append，issue #32，2026-07-09起）
```

---

## 十一、扩展方向

**已完成（2026-05-04）**
- 时区修复：三个文件统一改 `ZoneInfo("America/New_York")`，冬令时自动处理
- Telegram HTML 注入修复：`reply()` 自动 `html.escape()`，`reply_html()` 发预格式化 HTML；`_md_to_tg_html()` 正文先 escape 再替换 Markdown 标记；`_build_status()` 动态值转义
- 搜索优先级修复：代码现与设计文档一致（anomaly 先行，LLM query 为 fallback）
- KG CLI 回填修复：`run_for_date()` 月度文件 fallback 改为正则定位 `## YYYY-MM-DD` section，绕过对文件名的日期提取；内部子节 `## 【小节】` 不再误截断

**已完成（2026-05-04）**（续）
- MemPalace mine 接入（基础层）：`Hermes/mempalace.yaml` 补建（room=hermes），crontab 由并发改为错开时间（防 ChromaDB SIGSEGV），Hermes 目录 140 个 drawer 已入 hermes room，memory_context_finance.py 异动 ticker 查询扩展为 hermes+finance 双 room 搜索

**已完成（2026-05-05）**
- Pass 2 模型升级为 deepseek-v4-pro（Pass 1 保持 flash）
- Skip 判断移至代码层：依赖 anomalies + triggered_geo_topics，LLM 调用前完成，零成本
- Pass 1 输出结构化 tavily_queries 数组（含 search_depth / days / max_results per query）
- 动态 query_days：`max(1, min(3, 距上次报告天数))`，节假日后自动扩展搜索窗口
- triggered_geo_topics 注入 prompt，LLM 只为 RSS 命中的主题生成查询
- AM 异动查询用 advanced（2 credits），PM slot 强制 basic；budget 按 credits 预检
- max_results 从 8 提升到 12

**已完成（2026-05-08）**
- 所有 LLM 提示词注入当前时间（`strftime("%Y-%m-%d %H:%M %Z")`，动态 EDT/EST）并要求以 NYSE 时区推理；主报告 SYSTEM_PROMPT、USER_PROMPT_TEMPLATE 及两个 `.format()` 调用均已更新；telegram_commands.py 三处 `now_str` 格式统一为 `%Z`
- TG 追问输出格式从3节扩展为5节，新增 KG/历史关联、待验证点、矛盾缺口
- 追问流水线内联 KG 写回：Claude 在答案末尾输出 `---KG---` + JSON 三元组，`_write_followup_triples()` 解析后 POST 到 bridge，fail-open；此前追问产生的事实从未进 KG，现已补全

**已完成（2026-05-11）**
- **yfinance 实时行情注入**：追问流水线在 Sonar 之前调 `yfinance.download(prepost=True, interval='1m')` 获取含盘前/盘后的最新 tick，格式化为带时间戳和来源标注的"价格基准"，注入 Claude user message。Sonar 文章价格仅参考，矛盾时以 yfinance 为准。
- **yfinance.news 注入**：`_fetch_yfinance_news()` 拉取过去48h内的相关新闻（`content.pubDate + title + provider`），注入 Claude user message，填补 Sonar 无法及时索引的盘前小时内新闻。
- **Sonar 价格上下文**：`_sonar_research(price_context=...)` 将 yfinance 价格事实注入 Sonar system prompt，引导 Sonar 聚焦今日催化剂；明确要求"找不到就说找不到"。
- **Claude 时间线推理约束**：推理规则要求明确区分历史事件与今日动态；无新催化剂时直接声明"动量延续"而非从历史文章推断。
- **Finnhub 全面接入**：`FINNHUB_API_KEY` 加入 `.env`；`_finnhub_quote()` + `_finnhub_news()` 作为 yfinance 失败时的 fallback（价格限常规时段；商品/FX 跳过并记录）；`fetch_prices.py` 有同样两级保护；`run_finance.py` step 6b 常态注入 Finnhub 公司新闻到定时报告 prompt。
- **KG 泄漏修复**：`---KG---` 分隔改用 `re.search(r"\n?---KG---\s*\n?")` 做 regex 匹配，不再依赖 LLM 输出精确换行。
- **进度提示去 emoji**：TG bot 立即回执从 `⏳` 改为"收到，处理中..."；各步骤提示改为文字描述（无图标）。
- **AM/PM 价格数据 slot 感知**：`fetch_prices(slot)` AM 使用 `yf.download(period="2d", interval="1m", prepost=True)` 盘前最新成交价 vs 昨收判断异动；PM 使用今日收盘 + 盘后最新成交价两路综合判断。`PriceRow` 增加 `afterhours_price/pct/slot` 字段；`format_price_table(slot)` 按时段输出不同列。Finnhub fallback 无盘前/盘后，记 warning 后返回日线等价数据。

**已完成（2026-05-13）**
- Tavily 情报拉取升级为四层架构（Search+ScriptFilter+LLM语义过滤+Extract）；Layer 1 全部 basic，Layer 2b LLM 语义过滤，Layer 3 Extract 获取全文 chunk
- RSS 扩展至 11 个源：新增 CNBC、MarketWatch、Foreign Policy、Al Jazeera、Seeking Alpha
- Sonar 宏观快照接入（step 6c，AM+PM）：query 从 watchlist 动态构建，个人化 system prompt
- fetch_prices.py slot 感知完善：AM/PM 输出不同列，PM 收盘价强制日线官方値，SerpApi fallback 接入

**已完成（2026-05-17）**
- DeepSeek 直连迁移：Pass 1/2 和 TG Step 1 从 OpenRouter 迁移到 `api.deepseek.com`，V4 Flash thinking 模式统一加 `disabled`，Pass 2 开 thinking budget_tokens=3000
- KG 提取模型升级：从 gpt-oss-20b 迁移到 claude-haiku-4-5 via OR（主力）+ deepseek-v4-flash 直连（fallback）；谓词词汇表约束统一 had_move_pct
- KG 价格快照直写：`_write_price_snapshot()` 从 price_rows 直写全量 ticker price_level，无 LLM，每次约 +16 条

**已完成（2026-05-18）**
- KG 三元组全面接入报告与追问：`memory_context_finance.py` 完整重写，谓词三层分类（框架/事件/跳过），全持仓差异化注入，字符预算 6000（KG 3200/MP 1200/Obs 800 独立截断）
- KG monitor_item 主动发现：反向查找（新闻→KG），第三 skip 豆免条件，kg_monitor_section 注入 Pass 1/2
- TG 追问 KG 注入：`_kg_query_bridge()` + `_filter_framework_triples()` 对 relevant_tickers 查 KG，注入 Claude user message
- KG 写回保护：`_safe_write_triple()` 框架类谓词硬拦截，事件类 7 天去重，防自我激赡回路
- 语义过滤器和 KG 提取器切回 DeepSeek V4 Flash 直连（原 Haiku/Bedrock，~$0.0079/次 → ~$0.00036/次，降低 22 倍）

**已完成（2026-05-19）**
- 新闻源扩充：RSS 11→14 个（Reuters/AP/WSJ 通过 Google News RSS `site:` 过滤，实测各 30 条，总量 245→294）
- Guardian Open Platform API 接入：`fetch_guardian_news()` 函数，fail-open，`GUARDIAN_API_KEY` 控制，`run_finance.py` RSS 后合并
- 直连受阻（Reuters/AP/WSJ/Guardian）已全部通过 Google News RSS 或官方 API 间接覆盖

**已完成（2026-05-20）**
- DeepSeek 直连 OR flex fallback：所有 DeepSeek 直连调用点（Pass 1/2、语义过滤、TG Step 1、KG 提取）在耗尽重试后自动 fallback 到 OR flex 模式。v4-flash → `google/gemini-3.1-flash-lite`；v4-pro → `google/gemini-3.5-flash`。两个模型经集成测试验证（mock DeepSeek SSL 失败→OR flex 触发→正确解析 JSON→返回有效结构）
- KG 提取 fallback 从 Haiku 改为 gemini-3.1-flash-lite flex，`_call_api()` 新增 `flex` 参数
- Finnhub fetch 加 1 次 timeout 重试（3s 后重试，仍 fail-open，无 fallback 模型）
- 修复触发场景：5:30 AM ET DeepSeek SSL 全程不可达约 15 分钟，AM 报告失败后手动补跑

**已完成（2026-05-28 晚）**
- **MemPalace per-day drawer 写入**：`mempalace_bridge.py` 新增 `POST /mempalace/add_drawer` 端点；`run_finance.py` 新增 `_mempalace_add_daily_drawer()`，在 `write_report()` 后自动写入；`backfill_drawers.py` 一次性回填历史 44 sections。检索粒度从月度文件级降至每报告级。

**已完成（2026-05-28）**
- **Pass 2 深度推理重构**：去掉 4 节硬格式，改为「要求+围栏」自由展开。Pass 2 独立 `USER_PROMPT_TEMPLATE_P2`，JSON 输出只含 `report_md`（不再要求 `tavily_queries`）。Pass 1 路径零改动。
- **上下文分层（Layer A / Layer B）**：`call_llm()` 新增 `system_prompt` 参数。Pass 2 使用 `SYSTEM_PROMPT_P2`（Layer A，从 Obsidian `Layer_A_Prompt.md` 动态读取）+ `_load_personal_context()`（Layer B，持仓均价 + 投资框架）注入 user message。Layer A 文件可在 Obsidian 直接编辑，无需改代码。Portfonia 接入时只替换 Layer B 函数。
- **`Layer_A_Prompt.md` 创建**：`Hermes/Daily Intelligence/Layer_A_Prompt.md`，从 `金融资产信息.md` 提炼，涵盖：投资风格与核心原则、资产结构逻辑（指数/黄金/债券）、回撤三级框架、滞涨情境判断、分析原则、输出标准。剔除个人持仓数字和时间节点。

**已完成（2026-05-30）**
- **情报输入层持久化**：`run_finance.py` 新增两条存档路径，解决每次运行后价格表/新闻/Tavily全文完全丢失的问题。
  - **Context Log**（step 11b，`write_context_log()`）：价格快照 + 命中地缘/异动的 RSS 条目（非全量300条）+ Sonar 宏观原文 + 搜索任务列表，append 到 Obsidian `Daily_Intel_context_YYYYMM.md`，被 MemPalace mine，支持语义检索"某日市场context"。
  - **Extract Archive**（step 9b，`write_extract_archive()`）：Tavily Extract 清洗全文（< 60字短行剥离）+ Layer 2b 候选列表，写入 `~/Daily_Intelligence/archives/YYYYMM/YYYY-MM-DD-{slot}-extract.md`，Obsidian 之外，永不被 mine，用于原始情报审计和中期回顾。
  - 设计依据：Extract 原始网页含导航/广告碎片，不适合矿化；report_md 是 Extract 的精炼产物，已有；Context Log 填补"驱动报告的原始触发信号"的检索空白。两者均 fail-open。

**已完成（2026-05-26）**
- **IBKR Client Portal Gateway 接入**：隔夜/周末 ATS/OTC 实时行情（唯一能覆盖此时段的免费方案）。`ibkr/quotes.py` 实现 `get_quote()`/`get_quotes()`，session-aware，retry 装饰器区分维护窗口与真实 auth 失败。`keepalive.py` 每60s /tickle 防 idle 超时。launchd 管理开机自启。
- **三层数据源路由**：`_fetch_realtime_prices()` 按时段选主力——交易时段 yfinance → 隔夜/周末 IBKR。非实时 fallback 均标注"非实时"（包括 Finnhub）。代码有 `# FUTURE:` 标注，待 IBKR 稳定后一键翻转优先级。
- **IBKR 授权状态监控**：`_ibkr_auth_note()` 注入报告 footer，session 失效时明确报警并附 3 分钟操作步骤（~30 天需人工浏览器重登）。
- **追问流水线 bug 修复**：`_unified_preprocess` max_tokens 350→600（followup JSON 截断）；KG 分隔符 `---KG---`→`===KG===`（LLM 误解为 markdown 分隔符）；`_fetch_realtime_prices` session-aware 重写。
- **KG 词表清理**：删除 `stock_price`/`price_change_pct`/`stock_price_change`，别名合并至 `price_level`/`had_move_pct`；`_PRICE_PREDICATES_BLOCKED` 双层硬拦截（kg_extractor + prompt 禁止列表）。

**已完成（2026-06-02）**
- **OR provider 实测 + DeepSeek 调用层全面修复**：NovitaAI 已不再服务 V4 Flash/Pro。三个文件的 `DS_OR_PROVIDERS` 全部改为 `["DigitalOcean"]`。V4 Flash 调用全部移除 `thinking:disabled`（StreamLake fallback 时该参数会导致 content=None）；V4 Pro 保留 `thinking:enabled,budget=3000`（Together/Fireworks 必需）。共 6 处修改。
- **KG 生成端改造**（来自 MemPalace_KGTriples 改造计划）：`kg_extractor_finance.py` 和 `telegram_commands.py` 新增 `normalize_entity()`（写入前归一化实体 alias）、`persist_pending_vocab()`（new_entities/new_predicates 写入 pending_review.json）。`_safe_write_triple()` 新增实体/谓词长度硬拦截（>30 / >20 字符）。`_write_followup_triples()` 补全保护：工个原来缺少 framework/price predicate 拦截（bug），现已补入六层过滤。`KG_EXTRACT_OR_PROVIDERS` 改为 DigitalOcean-first（结构化提取需要高精度）。

**已完成（2026-06-12）**
- **KG triples 系统全面下线**：Layer 3 整体移除，回退为两层知识体系（Obsidian + MemPalace）。详见文末变更记录。
- **Footer 精简 + TG 独立运行状态消息**：`finance_footer()` 移除“与中国企业情报完全隔离”声明和“Tavily今日剩余”计数，footer 简化为仅含 `_Daily_Intel · {date} ET_` + IBKR 状态行。`_ibkr_auth_note()` 的 gateway 不可达分支（`except Exception`）改为返回空字符串——IBKR 暂时停用，报告不再提示“gateway 未运行”；“需要重新授权”分支（gateway 可达但未认证）保持不变。新增 `build_status_message()` + main() step 13b，将 Tavily/SerpApi 本次用量、情报源状态、LLM/Provider 清单作为独立 TG 消息发送，邮件和 Obsidian 正文不受影响。

**已完成（2026-06-30）**
- **Tavily Extract 信源置信度打标（issue #19）**：Reuters 视频聚合页孤立 caption（无时间戳）曾被 Pass 2 当确定事实写入报告（“签署仪式定于周五”，用户核实其他信源查无此消息）。新增 Layer 3.5：`_detect_low_structure()`（识别视频/聚合页 caption 堆叠）+ `_lookup_published_date()`（从 extract 前的 search 结果池反查发布时间，Tavily /extract 本身不带日期字段）+ `_compute_corroboration()`（规则式事实指纹交叉域名匹配，零 API/LLM 成本），汇总为 `_source_confidence_tags()`。同时接入 `format_extract_results()`（嗂给 Pass 2 LLM）和 `write_extract_archive()`（本地审计存档）。`USER_PROMPT_TEMPLATE_P2` 新增第④条硬性要求：单一信源/无时间戳/视频聚合页的具体断言必须用“未证实/待核实”降级表述，不得以确定语气呈现。方向 4（wire 原文优先/视频路径降权）并入 issue #14（多搜索服务商矩阵+相关度分类）范围一并评估，方向 5（输出侧二次核验）暂缓。

**已完成（2026-07-02）**
- **getUpdates 轮询简化为无状态（issue #25）**：用户复查 issue #22/#23 的同步重试方案后指出"太重"——轮询循环本身每 ~30 秒自然重跑，循环节奏就是现成的重试机制，不需要单次调用内再套同步重试。改为无状态单次调用：失败静默跳过，持续失败 ≥ 30 分钟才升级 WARNING，恢复时记录真实停机时长。比原方案更简单，不是更复杂。
- **AM 预判校准闭环（issue #10）**：详见第 5.5 节。
- **市场见顶预警框架 + FRED 流动性快照（issue #26）**：评估用户分享的市场见顶指标框架后，认可两根支柱（流动性管道+产业资本开支二阶导），其余降级或排除，整理为活文档 `市场见顶预警指标.md`（【正常/观察/警戒】三档+分资产操作指引，定位“参考，非清仓触发”）。流动性三项（准备金/SOFR-RRP利差/TGA）接入 FRED 免费 API 自动化，详见第 5.1 节。

**已完成（2026-07-01/02）**
- **Telegram Bot 容错全面统一（issue #20/#21/#22/#23）**：用户定时巡检报告 `telegram_commands.py` 5 天内产生 3.4 万条 `read operation timed out` + 207 条 SSL EOF + 5 次 409 Conflict，均为 WARNING/INFO，"带病运行"。排查定位到四个独立问题并依次修复：① `_tg()` 客户端 timeout（10s）短于 Telegram 长轮询服务端等待时长（30s），几乎每次空轮询都自己打断自己，连带引发 409（issue #20）；② 这个修复部署后巡检又报警，排查确认不是回归——本机 Shadowrocket TUN 隐道对 `api.telegram.org`（域名特定，对照测试确认 Slack/OpenAI 同条件下无此问题）有约 25-30% 新建 TLS 连接瞬时失败率，之前被 timeout 刷屏噪声淮没，修复后成为唯一剩余错误类型而变得显眼（issue #22）；③ 发送调用（`send_telegram_report`/`send_telegram_alert`）与轮询调用原本分属两套独立实现，重试补丁只打在高频路径，用户指出这是同一个坑只是运气好没暴露，要求容错覆盖全部调用路径且日志级别反映"是否需要人关注"（issue #23）。新建 `scripts/telegram_utils.py::call_telegram()` 为共享底层函数，两个脚本统一调用，全仓库 grep `api.telegram.org` 确认无遗漏裸调用点。这条"容错要覆盖依赖的全部调用路径，日志级别反映是否需要关注"的原则已存为跨项目 memory。
- **日志凭据泩露修复（issue #21，排查 #20 时意外发现）**：httpx 自带的 INFO 级请求日志把完整 URL（含 Telegram bot token 在路径里、Finnhub/Guardian key 在查询参数里）明文写进了 644 权限的 `/tmp` 日志文件。两个脚本都加了 `logging.getLogger("httpx").setLevel(logging.WARNING)`。
- **Sonar 宏观快照防过时/防幻觉（issue #24）**：详见第五节 Sonar 部分。

**待规划**
- **候选统一打标层（issue #14，并入方向 4）**：`score_and_filter()` 之后、`_semantic_relevance_filter()` 之前加统一打标步骤，产出两个 tier：相关度（direct_company_news/sector_related_news/macro_market_news，来自 #14）+ 信源形态（wire_article/video_hub/aggregator_listing，来自 #19 方向 4）。信源形态判断前移到候选阶段（URL 路径正则前筛 `/video/`/`/watch/` 等），而非现有 `_detect_low_structure()` 那样等 extract 抹完全文才事后判断——能在花 Tavily extract credit 之前就把视频/聚合页候选降权。同域名同事件时优先保留文章版，视频版降权/丢弃。用户决定先观察一段时间再评估优先级。
- MemPalace 细切片：月度报告目前整文件级 drawer，跨日报 embedding 较粗；可在 run_finance.py 写报告后直接 mempalace_add_drawer 做 per-day 切片，不增加 Obsidian 文件
- TG 追问后置 Extract：Sonar 返回引用 URL 后对前 1-2 个 URL 调 `tavily_extract()`，补全文作为 Claude 辅证证据，仅当异动标的且 budget ≥ 2cr 时触发

**可选扩展**
- 盘后对比版本：收盘后运行，对比开盘前预判与实际走势
- 推理层模型优化：Grok 4.3 积累更多对比样本后评估是否替换 Claude（$0.025 → $0.011/次）
- 多用户支持：watchlist.md 扩展为多用户配置

---

## 十二、季度 SAS 深度复盘系统（sas_review.py，issue #30-33）

### 12.1 定位与与每日流水线的分工

Daily Intelligence 原本只有一条频次流水线：AM/PM 日报，面向“今天发生了什么”。Investment Operating Manual 第7节定义的 Strategic Alpha Score（SAS）是一套完全不同节奏的判断体系——它追踪的是战略演化（本邽同竞争、管理层养现、内部人意图），而非股价波动，“建议每半年更新一次”（Manual 7.1）。将这类分析塑进日频流水线会两头不讨好：每日跑浪费钱，且 LLM 会被迫从碎片新闻中强行提炼“战略演化”结论。sas_review.py 是独立的第二条流水线，与 AM/PM 完全分开运行，只在财报后真正需要重新打分的节点触发。

三个 issue 分工：
- **issue #30**：AM/PM 日报 `_load_framework()` 改从 Manual 提取能力边界/Portfolio Construction/Expectation Gap 信号清单（见第七节7.1），不自动打分，只是把决策框架注入日报 prompt
- **issue #31**：日报 Pass 2 新增 “SAS候选证据标注”要求 + `sas_candidates` 字段，命中 Manual 7.4 内部信号清单时自动 append 写入 `SAS候选证据日志.md`，作为季度复盘的证据队列，本身不打分
- **issue #32**：`sas_review.py` 本身——财报触发判定 + 数据源拓展（edgartools）+ 直接 LLM 打分 + 持久化记录
- **issue #33**：日报层补齐认知提升信号缺口（5.1/7.1b 节），为 #31/#32 提供持仓权重计算函数复用

### 12.2 持久化追踪列表

`sas_tracked_tickers.json`（项目根目录，非 Obsidian）原子写入（临时文件+`os.replace`）。`_update_tracked_tickers()` 每次运行时把 `_get_core_holding_tickers()`（排除 QQQM/VOO/EWJ/SGOL/BOXX/CASH 的主动个股层）中权重 `_get_portfolio_weights() > 2.0%` 的标的加入追踪集合，**只增不减**——清仓/回撤不会自动移除，因为历史 SAS 判断对未来重新建仓仍有参考价值。人工 `--exclude TICKER` 才能永久移除（历史 `Finance/SAS_Review/{TICKER}.md` 文件不删，仅停止未来自动触发；若未来重新建仓超 2% 权重会被自动重新加回）。

### 12.3 财报触发判定

`_is_triggered_today(ticker)`：从 Finnhub `/calendar/earnings` 取最近一次已发生的财报事件，结合 `exchange_calendars` 计算“反应首日后第3个交易日”：AMC（盘后发布）推迟到下一个 session 开始计数，BMO/DMH 当日即计。命中则自动触发（`main()` 无参数运行时遍历所有 tracked 标的）。

**`_fetch_earnings_anchor()` 是整个流水线唯一的 fail-closed 步骤**（issue #32 设计点 5）：真实 EPS/营收 surprise 数据是防幻觉锚点，同日重试 3 次（指数退避 5s/10s/20s），耗尽则 `rf.send_telegram_alert()` 显式报警并附手动重跑命令，**不静默失败**——没有真实财报数据就不进行任何分析。

### 12.4 数据源（`sec_edgar_utils.py`，2026-07-09 验证结果）

实测两个原计划数据源均不可用，从 v1 范围移除：Finnhub 机构持仓（13F）免费 key 返回权限错误（付费 tier 功能）；yfinance 期权链 `impliedVolatility` 数据损坏（bid/ask 均 0，IV 呈规律翻倍的占位符模式，非真实定价）。

取而代之的是 `edgartools`（已入 `.venv`，`requirements.txt` 已更新）：
- **`get_insider_buys(ticker, lookback_days=120)`**：Form 4 内部人交易明细，仅取 transaction code=`'P'`（开放市场/私人买入）且 `security_type="non-derivative"`，硬性排除 M（期权行使）/F（纳税扣扣）/A（RSU结予）/G（赠与）/S（卖出）等 routine 事件，只保留真正自主性买入
- **`get_risk_factor_diff_input(ticker, max_chars=6000)`**：最近两期 10-K 的 `risk_factors` 正文，不自建 diff 算法，原文并列交给 LLM 做语义层面的措辞变化判断（措辞变化本质上是语义问题，不适合程序化 diff）
- 两个函数均 fail-open，仅需 `edgar.set_identity()` 声明联系方式（SEC 礼貌性要求，非 API key），无需认证
- **13F 按标的明确不实现**：SEC 13F 按机构申报（每家机构报全部持仓），反向聚合“谁持有标的X”需跨机构聚合，是建索引工程而非季度脚本任务，明确列为 Non-goal

### 12.5 打分与输出

`SAS_REVIEW_MODEL = "~anthropic/claude-sonnet-latest"`（OR，**v1 故意不接 fallback**——观察实际效果后再评估）直接对 Strategic Space / Execution / Expectation Gap / Alpha Potential 四维度打分，每项 0-10 分 + 100-300 字依据，prompt 硬性要求不得以股价作为打分理由。注入上下文：`_load_sas_rubric()`（Manual 第7节完整方法论原文，正则定位“7. Strategic Alpha Score”→“8. 如何阅读 SAS”之间的区域）+ 财报锚点 + `_compute_holding_signals()`（仅供引用，不作为打分理由）+ 内部人买入 + 10-K 语言变化 + SAS 候选证据日志（issue #31）+ Finnhub 新闻 + Tavily 基础搜索（共享 AM/PM 同一日预算池）+ 持仓框架背景。成本从 OpenRouter 响应 `usage.cost` 字段直接读取（2026-07-09 验证字段存在，无需硬编码价格表）。

输出写入 `Finance/SAS_Review/{TICKER}.md`（每 ticker 一份，`## {日期}` 分节 append，采用读全文+临时文件+`os.replace` 的重量原子写入模式——比 `run_finance.py` 现有的 `open(path,'a')` 更重，因为这份数据有多季度比较价值，值得额外严谨性），并发邮件。

### 12.6 运维：NOTIFY_ONLY、防重、并发锁

- **`NOTIFY_ONLY = True`**（当前默认）：自动扫描（无 `--ticker` 的定时运行）命中触发条件时只发邮件提醒（附手工执行命令），不自动跑分析不自动花钱。原因：财报触发逻辑尚未经生产验证，且每次真实运行花钱（~$0.05）并写入永久、难以撤销的历史记录。`--ticker`（手动）不受此开关影响，总是真实运行。观察几个真实财报季后可改 `False` 转全自动。
- **`_has_today_entry()` 防重守卫**：自动触发前先检查 `Finance/SAS_Review/{TICKER}.md` 今天日期分节是否已存在，避免调度重复触发/进程重启后重复提醒或重复收费。
- **`_acquire_lock()` 并发锁**（`sas_review.lock`，`fcntl.flock`，复用 `run_finance.py` 同模式）：同一时刻只允许一个实例运行，避免手动补跑与定时任务重叠。
- **每日扫描总是发 TG 摘要**（`_send_daily_scan_summary()`，issue #35，2026-07-09）：自动扫描（无 `--ticker`）无论是否有 ticker 命中，都会发一条 Telegram 文本摘要，列出当天每个追踪 ticker 的命中/未命中状态及原因（上次财报日期+触发窗口还没到/已过，或查不到财报记录，或今日已跑过去重跳过）——解决之前"静默跳过和脚本崩溃从外部看一模一样"的可观测性缺口。`--ticker` 手动模式不发这条摘要。

### 12.7 调度接入

无独立 plist，串联在 `com.daily-intel.finance.pm.plist`（见第九节9.1）：`ProgramArguments` 为 `/bin/bash -c "run_finance.py; sas_review.py"`，分号分隔确保 `run_finance.py` 失败不阻塞 `sas_review.py`。手动重跑命令：

```bash
cd ~/Daily_Intelligence
HERMES_DATA=~/.hermes OBSIDIAN_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview" \
  .venv/bin/python scripts/sas_review.py --ticker TSLA

# 永久移除追踪（清仓后）
.venv/bin/python scripts/sas_review.py --exclude TSLA
```

---

## 变更记录（2026-05-26）

### 追问流水线三项 bug 修复

**1. `_unified_preprocess` max_tokens 350→600**
followup 类 JSON 输出（query + search_queries×2 + question_intent 等）超过 350 tokens，截断导致 JSONDecodeError，action 退化为 unknown。修复后 600 tokens 足够。

**2. KG 分隔符 `---KG---` → `===KG===`**
V4 Flash 将 `---KG---` 解读为 markdown 水平线，实际输出 `---\nKG---`，regex 失配，KG +0。改用 `===KG===` 并加注"不能分行"。regex 同时兼容三种格式（新/旧/分行）。

**3. `_fetch_realtime_prices()` session-aware 重写**
`Ticker.info.postMarketPrice/preMarketPrice/regularMarketPrice` 与 Yahoo Finance app 同源：

| ET 时段 | 价格字段 | 涨跌基准 |
|---|---|---|
| 04:00-09:29 | preMarketPrice | 昨收 |
| 09:30-15:59 | regularMarketPrice | 昨收 |
| 16:00-19:59 | postMarketPrice | 今收（regularMarketPrice） |
| 其他 | regularMarketPrice | 昨收，标注"休市" |

旧实现（`yf.download(prepost=True)` / `fast_info.last_price`）仅返回常规收盘价，不含盘前/盘后。

周末 OTC 价格（Yahoo 私有 feed）无任何公开 API 可达，已知限制。

### KG 词表清理 + 价格谓词拦截

- 词表删除 `stock_price`/`price_change_pct`/`stock_price_change`，别名合并至 `price_level`/`had_move_pct`
- `kg_extractor_finance.py` 新增 `_PRICE_PREDICATES_BLOCKED`，`_safe_write_triple()` 硬拦截价格谓词
- Step 4 prompt 禁止列表新增价格类谓词

价格数据由 `_write_price_snapshot()` 直接从 price_rows 写入，LLM 不应参与。

### Polygon.io key 备存

`POLYGON_API_KEY` 存入 `.env`。免费 tier 仅延迟历史聚合，`snapshot` 端点 `NOT_AUTHORIZED`。Starter（$29/月）起支持实时，届时可作为 Yahoo Finance app 级别数据源的备用方案。

---

## 变更记录（2026-06-02 下）KGTriples 审计修复

### 对照改造计划审计，修复 10 项偏移

**`memory_context_finance.py`**
- `_fmt_triple()`：confidence < 0.8 时追加 `conf=X.XX` 标注
- event predicate cap：3 → 5（driven_by/correlated_with 被 upside_catalyst 系统性挤出）

**`kg_extractor_finance.py`**
- `persist_pending_vocab(source_doc, new_entities, new_predicates, source_script="kg_extractor_finance")`：source_script 改为参数；内部过滤 new_predicates len > 20
- `_filter_entity_candidates()`：新增（过滤 new_entities：len > 30 + 括号含数字/百分比）
- `_build_system_prompt()`：追加 object 字段约束（禁止顿号列表、形容判断词、条件句；investment_view 只写状态词）

**`telegram_commands.py`**
- `persist_pending_vocab`：同上签名规范化；内部过滤 new_predicates len > 20
- `_filter_entity_candidates()`：新增（同 kg_extractor_finance.py）
- `_filter_framework_triples()` fallback 集合：补入 `driven_by`、`correlated_with`
- `_mempalace_context()`：改为同时查 finance + hermes 两个 room（日报历史在 hermes）
- `_unified_preprocess` prompt：新增 `relevant_entities` 字段（非 ticker 具名实体，最多 3 个）
- `_preprocess_question()`：透传 `relevant_entities`
- `_llm_followup()`：
  - KG 块移至 mp_ctx 之前（KG 作为精确锚点在向量上下文之前）
  - `relevant_entities` 中的实体调 `_kg_query_bridge()`，结果过滤 SKIP_PREDICATES 后注入 kg_lines
  - kg_lines 格式加 confidence 标注（conf < 0.8 时显示）
- Step 4 prompt ===KG=== 段：
  - `new_entities` 专有名词约束
  - object 三项明确禁止：顿号/逗号列表、形容判断词（脆弱/悬而未决）、条件句（若...则...）
  - `investment_view` object 只写状态词或简短触发名

**`Daily_Intel优化计划.md`**
- B7 新增：KG 实体引导二次向量搜索（改造计划缺口2），含完整 DI 实现方案
- A8 改为观测中（cap 已调整）
- C1 技术债说明更新（函数副本名单扩展）

### KG object 质量规范（2026-06-02 补充）

KG entity 判断唯一标准：**能否被另一条 triple 独立引用？** 不能则是文本，不属于 KG。

| 类型 | 示例 | 判定 |
|---|---|---|
| 专有名词 | `Huawei_ban`, `TSMC` | 合法 |
| 简短状态词 | `hold`, `oversold`, `bullish` | 合法 |
| 顿号合并多值 | `以色列-黎巴嫩停火协议脆弱、伊朗局势悬而未决` | 非法，拆条 |
| 含形容判断词 | `脆弱`、`悬而未决`、`仍在进行` | 非法，向量库 |
| 条件句 | `若跌破100美元则考虑减持1/3` | 非法，investment_view 只写 `reduce` |
| 分析结论句 | `可能走强打压黄金` | 非法，向量库 |

### persist_pending_vocab 规范接口（对齐改造计划）

```python
def persist_pending_vocab(
    source_doc: str,
    new_entities: list,       # 已经过 _filter_entity_candidates() 过滤
    new_predicates: list,     # 内部过滤 len > 20
    source_script: str = "kg_extractor_finance",  # 调用方明确传入
) -> None:
```

Schema（`~/.hermes/kg_vocab/pending_review.json`）：
```json
{"entries": [{"timestamp": "...", "source_doc": "...", "source_script": "...", "new_entities": [], "new_predicates": []}]}
```


---

## 变更记录（2026-06-12）KG triples 系统全面下线

**决策背景**：KG（Knowledge Graph）系统自 2026-05-17 上线后经历多轮迭代——谓词三层分类、写回保护、monitor_item 主动发现、词汇表注入、object 质量规范、6维查询分解评估等（详见上方历史变更记录）。复杂度持续累积，但实际价值未达预期：object 死端节点问题反复出现（见"KG object 质量规范"一节），fallback 集合遗漏等小 bug 持续浮现，维护成本与收益不成比例。决定将 Layer 3（Knowledge Graph）整体移除，系统回退为**两层知识体系**（Obsidian 全文 + MemPalace 向量检索）。本文档第三节"三层知识体系"已改为"两层知识体系"，原 3.3 KG 一节已删除；架构图、运行逻辑伪代码、模型路由表、目录结构均已同步移除 KG 相关条目。本节及以上所有 KG 相关历史变更记录作为决策层审计留痕，原样保留，标注为"已下线子系统"的历史参考。

**执行范围（与 `~/Daily_Intelligence/CLAUDE.md` 2026-06-12 条目一致）：**

- 删除 `kg_extractor_finance.py`（报告后三元组提取，526 行）
- `memory_context_finance.py` 重写：移除谓词三层分类常量（`FRAMEWORK_PREDICATES`/`EVENT_PREDICATES`/`SKIP_PREDICATES`/`_ALWAYS_ON_PREDICATES`）、`_kg_query`/`_score_triple`/`_fmt_triple`/`get_kg_monitor_hits`/`_load_entity_alias_map`/`_resolve_query_names`；`get_finance_context()` 签名移除 `all_tickers`/`news_text` 死参数，仅保留 MemPalace + Obsidian 两段，字符预算合计上限 2000（MP 1200 / Obs 800）
- `run_finance.py` 移除：两处 KG import、`_write_price_snapshot()`、`_tg_notify()`（伴随其唯一调用方一并移除）、两个 prompt 模板中的 `{kg_monitor_section}` 占位符、step 5b（KG monitor_item 主动触发）、step 12（KG 提取）和 12b（价格快照写入）；skip 条件简化为仅 anomaly/geo；步骤重排为 0-13
- `telegram_commands.py` 移除：`import functools`、五个 KG vocab 函数（`_load_entity_alias_map`/`load_kg_vocab`/`normalize_entity`/`_filter_entity_candidates`/`persist_pending_vocab`）、`_kg_query_bridge()`、`_filter_framework_triples()`、`_write_followup_triples()`；`_unified_preprocess` prompt 和 `_preprocess_question` 移除 `relevant_entities` 字段；`_llm_followup()` 移除 KG 决策框架三元组注入段和 `===KG===` 内联写回指令及响应解析逻辑
- 三文件均通过 `py_compile` + import smoke test；TG bot 通过 `launchctl stop/start com.daily-intel.finance.telegram` 重启生效

**附带修正**：文档历史上将 TG bot 的 launchd label 误记为 `com.hermes.finance.telegram`（踩坑记录21、32、调度章节），实际注册 label 为 `com.daily-intel.finance.telegram`（`launchctl list | grep finance` 确认）。CLAUDE.md 相关条目已修正。


---

## 变更记录（2026-06-12 下）footer 精简 + TG 独立运行状态消息

**背景**：原 `finance_footer()` 在每份报告（邮件/Obsidian/TG）末尾固定附加三类信息——隔离声明、Tavily 剩余额度、IBKR 授权状态。其中隔离声明是面向 Hermes MI 的架构说明，与单次报告无关；Tavily 剩余额度是运维信息，混在报告正文降低可读性；IBKR 当前已暂停使用，gateway 不可达分支的报警提示已无意义。

**改动**：

1. `finance_footer(date_str, budget)` 移除"与中国企业情报（[Hermes MI]）完全隔离：独立收件人、独立数据源、独立预算。"行和"Tavily今日剩余: N/20"行，仅保留：
   ```
   ---
   _Daily_Intel · {date} ET_
   {ibkr_note}
   ```
2. `_ibkr_auth_note()` 的 `except Exception:`（gateway 不可达）分支改为 `return ""`，不再输出"[!] IBKR 数据接口无法连接（gateway 未运行）..."提示。`if s.get("authenticated"):`/`else:`（gateway 可达但未认证，"需要重新授权"）分支不变——IBKR 重新启用后该报警仍会正常触发。
3. 新增 `build_status_message()`，在 `main()` 末尾作为 step 13b 调用，生成一条独立 Markdown，通过 `send_telegram_report()` 单独发送到 TG（不进入邮件/Obsidian 正文）。内容：
   - Tavily 本次用量 + 今日剩余 / SerpApi 本次用量（如有）+ 本月已用
   - 情报来源状态：RSS(+Guardian) 条数、Finnhub 即时新闻是否注入、Sonar 宏观快照成功/失败、Tavily/SerpApi 搜索任务数+原始结果数+筛选后条数、Tavily Extract 篇数
   - LLM/Provider 清单：Pass 1、语义过滤（如触发搜索）、Sonar 宏观快照（如成功）、Pass 2（如有 Tavily 数据），均标注 `OR/{DS_OR_PROVIDERS}`

**验证**：三处改动均通过 `~/Daily_Intelligence/.venv/bin/python -m py_compile run_finance.py` + smoke test（mock 数据调用 `build_status_message()`/`finance_footer()`，并验证 `_md_to_tg_html()` 正确转换 `**Daily_Intel 运行状态**` → `<b>Daily_Intel 运行状态</b>`）。

---

## 变更记录追加：2026-07-06 — `call_llm()` 429 限流重试修复（issue #29）

LLM 调用层的容错设计一直是"网络错误/5xx 重试，4xx 不重试"（4xx 通常意味着请求本身有问题，重试没有意义）。但 OpenRouter 的 429（限流）虽然是 4xx，性质上却和 5xx 一样是瞬时可恢复的，之前被误归入"不重试"一类，导致 2026-07-06 夜盘收市速报在遇到限流时 Pass 1 直接放弃、连 OR flex fallback 都没走到，报告静默失败未发送。

修复：`call_llm()` 的 `httpx.HTTPStatusError` 分支把 429 从"直接返回空"改为并入 `>= 500` 的重试路径。这是对现有容错设计的一处补漏，不改变整体"网络/5xx 重试、其余 4xx 不重试"的分类原则——只是把 429 正确归类到"瞬时可恢复"一侧。详见 `Daily Intelligence 开发部署日志.md` 2026-07-06 条目，commit `c985b0c`，issue #29。

---

## 变更记录追加：2026-07-09 — 设计文档全面校对 + 新增第十二节（季度 SAS 深度复盘系统）

对照 `scripts/sas_review.py`、`scripts/sec_edgar_utils.py` 当前实现逐行核对设计文档，修复两处过时表述、补齐一处缺失的模型选型行、新增完整的第十二节：

1. **目录结构参考（第十节）**：`sas_review.py` 一行原写"尚未接入 launchd，手动运行"——已过时（PM plist 已于 2026-07-09 早些时候串联执行，见 commit `34bb1f1`），改为准确描述并指向第九节 9.1；补充 `sas_review.lock`（运行时并发锁文件）进文件清单，此前遗漏。
2. **LLM 选型表（8.1 节）**：原表只列 `run_finance.py`/`telegram_commands.py` 七个调用点，缺 `sas_review.py` 的 SAS 打分调用（`~anthropic/claude-sonnet-latest` via OR，无 fallback，成本从 OR `usage.cost` 字段直接读取）——补为第 8 行。
3. **新增第十二节**：完整描述 SAS 季度深度复盘系统——issue #30/#31/#32/#33 的分工关系、持久化追踪列表（`sas_tracked_tickers.json`，只增不减）、财报触发判定（Finnhub calendar + exchange_calendars 第3交易日）、唯一 fail-closed 步骤（财报锚点拉取，重试耗尽即报警不静默失败）、数据源验证结论（Finnhub 13F 和 yfinance 期权 IV 均不可用，改用 edgartools 拿 Form 4 内部人买入 + 10-K risk factors）、打分与输出（含 Manual 第7节 rubric 提取正则）、NOTIFY_ONLY/防重/并发锁三项运维机制、launchd 接入方式。
4. 顶部"最后更新"元信息同步更新至本次日期。

本次审阅未发现其余章节（一至十一节）与当前代码实现存在实质性偏差；`sas_review.py` 手动运行 `--ticker TSLA` 已于本次会话验证一次（成本 $0.0542，写入 `Finance/SAS_Review/TSLA.md`）。


---

## 变更记录追加：2026-07-09（下）— 移除条件代号引用，改自然语言自解释（issue #34）

**背景**：`Finance/Investment Operating Manual v1.0.md` 第6节用字母代号标注减仓触发情形（条件A/B/C），第7.4/第9节内部又反过来引用这些代号（"第6条的条件A"、"Manual第3条"）；`run_finance.py` 的 Pass 2 分析要求（USER_PROMPT_TEMPLATE_P2）用①-⑧编号，⑤引用⑦的核对结果、⑧引用⑤⑦。用户反馈：代号时间长了记不住，文档和 prompt 应自然语言自解释，尽量不做（包括文档内的）互相引用。排查还发现编号方案已经腐化的实证——`VERIFIABLE_SIGNALS_INSTRUCTION_P2` 标签写的是"⑤"，但实际拼接位置在模板里是"⑧"之后，编号早就与真实顺序脱节。

**改动**：
1. Manual 第6节"条件A/B/C：xxx" → 去掉字母前缀，改纯描述性标题（Alpha大幅兑现 / 出现更高赔率机会 / 仓位结构性超载）
2. Manual 第7.4节"与选股标准第5节呼应" → 内联复述该节实际要求，不点号
3. Manual 第9节框架漂移自检三问中"第3条/第6条/第6条的条件A" → 全部改为直接描述规则内容
4. `run_finance.py` `USER_PROMPT_TEMPLATE_P2` 分析要求从①-⑧编号改为描述性粗体小标题（如"**持仓异动核对（唯一允许给出加减仓建议的依据来源）**"），互相引用处（原⑤引用⑦、⑧引用⑤⑦）改为在本条内联复述被引用规则的完整内容，不再要求读者跳转编号
5. `_compute_holding_signals()` 注入文本"减仓条件C"→"仓位结构性超载"
6. 顺带修复 `VERIFIABLE_SIGNALS_INSTRUCTION_P2` 的编号漂移 bug——新方案不再依赖顺序编号，这类漂移不会再发生

**设计原则（已存跨项目 memory `feedback_no_coded_references`）**：任何面向人或 LLM 反复解读的规则性文档/prompt，凡涉及边界条件、决策规则的引用，一律在引用处直接自然语言复述内容，不用字母/数字代号引用同文档内其他位置的定义——手工维护的编号会随内容增删静默腐化，且代号本身不承载语义，读者（或未来的自己）需要额外一次跳转才能理解。

**验证**：`py_compile` 通过；`_load_framework()` 实测输出确认 Manual 抽取内容不再含字母代号；`USER_PROMPT_TEMPLATE_P2.format(...)` mock 参数渲染无异常。issue #34（已关闭），commit `0db1759`。

---

## 变更记录追加：2026-07-19 — LLM JSON 解析统一迁移至 `llm_json_utils.parse_llm_json()`（issue #49）

`sas_review.py` 此前已用跨项目 canonical JSON 提取/修复工具（`~/Homepage/llm_json_utils.py`，issue #15/PR #22 重写），但 `llm_client.py::call_llm()`（主调用 + OR flex fallback 两处）和 `telegram_commands.py::_unified_preprocess()` 仍是重写前的手写正则（fence 剥离 + 掐头去尾找花括号），后者结尾散文带花括号时会截断错位——这正是 `llm_json_utils.py` 重写要修的那类 bug，在这两处原样复现。改为统一 `sys.path.insert(0, "~/Homepage")` + `from llm_json_utils import parse_llm_json`，与 `sas_review.py` 同一引入模式。

**PR review 追加修复一个真实 P1**：`parse_llm_json()` 文档声明返回类型是 `Any` 而非 `dict`——当外层 JSON 对象本身损坏但内部某个数组字段单独能完整解析时，"挑最长可解析候选"的启发式会直接返回那个数组而非修复外层对象。两处消费方都默认拿到 dict：`call_llm()` 做 `result["_llm_meta"] = {...}` 会 `TypeError`，被外层 `except Exception: return {}` 悄悄吞掉；`_unified_preprocess()` 的调用方紧接着 `cmd["_raw_text"] = text` 同样 `TypeError`，但这里没有 try/except 包裹，会直接崩掉整个 Telegram 长轮询循环。修复：两处均加 `isinstance(result, dict)` 判空，转换成既有的重试/兜底路径处理。`telegram_commands.py` 改动按坑32重启了 TG bot。commit `10e1963`，issue #49（已关闭）。

---

## 变更记录追加：2026-07-20 — 6 个 budget/quota tracker 参数化（issue #41）

Tavily/SerpApi/Adanos/Apify/Brave 五个 tracker 的 load/save/remaining 逻辑高度重复，未参数化。新增叶子模块 `scripts/quota_store.py`（`load_quota`/`save_quota`/`remaining`），只承载这五个 tracker 真正重复的样板逻辑（读 JSON/校验周期键/原子写）；`budget_trackers.py` 五个函数改为委托调用，**公开名字/签名完全不变**，`run_finance.py`/`intel_sources.py`/`sas_review.py` 全部调用点零改动。

Parallel.ai（`telegram_commands.py`）**刻意不纳入**同一契约——它的 `load` 是 setdefault 部分 schema 补全（其余五个是整体重置），`remaining` 在无上限时返回 `float("inf")`（其余五个是 `max(0, limit-used)` 的 int），且有一套跟其余五个都不同的美元加权双字段+独立通知冷却字段模型；只让 `save_parallel_budget()` 复用共享的原子写入，其余原样保留。设计上明确不参数化"何时计入用量"（Adanos 收到任何 HTTP 响应就计数 vs Apify 成功/超时计数+连接失败不计数 vs Brave 仅成功计数）和"谁来 save"——这些是已经分化的业务行为，硬塞进通用函数当 flag 只会制造新 bug。

调查中额外确认两个真实 bug，同一 PR 一并修复：`sas_review.py::_fetch_tavily_context()` 对同一个 `budget` dict 做了冗余的二次 `rf.save_budget()`（`rf.tavily_search()` 内部已存过一次），已删除；`telegram_commands.py` 硬编码 `TAVILY_DAILY_LIMIT=10`，与 `budget_trackers.py` 实际生效的 `20` 不一致，TG「状态」指令用量分母显示错了一段时间，已修正为从 `budget_trackers.py` 导入真实常量。commit `b77b3f6a`，issue #41（已关闭）。

---

## 变更记录追加：2026-07-21 — 社交舆情第三方字段消毒（issue #38）

`_polymarket_brief()`/`_adanos_x_sentiment()`/`_reddit_sentiment_brief()`（`scripts/intel_sources.py`）此前把第三方返回字段未经消毒直接拼进 markdown 注入 Pass 1/2 prompt——第三方响应里的换行符可伪造出假的 `##` 小节边界，误导 LLM 对 prompt 结构的解读。新增 `_sanitize_field()`（折叠空白+剔除控制字符+长度截断）和 `_sanitize_ticker()`（`^[A-Z]{1,5}$` 白名单 + 可选 `allowed` 参数做请求集合成员校验），三处注入点全部套用；只有 Apify Reddit 的 `ticker` 是第三方在响应里回传（不可信），Adanos/Polymarket 的 ticker 是调用方自己传入（可信），因此只有前者需要白名单校验。

PR review 抓到一个真实 bug（`_sanitize_ticker` 先 `str(value)` 再判断类型——`str(None).upper() == "NONE"` 恰好匹配自己写的白名单正则，导致缺失/`null` ticker 的行不再被跳过，反而渲染进 prompt——这正是修复本身引入的回归，见 `docs/PITFALLS.md#84`）。经两轮 review 全部修复并补充 `scripts/test_intel_sources_sanitize.py`（8/8 通过）。commit `f519cd5`，issue #38（已关闭）。详见 [第 5.1 节](#五一数据采集) 社交舆情段落。

---

## 变更记录追加：2026-07-23 — 语义过滤模型从 deepseek-v4-flash 切换至 google/gemma-4-31b-it（issue #53/PR #54）

**触发原因**：2026-07-22 生产环境真实崩溃，`_haiku_relevance_filter()`（Layer 2b 语义排序）报 `'NoneType' object has no attribute 'strip'`。排查确认：`deepseek/deepseek-v4-flash` 即使不发送 `thinking`/`reasoning` key，在当前 OpenRouter 路由下仍会隐式产生 reasoning token，把该函数 `max_tokens=80` 的预算烧在看不见的思考过程上，导致 API 返回 `content: null`；同批确认 `DS_OR_PROVIDERS` 的 provider pin（`DigitalOcean`/`Venice`）并未被 OpenRouter 可靠遵守（Pass 1 两次调用实际都落到了 `Alibaba`）。

**验证**：姊妹项目 `PC611-homepage` 的 LLM-eval 框架测出 `google/gemma-4-31b-it` 是唯一 100% 通过的模型；用 `_haiku_relevance_filter()` 真实 prompt 模板 + 真实形态样本数据做了两次真实付费调用（`max_tokens` 80/150），`completion_tokens_details.reasoning_tokens` 均回传 0，`finish_reason=stop`——验证通过后才切换。

**实现**：`SEMANTIC_FILTER_MODEL` 切换；函数改名为 `_semantic_relevance_filter()`（原名从未用过 Haiku）；返回值从 `list[dict]` 改为 `(filtered, meta)`，真实 provider/fallback 信息串联进 `build_status_message()` TG 状态行（此前硬编码 `"OR/DigitalOcean"`）；移除不可靠的 `DS_OR_PROVIDERS` provider pin 及其在 `run_finance.py` 里因此变为无用的模块级常量。

**PR review 追加修复 4 处**（协作者账号 `blacktomb42`）：`max_tokens` 80→200（原值对 prose/代码块包裹的输出余量不足）；成功响应补 usage/finish_reason 日志（含 `reasoning_tokens`），对 `finish_reason=length` 且内容为空的情况显式告警；`sem_filter_meta` 区分"从未调用 LLM"（`{"skipped": "no_results"|"no_api_key"}`）vs"主备均失败"（`{}`），修复 TG 状态消息把跳过路径误报为双重失败的准确性问题；新增 `scripts/test_run_finance_semantic_filter.py`（5/5，mock `httpx.post` 复现真实故障形态）。commit `a9cbdca`，issue #53（已关闭）。详见第 8.1 节 LLM 选型表第 3 行。

---

## 变更记录追加：2026-07-25 — LLM 选型集中到 llm_config.py + llm_config.json（issue #11/PR #56）

**背景**：issue #11 原始范围是"评估用 Grok 4.3 替换 DeepSeek V4 Flash 做 TG 追问 Step4 主力"。用户提出三点讨论方向：① 更新过时状态文案；② LLM 选型 JSON 化，运行时可改不需要走 PR review；③ Step4 主力考虑开 thinking，fallback 升级为更强模型。三点讨论定稿后实施，范围在实现过程中扩大。

**1. 状态文案修正**：`_build_status()` 硬编码 `个人化推理：{model}（Azure）`，自 2026-05-21 迁移离开 Azure/Claude Sonnet 后就是过时文案；追问回答 footer 也硬编码"V4 Flash"标签。均改为从 `llm_config` 动态取值，不会再次腐化。

**2. `scripts/llm_config.py`**：全项目 8 个 LLM 调用点（`report_pass1`/`report_pass2`/`semantic_filter`/`macro_brief`/`tg_preprocess`/`tg_gap_detect`/`tg_research`/`tg_followup`）集中为 named stage，`DEFAULTS` 是唯一最终兜底。可由项目根目录 `llm_config.json` 逐字段覆盖，加载器 fail-safe：文件缺失/JSON 损坏/未知 stage 或字段/字段类型或取值非法均逐字段回退默认值并记 WARNING，不让流水线崩溃；每处生效覆盖记 INFO 日志。新增跨字段安全网 `_enforce_thinking_budget()`——`max_tokens` 必须比 `thinking.budget_tokens` 多留至少 500 headroom，不满足则两个字段一起回退（field-level 校验各自独立通过时无法发现这类组合风险，这是 PR review 提出的加固项）；`_v_providers` 透传未知 OpenRouter provider key（如 `data_collection`）而非静默丢弃；`_build()`/`stage()` 用 `copy.deepcopy` 避免多个 stage 共享的 provider 默认对象被跨 stage 污染（长驻的 TG bot 进程尤其需要这层防护）。

**`llm_config.json` 一度被误设为 gitignore，当场被用户指出没有站得住的理由**：最初照搬 `tg_offset.json`/budget 计数器那类"运行时状态"文件的 gitignore 套路，没有意识到这个文件性质完全不同——它是人手改的、有意图的配置决策，跟 `watchlist.md` 是同一类东西而非机器写的临时状态，且不含任何敏感信息，没理由不入库。gitignore 掉的代价：没有审计记录（`git log` 看不到改过什么、何时改的）、文件丢失会静默退回 DEFAULTS 没人知道、而且这个决定本身让"配置文件"从未被真正创建出来（cp 模板才能用，功能等同于没做）。已改正：取消 gitignore，把该文件（当前内容与 DEFAULTS 完全一致，零覆盖）入库；代码注释/README/本文档所有"gitignored"表述一并修正（commit `fc7b91f`，直接提交 main，纯配置/文档修正无功能改动）。

**3. TG Step4（`tg_followup`）**：开启 `thinking`（budget=3000，max_tokens 8000→12000，timeout 120s→180s——thinking token 先于可见输出生成，短超时会把大部分调用逼进 fallback）；fallback 由 `x-ai/grok-4.3` 升级为 `x-ai/grok-4.5`，用 OpenRouter 统一 `reasoning={"effort":"medium"}` 参数（"medium"是 effort 档位不是独立模型 slug，已用真实调用验证 slug 存在、参数生效、provider=xAI）。

**实现过程中额外发现并修复两处真实生产 bug（不在原计划范围）**：`tg_gap_detect`（Step3 P1 补搜判断，60-token 预算）和 `tg_preprocess`（Step1 意图分类+字段抽取）此前都用 `deepseek-v4-flash`。用这两个 stage 各自的真实 prompt 直接实测：`tg_gap_detect` 在未开 thinking 的情况下把整个 60-token 预算烧在隐藏推理上（`finish_reason=length`，`reasoning_tokens=60`，`content=None`），函数自身的 `try/except` 静默吞掉随之而来的 `AttributeError`、返回 `None`，跟"正确判断出无需补搜"完全无法区分——这个功能自 2026-05-23 上线起大概率就没有真正生效过，只是看起来优雅地 fail-open。`tg_preprocess` 更严重：`temperature=0` 下对同一条简单指令（"删关键词 US-Iran blockade"）连续 3 次真实调用给出 3 种不同的错误结果（幻觉出枚举外的 action 值"remove_keyword"、预算耗尽 content 全空、JSON 从中间截断落到"unknown"），三次都没能给出正确的 `remove_geo`；这个 stage 是 bot 的指令路由入口而非可选增强，出错意味着用户的加/删指令表面上"没反应"或被错误分类。两处均切换为 `google/gemma-4-31b-it`（参考跨项目题库 Obsidian `Hermes/Homepage/LLM-No-Reasoning-eval设计与实现.md`，该题库 21 case × n=10 全量测试 gemma-4-31b-it 210/210 100% 通过，是目前唯一验证到零失误的候选），实测零 reasoning token、5 种指令类型全部正确复现（含直接跑通真实代码路径验证）。已知代价：`tg_preprocess` 在跨标的关联问题上 gemma 的 `relevant_tickers` 更保守，但 deepseek 自己在同一问题上也不稳定，不算可靠优势。

**PR #56 review（协作者账号 `blacktomb42`）修复 2 个真实 bug**：① `answer = content or reasoning_content` 在 thinking 耗尽预算的真实场景（`content=null`+`finish_reason=length`+`reasoning_content` 含部分思维链）下，会把思维链原文当作答案返回给用户——这段思维链文本让 `answer` 变成 truthy，budget-exhausted 分支完全不会触发，等于本 PR 专门为修 issue #53 加的防护在唯一真正需要它生效的场景下失效了。修复：改为按 `finish_reason` 消歧，只有 `content` 是有效答案来源，`finish_reason=="length"` 时绝不提升 `reasoning_content`。② 主力 HTTP 200 但内容为空/预算耗尽时此前直接报错，从不尝试已配置的 fallback（只在传输层失败耗尽 3 次重试后才会 fallback）——改为空内容也走一次 fallback，跟传输失败同等对待，不再让用户对着一个能用的 fallback 被要求去改配置文件。

**Review 之后又出现一条 P1 claim，实测证伪**："model-only 覆盖（只改 `model`/`thinking`，不动 `providers`）会残留跟新模型不兼容的 provider pin，导致换模型实际失效"。真实调用复现：`model=x-ai/grok-4.5` + 遗留的 `provider={order:[DigitalOcean,Venice], allow_fallbacks:true}`（这正是 `tg_followup` 默认继承到的值）——结果 HTTP 200、`provider:xAI`、正常返回，因为 `allow_fallbacks:true` 语义本身就会在 order 列表都不支持目标模型时自动路由到支持的 provider，不会报错或用错模型。对照测了 `allow_fallbacks:false`，确认这种情况下才会真的 404——但那不是默认值，也不是 review 给出的复现配置实际继承到的值。**教训与本项目一贯做法一致**：评价/验证他人（或其他 LLM）review 的 claim 要拿真实调用核实，不能只看是否读起来有道理，跟 `feedback_verify_llm_review_claims` 是同一条原则。

**端到端验证**（2026-07-25，周六用 `FINANCE_FORCE_RUN=1 FINANCE_FORCE_SLOT=pm` 手动跑一次真实 PM 报告，非交易日不影响真实定时任务）：日志正确显示 `LLM tokens [report_pass1/deepseek/deepseek-v4-flash]`、`[report_pass2/deepseek/deepseek-v4-pro]` 的 stage 标签、语义过滤 `reasoning=0`（provider=Crusoe）——`llm_config` 的默认值和标签在真实流水线里生效，邮件/TG/Obsidian 全部正常发出。手动跑该脚本时用 Claude Code Bash 工具默认 2 分钟超时会被杀（历史实测完整跑一次约 2m20s，属正常耗时非异常），需要显式加长超时；被杀进程的 `finally` 清理来不及跑，`run_finance.lock` 残留旧 PID，但 `fcntl.flock` 跟进程走、进程一死锁自动释放，不阻塞重跑（见 `docs/PITFALLS.md#86`）。

`telegram_commands.py` 改动已按坑32重启 `com.daily-intel.finance.telegram` 并确认加载新代码。PR #56 squash-merge（`f351dd5`），远程分支已删除，issue #11 随 `Closes #11` 自动关闭。详见第 8.1 节 LLM 选型表第 5/6b/7 行、第十节目录结构。

## 变更记录追加：2026-08-04 — `report_pass1`/`am_calibration` 切换 gemma-4-31b-it（issue #59，PR #61）

> 2026-09-24 从仓库 `docs/design.md` 回补：该条当时只写进了仓库快照，Obsidian 版漏记。`report_pass1` 已随 PR #89 删除，本条保留作选型记录。

**背景**：`report_pass1`（`deepseek/deepseek-v4-flash`，未显式传 `thinking` key）2026-08-03 出现两次真实生产故障——AM 主报告调用 2/3 次被隐式推理吃满 `max_tokens=4000` 预算（`finish_reason=length`）；`calibration.py::evaluate_am_calibration()` 当时复用同一 `report_pass1` stage 做 PM 校验，3/3 次全部失败，靠 `fallback_model` 兜底才拿到结果。根因与 issue #53 当年修过的"隐式推理吃光判别式小任务预算"是同一模式，只是 `report_pass1` 从未被纳入那次修复范围。

**改动**：
1. `llm_config.py` DEFAULTS：`report_pass1.model` → `google/gemma-4-31b-it`，`providers` → `None`（不再走 DeepSeek 专属的 DigitalOcean/Venice pin）
2. 新增独立 stage `am_calibration`（同样默认 `google/gemma-4-31b-it`），`calibration.py::_evaluate_am_predictions()` 改用该 stage 而非复用 `report_pass1`——理由与 `tg_gap_detect`/`tg_followup` 拆分为独立 stage 一致，避免未来调 report_pass1 预算/模型时静默影响这个无关的 PM 判断
3. `llm_config.json`/`llm_config.example.json` 同步更新；新增回归测试 `test_calibration_uses_its_own_stage_not_report_pass1`

**验证**：用 2026-08-03 当天真实生产故障数据（真实价格表、RSS、Sonar宏观快照、AM可验证信号清单）重建两种 prompt 形状直接调用 OpenRouter 对比——`deepseek-v4-flash` 同条件下 4/4 复现真实故障（确认测试 prompt 忠实复现生产条件）；`google/gemma-4-31b-it` 6/6 全部 `reasoning_tokens=0`、`finish_reason=stop`，completion 仅占预算16-20%，路由到3个不同 OR provider 均稳定；内容质量核查（非仅结构校验）确认输出正确。另用真实生产 `call_llm(stage="report_pass1")`/`calibration._evaluate_am_predictions()` 端到端冒烟测试确认代码路径正确接入。`test_llm_config.py` 21/21 通过。

**同批核查（issue #60，未在本 PR 实施）**：仅剩 `tg_followup` 用 `deepseek-v4-flash`（刻意保留以维持开放式持仓推理质量）。压力测试发现该 stage 同样会无视 `thinking.budget_tokens=3000` 软上限（1/3 次烧穿 `max_tokens=12000`），已有 fallback 兜底且真实生产 0 次失败，判定为低优先级。候选 `openai/gpt-5.6-luna`（非pro）+`reasoning.effort=high` 已用同一真实数据验证 7/7 可行，实施需要代码改动（OpenAI 系模型走 OpenRouter 统一 `reasoning` 参数而非 DeepSeek 的 `thinking` 字段），当时建议单独排期；随后的实施见下一条 2026-08-05 记录。issue #59/PR #61 review（协作者 `blacktomb42`）额外指出 CLAUDE.md 状态记录和本文档未同步，均已修正。

---

## 变更记录追加：2026-08-05 — tg_followup/report_pass2 切 gpt-5.6-luna，report_md 脱离 JSON，sas_candidates 独立 stage（issue #60/PR #62）

**触发原因**：`tg_followup`（issue #11 时切到 `deepseek-v4-flash+thinking`）压力测试发现 `thinking.budget_tokens` 是软性提示、不是强制上限——1/3 次真实调用无视 `budget_tokens=3000` 一路烧穿 `max_tokens=12000`。同期用户观察到 DeepSeek V4 Flash 系列表现波动，借机一并评估 `report_pass2`（`deepseek-v4-pro+thinking`）换模型。真实数据对比（2026-08-03 PM 数据重建）发现 `deepseek-v4-pro` 一处真实正确性 bug：把 ORCL/CACI 事件在 `sas_candidates` 字段归类为"生态位验证"，却在"持仓异动核对"正文里把同一事实当"认知提升-战略节点解锁"处理，给出违反 prompt 规则的加仓建议；同批还错把 SPCX 标注成 ETF。候选 `openai/gpt-5.6-luna`（非pro）+`reasoning.effort=high` 在同一测试中未出现这两个问题。

**实现**（`scripts/llm_config.py`/`llm_client.py`/`run_finance.py`/`telegram_commands.py`）：
1. `tg_followup`、`report_pass2` 均切换到 `openai/gpt-5.6-luna` + `reasoning:{"effort":"high"}`，`thinking` 字段清空；`providers` 改为硬锁定 `{"order":["OpenAI"],"allow_fallbacks":false}`（不允许降级到其他 provider，与姊妹项目 `paperview/clip_processor.py` Stage 2 的已验证配置一致）；`max_tokens` 均提到 16000。
2. `report_pass2` 的 `report_md` 不再包在 JSON 里——`llm_client.py::call_llm()` 新增 `parse_json=False` 模式，直接返回裸 markdown 文本。原因：`report_md` 是全项目单次调用最大的 payload，也是唯二撞过 `finish_reason=length` 的 stage 之一（另一个是已修复的 `report_pass1`），JSON 包裹意味着截断发生在字符串中途会把已经写好的大半份报告一起作废；裸文本被截断只丢末尾。
3. `sas_candidates`（issue #32，命中 Manual 7.4 内部信号清单/认知提升标准时的证据队列）从 Pass 2 JSON 的一个字段拆成独立的 `sas_candidate_extract` stage（`google/gemma-4-31b-it`），复用 Pass 2 已经组装好的价格/新闻/持仓上下文单独调用一次，互相隔离——`sas_candidates` 提取失败不再能连累 `report_md`。
4. 六个 `google/gemma-4-31b-it` stage（`report_pass1`/`am_calibration`/`sas_candidate_extract`/`semantic_filter`/`tg_preprocess`/`tg_gap_detect`）的 `providers` 从不锁定统一改为锁定 OpenInference（`allow_fallbacks:true`）——此前观察到真实调用散落在 Friendli/Crusoe/Novita/OpenInference 多个 provider。

**Review 抓到一个真实 P0**（`blacktomb42`，PR #62，两轮）：`call_llm()` 的免 JSON 路径直接复用了旧的 `content or reasoning_content or reasoning` 取值顺序——`finish_reason=="length"` 且 `content` 为空时会把部分思维链错误当成 `report_md` 正文返回、不触发重试/fallback，是 PR #56 修过的"CoT 被错误提升为答案"这个 bug 在新调用路径里的重现。用镜像 `_parse_step4_response` 判断逻辑的 `_resolve_content()` 修复，primary 和 flex-fallback 两条路径都改了。另修了 4 项非阻断建议（SAS 抽取包 try/except 隔离、hard pin 跳过 flex fallback 的行为记入注释、legacy JSON 包裹防御性 unwrap、删除死代码 `_DS_PROVIDERS`）+ 二轮 review 的 2 项残留（SAS prompt 措辞防止裸 `[]` 输出、Step4 日志措辞更新）。61/61 测试通过。

**合并后验证**：squash merge `384be56`，issue #60 自动关闭。`com.daily-intel.finance.telegram` 已重启确认生效。当晚用户要求对同一交易日完整重跑一次 PM 报告（合并前 17:11 ET 已用旧代码跑过一次），产出真实生产双跑对比——`gpt-5.6-luna` 在信源怀疑度处理、"生态位验证不能直接当认知提升"规则遵守度上实测优于 `deepseek-v4-pro`；`sas_candidate_extract` 独立调用正确遵守了 fact 字段"含来源"的格式要求，旧的 Pass2 内嵌调用没有。完整对比记录见 Obsidian `Hermes/Homepage/LLM-Reasoning-eval设计与实现.md`（"生产环境双跑对比"节）和 `LLM-No-Reasoning-eval设计与实现.md`（§21.3/§21.5）。


## 变更记录追加：2026-08-11 — fetch_52week_stats 重试 + yfinance ERROR 降噪（issue #63/PR #64）

**触发**：2026-08-10 AM 主价格 18/18 成功后，Pass2 Layer B 的 `fetch_52week_stats()` 对 INTC 拉 `period=1y` 瞬时失败；yfinance 连打 3 行 `possibly delisted` ERROR，Homepage healthcheck `finance 新错误`（阈值 1）WARN；报告仍正常发出。

**决策**：不做 Finnhub 1y candle fallback（免费档无权限）；只做 (A) bulk miss → `Ticker.history` + 1 次延迟重试；(B) 拉取期间 yfinance logger → CRITICAL，失败改应用 WARNING。

**实现**：`scripts/fetch_prices.py`；测试 `scripts/test_fetch_52week_stats.py` 12/12。squash merge `b6acbda`，issue #63 关闭。无需重启 TG bot。

## 变更记录追加：2026-08-13 — Telegram 进程级 httpx.Client（issue #65/PR #66）

**触发**：KeepAlive `telegram_commands.py` 跑 8 天后 `phys_footprint` 386MB（峰值 675）；同代码 6h 到 459MB。冷启动 import 只有 38MB。

**决策**：不是「httpx 没关 Client」（顶层 `post()` 已经 `with Client()`），是短命 Client+TLS 在永不退出进程里被 macOS libmalloc 碎片化。主修复 = 进程级单例 Client；连续输运失败/满 6h 重建；24h `sys.exit(0)` 交 KeepAlive。顺带 #58：`call_telegram` 补 `ConnectTimeout`。不收口 OpenRouter/Exa/Finnhub。

**验收**：squash `dda8d66`，已 kickstart。同一 PID 13h10m → 58MB（旧斜率约 70MB/h）。剩余 ~2MB/h，有日切不会堆到两周后数百兆。

## 变更记录追加：2026-08-13 — 主路径 yfinance 8d/2d 降噪 + 日线按列合并重试（issue #67/PR #68）

**触发**：2026-08-13 AM `yf.download(period=8d)` / `period=2d` 对十余个 ticker 瞬时假 delisted；yfinance 库 logger 连打 34 行 ERROR，Homepage healthcheck 阈值 1 必报。报告仍发出，价格 8/18。issue #63 只把 `_quiet_yfinance_logs()` 套在 `fetch_52week_stats`（`period=1y`）上。

**决策**：quiet 扩到主路径两次 download；日线缺价再拉一次，按列合并回第一帧（不整表覆盖，避免更差的 retry 丢掉 Finnhub 补不上的商品/FX）；取值必须列名对得上，防止坍缩单列把幸存者价格写到别的 ticker。

**实现**：`scripts/fetch_prices.py`；测试 `scripts/test_fetch_prices_yfinance_noise.py` 10/10。两轮 review 后 squash `df36a78`，issue #67 关闭。无需重启 TG bot。


## 变更记录追加：2026-09-03/09-04 — PM 报告日线批量滞后时静默用昨收顶替今收（issue #69/PR #70，已合并 `d457455`）

**触发**：用户对照 yfinance iOS App 真实数据，发现 09-03 夜盘报告 PLTR 价格描述与实际完全对不上（报告"收盘 $169.46"，真实收盘 $182.53+7.71%）。排查确认报告里的"收盘价"其实是昨收——批量扫描 6 月至今全部夜盘价格快照，确认 09-02（14/18 标的）、09-03（13/18 标的）"日内↑↓（vs前收）"集体显示 `+0.00%`，是历史级异常，此前正常噪音只有 0-5/18。

**根因**：`fetch_prices.py` PM 分支在批量日线（`period=8d, interval=1d`）当天数据缺失时静默回退成批量表最后一行（=昨天），且这个 fallback 路径完全没有日志。`git log` 确认 `fetch_prices.py` 自 08-13（#67/#68）起未改动，`yfinance` 版本锁定 1.3.0 未变，launchd 调度时间未漂移——排除代码/依赖/调度侧变量，指向 Yahoo 后端日线数据落库延迟本身变得更严重/更常态化（外部数据源时序行为，不受我方控制）。

**权衡**（用户提出加 fallback 接口 / 推迟取数时间 / 两者都做三个候选，均评估后否决，见 issue #69 comment）：不加新数据源——当前故障不是缺数据，是有正确数据（intraday `prepost=True` 当时已经拿到今天数据，`vs今开`/`盘后`两列因此是对的）却被代码丢弃未用；不推迟报告时间——Yahoo 侧滞后没有 SLA，推迟只是"赌赢概率"变化不是消除，且牺牲"夜盘速报"本该有的时效性。

**设计与实现**（见 issue #69 comment 详细契约）：PM"今日收盘/今日开盘"改由同一次运行已拉取的 intraday（`period=2d, interval=1m, prepost=True`）提供——`_get_pm_prices()` 新增返回今日开盘（新增 `_opens_1m()`/`_field_1m()` helper），原先只被丢弃的今日常规时段收盘价改为启用；批量日线只保留给 5 日涨幅等需要多日窗口的计算。intraday 也没有今天数据时，该 ticker 直接从 `rows` 中剔除 + WARNING（`f"{ticker} PM: no intraday regular-session close available, price unavailable"`），`run_finance.py` 现有的失败标的检测无需改动就自动接管。`week_change_pct` 修正为从 `_closes_prev` 的末尾往前数 5 个交易日作为锚点。

**协作者 `blacktomb42` 首轮 review 给了 REQUEST_CHANGES，抓到 2 个真实 bug，均已修复**：① `_get_pm_prices` 的"常规时段"筛选只卡了 `time<=16:00` 上界、没卡 `>=09:30` 下界——`prepost=True` 拉到的 04:00 盘前 bar 也满足这个条件，`opens_today.iloc[0]` 在真实运行中会把盘前价错误当成"今日开盘"，`vs今开` 列会算错——已改为显式 `09:30<=time<=16:00` 双边界，并在测试 fixture 里加入盘前 bar 复现。② PM 分支全部 ticker 因 intraday 也缺今天被逐 ticker `continue` 后 `rows` 为空时，函数尾部原有的 `if not rows: 走 Finnhub fallback` 仍会整表回填 regular-session 价格，绕过了刚做的硬失败设计、跳过 `run_finance.py` 的禁引声明——已改为 `slot=="pm"` 且整表落空时直接返回 `[]`，不再走 Finnhub。

**测试**：`scripts/test_fetch_prices_pm_intraday_source.py` 现 3/3（新增盘前 bar 回归用例 + "Finnhub key 存在也不应被调用"断言），`test_fetch_prices_yfinance_noise.py`（10/10）无回归。本次改动仅限 `fetch_prices.py`，不涉及 `telegram_commands.py`，无需重启 TG bot。

**状态**：第二轮 re-review APPROVE（附一条非阻塞 nit：`_get_pm_prices` docstring 仍写 `<=16:00` 未提 `>=09:30`，已在合并前顺手改一行同步），squash 合并至 main（`d457455`），远程分支已删除，issue #69 自动关闭。此前文档一度在 PR 未合并时预写了"已合并"，已改正——状态段只能记已发生的事。

## 变更记录追加：2026-09-21 — Digitimes RSS + 异动追因 query + 7 天围栏（issue #72/PR #73，已合并 `4a54d39`）

**触发**：2026-09-21 AM INTC 盘前 +5.47% 正确标记异动，未检索到 Digitimes 英特尔-友达 Micro LED 包装新闻；Tavily 被 `INTC CL=F stock news earnings` 导向过时 Q2 财报；INTC 被异动/Pass1/rotation 各查一次。

**实现**：Digitimes 加入 `RSS_FEEDS`；`_anomaly_search_jobs` 前 3 大 `|change_pct|` 各一条追因 query；7 天围栏只作用于 `_anomaly_query` job，年龄相对 `now_et`；rotation 与当天异动 ticker 去重；Pass1 `{anomaly_tickers_note}`。

**Review**：初版围栏打在 pooled `score_and_filter`（砍 rotation 30 天窗）且用墙钟（FORCE_DATE 补跑误删）。已修。测试 `test_issue72_anomaly_search.py` 9/9。不改 `telegram_commands.py`。

**未改**：rotation 命中仍进同一 `tavily_section`（issue #74）。

## 变更记录追加：2026-09-22 — PM 异动覆盖 + Finnhub 公平截取（issue #76/PR #79，已合并 `924f923`）

**触发**：2026-09-21 PM 有 6 个异动时，Finnhub AH 内容触发旧短路，前3大异动没有 Tavily 追因；同时 #72 的去重集合错误使用全量异动，导致第4名以后虽没有 anomaly job，仍被 Pass1 与 rotation 当成“已覆盖”。Finnhub 另有全 ticker 混池只取最新15条的公平性问题。

**实现**：AM/PM 均生成前3大异动 job，移除 `finnhub_covers` 死参数；job 携带 `_anomaly_ticker`，Pass1/rotation 只从实际 job 推导覆盖集合。Finnhub 保留跨 ticker去重，改为每 ticker 各取最近5条后合并。Tavily 日预算 20→25。

**验证与状态**：先写失败回归测试，覆盖 6 个 PM 异动、第四名 NVDA 不进覆盖 note 且 rotation 不跳过、Finnhub 5×5 与跨 ticker 去重、预算常量；实现后定向 13/13、全仓库 99/99、`compileall` 与 `git diff --check` 通过。`blacktomb42` 对 exact head `5600abd` 的 pending review 为 0 bugs / 1 文档同步 suggestion / 0 nits，文档同步后 PR #79 squash 合并为 `924f923`，issue #76 自动关闭。未部署、未触发生产报告；issue #74 的 rotation 结果同池问题不在本次范围。

## 变更记录追加：2026-09-23 — 多日累计涨跌追因（issue #80/PR #81，已合并 `9a05d3f`）

**触发**：INTC 从 2026-09-19 附近到 09-21 累计上涨约 30%（友达 Micro LED 先进封装）。流水线只看“今天是不是异动”。09-22 盘前 -1.28%、收盘 +1.71%，都低于 3% 单日阈值，之后没有任何机制回头追这条催化剂。

**实现**：`_compute_multiday_moves()` / `_unexplained_move_search_jobs()`。阈值 3 日绝对涨跌 ≥15% 或 5 日 ≥20%，3 日优先。与当日异动 job 去重，每次最多 2 条，插在异动之后、Pass 1 之前。排除商品/FX/指数 ETF 和 AAOI。不持久化“是否已解释”。Pass 1 增加 `{unexplained_move_note}`。

**Review（`blacktomb42`，合入前已修）**：① 无单日异动且无地缘命中时，skip 发生在多日检测之前，独立触发无效。检测改到 skip 之前，有多日 job 则继续跑。② job 只设 `days`，搜索循环仍用上次报告日到今天的 `start_published_date`，夜盘会把 09-21 的催化剂切掉；`min(query_days, window+2)` 也提供不了缓冲。job 现在自带 `start_date`/`end_date`：行情第一个交易日再往前 2 个自然日，到报告日。AM 分子是前收，锚点比 PM 多回一个交易日。09-22 INTC：开盘前从 09-14 起，夜盘从 09-13 起。③ 日线不足 3 或 5 个交易日时，旧逻辑用最早收盘价仍标成完整窗口。触发档改为空、不触发；价格表 5 日涨跌的短历史回退保留。

**验证**：`scripts/test_issue80_unexplained_move.py` 10/10；issue #72 9/9、#76 4/4、PM 价格 3/3、yfinance 噪音 10/10。未跑付费报告。不改 `telegram_commands.py`。issue #74 未改。

## 变更记录追加：2026-09-23 — Extract 名额预留（issue #82/PR #83，已合并 `cf9cc33`）

**触发**：2026-09-22 PM 重跑验证 issue #80 时，INTC 的未解释大涨 query 正确生成（`INTC stock surged 27.5% over 5 trading days...`，3 条结果），但进入 `score_and_filter` 后被同批地缘新闻的 keyword bonus 压下去。Extract 的 10 个 URL 里没有 INTC，Pass 2 仍写“没有对应的新公司级事实”。

**实现**：异动（最多 3）和未解释大涨（最多 2）各留一条 Extract URL，只在自己的结果里按域名、时效、是否视频页选，不参加开放池打分。开放池预筛 25、语义过滤约 15。`tavily_extract()` 仍是每次最多 10 个 URL；预留先发，开放池再按 10 个一批。满载约 20 个 URL、4cr。日上限仍为 25。Tavily 文档上限是单次 20 个 URL，代码里的 10 是本项目的 2cr 上限。

**一份名单**：`_must_answer_tickers()` 是唯一组装点。下游不再各自拼「这次必须解释哪些 ticker」。7 天围栏、开放池 keyword bonus、Extract 重排 query 都读这份。Extract 的预留 query 是整份名单；开放池 query 是同一份名单加地缘词。新增一类必须解释的 ticker 时，job 写 `_must_answer_ticker` 即可，不用改三处判定。

**Review**：对 `d7436ea` 的意见指出预留 URL 进了 Extract，但共享 query 仍只有异动 ticker 和地缘词，安静的多日大涨不会进 Tavily 的 chunk 重排。`6af2dfc` 拆开两次意图。`bbb9a90` 把三处消费收成一份名单。

**验证**：`scripts/test_issue82_extract_reservation.py` 15/15，加上 #72 9/9、#76 4/4、语义过滤 5/5、#80 10/10。未跑付费报告。不改 `telegram_commands.py`。issue #74 未改。

## 变更记录追加：2026-09-23/24 — Pass 2 截断修复、社交舆情只查个股、Extract 补齐、供给事件例外（PR #94–#97）

四个 PR 由云端 session 在 2026-09-23 晚至 09-24 合并（云端访问不到 vault，本节由宿主机 session 于 09-24 同步）。同步时一并把正文按 issue #87 PR #89（已合并 `65816d9`）之后的实际代码改写：第二节流程图、§5.1、新 §5.1b（情报快照与代码深挖）、§5.2、§5.4、§7.1b、第八节选型表、第十节目录结构；#89 之前的 Search+Extract 三层设计保留为 §5.1c 历史记录。#87–#93 本身在本文件中此前没有独立变更记录，其设计以正文与仓库 `docs/design.md` 为准。

### Pass 2 截断被当成功发出（PR #94，`778b5fc`）

2026-09-23 PM 定时报告写完“要点”后，在“INTC（持仓）收于$122.57，”处断掉，后面直接接上预判校验；邮件和 TG 照常发出，TG 运行状态也显示 Pass 2 成功，没有任何告警。日志：`LLM tokens [report_pass2/openai/gpt-6-luna]: prompt=16792 completion=16000 reasoning=15781 finish_reason=length`，正文只剩约 219 token。

根因两层：① issue #90/PR #91 把模型切到 `gpt-6-luna`、推理强度 xhigh，但 `max_tokens` 仍是 16000，推理与正文共用预算，被推理用光；② `llm_client._resolve_content()` 只在正文为空时把 length 截断当失败，非空的截断正文照常作为成功返回。这是 PR #62 有意留下的行为，当时有测试 `test_call_llm_parse_json_false_accepts_partial_content_on_length` 锁住它，本次改成相反的预期（拒绝残缺正文并走 fallback）。

修复：`parse_json=False` 路径新增 `_free_text()`，`finish_reason=length` 时即使有正文也判为失败，依次走同模型重试 → `fallback_model` → `run_finance` 输出代码生成的情报快照摘要并发 TG 告警「Pass 2 失败」。`report_pass2.max_tokens` 16000→32000（`llm_config.py` DEFAULTS、`llm_config.json`、`llm_config.example.json` 三处同步）。`call_llm()` 非流式 HTTP 超时由固定 180s 改为 `_http_timeout()` = `max(180, max_tokens // 50)`，32000 对应 640s。

补跑验证（2026-09-23 18:38 PT）：`completion=12336 reasoning=10358 finish_reason=stop`，正文约 1978 token，Pass 2 用时约 143s。对照 `gpt-5.6-luna`/high 的正文：09-22 PM 约 3423 token、09-23 AM 约 2125 token——xhigh 推理量约翻倍，正文并未变长。32000 的余量目前只有这一个样本支撑，需继续观察推理峰值（约 25000 为重新评估阈值）。

### 社交舆情只查个股（PR #95，`cb67806`）

09-23 PM 把 `CL=F` 发给 Adanos 返回 422；Adanos 只要收到 HTTP 响应就计入月度额度，白白消耗。新增 `run_finance._social_tickers()`：只保留 watchlist 个股中不在 `intel_collect.ETFS`（QQQM/VOO/EWJ/SGOL）里的标的，异动个股在前，最多 4 个。Adanos 与 Apify Reddit 共用这份列表。

### Extract 补齐与直链解析日志（PR #96，`c7f0fa6`）

Extract 按 `ceil(URL 数/5)` 计费，09-23 PM 只送 7 个 URL 却付了 2cr。第一轮收集后按涨跌幅从强到弱补到下一个 5 的倍数（最多 10）：先取该标的下一条不同落地域名的直链（只限原本就走直链、或因 Extract 名额满被跳过的标的），再取搜索结果第 3 条。`run_finance` 搜索 `max_results` 2→3（basic 仍 1cr）。补进来的 URL 排在末尾，预算不够时最先被截掉；快照新增 `extract_topup_count`。`_direct_leads()` 每次调用按标的记 INFO：`Deepen direct leads {ticker}: N leads (limit L), Finnhub redirects resolved a/b, X.Xs`；302 解析结果同次运行内缓存，补齐轮不重复发 HEAD。背景：09-23 PM 在 18:34:51（FRED）到 18:36:07（第一条 Tavily）之间 76 秒无日志，时间耗在串行 Finnhub 302 HEAD 解析上。并发解析和单标的解析上限这次不做，先看日志数据。

### Pass 2 供给事件例外（PR #97，`2d596b8`）

Pass 2 提示词在“无进展就省略”规则后加例外：已排期的供给事件（限售股解禁、增发或 ATM 发行、配售、指数纳入或剔除调整）在生效日之前和之后的报告里都要保留，不算“无进展”；写明生效日期、规模，以及与当天价格或成交量的关系。触发案例：09-23 PM 的 SPCX 约 3.28 亿股解禁、AAOI 至多 6 亿美元 ATM 发行。已知局限：规则只保证模型不删，不保证信息找得到；生效日当天没有相关新闻时 Pass 2 看不到该事件。候选方案“供给事件日历”（代码按日期注入）未实现。

### 同一次排查的其他结论（未改代码）

- Sonar 宏观快照没有浪费：用于 Pass 2 正文、PM 校准和 Context Log 三处。但 #89 之后 SAS 候选抽取的输入只剩情报快照（#89 之前还能看到 RSS、Finnhub、Sonar、社交舆情和 Tavily），是否把 Sonar 加回来未定。
- `sas_candidate_extract` 的 `completion=13` 即 `{"sas_candidates": []}`，是正常空结果。AAOI 是观察标的，按规则不进 SAS；解禁、增发这类短期供给事件属于正文职责，不是 SAS 的职责。
- 信息量偏少除截断外，#89 本身有结构性原因：输入收窄（无开放池，地缘话题无全文）且提示词要求“无进展就省略”。需观察几天正常报告后单独评估，#94–#97 均未处理。
