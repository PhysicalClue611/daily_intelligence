# 踩坑记录详情

本文件是 `CLAUDE.md` 踩坑记录索引的详情存储。CLAUDE.md 只保留一行摘要，
完整的现象/根因/修复叙述在这里，按子系统分类，编号与 CLAUDE.md 索引一一对应。

**为什么拆分**：CLAUDE.md 每个新 session 都会被完整加载进上下文；踩坑记录持续增长，
详细叙述留在 CLAUDE.md 会不断推高每次 session 的起始 token 成本。本文件不会被自动加载，
需要时用 grep/Read 按编号或关键词查找即可（如 `grep -n "^### 37\." docs/PITFALLS.md`）。

**归类原则**：按受影响的子系统分类，不按时间顺序。已下线子系统（KG）单独成节——
其中的坑因为触发代码已被删除，不会再复现，但保留作历史参考（若未来重新引入类似设计）。

---

## 一、价格与行情数据源（yfinance / Finnhub / IBKR / Polygon）

### 1. Yahoo Finance 429
容器内不可达，yfinance 需要宿主机运行。生产环境每天一次不触发。

### 2. yfinance MultiIndex
`group_by="column"` 时用 `data["Close"][ticker]` 访问。

### 35. Finnhub webhook secret ≠ REST API key
（2026-05-11）Finnhub 控制台有两个不同凭证——webhook secret（`X-Finnhub-Secret`，用于验证 Finnhub 推送给你的事件）和 REST API key（query param `?token=KEY`，用于主动调用 Finnhub 接口）。轮询价格/新闻只需 REST API key，webhook secret 完全无关。

### 36. Finnhub 免费 tier 不含盘前/盘后数据
（2026-05-11）`/api/v1/quote` 返回的 `c` 是常规交易时段最新价，盘前/盘后需付费 tier。用作 yfinance 限流时的 fallback 可接受（至少有昨收和日内涨幅），但不能替代 yfinance 的 `prepost=True` 功能。商品期货（`GC=F`、`CL=F`）和特殊指数（`^TNX`、`DX-Y.NYB`）在 Finnhub 免费 tier 无数据，fallback 时跳过这些 ticker。

### 57. yfinance 盘后价格字段
（2026-05-26）`fast_info.last_price` 和 `yf.download(prepost=True)` 均只返回常规收盘，不含盘前/盘后。正确字段：`Ticker.info.postMarketPrice`/`preMarketPrice`/`regularMarketPrice`，与 Yahoo Finance app 同源。需按 ET 时段选字段（04:00-09:29 盘前/09:30-15:59 常规/16:00-19:59 盘后）；盘后基准用今日收盘（regularMarketPrice）而非昨收，与 app 一致。

### 58. Polygon.io 免费 tier 无实时数据
（2026-05-26）`snapshot`/`last_trade` 端点 `NOT_AUTHORIZED`，免费 tier 仅延迟历史聚合（与 yfinance 相同数据集）。`POLYGON_API_KEY` 存入 `.env` 备用，Starter（$29/月）起支持实时。

### 59. 周末/隔夜 OTC 价格不可达（yfinance/Polygon 免费 tier）
（2026-05-26）Yahoo Finance app 周末场外报价来自 Yahoo 私有移动端 feed；Polygon.io 免费 tier snapshot/last_trade 端点 NOT_AUTHORIZED。解法：IBKR Client Portal Gateway（见坑60-64）。

### 60. IBKR gateway `--conf` 只接受 classpath 资源名
（2026-05-26）`root/` 目录必须在 `-cp` classpath 里，`--conf` 只传文件名（`conf.json`），不传路径。传绝对路径或相对路径均报 `java.io.IOException: Stream closed`，错误发生在配置读取阶段，与端口无关。

### 61. macOS port 5000 被 AirPlay Receiver 占用
（2026-05-26）`com.apple.ControlCenter` 持续监听 5000 端口。gateway 改为 5001。或关闭：System Settings → General → AirDrop & Handoff → AirPlay Receiver。

### 62. IBKR gateway Java 版本用 11，不用 21
（2026-05-26）gateway JAR 编译于 Java 8（class version 52），Netty 4.1.15 在 Java 21 有 `--illegal-access` 反射封禁，Java 11 运行稳定无警告。安装：`brew install openjdk@11`。

### 63. IBKR gateway conf 文件必须 JSON，不能 YAML
（2026-05-26）Vert.x 3.5.0 的 `--conf` 仅支持 JSON，runtime 里没有 `vertx-config-yaml`；原始 `conf.yaml` 虽语法合法但被 Vert.x 拒绝，已转换为 `conf.json`。

### 64. IBKR 强制单一 brokerage session，iOS App 登录踢掉 gateway
（2026-05-26 确认）IBKR 同一账号只允许一个 brokerage session 并发。iOS App 登录 → gateway session 立即失效（keepalive 报 `authenticated=False`）。反向同理：gateway 登录后 iOS App 在线功能受限。恢复流程：① iOS App 退出登录（Logout，不是关 App）② 确认 gateway 进程存在（`pgrep -la GatewayStart`）③ `open https://localhost:5001` 浏览器登录 ④ 手机 IB Key 批准推送 ⑤ 浏览器显示"Client login succeeds" ⑥ 验证：`curl -sk https://localhost:5001/v1/api/iserver/auth/status` 显示 `authenticated=true, competing=false`。iOS App 活跃时不得尝试 gateway 登录——challenge code 页无论输对输错都会 Authentication failed，根因是服务端拒绝竞争 session。

### 71. yfinance 早间瞬时故障导致 ticker 静默丢失 → LLM 幻觉价格
（2026-06-18 发现修复）5:30 AM ET yfinance bulk download 对多个 ticker 同时返回空 DataFrame（报 "possibly delisted"，实为瞬时故障，Jun 17/18 各触发一次，受影响：INTC/QCOM/TSLA/NVDA/AMKR/QQQM/SPCX/^TNX）。bulk download 无异常抛出，Finnhub 全局 fallback 不触发，个别 ticker 被 `continue` 丢弃。LLM 收不到价格行，却从 RSS 新闻 / portfolio context 拼凑出幻觉价格（INTC 昨收出现 $183.53，实际 $121.10）。修复：① `fetch_prices.py` 新增 `_finnhub_single_ticker()`，在 `len(closes_daily) < 2` 和 AM `len(_closes_pre) < 2` 两处 `continue` 前先调 Finnhub 单 ticker 补价（AM slot 进一步尝试 `yf.Ticker.info.preMarketPrice`，不同 Yahoo 端点，transient 故障期间通常仍可达）；② `run_finance.py` 价格表生成后检测 `failed_tickers`，非空时向 Pass 1 / Pass 2 prompt 注入"以下标的价格数据获取失败，不得引用具体价格数字"声明。

---

## 二、新闻源 / 邮件 / 预算配置

### 4. feedparser + Python 3.14 TLS
httpx 拉取原始 XML，feedparser 只做解析。

### 5. RSS DNS 限制
Reuters/AP/WSJ/Guardian 不可用，改用 NYT/BBC/FT。

### 6. BUDGET_PATH 位置
项目本地 `_PROJ_DIR / "finance_tavily_budget.json"`，与 Hermes 解耦。

### 21. SerpApi 月度预算独立跟踪
不与 Tavily 共用 budget 文件。`finance_serpapi_budget.json` 用 `year_month: "YYYY-MM"` 作 key，月初自动重置。TG bot 是常驻进程，加新 env var 后需重启才能读到：`launchctl stop/start com.daily-intel.finance.telegram`。

### 29. Gmail OAuth 重授权：ShadowRocket 拦截 localhost 回调
ShadowRocket TUN 模式拦截所有出站流量包括 localhost 回调。`run_local_server(port=N)` 启动后授权页点 Continue，Google 将 `code` 重定向到 `http://localhost:N/?code=...`，请求被 ShadowRocket 拦截，Python server 永远收不到，授权挂死。另外两个死路：OOB 流程（Google 2022 年废弃）、`flow.run_console()`（新版已移除）。**有效方案**：`run_local_server(port=8080)`，8080 端口在 ShadowRocket 规则中默认 bypass。

### 30. Gmail send 在 token 失效时仍可能实际发出邮件
Gmail API 调用和 token refresh 是两个独立步骤。send 请求使用当前 access token 发出后，库尝试 refresh 时抛 `invalid_grant`——但邮件已在 API 层面投递成功。日志显示 `ERROR Gmail send failed` 但邮件实际到达，补发时形成两封。诊断时不能只看日志 ERROR，需查收件箱确认。

### 31. google-auth SCOPES 陷阱补充
`~/.hermes/skills/` 下的 `setup.py` 提供 `--auth-url/--auth-code` 重授权流程，但它写入 `HERMES_HOME/google_token.json`，而 `finance_gmail.py` 读取的是 `~/.hermes/token.json`——路径不同，用 setup.py 重授权不解决 finance_gmail.py 的 token 问题。token refresh 失败时直接编辑/删除 `~/.hermes/token.json` 后运行脚本触发重授权。

### 3. Gmail invalid_scope
finance_gmail.py 独立客户端，只声明 send+readonly。

---

## 三、LLM 调用与 Provider 路由

### 12. Perplexity 隐藏搜索费
所有 Perplexity 模型在 token 费之外固定收 $0.005/次搜索调用，与 token 量无关。OR 页面未显眼提示，导致实际成本远高于 token 计算。选模型时需考虑这个固定成本。

### 13. DeepSeek R1 拒绝 2026 日期
R1 训练截止约 2025 年中，遇到注入的 2026 日期会主动判定为"未来时间"并拒绝接受，回退到训练数据（2023 年事件）。R1 不适合实时数据合成场景。

### 14. GPT-4o search 隐藏费用
OpenAI search 模型在 token 费之外还有 per-search 调用费，导致 $0.006 token 成本变成 $0.04 实收。

### 19. Sonar/Claude 输出截断
Sonar `max_tokens=700`、Claude `max_tokens=900` 均撞顶导致回答截断。已分别提高到 1500 和 2000。

### 22. OpenRouter 连接不稳定
peer closed connection / Server disconnected / SSL UNEXPECTED_EOF 等瞬时错误偶发。已在 call_llm() 和 telegram_commands.py 三个 LLM 调用点加网络/5xx 重试（2次，指数退避 2s/4s），4xx 和 JSON parse 不重试。重跑时可用 FINANCE_FORCE_DATE + FINANCE_FORCE_SLOT 补跑历史报告。

### 37. DeepSeek V4 Flash 默认开 thinking 模式
（2026-05-17）`deepseek-v4-flash` 和 `deepseek-v4-pro` 默认 `thinking: enabled`，推理阶段耗尽 token 后 `content` 字段可能为 null。直连 DeepSeek API 时必须显式传 `"thinking": {"type": "disabled"}`（Pass 1/Step 1），或 `{"type": "enabled", "budget_tokens": N}` + 足够大的 `max_tokens`（Pass 2）。通过 OpenRouter 的旧调用没有这个问题，因为 OR 的 gpt-oss-20b 等模型不是推理模型。

### 41. DeepSeek API 早间 SSL 全程不可达
（2026-05-20 发现）5:30 AM ET 定时触发时，`api.deepseek.com` 出现连续 SSL EOF 错误，3 次重试均失败（共约 15 分钟窗口不可达），Pass 1 和 Pass 2 均失败，报告未生成。现有重试策略（2次，退避 2s/4s）在 API 整体不可达场景下无效——已通过 OR flex fallback（gemini）解决。手动补跑步骤：`FINANCE_FORCE_RUN=1` + 正常运行命令，在 10:20 AM 完成补跑。

### 42. OR flex fallback max_tokens 必须足够大
（2026-05-20 测试发现）gemini-3.5-flash via OR flex 内部有思维链推理（`reasoning` 字段），消耗大量 token，若 `max_tokens` 太小（如 200）会导致 content 被截断（JSON 不完整）。OR flex fallback 的 max_tokens 应与 DeepSeek 主路径一致（Pass 1 = 4000，Pass 2 = 8000）。gemini 模型不接受 DeepSeek 的 `thinking` 参数，fallback 调用时已去掉该字段。

### 43. OR 波浪号前缀 `~model` = always-latest alias
（2026-05-20）`~anthropic/claude-sonnet-latest` 是 OR 维护的动态指针，始终路由到该系列最新版本，无需手动追版本号。不带波浪号的 `anthropic/claude-sonnet-latest` 在 OR 是无效 ID（400 Bad Request）。需要固定版本时用精确 ID（如 `anthropic/claude-sonnet-4-6`）；接受滚动更新时用 `~` 前缀。

### 44. Azure provider 已放弃 Sonnet 路由
（2026-05-20 确认）OR 面板显示 Azure 不再提供 `anthropic/claude-sonnet-4-6`。`provider: {order: [Azure], allow_fallbacks: False}` 组合导致 400，05-18 和 05-20 各触发一次。修复：改用 `~anthropic/claude-sonnet-latest` 不锁 provider，让 OR 自动选活跃 provider（当前路由到 Google）。新增 Grok 4.3 作为任何失败时的 fallback。

### 45. Exa `/search` 与 `/chat/completions` 分属不同计费桶
（2026-05-20 确认）dashboard 上 "search" 和 "answer" 是两个独立计数器。`/search` 端点计入 search（$7/1k）；`/chat/completions`（`model="exa"`）计入 answer（$5/1k）。免费额度（$20 初始 credit）覆盖两者，但两桶独立计量，不共享。作为 fallback 实际消耗极低。

### 52. DeepSeek 直连暴露个人金融数据
（2026-05-21 决策）LLM 提示词含持仓成本价、框架判断、KG 三元组等高度个人化的金融数据。直连 `api.deepseek.com` = 将这些数据发给 DeepSeek 的训练流水线。修复：全部迁移至 OR/Novita，OR 承诺不将用户数据用于训练，novita/fp8 是 DeepSeek 模型的 fp8 量化版本，成本略高（OR 抽成约 5-10%）但隐私边界明确。三个文件共六个调用点全部覆盖。

### 69. `resp.json()` 与 `json.loads(content)` 混用同一 handler 导致 UnboundLocalError 崩溃
（2026-06-09 发现修复）Pass 2 调用 OR/DigitalOcean 时 HTTP 响应体被截断，`resp.json()` 抛 JSONDecodeError，同一个 `except` handler 内访问了尚未赋值的 `content` 变量，引发 `UnboundLocalError`，整个进程崩溃，邮件/TG 均未发出。修复：在 `resp.raise_for_status()` 后立即初始化 `content = ""`，将 `resp.json()` 单独包进内层 `try/except`，失败时 `continue` 走重试而非崩溃。规律：HTTP body 解析失败（传输故障）和 LLM 内容格式错误是两类不同故障，不能混在同一个 exception handler。

### 70. `json.loads(json_str)` 失败直接 `return {}`，未走重试/fallback，导致 AM 报告整体跳过未发
（2026-06-15 发现修复）Pass 1 因模型输出转义错误 `json.loads` 失败、Pass 2 因 `msg.content` 在 max_tokens 前被截断（无收尾 `}`，regex 清理后 `json_str` 变空字符串）`json.loads` 失败，两次均命中 `except json.JSONDecodeError: return {}`，导致 `report_md` 全程为空，触发 `"Empty report_md, skipping"; sys.exit(0)`——进程正常退出（非崩溃）但报告/邮件/TG 全部缺失。修复：该 `except` 改为 `last_error = e; continue`，纳入与网络错误相同的重试循环，重试耗尽后进入 OR flex fallback。69 号坑只覆盖了"HTTP body 非法 JSON"，本条覆盖"模型内容非法 JSON"——两者是同一类问题在 `call_llm()` 不同环节的重复，现已统一处理。

---

## 四、情报检索与信源质量（Tavily / Parallel.ai / Extract）

### 24. 搜索触发优先级倒置
（已修复 2026-05-04）原代码先触发 LLM `tavily_query`，异动搜索反而是 fallback，与设计文档相反。修复：抽出 `_do_search()` 辅助函数，anomaly 先行，`tavily_query` 仅在无 anomaly 结果时触发。

### 34. Sonar 无法获取盘前即时价格
（2026-05-11 识别）Sonar 是新闻全文搜索，不是行情 API；盘前几小时内的最新价格可能未被任何文章收录，Sonar 会从不同日期的文章里拼出矛盾价格。根本解：在追问流水线中注入 yfinance 实时行情作为价格基准，并在 Claude 的推理规则里明确"以 yfinance 为准，Sonar 价格仅参考"。

### 46. Exa 追问 Step 3 不加 RSS 增强
（2026-05-20 决策）RSS 14 源在 Step 3 用途有限——用户追问是针对特定 query，Sonar/Exa 定向搜索比从 316 条泛新闻过滤更精准；yfinance.news 已覆盖 ticker 定向的最新新闻；重抓 RSS 需 14 次 HTTP + 过滤，5-8s 延迟不值得。缓存复用也无法解决"下午追问时早报 RSS 已陈旧"的问题。

### 48. Parallel.ai SDK 版本差异
（2026-05-21）Hermes 用的是 `parallel-web==0.4.2`（有 `beta.search`/`beta.extract` + `mode="agentic"/"fast"/"one-shot"`）；DI venv 装的是 `parallel-web==0.6.0`（直接 `client.search`/`client.extract`，`mode="basic"/"advanced"`）。两者 API 接口不同，不能直接复制 Hermes 的调用代码。REST 端点：`https://api.parallel.ai/v1/search` 和 `/v1/extract`。

### 73. Tavily extract 对视频聚合页返回无时间戳 caption 堆叠 → LLM 误判为当前时效新闻
（2026-06-30 发现，issue #19）Reuters 等视频聚合页（`/video/watch/...`）extract 回来的不是文章正文，而是多条视频缩略图 caption 堆叠文本，其中孤立 caption（如"US, Iran reach agreement to end war, signing set for Friday"）无独立时间戳，可能是过期视频，与当前抓取日期无关。Pass 2 LLM 曾把这类孤证当确定事实写入报告（"签署仪式定于周五"，用户核实后其他信源查无此消息）。

**讨论与方案取舍**：用户明确排除"简单拉黑视频域名"的方案——① 视频聚合页有时确有独家报道；② 结构性问题（无时间戳、caption 堆叠）不止 Reuters 一家；③ 排除信源解决不了"LLM 不区分单信源孤证与多信源印证事实"这个更根本的问题。开了 GitHub issue #19，记录 5 个改善方向：① 多渠道交叉确认、② 置信度标注带入 LLM 最终合成、③ 内容结构探测（caption 堆叠识别）、④ wire 原文优先/视频路径降权、⑤ 输出侧二次核验。用户确认方向 1/2/3 立即实现，方向 4 并入 issue #14（多搜索服务商矩阵 + 相关度分类）的实现范围一并评估，方向 5 暂缓。

**实现（commit 73f954b, 3c55e39）**：`run_finance.py` 新增四个 helper（`format_extract_results` 之前）：
- `_detect_low_structure(text)`：正则统计裸时间戳前缀（`\d{1,2}:\d{2}`）密度 vs 句子数，识别视频聚合页 caption 堆叠（阈值：时间戳数 ≥3 且多于句子数）
- `_lookup_published_date(url, candidates)`：从 extract 前的 search 结果池（`score_and_filter` 输出，带 `published_date` 字段）按 URL 反查发布时间——Tavily `/extract` 响应本身不带日期字段，只有 `/search` 有
- `_compute_corroboration(url, text, candidates)`：规则式事实指纹匹配（`_extract_key_phrases`：大写多词短语 + 星期/日期/百分比/金额 token），统计候选池中有多少个其他独立域名与本条内容存在关键词重叠，作为交叉印证信号——零额外 API/LLM 成本，纯正则启发式，存在假阴性
- `_source_confidence_tags(url, full_text, candidates)`：汇总以上三者为 `[信源类型 | 发布时间 | 交叉印证]` 一行标签

**接入点**：`format_extract_results()` 新增 `candidates` 参数，每条 Extract 来源前插入确认标签行；`write_extract_archive()` 复用同一 tag 函数写入本地 archive；`USER_PROMPT_TEMPLATE_P2` 新增第④条硬性规则：单一信源/无时间戳/视频聚合页的具体断言必须用"未证实"/"单一信源，待核实"等措辞明确降级，不得以确定语气写成既成事实；有多个独立域名佐证的可正常陈述。并加了一句一般性提醒："快变的宏观/市场行情下，传统媒体报道常滞后于现状，未标注可靠时间戳的信息尤其容易过时，宁可标注不确定，也不要把孤证当结论"——回应用户"没有时间戳的情报也要特别保留/标注"的要求。

**验证**：用原始故障场景做单元级复现——Reuters 视频聚合页 caption 文本 vs 有 `published_date` 的 marinelink 文章文本，标签行为符合预期（视频页标"无独立时间戳，谨慎对待"+"单一信源"，文章正常显示发布时间）。`py_compile` + 模块 import smoke test 通过。

**遗留**：方向 4（wire 原文优先/视频路径降权）并入 issue #14 范围：把"信源形态"判断从"extract 抓完全文后事后打标"前移到"候选阶段 URL 路径正则前筛"（识别 `/video/`、`/watch/`、`/gallery/` 等），与 #14 原有的"相关度分类"（个股直接命中/行业关联/宏观背景）合并成统一候选打标层，同域名同事件的视频版/文章版做去重降权，省 Tavily extract credit。用户决定先观察一段时间再评估是否启动。方向 5（输出侧二次核验）暂缓，需评估额外搜索额度成本（当前 Tavily 预算仅 20cr/日）。

### 78. Sonar 宏观快照报告过时/幻觉信息，未限定检索时间窗且未锚定实时价格
（2026-07-02 发现修复，issue #24）2026-07-02 AM 报告的 Sonar 宏观快照声称"原油飙破 $100、黄金下跌、美元走强"（典型滞涨初期画面），但同一份报告的实际价格数据显示 WTI 跌破 $70（$68.58，-2.13%），黄金 ETF 盘前涨 1.30%——与 Sonar 描述完全相反。Pass 2 LLM 靠自己核对价格数据发现了矛盾并在报告里做了修正说明，但这只是运气好被下游 LLM 接住，机制上没有防线——用户明确要求：严格限定 Sonar 情报的检索时间窗，或找别的办法拿到新情报；至少要求返回信息带时间戳。

**根因**：`_sonar_macro_brief()` 调用 Perplexity Sonar via OpenRouter 时：① 未设置 `search_recency_filter`，底层搜索没有时间窗约束，可能召回/合成陈旧或不相关文章；② 未把调用这一步之前 pipeline 已经算好的实时价格表（`price_table`）注入给 Sonar 做锚定，Sonar 完全靠自己的搜索结果"重新发明"价格；③ prompt 只泛泛要求"time-stamped developments"，没有强制每条具体断言必须带时间戳，也没要求"找不到近24小时更新就明说，不能拿旧信息冒充当前"。

**验证**：直接测试 `perplexity/sonar` via OpenRouter 加 `search_recency_filter: "day"`——OpenRouter 确认透传给 Perplexity（不报错，结果明显不同），同样问"WTI 当前价格"，加参数后返回 $68.00（对比真实 $68.58，误差很小）并附带具体来源时间戳。进一步用假造 price_table 注入测试，模型正确遵循"若搜索结论与注入价格冲突，以注入价格为准并明确指出冲突"，且对缺乏近24小时更新的具体问题（如 USD/JPY）正确输出"no update in past 24h"而非编造。

**修复**：`_sonar_macro_brief()` 三处改动——① OR payload 加 `"search_recency_filter": "day"`；② 新增 `price_table` 参数，system prompt 注入"以下为实时价格数据（权威，非搜索结果），若你的搜索结论与之冲突，以此为准并明确指出冲突"；③ user prompt 加"对每个具体数字/断言说明来源发布时间；若某话题近24小时无更新，明确说明'无近24小时更新'，不要用旧信息冒充当前"。`telegram_commands.py` 的 `_sonar_research()`（TG 追问流水线的 Sonar fallback，同一模型同一风险，此前已有价格锚定但缺这个检索层约束）同步加 `search_recency_filter: "day"`。

---

## 五、Telegram Bot 与追问流水线

### 9. Telegram 重复标题
report_md 已有 `#` 标题，send_telegram_report 不应再加 subject 前缀。

### 11. Telegram bot token 冲突
Hermes Agent 已持续监听 Hermes bot token 的 getUpdates，共用 token 导致消息被随机消费。Daily Intelligence 使用独立 bot @PhyCluFintel_bot。

### 16. RSVP 延迟
原来 RSVP 在 `_parse_command`（LLM调用）之后发送，导致 ~20 秒延迟。修复：主循环收到消息立即发 `⏳`，再做 LLM 分类。

### 23. Telegram HTML 注入
（已修复 2026-05-04）`reply()` 使用 `parse_mode=HTML` 但未转义，LLM 回答含 `<`, `>`, `&` 时 Telegram 返回 Bad Request，`_tg()` 静默吞掉，用户收不到回复。修复：拆成 `reply()`（自动 escape）和 `reply_html()`（预格式化 HTML），`_md_to_tg_html()` 正文先 escape 再替换 Markdown 标记。

### 32. TG bot 代码改动后需重启才生效
bot 是 `KeepAlive` 常驻进程，改完 `telegram_commands.py` 必须 `launchctl stop/start com.daily-intel.finance.telegram`，否则跑旧代码，改动不生效。每次改 bot 代码后的标准收尾动作。

### 50. Step 4 不应使用 `_deepseek_post()` 的 OR flex fallback
（2026-05-21 发现）`_deepseek_post()` 内部 DeepSeek 失败时会静默 fallback 到 gemini-3.1-flash-lite，外层 `model_label` 不感知，显示"V4 Flash"但实为弱模型；且弱模型成功返回后，外层 Grok 4.3 fallback 永远不触发。Step 4 已改为自管重试（3次）+ Grok 4.3 fallback，bypasses `_deepseek_post()`。

### 53. `_preprocess_question` 漏传 `search_queries` 字段
（2026-05-23 发现并修复）`_unified_preprocess` 生成的2条互补 query 存放在 `pre["search_queries"]`，但 `_preprocess_question` 的返回 dict 不含该字段，导致 `_llm_followup` 里 `ctx.get("search_queries")` 始终为 None，退化为单条 query。该 bug 从 2026-05-21 流水线上线起就存在，"Step 1 生成2条互补 query"的设计从未实际生效。排查线索：日志显示"情报检索（1条查询）"而预期应为2条。修复：`_preprocess_question` 返回 dict 加 `"search_queries": pre.get("search_queries") or []`。**教训：发现日志数字与预期不符时，不能以"不在本次计划范围内"为由放过，必须当场查清楚。**

### 55. `_unified_preprocess` max_tokens 截断
（2026-05-26）followup 类需输出 query + search_queries（两条英文约 200 字）+ question_intent 等，合计超过 350 tokens，JSON 截断，`json.loads` 抛 JSONDecodeError，fallback 到 `{"action": "unknown"}`，bot 回复"未识别指令"。修复：max_tokens 350→600。

### 74. getUpdates 长轮询超时配置错位：客户端 timeout(10s) < Telegram 长轮询 timeout(30s)，引发超时刷屏+409冲突
（2026-07-01 发现修复，issue #20）用户巡检报告：`telegram_commands.py`（launchd KeepAlive 常驻）5 天内产生 34,117 次 `read operation timed out`、207 次 `SSL UNEXPECTED_EOF_WHILE_READING`、5 次 `HTTP 409 Conflict`（getUpdates），全是 WARNING/INFO，无崩溃，"带病运行"。

**根因（单一根因解释全部三症状）**：`_tg()`（:80）对所有 Telegram API 调用统一用 `httpx.post(..., timeout=10)`，但主循环调用 `getUpdates` 传的 payload 是 `{"timeout": 30, ...}`——这个 `30` 是 Telegram **长轮询**参数，告诉服务端没有新消息时最多挂起连接 30 秒。客户端 read timeout（10s）比服务端承诺的等待时间短 20 秒，于是几乎每次空轮询（绝大多数周期）都在服务端还没来得及返回前被客户端自己掐断，抛 `ReadTimeout`，`except Exception` 吞掉记成 WARNING，`sleep(5)` 后重试，5 天堆出 3.4 万条日志。409 由此连锁触发：客户端本地放弃读取不等于服务端立即感知取消，若下一次 `getUpdates` 恰好落在上一个请求服务端仍未释放的窗口内，Telegram 判定"同一 bot 两个并发 getUpdates"返回 409。SSL EOF（207次/5天）大概率是同一族问题的极端情况（Telegram 强制断开重叠连接）。

**已排除的其他可能性**（巡检报告列出但验证不成立）：`ps aux` 确认只有一个进程；`getWebhookInfo` 确认未设 webhook；全盘 grep bot token 仅在 `~/.hermes/.env.bak*`（过期备份，无脚本加载）中出现，活跃的 `~/.hermes/.env` 不含该 token。

**修复**：`_tg()` 新增 `timeout` 参数（默认保持10，向后兼容其他调用），`run()` 调用 `getUpdates` 时显式传 `timeout=POLL_TIMEOUT+5`（35s），确保客户端永远比服务端多等。重启后观察 45 秒无任何新增 timeout/409/SSL EOF 记录（修复前平均 10-15 秒一条）。

**修正（见 #76）**：上面"SSL EOF 是同一族问题的极端情况"的推测**不成立**。SSL EOF 是独立于本条的另一个背景故障（Shadowrocket TUN 隧道对新建 TLS 连接约 25-30% 瞬时失败率），修复本条之后由于噪音基线归零而变得显眼，被巡检误判为"回归"，实际两者互不相关，详见 #76。

### 76. Shadowrocket TUN 隧道对新建 TLS 连接有 ~25-30% 瞬时失败率，#74 修复后噪音基线归零，被巡检误判为回归
（2026-07-02 发现修复，issue #22）#74 修复重启后，定时巡检报警：`telegram_commands.py` 最近 150 行日志 149 条命中错误关键词，末条为 `SSL: UNEXPECTED_EOF_WHILE_READING`。第一直觉是 #74 的修复（客户端超时从 10s 延长到 35s）引入了新问题（连接持有更久，触碰某个中间层空闲超时）——**验证后证明这个直觉错了**，记录下来是因为这类"刚修完一个坑，紧接着看起来像回归"的场景值得警惕。

**排查过程**：① 停用 launchd 实例避免探测和生产实例撞车产生新 409；② 用不同 `payload.timeout`（15/20/25/30s）各发一次 `getUpdates`，全部在预期时长内正常返回——排除"固定时长空闲踢出"假设；③ 改用连续 24 次探测（间隔仅 2s），复现：**6/24 失败（25%），全部在约 3.1-3.2 秒内以 `ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]` 失败**——精确到 0.1s 的重复超时特征说明是某个中间层固定内部机制，不是随机网络抖动；失败与成功交替出现，与运行时长无关（第 21 次仍是 OK 30.9s），排除"代理节点逐渐劣化"假设。

**根因**：本机 Shadowrocket 处于 TUN 模式常驻（同一机制此前在 #29 记录过，那次是 Gmail OAuth 回调被拦截），对新建的 TLS 连接（到 `api.telegram.org`）有约 25-30% 的瞬时失败率，失败恒定发生在约 3.2 秒（大概率是其内部代理节点选择/握手重试耗尽后的失败上限），与 HTTP 层请求的 timeout 长短无关。

**为什么 #74 修复前没被注意到**：修复前几乎每次轮询都被自己的 10s 客户端超时打断（`ReadTimeout`），5 天堆出 3.4 万条噪音，把 SSL EOF 的绝对计数（207条/5天，占比<1%）完全淹没。#74 修复后 `ReadTimeout` 噪音归零，SSL EOF 变成日志里唯一剩下的错误类型，绝对占比骤升到 healthcheck 阈值以上——是同一个一直存在、从未被独立观测到的背景故障率的可见度提升，不是回归。

**功能性影响：低**。现有 `except Exception: sleep(5); continue` 兜底能自动恢复，Telegram 服务端按 offset 游标保留未确认消息，不会丢消息，只是单次轮询失败后多约 5 秒延迟。

**修复**：`_tg()` 增加连接级快速重试——仅针对 `httpx.ConnectError` 立即重试一次（不等待，因为这类失败本身只需 ~3.2s 就会失败，不是"卡住"），命中概率降到约 25%² ≈ 6%（两次独立失败叠加）。其余异常类型（`ReadTimeout` 等）行为不变，仍走原有外层 5s sleep 重试。

**遗留（环境层面，不属于代码修复范围）**：Shadowrocket 代理节点稳定性/TUN 握手超时配置不在项目控制范围，若失败率持续高企可能需要检查节点选择策略，或让该 bot 进程走分应用直连规则绕过隧道。

**方法论教训**：收到"回归"报警时，先验证是否真的是新问题，还是旧问题的可见度因为噪音基线下降而提升——两者的修复方向完全不同，前者要回滚/复查刚才的改动，后者是发现了一个独立的、之前被掩盖的问题。这次差点把无关的独立故障误诊为 #74 修复的副作用。

**补充确认（同日，用户追问"为什么其他服务没这个问题"后验证）**：对照测试同一时段、同一隧道下 `api.telegram.org` vs `slack.com` vs `api.openai.com` 各 30-45 次独立连接：Telegram 3/45≈6.7%失败，Slack 0/45，OpenAI 0/30。排除"隧道对所有新连接都不稳定"，问题精确指向 Telegram 域名——大概率是 Shadowrocket 分流规则把 Telegram（国内被墙服务的典型分流对象）单独路由到了稳定性更差的代理节点/规则组，而 Slack/OpenAI 走直连或更稳定的通用代理组。叠加本脚本的高频轮询（~30s/次 vs Slack 检查脚本 5min/次），暴露次数差 10 倍，绝对故障计数被放大，且只有本项目的日志被巡检脚本盯着看，三个因素共同导致"只有这里持续报错"。

### 77. Telegram 发送调用（sendMessage）与轮询调用（getUpdates）分属两套独立实现，重试补丁只覆盖了轮询
（2026-07-02 发现修复，issue #23）#76 的重试修复只加在 `telegram_commands.py` 的 `_tg()`（轮询用），`run_finance.py` 里 `send_telegram_report()`/`send_telegram_alert()`（报告推送、故障告警）是完全独立手写的 `httpx.post()`，没有同样的重试。当时的判断是"发送调用频率低（数十次/5天 vs 轮询数万次/5天），统计上不容易撞上失败窗口"——用户指出这是同一个坑，只是运气好没暴露，明确提出三点要求：① 功能上轮询/发送/测试都要同等抗故障；② 重试后成功不算"警告"，是设计好的正常路径；③ 只有真正耗尽重试才该是 WARNING 级别，日志严重级别要反映"是否需要人关注"而不是"底层是否发生过一次网络抖动"。

**修复**：新建 `scripts/telegram_utils.py`，单一函数 `call_telegram(bot_token, endpoint, payload, timeout, max_connect_retries=2)`：对 `httpx.ConnectError` 立即重试（不退避），重试后成功记 `logger.info("...recovered after N retry(ies)")`，全部耗尽才 `logger.warning(...)`。两个脚本的所有 Telegram 调用点统一改为调用这一个函数（`_tg()` 收窄为一行委托），全仓库 grep `api.telegram.org` 确认无遗漏裸调用点。顺带修正：`send_telegram_report`/`send_telegram_alert` 原实现只检查 HTTP 状态码，没检查 Telegram API 返回体的 `ok` 字段（API 层面失败如 `ok: false` 会被当成功放过），现在两者都查。

**验证**：生产重启后 5 分钟窗口，日志只有启动行 + 2 条 "recovered after N retry(ies)"，全部 INFO，零 WARNING。

**方法论教训（已存入项目 memory，跨项目通用）**：给不稳定外部依赖加容错时，容错机制要覆盖该依赖的**全部**调用路径（轮询/发送/测试/告警），不能挑一个高频路径打补丁就停手——低频路径只是统计上不容易暴露，不代表没有同样的坑。日志严重级别应该反映"人是否需要关注"，重试后自愈的瞬时故障是设计好的正常路径，记 INFO；只有兜底耗尽的真实故障才该是 WARNING/ERROR。

### 79. getUpdates 轮询的同步重试其实是多余的——轮询循环本身就是现成的重试机制
（2026-07-02 发现修复，issue #25，用户对 #76/#77 方案的复查）#76/#77 给 `getUpdates` 加了同步重试（`call_telegram()` 内对 `httpx.ConnectError` 立即重试一次）。用户复查后指出这个机制"太重"：

> 检查TG追问的机制现在30s一次每次都面向连接失败重试这个机制太重了。我觉得应该是无状态的，拉到了就拉回来处理，没拉到就pass等下一轮。如果持续失败，且机器本身是联网中，比如半小时都持续失败那才尝试重试，耗尽重试了的话记Warning日志。

**核心洞察**：`getUpdates` 轮询循环本身每 ~30 秒自然重跑一次——循环节奏本身就是现成的重试机制，不需要在单次调用内部再套一层同步重试。同步重试给约 25% 的失败周期都多加了 ~3.2 秒延迟（连接失败本身的耗时），但收益很小：反正下一轮 ~7-10 秒后自然会再拉一次，多等这一轮对 Telegram bot 场景没有任何感知差异（消息按 offset 游标在服务端排队，不会丢）。

**实现（比 #76/#77 的方案更简单，不是更复杂）**：`run()` 的轮询调用改为绕过 `call_telegram()`，直接单次 `httpx.post()`：
- **成功**：正常处理；若之前处于失败状态，记 INFO（`getUpdates recovered after Ns`，N 是真实停机秒数，不是重试次数）
- **失败**：静默跳过（不重试、不记 WARNING），`time.sleep(5)` 交给下一轮循环自然重试；用一个 `failing_since` 时间戳追踪连续失败起点
- **持续失败 ≥ 30 分钟**：记一条 WARNING（含已持续失败的分钟数），重置计时器避免此后每轮重复报警，30 分钟后若仍未恢复才再报一次

`sendMessage` 相关调用（"收到，处理中..."确认、最终回复、`run_finance.py` 的报告推送/告警）**不受影响**，继续走 `call_telegram()` 的同步重试——这些是一次性事件，没有"下一轮"兜底，失败了就是真没发出去，值得在当次调用内多试一次。

**验证**：状态机逻辑用缩短阈值（3s 代替 1800s）做了独立单元测试，行为符合预期。生产部署后日志出现 `getUpdates recovered after 44s`/`36s`（真实停机秒数，非重试次数），零 WARNING，单次失败完全静默不留日志。

**与 #76/#77 的关系**：不是推翻，是简化——#76/#77 解决的"轮询和发送都要抗故障"仍然成立（发送路径的重试没变），但轮询路径原本套的同步重试是多余复杂度，因为轮询循环自带的天然节奏已经提供了等效的重试能力，且成本更低（不占用当次调用的额外延迟）。

---

## 六、调度与并发（launchd / cron）

### 7. 重复邮件
干跑 + launchd 各触发一次。修复：月度文件 section header 防重。

### 8. 00:00 ET 调度陷阱
午夜整点已是新一天，次日若非交易日则静默退出。应使用 23:59 ET（20:59 PT）。

### 17. ET 硬编码 UTC-4
（已修复 2026-05-04）原 `ET = timezone(timedelta(hours=-4))` 是 EDT，冬季差 1 小时。三个文件已统一改为 `ZoneInfo("America/New_York")`，夏/冬令时自动处理。

### 27. crontab 并发 mine 触发 ChromaDB SIGSEGV
（已修复 2026-05-04）7 个 mine 任务全部 `0 3 * * *` 同时启动，并发写 ChromaDB 导致部分任务（Hermes、STEM）静默崩溃，0 drawer 写入。修复：改为错开执行（AI 03:00 → Finance 03:03 → Health 03:06 → Lifestyle 03:09 → SHAPE 03:12 → STEM 03:15 → Hermes 03:25）。

### 28. 手动验证用当日日期 + FORCE_RUN 会触发防重拦截定时任务
（2026-05-05）`FINANCE_FORCE_RUN=1` 绕过防重并实际写入报告；定时任务随后触发时发现 section 已存在，正确退出。这是设计行为，但测试时应使用历史日期（`FINANCE_FORCE_DATE=昨天或更早`）而非当日，避免消耗当日 Tavily 配额、提前触发报告并使定时任务空转。

### 65. macOS launchd `StartCalendarInterval` 数组只注册第一个时间
（2026-05-29 确认）一个 plist 里的 `StartCalendarInterval` 若写成数组含多个时间点，macOS 只注册第一个为 XPC activity，其余静默丢失。症状：第二个定时任务从不触发，系统日志里只能看到一个 activity ID。修复：每个定时任务独立一个 plist 文件，`StartCalendarInterval` 为单个 dict 而非数组。报告任务已拆分为 `com.daily-intel.finance.am.plist` 和 `com.daily-intel.finance.pm.plist`。

### 72. 三个 launchd plist 并存导致 AM/PM 报告各发两次
（2026-06-29 发现修复，issue #18）`com.daily-intel.finance.plist`（旧，2026-05-29 拆分为独立 am/pm plist 时忘记卸载，`StartCalendarInterval` 数组仍含 5:30 AM + 17:10 PM 两个时间点）与新拆分的 `com.daily-intel.finance.am.plist`/`.pm.plist` 同时被 launchd 加载，每个报告时间点被两个 job 同时触发。两个进程实例都在写入 Obsidian 文件之前完成 `_monthly_dedup` 检查（此时文件都还没被对方写入），于是各自跑完整 LLM pass 并各自发送，日志证据显示同一时刻（如 17:10:07）"=== Daily_Intel run ==="出现两次，或相隔 28 秒各写入一次 "开盘前简报"。修复：① `launchctl unload` 卸载旧 plist（重命名为 `.disabled` 留痕，未从磁盘删除）；② `run_finance.py` 新增 `_acquire_lock()`（`fcntl.flock` 排他锁，`LOCK_FILE = run_finance.lock`），`main()` 入口获取锁，第二个并发实例拿不到锁立即 `sys.exit(0)` 退出，不再跑重复的 LLM pass。

---

## 七、报告生成与持久化

### 10. PM slot LLM 标题
LLM 始终写「开盘前简报」，需在写文件前 `re.sub` 替换为「夜盘动向」。

### 18. 持仓快照截断
`_get_portfolio_snapshot()` 原来有 600 字符上限，导致 QCOM 等靠后的持仓被截断，LLM 无法看到。修复：去掉字符限制，改为只提取 IB 美股持仓，过滤 CASH 和 A 股 ETF 编号（纯噪音）。

### 40. 月度文件重复节和乱序
（2026-05-17 修复）`run_finance.py` 的 append 写入逻辑在某些重跑或边界场景下会写入重复的 section（本月发现 05-04 PM×1、05-11 PM×3、05-12 AM×1 共 5 个重复，以及 05-09 插入错误位置）。修复脚本：按 `top_headers` 精确定位后去重（保留最长版本）并按时间戳重排。备份文件：`Daily_Intel_report_202605.md.bak`。根因未完全排查，持续观察。

### 51. `_get_portfolio_snapshot()` 提取 `现价` 而非 `均价`
（2026-05-21 发现并修复）regex 抓取 `现价`（portfolio_report 写入时的市价，如 NVDA $225.32），但该文件更新于 2026-05-15，现价已是6天前的旧值。LLM 把旧现价当成本价，INTC 偏差最大（现价 vs 真实均价差距显著）。修复：regex 改为捕获 `均价`，输出格式改为 `成本@190.17`，加注"浮盈%为报告日数据，实时盈亏以yfinance现价计算"。

---

## 八、MemPalace

### 26. MemPalace Hermes mine 进 general room
（已修复 2026-05-04）`Hermes/` 目录缺少 `mempalace.yaml`，mine 自动用目录名 `Hermes` 或 `general` 作 room，导致日报进错索引，`room=finance` 查不到。修复：新建 `Hermes/mempalace.yaml` 指定 `room=hermes`，touch 全目录 .md 强制重建，140 个 drawer 已写入 hermes room。

### 67. `_mempalace_context` 只查 finance room，漏掉日报历史
（2026-06-02 发现修复）日报/追问记录由 run_finance.py 写入 `room=hermes`，追问时 `_mempalace_context()` 原只查 `room=finance`，追问"之前报告怎么说"类问题命中率为零。已改为同时查 finance + hermes 两个 room。

---

## 九、方法论与一般性教训

### 15. Python 字符串内中文引号
`"` 和 `"` 放入 Python 双引号字符串会触发 SyntaxError，一律改用 `【】`。

### 20. 超长会话 token 用量
单次会话内读取大文件（如 `金融资产信息.md` ~5000 tokens）+ 积累大量代码和日志上下文，会快速耗尽 5 小时窗口限额。大文件按需分节读取，长文档生成建议在新会话里做。

### 47. 数据质量 > LLM 档次
（2026-05-21 验证）Hermes agent 用 DeepSeek V4 Flash + parallel.ai full-text extract 对 NVDA 财报做出的分析，质量接近甚至超过 DI 用 Claude Sonnet + Sonar 摘要的效果。差距不在模型，在信息输入：full-text extract 保留了 Sonar 摘要层丢失的细节（DC 收入 Hyperscale vs ACIE 分拆、期权隐含波动率等）。教训：换更好的数据源比换更贵的模型更有效率。

### 49. 格式硬约束压制 LLM 分析深度
（2026-05-21 发现）将 V4 Flash 套进"每节1-3句"的5节硬格式，输出反而比无格式约束的 Hermes（同样 V4 Flash）浅很多。根本原因：硬格式迫使模型"填格子"而非自由展开推理。修复：改为参考建议（"分析维度参考，自由展开，不要机械填格子"），V4 Flash 输出深度立即对标 Hermes 水平。

---

## 十、凭据与日志卫生

### 75. Telegram bot token / Finnhub / Guardian API key 以明文形式写入世界可读的 /tmp 日志文件
（2026-07-01 发现修复，issue #21，排查 issue #20 时意外发现）`telegram_commands.py` 和 `run_finance.py` 均用 `logging.basicConfig(level=logging.INFO)` 配置根 logger。httpx 库内建的请求日志同样走 Python logging 且默认 propagate 到 root，在 INFO 级别输出完整请求 URL——而 Telegram Bot API 把认证 token 直接编码在 URL 路径（`https://api.telegram.org/bot<TOKEN>/method`），Finnhub/Guardian 把 key 放在查询参数（`?token=`/`?api-key=`），于是每次 API 调用都把明文凭据写进日志。`/tmp/finance_telegram.log`（79000+行）和 `/tmp/daily_intelligence.log` 均为 `-rw-r--r--`（644，本机任何本地账户可读）。实测 `/tmp/daily_intelligence.log` 中已有 54 处明文 `api-key=`/`token=`。

**影响面**：Telegram bot token（可完全冒充 `@PhyCluFintel_bot`）、Finnhub/Guardian API key（免费/低权限，无资金操作权限）。不涉及账号密码或其他系统。

**修复**：两个文件的 `logging.basicConfig()` 之后各加一行 `logging.getLogger("httpx").setLevel(logging.WARNING)`，httpx 自身请求日志降级，不再输出完整 URL；应用层日志（`_tg()` 内的 warning 等）仍能感知调用失败，不影响可观测性。

**遗留（需用户执行，代码层无法完成）**：① 已暴露的凭据视为泄露处理——Telegram bot token 建议通过 BotFather `/revoke` 重新生成；Finnhub/Guardian key 评估是否重新生成；② 历史日志文件内容已含明文凭据，建议清理或轮转。

**举一反三**：任何脚本若 `logging.basicConfig(level=INFO)` 且底层调用 httpx/requests 访问带 token 查询参数或路径的 API，都有同类风险——新增外部 API 调用时应默认检查 URL 是否含凭据，若含则该脚本必须显式压低 `httpx`/`requests` logger 级别，不能依赖"忘了配置"的默认状态。

---

## 十一、已下线子系统：Knowledge Graph（KG 三元组，2026-06-12 全面下线）

**说明**：以下踩坑记录的触发代码（`kg_extractor_finance.py` 及 `memory_context_finance.py`/`telegram_commands.py` 中的 KG 相关函数）已于 2026-06-12 全面删除，系统回退为两层知识体系（Obsidian + MemPalace）。这些坑**不会再复现**，保留仅供未来若重新引入类似的实体关系图谱设计时参考，不需要在日常排障时优先检索。

### 25. KG CLI section 被内部小节截断
（已修复 2026-05-04）月度文件内 `## 【价格异动】` 等小节和 `## YYYY-MM-DD` 日期 header 共用 `## ` 层级，简单 `(?=\n## |\Z)` lookahead 在第一个小节就截断（section 只有 72 字符）。同时月度文件名不含日期，`process_report()` 的文件名正则失配，`date_str` 默认今日。修复：lookahead 改为 `(?=\n## \d{4}-\d{2}-\d{2} |\Z)`，并在 `run_for_date()` 中直接定位 section 后调 `_process_triples()`，绕过 `process_report()`。

### 33. KG 三元组泄漏给用户
（已修复 2026-05-11）Claude 输出 `---KG---` 前无精确 `\n`，`"\n---KG---"` 字符串 split 失败，`raw_content` 整体作为 answer 发出，KG JSON 暴露在 Telegram 消息里。修复：改用 `re.search(r"\n?---KG---\s*\n?")` 做 regex 匹配，兼容任意换行格式。

### 38. KG 三元组谓词碎片化
（2026-05-17 识别）旧模型（gpt-oss-20b）提取的早期三元组用了 `price_move_pct`，新模型（Haiku）使用 `had_move_pct`。两者表达同义，但 KG 图谱里是两个独立 predicate，按谓词聚合查询时会漏掉旧数据。修复：system prompt 加词汇表约束，强制统一为 `had_move_pct`。历史旧三元组未做迁移（影响面有限，KG 查询通常按实体而非谓词检索）。

### 39. Haiku via Bedrock/OR 成本严重低估
（2026-05-18 发现）2026-05-17 将 KG 提取主力从 DeepSeek V4 Flash 换成 Haiku 4.5 时，成本估算未更新。实测 AM 报告两个 Haiku 调用合计 $0.0079（vs 估算 $0.0002），差距 ~40x。Bedrock 路由 OR 的 Haiku 定价约 $1/$5 per MTok；DeepSeek 直连 $0.07/$0.28 per MTok，相差 22 倍。已切回 DeepSeek 直连为主力，Haiku 降为 fallback。教训：换主力模型时必须同步核算并更新成本估算。

### 54. `kg_extractor_finance.py` 中 `_HOME` 未定义
（2026-05-25 发现并修复）`_OBSIDIAN_ROOT` 在模块级使用 `_HOME` 但文件中无定义，`NameError` 在 `run_finance.py` 始终传入 `report_text` 的生产路径下不触发，仅在 standalone CLI（`process_report`/`run_for_date` 无 `report_text` 路径）下崩溃。修复：在 `load_dotenv()` 之后加 `_HOME = os.path.expanduser("~")`。

### 56. LLM 把 `---KG---` 解读为 markdown 水平线
（2026-05-26）Step 4 LLM 将 `---KG---` 分成两行 `---`（水平线）+ `KG---`，regex 失配，KG 块整体跳过，KG +0。修复：分隔符改为 `===KG===`，prompt 加注"不能分行"，regex 同时兼容三种格式。

### 66. KG object 死端节点：长度合规但不可检索
（2026-06-02 发现）LLM 提取的 triple object 可能通过长度检查（≤ 30 chars）、无括号数字，但仍是死端节点——无法被另一条 triple 引用。三类典型模式：① 顿号/逗号合并的多值列表（`以色列-黎巴嫩停火协议脆弱、伊朗局势悬而未决`，应拆成多条）；② 含形容判断词的描述短语（脆弱、悬而未决、仍在进行）；③ 条件句型操作指令（`若跌破100美元则考虑减持1/3`，investment_view 只写状态词 bullish/bearish/hold/reduce）。已在两处 KG prompt 补充明确禁止规则，但 LLM 仍可能绕过——定期 `--scan-kg` 清洗是兜底。

### 68. `_filter_framework_triples` fallback 集合漏 driven_by/correlated_with
（2026-06-02 发现修复）从 memory_context_finance import 失败时的 hardcoded 集合未包含 `driven_by`、`correlated_with`，这两类谓词被静默过滤。已补入 fallback 集合。
