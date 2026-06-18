# Daily Intelligence — 项目记忆

每日财经情报系统，独立于 Hermes Agent（`~/Hermes`）运行。

---

## 当前系统状态（2026-05-04）

**已上线。每交易日两次自动运行（开盘前 + 夜盘）。已接入 MemPalace/KG。Telegram 双向控制已启用。TG 追问三步流水线（V4 Flash + Sonar + Claude claude-sonnet-4-6）已上线。SerpApi 已接入为 Tavily 日配额耗尽后的 fallback。LLM 调用层已加重试（网络/5xx 自动 2 次重试）。支持 FINANCE_FORCE_DATE / FINANCE_FORCE_SLOT 手动重跑。时区已改用 ZoneInfo（冬令时自动处理）。Telegram HTML 转义已修复。搜索优先级（anomaly 先行）已与设计文档对齐。KG CLI 历史回填修复（AM+PM 均提取）。**

### 目录结构

```
~/Daily_Intelligence/
├── AGENTS.md                          ← 本文件
├── scripts/
│   ├── run_finance.py                 ← 主入口
│   ├── fetch_prices.py                ← yfinance 价格拉取（宿主机运行）
│   ├── fetch_news.py                  ← RSS 聚合（NYT/BBC/FT，httpx+feedparser）
│   ├── finance_gmail.py               ← Gmail client（send+readonly scope）
│   ├── memory_context_finance.py      ← KB 上下文注入（bridge REST API）
│   ├── kg_extractor_finance.py        ← 报告后提取 KG 三元组
│   ├── telegram_commands.py           ← Telegram 双向指令控制（long polling）
│   └── migrate_reports.py             ← 一次性迁移旧日报格式到月度文件
├── .venv/                             ← 宿主机专用 Python 环境
├── finance_tavily_budget.json         ← Tavily 每日计数（10次上限，自动重置）
└── tg_offset.json                     ← Telegram getUpdates offset 持久化
```

### 依赖外部资源

| 资源 | 路径 | 说明 |
|---|---|---|
| 监控配置 | Obsidian: `Hermes/Daily Intelligence/watchlist.md` | 手工编辑或 TG 指令修改 |
| 月度报告 | Obsidian: `Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_YYYYMM.md` | 脚本 append 写入 |
| 持仓快照 | Obsidian: `Finance/portfolio_report_latest.md` | portfolio-agent 覆盖更新 |
| Gmail token | `~/.hermes/token.json` | 借用 Hermes 的 OAuth token |
| API keys | `~/.hermes/.env` | OPENROUTER_API_KEY, TAVILY_API_KEY, SERPAPI_API_KEY, FINANCE_TELEGRAM_BOT_TOKEN, FINANCE_TELEGRAM_CHAT_ID |
| email_sender | `~/.hermes/skills/intel/china-intel/scripts/` | 共享工具，只读借用 |

---

## 调度

```
报告任务:   com.hermes.finance.plist
  - 5:30 AM PT  = 8:30 AM ET  → 开盘前简报（slot=am）
  - 8:59 PM PT  = 23:59 ET    → 夜盘动向（slot=pm）
  非交易日: exchange_calendars 检查后静默退出

Telegram bot: com.hermes.finance.telegram.plist
  - KeepAlive 常驻，long polling timeout=30，响应延迟 < 1s
  - Bot: @PhyCluFintel_bot（独立 token，与 Hermes bot 隔离）

日志:
  /tmp/daily_intelligence.log     ← 报告任务
  /tmp/finance_telegram.log       ← Telegram bot
```

手动触发：
```bash
HERMES_DATA=~/.hermes \
OBSIDIAN_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview" \
~/Daily_Intelligence/.venv/bin/python ~/Daily_Intelligence/scripts/run_finance.py
```

---

## 运行逻辑（run_finance.py）

```
0.  环境变量覆盖：FINANCE_FORCE_DATE → 强制报告日期；FINANCE_FORCE_SLOT=am|pm → 强制时段；FINANCE_FORCE_RUN → 绕过交易日/防重检查
1.  NYSE 交易日检查 → 非交易日退出（FORCE_DATE/FORCE_RUN 时跳过）
2.  确定 run_slot（am: hour<18 / pm: hour≥18，FORCE_SLOT 优先）和 slot_label（开盘前简报/夜盘动向）
3.  防重检查：月度文件中搜索 "## {date} {slot_label}" → 存在则退出
4.  读取 watchlist.md
5.  yfinance 批量拉取 tickers
6.  RSS 聚合（6个 feed，过去24小时）
7.  bridge 拉取 KB 上下文（fail-open）：MemPalace + KG + Obsidian 搜索
8.  LLM pass 1（deepseek-v4-flash）注入 kb_section → 分析 + skip 判断 + Tavily query
    （网络/5xx 错误自动重试 2 次，间隔 2s/4s；4xx 和 JSON parse 不重试）
9.  条件触发 Tavily（异动优先，LLM query 次之，互斥）
10. 有 Tavily → LLM pass 2 合并生成最终报告
11. skip=true → 退出，不写文件不发通知
12. PM slot：替换报告标题为「夜盘动向」
13. Append 到 Obsidian 月度文件 Daily_Intel_report_YYYYMM.md
14. KG 提取（kg_extractor_finance.py）→ 直接接受 report_text，≤15 条三元组写回 KG（fail-open）
15. 发邮件 → watchlist.md 中配置的收件人
16. 发 Telegram（@PhyCluFintel_bot）→ Markdown 转 HTML，超 4096 字符自动分段
```

手动重跑（补跑历史报告）：
```bash
FINANCE_FORCE_DATE=2026-05-01 FINANCE_FORCE_SLOT=pm \
HERMES_DATA=~/.hermes \
OBSIDIAN_PATH="~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview" \
~/Daily_Intelligence/.venv/bin/python ~/Daily_Intelligence/scripts/run_finance.py
```

---

## Telegram 指令（telegram_commands.py）

| 指令示例 | 动作 |
|---|---|
| `加 MSFT` / `删 INTC` | 增删个股，直接改 watchlist.md |
| `加收件人 x@x.com` | 增删收件人 |
| `加关键词 US-Iran blockade` | 增删地缘政治关键词 |
| `状态` | watchlist + Tavily用量 + 最近AM/PM报告 + launchd状态 + 推理流水线配置 |
| `强制运行` | FINANCE_FORCE_RUN=1 绕过防重立即触发 |
| 自然语言提问 | 三步流水线：V4 Flash 预处理 → Sonar 研究简报 → Claude claude-sonnet-4-6 个人化推理 |

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

## Tavily 预算

- 上限：10次/日，`finance_tavily_budget.json` 按 ET 日期自动重置
- 主报告：0-2次/天；TG 追问不消耗 Tavily（Sonar 内建搜索）
- 超出上限：跳过 Tavily，继续生成基础报告

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

## RSS Feeds（已验证可达）

| Feed | URL |
|---|---|
| NYT Business | `https://rss.nytimes.com/services/xml/rss/nyt/Business.xml` |
| NYT World | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` |
| NYT Politics | `https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml` |
| BBC Business | `https://feeds.bbci.co.uk/news/business/rss.xml` |
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` |
| FT World | `https://www.ft.com/world?format=rss` |

不可用（DNS/TLS 受限）：Reuters、AP、WSJ、Guardian

---

## KB 接入说明

- bridge URL：`http://localhost:8765`（宿主机直连）
- MemPalace 查询：`wing=paperview, room=finance`，sim~0.4（随 KG 积累改善）
- KG 三元组：`subject`=ticker/宏观实体，`predicate`=snake_case 英文，`source_file`=`{date}-finance.md`
- 所有 bridge 调用 fail-open

---

## 踩过的坑

1. **Yahoo Finance 429**：容器内不可达，yfinance 需要宿主机运行。生产环境每天一次不触发。

2. **yfinance MultiIndex**：`group_by="column"` 时用 `data["Close"][ticker]` 访问。

3. **Gmail invalid_scope**：finance_gmail.py 独立客户端，只声明 send+readonly。

4. **feedparser + Python 3.14 TLS**：httpx 拉取原始 XML，feedparser 只做解析。

5. **RSS DNS 限制**：Reuters/AP/WSJ/Guardian 不可用，改用 NYT/BBC/FT。

6. **BUDGET_PATH 位置**：项目本地 `_PROJ_DIR / "finance_tavily_budget.json"`，与 Hermes 解耦。

7. **重复邮件**：干跑 + launchd 各触发一次。修复：月度文件 section header 防重。

8. **00:00 ET 调度陷阱**：午夜整点已是新一天，次日若非交易日则静默退出。应使用 23:59 ET（20:59 PT）。

9. **Telegram 重复标题**：report_md 已有 `#` 标题，send_telegram_report 不应再加 subject 前缀。

10. **PM slot LLM 标题**：LLM 始终写「开盘前简报」，需在写文件前 `re.sub` 替换为「夜盘动向」。

11. **Telegram bot token 冲突**：Hermes Agent 已持续监听 Hermes bot token 的 getUpdates，共用 token 导致消息被随机消费。Daily Intelligence 使用独立 bot @PhyCluFintel_bot。

12. **Perplexity 隐藏搜索费**：所有 Perplexity 模型在 token 费之外固定收 $0.005/次搜索调用，与 token 量无关。OR 页面未显眼提示，导致实际成本远高于 token 计算。选模型时需考虑这个固定成本。

13. **DeepSeek R1 拒绝 2026 日期**：R1 训练截止约 2025 年中，遇到注入的 2026 日期会主动判定为"未来时间"并拒绝接受，回退到训练数据（2023 年事件）。R1 不适合实时数据合成场景。

14. **GPT-4o search 隐藏费用**：OpenAI search 模型在 token 费之外还有 per-search 调用费，导致 $0.006 token 成本变成 $0.04 实收。

15. **Python 字符串内中文引号**：`"` 和 `"` 放入 Python 双引号字符串会触发 SyntaxError，一律改用 `【】`。

16. **RSVP 延迟**：原来 RSVP 在 `_parse_command`（LLM调用）之后发送，导致 ~20 秒延迟。修复：主循环收到消息立即发 `⏳`，再做 LLM 分类。

17. **ET 硬编码 UTC-4**（已修复 2026-05-04）：原 `ET = timezone(timedelta(hours=-4))` 是 EDT，冬季差 1 小时。三个文件已统一改为 `ZoneInfo("America/New_York")`，夏/冬令时自动处理。

18. **持仓快照截断**：`_get_portfolio_snapshot()` 原来有 600 字符上限，导致 QCOM 等靠后的持仓被截断，LLM 无法看到。修复：去掉字符限制，改为只提取 IB 美股持仓，过滤 CASH 和 A 股 ETF 编号（纯噪音）。

19. **Sonar/Claude 输出截断**：Sonar `max_tokens=700`、Claude `max_tokens=900` 均撞顶导致回答截断。已分别提高到 1500 和 2000。

20. **超长会话 token 用量**：单次会话内读取大文件（如 `金融资产信息.md` ~5000 tokens）+ 积累大量代码和日志上下文，会快速耗尽 5 小时窗口限额。大文件按需分节读取，长文档生成建议在新会话里做。

21. **SerpApi 月度预算独立跟踪**：不与 Tavily 共用 budget 文件。`finance_serpapi_budget.json` 用 `year_month: "YYYY-MM"` 作 key，月初自动重置。TG bot 是常驻进程，加新 env var 后需重启才能读到：`launchctl stop/start com.hermes.finance.telegram`。

22. **OpenRouter 连接不稳定**：peer closed connection / Server disconnected / SSL UNEXPECTED_EOF 等瞬时错误偶发。已在 call_llm() 和 telegram_commands.py 三个 LLM 调用点加网络/5xx 重试（2次，指数退避 2s/4s），4xx 和 JSON parse 不重试。重跑时可用 FINANCE_FORCE_DATE + FINANCE_FORCE_SLOT 补跑历史报告。

23. **Telegram HTML 注入**（已修复 2026-05-04）：`reply()` 使用 `parse_mode=HTML` 但未转义，LLM 回答含 `<`, `>`, `&` 时 Telegram 返回 Bad Request，`_tg()` 静默吞掉，用户收不到回复。修复：拆成 `reply()`（自动 escape）和 `reply_html()`（预格式化 HTML），`_md_to_tg_html()` 正文先 escape 再替换 Markdown 标记。

24. **搜索触发优先级倒置**（已修复 2026-05-04）：原代码先触发 LLM `tavily_query`，异动搜索反而是 fallback，与设计文档相反。修复：抽出 `_do_search()` 辅助函数，anomaly 先行，`tavily_query` 仅在无 anomaly 结果时触发。

25. **KG CLI section 被内部小节截断 + 仅取首 section**（已修复 2026-05-04）：月度文件内 `## 【价格异动】` 等小节和日期 header 同层级，lookahead `(?=\n## |\Z)` 在第一个小节就截断；同时 `re.search` 只取首个日期 section，AM+PM 均有时漏掉 PM。修复：lookahead 改为 `(?=\n## \d{4}-\d{2}-\d{2} |\Z)`；`re.search` 改为 `re.finditer`，对同一天所有 sections 累加提取。

---

## 下一步优先事项

1. **观察 OR 连接稳定性**：重试逻辑是否能覆盖大部分瞬时故障，是否需要引入 provider 降级（如 V4 Flash 主用 Nebius 备用 Parasail）
2. **KB context 质量**：随每日 KG 三元组积累，room=finance 召回相关性应逐步从 ~0.4 提升
3. **MemPalace mine 接入**：将月度报告写入向量索引，让历史异动和追问记录进入语义搜索
4. **watchlist 调整**：根据实际报告质量增减 ticker 或地缘政治主题
