# Daily Intelligence 状态与变更历史（从 CLAUDE.md 迁出，2026-09-25）

> 按原文迁出，未改动内容。原来在 CLAUDE.md 里按时间倒序排列（新的在上），这里保持同样的顺序。现行架构以 Obsidian 设计文档和 CLAUDE.md「当前架构速览」为准；本文件只用于追溯某次改动的来龙去脉。标着「已被 PR #89 取代」的段落，描述的机制已经从主流程里移除。

## 「打扫战场」规则的背景（原 CLAUDE.md 该规则的背景段）

**背景（2026-07-23 教训）**：全局 `~/.claude/CLAUDE.md` 的"打扫战场"清单第 4 项写的是"Obsidian 设计文档/开发日志"合并为一条，但本项目此前多次收尾只更新了`Daily Intelligence 开发部署日志.md`（叙事型日志），没有同步更新`Daily_Intel设计文档.md`（架构权威参考）——两者分别更新责任被"日志更新了"顺带带过，导致设计文档"最后更新"停留在 2026-07-09，而代码早已新增 Brave News、Polymarket/Adanos/Apify Reddit 三路社交舆情、字段消毒、quota_store 参数化重构、llm_json_utils 迁移、语义过滤模型切换等六个未记录的重大变更，用户发现后要求专项修复（本次已补齐，见文档内 2026-07-23 变更记录）。

---

## issue #87 PR1（Pass 0 影子情报快照，PR #88 已合并）

PR #88 引入 `intel_pass0.py` 和 `intel_collect.py`，当时作为报告旁路运行的影子收集器。它对 watchlist 个股（排除 QQQM/VOO/EWJ/SGOL）按标的收集 Finnhub company-news、公司名 Google News RSS、现有 RSS 和 Guardian；按别名边界匹配与标的内去重，覆盖记录保留原始条数和错误。别名可在 watchlist `## 实体别名` 写 `INTC: Intel, 英特尔`，缺失时 Finnhub profile2 补全并缓存到 gitignore 的 `entity_alias_cache.json`。Google News 每次实际 HTTP 尝试（包括重试）至少间隔 1 秒，且与 Finnhub 使用独立线程池；其 RSS 链接只作线索，不解码原文。中文别名用 ASCII 边界匹配，拉丁别名仍用词边界。多日异动时 RSS/Guardian 共享抓取窗口扩到最早标的起点，再按各标的窗口分拣；报告与回放共用 `publication_window.py` 的交易日窗口计算。PR #89 将这套收集器接入主报告路径，见下节。

PR #88 的影子情报快照只做免费收集，不调用 LLM 或 Tavily。每次运行原子写入 `archives/YYYYMM/YYYY-MM-DD-{slot}-intel-snapshot.json`，实体只保存移动、覆盖、条目和预留的 `fulltext`；条目标题与上一次运行情报快照归一化后相同则标 `seen_before`。`intel_pass0.py --replay YYYY-MM-DD --slot am|pm [--ticker SYMBOL]` 仍只建本地 JSON/Markdown 情报快照，不发通知、不写 Obsidian；回放优先用当时 context log 的盘前/日内涨跌，读不到才用日线近似；历史日线重建 3/5 日阈值和加长窗口。RSS 明确跳过，Guardian 用历史日期窗读取。24 条回溯评估集和收集召回验收入口位于 `scripts/eval/`。

PR #88 已合并；独立回放 22/24，零 LLM 调用。合并后的影子观察重点是收集召回、`seen_before` 重复率、Google News 稳定性与 Pass 0 耗时。issue #87 D5 的可验证信号与社交舆情选择留给后续决策，PR1 未触及。

---

## issue #87 PR #89（Pass 2 去套话 + PR3 切换，已合并 `65816d9`）

owner 已要求把原规划 PR3 并入 #89。主流程在价格与多日涨跌计算后调用 `intel_pass0.build_intel_snapshot(archive=False)` 收集免费信源；收集异常时生成带错误覆盖记录的应急情报快照。只有标的异动/新闻或命中地缘话题才出报告。`intel_deepen.py` 用代码选绝对涨跌最大的最多 5 个异动标的，每标的挑最多 2 条不同域名且标题命中别名的 direct/Finnhub 302 链接做 Extract；没有可用链接才搜索 `Why is {公司名} stock {up|down}`，最多 3 次 basic 搜索、10 个 Extract URL，合计最多 5 Tavily credit。SerpApi fallback 保留。正文片段和覆盖记录写回同一个原子情报快照存档；停止新写 `*-extract.md`。

`intel_render.py` 把异动标的最多 25 条（标题、来源、时间、摘要、此前已报道标记、正文与覆盖）、无异动持仓最多 8 个标题、无异动观察标的一行、地缘话题最多 8 条/话题且总数最多 40 条送入 Pass 2。Pass 2 总在有报告材料时运行，直接对情报快照归因；三状态为已知原因、线索待核实、未找到原因（附覆盖）；检索失败写“未能完成检索”。无文本/异常时发送代码渲染情报快照摘要并发 TG 告警。SAS 候选抽取读相同情报快照段落，JSON 输出格式不变；运行状态消息按情报快照来源覆盖和深挖结果显示。

近 5 个 NYSE 交易日的历史报告按异动/多日阈值标的抽取实体段落，跨月读取、同档只取首份、排除当前档，每标的最多 600 字符，提示只写新增事实。FRED 档位、个股 15% 仓位跨越和 52 周新高/低按上次成功报告的情报快照 `context_state` 比较，仅变化时注入。15% 不覆盖 QQQM/VOO/EWJ/SGOL/BOXX/CASH；上次没有该标的的权重或 52 周记录时不注入（issue #99）。社交舆情只对报告出现的标的每个注入一行。旧 LLM Pass 1、语义过滤、开放池、预留名额、异动/多日/轮询搜索 job、Brave 主流程调用及旧 RSS 分桶已移除，`llm_config.json` 删除旧两个 stage。Layer A 私有文件的旧“结论必须可操作”句运行时精确替换，其余个人原则保留。之后的后续修正见 2026-09-23 晚 / 09-24 状态段（PR #94–#97）。
---


## 当前系统状态（2026-09-25，issue #111 / PR #112，已合并 `a92ff0d`）

安静日也深挖（`intel_deepen.py`），分三层，额度不够时从后往前砍：
1. 异动股：最多 5 只，每只 2 条直链；没有直链才搜索。
2. 宏观：命中条数最多的前 3 个话题，每个话题 3 条直链；排除 `news.google.com`、`ft.com`/`wsj.com`/`barrons.com`。正文写入 `macro_digest.fulltext`。
3. 安静个股：最多 5 只。入选信号依次为：接近多日阈值（3 日 ≥8%，或 5 日 ≥9%，为覆盖 PLTR，owner 确认）；有新 8-K；新闻量异常（未报道条数 ≥8，且不低于同档最近 10 份快照中位数的 2 倍）。每只 3 条未报道过的直链。

额度与 Extract：
- 定时 AM 上限 13cr，定时 PM 用当天剩余的全部额度，不为补跑预留。每次运行最多 5 次搜索。
- Extract 每批 ≤20 个 URL（Tavily 单次上限），超过就分批。
- 手动运行（`FINANCE_FORCE_RUN`/`FINANCE_FORCE_DATE`，含 TG 强制运行）不看当天日账：AM 13、PM 12。用量记在 `finance_tavily_manual_budget.json`，不占定时额度。`sas_review.py --ticker` 手动模式同样单独记账，每次 1cr。按月计的 SerpApi/Adanos/Apify/Brave 不变。

Pass 2 渲染：抓过正文的安静标的按异动规格（25 条加摘要）；其他持仓每只 12 个标题，附 ≤120 字摘要；宏观标题附正文。

Yahoo 按个股来源改用 `yfinance.Ticker(t).get_news(count=20)`，覆盖字段仍叫 `yahoo_rss`。原来的 `feeds.finance.yahoo.com` RSS 自 #105 上线起在本机返回 404/429，一条都没取到过；当时只用回放验收，而回放会跳过 Yahoo，所以没发现（PITFALLS #96）。

实现由 Codex 完成，经 owner 审批。免费回放 21/24（owner 接受；AAOI 在 main 上同样失败）。没有改 `telegram_commands.py`，无需重启 bot。

首次运行 09-25 PM 实测：
- Tavily 用 6/25cr：1 次搜索，Extract 分 20+5 两批，成功 20 篇。
- 20 篇正文里约三分之一有实际内容。其余是 Yahoo 导航栏、边栏/页脚、Foreign Policy 付费墙、搜索返回的旧文。原因是 Extract 查询词是通用的 `financial company event evidence`，正文只取前两个 chunk、截到 1200 字。
- Pass 2 prompt 24.8k token（此前 9.7k–15.7k），报告约 1650 字（此前 1250–1430 字）。

后续见 issue #113：
- R1 待 owner 决定：要不要增加每只标的的链接数，把额度用满。写 handoff 前必须先问 owner。
- R2–R5 已确定：跨运行 URL 去重；新闻量异常基线的最少快照数；manual 记账文件加进 gitignore；几处小修。
- Extract 质量相关的候选改动已作为 comment 追加到 #113，待 owner 确认。

## 当前系统状态（2026-09-24，issue #105，已合并 `5b8efc8`）

Pass 0 增加两路免费来源。SEC 8-K：每只个股按该标的窗口查提交的 8-K，`source=SEC 8-K`，`publisher_domain=sec.gov`，`url_kind=sec_filing`，标题含 Item 编号和 accession，渲染成「公司公告（8-K Item x.xx）」；不进 Extract。CIK 来自 SEC `company_tickers.json`，缓存在 gitignore 的 `cik_cache.json`。联系人身份用 `sec_edgar_utils.sec_user_agent()`（读 `FINANCE_FROM_ADDRESS`，未设置时沿用 SAS 客户端已有的回退）。请求间隔 0.12 秒。找不到 CIK 或请求失败只记该标的覆盖错误。Yahoo 按个股 RSS：`url_kind=direct`，域名取文章真实主机，与现有标题去重并保留多个 `publisher_domains`；回放跳过，覆盖里写 `yahoo_rss: skipped in replay`。日志行 `Pass 0 sec_8k` / `Pass 0 yahoo_rss`。免费回放 22/24，两处未命中与合并前相同（09-14 PM AMKR、09-21 AM INTC）。未跑付费报告。未改 `telegram_commands.py`，无需重启机器人。blacktomb42 批准后 squash 为 `5b8efc8`（PR #108）。

## 当前系统状态（2026-09-24，issue #99 / #101，已合并 `a9b6991`）

15% 跨越只对个股，排除集合与 `_CORE_HOLDING_EXCLUDE` 相同（常量在 `pass2_context.py`，`run_finance` 再导出）。上次没有该标的权重时不注入；52 周新高/低同样要求上次已有该标的。Pass 2 提示词要求只陈述事实和传导，不逐条否定材料里没人提出的推论；持仓段只写当天有新事件、异动或已排期供给事件的标的。PM 盘后说明只在方向相反或达到异动阈值时写。`parse_json=False` 遇到 `finish_reason=length` 不重复同一请求：`reasoning.effort` 降一档重试一次（xhigh→high），再截断进入 fallback。情报快照 `pass2` 在成功时记录 `_llm_meta`，走代码摘要时记录失败原因。同一 PR 还修 TG 改 watchlist：节边界用前瞻，不再吃掉下一节标题；收件人一行一条；`_write_watchlist()` 改为临时文件加 `os.replace`，空正文不覆盖非空文件。已 squash 合并为 `a9b6991`。未跑付费报告。`com.daily-intel.finance.telegram` 已于 2026-09-24 06:21 ET 重启（PID 55749，日志 `Finance Telegram bot started`）。

## 当前系统状态（2026-09-24，Extract 补齐 / 社交舆情只查个股）

PR #95：Adanos/Reddit 只查非 ETF 个股（`_social_tickers()`），避免 `CL=F` 这类 422 消耗 Adanos 月额度。随后一 PR：`intel_deepen.py` 把 Extract URL 补齐到下一个 5 的倍数（最多 10，同一 credit 档），搜索 `max_results` 2→3；`_direct_leads()` 按标的记录 Finnhub 302 解析次数与耗时（09-23 PM 这里静默耗时 76s）。并发解析和单标的解析上限暂不实现。Pass 2 提示词新增例外：已排期的供给事件（解禁、增发/ATM、配售、指数调整）在生效日前后都要保留，不算“无进展”。

## 当前系统状态（2026-09-23 晚，Pass 2 截断修复）

2026-09-23 PM 报告在第一节半句处断掉，却按成功发出。根因是 `report_pass2`（`gpt-6-luna`/xhigh）的 `max_tokens` 仍为 16000，推理吃掉预算后 `finish_reason=length`；`llm_client.py` 只拒绝空正文，残缺正文照常返回，无重试、无 fallback、无告警。当时改为：`parse_json=False` 的截断正文一律视为失败，同模型重试、fallback，最后用代码摘要并发 TG 告警。issue #99 起 length 不再同请求重试，改为 reasoning.effort 降一档一次，再截断则 fallback。`max_tokens` 提到 32000，HTTP 超时改为 `max(180, max_tokens // 50)`。`test_llm_config.py` 30/30。Obsidian 设计文档与开发日志待宿主机 session 同步（云端 session 无法访问 vault）。

## 当前系统状态（2026-09-23，issue #82 / PR #83，已合并 `cf9cc33`）（**已被 PR #89 取代**：下述机制已从主流程移除，保留作历史）

**Extract 名额预留**。异动最多 3 个、未解释大涨最多 2 个，各自预留一条 Extract URL，不参加开放池的 `score_and_filter`。开放池预筛 25、语义过滤约 15。`tavily_extract()` 仍是每次最多 10 个 URL；预留先发，开放池再分批。满载约 20 个 URL、最多 4cr。日上限仍是 25。

必须解释的 ticker 只由 `_must_answer_tickers()` 组装一次。7 天围栏、开放池 keyword bonus、Extract 重排 query 都读这份名单，不再各自拼接类别字段。Extract 预留 query 是整份名单；开放池 query 是同一份名单加地缘词。Tavily 用这条 query 重排 chunk。

测试 `scripts/test_issue82_extract_reservation.py` 15/15，外加 #72/#76/语义过滤/#80。未跑付费报告。不改 `telegram_commands.py`。issue #74 仍未改。

## 当前系统状态（2026-09-23，issue #80 / PR #81，已合并 `9a05d3f`）（**已被 PR #89 取代**：下述机制已从主流程移除，保留作历史）

**多日累计涨跌强制追因**。个股 3 个交易日绝对涨跌 ≥15%，或 5 日 ≥20%，即使当天不是单日异动，也生成一条写明真实幅度的 basic Tavily query。与当日异动 job 去重，每次最多 2 条，排在异动之后、Pass 1 之前。不持久化“是否已解释”。商品/FX/指数 ETF（`GC=F`、`CL=F`、`^TNX`、`USDCNY=X`、`USDJPY=X`、`DX-Y.NYB`、`QQQM`、`VOO`、`EWJ`）和观察标的 `AAOI` 不进入这层。发布日期由 job 自己设定：行情第一个交易日再往前 2 个自然日，到报告日；AM 锚点比 PM 同窗口多回一个交易日。日线不足 3 或 5 个交易日时该档为空、不触发；价格表 5 日涨跌仍可用最早收盘价。全市场无单日异动且无地缘命中时，只要这层有 query，运行不退出。`blacktomb42` 三处 REQUEST_CHANGES 已在 `0d2f110` 修入后 squash。不改 `telegram_commands.py`。issue #74 仍未改。

## 当前系统状态（2026-09-22，issue #76 / PR #79）（**已被 PR #89 取代**：下述机制已从主流程移除，保留作历史）

**PM 异动追因覆盖 + Finnhub 公平截取**。AM/PM 现在都会为按 `|change_pct|` 排序的前 3 大异动生成独立 Tavily basic query；Finnhub 仅作为补充信息源，不再短路 PM 异动搜索。每个 anomaly job 带 `_anomaly_ticker`，Pass1 `{anomaly_tickers_note}` 与 issue #33 rotation 去重只使用实际生成 job 的 ticker 集合，而不是全量异动列表，因此第 4 名及以后异动仍可由 Pass1 或 rotation 搜索。

`fetch_finnhub_news()` 保留跨 ticker headline 去重与单 ticker 原始 15 条上限，改为每 ticker 各取最近 5 条后再合并展示；AM 最多请求 8 个 ticker、PM 最多 5 个不变。`TAVILY_DAILY_LIMIT` 从 20 提至 25。回归测试覆盖 6 个 PM 异动、第四名 NVDA 不被误标覆盖、Finnhub 5×5 公平截取/跨 ticker 去重和预算常量；全仓库 99/99 测试通过。未改 issue #74 的 rotation 结果同池问题。

---

## 当前系统状态（2026-09-21，issue #72 / PR #73，已合并 `4a54d39`）（**已被 PR #89 取代**：下述机制已从主流程移除，保留作历史）

**异动归因搜索精度（issue #72 / PR #73，squash `4a54d39`）**。2026-09-21 AM 正确把 INTC 盘前 +5.47% 标为异动，但 Pass 2 只能引用 Sonar 泛化归因；真实驱动是 Digitimes 首发的英特尔-友达 Micro LED 先进封装。三处缺口：RSS 无台湾半导体贸易媒体；异动 query 是 `"{tickers} stock news earnings"` 且多标的合并；INTC 被异动 / Pass1 / rotation 各查一次。

实现：① `RSS_FEEDS` 增加 Digitimes `https://www.digitimes.com/rss/daily.xml`（15 源 + Guardian）。② `_anomaly_search_jobs()` 按 `|change_pct|` 取前 3，各一条 `surge|drop + 幅度 + premarket|afterhours + reason + 日期`，`days=min(query_days,7)`。③ 7 天围栏只打在 `_anomaly_query` job 上（`_drop_stale_dated_results`，年龄相对 `now_et`，无日期放行）；合并池 `score_and_filter` 不再切 rotation 的 30 天窗。④ rotation 命中当天异动 ticker 则跳过；Pass1 `{anomaly_tickers_note}` 禁止重复建议同名 ticker。

`blacktomb42` REQUEST_CHANGES 两条均已修后合并：P1 初版把 7 天过滤放进 pooled `score_and_filter`，等于把 issue #33 的 30 天窗砍成 7 天；P2 用 `datetime.now(ET)` 会让 `FINANCE_FORCE_DATE` 补跑丢掉相对报告日仍新鲜的证据。测试 `test_issue72_anomaly_search.py` 9/9。不改 `telegram_commands.py`。

**未改（另开 issue #74）**：rotation 30 天材料仍与异动/地缘结果进入同一 `tavily_section`，Pass 2 写【价格异动】时可能串味。候选落点：只进 SAS 日志、或独立小节且禁止用来解释当日 [!]。

---

## 当前系统状态（2026-09-04，issue #69 / PR #70，已合并 `d457455`）

**PM 价格数据静默失真修复（issue #69 / PR #70，squash `d457455`，已合并至 main，远程分支已删除，issue #69 随 `Closes` 自动关闭）**。09-02/09-03 连续两个交易日，夜盘报告价格表 18 个标的的「日内↑↓（vs前收）」集体显示 `+0.00%`（此前正常噪音只有 0-5/18），用户对照手机 App 真实数据发现（PLTR 报告写"收盘 $169.46"，真实 $182.53）。根因：`fetch_prices.py` PM 分支在批量日线（`period=8d, interval=1d`）当天数据于 20:10 ET 运行时尚未落库（Yahoo 后端时序行为，非我方回归）时，`_closes_today` 为空静默回退成"批量表最后一行"（=昨天），且该 fallback 路径无任何日志；同一次运行已经正确拉取的 intraday（`period=2d, interval=1m, prepost=True`）数据里其实有正确的今日收盘（`vs今开`/`盘后`两列因此一直是对的），却被 `_, ah_price = _get_pm_prices(...)` 丢弃。

修复方向：PM「今日收盘/今日开盘」改由 intraday 提供——`_get_pm_prices()` 扩展为返回 `(today_open, today_close, ah_price)`（新增 `_opens_1m()`/`_field_1m()` helper）；批量日线只保留给 `prev_close` 和 5 日涨幅这类需要多日窗口的计算。双源皆缺（intraday 也没有今天数据）时不允许任何静默兜底——ticker 直接从 `rows` 剔除 + WARNING，`run_finance.py` 现有的失败标的检测机制自动接管、注入价格禁引声明（复用 2026-06-18 模式）。顺带修正 `week_change_pct` 的锚点从"批量表最后一行"改为 `_closes_prev.iloc[-5]`。

**协作者 `blacktomb42` 首轮 review REQUEST_CHANGES（2026-09-04），抓到 2 个真实 bug，均已在 feature 分支修复**：① `_get_pm_prices` 的"regular session"筛选只卡了 `<=16:00` 上界、没卡 `>=09:30` 下界——`prepost=True` 拉到的 04:00 盘前 bar 也满足这个条件，`opens_today.iloc[0]` 会错误地把盘前价当"今日开盘"，`vs今开`列在真实运行中会算错；已改为显式 `09:30<=time<=16:00` 双边界，并在测试 fixture 里加入盘前 bar 复现。② PM 分支所有 ticker 都因"intraday 也没有今天数据"被 per-ticker `continue` 后，`rows` 为空，函数尾部原有的 `if not rows: 走 Finnhub fallback` 逻辑仍会整表回填 regular-session 价格，绕过了刚做的硬失败设计——已改为 `slot=="pm"` 且整表落空时直接返回 `[]`，不再触发 Finnhub 回填。新增回归测试覆盖两处（含"Finnhub key 存在也不应被调用"的断言）。TDD：`scripts/test_fetch_prices_pm_intraday_source.py` 现 3/3，`test_fetch_prices_yfinance_noise.py`（10/10）无回归。不改 `telegram_commands.py`，无需重启 TG bot。第二轮 re-review APPROVE（附一条非阻断 nit：`_get_pm_prices` docstring 仍写 `<=16:00` 未提 `>=09:30`，已在合并前顺手改一行同步）。squash 合并至 main（`d457455`），远程分支已删除，issue #69 自动关闭。此前文档一度在 PR 未合并时提前写"已合并"，被 review 指出后改正——教训：状态段落只应描述已发生的事，不能预写尚未发生的合并结果。

---

## 当前系统状态（2026-08-13，issue #67 / PR #68，已合并）

**主价格 yfinance 8d/2d 假 delisted ERROR 触发巡检（issue #67 / PR #68，squash `df36a78`）**。2026-08-13 AM 05:30 ET：`yf.download(period=8d)` 14 ticker + `period=2d` 盘前 16 ticker 假 delisted，库 logger 连打 34 行 ERROR，Homepage healthcheck `finance 新错误`（阈值 1）。报告/邮件/TG 仍发出；价格表 8/18（Finnhub 补上 INTC/QCOM/SPCX/AMKR；QQQM/VOO/EWJ/SGOL Finnhub 也 SSL 超时；商品/FX 免费档无数据）。同窗 NYT RSS 握手超时，是早间出站不稳，不是退市。#63/`_quiet_yfinance_logs()` 只包了 `fetch_52week_stats`（`period=1y`），主路径没盖到。

修复：日线 8d 与盘前 2d 都走 `_yf_download()`（内部 quiet）；日线缺价 sleep 1s 再拉一次，`_merge_daily_ohlc()` 只叠回「第一次缺、第二次有」的列，不整表覆盖；`_ohlc_series` 要求列名/`Series.name` 等于目标 ticker，避免 yfinance 收成单列时把 AMKR 价写到 INTC。测试 `scripts/test_fetch_prices_yfinance_noise.py` 10/10。两轮 `blacktomb42` review（整表覆盖、坍缩列误认）均已修后 APPROVE。不改 `telegram_commands.py`，无需重启 TG bot；下次 AM/PM 自动吃到新 `fetch_prices.py`。

---

## 当前系统状态（2026-08-13，issue #65 / PR #66，已合并 + 13h 浸泡）

**KeepAlive Telegram 长轮询泄漏（issue #65 / PR #66，squash `dda8d66`）**。`com.daily-intel.finance.telegram` 每 30s 裸 `httpx.post(getUpdates)`：httpx 0.28 顶层 `post()` 已是 `with Client()`，不是忘关 Client；短命 Client+TLS 在永不退出的进程里被 macOS `MALLOC_NANO`/`TINY` 吃成不可回收碎片。修复前：冷启动 38MB；8 天 PID 21106 → 386MB（峰值 675）；同代码 6h → 459MB。本 session 开 issue、kickstart 缓解（386→30MB），实现在 PR #66。

实现：`telegram_utils` 进程级单例 `httpx.Client`；`poll_telegram()` 给 `run()`（仍无内层重试，issue #25）；连续 3 次 `TransportError` 或满 6h 重建 Client；`call_telegram` 补 `ConnectTimeout`（关掉 issue #58 / 坑 87）；满 24h 在下一轮 poll 前 `sys.exit(0)` 交给 KeepAlive。测试 `scripts/test_telegram_utils.py`。合并后已 kickstart（PID 31487，03:34:23，晚于 `dda8d66` 03:34:16）。

浸泡（同一 PID 31487）：8min 30MB → 2h 36MB → **13h10m 58MB**（峰值=当前）。旧斜率约 70MB/h，13h 应近 1GB；现约 2MB/h 残余。有 24h 日切，两周不会堆到数百兆。OpenRouter/Exa/Finnhub 的 `httpx.post` 未收口（低频、不同 host）。

---

## 当前系统状态（2026-08-11，issue #63 / PR #64，已合并）

**`fetch_52week_stats` 韧性 + 巡检降噪（issue #63 / PR #64，squash `b6acbda`）**。2026-08-10 AM 主价格 18/18 成功后，Pass2 Layer B 拉 52 周日线时 yfinance 对 INTC 瞬时假 delisted（`period=1y`），库 logger 连打 3 行 ERROR，触发 Homepage healthcheck `finance 新错误`（阈值 1）；报告/邮件/TG 仍成功发出。修复：① bulk miss/短序列 → `Ticker.history` + 1s 后再试 1 次（fail-open 保留）；② 拉取期间 `yfinance` logger → CRITICAL，失败改我们 `WARNING`。Finnhub 免费档无 candle，未做跨源 1y fallback。单元测试 `scripts/test_fetch_52week_stats.py` 12/12（含 review 后单 ticker bulk 形状覆盖）。不改 `telegram_commands.py`，无需重启 TG bot；下次 AM/PM 自动吃到新 `fetch_prices.py`。

---

## 当前系统状态（2026-08-04/05，issue #60 / PR #62，已合并 + 生产双跑验证）

**issue #60 三项改动全部实现并合并（PR #62，squash `384be56`）**：① `tg_followup`（Telegram Step 4）从 `deepseek/deepseek-v4-flash`+`thinking` 切换为 `openai/gpt-5.6-luna`（非pro）+`reasoning:{"effort":"high"}`，provider 锁定 `{"order":["OpenAI"],"allow_fallbacks":false}`，`max_tokens` 12000→16000；② `report_pass2` 同样从 `deepseek/deepseek-v4-pro`+`thinking` 切到 `gpt-5.6-luna`+`reasoning.effort=high`（同一套 provider 锁定），`max_tokens` 8000→16000，且 `report_md` 脱离 JSON 包裹（`llm_client.py::call_llm()` 新增 `parse_json=False` 模式，直接返回裸 markdown，防止截断发生在 JSON 字符串中途时把整份报告一起作废）；③ `sas_candidates`（issue #32）从 Pass2 JSON 的一个字段拆成独立的 `sas_candidate_extract` stage（`google/gemma-4-31b-it`），复用 Pass2 已组装好的上下文单独调一次，失败不再连累 report_md。六个 `google/gemma-4-31b-it` stage（`report_pass1`/`am_calibration`/`sas_candidate_extract`/`semantic_filter`/`tg_preprocess`/`tg_gap_detect`）的 `providers` 从不锁定改为锁定 `OpenInference`（`allow_fallbacks:true`，观察到真实调用此前散落在 Friendli/Crusoe/Novita/OpenInference 多个 provider）。

**Code review（`blacktomb42`，PR #62，两轮，第二轮 APPROVED）抓到一个真实 P0**：`llm_client.py::call_llm()` 的 `parse_json=False` 路径沿用了旧的 `content or reasoning_content or reasoning` 取值顺序——`finish_reason=="length"` 且 `content` 为空时（issue #53/#59/#60 反复出现的"reasoning 吃满预算"故障形态），会把部分思维链错误地当成 `report_md` 正文返回，且不触发重试/fallback。这是 PR #56 修过的 Telegram Step4 CoT-promotion bug 在新调用路径里的再现，用新的 `_resolve_content()`（镜像 `_parse_step4_response` 的既有防线：`finish_reason=="length"` 时绝不提升 `reasoning_content`）修复，primary 和 flex-fallback 两条路径都改了。同批修了 4 项非阻断建议：SAS 抽取包 try/except 隔离、hard pin 跳过 flex fallback 的行为记入注释而非静默、legacy JSON 包裹防御性 unwrap、删除死代码 `_DS_PROVIDERS`（合并后已无任何 stage 用 DeepSeek 模型）。第二轮 re-review 又指出两处非阻断残留（SAS prompt 措辞可能诱导裸 `[]` 输出浪费重试；Step4 日志误提已废弃的 `thinking.budget_tokens`），均已修复后合并。61/61 测试通过（`test_llm_config.py` 28/28，新增 5 条专门覆盖 CoT-promotion 修复的用例）。

**合并后 `com.daily-intel.finance.telegram` 已重启**（PID 变化+启动时间晚于 merge，日志确认 `Finance Telegram bot started`）。`llm_config.json`/`run_finance.py`/`llm_client.py`/`calibration.py` 改动无需重启（前者 mtime 热加载；后三者每次 launchd 触发都是全新进程）。

**真实生产双跑验证（2026-08-04）**：合并当晚用户要求"完整重跑一次今日PM报告"——当天 17:11 ET 定时 PM 报告已用旧代码（`deepseek-v4-pro`）跑过一次，合并生效后 20:22 ET 用新代码（`gpt-5.6-luna`）完整重跑一次，两次都是真实 `run_finance.py` 完整流水线执行（非模拟测试），构成一次真实（虽非受控 A/B，两次运行相隔3小时、Tavily 搜索结果不同）的生产环境对比。核心发现：`gpt-5.6-luna` 对二手信源的怀疑度处理明显更彻底（几乎每条依赖间接信源的断言都显式标注"单一来源待核实"/"同源媒体重复报道不等于独立交叉验证"，deepseek-v4-pro 同类处理明显更简略）；对"生态位验证不能直接当认知提升"这条防线也主动说得更清楚。`sas_candidate_extract` 独立调用（`gemma-4-31b-it`）正确遵守了 prompt 里"fact 字段含来源"的要求，旧的 Pass2 内嵌调用（`deepseek-v4-pro`）没有。`gpt-5.6-luna` 的 report_md 明显更长（completion 10166 vs 3262 token，约3倍）但 reasoning 占比反而更低（56% vs 74%），即更多预算转化成了可见分析而非纯思考。完整对比记录已同步更新进 Obsidian `Hermes/Homepage/LLM-Reasoning-eval设计与实现.md`（"生产环境双跑对比"节，在已有的 report_pass2 章节内原地更新，不是新增独立段落）和 `LLM-No-Reasoning-eval设计与实现.md`（§21.3 状态行更新为已实施+已生产验证，新增 §21.5 记录 `sas_candidate_extract` 的引用格式服从对照）。

两份报告均已真实发出（email+TG各一次），今日 Obsidian 月度文件因此有两条"## 2026-08-04 夜盘收市速报"，用户知情，未做去重处理（保留供对比）。

---

## 当前系统状态（2026-08-04，issue #59 / PR #61，已合并；issue #60 排查中）

**issue #59 已在 PR #61 实现并合并**（下方 2026-08-03/04 章节记录的是排查阶段，结论仍然成立，但"未改代码"等表述已过时——本章节记录实现结果）。`report_pass1`（`scripts/llm_config.py` DEFAULTS）从 `deepseek/deepseek-v4-flash` 切换为 `google/gemma-4-31b-it`（`providers` 同步改为 `None`，不再走 DeepSeek 专属的 DigitalOcean/Venice pin）。`calibration.py::_evaluate_am_predictions()` 不再复用 `report_pass1` stage，改为独立的 `am_calibration` stage（同样默认 `google/gemma-4-31b-it`）——理由与 `tg_gap_detect`/`tg_followup` 拆分为独立 stage 一致：避免未来调 report_pass1 预算/模型时静默影响这个无关的 PM 校验判断。`llm_config.json`/`llm_config.example.json` 同步更新，新增回归测试 `test_calibration_uses_its_own_stage_not_report_pass1`，`test_llm_config.py` 21/21 通过。

验证方式：用 2026-08-03 当天真实生产故障数据（真实价格表、RSS、Sonar宏观快照、AM可验证信号清单）重建两种 prompt 形状直接调用 OpenRouter 对比——`deepseek-v4-flash` 在相同条件下 4/4 复现真实故障（reasoning吃满预算，`finish_reason=length`，确认测试prompt忠实复现生产条件）；`gemma-4-31b-it` 6/6 全部 `reasoning_tokens=0`、`finish_reason=stop`，completion仅占预算16-20%，路由到3个不同OR provider均稳定；内容质量核查（非仅结构校验）确认AM报告四节结构、`[!]`标的识别、可验证信号格式、PM校验verdict/理由均合理。另用真实生产 `call_llm(stage="report_pass1")`/`calibration._evaluate_am_predictions()` 端到端冒烟测试确认代码路径正确接入。完整证据链见 issue #59 评论。

**核查剩余 DeepSeek V4 Flash 调用点（同一 PR 范围内按用户要求核查）**：仅剩 `tg_followup`（Telegram Step4，刻意保留，因为该任务需要真实推理质量，issue #11 已验证过取消 thinking 会明显降质）。用真实历史追问（07-29 INTC/PLTR/SPCX 加仓问题）重建 Step4 prompt 压测：**`tg_followup` 同样会无视 `thinking.budget_tokens=3000` 的软上限**——1/3 次直接烧穿 `max_tokens=12000`（`reasoning=11999`，`content_len=0`）。因为该 stage 已有 budget-exhausted → fallback（`x-ai/grok-4.5`）的优雅降级路径、且真实生产至今 0 次失败（07-25 起 4/4 成功），判定为低优先级、非阻断，未并入本 PR，另开 **issue #60** 跟踪。

**issue #60 后续（2026-08-04，同日追加验证）**：按用户要求验证候选 `openai/gpt-5.6-luna`（非pro）+`reasoning.effort=high` 是否适用于 `tg_followup`。用同一套真实数据测试：**7/7 全部成功**（`max_tokens=12000` 档3/3、`max_tokens=16000` 档4/4，均 `finish_reason=stop`，无一次截断）；12000 预算下有一次用到94.7%，余量偏紧，若采用建议一并把 `max_tokens` 提到16000。内容质量抽查合格，其中一次分析主动发现并正确处理了持仓快照时间戳晚于分析时点的边界情况。**候选已验证可行，未实施**——实施复杂度高于 report_pass1（OpenAI系模型走 OpenRouter 统一 `reasoning` 参数而非 DeepSeek 的 `thinking` 字段，`_step4_fallback_request()` 目前只对 fallback 模型传 `reasoning`，主模型路径需要跟着改代码；还需要把 `max_tokens` 一并调整、评估真实成本变化），建议单独排期，不建议顺手改。完整数据见 issue #60 评论。

---

## 当前系统状态（2026-08-03/04，issue #58 / issue #59，排查记录——issue #59 已在 PR #61 实现，见上方章节）

**issue #58：Telegram 发送重试逻辑漏捕获 `httpx.ConnectTimeout`**。2026-08-03 AM 报告（08:30 ET）生成、邮件均成功，但 TG 正文+状态消息两条消息均未送达——`telegram_utils.py::call_telegram()`（坑74/76 修复产物）的重试只捕获 `httpx.ConnectError`，当天实际异常是 SSL **握手超时**（`httpx.ConnectTimeout`，httpx 里与 `ConnectError` 是并列兄弟类，非子类关系），直接落进 `except Exception` 分支零重试放弃。已手动补发当次报告（读取 Obsidian 当天报告内容，复用 `send_telegram_report()` 重发成功）。已开 issue 记录根因和影响范围（`send_telegram_alert()` 告警链路本身也可能被同一根因打掉），**未改代码**，等用户确认修复方向（`except httpx.ConnectError` 扩大为 `httpx.TransportError` 或显式加 `ConnectTimeout`）。

**issue #59：`report_pass1`（deepseek/deepseek-v4-flash）隐式推理预算不稳定，同一天两次真实生产故障**。AM 报告（08:32-08:34 ET）主报告调用连续 2 次 `finish_reason=length` 截断（reasoning 占满 `max_tokens=4000` 的 100%），第 3 次险胜；PM 报告（17:10-17:16 ET）`evaluate_am_calibration()`（`calibration.py:150`，复用同一 `report_pass1` stage 做 AM 预判校验）**3 次全部失败**（同样 100% reasoning 占满预算），最终触发 `fallback_model` 成功切到 `google/gemini-3.1-flash-lite` 才拿到结果——PM 报告本身无用户可见影响（fail-open+fallback 生效），但浪费约 2 分钟重试+额外调用成本。根因与 issue #53 当年在 `tg_preprocess`/`tg_gap_detect`/`semantic_filter` 修过的缺陷模式完全一致（隐式推理吃光预算），但 `report_pass1` 从未被纳入那次修复范围，配置里"不传 thinking key 就默认不推理"的假设被今天两次故障直接证伪。

额外用真实调用测试证实**换模型/加预算都不能可靠解决**：`~deepseek/deepseek-v4-flash-latest`（同一 Pass2 级别 prompt，仅替换 `report_pass2` 的 model 字段做对比，其余 provider/thinking/max_tokens/temperature 不变）在 8000/16000/24000 三档 `max_tokens` 下 reasoning 占比分别 89%/80%/**100%（可见内容清零）**，同一 prompt 三次调用三种质变结果；对照 `deepseek-v4-pro` 当天全部 4 次真实观测 reasoning 占比 20-56%，从未越界。另有独立佐证：Obsidian `Hermes/Homepage/LLM-Reasoning-eval设计与实现.md`（另一项目 `clip_processor.py` Stage2 选型）记录了 `deepseek/deepseek-v4-flash` 开 thinking 在真实生产密度文档上 `max_tokens=20000` 仍被 91% reasoning 吃穿截断，该项目已放弃整个 DeepSeek V4 Flash 系列做长文任务，改用 `openai/gpt-5.6-luna`（非 pro）+`reasoning.effort=high`。issue #59 记录了完整证据链和候选方向（`google/gemma-4-31b-it` 或 `openai/gpt-5.6-luna` 非pro，均需用 `report_pass1` 真实 prompt 单独验证，不能直接迁移别处结论）。**此段"未改代码"为排查阶段记录，`google/gemma-4-31b-it` 已验证通过并在 PR #61 实现合并，见上方 2026-08-04 章节**；`report_pass2`（pro）证据支持维持不变。

**流程教训（记入坑记录）**：诊断 `report_pass2` 模型对比时，Bash 工具对同步命令判定超时（"exit 143"）、本地看不到输出文件，误判为"进程被杀、无任何副作用"——实际网络请求已完整发出，OpenRouter 服务端不会因本地客户端被杀而取消处理和计费，导致产生了一笔当时未察觉、未披露的真实调用（约 $0.015），直到用户提供的 OR 活动 CSV 才发现。另外在同一次排查中把"12 小时前的测试"当成"刚发生"分析，被用户指出后才用 `TZ=... date` 核实真实时间——多轮对话中不能靠对话内的相对顺序推断真实经过的时间，必须查证。见坑记录第87/88条。

---

## 当前系统状态（2026-07-25，issue #11 / PR #56）

**LLM 选型集中到 `scripts/llm_config.py` + `llm_config.json`（git 追踪，可运行时改，不需要 PR）**。issue #11 原始范围是"评估用 Grok 替换 DeepSeek 做 TG 追问 Step4 主力"，实现中范围扩大为：① 全项目 8 个 LLM 调用点（`report_pass1`/`report_pass2`/`semantic_filter`/`macro_brief`/`tg_preprocess`/`tg_gap_detect`/`tg_research`/`tg_followup`）集中为 `llm_config.py` 里的 named stage，可由项目根目录 `llm_config.json` 逐字段覆盖；② 加载器 fail-safe：文件缺失/损坏/字段非法逐字段回退默认值并记 WARNING，不让流水线崩；③ 新增跨字段校验 `_enforce_thinking_budget()`——`max_tokens` 必须比 `thinking.budget_tokens` 多至少 500，否则两个字段一起回退（防止手改配置复现 issue #53 的预算耗尽）；④ `_v_providers` 透传未知 provider key（如 `data_collection`）而非静默丢弃；⑤ `_build()`/`stage()` 用 `copy.deepcopy`，避免多个 stage 共享的 provider 默认对象被跨 stage 污染。

**`tg_followup`（Step4）**：开启 `thinking`（budget=3000, max_tokens 8000→12000），fallback `grok-4.3`→`grok-4.5`（`reasoning={"effort":"medium"}`，已用真实调用验证 slug 和参数）。

**实现过程中额外发现并修复两处真实生产 bug（不在原计划内）**：`tg_gap_detect`（Step3 P1 补搜判断）和 `tg_preprocess`（Step1 意图分类）此前都用 `deepseek-v4-flash`，用真实 prompt 实测后发现——`tg_gap_detect` 在未开 thinking 的情况下把 60-token 预算全烧在隐藏推理上，`content=None`，自身 `try/except` 静默吞掉异常返回 `None`，跟"正确判断无需补搜"完全无法区分，功能自 2026-05-23 上线起大概率就没真正生效过；`tg_preprocess` 更严重——`temperature=0` 下同一条简单指令连续 3 次调用给出 3 种不同错误结果（幻觉 action 值/预算耗尽 content 全空/JSON 中途截断），是 bot 的指令路由入口，出错等于用户"加/删指令没反应"。两处均切换为 `google/gemma-4-31b-it`（参考跨项目题库 Obsidian `Hermes/Homepage/LLM-No-Reasoning-eval设计与实现.md`，210/210 全量验证），实测零 reasoning token、输出稳定。

**PR #56 review（`blacktomb42`）修复 2 个真实 bug**：① `answer = content or reasoning_content` 在 thinking 耗尽预算时（`content=null`+`finish_reason=length`+`reasoning_content`含部分思维链）会把思维链原文当答案返回给用户，budget-exhausted 分支完全不触发——改为按 `finish_reason` 消歧，`length` 时绝不提升 `reasoning_content`；② 主力 HTTP 200 但内容为空时此前直接报错，不会尝试已配置的 fallback——改为空内容也走一次 fallback，跟传输失败同等对待。

**Review 之后又出现一条 P1 claim（"model-only 覆盖会残留不兼容的 provider pin，导致换模型失效"），实测证伪**：真实调用 `model=x-ai/grok-4.5` + 遗留的 `provider={order:[DigitalOcean,Venice], allow_fallbacks:true}`，结果 HTTP 200、`provider:xAI`、正常返回——`allow_fallbacks:true` 语义本身就会在 order 列表不支持目标模型时自动路由到支持的 provider，不会报错。对照测了 `allow_fallbacks:false`，确认这种情况下才会真 404——但那不是默认值也不是 review 给出的复现配置会继承到的值。教训与本项目一贯做法一致：**评价/验证 review claim 要拿真实调用核实，不能只看是否读起来有道理**（同 `feedback_verify_llm_review_claims`）。

**`llm_config.json` 一度被误设为 gitignore，用户当场指出没有站得住的理由，已改正**：最初照搬 `tg_offset.json`/budget 计数器那类"运行时状态"的 gitignore 套路，没意识到这个文件的性质完全不同——它是人手改的、有意图的配置决策（跟 `watchlist.md` 同类），不是机器写的临时状态；gitignore 掉之后代价是没有审计记录（`git log` 看不到改过什么）、文件丢了会静默退回 DEFAULTS没人知道、而且这个决定本身让"配置文件"从未被创建出来（功能等于没做）。已取消 gitignore、把文件（当前内容与 DEFAULTS 完全一致，零覆盖）入库，代码注释/README/设计文档里所有"gitignored"表述一并修正（`fc7b91f`，直接提交 main，因为是纯配置修正无功能改动）。

**已用真实 PM 报告验证端到端生效**（2026-07-25，周六用 `FINANCE_FORCE_RUN=1 FINANCE_FORCE_SLOT=pm` 手动跑，非交易日不影响真实定时任务）：日志显示 `LLM tokens [report_pass1/deepseek/deepseek-v4-flash]`、`[report_pass2/deepseek/deepseek-v4-pro]` 标签正确、语义过滤 `reasoning=0`（provider=Crusoe）——`llm_config` 的 stage 标签和默认值在真实流水线里生效，邮件/TG/Obsidian 全部正常发出。**踩坑**：手动跑 PM 报告用 Bash 工具默认 2 分钟超时会被杀（历史实测完整跑一次约 2m20s），需要显式加长 timeout；被杀的进程来不及执行 `finally` 清理 `run_finance.lock`，但 `fcntl.flock` 跟着进程走，进程一死锁自动释放，下次运行不受阻，锁文件里的旧 PID 只是无害的过期内容。

`telegram_commands.py` 改动已按坑32重启 `com.daily-intel.finance.telegram` 并确认加载新代码。PR #56 squash-merge（`f351dd5`），远程分支已删，issue #11 随 `Closes #11` 自动关闭。

---

## 当前系统状态（2026-07-23 晚，issue #55，留观中）

**Sonar 可观测性补齐 + BYOK 修正 issue #53 因果表述**。用户回看当天 AM 报告真实日志和 OR 活动面板带出两个独立问题，直接在 `main` 上修复（`ba1954b`，未开 feature branch/PR——用户认为改动范围小，走完整分支流程是不必要开销，本次按此简化，不代表流程惯例变更）。

**问题一**：`_sonar_macro_brief()`（`scripts/intel_sources.py`）此前 `max_tokens=800`，OR 面板显示调用 `finish_reason=length`（被截断），但代码侧无任何 `finish_reason`/`usage` 日志——同 issue #53/PR #54 刚给语义过滤补上的可观测性模式未同步到 Sonar。修复：`max_tokens` 800→1500；成功响应后记录 `prompt_tokens`/`completion_tokens`/`finish_reason`/`provider`；`finish_reason=="length"` 单独告警。新增 `scripts/test_intel_sources_sonar.py`（3/3 通过）。**留观**：下次 AM/PM 报告运行后查 `/tmp/daily_intelligence.log` 里 `Sonar macro brief` 行的 `finish_reason` 是否稳定为 `stop`，观察窗口 3-5 个交易日，issue #55 暂不关闭。

**问题二**：用户确认账号在 OpenRouter 为 DeepSeek 配置了 BYOK（DeepSeek 官方 API token）。这修正了 issue #53 排查时"`DS_OR_PROVIDERS` pin 到 `["DigitalOcean","Venice"]` 仍路由到 `Alibaba` = OR provider pin 机制不可靠"的归因——更准确的解释是 BYOK 容量/限流耗尽时 `allow_fallbacks:True` 触发的预期降级，不是 pin 失效。已在 issue #53 补发澄清评论，设计文档第 8.1 节"OR provider 实测结论"追加说明。此项已定论，不需要继续观察。

**顺带核实**：用户提供的 OR management/provisioning key 查证后确认对此类排查没有用——它只管理 API key 本身（`/api/v1/keys` 增删改查/限额），不能调用 completion 端点；连需要它鉴权的 Analytics 端点也只返回按天/endpoint 聚合统计，不含单次调用 finish_reason/provider 明细（真正的明细在每次调用响应体里，已直接记日志）。未接入代码、未写入 `.env`。

**踩坑**：本次排查中 `obsidian_read_note` 对超大文件降级为文本转储时，转储内容是 JSON 转义字符串（字面 `\n`/`\"`），不能直接当原文用于 `search_replace`，需先 `json.loads` 还原。见 `docs/PITFALLS.md#85`。

---

## 当前系统状态（2026-07-23，issue #53 / PR #54）

**语义过滤模型从 deepseek-v4-flash 切换至 google/gemma-4-31b-it，issue #53 关闭**。背景：2026-07-22 生产环境真实崩溃（`_haiku_relevance_filter()` 报 `'NoneType' object has no attribute 'strip'`）——`deepseek-v4-flash` 即使不发 `thinking`/`reasoning` key，在当前 OR 路由下仍会隐式产生 reasoning token，把 `max_tokens=80` 的预算烧在看不见的思考上，导致 `content` 返回 `null`；同批还确认 `DS_OR_PROVIDERS` 的 provider pin（`DigitalOcean`/`Venice`）并未被 OpenRouter 可靠遵守（实际路由到了 Alibaba）。issue #53 用真实付费调用对姊妹项目 LLM-eval 框架（`PC611-homepage`）测出的唯一 100% 通过模型 `google/gemma-4-31b-it` 做验证，两档 `max_tokens`（80/150）均确认 `completion_tokens_details.reasoning_tokens=0`、`finish_reason=stop`，成本反而更低，验证通过。

**PR #54 实现**：`SEMANTIC_FILTER_MODEL` 切换 + 移除不可靠的 provider pin；`_haiku_relevance_filter()` 改名为 `_semantic_relevance_filter()`（原名从未用过 Haiku，issue 最后一条评论指出的命名误导一并解决）；返回值从 `list[dict]` 改为 `(filtered, meta)`，`meta` 携带真实 provider/fallback 信息，`build_status_message()` TG 状态行不再硬编码 `"OR/DigitalOcean"`；`run_finance.py` 模块级 `DS_OR_PROVIDERS` 常量确认全文件无其他引用后一并删除（Pass 1/2 走 `call_llm()`，那边在 `llm_client.py` 有自己独立的一份）。

**PR review 抓到 4 处真实问题（协作者账号 `blacktomb42`，与此前"同账号 PhysicalClue611 发起的 Grok review"是不同的协作模式——本次是仓库的另一个 collaborator 账号，approve 状态下带 4 条 inline review）**，全部verified并修复：① `max_tokens=80` 对 prose/代码块包裹的输出几乎没有余量（本次崩溃的根因就是预算耗尽），提到 200；② 成功响应缺 usage/finish_reason 日志——这正是当初崩溃要靠额外付费调用才能诊断出来的原因，补上 `prompt_tokens`/`completion_tokens`/`reasoning_tokens`/`finish_reason` 日志，并对"`finish_reason=length` 且内容为空"的情况加显式"budget exhausted"告警；③ `sem_filter_meta` 原先用同一个 `{}` 同时表示"从未调用 LLM"（无候选/无 key）和"主备均失败"，导致 TG 状态消息把跳过路径误报成"LLM 主+备均失败"——改为 `{"skipped": "no_results"|"no_api_key"}` vs `{}` 两种语义分离；④ 缺自动化回归测试——新增 `scripts/test_run_finance_semantic_filter.py`（mock `httpx.post` 复现 `content=None`/`finish_reason=length` 的真实故障形态，5/5 通过，无 pytest 依赖，同 `test_intel_sources_sanitize.py` 的模式）。

经用户确认后 squash-merge（`a9cbdca`），删除远程分支，issue #53 通过 PR body 的 `Closes #53` 自动关闭（另补发总结评论）。

**合并流程细节**：merge 首次尝试报 "Head branch is out of date"，但 `gh pr view --json mergeStateStatus` 显示 `CLEAN`——`git ls-remote` 直接查询确认远程分支 ref 其实已经是最新 commit，是 GitHub REST API 的 `pulls/{n}.head.sha` 缓存滞后于实际 git 状态（约 1-2 分钟），并非真实冲突。用 Bash `run_in_background` 跑一个轮询 `head.sha` 匹配后退出的 until 循环等到缓存追上，再重试 merge 成功。

---

## 当前系统状态（2026-07-21，issue #38 / PR #52）

**社交舆情字段消毒，issue #38 关闭**。`_polymarket_brief()`/`_adanos_x_sentiment()`/`_reddit_sentiment_brief()`（`scripts/intel_sources.py`）此前把第三方返回字段（Polymarket question/outcome、Adanos buzz/bullish/mentions/trend、Apify ticker/signal/mentions_24h/rank_change）未经消毒直接拼进 markdown 注入 Pass 1/2 prompt——第三方响应里的换行符可伪造出假的 `##` 小节边界，误导 LLM 对 prompt 结构的解读。新增 `_sanitize_field()`（折叠空白+剔除控制字符+长度截断）和 `_sanitize_ticker()`（`^[A-Z]{1,5}$` 白名单 + 可选 `allowed` 参数做请求集合成员校验），三处注入点全部套用；Adanos/Polymarket 的 ticker 本就是调用方自己传入（可信），只有 Apify Reddit 的 `ticker` 是第三方在响应里回传（不可信），因此只有这一处需要白名单校验。

**PR #52 经历两轮真实 review 抓到 1 个真 bug（同账号 PhysicalClue611，判断是 Grok 通过该账号发起，与本项目历史上"Grok via GitHub connector"的协作模式一致）**：`_sanitize_ticker` 先 `str(value)` 再做类型判断——`str(None).upper()` 等于 `"NONE"`，恰好匹配自己写的 `^[A-Z]{1,5}$` 白名单正则，导致 Apify 返回缺失/`null` ticker 的行不再被 `if not ticker: continue` 跳过，反而以 `- NONE: signal=...` 形式渲染进 prompt——这正是修复本身引入的回归，且本人自查时只测试了畸形*字符串*输入（注入换行、超长字符串），从未测试 `None` 这个函数自己文档承诺要拒绝的输入类型。同批修复 3 条 suggestion（ticker 白名单增加请求集合成员校验、`_sanitize_field` 补充控制字符剔除+提前截断防病态大字段、Polymarket 关键字参数风格统一）+ 1 条 nit。第二轮 review 确认前 4 项修复全部生效，仅剩"缺自动化测试"一条 suggestion（非阻断），已追加 `scripts/test_intel_sources_sanitize.py`（无 pytest 依赖的纯函数断言脚本，8/8 通过）一并解决。

**教训**：这次的 bug 不是"复杂逻辑出错"，是"验证覆盖面不够"——写了针对性的对抗性测试（畸形字符串），却漏了函数自己文档里明确写出的另一类输入（`None`/`missing`）。手动等效 code review（因 `/code-review` 无法被模型直接调用）容易系统性偏向"我认为对抗者会怎么攻击"而非"这个函数完整的输入定义域"，后者才是纯函数测试该覆盖的范围。

经用户确认后 squash-merge（`f519cd5`），删除远程分支，issue #38 通过 PR body 的 `Closes #38` 自动关闭（另补发总结评论，因为 `gh issue close --comment` 在 issue 已被自动关闭后会整体失败，评论也不会发出——需要拆成独立的 `gh issue comment` 调用）。

## 当前系统状态（2026-07-20 晚，issue #41 / PR #51）

**参数化统一 6 个 budget/quota tracker，issue #41 关闭**。issue #41 的原子写入部分早前已在 `1bb3620` 修完；剩余"6份 load/save/remaining 逻辑重复、未参数化"这半部分本次解决。新增叶子模块 `scripts/quota_store.py`（`load_quota`/`save_quota`/`remaining`），只承载 Tavily/SerpApi/Adanos/Apify/Brave 五个 tracker 真正重复的样板逻辑（读JSON/校验周期键/原子写）；`budget_trackers.py` 五个函数改为委托调用，**公开名字/签名完全不变**，`run_finance.py`/`intel_sources.py`/`sas_review.py` 里的全部调用点零改动。Parallel（`telegram_commands.py`）**刻意不纳入**同一契约——它的 `load` 是 setdefault 部分schema补全（其余5个是整体重置），`remaining` 在无上限时返回 `float("inf")`（其余5个是 `max(0, limit-used)` 的int），且有一套跟其余5个都不同的美元加权双字段+独立通知冷却字段模型；只让 `save_parallel_budget()` 复用共享的原子写入，其余原样保留。设计上明确不参数化"何时计入用量"（Adanos收到任何HTTP响应就计数 vs Apify成功/超时计数+连接失败不计数 vs Brave仅成功计数）和"谁来save"（Brave自己函数内部save vs Adanos/Apify靠调用方save）——这些是已经分化的业务行为，硬塞进通用函数当flag只会制造新bug。

调查过程中额外确认两个真实bug，同一PR一并修复：① `sas_review.py::_fetch_tavily_context()` 对同一个 `budget` dict 做了冗余的二次 `rf.save_budget()`（`rf.tavily_search()` 内部已经存过一次），已删除；② `telegram_commands.py` 硬编码 `TAVILY_DAILY_LIMIT=10`，与 `budget_trackers.py` 实际生效的 `20` 不一致——TG「状态」指令的Tavily用量分母显示错了一段时间，已修正为从 `budget_trackers.py` 导入真实常量。

**PR #51 流程细节值得记录**：另一个 agent session（同一 GitHub 账号 PhysicalClue611）对本 PR 留了一条 **PENDING**（未 submit）review——网页和常规 `gh pr view`/列表 API 都看不到，必须直接调 `gh api .../pulls/{n}/reviews/{id}/comments` 才能读到内容（与此前 PR #46 的经历一致，见 issue #19 状态记录）。该 review 给出 0 bugs / 1 suggestion / 1 nit，均属实：suggestion 指出 `_build_status()` 迁移到新 loader 后外层 `try/except: pass` 已是死代码（loader 本身已 fail-open，外层 except 只会把 loader 内部真 bug 静默吞成 `used=0`），已删除；nit 指出 `budget_tracker.py`（新 primitive，单数）vs `budget_trackers.py`（已有 wrapper，复数）一字之差容易 import 写错，标注"not blocking"但顺手 rename 成 `quota_store.py`。**尝试 inline 回复 review thread 时失败**——GitHub API 报错"user_id can only have one pending review per pull request"：因为本 token 和该 PENDING review 是同一账号，回复会和这条别人未提交的 pending review 冲突，遂改用 PR 顶层 comment 说明修复对应的 commit，没有强行 submit/dismiss 别人的 pending review。

**`/code-review` 无法被 LLM 直接调用**（`disable-model-invocation`），只能用户手动触发——push 前置 hook 拦截时改为手动做等效审查（完整读diff、查未用import、查循环import、逐函数验证行为等价性），未使用 `/code-review` skill 本身。

PR `b636496` 经用户确认后 squash-merge（`b77b3f6a`），删除远程分支，issue #41 关闭。`telegram_commands.py` 改动触发已知踩坑（坑32），已重启 `com.daily-intel.finance.telegram`。

## 当前系统状态（2026-07-20）

**移除 2026-07-19 心跳文件机制（`write_heartbeat()`）**。背景：07-19 为修复外部巡检系统对非交易日/无信号跳过的误报，给 `run_finance.py` 加了 `write_heartbeat()`（原子写入 `last_run_status.json`，覆盖全部退出路径），并把巡检侧改用读取该文件的判据需求转交给巡检脚本维护 session。用户随后直接在巡检脚本里去除了对本项目的巡检——本项目不再被外部巡检监控，`write_heartbeat()` 及其全部调用点因此失去唯一消费方，属于死代码，移除。`HEARTBEAT_PATH`/`last_run_status.json` 的 `.gitignore` 条目同步移除。GitHub issue #48（心跳机制的设计与验证记录）已关闭，本次移除未新开 issue（纯粹的死代码清理，无需追踪）。**保留的历史价值**：本节上方"2026-07-19"条目完整记录了当时的根因排查（非交易日/无异动跳过是设计内行为、巡检不该内置NYSE交易日历、职责分离原则）——这套排查结论本身仍然成立，只是巡检监控范围的决策变了，不代表当时的分析有误，故不删除旧条目。

## 当前系统状态（2026-07-19 晚，issue #49 / PR #50）

**`llm_client.py`/`telegram_commands.py` 迁移至 `llm_json_utils.parse_llm_json()`**。此前 `sas_review.py` 已用这个跨项目 canonical JSON 提取/修复工具（`~/Homepage/llm_json_utils.py`，issue #15/PR #22 重写），但 `llm_client.py::call_llm()`（主调用+OR flex fallback 两处）和 `telegram_commands.py::_unified_preprocess()` 仍是重写前的手写正则（fence 剥离 + 掐头去尾找花括号），后者结尾散文带花括号时会截断错位——这正是 `llm_json_utils.py` 重写要修的那个 bug，在这两处原样复现。改为统一 `sys.path.insert(0, "~/Homepage")` + `from llm_json_utils import parse_llm_json`，与 `sas_review.py` 同一引入模式。已用真实"结尾散文带花括号"/"fence 内未转义引号需 repair"两个场景验证解析结果正确（旧正则会错位）。

**PR review 抓到一个真实 P1（同日追加修复）**：`parse_llm_json()` 文档明确声明返回类型是 `Any` 而非 `dict`——当外层 JSON 对象本身损坏（如未转义引号），但对象内部某个数组字段（`search_queries`/`tavily_queries`）单独能完整解析时，"挑最长的可解析候选"这条启发式会直接返回那个数组而不是修复外层对象。两处消费方都默认拿到的是 dict：`call_llm()` 做 `result["_llm_meta"] = {...}` 会 `TypeError`，被外层兜底的 `except Exception: return {}` 悄悄吞掉（不重试，静默丢弃一个"看起来有效"的响应）；`_unified_preprocess()` 的调用方 `run()` 紧接着做 `cmd["_raw_text"] = text`，同样 `TypeError`，但这里没有任何 try/except 包裹，会直接崩掉整个 Telegram 长轮询循环（KeepAlive 会重启，但同样畸形的响应会反复触发崩溃循环）。修复：两处均加 `isinstance(result, dict)` 判空——`call_llm()` 里改为 `raise json.JSONDecodeError(...)`，复用既有的"当作解析失败重试"路径；`_unified_preprocess()` 里改为 `raise ValueError(...)`，落进既有的 `except Exception -> {"action": "unknown"}` 兜底。已用构造出的真实触发字符串（未转义引号在前、结构完好的数组在后）复现问题并验证修复后两处均优雅降级、不崩溃。`telegram_commands.py` 改动触发已知踩坑（坑32），已重启 `com.daily-intel.finance.telegram` 并确认日志显示 `Finance Telegram bot started`。squash-merge 至 `main`（`10e1963`），远程分支已删除，issue #49 随 merge 自动关闭。

## 当前系统状态（2026-07-19）

**巡检误报修复：新增 `write_heartbeat()` 心跳文件，取代巡检脚本对 launchd exit code + Obsidian header 的反推判断**。背景：外部巡检系统对 07-18（周六）的 finance am/pm 报了 WARN——launchd exit=0 但 Obsidian 无对应 header。根因排查：07-18 是非交易日，`run_finance.py` 第一步 NYSE 交易日检查后本就会 `exit 0` 不产出报告，这是设计内行为；即使是交易日，第7步"无异动+无地缘触发"同样会静默 `exit 0`。巡检脚本靠"launchd是否成功退出"+"Obsidian有无header"两个信号反推业务状态，这个反推链条本身就是错的——用户明确否决了"让巡检脚本内置NYSE交易日历做判断"的方案（业务逻辑判断权不该下放给巡检系统）。

修复改为职责分离：`run_finance.py` 新增 `write_heartbeat(slot, outcome, date_str)`（原子写入 `~/Daily_Intelligence/last_run_status.json`，已入 `.gitignore`），覆盖 `_main_body()` 全部退出路径——`skipped_non_trading_day`/`skipped_duplicate`/`skipped_lock_held`/`skipped_no_signal`/`error`/`report_sent`，以及 `__main__` 顶层异常兜底。巡检系统改为只读这个文件的 `timestamp`+`outcome`，新鲜度够即判定正常（不关心 outcome 具体是什么），文件未按预期时间窗更新才是真正异常（进程没跑起来/卡死）。已用真实周日运行验证（`is_nyse_trading_day()` 返回 `False`，心跳正确写入 `outcome: skipped_non_trading_day`）。巡检脚本本身的改动待后续在巡检项目侧完成（不属于本项目范围）。

## 当前系统状态（2026-07-18）

**Issue #10 十日复盘 + PR #44：AM预判校准闭环的测量基础设施升级**。距 07-02 上线已运行10个交易日（07-06~07-17），首次做量化复盘：39条可验证信号，总体命中率34.4%（11 hit/21 miss/7 inconclusive），前后两段（07-06~07-10 vs 07-13~07-17）命中率从23.5%升至46.7%、inconclusive率从26%降到6%，且行为转折时间点与07-13当天写入的教训（"避免依赖事后确认的事件"）精确对齐——方向符合"自回归优化在起作用"的假设，但样本量太小（各段15-17条）+ 存在混淆变量（07-13恰好赶上美联储证词+ASML财报两个真实日历事件），不足以下"验证生效"的结论，只能算阳性信号。完整数据见 issue #10 评论。

**PR #44（issue #10 后续，分两轮提交）**：核心闭环机制（AM输出可验证信号→PM核验→知识回注AM）本身未改动，只加测量基础设施层——PM评估器新增 `resolvable_from_eod_data`（该信号能否仅凭EOD价格数据判定）和 `miss_type`（`framework_falsified` 框架证伪 vs `threshold_miscalibrated` 阈值未卡准）两个判定维度；新增结构化 `finance_calibration_log.jsonl`（gitignore、纯append）与既有Obsidian散文记录并行；`compute_calibration_metrics()` 计算滚动窗口统计；`_load_recent_calibration_notes()` 在教训原文前新增量化统计横幅（如"命中率34%，inconclusive率6%"），让AM读教训时有数字锚点——这是本次唯一触碰"闭环"本身的改动；新增TG指令「校准统计」。

**两轮code review共修复4个真实bug**：自查（`/code-review` medium）发现 `compute_calibration_metrics()` 未按日期去重，`强制运行`重跑同一天PM会导致该天信号被重复计入滚动窗口。Grok独立inline review（同一PR）额外发现3个真实bug——`compute_calibration_metrics()`对脏数据（`date: null`、非对象JSON行）无防护，且TG「校准统计」调用链上无外层try/except，脏数据能直接崩掉bot进程；`resolvable_from_eod_data`计数未绑定合法verdict分支，LLM大小写漂移（"Hit"而非"hit"）会导致`resolvable_rate`统计值超过1.0。全部修复并有针对性单元测试验证（构造脏数据/大小写漂移/重复日期等真实触发场景，非纸面推理）。经用户确认后squash-merge（`d73f24b`）+ 删除远程分支，`telegram_commands.py`改动触发已知踩坑（坑32），重启了`com.daily-intel.finance.telegram`。

**Grok review质量评价**：3个bug全部验证属实、无误报，且发现了本地自查`/code-review`遗漏的问题（自查只测了正常路径的重复计数，没测脏数据/类型漂移这类对抗性输入）；3条suggestion中2条采纳（verdict大小写归一化、LLM布尔值容错解析）、1条搁置（TG关键词短路建议，判断方向对但未意识到这是`_unified_preprocess`架构级通用缺口而非`calibration_report`独有，已开issue #45单独追踪，不在此PR内打局部补丁）；1个nit（docstring错误声称被AM banner使用）已修。

**新开issue #45**：`_unified_preprocess`（`telegram_commands.py`）全部现有指令（状态/强制运行/加删ticker等）均无关键词短路，完全依赖LLM分类，存在低概率但非零成本的误路由风险（如"校准统计"被误判为`followup`触发Parallel/Sonar付费搜索）。优先级低，视TG实际使用中是否真的多次触发误路由再决定是否动手。

**下一步观察方向**（严格遵循"每周稳定小增量"节奏，本次不做AM信号生成规则本身的改动）：① 量化横幅注入后1-2周命中率/inconclusive率滚动值是否有可归因变化；② `miss_type`分布积累到有意义样本量（预计3-4周）后，评估框架证伪vs阈值未卡准的比例，作为后续是否收紧AM规则的依据。

**Issue #19 收尾 + PR #46：跨源印证指纹漏检单token/全大写实体（同日晚间，独立于上面issue #10工作）**。背景：用户在GitHub网页发现issue #19正文被"Grok (via GitHub connector)"整个覆盖替换成一条review文本（应该是走了PATCH issue body而非POST comment，`editor: PhysicalClue611`）——用GraphQL `userContentEdits`历史找回原文并恢复，Grok review改存为独立comment后删除（因为质量一般，用户要求换Grok CLI重跑）。用户已直接告知Grok全局记住"review issue只能加comment不能改body"，不需要在本项目CLAUDE.md重复记录这条（属于Grok自身行为准则，不是本项目代码问题）。

Grok CLI 4.5 Medium重新review issue #19后，逐条验证确认属实（`_PHRASE_RE`正则结构性缺陷、真实07-17 archive里CNBC被错标"单一信源"、Pass 2 prompt "N≥1"门槛原文引用）——分类处理：① 正则漏检单token/全大写实体（"Hormuz"/"US"）→ 本issue下开PR修复；② Pass 2强断言应与普通背景事实使用不同印证门槛→ 拆到独立issue #47（设计取舍，需观察真实数据再决定，避免约束太严导致LLM输出过度保守）；③ 方向4只是轻量v1、方向5继续暂缓→ 记录不处理。

**PR #46**：新增`build_keyword_set()`（从`score_and_filter`已有逻辑提取共享），`extra_keywords`参数贯穿`_extract_key_phrases`→`_compute_corroboration`→`_source_confidence_tags`。用真实07-17 archive复现验证：CNBC案例印证数从0→4。经两轮`/code-review`（low+medium，各发现并修复cleanup findings，无correctness bug）。

**合并前又发现一个真实回归（PR #46自己引入的）**：Grok CLI对PR #46的inline review（GitHub API上以`PENDING`状态存在，未提交，故网页/API均不可见——通过`gh api .../reviews/{id}` + `.../reviews/{id}/comments`直接读取才发现）指出：复用`score_and_filter()`的宽松关键词表（含拆分词"east"/"middle"、含anomaly ticker）做跨源印证判定，会把"仅共享拆分词或ticker但完全不同事件"的两篇报道误判为"已印证"——现场用真实数据复现两个假阳性场景全部属实（东海岸停电 vs 东亚芯片需求共享"east"；同ticker财报vs CEO离职共享"INTC"）。修复：`build_keyword_set()`新增`split_phrases`参数，跨源印证路径改用`split_phrases=False`（只保留字面配置关键词，不拆分不含ticker），原始bug修复效果不受影响。详见坑记录第81条。**教训**：这个回归是本人在同一PR里引入、且被自己两轮`/code-review`漏掉的，交叉验证（哪怕是另一个LLM的review）在这里体现了实际价值，不是走过场。

**多次Grok review质量对比（同一issue/PR，4个不同来源）**：iOS App fast模式（issue级）→ 把issue body整个覆盖，且内容质量本身也一般（漏检已解决项、无代码引用）；iOS App fast模式（PR级）→ 举了一个不成立的假阳性例子（watchlist里根本没有裸"US"关键词，2026-07-16已修复的坑24就是同一类问题）；iOS App heavy模式（PR级）→ 质量明显提升，但把PR #43（issue #42）的leaf module架构工作错误地记到PR #46头上；Grok CLI 4.5 Medium（issue级+PR级inline）→ 全部claim逐条验证属实，且PR级review抓到了本人自己漏掉的真实回归。**结论**：不同触发方式/模式下同一个"Grok"品牌的review质量差异巨大，评价他人（或其他LLM）的review时必须逐条对照真实代码/配置验证，不能只看是否"读起来专业"。

Issue #19已关闭（关闭条件B：核心防护done，issue #47单独跟踪残余设计问题）。PR #46已squash merge（`89c68f6`）。

## 当前系统状态（2026-07-17）

**本仓库存在多 agent session 并发工作（2026-07-17 发现）**：观察到至少两个 agent session 各自开了 feature branch + PR 在并行工作——`fix/apify-reddit-36-37`（PR #39，修复 issue #36/#37）和 `agent/parallel-budget-controls`（PR #40，issue #7 Parallel.ai 预算控制）。**任何新 session 涉及 git 操作前，先 `git branch --show-current` 确认当前分支、`gh pr list --state open` 确认是否已有别的 session 开着 PR，不要默认自己是仓库里唯一的改动来源**，完整原则见全局 `~/.claude/CLAUDE.md`"多 Agent/多分支并发协作仓库操作原则"一节。已有 PR 的工作只汇报不擅自 push/merge。

**PR #39/#40 由 Claude 审查+合并（2026-07-17 同日）**：本仓库首次确认跨 LLM 厂商的多 agent 协作细节——PR #39 由 Grok 独立完成（self-audit 发现 issue #36/#37、开分支、修复、提 PR，全流程无人工/其他 agent 介入）；PR #40 由 Codex 起草初版（本地美元估算硬控 Parallel 用量）、Grok 在同一分支上接手推翻重写（改为"优先用 Parallel 直到真实拒绝信号（401/402/403/billing 语义错误）才降级 Sonar，本地估算仅观测，可选硬上限只做灾难闸门"，与本项目 issue #10/#24 已沉淀的"不用未经验证代理指标下判断"方法论一致）。Claude 对两个 PR 各跑一次 code-review（#39 low effort、#40 medium effort，因改动量更大），均未发现阻断级问题，经用户确认后 squash-merge + 删除远程分支，本地 `main` fast-forward 同步；PR #40 因改了 `telegram_commands.py`，按已知踩坑（坑32：TG bot 代码改动需重启才生效）重启了 `com.daily-intel.finance.telegram` launchd 服务；issue #7 因 PR #40 完整覆盖其两个候选方案（且方案本身被 Grok 迭代得更优）而关闭。**Claude 在此类仓库的角色明确为：对不同来源（各 LLM 厂商/人类）的 PR 一视同仁的最终代码审查与合并把关人**，完整协作原则见全局 `~/.claude/CLAUDE.md`"跨 LLM 厂商接力编辑同一分支/PR"一节。

## 当前系统状态（2026-07-16）

**Issue #17 第3步实现：Reddit 舆情接入（Apify Stock Sentiment Intelligence actor）**。前两步（Polymarket + Adanos）已于 06-25 落地，Reddit 一直空缺——官方 API 免费层限定非商业用途且 100 req/min，用户创建 Apify 账号（$5 一次性免费额度）后改用第三方聚合 actor `benthepythondev/stock-sentiment-intelligence`（专门做 WSB/r-stocks/r-investing 股票情绪聚合，非通用 Reddit 爬虫）。新增 `_reddit_sentiment_brief()`（`scripts/run_finance.py`），一次 API 调用批量查询全部待测 ticker（复用 Adanos 同一份 `_social_tickers` 优先级列表：异动标的优先，最多4个），减少按次收费的 actor 启动开销。定价 pay-per-event（$0.001/result + $0.00005/run），已用真实 token 实测 2-ticker 调用验证字段名（`ticker`/`sentiment_signal`/`mentions_24h`/`mention_change_pct`/`rank_change` 均为真实响应字段，非推测——对比 Adanos 当初字段名是猜的，这次直接测出来，更可靠）。`APIFY_MONTHLY_LIMIT=60` runs/月（`finance_apify_budget.json`，与 Brave 一致用原子写入，因为这个文件强制真实花费上限），60次×~4ticker预估月成本约 $0.25，远低于 $5 额度。**修了 Adanos 遗留的一个设计缺陷**：预算计数放在 `raise_for_status()` 之后才自增（Adanos 是先自增再校验，失败请求也计入免费额度消耗，见 issue #14 同类 bug）——这里因为是真金白银付费，不能延续那个模式。`social_sentiment_section` 拼接顺序：Polymarket + Adanos + Reddit + FRED liquidity，同一个 prompt 注入槽，未新增模板变量。`build_status_message()` TG 状态消息新增一行 `Reddit舆情(Apify)` 显示成功/额度。`.env` 新增 `APIFY_API_TOKEN`，`.gitignore` 新增 `finance_apify_budget.json`。已用真实 token 跑通 py_compile + 两次真实付费调用（一次 API 探测 + 一次集成后的函数级验证，各 ~$0.001-0.002），未跑完整 AM/PM 报告流水线验证端到端注入效果。

## 当前系统状态（2026-07-15）

**Issue #14 部分实现：Brave News 接入 + 跨源标题去重（commit `39b888b`）**。背景：审计历史 issue 时发现今日（07-15）AM 报告真实 Extract 存档显示 9/10 名额被 ASML 财报 + 美伊霍尔木兹两个事件的重复报道占满（4份ASML财报变体、3份Iran/Hormuz变体），当天 INTC/NVDA/AMKR、PayPal-Stripe、中国GDP三个搜索 query 一条 Extract 都没进去——证明跨源去重不是理论优化，是真实发生的功能性缺陷。

**Brave News**（`fetch_brave_news()`）：独立西方搜索引擎，非 Google 代理（区别于 Serper/SerpApi）。按 anomaly ticker + geo topic 逐条查询，最多4条/次。**Brave Search API 已于 2026 年取消免费层**，现为 $5/月预付额度用完后按量扣费（$0.003-0.005/次）——接入时加了硬性月度预算上限（`BRAVE_MONTHLY_LIMIT=800`，`finance_brave_budget.json`，到量自动停用不再调用，绝不产生额外扣费）。`BRAVE_API_KEY` 已存入 `.env`。

**跨源标题去重（`score_and_filter()`）——方案推翻重来的真实教训**：最初提议字符级相似度（`difflib.SequenceMatcher`）+ 24h时间窗口，用真实 Extract 存档数据验证时**实测完全失效**——同一 ASML 财报事件的标题变体（"raises 2026 forecast" vs "hikes sales forecast"）字符相似度只有 0.33-0.73，远低于任何安全阈值，因为不同媒体用词和语序都不同。改用 token 重叠度（Jaccard，去停用词），且**限定只在标题命中同一个 tracked ticker/geo 关键词时才比较**（避免不相关新闻因共享通用财经词被误判重复）——这个组合在真实数据上验证有效：正确合并了 ASML 集群里2条最冗余的表述，同时正确保留了"美国重启霍尔木兹封锁"（地缘行动本身）vs"油价上涨"（市场反应）这类同一事件的不同报道角度（token重叠度低，不应合并，实测未被误删）。

同时把 issue #19 方向4（wire原文优先/视频路径降权）以最小成本折进同一处打分逻辑：`/video/`、`/watch/` 路径给 -0.08 惩罚，无需硬性排除规则。

**Issue 清理**：本次会话核实关闭 6 个 issue——#3（SPCX价格管道已自愈）、#6（不再推进IBKR翻转）、#8（KG已下线，纯向量版wontfix）、#9（KG vocab重复副本确认已清理）、#18（重复报告fix已生效验证）、#26（FRED流动性快照已实现，issue描述与代码不符）。核实 #17（Polymarket+Adanos社交舆情）已完整落地并有真实数据佐证（用真实key验证Adanos字段名猜测正确）。

**次日 `/code-review` 修复7个真实bug（2026-07-16，commit `fa01f34`）**：对上面的 Brave News + 跨源去重代码跑 `/code-review`，8个findings全部verified，修了7个（1个budget-tracker三胞胎重复代码技术债延后）。最关键的一条：`score_and_filter()` 关键词锚定原先靠拆分话题标签本身（"US-Iran"→"us"/"iran"），既漏（"Hormuz"单独出现时锚不上，正是07-15 PM报告里实际漏网那对重复项的根因）又误报（"us"子串命中"focus"/"trust"）。改为直接用 watchlist.md 里已经维护好的完整 `geo_keywords` 字典，并把多词短语（"Strait of Hormuz"）拆出关键单词各自入锚。另外三个是真实防护漏洞：`fetch_brave_news()` 的 geo_topics query 被 `max_queries` 二次截断吞掉（AM slot 8个ticker时永远查不到地缘话题）；budget计数在 `raise_for_status()` 之前自增，key失效也照样烧硬上限；Brave自己的结果从未经过跨源去重。全部用真实存档数据+真实API调用验证，不是纸面推理。详见 issue #14 评论。

**`gh` CLI 鉴权方式确认**：本项目禁止切换全局 `gh auth login`，`.env` 中 `GITHUB_TOKEN` 通过 `GH_TOKEN="$GITHUB_TOKEN" gh <command>` 单次注入使用，详见下方 Git/GitHub 章节。

**尚未验证**：以上改动已通过 py_compile + 复现真实故障场景的单元测试 + 真实 Brave API 调用验证，但未跑完整付费 AM/PM 报告流水线。下次真实运行后可 `grep "score_and_filter\|Brave News" /tmp/daily_intelligence.log` 确认线上去重命中率和 Brave 数据质量。

---

## 当前系统状态（2026-07-13 晚）

**手工 PLTR SAS 报告 + `sas_review.py` JSON 解析健壮性修复（issue #32 follow-up）**：手工 `--ticker PLTR` 跑通一次（$0.0538，已写入 `Finance/SAS_Review/PLTR.md` 并发邮件；注：PLTR 在 watchlist.md 里但不在 `sas_tracked_tickers.json` 自动追踪列表里，两者是独立的清单）。跑的过程中 OR log 出现两次计费调用——`_call_sas_review_llm()` 第一次调用返回的 JSON 里 rationale 字段含未转义引号，`json.loads` 硬失败，脚本原有重试逻辑直接整次重跑（含费用，第二次又花了一遍钱）。修复：解析失败时先用 `json_repair.repair_json()` 尝试就地修复，只有连修复都失败才计入 retry 消耗新的付费调用。已用真实故障字符串（rationale 内嵌引号）验证修复生效。新增依赖 `json-repair==0.61.4`（`requirements.txt`）。commit `e73ba78`。未开 issue（一次性健壮性加固，无后续观察点）。

**追加重构（同日）**：用户提出应维护一个跨项目通用的 LLM JSON 清洗/修复工具，而非每个项目各写一份、且不想被特定 LLM/通道锁定。评估后同意——`json_repair`本身已经是 provider-agnostic 的修复算法，真正该复用的是"从 markdown 代码块摘出 JSON blob + 严格解析失败后修复 + 记日志"这段 glue 逻辑。canonical 版本存放在 `~/Homepage/llm_json_utils.py`（该目录本身就是各独立项目共享工具脚本的存放地，无 monorepo，靠 `sys.path.insert` 跨仓库 import，不是 pip 包）。`sas_review.py` 改为 `sys.path.insert(0, "~/Homepage")` 后 `from llm_json_utils import parse_llm_json`，原先内联的 extract+parse+repair 代码删除，`_call_sas_review_llm()` 简化为一行调用。注意：这条 cross-repo import 只在宿主机路径有效，`sas_review.py` 本身也从不在 Hermes MI 容器里跑（`_IN_CONTAINER` 分支只影响 Obsidian 路径），两者互相印证不冲突。`json-repair` 依赖仍需装在 Daily Intelligence 自己的 `.venv`（import 共享文件不会带装依赖）。已用真实 markdown 代码块+未转义引号的完整场景重新跑通验证。

## 当前系统状态（2026-07-12）

**OpenRouter 调用接入归属 Header（跨项目通用约定，同步存入 `~/.claude/CLAUDE.md`）**：用户在 OR 后台发现 Hermes 调用显示 "App - Hermes Agent" 标签，本项目调用无标签。原因是 OR 通过 `HTTP-Referer`（必需）+ `X-OpenRouter-Title`（可选，日志里 App 名称来源）两个 header 做归属；本项目 3 个脚本共 11 处 OR 调用均未设置。修复：`run_finance.py`/`telegram_commands.py` 各自定义 `OR_ATTRIBUTION_HEADERS` 常量（`sas_review.py` 复用 `rf.OR_ATTRIBUTION_HEADERS`），全部调用点 `headers` dict 用 `**OR_ATTRIBUTION_HEADERS` 合并。commit `16ccae9`。未开 issue（跟 issue 先行原则的豁免条件一致：一次性机械改动，无后续观察点）。

**追加修正（2026-07-13）**：commit `16ccae9` 把 `HTTP-Referer` 写成纯字符串 `"PhysicalClue611"`，当天 AM 报告 OR log 4 条调用 App 全部显示 "Unknown"，一度误判为"忘记部署"。查官方文档（`https://openrouter.ai/docs/app-attribution`）确认 `HTTP-Referer` 必须是合法 URL 格式，纯字符串会被 OR 静默丢弃整个归属（`X-OpenRouter-Title` 连带失效）。修正为 `"HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence"`（两文件同步改），TG bot 已重启生效；AM/PM 报告脚本按 launchd 每次从磁盘直接执行，下次运行自动生效，无需额外部署步骤。`~/.claude/CLAUDE.md` 全局约定和跨项目 memory `reference_openrouter_attribution_headers` 同步修正——Portfonia 项目大概率有同样问题，未验证。commit `afc69ec`。**已验证**：同日用最小成本 curl（`google/gemini-3.1-flash-lite`，$0.000003）直接调用新 header，OR dashboard 确认 App 列正确显示项目名+仓库链接。已写好一份可复用的自检提示词（检查其他项目 OR 调用是否有同样 `HTTP-Referer` 格式问题），交给用户手动分发给其他项目 session，未在本项目仓库内留存文件。

## 当前系统状态（2026-07-09 下）

**移除条件代号引用，改自然语言自解释（issue #34）**：用户反馈 AM/PM 报告 prompt 和 `Finance/Investment Operating Manual v1.0.md` 里大量用代号引用边界条件——Manual 第6节"条件A/B/C"、prompt 里①-⑧编号、Manual 第9节"第6条的条件A"这类文档内跨引用，代号时间长了记不住。排查还发现编号方案已经腐化的实证：`VERIFIABLE_SIGNALS_INSTRUCTION_P2` 标签写"⑤"，实际拼接位置在模板里是"⑧"之后。修复：Manual 第6节条件A/B/C 改纯描述性标题（Alpha大幅兑现/出现更高赔率机会/仓位结构性超载），第7.4/第9节内部跨引用改为直接复述规则内容；`run_finance.py` `USER_PROMPT_TEMPLATE_P2` 的①-⑧编号改为描述性粗体小标题，互相引用处改为内联复述。`Daily_Intel设计文档.md` 第7.1/7.1b/十二节同步更新旧编号描述。已存跨项目 memory `feedback_no_coded_references`（决策规则用自然语言自解释，不用字母/数字代号互相引用）。commit `0db1759`，issue #34（已关闭）。

**依赖文档接入 GH repo（docs/ + templates/ 分层）**：`Finance/Investment Operating Manual v1.0.md`、`Hermes/Daily Intelligence/Daily_Intel设计文档.md`、`Hermes/Daily Intelligence/Layer_A_Prompt.md` 三份文档此前只存在于私有 Obsidian vault，脚本靠 `OBSIDIAN_PATH` 在运行时读取，repo 里完全没有对应内容——开源后其他实现者无从参考。按内容分类接入：`docs/design.md` 是设计文档快照（本身面向独立实现者写的，直接原样入库，去掉含个人笔记标题的 frontmatter，文件头附一句"与 Obsidian 权威版本手动同步"的说明）；Manual 和 Layer_A_Prompt 不含任何持仓数字/账户信息，作为 `templates/investment_operating_manual.example.md` 和 `templates/layer_a_prompt.example.md` 入库（起点模板，供新用户复制到自己 Obsidian 后按个人情况修改，脚本本身不读这两个模板文件）。`sas_tracked_tickers.json`/`sas_review.lock` 补进 `.gitignore`（运行时状态，此前遗漏）。README 补充这三份文件的位置说明、`sas_review.py`/`sec_edgar_utils.py` 补进 Project structure 清单（此前 README 完全没提过 SAS 系统）。

---

## 当前系统状态（2026-07-08）

**Investment Operating Manual v1.0 接入 AM/PM 报告（issue #30，2026-07-08 当晚真实 PM 报告验证后追加修正）**：⑤/⑦ 措辞首次上线后用户反馈"能力圈内/外"黑话令人困惑，且逻辑不对称——圈外明确"不构成操作依据"，圈内却只说"可以讨论含义"，容易误读成"圈内=有操作依据"。修正为直接指代【能力边界】清单（"清单内/清单外"，不再用"能力圈"术语），并重新划清分工：⑤ 只决定能不能讨论战略含义，不决定该不该动；⑦ 才是唯一允许产出加减仓建议的依据来源，⑤ 的讨论不能替代 ⑦ 的核对结果。commit `86d47a5`。新写的 `Finance/Investment Operating Manual v1.0.md`（能力边界/Expectation Gap/SAS/Portfolio Construction 完整决策框架）此前完全不在日报注入链路里——`_load_framework()` 一直读的是旧的 `金融资产信息.md` 摘录。本次改为直接从 Manual 提取三段运行性规则（第2节能力边界、第6节 Portfolio Construction 含认知提升标准/减仓条件A/B/C、第7.4节 Expectation Gap 内部信号清单），注入 Pass 2 Layer B；Pass 2 prompt 新增两条分析要求：⑤能力圈内外标注（圈外驱动因素须显式标注"不构成操作依据"，不得暗示操作）、⑦持仓类异动核对清单（须对照认知提升/减仓枚举条件逐条核对，不满足则明确声明不构成依据）。不做 SAS 自动打分（仍为人工季度任务，见 issue #32）。实测提取内容 2498 字符、Pass2 prompt 组装正常，未跑完整付费流水线（避免当日报告重复）。同批还创建了 issue #31（SAS候选证据日志）、#32（季度财报深度分析脚本，>2%持仓 + earnings-triggered + 手工指定ticker，模型选用 claude-sonnet via OR、需报告实际花费、LLM 直接打分），均待实现。

**SAS 候选证据日志接入（issue #31）**：Pass 2 prompt 新增 ⑧号规则 + `sas_candidates` JSON 字段——命中 Manual 第7.4节内部信号清单（内部人增持/资本配置持续性/生态位验证/监管语言变化/历史先例）或第6节认知提升标准三条之一、且涉及实际持仓标的时，输出 `{ticker, category, fact}`（category 为枚举字符串，非自由文本）。新增 `write_sas_candidate_log()` append-only 写入 `SAS候选证据日志.md`，复用 issue #10 校准记录的通用 append 助手，fail-open，纯证据队列不参与自动打分。测试用 scratch 文件验证了正常写入/缺字段过滤/二次append不截断三种场景。commit `137b192`。

**每日情报搜集阶段补齐（issue #33）**：#31 上线后用真实 INTC 报告内容测试，`sas_candidates` 全为空——排查发现根因是搜集阶段结构性缺口：AM/PM 搜索完全由价格异动/地缘关键词触发，对核心持仓"没异动但该主动查"的情况（认知提升三条标准）和纯计算类信号（股价相对位置、仓位占比）完全没有覆盖。补齐三项：① `fetch_prices.py::fetch_52week_stats()` 计算52周区间百分位+距历史高点回撤（纯计算，零成本，替代 LLM 从文本自行估算"高位/低位"）；② `_get_portfolio_weights()`/`_compute_holding_signals()` 计算持仓占组合%（减仓条件C判断的既定事实，⑦号规则已更新为直接读取该值而非自行估算）；③ `_rotation_search_job()` 每日轮询一个核心持仓（AMKR/INTC/NVDA/QCOM/TSLA，日期取模确定，无需状态文件）主动生成认知提升相关搜索，追加在异动/地缘/LLM查询之后，只消耗剩余 Tavily 预算不抢占真实信号的额度。SEC EDGAR（Form 4/10-K语言变化）作为 13F/期权数据一起，明确留给 #32 季度深度分析场景，不进日频流水线。测试：真实数据验证核心持仓解析（AMKR/INTC/NVDA/QCOM/TSLA）、权重计算（INTC 10.4%等）、52周统计、7天轮询确定性（每个标的固定命中同一天）、prompt 组装、fetch_52week_stats 空列表/无效ticker 边界情况均通过。

**Issue #32 设计定稿 + 数据源验证（2026-07-09）**：季度财报深度分析脚本设计敲定五处细化（追踪列表按核心个股永久持久化不因回撤/卖出移除、依赖 #33 的权重函数、数据源、价格数据"打分不引用但需真实锚点"、失败后当日重试+TG报警而非静默抛错）。实测三个原假设数据源：Finnhub 机构持仓（13F）免费key返回权限错误，付费tier功能；yfinance 期权链 impliedVolatility 数据确认损坏（bid/ask全0，IV呈规律翻倍的占位符模式，非真实定价）——两者均从 v1 范围移除。新发现 `edgartools`（已装入 `.venv` 并更新 `requirements.txt`）可免费按 ticker 直接拉取 Form 4 内部人交易明细（含 transaction code 可区分公开市场买入 vs RSU归属等routine事件）和 10-K risk factors 结构化正文，替代了两项原计划信号源，无需自建 SEC 全文检索。13F 按机构申报非按标的，反向聚合工程量大，v1 明确跳过。详见 issue #32 完整评论。尚未开始写 `sas_review.py`。

**Issue #32 实现完成（2026-07-09）**：`scripts/sas_review.py`（新脚本）+ `scripts/sec_edgar_utils.py`（封装 edgartools 调用）。核心组件：①持久化追踪列表 `sas_tracked_tickers.json`（原子写入，核心个股权重>2%永久加入不因回撤/卖出移除）；②财报触发判定（Finnhub calendar/earnings + exchange_calendars 计算"财报后第3个交易日"，AMC/BMO区分反应起始session）；③fail-closed财报锚点（Finnhub /stock/earnings，同日重试耗尽后 `send_telegram_alert()` 报警而非静默失败）；④Form4内部人买入（`edgartools`，仅取code='P'公开市场买入，排除RSU归属等routine事件）+ 10-K risk factors跨期对比（原文交给LLM语义diff，不自建diff算法）；⑤`~anthropic/claude-sonnet-latest` via OR直接打分SAS四维度，用OpenRouter `usage.cost`字段拿真实花费（无需硬编码价格表，2026-07-09验证该字段存在）；⑥每ticker一份md文件原子append（读全文+temp+os.replace，比run_finance.py现有的`_append_calibration_entry()`更重但匹配这份数据的多季度比较价值）。真实测试：INTC完整跑通一次（$0.0525），正确引用10-K措辞变化（"foundry strategy"→"external foundry strategy"）和真实52周价格数据，正确处理"无内部人买入"为中性事实而非编造，遵守"打分不引用价格"纪律。该次真实结果已写入 `Finance/SAS_Review/INTC.md` 并发送邮件，作为INTC第一条历史记录。补充两道围栏（commit `4752231`）：`_has_today_entry()` 防重复（今天已写入历史文件则跳过）+ `_acquire_lock()` 并发锁（复用 run_finance.py 模式）。新增 `NOTIFY_ONLY` 默认模式——自动扫描触发时只发邮件提醒（含手工执行命令），不自动花钱跑分析，待观察几个真实季度触发逻辑稳定后再改 `NOTIFY_ONLY=False` 转全自动；`--ticker` 手工模式不受影响。新增 `--exclude TICKER` 支持清仓后永久移除追踪（历史文件保留，未来重新建仓超2%权重会自动重新追踪）。**已接入 launchd（2026-07-09）**：`~/Library/LaunchAgents/com.daily-intel.finance.pm.plist` 的 `ProgramArguments` 改为 `/bin/bash -c "run_finance.py; sas_review.py"`（`;` 分隔，前者失败不挡后者），复用原 20:10 ET 运行时点，未新增独立 plist。`plutil -lint` 校验通过，`launchctl unload`+`load` 重新加载生效。当前 `NOTIFY_ONLY=True`，触发时只发邮件提醒不自动跑分析。

## 当前系统状态（2026-07-06）

**`call_llm()` 429 限流修复（2026-07-06，issue #29）**：夜盘收市速报生成失败——OpenRouter 返回 `429 Too Many Requests`，`call_llm()` 把它当普通不可重试的 4xx 直接 `return {}`，既不重试也跳过了 OR flex fallback，导致 Pass 1 空手而归、报告静默跳过未发送。429 是限流性质的瞬时错误，修复为与 5xx 同等对待（`status_code >= 500 or status_code == 429`），走 exponential backoff 重试，耗尽后落入 flex fallback。已手动补跑当次报告成功。见踩坑记录第80条，commit `c985b0c`。

## 当前系统状态（2026-07-02）

**Telegram Bot 容错全面重构（2026-07-01/02，issue #20/#21/#22/#23）**：用户提供的定时巡检报告显示 `telegram_commands.py` 5天内 3.4万条超时警告+207条SSL EOF+多次409冲突。四层排查：① `_tg()` 客户端 timeout(10s) 短于 getUpdates 长轮询服务端等待(30s)，几乎每次空轮询自断触发 409（issue #20，修复：`timeout=POLL_TIMEOUT+5`）；② 排查中意外发现 httpx INFO 日志把 Telegram/Finnhub/Guardian 凭据明文写入 644 权限的 `/tmp` 日志文件（issue #21，修复：两脚本均 `logging.getLogger("httpx").setLevel(WARNING)`）；③ 修复①后巡检又报警，验证证明不是回归而是本机 Shadowrocket TUN 隧道对 `api.telegram.org` 域名特定的 ~25-30% 瞬时连接失败率（对照 Slack/OpenAI 同隧道零失败确认域名特定），此前被①的噪音淹没（issue #22，修复：`ConnectError` 快速重试一次）；④ 用户指出重试补丁只覆盖轮询未覆盖发送（`send_telegram_report`/`send_telegram_alert`），要求容错覆盖全部调用路径且日志级别反映"是否需要人关注"（重试成功=INFO，耗尽才WARNING）（issue #23，修复：新建 `scripts/telegram_utils.py::call_telegram()` 共享函数，两脚本统一调用）。方法论已存为跨项目 memory `feedback_uniform_fault_tolerance.md`。见踩坑记录第74-77条。

**Sonar 宏观快照防过时/防幻觉（2026-07-02，issue #24）**：AM报告 Sonar 快照声称"WTI破$100"，实际价格$68.58——Pass2 LLM 自己核对发现矛盾并修正，但机制上无防线。修复 `_sonar_macro_brief()`：① OR payload 加 `search_recency_filter: "day"`（实测确认 OpenRouter 透传给 Perplexity，同一查询加参数前后价格准确度显著改善）；② 注入 pipeline 已算好的 `price_table` 作为权威锚点，冲突时以此为准；③ prompt 强制每条断言带时间戳，无近24h更新须明说不得编造。`telegram_commands.py::_sonar_research()` 同步加固。见踩坑记录第78条。

**getUpdates 轮询改无状态单次调用（2026-07-02，issue #25）**：用户复查issue #22/#23的同步重试方案后指出"太重"——轮询循环本身每~30s自然重跑，循环节奏就是现成的重试机制，不需要单次调用内再套一层。改为：拉不到就静默跳过，`sleep(5)`交给下一轮；持续失败满30分钟才升级为WARNING（而非每次重试耗尽就报）。生产验证：日志格式从"recovered after N retry(ies)"变为"recovered after Ns"（真实停机秒数），零WARNING，单次失败完全不留痕迹。`sendMessage`类调用（无自然重试兜底）不受影响，仍用`call_telegram()`同步重试。见踩坑记录第79条。

**AM 预判校准闭环（2026-07-02，issue #10）**：把"盘后对比版本"从独立报告改造成闭环学习机制。AM报告Pass 1/2 prompt新增条件指令（仅AM slot），报告结尾固定追加"## 可验证信号"小节（2-4条条件-结果式可核验断言）；PM pipeline新增`evaluate_am_calibration()`步骤（报告定稿后、写入Obsidian前），定位当天AM报告的该小节（复用#25教训的日期戳边界定位模式），用一次DeepSeek V4 Flash调用（~$0.0005）对照实际价格/新闻判定hit/miss/inconclusive，提炼"知识条目"（教训而非罗列对错）。默认不进报告正文，评估步骤自行判断是否"重要到该展示"（给方向性原则而非硬规则，观察一段时间）。今天(07-02)的AM报告是旧prompt生成、无"可验证信号"小节，今晚PM运行会静默跳过，机制从明天AM报告起真正生效。Issue #10 保持open，观察1-2周真实数据。

**AM 预判校准知识改为 Obsidian 为主，不依赖 MemPalace（2026-07-02 同日修正）**：用户指出 MemPalace `finance` room 最近多次全部重建，持久化内容应多留在 Obsidian 并要求考虑备份。复查发现最初"AM 通过现有`get_finance_context()`的 MemPalace 搜索自动捞到校准知识"是未经验证的假设（那个搜索是通用query，非针对校准知识，且对 MemPalace 不可用零容错）。修正：新增`_load_recent_calibration_notes()`直接读 Obsidian`预判校准记录.md`（不经bridge/MemPalace）注入AM prompt（新模板变量`{calibration_notes}`）；新增本地备份镜像`backups/预判校准记录_backup.md`（已gitignore），与Obsidian独立写入，读取时Obsidian缺失自动fallback到本地备份；MemPalace drawer保留但降级为非必需的锦上添花层。用删除模拟文件的方式验证了fallback正确工作。commit `b1df5c4`。

**市场见顶预警框架 + FRED流动性快照（2026-07-02，issue #26）**：用户分享一份YouTube视频总结的"市场见顶先行指标"，评估后认可两根支柱——流动性水位（准备金/SOFR-RRP利差/TGA/SRF）和产业资本开支二阶导数（"思科悖论"），其余指标（0DTE占比、内部人减持比、前十大集中度、纳指前瞻PE、未定义的"4%经典指标"）降级为背景参考或直接排除。整理成活文档`Hermes/Daily Intelligence/市场见顶预警指标.md`，按【正常/观察/警戒】三档+分资产操作指引表达，明确定位"参考背景，非清仓触发"。流动性三项（准备金/SOFR-RRP/TGA）接入FRED免费API自动化（`fetch_liquidity_snapshot()`，`FRED_API_KEY`），折进`social_sentiment_section`同一注入槽，Pass 2新增第⑥条分析要求约束LLM只能给出与该tier匹配的克制建议。SRF无干净免费数据源，保留人工检查。首次实测：SOFR-RRP利差16bp已达【警戒】（持续两周非单日噪音），准备金/TGA正常。commit `dae2494`。

**踩坑记录结构重组（2026-06-30）**：CLAUDE.md 踩坑记录从完整叙事（每条80-250 tokens，累计17.5K字符）改为一行索引+详情文件指针，完整叙述迁至 `docs/PITFALLS.md`（git-tracked，按需 grep/Read，不自动加载每个 session）。CLAUDE.md 全文从71.2K降至49.3K字符（-31%）。KG 相关8条历史踩坑标注"已下线子系统"归档。新增踩坑一律遵循此规范：这里加一行索引，详情写 `docs/PITFALLS.md` 对应分类小节。

## 当前系统状态（2026-06-18）

**开源准备（2026-06-18）：IBKR 代码注释禁用（`_ibkr_auth_note()` / `_fetch_ibkr_prices()` 均返回 `""`，函数体保留供将来本地启用），`ibkr/` 目录从 git tracking 移除（`git rm -r --cached`），`.gitignore` 将 `ibkr/` 整目录排除。隐私清理：硬编码邮箱地址改为 env var（`FINANCE_FROM_ADDRESS`），用户名路径全部改为 `$HOME/`，`memory_context_finance.py` 中个人姓名从 MemPalace query 移除。**

**yfinance 早间瞬时故障修复（2026-06-18）：新增 `_finnhub_single_ticker()` helper（`fetch_prices.py`），在 bulk download 返回 0 行的单 ticker 上自动触发 per-ticker Finnhub fallback；AM slot 进一步尝试 `yf.Ticker.info.preMarketPrice`（不同 Yahoo 端点，transient 故障期间通常仍可达）。`run_finance.py` 价格表生成后检测 `failed_tickers`，非空时向 Pass 1 / Pass 2 prompt 注入价格禁引声明，阻止 LLM 幻觉价格。根因：bulk download 失败不抛异常，原代码无 per-ticker fallback，受影响 ticker 静默丢弃 → LLM 从零散 context 编造价格数字（INTC 昨收出现 $183.53 幻觉）。见踩坑记录第 71 条。**

## 当前系统状态（2026-06-16）

**LLM 持仓幻觉修复（2026-06-16）：`_load_personal_context()` 头部新增权威声明，明确「IB美股持仓快照是唯一持仓依据，价格表中未出现的标的均为观察标的，框架文本中的计划建仓不等于当前持仓」。根因：`金融资产信息.md` Dream Bucket 章节含"为SPCX建仓做准备"文本被注入 Pass 2，LLM 把"计划"当"现实"，错误进一步被 MemPalace 历史报告上下文强化。watchlist 监控标的 ≠ 持仓，是全局原则，修复在提示层。**

## 当前系统状态（2026-06-15）

**可观测性补丁（2026-06-15）：`call_llm()` 新增 provider 日志和 `_llm_meta` 返回字段（`{model, provider, attempts, fallback, primary_attempts}`），新增 `send_telegram_alert()`（fail-open）在 `report_md` 为空或 `main()` 崩溃时主动推送 TG 告警（不再无声失败），`build_status_message()` 的 LLM/Provider 段改为显示实际成功的 provider 和重试次数（`_fmt_llm_meta()`）。见踩坑记录第69/70条及当日开发日志详述 OR provider 路由三层结构：[DeepSeek + DigitalOcean/Venice] × 最多3次重试 → [Gemini flex fallback] × 1次。**

## 当前系统状态（2026-06-12）

**KG triples 系统全面下线（2026-06-12）：Layer 3（Knowledge Graph 实体关系图谱）整体移除，系统回退为两层知识体系（Obsidian 全文 + MemPalace 向量检索）。删除 `kg_extractor_finance.py`（报告后三元组提取，526行）。`memory_context_finance.py` 重写：移除谓词三层分类常量（FRAMEWORK_PREDICATES/EVENT_PREDICATES/SKIP_PREDICATES/_ALWAYS_ON_PREDICATES）、`_kg_query`/`_score_triple`/`_fmt_triple`/`get_kg_monitor_hits`/`_load_entity_alias_map`/`_resolve_query_names`，`get_finance_context()` 签名移除 `all_tickers`/`news_text` 死参数，仅保留 MemPalace + Obsidian 两段。`run_finance.py` 移除：两处 KG import、`_write_price_snapshot()`（价格快照直写）、`_tg_notify()`（伴随其唯一调用方一并移除）、两个 prompt 模板中的 `{kg_monitor_section}` 占位符、step 5b（KG monitor_item 主动触发，含 `news_mentioned_tickers`/`kg_monitor_hits`，skip 条件简化为仅 anomaly/geo）、step 12（KG 提取）和 12b（价格快照写入），步骤重排为 0-13。`telegram_commands.py` 移除：`import functools`、五个 KG vocab 函数（`_load_entity_alias_map`/`load_kg_vocab`/`normalize_entity`/`_filter_entity_candidates`/`persist_pending_vocab`）、`_kg_query_bridge()`、`_filter_framework_triples()`、`_write_followup_triples()`；`_unified_preprocess` prompt 和 `_preprocess_question` 移除 `relevant_entities` 字段；`_llm_followup()` 移除 KG 决策框架三元组注入段和 `===KG===` 内联写回指令及响应解析逻辑。三文件均通过 py_compile + import smoke test。TG bot 已重启（`launchctl stop/start com.daily-intel.finance.telegram`）。**

**附带修正：TG bot launchd label 纠正**：项目文档历史上多处写作 `com.hermes.finance.telegram`（坑21、32、调度章节），实际 launchd label 为 `com.daily-intel.finance.telegram`（`launchctl list | grep finance` 验证）。本次重启命令已用正确 label，文档同步修正。

**决策动机**：KG 三元组系统自 2026-05-17 起经历多轮迭代（词表注入、写回保护、object 质量规范、6维查询分解评估等，详见踩坑记录25/33/37-39/50/53-69），复杂度持续累积但价值未达预期（见坑66 死端节点问题、坑68 fallback 集合遗漏）。下线后系统回到 Obsidian + MemPalace 两层架构，降低维护面。原 KG 相关踩坑记录保留作历史参考，标注为已下线子系统。

**Footer 精简 + TG 独立运行状态消息（2026-06-12）**：`finance_footer()` 移除"与中国企业情报（[Hermes MI]）完全隔离：独立收件人、独立数据源、独立预算。"声明行和"Tavily今日剩余"计数，footer 简化为仅 `_Daily_Intel · {date} ET_` + IBKR 状态行。`_ibkr_auth_note()` 的 gateway 不可达分支（`except Exception`）改为返回空字符串——IBKR 暂时停用，报告中不再提示"gateway 未运行，操作 login.sh"；"需要重新授权"分支（gateway 可达但未认证）保持不变。新增 `build_status_message()` 函数 + main() 新增 step 13b：将 Tavily/SerpApi 本次用量与剩余额度、情报源状态（RSS+Guardian 条数 / Finnhub 即时新闻 / Sonar 宏观快照 / Tavily+SerpApi 搜索与 Extract 结果数）、LLM/Provider 清单（Pass1 / 语义过滤 / 宏观快照 / Pass2，均标注 OR + `DS_OR_PROVIDERS`）拼成独立 Markdown，通过 `send_telegram_report()` 作为单独 TG 消息发送；邮件正文和 Obsidian 月度文件不受影响。三处改动均通过 py_compile + smoke test 验证。

**注**：上条（2026-05-30 状态段内、原 2026-05-26 footer 描述）"未认证或不可达时报警"已部分过时——2026-06-12 起 IBKR gateway 不可达分支不再报警，仅"需要重新授权"分支保留报警，见本条。

## 当前系统状态（2026-06-02）

**KGTriples 审计修复（2026-06-02）：对照 `MemPalace_KGTriples_改造计划.md` 做系统性审计，修复10项偏移。`persist_pending_vocab()` 签名规范化（source_script 改为参数）并在内部过滤 new_predicates len > 20；新增 `_filter_entity_candidates()`（new_entities 过滤 len > 30 + 括号含数字/百分比）；`_build_system_prompt()` 和 Step 4 prompt ===KG=== 段新增 object 字段约束（禁止顿号列表、形容判断词、条件句；investment_view 只写状态词）；`_filter_framework_triples()` fallback 集合补入 `driven_by`/`correlated_with`；`_mempalace_context()` 改为同时查 finance + hermes 两个 room（日报历史在 hermes）；`_unified_preprocess` 新增 `relevant_entities` 字段，追问 KG 查询扩展至非 ticker 具名实体；KG 注入顺序修正（KG 块移至向量上下文之前）；`_fmt_triple()` 加 confidence 标注（conf < 0.8 时显示）；event predicate cap 3→5（driven_by/correlated_with 被系统性挤出问题）。B7（KG 实体引导二次向量搜索）写入优化计划待实现。**

**KG object 质量问题发现（2026-06-02）：PM 报告产出 10 条 LLM 提取三元组，3 条存在 object 死端节点问题——分别是条件句型操作指令（investment_view）、顿号合并的两个短语（driven_by）、含条件判断的分析句（trend 未注册谓词）。三条均通过长度检查（13/22/25 chars），根因是 prompt 未明确禁止这些模式。已在两处 KG prompt 补充三项禁止规则。见踩坑记录第 66 条。**

## 当前系统状态（2026-05-30）

**情报输入层持久化（2026-05-30）：`run_finance.py` 新增 `write_context_log()`（step 11b）和 `write_extract_archive()`（step 9b）。Context Log 写入 Obsidian `Daily_Intel_context_YYYYMM.md`（价格快照 + 触发 RSS 条目 + Sonar 宏观 + 搜索任务，被 mine）；Extract Archive 写入 `~/Daily_Intelligence/archives/YYYYMM/YYYY-MM-DD-{slot}-extract.md`（清洗后 Tavily 全文 + Layer 2b 候选，Obsidian 之外，永不被 mine）。两处均 fail-open。`filtered` 和 `extract_results` 变量初始化提前至 `if raw_results:` 块之前，保证 archive 函数在块外可访问。`ARCHIVE_DIR = _PROJ_DIR / "archives"` 常量已加。**

## 当前系统状态（2026-05-28 晚）

**MemPalace per-day drawer（2026-05-28）：`mempalace_bridge.py` 新增 `POST /mempalace/add_drawer` 端点（使用 `mempalace.palace.get_collection` 直接写 ChromaDB，幂等，WAL 安全）。`run_finance.py` 每次报告写入 Obsidian 后自动 POST 一个 `日期+slot` drawer 到 `wing=paperview, room=finance`。历史 44 个 section（04/05 月）已通过 `scripts/backfill_drawers.py` 顺序回填（2s/条）。从此 MemPalace 语义检索粒度从月度文件级降至每报告级。**

**Pass 2 深度推理重构（2026-05-28）：Pass 2（DeepSeek V4 Pro）改用独立 `USER_PROMPT_TEMPLATE_P2`（去掉4节硬格式，改为要求+围栏，JSON 只输出 `report_md`）+ `SYSTEM_PROMPT_P2`（Layer A，从 `Layer_A_Prompt.md` 动态读取）+ `_load_personal_context()`（Layer B：持仓均价 + 投资框架，注入 user message）。`call_llm()` 新增 `system_prompt` 参数。`Layer_A_Prompt.md` 文件存放于 `Hermes/Daily Intelligence/`，可在 Obsidian 直接编辑，下次报告自动引入。Pass 1 路径零改动。**

**已上线。每交易日两次自动运行（开盘前 + 夜盘）。已接入 MemPalace/KG。Telegram 双向控制已启用。TG 追问四步流水线（V4 Flash + yfinance实时行情/新闻 + Parallel.ai + V4 Flash）已上线。SerpApi 已接入为 Tavily 日配额耗尽后的 fallback。Finnhub 全面接入：① yfinance 价格和新闻的 fallback（telegram_commands.py）；② 定时报告 step 6b 注入 watchlist 股票的即时新闻（run_finance.py），异动标的优先、最多8个ticker、免费无配额。LLM 调用层已加重试（网络/5xx 自动 2 次重试）。支持 FINANCE_FORCE_DATE / FINANCE_FORCE_SLOT 手动重跑。时区已改用 ZoneInfo（冬令时自动处理）。所有 LLM 提示词已注入当前时间（%Z 动态 EDT/EST）并要求以 NYSE 时区推理。追问流水线内联 KG 三元组写回（`===KG===` 分隔，fail-open）。追问中注入 yfinance session-aware 实时行情（Ticker.info 盘前/盘后/常规，与 Yahoo Finance app 同源）和 yfinance.news；Sonar 接收价格上下文；V4 Flash 有时间线推理约束。KG 词表清理：删除冗余价格谓词（stock_price/price_change_pct/stock_price_change），别名合并至 price_level/had_move_pct；LLM 提取器和追问流水线均加价格谓词硬拦截。POLYGON_API_KEY 已存入 .env（free tier 仅延迟数据，暂不接入代码；Starter $29/月起支持实时）。IBKR Client Portal Gateway 已接入（Java 11，port 5001，launchd 管理）：隔夜/周末时段（20:00-03:50 ET + 周末）以 IBKR 为主力实时数据源，工作日交易时段（04:00-20:00 ET）以 yfinance 为主力、IBKR 为 fallback；yfinance/Finnhub 在非交易时段均标注"非实时"。每次 AM/PM 报告 footer 自动检查 IBKR 授权状态，未认证或不可达时报警，提示运行 `~/Daily_Intelligence/ibkr/login.sh`。触发重登场景：iOS App 登录踢出 gateway（最常见）、~30 天 server-side 过期、gateway 进程崩溃。PM 报告时间已调整为 5:10 PM PT（20:10 ET，NYSE 盘后结束后 10 分钟）。**

**DeepSeek 全面迁移至 OR/Novita（2026-05-21）：所有 DeepSeek 调用从直连 `api.deepseek.com` 迁移到 OpenRouter + `DS_OR_PROVIDERS = {"order": ["Novita"], "allow_fallbacks": True}`。理由：提示词含个人金融数据，直连 DeepSeek 暴露数据给第三方，通过 OR/Novita 走 fp8 量化版本可兼顾隐私与成本。覆盖范围：`run_finance.py call_llm()`（Pass 1/2）、`_haiku_relevance_filter()`（语义过滤）、`telegram_commands.py _deepseek_post()`（TG Step 1）、Step 4 自管重试、`kg_extractor_finance.py`（KG 提取）。`DEEPSEEK_API_KEY` 不再用于 LLM 调用，`DEEPSEEK_BASE_URL` 常量已移除，全部使用 `OR_BASE_URL`。fallback 路径不变：OR/Novita 失败 → gemini-3.1-flash-lite/gemini-3.5-flash via OR flex。`thinking: {type: disabled}` 保留（DeepSeek Flash 模型必要参数，OR 透传给 Novita）。**

**DeepSeek 直连 OR flex fallback 历史记录（2026-05-20，已被上条替代）：所有 DeepSeek 直连调用点在耗尽重试后自动 fallback 到 OpenRouter flex 模式（`service_tier: "flex"`）。映射：v4-flash → `google/gemini-3.1-flash-lite`；v4-pro → `google/gemini-3.5-flash`。Finnhub fetch 加 1 次 timeout 重试（等 3s 后重试，无 fallback，fail-open）。触发场景：5:30 AM ET DeepSeek SSL 全程不可达约 15 分钟，导致 AM 报告失败，手动在 10:20 AM 补跑。**

**TG 追问 Step 4 模型更换（2026-05-20）：`REASONING_MODEL` 从 `anthropic/claude-sonnet-4-6`（locked to Azure）改为 `~anthropic/claude-sonnet-latest`（OR always-latest alias，无 provider 约束）；新增 `REASONING_FALLBACK = "x-ai/grok-4.3"`，主模型任何失败时自动切换。原因：Azure 放弃了 Sonnet 的路由支持，`allow_fallbacks: False` 锁死导致 400 在 05-18 和 05-20 各触发一次。OR 波浪号前缀（`~model`）表示"always-latest alias"——OR 维护的动态指针，始终指向该系列当前最新版本，无需手动追版本号。当前实际路由到 `anthropic/claude-4.6-sonnet-20260217` via Google，延迟实测 2.8s。**

**Sonar fallback 完整链（2026-05-20）：所有 Sonar 调用先重试 1 次（等 5s），再进入 fallback。step 6c（报告宏观快照）：Sonar → 重试 → `""` 空节（不走 Exa，RSS 14源 + Tavily Extract 已覆盖宏观面）。Exa API key 存入 `~/Daily_Intelligence/.env`（`EXA_API_KEY`），调用端点 `https://api.exa.ai/chat/completions`。**

**TG 追问流水线全面重构（2026-05-21）：Step 3 从 Sonar 改为 Parallel.ai search + extract（主），Sonar 降为 fallback；Step 4 历经 Claude Sonnet → Gemini 3.5 Flash（因格式过于机械否决）→ DeepSeek V4 Flash via OR/Novita（最终，同日 05-21 又因隐私原因从直连迁移至 OR/Novita）。成本 ~$0.025 → ~$0.010/次（-60%）。追加改进：5节硬指令→参考建议（自由展开）；系统提示禁对话体开场白；Step 1 生成2条互补 query（事件角度 + 量化/技术角度）；extract 增至3 URL、4000 chars、跨文章段落去重；Step 4 绕过 `_deepseek_post()` 自管重试以确保 model_label 精确、Grok 4.3 fallback 正确触发；`_append_followup` 标签动态化（Sonar→research_source）；KG JSON 解析健壮化（支持多行数组和逐对象格式）。Parallel.ai key: `PARALLEL_API_KEY`，SDK `parallel-web==0.6.0`，计费：Search $5/1k、Extract $1/1k。**免费额度为一次性 $20 credit（约 20,000 requests），用完需充值，谨慎使用。****

**追问流水线三项优化（2026-05-23）：① P1 自适应第三条 query：Parallel 搜索成功后，V4 Flash 判断是否存在明显盲区（缺价格路径/市场反应/基本面解释之一），有则生成第3条补漏 query 并再次调用 Parallel（+~$0.0001 + 可能 +$0.005）；② P2 aggregator URL 优先：extract 前按域名排序，stockanalysis.com/macrotrends.net/finviz.com/tipranks.com/finance.yahoo.com 等聚合页面优先进入 extract 列表，提升单次 extract 的信息密度；③ 日报 Pass 1 prompt 加 hint：对持仓 ticker 的个股查询建议加 `site:stockanalysis.com` 或 `site:macrotrends.net` 以偏向结构化历史数据。**

**KG 提取器词汇表注入（2026-05-25）：`kg_extractor_finance.py` 和 `telegram_commands.py` 新增 `load_kg_vocab()`（`@functools.lru_cache`），从 `~/.hermes/kg_vocab/{predicate_vocab.json,entity_aliases.json}` 动态加载规范词汇；`kg_extractor_finance.py` 新增 `_build_system_prompt()` 生成带词汇表的提示；triple 格式升级为 `{"triples":[...], "new_entities":[], "new_predicates":[]}` 结构化对象，每条 triple 增加 `derivable/derivable_reason/scope/source_type/inference_chain` 字段；`_parse_triples()` 兼容新 object 格式和旧 array 格式，过滤 `derivable=false` 和 `confidence<0.5`，scope 为日期格式时写入 `valid_to`；LLM 提议的 `new_entities/new_predicates` 记录到日志（不自动写入 vocab 文件）；`_write_followup_triples()` 同步支持新格式和相同过滤规则；`_call_api()` 新增 `system_msg` 参数覆盖默认系统提示；修复 `kg_extractor_finance.py` 中 `_HOME` 未定义 bug（影响 standalone CLI 路径）。技术债：`load_kg_vocab()` 在两个文件各一份副本，未来抽取到 `kg_vocab_utils.py`。**

**`_preprocess_question` 漏传 search_queries bug 修复（2026-05-23）：`_unified_preprocess` 生成的2条互补 query 在 `_preprocess_question` 返回时被丢弃（未包含 `search_queries` 字段），导致 `_llm_followup` 的 `ctx.get("search_queries")` 始终为 None，退化为单条 query。该 bug 从流水线上线起就存在，2条互补 query 设计从未生效。修复：`_preprocess_question` 返回 dict 加入 `"search_queries": pre.get("search_queries") or []`。修复后"情报检索"步骤显示"2条查询"。教训：发现数字或行为与预期不符时，不能以"不在本次计划范围内"为由放过，必须立即查清楚。**

**语义过滤器与 KG 提取器切换至 DeepSeek V4 Flash（2026-05-18，2026-05-21 迁移至 OR/Novita）：原为 Haiku 4.5 via OR（$1/$5 per MTok），切回 DeepSeek V4 Flash 后降至 $0.07/$0.28 per MTok（约 22 倍成本差）。函数名保留 `_haiku_relevance_filter`，常量改名为 `SEMANTIC_FILTER_MODEL`。2026-05-21 因隐私原因进一步从直连迁移到 OR/Novita，成本略升（OR 抽成约 5-10%），但个人金融数据不再直接暴露给 DeepSeek。KG 提取 fallback：gemini-3.1-flash-lite flex。**

**KG 三元组全面接入报告与追问（2026-05-18）：`memory_context_finance.py` 完整重写，引入谓词三层分类（框架类/事件类/跳过类）；KG 注入从"仅异动 ticker 取 3 条"扩展为"全部持仓按谓词类别差异化注入"；字符预算从共享 1500 提升为分段独立（KG 3200 / MemPalace 1200 / Obsidian 800，总上限 6000 chars ≈ 1500 tokens）。新增 `get_kg_monitor_hits()` 实现主动发现：RSS 命中新闻的 ticker → 查 KG monitor_item → 子串匹配，命中则触发报告并注入 `kg_monitor_section`（新增第三个 skip 豁免条件）。TG 追问流水线新增 `_kg_query_bridge()` + `_filter_framework_triples()`，对 `relevant_tickers` 查 KG 并注入 Claude user message。写回保护：`kg_extractor_finance.py` 引入 `_safe_write_triple()`——框架类谓词（action_state/exit_trigger/max_position_cap 等）硬拦截，事件类谓词 7 天去重，防止 LLM 提取→写回→再读的正反馈自激荡回路。谓词常量在 `memory_context_finance.py` 单一定义，`telegram_commands.py` 直接 import。**

**LLM 调用层重构历程（2026-05-17 → 2026-05-21）：2026-05-17 从 OR 迁移到 DeepSeek 直连（省抽成）；2026-05-21 因隐私原因全部迁回 OR/Novita。DeepSeek V4 Flash 默认开 thinking 模式会导致 content 为 null，已全部加 `thinking: {type: disabled}`（`call_llm()` 和 `_deepseek_post()` 自动注入）。Pass 2（deepseek-v4-pro via OR/Novita）单独保留 thinking，`budget_tokens=3000`，`max_tokens=8000`，timeout=180s。Sonar provider：`{order: [Perplexity], allow_fallbacks: False}`。**

**KG 提取全面升级（2026-05-17）：提取模型从 gpt-oss-20b（偶发 null content）改为 claude-haiku-4-5 via OR（主力）+ deepseek-v4-flash 直连（fallback），各自有独立 retry（Haiku 2次，DeepSeek 1次）。System prompt 统一谓词：所有价格涨跌一律用 `had_move_pct`，禁止 price_move_pct 等变体。报告和追问完成后各发一条独立 TG 通知（`KG: +N 条三元组写入`）。KG 总量 615→830 条，已回填 5 月全部缺失日期。Hermes Agent SOUL.md 新增 kg_query 财经三元组专项规则：必须逐条引用返回的三元组，不得以"无记录"代替。**

**KG 价格快照直写（2026-05-17）：新增 `_write_price_snapshot(price_rows, date_str)` 函数，在每次报告生成后（step 12b）直接从 `price_rows`（fetch_prices 已有数据，无额外 API 调用）向 KG 写入全部 watchlist ticker 的 `price_level` 三元组。AM 写盘前价，PM 写收盘价 + 盘后价（如有）。不依赖 LLM 提取，非异动 ticker 也有完整价格记录。每次报告约写入 16 条，计入 TG 通知的 `KG: +N` 总数。修复了"KG 只记录异动但不跟踪绝对价位"的结构性缺口。**

**fetch_prices.py 已支持 slot 感知（2026-05-13 更新）：AM 报告两列——昨日全日↑↓（昨收vs前日收，与 Yahoo Finance 严格一致）+ 盘前涨跌（盘前价vs昨收）；PM 报告三列——今日表现（今收vs今开）+ vs昨收 + 盘后涨跌。session_change_pct 字段区分交易日内与收盘后变动。report_date 参数 + 日期过滤确保跨午夜/FORCE_DATE 重跑时正确选取日线数据。PM 收盘价强制使用日线官方收盘（非 intraday 1m 最后봉），与 Yahoo Finance 严格一致。**

**Tavily 情报拉取已升级为四层架构（2026-05-13）：Layer 1 = basic search（全部 basic，不再用 advanced）；Layer 2a = score_and_filter（脚本打分 N→15）；Layer 2b = _haiku_relevance_filter（Haiku 语义过滤 15→10，识别上下游供应链和宏观传导，非仅 ticker 匹配，~$0.0001/次，fail-open）；Layer 3 = tavily_extract（批量全文抽取，1cr/5URLs，10 URLs=2cr，chunks_per_source=2，600字/chunk）。Pass 2 LLM 拿到全文 chunk 而非 250-char 截断摘要。search 与 extract 均支持 start_date/end_date 精确时间过滤。**

**Sonar 宏观快照已接入（2026-05-13）：AM 和 PM 报告均新增 step 6c，调用 `_sonar_macro_brief()` 生成实时多源宏观简报（perplexity/sonar via OpenRouter）。query 从 watchlist 动态构建（持仓 + 地缘主题），随 watchlist 变化自动演化；AM 聚焦过去12h隔夜发展，PM 聚焦当日盘面驱动 + 盘后/隔夜风险；portfolio 快照注入 system prompt 实现个人化。~$0.005/次，fail-open，注入 Pass 1 和 Pass 2 两个 LLM prompt。**

**新闻源扩充至 14 个 RSS + Guardian API（2026-05-19）：RSS_FEEDS 新增 Reuters/AP/WSJ（通过 Google News RSS `site:` 过滤间接获取，<1h 延迟，Google 基础设施可靠），实测各源各 30 条，总量 245→294。Guardian Open Platform API（`content.guardianapis.com/search`，免费 500次/日）作为独立新闻源并入，`fetch_guardian_news()` 函数 fail-open，key 存 `GUARDIAN_API_KEY`。`run_finance.py` RSS 聚合后合并 Guardian 结果并按时间重排。**

**RSS 扩展至 11 个源（2026-05-13）：新增 CNBC、MarketWatch（财经速报）、Foreign Policy（地缘战略深度）、Al Jazeera（中东/非西方视角）、Seeking Alpha（个股机构分析）。Politico 403 已排除。实测 48h 窗口 245 条文章（原 6 源 ~130 条）。**

