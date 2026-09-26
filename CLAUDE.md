# Daily Intelligence — 项目记忆

每日财经情报系统，独立于 Hermes Agent（`~/Hermes`）运行。

**本项目将开源（2026-06-18 决策）。** 代码维护须遵守开源标准：无硬编码邮箱地址、无用户名路径（一律 `$HOME/`）、无 broker 专属目录（`ibkr/` 已从 git 中移除）。API key、收件人等均通过环境变量或 Obsidian 配置文件注入，不得写死在代码中。

**按需查阅的详细文件**（2026-09-25 做 context trim 时从本文件迁出，内容一字未删）：
- `docs/playbooks/status_history.md` — 2026-05 至 09 各次「当前系统状态」段（每个 issue/PR 的实现细节、review 记录、生产验证），以及 #87 两个 PR 的完整说明
- `docs/playbooks/reference_legacy.md` — 旧目录结构、旧 TG 追问流水线、月度报告格式、与 Hermes MI 的隔离边界、RSS 源表、已结束的观察项、待实现功能（A 股行情 / Parallel 降级 / Serper）、设计决策备忘
- `docs/PITFALLS.md` — 踩坑索引（1–96）加详情
- `docs/playbooks/github_issue_format.md` — issue 五段式格式规范
- `docs/design.md` — 设计文档的仓库快照（权威版本在 Obsidian）

---

## [强制] Session 初始化

**每个新 session 开始时，无论用户第一句话是什么，必须先读以下两个文档，再做任何其他操作：**

1. `Hermes/Daily Intelligence/Daily_Intel设计文档.md` — 权威架构参考，API key 路径、LLM 选型、流水线细节均以此为准
2. `Hermes/Daily Intelligence/Daily Intelligence 开发部署日志.md` — 近期变更与踩坑记录

**若用户第一条消息已明确指定"阅读文档"，该指令必须立即执行，不得跳过或延后。**

CLAUDE.md 仅作快速索引，两文档不一致时以 Obsidian 设计文档为准。

---

## [强制] "打扫战场"必须包含设计文档更新

*背景见 playbook `status_history.md`（2026-07-23：只更新了开发日志，设计文档停在 07-09，落后六项重大变更）。*

**规则：本项目每次"打扫战场"，第 4 项（Obsidian 文档更新）必须显式拆成两个独立动作，都要做，不能只做一个就视为完成：**

1. 更新 `Daily Intelligence 开发部署日志.md`（叙事型，append 到文件尾部，记录"这次做了什么、踩了什么坑"）
2. 检查并更新 `Daily_Intel设计文档.md`（架构权威参考）——本次会话若新增/修改了数据源、LLM 选型、流水线步骤、目录结构、prompt 注入槽，必须同步反映到对应章节（第五节数据采集、第八节 LLM 选型表、第十节目录结构等），而不是只在开发日志里提一笔。判断标准：**如果这次改动会让 session 初始化时读到的架构描述、模型选型表、文件清单出现任何一处"与代码不符"，就必须更新设计文档**，哪怕只是一行表格或一个函数名。
3. 大改动（新数据源、新子系统、模型切换等）额外在文档末尾追加"变更记录追加：YYYY-MM-DD"小节；小改动（如单个函数改名、单个常量调整）直接原地修正对应章节的过时表述，不必单独开变更记录小节。
4. 更新"最后更新"元信息行（文档顶部），反映本次改动的日期和摘要。

---

## 当前架构速览（2026-09-25，`66e1ff4`）

*各次改动的细节见 playbook `status_history.md`；权威描述是 Obsidian 设计文档。*

- **价格**：由 yfinance 在宿主机上拉取；PM 的今日收盘价取 intraday 数据（#69）。两次运行分别是 AM 08:30 ET 和 PM 20:10 ET（PM 结束后接着跑 `sas_review.py`，当前为 NOTIFY_ONLY）。
- **Pass 0（免费，不调用 LLM）**：`intel_pass0.py`/`intel_collect.py` 按个股收集以下来源：Finnhub、Google News（公司名）、7 个 RSS 源、Guardian、SEC 8-K、Yahoo 个股新闻（yfinance `get_news`，覆盖字段仍叫 `yahoo_rss`）。标题按别名匹配、去重；与上一份快照重复的标 `seen_before`。
- **Pass 1（代码规则，不调用 LLM，`intel_deepen.py`）**：分三层，额度不够时从后往前截。
  1. 异动股：最多 5 只，每只 2 条直链，没有直链才搜索。
  2. 宏观：3 个话题，每个话题 3 条直链，排除 news.google.com 和付费墙站点。
  3. 安静个股：最多 5 只，按「接近多日阈值 3 日 8% / 5 日 9%」「新 8-K」「新闻量异常」入选，每只 3 条。

  每次运行最多搜索 5 次；新闻量异常至少要有 5 份同档历史快照。最近 6 次已抓正文的 URL（去 fragment、`utm_*`）不再抓。Extract 按标的或话题生成 query，同层小组可合批；每批 ≤20 URL，按 `ceil(n/5)` 计费，每篇最多 3 个 chunk、2000 字。清洗后不足 300 字或样板噪声过高的正文不进入报告。
- **Tavily 额度**：每日 25cr。定时 AM 最多 13，定时 PM 用当天剩下的全部。手动运行（`FINANCE_FORCE_RUN`/`FINANCE_FORCE_DATE`/TG 强制运行）不看日账，AM 13 / PM 12，用量单独记在 `finance_tavily_manual_budget.json`。`sas_review.py --ticker` 手动运行每次 1cr，也单独记账。SerpApi 只在 Tavily 用不了时兜底。
- **Pass 2**：`report_pass2` = `openai/gpt-6-luna`，推理 xhigh，`max_tokens` 32000。近 3 个交易日至 7 个自然日内、与当日走势相关的已报道事件可完整分析；本次抓到正文的公司级实质事件必须覆盖，除非近五日报告已经写过且无新进展。截断时先降一档推理强度，再换 fallback，最后退回代码摘要并发 TG 告警。另有 SAS 候选抽取（gemma）和 PM 核对 AM 预判（gemma）。
- **输出**：Obsidian 月度报告、情报快照 `archives/YYYYMM/*-intel-snapshot.json`、Context Log、MemPalace、邮件、TG，以及一条单独的 TG 运行状态消息。
- **LLM 选型**：集中在 `scripts/llm_config.py`，可用 `llm_config.json` 覆盖（纳入 git），改完无需重启。
- **TG bot**：`com.daily-intel.finance.telegram`。改了 `telegram_commands.py` 必须 `launchctl kickstart -k gui/$(id -u)/com.daily-intel.finance.telegram`，并用启动时间核实已生效。

### 依赖外部资源

| 资源 | 路径 | 说明 |
|---|---|---|
| 监控配置 | Obsidian: `Hermes/Daily Intelligence/watchlist.md` | 手工编辑或 TG 指令修改 |
| 月度报告 | Obsidian: `Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_YYYYMM.md` | 脚本 append 写入 |
| 持仓快照 | Obsidian: `Finance/portfolio_report_latest.md` | portfolio-agent 覆盖更新 |
| Gmail token | `~/.hermes/token.json` | 借用 Hermes 的 OAuth token |
| API keys | `~/Daily_Intelligence/.env` | OPENROUTER_API_KEY, TAVILY_API_KEY, SERPAPI_API_KEY, FINANCE_TELEGRAM_BOT_TOKEN, FINANCE_TELEGRAM_CHAT_ID, GUARDIAN_API_KEY, FINNHUB_API_KEY, EXA_API_KEY, PARALLEL_API_KEY, POLYGON_API_KEY（free tier 备用，未接入代码），FRED_API_KEY（流动性水位快照，issue 见2026-07-02状态），BRAVE_API_KEY（Brave News，issue #14，2026年已取消免费层，月度预算硬上限见2026-07-15状态），GITHUB_TOKEN（gh CLI 鉴权，见下方 Git/GitHub 章节）（DI 脚本只读此文件，不 fallback 到 ~/.hermes/.env） |
| email_sender | `~/.hermes/skills/intel/china-intel/scripts/` | 共享工具，只读借用 |
| GitHub repo | `https://github.com/PhysicalClue611/daily_intelligence` (private) | physicalclue611@gmail.com 账户，SSH alias: `github-physicalclue611` |

---

## Git / GitHub

- **Repo**: `https://github.com/PhysicalClue611/daily_intelligence` (private)
- **账号**: physicalclue611@gmail.com（与宿主机主账号 portfonia 隔离）
- **SSH alias**: `github-physicalclue611`（`~/.ssh/config`，密钥 `~/.ssh/id_ed25519_physicalclue611`）
- **本地 git identity**:
  ```bash
  git config --local user.name "PhysicalClue611"
  git config --local user.email "physicalclue611@gmail.com"
  ```
- **push 命令**: `git push origin main`（`origin` 已配置为 `git@github-physicalclue611:...`，与裸 URL 等价——但必须用 remote 名而非完整 URL push，否则本地 `origin/main` 追踪指针不会更新，会让其他 session/工具用 `origin/main..HEAD` 误判为"未 push"，见 2026-07-16 踩坑：GitHub 上其实已经真更新，只是本地指针滞后，另一个 Grok session 因此误报工作区有未 push 改动）
- **Issues 追踪**: 未解决技术债、观察中功能均记录为 GitHub Issues
- **Issue 标准格式**（2026-09-21 起，固化自 Portfonia）：Summary/In scope/Out of scope/Links 写 body，Requirements/Reasons/Exploration/Design/Contract constraints 各开一条 comment，body 末尾用锚点链接串起 5 条 comment。简单单一根因 bug 可用旧的 2 段式（问题描述在 body + 排查权衡一条 comment + 设计契约一条 comment）。完整规范和创建顺序见 `docs/playbooks/github_issue_format.md`。
- **`gh` CLI 鉴权（issue/PR 操作，2026-07-15）**：本项目禁止切换全局 `gh auth login`。`.env` 中 `GITHUB_TOKEN`（physicalclue611 PAT）通过 `GH_TOKEN="$GITHUB_TOKEN" gh <command>` 单次注入鉴权，与 git push 的 SSH alias 是两条独立通道，不要混用。
- **由 Claude Code 负责 commit 和 push**（用户不需手动操作）

---

## 调度

```
报告任务（两个独立 plist，不可合并）:
  com.daily-intel.finance.am.plist  → 5:30 AM PT = 8:30 AM ET（开盘前简报，slot=am）
  com.daily-intel.finance.pm.plist  → 5:10 PM PT = 20:10 ET（夜盘动向，slot=pm）
  注：原单 plist 含两个 StartCalendarInterval 时间，macOS launchd 只注册第一个 XPC activity，
      第二个静默丢失。2026-05-29 拆分为两个独立 plist 修复此问题。
  PM plist 自 2026-07-09 起串联 sas_review.py（issue #32，`ProgramArguments` 改为
      `/bin/bash -c "run_finance.py; sas_review.py"`），复用同一运行时点，未新增 plist。
      sas_review.py 当前 NOTIFY_ONLY=True，触发时只发邮件提醒不自动跑分析。
  非交易日: exchange_calendars 检查后静默退出

Telegram bot: com.daily-intel.finance.telegram.plist
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
0.  FINANCE_FORCE_DATE / FINANCE_FORCE_SLOT / FINANCE_FORCE_RUN 覆盖，NYSE 交易日与月度报告同档防重检查
1.  读取 watchlist、预算和价格；AM 使用盘前价，PM 使用今日收盘和盘后价，并计算单日及 3/5 交易日涨跌
2.  免费 Pass 0：intel_pass0.build_intel_snapshot(archive=False) 按标的收集 Finnhub、Google News、RSS、Guardian、SEC 8-K、Yahoo 按个股新闻（issue #111 起改用 yfinance `get_news`）；多日异动扩大各来源发表窗口；回放跳过 Yahoo（与 RSS 相同）并按提交日期重建 8-K；收集整体失败时保留价格与窗口，生成带错误记录的应急情报快照
3.  无标的异动、标的新闻或命中地缘话题则退出；否则读取 KB，收集 Sonar 宏观、社交舆情和 FRED 流动性背景
4.  代码 Pass 1：异动股最多 5 只、宏观最多 3 个话题各 3 条直链、安静个股最多 5 只各 3 条直链；直链优先，无直链才搜索。安静个股新闻量异常需至少 5 份同档历史快照；接近阈值时可选此前已报道的条目，但跨运行已抓正文的 URL 会去重。每次最多 5 次搜索；搜索结果按发表窗口过滤，安静个股使用公司名加 ticker 的新闻查询。Extract 分层、按对象生成 query，同层小组可合批，每批 ≤20 URL、3 个 chunk，清洗与正文质量门槛后回填；定时 AM 13cr、PM 用当天剩余，手动运行单独限额。
5.  将深挖正文、来源覆盖、此前已报道标记写回情报快照并存档；按异动/安静持仓/地缘话题的数量上限渲染 Pass 2 输入，加入最近 5 个既往交易日报告与发生变化的背景信号
6.  Pass 2 直接按情报快照归因，输出 Markdown；空响应走同请求重试，`finish_reason=length` 降一档 effort 一次后再 fallback，仍失败则改用代码渲染的情报快照摘要并发 TG 告警。SAS 候选提取仍为独立 JSON 调用，PM 校准仍运行
7.  写入月度 Obsidian 报告、情报快照上下文和 MemPalace；发送邮件、Telegram 报告及独立运行状态消息
```

旧 LLM Pass 1、语义过滤、轮询 job、七天围栏、`score_and_filter()` 和 `*-extract.md` 新写入已从主报告路径移除；历史归档与早期变更记录保留作溯源。

**Footer 内容（2026-06-12 起）**：邮件/Obsidian/TG 报告正文的 footer 仅含 `_Daily_Intel · {date} ET_` + IBKR 状态行（仅"需要重新授权"时显示；gateway 不可达时不显示任何提示，因 IBKR 暂时停用）。原"完全隔离"声明行和"Tavily今日剩余"计数已移除，后者改入 step 17b 的独立 TG 状态消息。

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
| 自然语言提问 | 追问流程：gemma 预处理 → yfinance/Finnhub 实时行情与新闻 → Parallel.ai 搜索抓原文（失败时用 Sonar/Exa）→ gpt-5.6-luna 回答（失败时用 grok-4.5）；详见设计文档第六节 |

## Tavily 预算

- 上限：25 credits/日，`finance_tavily_budget.json` 按 ET 日期自动重置（从 10→15→20→25 逐步调整）
- Search：basic=1cr，advanced=2cr（已弃用，全部改 basic）；Extract：**5 URLs = 1 credit**（`math.ceil(n/5)`），单次最多 20 URLs（Tavily 上限），超过就分批
- 主报告（issue #111 起）：定时 AM 上限 13cr，定时 PM 用当天剩余的全部额度，不为补跑预留。每次运行最多 5 次 basic 搜索（先给异动股，再给安静个股）。Extract 分层（异动 > 宏观 > 安静个股），额度不够时从最后一层截；补齐到 5 的倍数的 URL 最先被截
- 手动运行（`FINANCE_FORCE_RUN`/`FINANCE_FORCE_DATE`/TG 强制运行）：AM 13、PM 12，不看当天日账，单独记账于 `finance_tavily_manual_budget.json`；`sas_review.py --ticker` 手动模式每次 1cr，同样单独记账
- TG 追问不消耗 Tavily（Sonar 内建搜索）
- Tavily 断连自动 fallback SerpApi（250次/月）；两者均耗尽则跳过搜索继续生成基础报告

## KB 接入说明

- bridge URL：`http://localhost:8765`（宿主机直连）
- MemPalace 查询：`wing=paperview, room=finance`，sim~0.4
- 所有 bridge 调用 fail-open

---

## 踩过的坑

*索引（1–96）和详情都在 `docs/PITFALLS.md`。新增踩坑时，一行索引和详情都写进该文件，不要再加进本文件。*

---

## 下一步优先事项

*已结束或已失效的旧项（原 1–17）见 playbook `reference_legacy.md`。*

18. **report_pass2 xhigh 推理峰值**（PR #94，2026-09-24 起）：`grep "LLM tokens \[report_pass2" /tmp/daily_intelligence.log` 看 `reasoning=`，若超过约 25000 就重新评估 `max_tokens=32000`（目前只有 09-23 补跑一个样本：reasoning 10358，被截断那次为 15781/16000）
19. **直链解析耗时**（PR #96）：`Deepen direct leads` 日志里各标的的 `Finnhub redirects resolved a/b` 与耗时，用来决定要不要做并发解析或单标的解析上限（09-23 PM 串行 HEAD 静默 76s）
20. **Extract 补齐命中率**（PR #96）：`Deepen Extract top-up` 日志出现频率与补入条数，以及快照 `extract_topup_count`/`extract_success_count`
21. **供给事件例外效果**（PR #97）：解禁、增发/ATM、配售、指数调整是否在生效日前后都出现在报告里；生效日当天无新闻时是否漏掉，据此评估要不要做代码按日期注入的“供给事件日历”
22. **SAS 候选命中频率**（#89 之后）：`SAS候选证据日志.md` 的新增频率；#89 后 SAS 抽取输入只剩情报快照（不含 Sonar），据此决定是否把 Sonar 加回 SAS 输入
23. **issue #99 合并后由验证方看**（实现方不跑报告）：连续 3 个交易日防御性否定句比例是否低于 10%；没有事件的标的是否还单独成段；快照 `pass2` 里的 token 与 `finish_reason`；QQQM 是否还出现在仓位段
24. **issue #101**（已于 06:21 ET 重启 bot）：下次用 TG 加删个股、关键词、收件人后，确认下一节标题仍在，收件人按行分开。
25. **issue #105**（PR #108，`5b8efc8`）：下次 AM/PM 看日志 `Pass 0 sec_8k` / `Pass 0 yahoo_rss` 的耗时；8-K 是否出现在报告里并带 Item 编号；Yahoo 失败是否只留在覆盖错误里。回放召回已是 22/24。供给事件日历和 8-K 附件正文仍未做。（Yahoo 已在 #111 改用 yfinance；回放基线降为 21/24）
26. **issue #111 观察**（PR #112，`a92ff0d`，2026-09-25 起）：`grep -E "run cap|Tavily used today|manual run" /tmp/daily_intelligence.log` 看每次的额度上限和日用量（09-25 PM 用了 6/25）。快照里看 `quiet_selected`、`macro_url_count`、`extract_success_count`。看各股 `yahoo_rss` 的条数和错误，连续 3 个交易日有过半标的失败就下线这一路。看 Pass 2 的 `prompt_tokens`（09-25 PM 为 24.8k）。
27. **issue #113（PR #115，`66e1ff4`，已合并；观察中）**：
    - 原 R1（扩大每只标的的链接数）已拆到 #114，暂缓。**写 #114 的 handoff 或动手实现前，必须先问 owner 要结论。**
    - R2–R11 已合并：跨运行 URL 去重、新闻量基线至少 5 份、manual 记账文件 gitignore、状态与宏观正文修复；Extract 按标的或话题写 query，3 个 chunk、每条最多 2000 字；付费墙名单和 Yahoo 导航清洗；安静个股搜索与日期过滤；正文质量门槛；近一周与当日走势相关的旧事件可分析；已抓到正文的公司级实质事件必须写入。测试脚本全过，免费回放 22/24；未跑新的生产报告。观察 `extract_dedup_skipped`、`extract_rejected_count`、Pass 2 token 与 SPCX NASA 合同是否实际写入。
