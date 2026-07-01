# Daily Intelligence — 项目记忆

每日财经情报系统，独立于 Hermes Agent（`~/Hermes`）运行。

**本项目将开源（2026-06-18 决策）。** 代码维护须遵守开源标准：无硬编码邮箱地址、无用户名路径（一律 `$HOME/`）、无 broker 专属目录（`ibkr/` 已从 git 中移除）。API key、收件人等均通过环境变量或 Obsidian 配置文件注入，不得写死在代码中。

---

## [强制] Session 初始化

**每个新 session 开始时，无论用户第一句话是什么，必须先读以下两个文档，再做任何其他操作：**

1. `Hermes/Daily Intelligence/Daily_Intel设计文档.md` — 权威架构参考，API key 路径、LLM 选型、流水线细节均以此为准
2. `Hermes/Daily Intelligence/Daily Intelligence 开发部署日志.md` — 近期变更与踩坑记录

**若用户第一条消息已明确指定"阅读文档"，该指令必须立即执行，不得跳过或延后。**

CLAUDE.md 仅作快速索引，两文档不一致时以 Obsidian 设计文档为准。

---

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

### 目录结构

```
~/Daily_Intelligence/
├── CLAUDE.md                          ← 本文件
├── scripts/
│   ├── run_finance.py                 ← 主入口
│   ├── fetch_prices.py                ← yfinance 价格拉取（宿主机运行）
│   ├── fetch_news.py                  ← RSS 聚合（NYT/BBC/FT，httpx+feedparser）
│   ├── finance_email.py               ← Resend email client
│   ├── memory_context_finance.py      ← KB 上下文注入（bridge REST API）
│   ├── telegram_commands.py           ← Telegram 双向指令控制（long polling）
│   └── migrate_reports.py             ← 一次性迁移旧日报格式到月度文件
├── .venv/                             ← 宿主机专用 Python 环境
├── finance_tavily_budget.json         ← Tavily 每日计数（自动重置）
└── tg_offset.json                     ← Telegram getUpdates offset 持久化
```

**注：`ibkr/` 目录不在 git 中（broker 专属，不随代码分发）。IBKR 相关代码已保留但禁用（`_ibkr_auth_note()`、`_fetch_ibkr_prices()` 均返回空字符串），可通过恢复函数体并在本地挂载 `ibkr/` 模块重新启用。**

### 依赖外部资源

| 资源 | 路径 | 说明 |
|---|---|---|
| 监控配置 | Obsidian: `Hermes/Daily Intelligence/watchlist.md` | 手工编辑或 TG 指令修改 |
| 月度报告 | Obsidian: `Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_YYYYMM.md` | 脚本 append 写入 |
| 持仓快照 | Obsidian: `Finance/portfolio_report_latest.md` | portfolio-agent 覆盖更新 |
| Gmail token | `~/.hermes/token.json` | 借用 Hermes 的 OAuth token |
| API keys | `~/Daily_Intelligence/.env` | OPENROUTER_API_KEY, TAVILY_API_KEY, SERPAPI_API_KEY, FINANCE_TELEGRAM_BOT_TOKEN, FINANCE_TELEGRAM_CHAT_ID, GUARDIAN_API_KEY, FINNHUB_API_KEY, EXA_API_KEY, PARALLEL_API_KEY, POLYGON_API_KEY（free tier 备用，未接入代码）（DI 脚本只读此文件，不 fallback 到 ~/.hermes/.env） |
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
- **push 命令**: `git push git@github-physicalclue611:PhysicalClue611/daily_intelligence.git main`
- **Issues 追踪**: 未解决技术债、观察中功能均记录为 GitHub Issues
- **由 Claude Code 负责 commit 和 push**（用户不需手动操作）

---

## 调度

```
报告任务（两个独立 plist，不可合并）:
  com.daily-intel.finance.am.plist  → 5:30 AM PT = 8:30 AM ET（开盘前简报，slot=am）
  com.daily-intel.finance.pm.plist  → 5:10 PM PT = 20:10 ET（夜盘动向，slot=pm）
  注：原单 plist 含两个 StartCalendarInterval 时间，macOS launchd 只注册第一个 XPC activity，
      第二个静默丢失。2026-05-29 拆分为两个独立 plist 修复此问题。
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
0.  环境变量覆盖：FINANCE_FORCE_DATE → 强制报告日期；FINANCE_FORCE_SLOT=am|pm → 强制时段；FINANCE_FORCE_RUN → 绕过交易日/防重检查
1.  NYSE 交易日检查 → 非交易日退出（FORCE_DATE/FORCE_RUN 时跳过）
2.  确定 run_slot（am: hour<18 / pm: hour≥18，FORCE_SLOT 优先）和 slot_label（开盘前简报/夜盘动向）
3.  防重检查：月度文件中搜索 "## {date} {slot_label}" → 存在则退出
4.  读取 watchlist.md
5.  fetch_prices(slot=run_slot)：
    - AM：日线数据取 prev_close + week_change；另拉 2d/1m prepost=True 取盘前最新价作为 price；change_pct = (premarket - prev_close) / prev_close
    - PM：日线数据取今日收盘为 price；另拉 1m prepost=True 取 16:00-20:00 ET 最后 bar 为 afterhours_price；is_anomaly = close_anomaly OR ah_anomaly
    - fallback：两级，yfinance失败→Finnhub（无盘前/盘后，记日志）；format_price_table(slot) 输出对应列
6.  RSS 聚合（14个 feed + Guardian API，过去24小时）→ 计算 triggered_geo_topics（RSS 命中的地缘政治主题列表）；Guardian key 存在时合并 Guardian 结果并重排
7.  代码层 skip：无 anomaly 且无 triggered_geo_topics → 静默退出（不调用 LLM，零成本）
8.  bridge 拉取 KB 上下文（fail-open）：MemPalace + Obsidian → kb_section；字符预算 2000（MP 1200 / Obs 800 独立截断）
8b. Finnhub 即时新闻（免费，无配额）：异动标的优先 + watchlist 股票补齐，取前8个，
    hours=min(query_days×24, 48)，注入 finnhub_news_section（fail-open）
8c. Sonar 宏观快照（perplexity/sonar，~$0.005）：`_sonar_macro_brief()` 从 watchlist 动态构建 query
    AM 聚焦过去12h隔夜，PM 聚焦当日盘面+盘后；portfolio 快照注入 system prompt；fail-open
    → sonar_macro_section，注入 Pass 1 和 Pass 2 prompt（紧接 Finnhub 之后）
9.  计算 query_days = max(1, min(3, 距上次报告天数))
10. LLM pass 1（deepseek-v4-flash）注入 price_table + RSS + finnhub_news + kb_section + triggered_geo_topics
    → 输出：{report_md, tavily_queries:[{query, search_depth, days, max_results}]}
    （网络/5xx 错误自动重试 2 次，间隔 2s/4s；4xx 和 JSON parse 不重试）
11. 构建搜索任务列表：代码生成异动查询（AM=advanced/PM=basic）在前，LLM 建议查询在后（PM slot 全部降为 basic）
12. 顺序执行搜索任务，每次预检 budget（advanced 需 2 credits），Tavily 断连自动 fallback SerpApi
13. 有搜索结果 → LLM pass 2（deepseek-v4-pro）合并生成最终报告（同样含 finnhub_news_section）
14. PM slot：替换报告标题为「夜盘动向」
15. Append 到 Obsidian 月度文件 Daily_Intel_report_YYYYMM.md
16. 发邮件 → watchlist.md 配置的收件人
17. 发 Telegram（@PhyCluFintel_bot）→ Markdown 转 HTML，超 4096 字符自动分段
17b. 发送 TG 独立运行状态消息（`build_status_message()`）：Tavily/SerpApi 本次用量+剩余、情报源状态（RSS/Guardian/Finnhub/Sonar/Tavily搜索+Extract）、LLM/Provider 清单；不进入邮件和 Obsidian 正文
```

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

- 上限：20 credits/日，`finance_tavily_budget.json` 按 ET 日期自动重置（从 10→15→20 逐步调整）
- Search：basic=1cr，advanced=2cr（已弃用，全部改 basic）；Extract：**5 URLs = 1 credit**（`math.ceil(n/5)`），最多 10 URLs = 2cr
- 主报告：AM/PM 全 basic search（3-4cr）+ 1次 Extract（1-2cr）≈ 5-6cr；budget 不足时按层降级
- TG 追问不消耗 Tavily（Sonar 内建搜索）
- Tavily 断连自动 fallback SerpApi（250次/月）；两者均耗尽则跳过搜索继续生成基础报告

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

## RSS Feeds（已验证可达，14个源 + Guardian API）

| Feed | URL | 定位 |
|---|---|---|
| NYT Business | `https://rss.nytimes.com/services/xml/rss/nyt/Business.xml` | 财经综合 |
| NYT World | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` | 国际新闻 |
| NYT Politics | `https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml` | 美国政治 |
| BBC Business | `https://feeds.bbci.co.uk/news/business/rss.xml` | 财经综合 |
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | 国际新闻 |
| FT World | `https://www.ft.com/world?format=rss` | 专业财经 |
| CNBC | `https://www.cnbc.com/id/100003114/device/rss/rss.html` | 快速财经/市场 |
| MarketWatch | `https://feeds.marketwatch.com/marketwatch/topstories/` | 市场数据驱动 |
| Foreign Policy | `https://foreignpolicy.com/feed/` | 地缘战略深度 |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | 中东/非西方视角 |
| Seeking Alpha | `https://seekingalpha.com/market_currents.xml` | 个股机构分析 |
| Reuters（via Google News） | `https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en` | 综合/财经（Google 代理，<1h延迟） |
| AP（via Google News） | `https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en` | 综合新闻（Google 代理） |
| WSJ（via Google News） | `https://news.google.com/rss/search?q=site:wsj.com&hl=en-US&gl=US&ceid=US:en` | 专业财经（Google 代理） |

**Guardian API**（`content.guardianapis.com/search`，非 RSS）：免费 500次/日，JSON 结构化，`GUARDIAN_API_KEY` 控制，fail-open。`fetch_guardian_news()` 拉取 business/world/politics/us-news 板块最新 20 条，合并进 RSS 结果统一排序。

不可用（DNS/TLS 受限）：Politico（403）。Reuters/AP/WSJ/Guardian 直连不可达，已通过 Google News RSS / Guardian API 覆盖。

---

## KB 接入说明

- bridge URL：`http://localhost:8765`（宿主机直连）
- MemPalace 查询：`wing=paperview, room=finance`，sim~0.4
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

21. **SerpApi 月度预算独立跟踪**：不与 Tavily 共用 budget 文件。`finance_serpapi_budget.json` 用 `year_month: "YYYY-MM"` 作 key，月初自动重置。TG bot 是常驻进程，加新 env var 后需重启才能读到：`launchctl stop/start com.daily-intel.finance.telegram`。

22. **OpenRouter 连接不稳定**：peer closed connection / Server disconnected / SSL UNEXPECTED_EOF 等瞬时错误偶发。已在 call_llm() 和 telegram_commands.py 三个 LLM 调用点加网络/5xx 重试（2次，指数退避 2s/4s），4xx 和 JSON parse 不重试。重跑时可用 FINANCE_FORCE_DATE + FINANCE_FORCE_SLOT 补跑历史报告。

23. **Telegram HTML 注入**（已修复 2026-05-04）：`reply()` 使用 `parse_mode=HTML` 但未转义，LLM 回答含 `<`, `>`, `&` 时 Telegram 返回 Bad Request，`_tg()` 静默吞掉，用户收不到回复。修复：拆成 `reply()`（自动 escape）和 `reply_html()`（预格式化 HTML），`_md_to_tg_html()` 正文先 escape 再替换 Markdown 标记。

24. **搜索触发优先级倒置**（已修复 2026-05-04）：原代码先触发 LLM `tavily_query`，异动搜索反而是 fallback，与设计文档相反。修复：抽出 `_do_search()` 辅助函数，anomaly 先行，`tavily_query` 仅在无 anomaly 结果时触发。

25. **KG CLI section 被内部小节截断**（已修复 2026-05-04）：月度文件内 `## 【价格异动】` 等小节和 `## YYYY-MM-DD` 日期 header 共用 `## ` 层级，简单 `(?=\n## |\Z)` lookahead 在第一个小节就截断（section 只有 72 字符）。同时月度文件名不含日期，`process_report()` 的文件名正则失配，`date_str` 默认今日。修复：lookahead 改为 `(?=\n## \d{4}-\d{2}-\d{2} |\Z)`，并在 `run_for_date()` 中直接定位 section 后调 `_process_triples()`，绕过 `process_report()`。

26. **MemPalace Hermes mine 进 general room**（已修复 2026-05-04）：`Hermes/` 目录缺少 `mempalace.yaml`，mine 自动用目录名 `Hermes` 或 `general` 作 room，导致日报进错索引，`room=finance` 查不到。修复：新建 `Hermes/mempalace.yaml` 指定 `room=hermes`，touch 全目录 .md 强制重建，140 个 drawer 已写入 hermes room。

27. **crontab 并发 mine 触发 ChromaDB SIGSEGV**（已修复 2026-05-04）：7 个 mine 任务全部 `0 3 * * *` 同时启动，并发写 ChromaDB 导致部分任务（Hermes、STEM）静默崩溃，0 drawer 写入。修复：改为错开执行（AI 03:00 → Finance 03:03 → Health 03:06 → Lifestyle 03:09 → SHAPE 03:12 → STEM 03:15 → Hermes 03:25）。

28. **手动验证用当日日期 + FORCE_RUN 会触发防重拦截定时任务**（2026-05-05）：`FINANCE_FORCE_RUN=1` 绕过防重并实际写入报告；定时任务随后触发时发现 section 已存在，正确退出。这是设计行为，但测试时应使用历史日期（`FINANCE_FORCE_DATE=昨天或更早`）而非当日，避免消耗当日 Tavily 配额、提前触发报告并使定时任务空转。

29. **Gmail OAuth 重授权：ShadowRocket 拦截 localhost 回调**：ShadowRocket TUN 模式拦截所有出站流量包括 localhost 回调。`run_local_server(port=N)` 启动后授权页点 Continue，Google 将 `code` 重定向到 `http://localhost:N/?code=...`，请求被 ShadowRocket 拦截，Python server 永远收不到，授权挂死。另外两个死路：OOB 流程（Google 2022 年废弃）、`flow.run_console()`（新版已移除）。**有效方案**：`run_local_server(port=8080)`，8080 端口在 ShadowRocket 规则中默认 bypass。

30. **Gmail send 在 token 失效时仍可能实际发出邮件**：Gmail API 调用和 token refresh 是两个独立步骤。send 请求使用当前 access token 发出后，库尝试 refresh 时抛 `invalid_grant`——但邮件已在 API 层面投递成功。日志显示 `ERROR Gmail send failed` 但邮件实际到达，补发时形成两封。诊断时不能只看日志 ERROR，需查收件箱确认。

31. **google-auth SCOPES 陷阱补充**：`~/.hermes/skills/` 下的 `setup.py` 提供 `--auth-url/--auth-code` 重授权流程，但它写入 `HERMES_HOME/google_token.json`，而 `finance_gmail.py` 读取的是 `~/.hermes/token.json`——路径不同，用 setup.py 重授权不解决 finance_gmail.py 的 token 问题。token refresh 失败时直接编辑/删除 `~/.hermes/token.json` 后运行脚本触发重授权。

32. **TG bot 代码改动后需重启才生效**：bot 是 `KeepAlive` 常驻进程，改完 `telegram_commands.py` 必须 `launchctl stop/start com.daily-intel.finance.telegram`，否则跑旧代码，改动不生效。每次改 bot 代码后的标准收尾动作。

33. **KG 三元组泄漏给用户**（已修复 2026-05-11）：Claude 输出 `---KG---` 前无精确 `\n`，`"\n---KG---"` 字符串 split 失败，`raw_content` 整体作为 answer 发出，KG JSON 暴露在 Telegram 消息里。修复：改用 `re.search(r"\n?---KG---\s*\n?")` 做 regex 匹配，兼容任意换行格式。

34. **Sonar 无法获取盘前即时价格**（2026-05-11 识别）：Sonar 是新闻全文搜索，不是行情 API；盘前几小时内的最新价格可能未被任何文章收录，Sonar 会从不同日期的文章里拼出矛盾价格。根本解：在追问流水线中注入 yfinance 实时行情作为价格基准，并在 Claude 的推理规则里明确"以 yfinance 为准，Sonar 价格仅参考"。

35. **Finnhub webhook secret ≠ REST API key**（2026-05-11）：Finnhub 控制台有两个不同凭证——webhook secret（`X-Finnhub-Secret`，用于验证 Finnhub 推送给你的事件）和 REST API key（query param `?token=KEY`，用于主动调用 Finnhub 接口）。轮询价格/新闻只需 REST API key，webhook secret 完全无关。

36. **Finnhub 免费 tier 不含盘前/盘后数据**（2026-05-11）：`/api/v1/quote` 返回的 `c` 是常规交易时段最新价，盘前/盘后需付费 tier。用作 yfinance 限流时的 fallback 可接受（至少有昨收和日内涨幅），但不能替代 yfinance 的 `prepost=True` 功能。商品期货（`GC=F`、`CL=F`）和特殊指数（`^TNX`、`DX-Y.NYB`）在 Finnhub 免费 tier 无数据，fallback 时跳过这些 ticker。

37. **DeepSeek V4 Flash 默认开 thinking 模式**（2026-05-17）：`deepseek-v4-flash` 和 `deepseek-v4-pro` 默认 `thinking: enabled`，推理阶段耗尽 token 后 `content` 字段可能为 null。直连 DeepSeek API 时必须显式传 `"thinking": {"type": "disabled"}`（Pass 1/Step 1），或 `{"type": "enabled", "budget_tokens": N}` + 足够大的 `max_tokens`（Pass 2）。通过 OpenRouter 的旧调用没有这个问题，因为 OR 的 gpt-oss-20b 等模型不是推理模型。

38. **KG 三元组谓词碎片化**（2026-05-17 识别）：旧模型（gpt-oss-20b）提取的早期三元组用了 `price_move_pct`，新模型（Haiku）使用 `had_move_pct`。两者表达同义，但 KG 图谱里是两个独立 predicate，按谓词聚合查询时会漏掉旧数据。修复：system prompt 加词汇表约束，强制统一为 `had_move_pct`。历史旧三元组未做迁移（影响面有限，KG 查询通常按实体而非谓词检索）。

39. **Haiku via Bedrock/OR 成本严重低估**（2026-05-18 发现）：2026-05-17 将 KG 提取主力从 DeepSeek V4 Flash 换成 Haiku 4.5 时，成本估算未更新。实测 AM 报告两个 Haiku 调用合计 $0.0079（vs 估算 $0.0002），差距 ~40x。Bedrock 路由 OR 的 Haiku 定价约 $1/$5 per MTok；DeepSeek 直连 $0.07/$0.28 per MTok，相差 22 倍。已切回 DeepSeek 直连为主力，Haiku 降为 fallback。教训：换主力模型时必须同步核算并更新成本估算。

40. **月度文件重复节和乱序**（2026-05-17 修复）：`run_finance.py` 的 append 写入逻辑在某些重跑或边界场景下会写入重复的 section（本月发现 05-04 PM×1、05-11 PM×3、05-12 AM×1 共 5 个重复，以及 05-09 插入错误位置）。修复脚本：按 `top_headers` 精确定位后去重（保留最长版本）并按时间戳重排。备份文件：`Daily_Intel_report_202605.md.bak`。根因未完全排查，持续观察。

41. **DeepSeek API 早间 SSL 全程不可达**（2026-05-20 发现）：5:30 AM ET 定时触发时，`api.deepseek.com` 出现连续 SSL EOF 错误，3 次重试均失败（共约 15 分钟窗口不可达），Pass 1 和 Pass 2 均失败，报告未生成。现有重试策略（2次，退避 2s/4s）在 API 整体不可达场景下无效——已通过 OR flex fallback（gemini）解决。手动补跑步骤：`FINANCE_FORCE_RUN=1` + 正常运行命令，在 10:20 AM 完成补跑。

42. **OR flex fallback max_tokens 必须足够大**（2026-05-20 测试发现）：gemini-3.5-flash via OR flex 内部有思维链推理（`reasoning` 字段），消耗大量 token，若 `max_tokens` 太小（如 200）会导致 content 被截断（JSON 不完整）。OR flex fallback 的 max_tokens 应与 DeepSeek 主路径一致（Pass 1 = 4000，Pass 2 = 8000）。gemini 模型不接受 DeepSeek 的 `thinking` 参数，fallback 调用时已去掉该字段。

43. **OR 波浪号前缀 `~model` = always-latest alias**（2026-05-20）：`~anthropic/claude-sonnet-latest` 是 OR 维护的动态指针，始终路由到该系列最新版本，无需手动追版本号。不带波浪号的 `anthropic/claude-sonnet-latest` 在 OR 是无效 ID（400 Bad Request）。需要固定版本时用精确 ID（如 `anthropic/claude-sonnet-4-6`）；接受滚动更新时用 `~` 前缀。

44. **Azure provider 已放弃 Sonnet 路由**（2026-05-20 确认）：OR 面板显示 Azure 不再提供 `anthropic/claude-sonnet-4-6`。`provider: {order: [Azure], allow_fallbacks: False}` 组合导致 400，05-18 和 05-20 各触发一次。修复：改用 `~anthropic/claude-sonnet-latest` 不锁 provider，让 OR 自动选活跃 provider（当前路由到 Google）。新增 Grok 4.3 作为任何失败时的 fallback。

45. **Exa `/search` 与 `/chat/completions` 分属不同计费桶**（2026-05-20 确认）：dashboard 上 "search" 和 "answer" 是两个独立计数器。`/search` 端点计入 search（$7/1k）；`/chat/completions`（`model="exa"`）计入 answer（$5/1k）。免费额度（$20 初始 credit）覆盖两者，但两桶独立计量，不共享。作为 fallback 实际消耗极低。

46. **Exa 追问 Step 3 不加 RSS 增强**（2026-05-20 决策）：RSS 14 源在 Step 3 用途有限——用户追问是针对特定 query，Sonar/Exa 定向搜索比从 316 条泛新闻过滤更精准；yfinance.news 已覆盖 ticker 定向的最新新闻；重抓 RSS 需 14 次 HTTP + 过滤，5-8s 延迟不值得。缓存复用也无法解决"下午追问时早报 RSS 已陈旧"的问题。

47. **数据质量 > LLM 档次**（2026-05-21 验证）：Hermes agent 用 DeepSeek V4 Flash + parallel.ai full-text extract 对 NVDA 财报做出的分析，质量接近甚至超过 DI 用 Claude Sonnet + Sonar 摘要的效果。差距不在模型，在信息输入：full-text extract 保留了 Sonar 摘要层丢失的细节（DC 收入 Hyperscale vs ACIE 分拆、期权隐含波动率等）。教训：换更好的数据源比换更贵的模型更有效率。

48. **Parallel.ai SDK 版本差异**（2026-05-21）：Hermes 用的是 `parallel-web==0.4.2`（有 `beta.search`/`beta.extract` + `mode="agentic"/"fast"/"one-shot"`）；DI venv 装的是 `parallel-web==0.6.0`（直接 `client.search`/`client.extract`，`mode="basic"/"advanced"`）。两者 API 接口不同，不能直接复制 Hermes 的调用代码。REST 端点：`https://api.parallel.ai/v1/search` 和 `/v1/extract`。

49. **格式硬约束压制 LLM 分析深度**（2026-05-21 发现）：将 V4 Flash 套进"每节1-3句"的5节硬格式，输出反而比无格式约束的 Hermes（同样 V4 Flash）浅很多。根本原因：硬格式迫使模型"填格子"而非自由展开推理。修复：改为参考建议（"分析维度参考，自由展开，不要机械填格子"），V4 Flash 输出深度立即对标 Hermes 水平。

50. **Step 4 不应使用 `_deepseek_post()` 的 OR flex fallback**（2026-05-21 发现）：`_deepseek_post()` 内部 DeepSeek 失败时会静默 fallback 到 gemini-3.1-flash-lite，外层 `model_label` 不感知，显示"V4 Flash"但实为弱模型；且弱模型成功返回后，外层 Grok 4.3 fallback 永远不触发。Step 4 已改为自管重试（3次）+ Grok 4.3 fallback，bypasses `_deepseek_post()`。

51. **`_get_portfolio_snapshot()` 提取 `现价` 而非 `均价`**（2026-05-21 发现并修复）：regex 抓取 `现价`（portfolio_report 写入时的市价，如 NVDA $225.32），但该文件更新于 2026-05-15，现价已是6天前的旧值。LLM 把旧现价当成本价，INTC 偏差最大（现价 vs 真实均价差距显著）。修复：regex 改为捕获 `均价`，输出格式改为 `成本@190.17`，加注"浮盈%为报告日数据，实时盈亏以yfinance现价计算"。

52. **DeepSeek 直连暴露个人金融数据**（2026-05-21 决策）：LLM 提示词含持仓成本价、框架判断、KG 三元组等高度个人化的金融数据。直连 `api.deepseek.com` = 将这些数据发给 DeepSeek 的训练流水线。修复：全部迁移至 OR/Novita，OR 承诺不将用户数据用于训练，novita/fp8 是 DeepSeek 模型的 fp8 量化版本，成本略高（OR 抽成约 5-10%）但隐私边界明确。三个文件共六个调用点全部覆盖。

53. **`_preprocess_question` 漏传 `search_queries` 字段**（2026-05-23 发现并修复）：`_unified_preprocess` 生成的2条互补 query 存放在 `pre["search_queries"]`，但 `_preprocess_question` 的返回 dict 不含该字段，导致 `_llm_followup` 里 `ctx.get("search_queries")` 始终为 None，退化为单条 query。该 bug 从 2026-05-21 流水线上线起就存在，"Step 1 生成2条互补 query"的设计从未实际生效。排查线索：日志显示"情报检索（1条查询）"而预期应为2条。修复：`_preprocess_question` 返回 dict 加 `"search_queries": pre.get("search_queries") or []`。**教训：发现日志数字与预期不符时，不能以"不在本次计划范围内"为由放过，必须当场查清楚。**

54. **`kg_extractor_finance.py` 中 `_HOME` 未定义**（2026-05-25 发现并修复）：`_OBSIDIAN_ROOT` 在模块级使用 `_HOME` 但文件中无定义，`NameError` 在 `run_finance.py` 始终传入 `report_text` 的生产路径下不触发，仅在 standalone CLI（`process_report`/`run_for_date` 无 `report_text` 路径）下崩溃。修复：在 `load_dotenv()` 之后加 `_HOME = os.path.expanduser("~")`。

55. **`_unified_preprocess` max_tokens 截断**（2026-05-26）：followup 类需输出 query + search_queries（两条英文约 200 字）+ question_intent 等，合计超过 350 tokens，JSON 截断，`json.loads` 抛 JSONDecodeError，fallback 到 `{"action": "unknown"}`，bot 回复"未识别指令"。修复：max_tokens 350→600。

56. **LLM 把 `---KG---` 解读为 markdown 水平线**（2026-05-26）：Step 4 LLM 将 `---KG---` 分成两行 `---`（水平线）+ `KG---`，regex 失配，KG 块整体跳过，KG +0。修复：分隔符改为 `===KG===`，prompt 加注"不能分行"，regex 同时兼容三种格式。

57. **yfinance 盘后价格字段**（2026-05-26）：`fast_info.last_price` 和 `yf.download(prepost=True)` 均只返回常规收盘，不含盘前/盘后。正确字段：`Ticker.info.postMarketPrice`/`preMarketPrice`/`regularMarketPrice`，与 Yahoo Finance app 同源。需按 ET 时段选字段（04:00-09:29 盘前/09:30-15:59 常规/16:00-19:59 盘后）；盘后基准用今日收盘（regularMarketPrice）而非昨收，与 app 一致。

58. **Polygon.io 免费 tier 无实时数据**（2026-05-26）：`snapshot`/`last_trade` 端点 `NOT_AUTHORIZED`，免费 tier 仅延迟历史聚合（与 yfinance 相同数据集）。`POLYGON_API_KEY` 存入 `.env` 备用，Starter（$29/月）起支持实时。

59. **周末/隔夜 OTC 价格不可达（yfinance/Polygon 免费 tier）**（2026-05-26）：Yahoo Finance app 周末场外报价来自 Yahoo 私有移动端 feed；Polygon.io 免费 tier snapshot/last_trade 端点 NOT_AUTHORIZED。解法：IBKR Client Portal Gateway（见坑60-63）。

60. **IBKR gateway `--conf` 只接受 classpath 资源名**（2026-05-26）：`root/` 目录必须在 `-cp` classpath 里，`--conf` 只传文件名（`conf.json`），不传路径。传绝对路径或相对路径均报 `java.io.IOException: Stream closed`，错误发生在配置读取阶段，与端口无关。

61. **macOS port 5000 被 AirPlay Receiver 占用**（2026-05-26）：`com.apple.ControlCenter` 持续监听 5000 端口。gateway 改为 5001。或关闭：System Settings → General → AirDrop & Handoff → AirPlay Receiver。

62. **IBKR gateway Java 版本用 11，不用 21**（2026-05-26）：gateway JAR 编译于 Java 8（class version 52），Netty 4.1.15 在 Java 21 有 `--illegal-access` 反射封禁，Java 11 运行稳定无警告。安装：`brew install openjdk@11`。

63. **IBKR gateway conf 文件必须 JSON，不能 YAML**（2026-05-26）：Vert.x 3.5.0 的 `--conf` 仅支持 JSON，runtime 里没有 `vertx-config-yaml`；原始 `conf.yaml` 虽语法合法但被 Vert.x 拒绝，已转换为 `conf.json`。

65. **macOS launchd `StartCalendarInterval` 数组只注册第一个时间**（2026-05-29 确认）：一个 plist 里的 `StartCalendarInterval` 若写成数组含多个时间点，macOS 只注册第一个为 XPC activity，其余静默丢失。症状：第二个定时任务从不触发，系统日志里只能看到一个 activity ID。修复：每个定时任务独立一个 plist 文件，`StartCalendarInterval` 为单个 dict 而非数组。报告任务已拆分为 `com.daily-intel.finance.am.plist` 和 `com.daily-intel.finance.pm.plist`。

64. **IBKR 强制单一 brokerage session，iOS App 登录踢掉 gateway**（2026-05-26 确认）：IBKR 同一账号只允许一个 brokerage session 并发。iOS App 登录 → gateway session 立即失效（keepalive 报 `authenticated=False`）。反向同理：gateway 登录后 iOS App 在线功能受限。恢复流程：① iOS App 退出登录（Logout，不是关 App）② 确认 gateway 进程存在（`pgrep -la GatewayStart`）③ `open https://localhost:5001` 浏览器登录 ④ 手机 IB Key 批准推送 ⑤ 浏览器显示"Client login succeeds" ⑥ 验证：`curl -sk https://localhost:5001/v1/api/iserver/auth/status` 显示 `authenticated=true, competing=false`。iOS App 活跃时不得尝试 gateway 登录——challenge code 页无论输对输错都会 Authentication failed，根因是服务端拒绝竞争 session。

66. **KG object 死端节点：长度合规但不可检索**（2026-06-02 发现）：LLM 提取的 triple object 可能通过长度检查（≤ 30 chars）、无括号数字，但仍是死端节点——无法被另一条 triple 引用。三类典型模式：① 顿号/逗号合并的多值列表（`以色列-黎巴嫩停火协议脆弱、伊朗局势悬而未决`，应拆成多条）；② 含形容判断词的描述短语（脆弱、悬而未决、仍在进行）；③ 条件句型操作指令（`若跌破100美元则考虑减持1/3`，investment_view 只写状态词 bullish/bearish/hold/reduce）。已在两处 KG prompt 补充明确禁止规则，但 LLM 仍可能绕过——定期 `--scan-kg` 清洗是兜底。

67. **`_mempalace_context` 只查 finance room，漏掉日报历史**（2026-06-02 发现修复）：日报/追问记录由 run_finance.py 写入 `room=hermes`，追问时 `_mempalace_context()` 原只查 `room=finance`，追问"之前报告怎么说"类问题命中率为零。已改为同时查 finance + hermes 两个 room。

68. **`_filter_framework_triples` fallback 集合漏 driven_by/correlated_with**（2026-06-02 发现修复）：从 memory_context_finance import 失败时的 hardcoded 集合未包含 `driven_by`、`correlated_with`，这两类谓词被静默过滤。已补入 fallback 集合。

69. **`resp.json()` 与 `json.loads(content)` 混用同一 handler 导致 UnboundLocalError 崩溃**（2026-06-09 发现修复）：Pass 2 调用 OR/DigitalOcean 时 HTTP 响应体被截断，`resp.json()` 抛 JSONDecodeError，同一个 `except` handler 内访问了尚未赋值的 `content` 变量，引发 `UnboundLocalError`，整个进程崩溃，邮件/TG 均未发出。修复：在 `resp.raise_for_status()` 后立即初始化 `content = ""`，将 `resp.json()` 单独包进内层 `try/except`，失败时 `continue` 走重试而非崩溃。规律：HTTP body 解析失败（传输故障）和 LLM 内容格式错误是两类不同故障，不能混在同一个 exception handler。

70. **`json.loads(json_str)` 失败直接 `return {}`，未走重试/fallback，导致 AM 报告整体跳过未发**（2026-06-15 发现修复）：Pass 1 因模型输出转义错误 `json.loads` 失败、Pass 2 因 `msg.content` 在 max_tokens 前被截断（无收尾 `}`，regex 清理后 `json_str` 变空字符串）`json.loads` 失败，两次均命中 `except json.JSONDecodeError: return {}`，导致 `report_md` 全程为空，触发 `"Empty report_md, skipping"; sys.exit(0)`——进程正常退出（非崩溃）但报告/邮件/TG 全部缺失。修复：该 `except` 改为 `last_error = e; continue`，纳入与网络错误相同的重试循环，重试耗尽后进入 OR flex fallback。69 号坑只覆盖了"HTTP body 非法 JSON"，本条覆盖"模型内容非法 JSON"——两者是同一类问题在 `call_llm()` 不同环节的重复，现已统一处理。

71. **yfinance 早间瞬时故障导致 ticker 静默丢失 → LLM 幻觉价格**（2026-06-18 发现修复）：5:30 AM ET yfinance bulk download 对多个 ticker 同时返回空 DataFrame（报 "possibly delisted"，实为瞬时故障，Jun 17/18 各触发一次，受影响：INTC/QCOM/TSLA/NVDA/AMKR/QQQM/SPCX/^TNX）。bulk download 无异常抛出，Finnhub 全局 fallback 不触发，个别 ticker 被 `continue` 丢弃。LLM 收不到价格行，却从 RSS 新闻 / portfolio context 拼凑出幻觉价格（INTC 昨收出现 $183.53，实际 $121.10）。修复：① `fetch_prices.py` 新增 `_finnhub_single_ticker()`，在 `len(closes_daily) < 2` 和 AM `len(_closes_pre) < 2` 两处 `continue` 前先调 Finnhub 单 ticker 补价（AM slot 进一步尝试 `yf.Ticker.info.preMarketPrice`，不同 Yahoo 端点，transient 故障期间通常仍可达）；② `run_finance.py` 价格表生成后检测 `failed_tickers`，非空时向 Pass 1 / Pass 2 prompt 注入"以下标的价格数据获取失败，不得引用具体价格数字"声明。

72. **Tavily extract 对视频聚合页返回无时间戳 caption 堆叠 → LLM 误判为当前时效新闻**（2026-06-30 发现，issue #19）：Reuters 等视频聚合页（`/video/watch/...`）extract 回来的不是文章正文，而是多条视频缩略图 caption 堆叠文本，其中孤立 caption（如"US, Iran reach agreement to end war, signing set for Friday"）无独立时间戳，可能是过期视频，与当前抓取日期无关。Pass 2 LLM 曾把这类孤证当确定事实写入报告（"签署仪式定于周五"，用户核实后其他信源查无此消息）。修复：新增 `_detect_low_structure()`（时间戳前缀密度 vs 句子数，识别 caption 堆叠）、`_lookup_published_date()`（从 extract 前的 search 结果池按 URL 反查发布时间，因为 Tavily `/extract` 响应本身不带日期字段）、`_compute_corroboration()`（规则式事实指纹匹配，统计有多少独立域名与本条内容重叠，零 API/LLM 成本的启发式交叉印证，存在假阴性）。三者汇总为 `_source_confidence_tags()`，在 `format_extract_results()`（LLM 提示词）和 `write_extract_archive()`（本地审计存档）中共用同一套标签。`USER_PROMPT_TEMPLATE_P2` 新增分析要求第④条：单一信源/无时间戳/视频聚合页的具体断言必须用"未证实/单一信源，待核实"降级措辞。方向 4（wire 原文优先/视频路径降权）和方向 5（输出侧二次核验）按用户决定暂缓未实现。

---

## 下一步优先事项

1. **Pass 2 重构效果验证**（2026-05-28 起）：观察报告是否从"每日财经速报"升级为"面向持仓框架的情报研判"——关键指标：① 异动标的下是否出现"对 INTC@均价 持仓逻辑的含义"类推理；② 地缘/宏观是否有具体传导路径而非泛泛描述；③ thinking 预算 3000 tokens 是否足够（完整分析 vs 截断）；④ `Layer_A_Prompt.md` 内容是否需要调整
2. **月度文件重复节根因排查**：已有修复脚本，但触发条件未明；观察 06 月文件是否再出现重复
3. **OR flex fallback 首次实战验证**：观察 DeepSeek 再次不可达时日志是否出现 `OR flex fallback succeeded` 且报告正常生成；关注 flex 延迟是否在可接受范围（预期 <30s 单次调用）
4. **追问流水线多 query 效果验证**：Step 1 新增 `search_queries` 双 query（事件角度 + 量化/技术角度），观察 Parallel.ai 是否能拿到期权 IV、历史财报模式等深层数据；对比单 query 和双 query 的内容质量差异
5. **watchlist 调整**：根据实际报告质量增减 ticker 或地缘政治主题

---

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
