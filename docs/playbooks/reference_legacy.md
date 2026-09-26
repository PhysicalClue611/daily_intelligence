# Daily Intelligence 旧版参考资料（从 CLAUDE.md 迁出，2026-09-25）

> 按原文迁出。其中不少内容已经过时，例如：TG 追问流水线仍写着 V4 Flash/Sonar/Claude Sonnet（现行为 gemma → yfinance → Parallel → gpt-5.6-luna）；目录结构还是 5 月的版本；RSS 表还是 7 个源的描述。现行内容以 Obsidian 设计文档（第六节 Telegram、第十节目录、5.2 来源）为准。这里保留，是为了方便溯源，也用于「不要重建某方案，除非先验证某前提」这类提醒。

## 目录结构（2026-05 版，已过时）

### 目录结构

```
~/Daily_Intelligence/
├── CLAUDE.md                          ← 本文件
├── scripts/
│   ├── run_finance.py                 ← 主入口
│   ├── fetch_prices.py                ← yfinance 价格拉取（宿主机运行）
│   ├── fetch_news.py                  ← RSS 聚合（FT/CNBC/Digitimes 等，httpx+feedparser）
│   ├── finance_email.py               ← Resend email client
│   ├── memory_context_finance.py      ← KB 上下文注入（bridge REST API）
│   ├── telegram_commands.py           ← Telegram 双向指令控制（long polling）
│   └── migrate_reports.py             ← 一次性迁移旧日报格式到月度文件
├── .venv/                             ← 宿主机专用 Python 环境
├── finance_tavily_budget.json         ← Tavily 每日计数（自动重置）
└── tg_offset.json                     ← Telegram getUpdates offset 持久化
```

**注：`ibkr/` 目录不在 git 中（broker 专属，不随代码分发）。IBKR 相关代码已保留但禁用（`_ibkr_auth_note()`、`_fetch_ibkr_prices()` 均返回空字符串），可通过恢复函数体并在本地挂载 `ibkr/` 模块重新启用。**


## Telegram 统一预处理（旧版描述）

### 统一预处理（单次 V4 Flash）

所有消息经一次 V4 Flash 调用完成意图分类 + followup 上下文提取，输出：

```json
{
  "action": "add_ticker | remove_ticker | add_geo | ... | followup | status | force_run | unknown",
  "item": "...",         // 指令类
  "query": "...",        // followup: 精准英文搜索词（含ticker、确切日期、盘中/盘后）
  "relevant_tickers": [], // followup: 持仓中最相关的ticker
  "framework_focus": "", // followup: 最相关的投资框架考量
  "question_intent": ""  // followup: 用户真实意图一句话
}
```

---


## TG 追问流水线

```
Step 1 — V4 Flash（统一预处理，~$0.0001）
  输入：用户消息 + 今昨日期 + 持仓快照
  输出：action 分类 + {query, relevant_tickers, framework_focus, question_intent}

Step 2 — Sonar（多源研究简报，~$0.005 含固定搜索费）
  输入：精准英文 query
  输出：事件驱动力 + 来源引用

Step 3 — Claude claude-sonnet-4-6 via Azure（个人化推理，~$0.019）
  系统提示：投资框架（启动时读入，module-level cache）
  用户消息：Sonar 简报 + 持仓快照 + MemPalace + question_intent
  输出：核心驱动力 / 对持仓含义 / 观察信号

追问完成后 → append 到月度文件（## 追问 YYYY-MM-DD HH:MM ET）
  格式：追问 / 情报（Sonar）/ 回答

总成本：~$0.025/次追问
```

持仓快照来源：`Finance/portfolio_report_latest.md`

---

## 月度报告格式

```
Daily_Intel_report_YYYYMM.md
---
date: YYYY-MM
source: Hermes Finance Daily Intelligence
---

# Daily Intelligence YYYY-MM

## YYYY-MM-DD 开盘前简报
_Tavily: N/10_

[report content]

---

## YYYY-MM-DD 夜盘动向
...

## 追问 YYYY-MM-DD HH:MM ET

**追问：** [用户问题]
**情报（Sonar，搜索词：...）：** [Sonar 简报节选]
**回答：** [Claude 个人化推理]

---
```

防重：检查月度文件是否含 `## {date} {slot_label}` header。

---

## 与 Hermes MI 的隔离边界

| 维度 | Hermes MI（china-intel） | Daily Intelligence |
|---|---|---|
| 数据源 | Tavily 为主 | yfinance + RSS 为主，Tavily 按需 |
| 收件人 | intel_config.yaml | watchlist.md |
| Obsidian 路径 | `Hermes/MI/` | `Hermes/Daily Intelligence/` |
| 邮件 subject | `[Hermes MI]` | `[Hermes Finance]` |
| 调度 | launchd 周日 8:59 AM | launchd 每日 2 次 |
| 运行环境 | Docker 容器 | 宿主机 |
| Gmail scope | send+readonly+modify | send+readonly |
| Telegram bot | Hermes bot（共用） | @PhyCluFintel_bot（独立） |
| bridge URL | host.lima.internal:8765 | localhost:8765 |

---

## RSS Feeds（7个源 + Guardian API；issue #85 起五个综合新闻源停用）

| Feed | URL | 定位 |
|---|---|---|
| FT World | `https://www.ft.com/world?format=rss` | 专业财经 |
| CNBC | `https://www.cnbc.com/id/100003114/device/rss/rss.html` | 快速财经/市场 |
| MarketWatch | `https://feeds.marketwatch.com/marketwatch/topstories/` | 市场数据驱动 |
| Foreign Policy | `https://foreignpolicy.com/feed/` | 地缘战略深度 |
| Seeking Alpha | `https://seekingalpha.com/market_currents.xml` | 个股机构分析 |
| Reuters（via Google News） | `https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en` | 综合/财经（Google 代理，<1h延迟） |
| Digitimes | `https://www.digitimes.com/rss/daily.xml` | 台湾/大陆半导体供应链贸易媒体（issue #72） |

**停用（issue #85，`RSS_FEEDS_DISABLED`，URL 保留，挪回 `RSS_FEEDS` 即恢复）**：NYT Business/World/Politics、BBC Business/World、Al Jazeera、AP（Google News）、WSJ（Google News）。2026-09-23 实测这五个源占 RSS 总量 57%，只有约 10% 与持仓/科技/宏观相关；在子串匹配打地缘标签、每桶只留 5 条的情况下，它们把持仓新闻挤出了 prompt。

**Guardian API**（`content.guardianapis.com/search`，非 RSS）：免费 500次/日，JSON 结构化，`GUARDIAN_API_KEY` 控制，fail-open。`fetch_guardian_news()` 拉取 business/world/politics/us-news 板块最新 20 条，合并进 RSS 结果统一排序。

不可用（DNS/TLS 受限）：Politico（403）。Reuters/AP/WSJ/Guardian 直连不可达，已通过 Google News RSS / Guardian API 覆盖。

---


## 已结束或已失效的观察项（原「下一步优先事项」1–17）

1. **Pass 2 重构效果验证**（2026-05-28 起）：观察报告是否从"每日财经速报"升级为"面向持仓框架的情报研判"——关键指标：① 异动标的下是否出现"对 INTC@均价 持仓逻辑的含义"类推理；② 地缘/宏观是否有具体传导路径而非泛泛描述；③ thinking 预算 3000 tokens 是否足够（完整分析 vs 截断）；④ `Layer_A_Prompt.md` 内容是否需要调整
2. **月度文件重复节根因排查**：已有修复脚本，但触发条件未明；观察 06 月文件是否再出现重复
3. **OR flex fallback 首次实战验证**：观察 DeepSeek 再次不可达时日志是否出现 `OR flex fallback succeeded` 且报告正常生成；关注 flex 延迟是否在可接受范围（预期 <30s 单次调用）
4. **追问流水线多 query 效果验证**：Step 1 新增 `search_queries` 双 query（事件角度 + 量化/技术角度），观察 Parallel.ai 是否能拿到期权 IV、历史财报模式等深层数据；对比单 query 和双 query 的内容质量差异
5. **watchlist 调整**：根据实际报告质量增减 ticker 或地缘政治主题
6. **[已失效，PR #89]** 语义过滤 stage 已删除。 原文：**gemma-4-31b-it 生产观察**（issue #53/PR #54，2026-07-23 起）：确认 `/tmp/daily_intelligence.log` 中 `Semantic filter tokens:` 行的 `reasoning=` 字段持续为 0（或至少不再吃满 `max_tokens`），`Semantic filter failed` 不再出现；观察新的 usage/finish_reason 日志是否足以在下次异常时免去外部付费调用排查
7. **`tg_gap_detect`/`tg_preprocess` 切换 gemma-4-31b-it 后的真实使用观察**（issue #11，2026-07-25 起）：`tg_gap_detect` 此前疑似从未真正生效过（deepseek 隐藏推理烧光60-token预算），观察 TG 追问日志里 `Step 4 tokens` 前是否开始出现真实的"补搜第3条 query"命中；`tg_preprocess` 观察日常加/删 ticker、地缘关键词等指令是否不再出现分类错误或截断（此前 deepseek 在简单指令上出现过 3 次调用 3 种错误结果）
8. **`llm_config.json` 实际使用观察**（issue #11，2026-07-25 起）：目前该文件内容与 DEFAULTS 完全一致（零覆盖），观察是否有实际调整需求（如某 stage 换模型、调预算）；每次编辑后确认 `/tmp/daily_intelligence.log` 或 `/tmp/finance_telegram.log` 出现对应的 `LLM config override:` 日志，验证改动真的生效
9. **[已失效，PR #89]** `report_pass1` 已删除，仅 `am_calibration` 的观察仍有效。 原文：**`report_pass1`/`am_calibration` 切 gemma-4-31b-it 后的生产观察**（issue #59/PR #61，2026-08-04 起）：模型选型本身已用真实 prompt 验证完成（6/6 通过，见上方状态章节），不需要再跑付费验证；观察项改为 `grep "LLM tokens \[report_pass1/google/gemma-4-31b-it\]\|LLM tokens \[am_calibration/google/gemma-4-31b-it\]" /tmp/daily_intelligence.log`，确认 `reasoning=0`、`finish_reason=stop` 在真实 AM/PM 报告运行中持续成立，不再出现 `finish_reason=length` 告警
10. **issue #65 残余上涨 / 24h 日切**（2026-08-13 起，主泄漏已修）：PID 31487 13h 58MB、约 2MB/h。确认次日 ~03:34 后 PID 换新、日志有 `Recycling telegram bot process after 24h uptime`；若日切失败且斜率不减速，两周可到 ~700MB
11. **issue #60/PR #62 生产观察**（2026-08-05 起，已实施+已合并，见上方状态章节）：`tg_followup`/`report_pass2` 均已切至 `gpt-5.6-luna`+`reasoning.effort=high`，2026-08-04 当晚已用真实生产双跑验证过一次（质量优于旧模型，见上方状态章节完整记录）；后续观察 `grep "LLM tokens \[report_pass2/openai/gpt-5.6-luna\]\|Step 4 tokens \[openai/gpt-5.6-luna\]" /tmp/daily_intelligence.log /tmp/finance_telegram.log`，确认 `finish_reason=stop` 持续成立、`provider=OpenAI` 稳定路由（硬 pin 不允许 fallback，需要留意是否出现非429 4xx 导致的静默降级到 Pass1-only report_md，PR #62 review 已识别此风险并记入 `llm_client.py` 注释，未做代码修复）
12. **真实成本核算**（issue #60 遗留缺口）：`gpt-5.6-luna` 与 `deepseek-v4-pro`/`deepseek-v4-flash` 的实际生产量级成本差异尚未核算，观察一段时间后可用 OR 账单核实
13. **issue #67/PR #68 生产观察**（2026-08-13 起，主路径已并入 #63 的 quiet logger）：下次 AM/PM 后 `grep -E "possibly delisted|yfinance daily bulk" /tmp/daily_intelligence.log`——期望不再出现 yfinance `possibly delisted` ERROR；bulk 抖时可见 `yfinance daily bulk incomplete` WARNING（及可选 `retry recovered` INFO），报告仍发出。52 周路径仍走同一套 `_quiet_yfinance_logs()`
14. **[已失效，PR #89]** 异动 query、7 天围栏、rotation 已删除；Digitimes 仍在 RSS 源里。 原文：**issue #72/#76 生产观察**（2026-09-21 起）：Digitimes 触发命中率；AM/PM 前 3 大异动各一条、Finnhub 仅补充、rotation 只与实际 anomaly job ticker 去重后 Tavily 日消耗；`anomaly fence: dropped` 与 `Issue #33 rotation skipped` 日志。
15. **[已失效，PR #89]** rotation 已删除，问题前提不再存在，issue #74 仍 OPEN 待关闭。 原文：**issue #74**（未实现）：rotation 30 天材料与异动证据同池，污染【价格异动】归因。改善方向见该 issue，不在 #72 / #80 范围。
16. **[已失效，PR #89]** 多日追因 query 已删除；3/5 日阈值现在只用于代码 Pass 1 选标的。 原文：**issue #80 生产观察**（2026-09-23 起）：安静日是否仍为 3 日 ≥15% 或 5 日 ≥20% 的个股发出追因；日志 `Issue #80 unexplained-move queries`；Tavily 日消耗是否仍留在 25cr 内。AAOI 不应出现在这层。
17. **[已失效，PR #89]** 预留名额与开放池已删除，Extract 规则见第 18-20 条。 原文：**issue #82 生产观察**（2026-09-23 起）：日志 `Issue #82 reserved extract slots` 是否含未解释大涨 ticker；Extract 存档里该 URL 标为预留；日消耗是否仍在 25cr 内。

## 待实现功能（已评估，用户确认，未动手）

### A1：A 股实时行情（腾讯 qt.gtimg.cn）

- 接入方式：`curl "https://qt.gtimg.cn/q=sh600519,sz000858" -H "Referer: https://finance.qq.com"`，返回 `var hq_str_sh600519="贵州茅台|...|当前价|...|昨收|..."` 格式，`|` 分割，索引 [3] 当前价、[4] 昨收，免费无 key
- 实现位置：`fetch_prices.py` 新增 `fetch_a_stocks()`；`watchlist.md` 新增 `## a_stocks` 段（格式：`sh600519 | 贵州茅台 | 3.0`）
- 设计原则：A 股行情走免费直连渠道，异常触发时同样复用现有 Tavily 搜索（中文 query）——与整体"免费渠道收数据、有限额 API 做综合"一致
- 状态：**未实现**，下次新 session 直接开始

### B2：Parallel.ai 降为二线 / 缩减触发范围

- 背景：Parallel.ai 是一次性 $20 credit（无月度重置），追问频率高时存在 credit 耗尽风险
- 核心价值：唯一能在单次调用内完成 search + full-text extract（4000字/篇原文）的工具；Sonar/Exa 返回的是合成摘要，财报数字、期权 IV 等细节会在摘要层丢失
- 可选方案 A：**禁用 P1 自适应第三条 query**（节省约 30-40% 用量），保留 2 query + 3 URL extract，每次追问固定 ~$0.007，$20 credit 约够 2800 次
- 可选方案 B：**Parallel 降为按需触发**，主路径改回 Sonar（$0.005/次固定），仅在用户明确要求深度原文时调用 Parallel
- 当前决策：维持现状，追问频率低时 $20 credit 足够支撑；若出现 credit 预警则优先执行方案 A
- 状态：**待观察**

### B1：Serper.dev 作为搜索三级 fallback（Daily Intelligence 专属）

- 现状：DI 只有 Tavily → SerpApi 两级；china-intel 已有三级（search_utils.py），DI 未复用
- 接入方式：`POST https://google.serper.dev/search`，header `X-API-KEY`，key 即 `SERPER_API_KEY`（已在宿主机 .env 配置）
- 实现位置：`run_finance.py` 搜索路由层，SerpApi 月配额（250次）耗尽后接管
- 状态：**未实现**

---

## 设计决策备忘

### 为何 TG 追问 Step 2 保留 Perplexity Sonar 而非换 Kiro ACP

评估过 Kiro ACP（AWS 内置 Web 搜索 agent，免费额度内零成本），最终保留 Sonar 原因：
1. **接口层开销**：Kiro 需要 `sessions_spawn runtime="acp"` 调用，是 agent 会话而非直接 API，响应延迟和接口稳定性不可控
2. **内容偏向**：Kiro 强项在 AWS 文档和技术研究，金融宏观话题质量未经验证
3. **当前成本可接受**：Sonar $0.005/次追问，每月追问频率低，总月度成本 < $1，不构成优化压力

### 为何不用 Yahoo Finance 直接 API（query1.finance.yahoo.com）

yfinance 已处理批量下载和 429 重试，容器外（宿主机）运行无封锁，实测未触发 429。直接 API 增加维护复杂度（UA、session 管理）而收益不明。**仅当 yfinance 在宿主机开始出现持续 429 时才切换。**

### 为何不接入基金净值（天天基金 fund.eastmoney.com）

无当前持仓中国公募基金，接入后 watchlist 和报告结构均需改动。如未来配置 A 股/港股基金再评估。


## 原「下一步」第 27 条（2026-09-25 下午版本，已由 CLAUDE.md 新版第 27 条取代）

27. **issue #113**：R1 待 owner 观察几天后决定，写 handoff 或实现前必须先问 owner 要结论；R2–R5 可直接做。Extract 正文质量（通用查询词、1200 字截断、Yahoo 导航栏、付费墙域名、安静个股搜索词与日期）是更主要的瓶颈，候选项已追加到 #113 comment，待 owner 确认。

## TG 指令表中「自然语言提问」一行的旧文（2026-09-25 替换）

| 自然语言提问 | 三步流水线：V4 Flash 预处理 → Sonar 研究简报 → Claude claude-sonnet-4-6 个人化推理 |
