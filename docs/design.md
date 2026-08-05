# Daily Intelligence 系统设计文档

> 面向独立实现者的完整设计参考。本文档描述一套个人财经情报系统的设计思路、体系结构和实现细节，适合在自有 Claude Code 环境中按需裁剪复用。
>
> **最后更新**：2026-08-04（issue #60：`tg_followup` 与 `report_pass2` 均切换至 `openai/gpt-5.6-luna`+`reasoning.effort=high`（provider锁定OpenAI）；`report_pass2` 的 `report_md` 脱离 JSON 包裹；`sas_candidates` 拆成独立 stage `sas_candidate_extract`；六个 `google/gemma-4-31b-it` stage 统一锁定 provider 为 OpenInference（allow_fallbacks）；详见第八节和文末变更记录。另含 2026-08-04 之前的 `report_pass1`/`am_calibration` 切换至 `google/gemma-4-31b-it`，issue #59/PR #61）

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
┌─────────────────────────────────────────────────────────────┐
│                     每日定时任务（launchd）                    │
│   5:30 AM PT = 8:30 AM ET  开盘前简报                        │
│   5:10 PM PT = 20:10 ET    夜盘动向（NYSE 盘后结束后 10 分钟）│
└───────────────────────┬─────────────────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │   run_finance.py   │  主入口
              │   NYSE 交易日检查   │
              │   防重（月度文件）  │
              └─────────┬──────────┘
                        │
      ┌─────────────────┼──────────────────────┐
      ▼                 ▼                       ▼
fetch_prices        fetch_news            memory_context
三层路由：           14 RSS + Guardian     (bridge REST)
yfinance（主）       API（14源）           MemPalace
→ IBKR gateway       NYT/BBC/FT/CNBC      Layer B 持仓框架
→ Finnhub（fallback） Reuters/AP/WSJ等
价格+异动检测
      │                 │                       │
      └─────────────────┼───────────────────────┘
                        │ 汇总输入
              ┌─────────▼──────────┐
              │  step 6: Finnhub   │  即时新闻（免费无配额）
              │  step 6c: Sonar    │  宏观快照（~$0.005）
              └─────────┬──────────┘
                        │
              ┌─────────▼──────────┐
              │  代码层 skip 检查   │  无异动+无geo → 退出
              └─────────┬──────────┘
                        │
              ┌─────────▼──────────┐
              │  LLM Pass 1        │
              │  deepseek/v4-flash │
              │  via OR/Novita     │
              │  事实梳理+生成      │
              │  tavily_queries[]  │
              └─────────┬──────────┘
                        │
              ┌─────────▼──────────┐
              │  Tavily/SerpApi    │  顺序执行，budget 预检
              │  anomaly 优先      │  全部 basic + Extract 全文
              │  Extract 层（3层） │  score→filter→semantic→extract
              └─────────┬──────────┘
                        │
              ┌─────────▼──────────┐
              │  LLM Pass 2        │  有 Tavily 数据时必跑
              │  deepseek/v4-pro   │  thinking=enabled（3000 tokens）
              │  via OR/Novita     │
              │  Layer A: SYSTEM_  │  平台通用投资原则（Layer_A_Prompt.md）
              │  PROMPT_P2         │
              │  Layer B: 持仓均价 │  个人上下文注入 user message
              │  + 投资框架        │
              └─────────┬──────────┘
                        │
      ┌─────────────────┼──────────────────────┐
      ▼                 ▼                       ▼
Obsidian月度         MemPalace              邮件+Telegram
文件 append          per-day drawer         推送通知
                                            （Markdown→HTML，
                                             超4096字自动分段）
```

```
┌─────────────────────────────────────────────────────────────┐
│              Telegram Bot（常驻 long polling）               │
│              独立 bot token，与宿主 Agent 隔离               │
└───────────────────────┬─────────────────────────────────────┘
                        │ 用户发消息 → 立即回"收到，处理中..."
              ┌─────────▼──────────┐
              │  Step 1            │
              │  V4 Flash 统一预处理│
              │  via OR/Novita     │
              │  意图分类+搜索词   │
              │  +tickers+框架考量 │
              └─────────┬──────────┘
                        │
          ┌─────────────┼─────────────────────────┐
          ▼             ▼                          ▼
    指令执行        状态报告                  自然语言追问
  (改watchlist)   (系统状态)               ┌──────────────┐
                                           │  Step 2      │
                                           │  yfinance    │
                                           │  实时行情+新闻│
                                           │  （free）    │
                                           └──────┬───────┘
                                                  │
                                           ┌──────▼───────┐
                                           │  Step 3      │
                                           │  Parallel.ai │
                                           │  search+     │
                                           │  extract（主）│
                                           │  → Sonar     │
                                           │  → Exa（fallback）
                                           └──────┬───────┘
                                                  │
                                           ┌──────▼───────┐
                                           │  Step 4      │
                                           │  V4 Flash    │
                                           │  via OR/Novita│
                                           │  持仓框架     │
                                           │  +MemPalace  │
                                           │  个人化推理   │
                                           │  fallback:   │
                                           │  Grok 4.3    │
                                           └──────┬───────┘
                                                  │
                                           月度文件 append
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
| `pm` | 日线（今收）+ 1m prepost=True（盘后） | 今日收盘价 | vs 昨收 | 日内超阈值 OR 盘后超阈值 |
| `daily` | 日线（兜底） | 最新日线收盘 | vs 前一日收盘 | 日内涨跌超阈值 |

`PriceRow` 新增字段：`afterhours_price`、`afterhours_pct`、`slot`。`format_price_table(slot)` 按 slot 输出不同列：AM 显示「盘前价/盘前涨跌（vs昨收）」，PM 增加「盘后涨跌」列。

yfinance 失败时 fallback 到 Finnhub `/api/v1/quote`（免费60 req/min）；Finnhub 无盘前/盘后数据，记 warning，返回日线等价数据。商品/FX（`GC=F`、`^TNX`、汇率等）在 Finnhub 免费 tier 无数据则跳过并记录。

**IBKR Client Portal Gateway（`ibkr/quotes.py`，隔夜/周末实时数据源）**：IBKR 自有 REST API，通过本地 gateway 代理，覆盖 ATS/OTC 隔夜时段和周末场外报价——这是 yfinance/Polygon 免费 tier 均无法覆盖的窗口。

**`_fetch_realtime_prices()` 三层路由策略（`telegram_commands.py`）：**

| 时段 | 主力 | Fallback 1 | Fallback 2 | 非实时标注 |
|---|---|---|---|
| 工作日 04:00-20:00 ET | yfinance Ticker.info（session-aware） | IBKR gateway | Finnhub | 无 |
| 隔夜 20:00-03:50 ET + 周末 | **IBKR gateway** | yfinance | Finnhub | yfinance/Finnhub 均标注"非实时" |

yfinance 字段按时段选择：`preMarketPrice`（04:00-09:29）/ `regularMarketPrice`（09:30-15:59）/ `postMarketPrice`（16:00-19:59，基准用今收）。`fast_info.last_price` 和 `yf.download(prepost=True)` 均只返回常规收盘，不含盘前/盘后，不可用。

`# FUTURE`：待 IBKR gateway 稳定后翻转优先级，改为 IBKR 全时段主力。代码中已用注释标出两处修改点。

**新闻（RSS 14个源 + Guardian API）**：

| 源 | 定位 |
|---|---|
| NYT Business/World/Politics | 财经综合 |
| BBC Business/World | 财经/国际 |
| FT World | 专业财经 |
| CNBC | 快速财经/市场速报 |
| MarketWatch | 市场数据驱动 |
| Foreign Policy | 地缘战略深度 |
| Al Jazeera | 中东/非西方视角 |
| Seeking Alpha | 个股机构分析 |
| Reuters（via Google News RSS） | 综合/财经，<1h 延迟 |
| AP（via Google News RSS） | 综合新闻 |
| WSJ（via Google News RSS） | 专业财经 |
| Guardian API | 国际/财经/政治，结构化 JSON，20条/次 |

过去 24h，按地缘政治关键词分类。Guardian API（`content.guardianapis.com/search`）fail-open，`GUARDIAN_API_KEY` 控制，结果合并进 RSS 统一时间排序。不可达：Politico（403）；Reuters/AP/WSJ 直连受阻，已通过 Google News 代理覆盖。

**Sonar 宏观快照（step 6c，AM + PM）**：`_sonar_macro_brief(slot, stocks, commodities, fx, geo_topics, now_et, portfolio_snapshot, price_table)` 调用 `perplexity/sonar`，query 从 watchlist 动态构建，随持仓变化自动演化。

| slot | 查询聚焦 |
|---|---|
| AM | 过去 12h 隔夜发展，对今日开盘有何影响 |
| PM | 当日盘面驱动因素 + 盘后/隔夜风险 |

system prompt 注入 portfolio 快照实现个人化。~$0.005/次，fail-open，注入 Pass 1 和 Pass 2 两个 LLM prompt。

**防过时/防幻觉加固（issue #24，2026-07-02）**：Sonar 是搜索+合成模型，不是行情 feed，曾在同一份报告中与实时价格直接矛盾（声称 WTI 破 $100，实际价格 $68.58）。三重加固：① OR payload 加 `search_recency_filter: "day"`（实测确认 OpenRouter 会透传给 Perplexity，不会被静默丢弃），限制底层搜索只召回过去24小时发布的源；② 把 pipeline 中已经算好的 `price_table`（fetch_prices 输出）注入 system prompt 作为权威真实数据，要求若搜索结果与之冲突则以注入价格为准并明确标注冲突；③ prompt 要求每条具体断言必须带时间戳，若某话题无近 24 小时更新必须明说，不得拿旧信息冒充当前。`telegram_commands.py` 的 `_sonar_research()`（TG 追问流水线的 Sonar fallback，同模型同风险）同步加了 `search_recency_filter`。

**Finnhub 即时新闻（step 6b，常态注入）**：`fetch_finnhub_news()` 对 watchlist 股票（异动标的优先，最多8个）调 Finnhub `/company-news`，时间窗口 `min(query_days×24, 48)h`，注入 Pass 1/Pass 2 prompt 的 RSS 与 Tavily 之间。免费，无配额，专注 ticker 级公司新闻，补充 RSS 的宏观视角。fail-open，单 ticker 失败不阻断整体。

**FRED 流动性水位快照（step 6e，AM+PM，issue #26，2026-07-02）**：`fetch_liquidity_snapshot()` 拉取银行准备金（`WRESBAL`）、SOFR（`SOFR`）、ON RRP 授予利率（`RRPONTSYAWARD`，注意不是 `RRPONTSYD`——后者是隔多逆回购**交易量**不是利率，实测数值差异巨大才发现搭错）、TGA余额（`WTREGEN`），按 `Hermes/Daily Intelligence/市场见顶预警指标.md` 的阈值分类【正常/观察/警戒】，整体取最高档，折进现有 `social_sentiment_section` 注入槽（不新增模板变量）。SRF用量 FRED 无对应序列，不自动化，留作文档里的人工检查项。选型理由：FRED 是比 Sonar 搜索更可靠的精确数据源（呼应 issue #24 的教训——LLM 搜索对精确数值不可靠，能用结构化权威数据源就不该靠 LLM 猜）。Pass 2 prompt 新增分析要求第⑥条，约束 LLM 只能给出与档位匹配的克制建议，不得因此单独触发清仓建议。

### 5.1b 情报拉取架构演化：Search+Extract 三层设计

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

**Haiku 语义过滤的非显然价値**：Haiku prompt 要求考虑上下游供应链和宏观传导，而非仅 ticker 名字命中。例：TSMC 产能收缩新闻即使不提 INTC，也与 INTC 高度相关。纯脚本关键词匹配覆盖不到这类语义关联。

当前实现：两阶层评分——脚本预筛选（`score_and_filter`，15条）+ DeepSeek V4 Flash 语义排序（`_haiku_relevance_filter`，函数名保留，模型 `deepseek/deepseek-v4-flash` via OR/Novita），识别上下游供应链、宏观传导渠道，而非仅匹配 ticker 名称。

#### 三层流程设计

```
Layer 1 — Discovery（basic search × 2-4，每条 1cr）
  全部为 basic（不再使用 advanced，Extract 来补深度）
  合并原始结果 raw_results（20-60 条）

Layer 2a — Script pre-screen（纯脚本，0cr）
  score_and_filter: N条 → 15条，去重 + 综合评分
  小于 1ms，无额外成本

Layer 2b — DeepSeek V4 Flash 语义过滤（DeepSeek直连，~$0.000035/次）
  _haiku_relevance_filter: 15条 → 10条（函数名保留以减少改动范围）
  识别直接催化剂、上下游供应链、宏观传导渠道
  不仅匹配 ticker 名称，语义理解业务影响
  thinking: disabled；fail-open：失败则回退到 script top-10

Layer 3 — Extract（1cr/5 URLs，10 URLs = 2cr）
  tavily_extract(10 URLs, query, chunks_per_source=2)
  每个 URL 返回 2 个高相关 chunk（600字/chunk，基于 query 对齐）
  Pass 2 LLM 拿到完整正文而非摘要
  计费公式：1cr/5 URLs，应以 5 的倍数提交（5/10）

Layer 3.5 — 信源置信度打标（issue #19，2026-06-30）
  每条 Extract 结果附加 [信源类型 | 发布时间 | 交叉印证] 标签行：
    信源类型：_detect_low_structure() 识别视频聚合页/caption堆叠（无独立时间戳，谨慎）
    发布时间：_lookup_published_date() 从 extract 前的 search 结果池按 URL 反查
              （Tavily /extract 响应本身不带日期字段，只有 /search 有）
    交叉印证：_compute_corroboration() 规则式事实指纹匹配（专有名词短语+日期/数字token），
              统计候选池中有多少个其他独立域名与本条内容重叠——零 API/LLM 成本的启发式，
              存在假阴性，0 不代表"确认单一信源"而是"本规则未找到重叠"
  标签同时写入 Pass 2 prompt（LLM 参考）和本地 Extract Archive（审计留痕）
  背景：Reuters 视频聚合页孤立 caption（无时间戳）曾被 Pass 2 当作确定事实写入报告
  （"签署仪式定于周五"，用户核实后其他信源查无此消息）
```

**语义过滤设计细节（Layer 2b）：**

输入：15 条摘要（每条 title + URL[:70] + snippet[:130]）+ 异动 ticker + geo 主题 + 持仓 ticker。
输出：JSON 数组 [i1, i2, ..., i10]，按相关性排序。
总 token：~300-500 input + ~40 output = **~$0.000035/次**（DeepSeek 直连，原 Haiku/Bedrock 约 $0.0021，降低 60 倍）。

判断标准（优先级递减）：
1. 直接催化剂（财报、交易、监管行动）
2. 上下游供应链（上游元件提供商、下游 OEM 客户、代工厂）
3. 行业性监管/出口管制（直接解释异动原因）
4. 地缘事件对市场的可量化传导（制裁、冲突升级）
5. 宏观信号与持仓暴露相关（联储/利率/汇率/商品供应冲击）

**budget 触发规则：**

| 余额 | 行为 |
|---|---|
| ≥ 3cr | 完整三层流程 |
| = 2cr | 跳过 Extract，仅用 filtered 搜索摘要 |
| = 1cr | 仅跑 1 次 basic search |
| = 0cr | 跳过搜索，继续生成 Pass 1 基础报告 |

**信用消耗对比：**

| 场景 | 旧流程 | 新流程 |
|---|---|---|
| AM 有异动 | 1 advanced(2) + 3 basic(3) = **5cr** | 4 basic(4) + 1 extract(2) = **6cr**，但全文 |
| PM 有异动 | 4 basic(4) = **4cr** | 3 basic(3) + 1 extract(2) = **5cr**，Finnhub 已覆盖异动层 |
| 仅 geo，无异动 | 3 basic(3) = **3cr** | 2 basic(2) + 1 extract(2) = **4cr** |

在 20cr/日预算下，全天 AM+PM 总消耗 ≈ 11cr，余量充足。

---

### 5.2 LLM 分析

**代码层 skip（LLM 调用前）**：
```python
triggered_geo_topics = sorted(set(t for item in news_items for t in item.topics))
if not anomalies and not triggered_geo_topics:
    sys.exit(0)  # 零 LLM 成本
```

**Pass 1（必须）**：
```json
输入：价格表 + 新闻 + KB上下文 + triggered_geo_topics + query_days
输出：{
  "report_md": "报告草稿",
  "tavily_queries": [
    {"query": "NVDA H200 export ban May 2026", "search_depth": "advanced", "days": 1, "max_results": 12},
    {"query": "Fed tariff recession signal", "search_depth": "basic", "days": 1, "max_results": 12}
  ]
}
```
- `triggered_geo_topics` 注入 prompt，LLM 只为命中主题生成查询，未命中不生成
- `query_days = max(1, min(3, 距上次报告天数))`，周末 / 节假日后自动扩展窗口

**搜索（条件触发）**：
- 执行顺序：代码生成的异动查询（优先）→ LLM 建议查询 → 核心持仓认知提升轮询查询（最后，issue #33）
- **认知提升轮询查询**（`_rotation_search_job()`，2026-07-08 issue #33）：现有搜索完全由异动/地缘触发，核心持仓（AMKR/INTC/NVDA/QCOM/TSLA）没异动的日子完全不会被主动查。新增每日按 `date.toordinal() % 标的数` 确定性轮询选中一个标的（无需状态文件），生成聚焦 Investment Operating Manual 第6节认知提升三条标准的查询，30天窗口，追加在其他查询之后——预算耗尽就自然被跳过，不抢占真实异动/地缘信号的额度。
- search_depth：AM 异动查询用 advanced（2 credits）；PM slot 所有查询强制 basic（1 credit）；LLM 建议：异动 ticker advanced，地缘/宏观 basic
- 每次调用前预检 budget_remaining ≥ credits_needed，不足则停止循环
- max_results=12（原 8）
- Tavily 断连自动 fallback SerpApi；两者均耗尽则跳过搜索继续生成基础报告

**Pass 2（有搜索结果时）**：使用 `openai/gpt-5.6-luna`（非pro，issue #60，此前依次是 `deepseek-v4-flash` → `deepseek-v4-pro`）合并 Tavily 结果生成最终报告，`report_md` 直接输出裸 markdown（不再是 JSON 字段，见第八节）。

**错误韧性**：两个 pass 的 LLM 调用（`call_llm()`）在遇到网络/5xx 错误时自动重试 2 次（指数退避 2s/4s），4xx 和 JSON 解析错误不重试。`telegram_commands.py` 中 DeepSeek 调用通过 `_deepseek_post()` 直连，Claude/Sonar 调用通过 `_openrouter_post()` 走 OR，均使用相同重试策略（网络/5xx 自动重试 2 次）。

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
| Obsidian 月度 Context Log `Daily_Intel_context_YYYYMM.md` | append section（step 11b）：价格快照 + 触发新闻标题 + Sonar 宏观原文 + 搜索任务列表 | 是 |
| Extract Archive `~/Daily_Intelligence/archives/YYYYMM/YYYY-MM-DD-{slot}-extract.md` | Tavily Extract 清洗全文（< 60字短行已剥离）+ Layer 2b 候选列表（step 9b） | 否（Obsidian 之外） |
| MemPalace per-day drawer | report_md 推送 bridge，wing=paperview, room=finance | — |
| 邮件 | Gmail API（send+readonly scope） | — |
| Telegram | Markdown → HTML，超 4096 字符自动分段 | — |
| TG 独立运行状态消息 | `build_status_message()`（step 13b）：Tavily/SerpApi 本次用量+剩余、情报源状态（RSS/Guardian/Finnhub/Sonar/Tavily搜索+Extract）、LLM/Provider 清单；与正文分开发送，不进邮件/Obsidian | 否 |

**Context Log 与 Extract Archive 的设计分工：**
- Context Log 存 Obsidian → MemPalace 矿化后可语义检索"某日早上市场context是什么"
- Extract Archive 存本地 → 不污染矿化索引，用于原始情报审计和中期回顾，建议保留 6 个月
- Extract 全文刻意不进 Obsidian：原始网页抓取含导航/广告碎片，矿化会产生大量低质量向量

### 5.5 AM 预判校准闭环（issue #10，2026-07-02）

**定位**：把原本"盘后对比版本"的设想改造成闭环学习机制——AM 报告输出可验证信号，PM 报告校验并沉淀为知识，知识反过来影响未来的 AM。不新增调度任务，折进现有 PM pipeline（PM 已在盘后跑，已经算好 EOD 价格表）。

**AM 报告新增"可验证信号"清单**：`USER_PROMPT_TEMPLATE`（Pass 1）和 `USER_PROMPT_TEMPLATE_P2`（Pass 2）新增条件性指令常量 `VERIFIABLE_SIGNALS_INSTRUCTION_P1`/`_P2`，通过模板变量 `{verifiable_signals_rule}` 注入，仅 `run_slot=="am"` 生效。要求报告结尾固定追加"## 可验证信号"小节，2-4条条件-结果式可核验断言（如"WTI跌破$65→通胀预期继续下修"），不写模糊定性描述。报告主体的自由叙事写法不受影响（呼应 05-21"格式硬约束压制LLM深度"的教训，见踩坑记录#49）。

**PM 校验步骤**：新函数 `evaluate_am_calibration()`，插在报告标题修正后、`write_report()` 之前，仅 PM slot 执行：
1. `_extract_report_section()` 定位当天 AM 报告 section——以下一个日期戳 `## YYYY-MM-DD` 为边界，不被报告内部的 `## 子标题`/`---`分隔符误判（复用 2026-05-04 修复 KG section 截断 bug 时确立的模式，见踩坑记录#25）
2. `_extract_verifiable_signals()` 提取"可验证信号"小节内容
3. `_evaluate_am_predictions()`：一次 LLM 调用（`llm_config.py` stage `am_calibration`，默认 `google/gemma-4-31b-it`，~$0.0005；issue #59 前曾复用 `report_pass1` stage 的 DeepSeek V4 Flash，2026-08-03 实测该模型隐式推理吃满预算导致 3/3 真实调用失败，PR #61 拆成独立 stage 并换模型），对照当日实际价格表+新闻上下文（Finnhub+Sonar），逐条判定 hit/miss/inconclusive，提炼一段"知识条目"（不是罗列对错，是可迁移的教训或验证），并判断是否值得展示
4. 若当天 AM 报告没有该小节（历史报告、或该步骤本身失败），静默跳过，不影响主流程——整个函数 fail-open

**知识沉淀与备份（2026-07-02 修正：Obsidian 为主，不依赖 MemPalace）**：`_write_calibration_knowledge()` 写三份：
1. **Obsidian**（源之真实）：追加写入 `Hermes/Daily Intelligence/预判校准记录.md`，一段话式的教训记录，不是数据行
2. **本地备份镜像**：`backups/预判校准记录_backup.md`（项目目录下，已加入 .gitignore，不进代码仓库），与 Obsidian 独立写入相同内容，防 Obsidian 侧丢失（sync 冲突、误删）
3. **MemPalace drawer**（`room=finance`，锰上添花）：仅作为语义检索的可选增强层，不是任何环节的必需依赖

**为什么不依赖 MemPalace**（用户 2026-07-02 提出）：最初设计假设"AM 能通过现有 `get_finance_context()` 的 MemPalace 搜索自动捕到校准知识"——这是个未经验证的假设，那个搜索是通用 query，不是针对校准知识专门设计的，而且对用户描述的"MemPalace finance room 最近已多次全部重建"这种故障零容错。已改为 `_load_recent_calibration_notes()` 直接读 Obsidian——不经 bridge、不经 MemPalace，若 Obsidian 文件缺失/不可读自动 fallback 到本地备份镜像。注入 AM prompt（Pass 1/2）新模板变量 `{calibration_notes}`，仅 AM slot 生效，取最近 5 条。

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
Step 1  google/gemma-4-31b-it 统一预处理（~$0.0001，stage `tg_preprocess`，issue #11）
        → 意图分类 + 精准英文搜索词 + relevant_tickers + 框架考量
        注：非 deepseek-v4-flash——2026-07-25 用本 stage 真实 prompt 实测，temperature=0
            下同一条简单指令（"删关键词 US-Iran blockade"）连续3次调用给出3种不同的错误结果
            （枚举外的幻觉 action 值 / 预算耗尽 content 全空 / JSON 从中间截断），gemma 在5种
            指令类型上全部正确复现，零 reasoning token

Step 2  实时行情（三层路由）+ yfinance.news（免费，无配额）
        → _fetch_realtime_prices()：按 ET 时段路由数据源
            工作日 04:00-20:00 ET：yfinance Ticker.info（主）→ IBKR → Finnhub
            隔夜/周末：IBKR gateway（主，ATS/OTC 真实报价）→ yfinance（"非实时"）→ Finnhub（"非实时"）
        → _fetch_yfinance_news()：过去48h内的相关新闻标题+时间（ET）+来源
        → 各有 Finnhub fallback（常规时段价格 + company-news，免费60 req/min）
        进度提示："获取实时行情及新闻（TICKER）..."

Step 3  Parallel.ai search + extract（主，~$0.007-$0.012）
        → _parallel_research(queries) → (brief, failure_kind|None, detail)：
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
            issue #7 成本第一杠杆：dashboard credit < $5 时设 PARALLEL_P1_ENABLED=0，
            只关 P1 补漏查询，主 Parallel 全文路径保持开启。
        → 产品原则：Parallel 信息密度高于 Sonar、费用仅略高，有 key 就优先走 Parallel；
          不因本地估算月度用量而提前改用 Sonar。
        → 观测：finance_parallel_budget.json 记录 search/extract 估算成本（状态消息展示），
          默认不做硬帽；PARALLEL_MONTHLY_BUDGET_USD>0 才启用可选灾难刹车。
        → 降级条件：
            ① Parallel 真实拒绝服务（余额不足/鉴权失败，含 401/402/403 与 billing 语义错误）
               → 单独发一条 TG notice（6h 冷却防刷）→ Sonar/Exa
            ② 可选硬帽触发 → 同样 TG notice → Sonar/Exa
            ③ 其它技术失败/空结果 → fail-open 静默 Sonar，不发「余额不足」notice
        → 原始全文直接传入 Step 4，无预摘要损耗
        → 失败 fallback：Sonar（重试1次→Exa model="exa"）
        进度提示："情报检索（N条查询）..."

Step 4  openai/gpt-5.6-luna（非pro）via OR（主，stage `tg_followup`，issue #60）
        System: 投资框架（module-level cache）+ NYSE 时区推理要求
                + 禁止对话体开场白（直接进入分析）
        User:   当前时刻(EDT/EST) + yfinance实时行情（价格基准） + yfinance.news
                + Parallel.ai原文情报 + 持仓快照（均价，非现价）+ MemPalace
                + question_intent
                + 推理规则：以yfinance为价格唯一基准；无新催化剂直接声明动量延续
        max_tokens=16000；reasoning={"effort":"high"}（OpenAI统一参数，issue #60，取代
        DeepSeek的thinking.budget_tokens——后者实测是软上限而非强制上限，见下方issue #60记录）；
        provider锁定{"order":["OpenAI"],"allow_fallbacks":false}；自管3次重试（timeout 180s）
        fallback: x-ai/grok-4.5（via OR，reasoning.effort=medium，max_tokens 8000）
        content 为空且 finish_reason=length 时报"预算耗尽"而非崩溃（issue #53 的崩溃形态）
        输出：自由展开分析（不设字数上限，参考维度：驱动力/持仓含义/待验证信号/信息缺口）
        注：Step 4 绕过 _deepseek_post()，自管重试确保 model_label 精确、Grok fallback 正确触发
        注：provider 路由与全部模型参数取自 llm_config stage `tg_followup`（issue #11），
            可由 llm_config.json（git 追踪）运行时覆盖；bot 按文件 mtime 自动重载，无需重启

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

**AM/PM 日报**（`run_finance.py::_load_framework()`，2026-07-08 起，issue #30）：改从 `Finance/Investment Operating Manual v1.0.md` 提取三段运行性规则——第2节能力边界、第6节 Portfolio Construction（含认知提升标准/减仓触发情形，2026-07-09 issue #34 起不再用字母代号标注）、第7.4节 Expectation Gap 内部信号清单——按标题正则定位，Manual 编辑后自动同步无需改代码。与 `_get_portfolio_snapshot()` 一同注入 **user message**（Layer B，非 system message）。Pass 2 prompt 同步新增以下分析要求（均为描述性小标题，不用编号，issue #34 一并把互相引用改为内联复述）：能力圈内外标注（圈外驱动因素须显式标注“不构成操作依据”），持仓异动核对（唯一允许给出加减仓建议的依据来源，对照认知提升/减仓具体标准逐条核对，不满足则明确声明不构成依据）。SAS候选证据标注（命中7.4内部信号/认知提升标准，见 issue #31）自 issue #60 起不再是 Pass 2 prompt 里的一条规则，而是拆成独立的 `sas_candidate_extract` stage（`google/gemma-4-31b-it`），复用 Pass 2 组装好的同一份价格/新闻/持仓上下文单独调用一次，输出 `sas_candidates` 数组——原因是 report_md 曾与 sas_candidates 共享同一个 JSON 信封，一次截断会把已经写好的整份报告一并作废（report_md 是全项目最大的单次 payload，也是撞过 `finish_reason=length` 的两个 stage 之一，见下方 issue #59/#60 记录），拆开后 report_md 直接输出裸 markdown（不再是 JSON 字段）。不自动计算 SAS 分数（仍为人工季度任务，见 issue #32）。

### 7.1b 持仓计算信号（user message，纯计算，零LLM/搜索成本，issue #33）

`_compute_holding_signals()` 将两项计算结果注入 Layer B，与持仓快照、投资框架并列：
- **52周区间百分位+距历史高点回撤**（`fetch_prices.py::fetch_52week_stats()`，yfinance period="1y"），对应 Manual 7.4 节"股价相对位置"信号，代码算好不让 LLM 从文本自行估算
- **持仓占组合%**（`_get_portfolio_weights()`，市值÷组合总USD市值），对应 Manual 第6节减仓条件C（>15%），⑦号规则直接读取这个计算值判断，不再让 LLM 自己从持仓快照文本估算百分比
- 适用范围仅限核心主动个股（排除 QQQM/VOO/EWJ/SGOL/BOXX/CASH），与 Manual 第1节三层结构对齐

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

**选型不再硬编码在各脚本里（issue #11，2026-07-25）**：下表的模型、provider 路由、thinking 预算、max_tokens、temperature 全部来自 `scripts/llm_config.py` 的 stage 定义，可由项目根目录的 `llm_config.json`（**git 追踪，非 gitignore**——最初照搬 `tg_offset.json` 那类运行时状态文件的套路做成了 gitignore，后来意识到这是人手改的、有意图的配置决策，跟 `watchlist.md` 是同一类东西而非机器写的临时状态，且不含任何敏感信息，没理由不入库；追踪进 git 不影响"改了立即生效不用走 PR"这条特性——那是 loader 每次读文件决定的，git 只是白得一份可追溯的修改历史）在运行时覆盖，无需改代码/走 PR。`llm_config.py` 内置的 DEFAULTS 即下表内容，也是唯一的最终兜底：配置文件缺失、JSON 损坏、字段类型/取值非法时逐字段回退到默认值并记日志，不会让流水线崩掉。每一处生效的覆盖在加载时记 INFO 日志（`LLM config override: <stage>.<field>: old -> new`）——git log 能看出改了什么、什么时候提交，但看不出某个具体进程运行时是否真的读到了这次改动，INFO 日志补的是这一层。仓库内 `llm_config.example.json` 是 schema 与默认值的说明性模板（有测试断言它与 DEFAULTS 完全一致）。

stage 名与调用点对应：`report_pass1` / `am_calibration` / `report_pass2` / `sas_candidate_extract` / `semantic_filter` / `macro_brief` / `tg_preprocess` / `tg_gap_detect` / `tg_research` / `tg_followup`。

| #   | 调用位置 | 用途 | 主力模型 | Fallback | max_tokens | 成本估算 |
| --- | --- | --- | --- | --- | --- | --- |
| 1   | `run_finance.py` Pass 1（stage `report_pass1`） | 报告草稿 + 生成 tavily_queries | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #59/#60——原 `deepseek-v4-flash` 在 2026-08-03 真实生产两次故障，reasoning 隐式吃满预算导致 `finish_reason=length`） | `google/gemini-3.1-flash-lite` OR flex | 4000 | ~$0.001 |
| 1b  | `calibration.py::_evaluate_am_predictions()`（stage `am_calibration`） | PM slot：对照实际数据核验 AM「可验证信号」，产出知识条目 | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #59/#60——从 `report_pass1` stage 拆分为独立 stage，避免未来调 report_pass1 预算/模型时静默影响这个无关的判断） | `google/gemini-3.1-flash-lite` OR flex | 4000 | ~$0.0005 |
| 2   | `run_finance.py` Pass 2 | 整合 Tavily 结果生成最终报告 | `openai/gpt-5.6-luna`（非pro）via OR/OpenAI（provider锁定，不允许fallback到其他provider；`reasoning={"effort":"high"}`，issue #60——原 `deepseek-v4-pro+thinking` 真实数据对比暴露自相矛盾判断，见文末记录） | `google/gemini-3.5-flash` OR flex | 16000 | ~$0.02（未核实精确单价） |
| 2b  | `run_finance.py` Pass 2 后（stage `sas_candidate_extract`，issue #60） | 独立提取 SAS 候选证据（原是 Pass 2 JSON 的一个字段，见上文"SAS候选证据标注"说明） | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #60，9/9 真实对抗测试验证） | `google/gemini-3.1-flash-lite` OR flex | 800 | ~$0.0003 |
| 3   | `run_finance.py` Layer 2b（stage `semantic_filter`） | 语义过滤 15→10 条搜索结果 | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #53/PR #54/#60） | `google/gemini-3.1-flash-lite` OR flex | 200 | ~$0.0001 |
| 4   | `intel_sources.py` step 6c（stage `macro_brief`） | Sonar 宏观快照（AM/PM 各一次） | `perplexity/sonar`（OR，`search_recency_filter="day"`，2026-07-02 加，见 issue #24） | 重试1次(5s) → `””` 空节 | 1500（issue #55 由 800 提高） | ~$0.005（含固定搜索费） |
| 5   | `telegram_commands.py` Step 1（stage `tg_preprocess`） | 统一预处理：意图分类 + 2条 query 生成 | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #11/#60） | `google/gemini-3.1-flash-lite` OR flex | 600 | ~$0.0001 |
| 6   | `telegram_commands.py` Step 3 | 追问原文情报（2条 query + P1 可选第3条 + 3 URL extract，P2 聚合 URL 优先） | Parallel.ai SDK `parallel-web==0.4.2` | Sonar（重试1次→Exa） | — | ~$0.007（无P1）/ ~$0.012（P1触发） |
| 6b  | `telegram_commands.py` Step 3 P1（stage `tg_gap_detect`） | gap detection：是否需要第3条 query | `google/gemma-4-31b-it`（OR，provider锁定OpenInference+allow_fallbacks，issue #11/#60——原 deepseek-v4-flash 在此 prompt 上实测烧光60-token预算于隐藏推理，功能实际从未跑成功过） | fail-open（不触发即跳过） | 60 | ~$0.0001 |
| 7   | `telegram_commands.py` Step 4（stage `tg_followup`） | 个人化推理 | `openai/gpt-5.6-luna`（非pro）via OR/OpenAI（provider锁定，不允许fallback到其他provider；`reasoning={"effort":"high"}`，issue #60——原 `deepseek-v4-flash+thinking` 实测同样会无视 `budget_tokens` 软上限烧穿 `max_tokens`，7/7 真实调用验证新配置） | `x-ai/grok-4.5`（OR，`reasoning={"effort":"medium"}`，max_tokens 8000） | 16000 | ~$0.01（模型换成本结构不同，未核实精确单价） |
| 8   | `sas_review.py`（季度手动/自动触发，issue #32） | 直接打 SAS 四维度分（Strategic Space/Execution/Expectation Gap/Alpha Potential） | `~anthropic/claude-sonnet-latest`（OR） | 无（v1 故意不接，观察实际效果后再评估） | 4000 | ~$0.05（OR `usage.cost` 实际读取，无硬编码价格表） |

**OR provider 实测结论（2026-06-02）：**
- **V4 Flash**：DigitalOcean（FP16，高精度）、Venice（US 机房，全精度，实测可用）、StreamLake（OR 默认路由，精度未知，已在 OR 账户排除）可用。NovitaAI 不再服务 V4 Flash。
- **V4 Pro**：Together、Fireworks 可用，必须传 `thinking:enabled+budget_tokens`，否则 content=None。DigitalOcean 不服务 V4 Pro。
- **thinking 参数兼容性**：Flash 不得传 `thinking:disabled`（StreamLake fallback 时会导致 content=None）；Pro 必须传 `thinking:enabled`。
- **Flash 开 thinking 的适用边界（issue #11，2026-07-25）**：TG 追问 Step 4 显式开 `thinking:enabled`（budget 3000）——不开时开放式持仓推理的答案质量明显更差。但这只对"输出预算充裕"的调用成立：issue #53 的生产崩溃正是 `max_tokens=80` 的判别式小任务被 reasoning token 吃光预算、`content` 返回 null。因此 Step 4（12000）与 gap detection（60）在 `llm_config.py` 中是两个独立 stage，互不共享 thinking 设置；Step 4 的响应解析也不再直接 `.strip()` content，`content` 为空且 `finish_reason=length` 时给出明确的"预算耗尽"提示而非抛异常。
- **判别式/分类式小任务优先用天生无 reasoning 开关的模型，而非"关掉 thinking 的 DeepSeek"（issue #11，2026-07-25，参考 Obsidian `Hermes/Homepage/LLM-No-Reasoning-eval设计与实现.md`）**：`deepseek-v4-flash` 未显式传 `thinking` key 时，OpenRouter 侧默认值是否为 enabled 不可控，且同一 prompt 反复调用的隐藏推理量本身不稳定——`tg_gap_detect`（60-token 预算）和 `tg_preprocess`（意图分类+字段抽取）两处实测均复现出真实故障（前者预算耗尽返回 null content；后者 temperature=0 下同一条指令 3 次调用给出 3 种不同错误结果，含枚举外的幻觉 action 值）。`google/gemma-4-31b-it` 在同一 prompt 上零 reasoning token、输出稳定，已在跨项目题库（`LLM-No-Reasoning-eval`，21 case × n=10 全量测试 100% 通过）验证为这类"精确抽取/分类/格式服从"任务的默认推荐，不需要每个项目各自重新测。已知代价：`tg_preprocess` 的 `relevant_tickers` 字段在跨标的关联问题上比 deepseek 曾经给出的结果更保守（如"高通被制裁"只标 QCOM，不主动带出 INTC/NVDA），但 deepseek 自己在同一测试里也未能稳定给出这个"更丰富"的结果，不能算可靠优势。
- **`report_pass1`/`am_calibration` 同样属于该模式，只是发现得晚（issue #59，2026-08-03/04）**：两个 stage 均是"结构化生成+判断"任务而非开放式推理，此前误以为"不传 thinking key 就默认不推理"，2026-08-03 两次真实生产故障（AM 主报告、PM 校准分别 2/3、3/3 调用被 reasoning 吃满 `max_tokens`）证伪了这个假设。切至 `google/gemma-4-31b-it` 后用同日真实数据重建两种 prompt 形状验证 6/6 通过（`deepseek-v4-flash` 对照组同条件下 4/4 复现故障），PR #61 实现。同批核查发现 `tg_followup`（唯一保留 `deepseek-v4-flash` 的 stage，因需要真实开放式推理）在压力测试下也会无视 `thinking.budget_tokens` 软上限（1/3 次烧穿 `max_tokens`），因已有 fallback 兜底且真实生产 0 次失败，未与 report_pass1 同批处理，另开 issue #60 跟踪；候选 `openai/gpt-5.6-luna`（非pro）+`reasoning.effort=high` 已验证 7/7 可行但未实施（需要代码改动，非纯配置切换）。

**注：**
- #6 Parallel.ai SDK `parallel-web==0.4.2`（Hermes 容器版本），DI 宿主机使用同一版本；search 返回 WebSearchResult，extract 返回 ExtractResult；dedup → P2 聚合 URL 排序 → extract top 3；每篇正文截 4000 字
- #6 fallback 链：Parallel.ai 失败 → Sonar（重试1次5s）→ Exa model=”exa”
- #7 历经 Claude Sonnet → Gemini 3.5 Flash（格式过于机械否决）→ DeepSeek V4 Flash via OR/DigitalOcean（当前，2026-07-25 起开 thinking）；决策依据：Hermes/DI 对比验证”数据质量 > 模型档次”原则。fallback 由 `x-ai/grok-4.3` 升级为 `x-ai/grok-4.5`（2026-07-25 用真实调用验证 slug 与 `reasoning.effort` 参数均被 OR 接受，provider=xAI）
- #4 step 6c Sonar 失败：重试1次 → `””` 空节（RSS 14源 + Sonar Extract 已覆盖宏观面，不走 Exa）
- OR flex 延迟实测约 12-15s，作为应急路径可接受；gemini 模型不接受 `thinking` 参数，fallback 调用自动去掉

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
│   ├── fetch_news.py               RSS 聚合
│   │   ├── memory_context_finance.py   KB 上下文注入
│   ├── telegram_commands.py        TG bot + 追问流水线
│   ├── sas_review.py               季度 SAS 深度复盘（issue #32，2026-07-09），已接入 PM launchd 串联运行（第九节9.1），详见第十二节
│   ├── sec_edgar_utils.py          封装 edgartools：Form 4 内部人买入过滤 + 10-K risk factors 取值
│   └── migrate_reports.py          旧格式迁移（一次性）
├── .venv/
├── sas_tracked_tickers.json        SAS 永久追踪标的列表（原子写入，人工才能移除）
├── sas_review.lock                 sas_review.py 并发锁（运行时产生，非代码仓库内容）
├── finance_tavily_budget.json      Tavily 每日计数
├── finance_serpapi_budget.json     SerpApi 月度计数（首次使用时自动创建）
├── tg_offset.json                  TG getUpdates offset
├── archives/                       Extract 全文 archive（Obsidian 之外，不被 mine）
│   └── YYYYMM/
│       └── YYYY-MM-DD-{slot}-extract.md
└── backups/                        本地备份镜像（gitignore，不进代码仓库）
    └── 预判校准记录_backup.md      与 Obsidian 预判校准记录.md 同步写入，防 Obsidian 侧丢失

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
- **候选统一打标层（issue #14，并入方向 4）**：`score_and_filter()` 之后、`_haiku_relevance_filter()` 之前加统一打标步骤，产出两个 tier：相关度（direct_company_news/sector_related_news/macro_market_news，来自 #14）+ 信源形态（wire_article/video_hub/aggregator_listing，来自 #19 方向 4）。信源形态判断前移到候选阶段（URL 路径正则前筛 `/video/`/`/watch/` 等），而非现有 `_detect_low_structure()` 那样等 extract 抹完全文才事后判断——能在花 Tavily extract credit 之前就把视频/聚合页候选降权。同域名同事件时优先保留文章版，视频版降权/丢弃。用户决定先观察一段时间再评估优先级。
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

## 变更记录追加：2026-08-04 — `report_pass1`/`am_calibration` 切换 gemma-4-31b-it（issue #59，PR #61）

**背景**：`report_pass1`（`deepseek/deepseek-v4-flash`，未显式传 `thinking` key）2026-08-03 出现两次真实生产故障——AM 主报告调用 2/3 次被隐式推理吃满 `max_tokens=4000` 预算（`finish_reason=length`）；`calibration.py::evaluate_am_calibration()` 当时复用同一 `report_pass1` stage 做 PM 校验，3/3 次全部失败，靠 `fallback_model` 兜底才拿到结果。根因与 issue #53 当年修过的"隐式推理吃光判别式小任务预算"是同一模式，只是 `report_pass1` 从未被纳入那次修复范围。

**改动**（第8.1节表格、第5.5节已同步更新）：
1. `llm_config.py` DEFAULTS：`report_pass1.model` → `google/gemma-4-31b-it`，`providers` → `None`（不再走 DeepSeek 专属的 DigitalOcean/Venice pin）
2. 新增独立 stage `am_calibration`（同样默认 `google/gemma-4-31b-it`），`calibration.py::_evaluate_am_predictions()` 改用该 stage 而非复用 `report_pass1`——理由与 `tg_gap_detect`/`tg_followup` 拆分为独立 stage 一致，避免未来调 report_pass1 预算/模型时静默影响这个无关的 PM 判断
3. `llm_config.json`/`llm_config.example.json` 同步更新；新增回归测试 `test_calibration_uses_its_own_stage_not_report_pass1`

**验证**：用 2026-08-03 当天真实生产故障数据（真实价格表、RSS、Sonar宏观快照、AM可验证信号清单）重建两种 prompt 形状直接调用 OpenRouter 对比——`deepseek-v4-flash` 同条件下 4/4 复现真实故障（确认测试 prompt 忠实复现生产条件）；`google/gemma-4-31b-it` 6/6 全部 `reasoning_tokens=0`、`finish_reason=stop`，completion 仅占预算16-20%，路由到3个不同 OR provider 均稳定；内容质量核查（非仅结构校验）确认输出正确。另用真实生产 `call_llm(stage="report_pass1")`/`calibration._evaluate_am_predictions()` 端到端冒烟测试确认代码路径正确接入。`test_llm_config.py` 21/21 通过。

**同批核查（issue #60，未在本 PR 实施）**：按用户要求核查项目内剩余 `deepseek-v4-flash` 调用点，仅剩 `tg_followup`（Telegram Step 4，刻意保留以维持开放式持仓推理质量，issue #11 已验证取消 thinking 会明显降质）。压力测试发现该 stage 同样会无视 `thinking.budget_tokens=3000` 软上限（1/3 次烧穿 `max_tokens=12000`），但已有 fallback 兜底且真实生产 0 次失败，判定为低优先级、非阻断，未并入本 PR。候选 `openai/gpt-5.6-luna`（非pro）+`reasoning.effort=high` 已用同一真实数据验证 7/7 可行（`max_tokens=16000` 档更稳），但实施需要代码改动（OpenAI 系模型走 OpenRouter 统一 `reasoning` 参数而非 DeepSeek 的 `thinking` 字段）而非纯配置切换，建议单独排期。issue #59/PR #61 review（协作者 `blacktomb42`）额外指出 CLAUDE.md 状态记录和本文档未同步，均已修正。

**issue #60 实施（2026-08-04）**：上面记录的候选方案已实现，另加两项在同一轮讨论中一并验证并入的关联改动。

1. **`tg_followup` 切换到 `openai/gpt-5.6-luna`（非pro）+ `reasoning.effort=high`**：`max_tokens` 12000→16000（一并调整，理由与当初 8000→12000 一致——留足余量而非卡着上限）；`providers` 从 DeepSeek 的 `{order:[DigitalOcean,Venice]}` 改为锁定 `{order:[OpenAI],allow_fallbacks:false}`（避免路由到可能不遵守 `reasoning` 参数的 provider，与姊妹项目 `clip_processor.py` Stage 2 的已验证配置一致）；`telegram_commands.py::_followup_reason()` 主模型请求路径新增 `reasoning` 字段透传（此前只有 fallback 分支支持这个字段）。
2. **`report_pass2` 的 `report_md` 脱离 JSON 包裹**：`llm_client.call_llm()` 新增 `parse_json` 参数（默认 `True`，向后兼容其余 7 个 stage），`parse_json=False` 时跳过 `parse_llm_json`，直接返回 `{"text": <裸文本>, "_llm_meta": {...}}`，仅做防御性代码围栏剥离（模型即使被要求不要包围栏，仍可能习惯性加上）。空文本视为失败走既有重试/fallback 路径，不会把空 report_md 当成功返回。`report_pass2` 模型本身**未换**（仍是 `deepseek/deepseek-v4-pro`）——上一轮真实数据对比虽然发现它在"认知提升 vs 生态位验证"判断上出现过一次自相矛盾（详见 GitHub issue #60 评论），但样本量 n=1，按当时记录的建议暂不换模型，只做架构层的 JSON 解耦。
3. **`sas_candidates` 拆成独立 stage `sas_candidate_extract`**：不再是 Pass 2 JSON 的一个字段，而是 Pass 2 report_md 调用之后的第二次独立调用，复用同一份已组装好的价格/新闻/持仓上下文（`run_finance.py::SAS_CANDIDATE_PROMPT_TEMPLATE`），模型选 `google/gemma-4-31b-it`（9/9 真实对抗测试验证，见 issue #60 评论）。带来一个此前没有的副作用（正面）：`sas_candidates` 提取失败不再能连累 `report_md`——原先两者共享一个 JSON 信封，一次解析失败两个都丢。

验证：`test_llm_config.py`（24/24）、`test_telegram_followup_reason.py`（16/16，含 `reasoning` 字段透传、`provider` 锁定断言）新增/更新用例覆盖 `parse_json=False` 的正常返回、空文本重试/fallback、`reasoning` 字段校验；另用真实生产 `llm_client.call_llm(stage="report_pass2", parse_json=False)`、`call_llm(stage="sas_candidate_extract")`、`telegram_commands._followup_reason()` 三处端到端冒烟测试确认代码路径正确接入（非仅 mock 测试）。

**`report_pass2` 模型换成 `openai/gpt-5.6-luna`（同日追加，用户确认后实施）**：上一段记录的"模型本身未换、按 n=1 建议暂不换"是 PR 首次提交时的状态；用户复核上一轮真实数据对比（ORCL/CACI 事件的分类自相矛盾、SPCX 被误标为ETF两处真实问题，详见 issue #60 评论）后决定直接换模型，不再等待更多样本。改动：`report_pass2` 的 `model` 改为 `openai/gpt-5.6-luna`（非pro），`thinking` 置空、新增 `reasoning={"effort":"high"}`，`providers` 从 DeepSeek 的 `{order:[DigitalOcean,Venice]}` 改为锁定 `{order:[OpenAI],allow_fallbacks:false}`，`max_tokens` 8000→16000——与 `tg_followup` 完全同一套处理方式。`llm_client.py::call_llm()` 原先只透传 `thinking` 字段、从不发送 `reasoning`（即使 stage 配置了它）——这是本次修复顺带发现并补上的真实缺口，若不修，`report_pass2`/`tg_followup` 配置的 `reasoning` 参数会被静默丢弃，模型退化为无显式推理强度设置调用。同批，六个 `google/gemma-4-31b-it` stage（`report_pass1`/`am_calibration`/`sas_candidate_extract`/`semantic_filter`/`tg_preprocess`/`tg_gap_detect`）的 `providers` 从 `null`（不锁定）统一改为 `{order:["OpenInference"],allow_fallbacks:true}`——此前这些 stage 的真实调用观察到落在 Friendli/Crusoe/Novita/OpenInference 等多个不同 provider 上，改为显式偏好 OpenInference、允许失败时降级到其他 provider（而非强制锁死不可 fallback）。真实生产 `call_llm()` 冒烟测试确认三处均正确接入：`report_pass2` 路由到 `OpenAI`，`sas_candidate_extract` 路由到 `OpenInference`，`report_pass1` 一次实测降级路由到 `Novita`（验证了 `allow_fallbacks:true` 确实按预期生效，不是摆设）。
