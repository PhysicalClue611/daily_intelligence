"""
telegram_commands.py — Telegram command handler for Daily Intelligence

Runs as a launchd KeepAlive daemon (com.daily-intel.finance.telegram), long-
polling getUpdates in a tight loop (see run()). Parses commands, modifies
watchlist.md directly, replies in the same chat.

Supported commands (natural language, LLM-parsed):
  - 加 MSFT / 删 INTC          → add/remove ticker
  - 加关键词 Fed降息            → add geo keyword
  - 删关键词 Fed降息            → remove geo keyword
  - 加收件人 x@x.com            → add recipient
  - 删收件人 x@x.com            → remove recipient
  - 状态 / status               → show current watchlist config + budget
  - 强制运行                    → bypass duplicate guard, run report now
  - 任意问题                    → LLM follow-up answer (finance context)

State: last seen update_id stored in ~/Daily_Intelligence/tg_offset.json
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import html
from pathlib import Path

import httpx
from dotenv import load_dotenv

_HOME = os.path.expanduser("~")
_DI_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_DI_ENV)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# httpx's own request logger (propagates to root at INFO) logs the full request
# URL, and Telegram's Bot API encodes the auth token in the URL path — without
# this, the bot token gets written in plaintext to the log file (issue #21).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
EXA_API_KEY        = os.getenv("EXA_API_KEY", "")
PARALLEL_API_KEY   = os.getenv("PARALLEL_API_KEY", "")
OR_BASE_URL        = "https://openrouter.ai/api/v1/chat/completions"
EXA_BASE_URL       = "https://api.exa.ai/chat/completions"
LLM_MODEL          = "deepseek/deepseek-v4-flash"
LLM_FALLBACK_FLASH = "google/gemini-3.1-flash-lite"   # OR flex fallback for v4-flash
DS_OR_PROVIDERS    = {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}

_PROJ_DIR = Path(__file__).parent.parent
OFFSET_FILE = _PROJ_DIR / "tg_offset.json"
BUDGET_PATH = _PROJ_DIR / "finance_tavily_budget.json"

_OBSIDIAN_ROOT = os.path.join(
    _HOME,
    "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview",
)
OBSIDIAN = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))
WATCHLIST_PATH = OBSIDIAN / "Hermes/Daily Intelligence/watchlist.md"
REPORTS_DIR = OBSIDIAN / "Hermes/Daily Intelligence/Daily Reports"

ET = ZoneInfo("America/New_York")
TAVILY_DAILY_LIMIT    = 10
SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY", "")
SERPAPI_MONTHLY_LIMIT = 250
SERPAPI_BUDGET_PATH   = _PROJ_DIR / "finance_serpapi_budget.json"
FINNHUB_API_KEY       = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE          = "https://finnhub.io/api/v1"



# ── Telegram API ──────────────────────────────────────────────────────────────

def _tg(endpoint: str, payload: dict, timeout: float = 10) -> dict:
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}",
            json=payload,
            timeout=timeout,
        )
        return resp.json()
    except Exception as e:
        logger.warning(f"Telegram API {endpoint} failed: {e}")
        return {}


def reply_html(html_text: str):
    """Send pre-formatted HTML to the configured chat, splitting if needed."""
    limit = 4096
    while html_text:
        chunk = html_text[:limit]
        html_text = html_text[limit:]
        _tg("sendMessage", {"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"})


def reply(text: str):
    """Escape plain text and send to the configured chat."""
    reply_html(html.escape(text))


# ── Offset persistence ────────────────────────────────────────────────────────

def load_offset() -> int:
    try:
        return json.loads(OFFSET_FILE.read_text()).get("offset", 0)
    except Exception:
        return 0


def save_offset(offset: int):
    OFFSET_FILE.write_text(json.dumps({"offset": offset}))


# ── Watchlist editor ──────────────────────────────────────────────────────────

def _read_watchlist() -> str:
    return WATCHLIST_PATH.read_text(encoding="utf-8")


def _write_watchlist(text: str):
    WATCHLIST_PATH.write_text(text, encoding="utf-8")


def _section_add(text: str, section: str, item: str) -> tuple[str, bool]:
    """Add item to a ## section. Returns (new_text, was_added)."""
    pattern = re.compile(rf"(## {re.escape(section)}\n)(.*?)(\n## |\Z)", re.DOTALL)
    m = pattern.search(text)
    if not m:
        return text, False
    body = m.group(2).strip()
    items = [i.strip() for i in re.split(r"[,\n]+", body) if i.strip()]
    if item in items:
        return text, False
    items.append(item)
    new_body = ", ".join(items)
    new_section = m.group(1) + new_body + "\n"
    new_text = text[:m.start()] + new_section + text[m.end():]
    return new_text, True


def _section_remove(text: str, section: str, item: str) -> tuple[str, bool]:
    """Remove item from a ## section. Returns (new_text, was_removed)."""
    pattern = re.compile(rf"(## {re.escape(section)}\n)(.*?)(\n## |\Z)", re.DOTALL)
    m = pattern.search(text)
    if not m:
        return text, False
    body = m.group(2).strip()
    items = [i.strip() for i in re.split(r"[,\n]+", body) if i.strip()]
    if item not in items:
        return text, False
    items.remove(item)
    new_body = ", ".join(items)
    new_section = m.group(1) + new_body + "\n"
    new_text = text[:m.start()] + new_section + text[m.end():]
    return new_text, True


def _geo_add(text: str, topic: str, keyword: str) -> tuple[str, bool]:
    """Add keyword to an existing geo topic, or create a new topic line."""
    section_pat = re.compile(r"(## 地缘政治关键词\n)(.*?)(\n## |\Z)", re.DOTALL)
    m = section_pat.search(text)
    if not m:
        return text, False
    body = m.group(2)
    # Try to find existing topic line
    topic_pat = re.compile(rf"({re.escape(topic)}: )(.*)")
    tm = topic_pat.search(body)
    if tm:
        kws = [k.strip() for k in tm.group(2).split(",") if k.strip()]
        if keyword in kws:
            return text, False
        kws.append(keyword)
        new_body = body[:tm.start()] + tm.group(1) + ", ".join(kws) + body[tm.end():]
    else:
        new_body = body.rstrip() + f"\n{topic}: {keyword}\n"
    new_section = m.group(1) + new_body
    if not new_section.endswith("\n"):
        new_section += "\n"
    return text[:m.start()] + new_section + text[m.end():], True


def _geo_remove(text: str, keyword: str) -> tuple[str, bool]:
    """Remove a keyword from any geo topic line."""
    section_pat = re.compile(r"(## 地缘政治关键词\n)(.*?)(\n## |\Z)", re.DOTALL)
    m = section_pat.search(text)
    if not m:
        return text, False
    body = m.group(2)
    new_body, found = "", False
    for line in body.splitlines(keepends=True):
        if ":" in line:
            head, kws_str = line.split(":", 1)
            kws = [k.strip() for k in kws_str.split(",") if k.strip()]
            if keyword in kws:
                kws.remove(keyword)
                found = True
                line = f"{head}: {', '.join(kws)}\n" if kws else ""
        new_body += line
    new_section = m.group(1) + new_body
    return text[:m.start()] + new_section + text[m.end():], found


# ── Status report ─────────────────────────────────────────────────────────────

def _build_status() -> str:
    wl = _read_watchlist()

    def _get_sec(name):
        m = re.search(rf"## {re.escape(name)}\n(.*?)(\n## |\Z)", wl, re.DOTALL)
        return m.group(1).strip() if m else ""

    stocks = _get_sec("个股与基金")
    commodities = _get_sec("商品期货")
    fx = _get_sec("汇率")
    recipients = _get_sec("收件人")
    geo = _get_sec("地缘政治关键词")

    budget_used = 0
    try:
        b = json.loads(BUDGET_PATH.read_text())
        if b.get("date") == datetime.now(ET).strftime("%Y-%m-%d"):
            budget_used = b.get("used", 0)
    except Exception:
        pass

    serpapi_used = 0
    try:
        ym = datetime.now(ET).strftime("%Y-%m")
        sb = json.loads(SERPAPI_BUDGET_PATH.read_text()) if SERPAPI_BUDGET_PATH.exists() else {}
        if sb.get("year_month") == ym:
            serpapi_used = sb.get("used", 0)
    except Exception:
        pass

    today_et = datetime.now(ET).strftime("%Y-%m-%d")
    monthly_files = sorted(REPORTS_DIR.glob("Daily_Intel_report_*.md"), reverse=True)
    last_am, last_pm = "无", "无"
    for mf in monthly_files:
        text = mf.read_text(encoding="utf-8", errors="replace")
        am_dates = re.findall(r"^## (\d{4}-\d{2}-\d{2}) 开盘前简报", text, re.MULTILINE)
        pm_dates = re.findall(r"^## (\d{4}-\d{2}-\d{2}) 夜盘动向", text, re.MULTILINE)
        if am_dates and last_am == "无":
            last_am = max(am_dates)
        if pm_dates and last_pm == "无":
            last_pm = max(pm_dates)
        if last_am != "无" and last_pm != "无":
            break

    # Scheduled task status via launchctl
    def _launchd_status(label: str) -> str:
        try:
            out = subprocess.check_output(
                ["launchctl", "list", label], stderr=subprocess.DEVNULL, text=True
            )
            pid = next((l.split("=")[1].strip().rstrip(";") for l in out.splitlines()
                        if "PID" in l), None)
            return f"运行中 PID={pid}" if pid and pid != "0" else "已停止"
        except Exception:
            return "未加载"

    report_task = _launchd_status("com.daily-intel.finance")
    tg_task     = _launchd_status("com.daily-intel.finance.telegram")

    # last_am and last_pm already populated above

    return (
        f"<b>Daily Intelligence 状态</b>\n\n"
        f"<b>监控标的</b>\n"
        f"个股/基金：{html.escape(stocks)}\n"
        f"商品期货：{html.escape(commodities)}\n"
        f"汇率：{html.escape(fx)}\n\n"
        f"<b>地缘政治关键词</b>\n{html.escape(geo)}\n\n"
        f"<b>收件人：</b>{html.escape(recipients)}\n\n"
        f"<b>今日（ET）：</b>{today_et}\n"
        f"<b>最近AM报告：</b>{last_am}\n"
        f"<b>最近PM报告：</b>{last_pm}\n"
        f"<b>Tavily今日用量：</b>{budget_used}/{TAVILY_DAILY_LIMIT}\n"
        f"<b>SerpApi本月用量：</b>{serpapi_used}/{SERPAPI_MONTHLY_LIMIT}\n\n"
        f"<b>定时任务</b>\n"
        f"报告任务（5:30/20:59 PT）：{html.escape(report_task)}\n"
        f"Telegram bot（常驻）：{html.escape(tg_task)}\n\n"
        f"<b>推理流水线</b>\n"
        f"预处理+指令分类：{html.escape(LLM_MODEL)}\n"
        f"搜索+事件合成：{html.escape(SEARCH_MODEL)}\n"
        f"个人化推理：{html.escape(REASONING_MODEL)}（Azure）"
    )


# ── OpenRouter HTTP helper (retry on network errors) ─────────────────────────

def _openrouter_post(payload: dict, timeout: int = 30, max_retries: int = 2):
    """POST to OpenRouter with retry on network/5xx errors. Raises on terminal failure."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"OR retry {attempt}/{max_retries} after {wait}s...")
                time.sleep(wait)
            resp = httpx.post(
                OR_BASE_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_error = e
                logger.warning(f"OR attempt {attempt+1}: HTTP {e.response.status_code}")
            else:
                logger.warning(f"OR {e.response.status_code}: {e.response.text[:200]}")
                raise
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = e
            logger.warning(f"OR attempt {attempt+1}: {e}")
    raise last_error


def _deepseek_post(payload: dict, timeout: int = 30, max_retries: int = 2):
    """POST to DeepSeek V4 Flash via OpenRouter with retry on network/5xx errors. Raises on terminal failure.
    No thinking param — thinking:disabled breaks StreamLake; Flash defaults to no-thinking without it."""
    payload = {**payload, "provider": DS_OR_PROVIDERS}
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"DS retry {attempt}/{max_retries} after {wait}s...")
                time.sleep(wait)
            resp = httpx.post(
                OR_BASE_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_error = e
                logger.warning(f"DS attempt {attempt+1}: HTTP {e.response.status_code}")
            else:
                raise
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = e
            logger.warning(f"DS attempt {attempt+1}: {e}")

    # OR flex fallback (gemini-3.1-flash-lite via OpenRouter, service_tier=flex)
    logger.warning(f"OR/Novita unavailable, trying OR flex fallback: {LLM_FALLBACK_FLASH}")
    or_payload = {k: v for k, v in payload.items() if k not in ("thinking", "provider")}
    or_payload["model"] = LLM_FALLBACK_FLASH
    or_payload["service_tier"] = "flex"
    try:
        resp = httpx.post(
            OR_BASE_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            json=or_payload,
            timeout=120,
        )
        resp.raise_for_status()
        logger.info(f"OR flex fallback succeeded: {LLM_FALLBACK_FLASH}")
        return resp
    except Exception as e:
        logger.warning(f"OR flex fallback also failed: {e}")
    raise last_error


# ── Unified preprocessor (intent classification + followup context, single LLM call) ──

def _unified_preprocess(text: str) -> dict:
    """Single V4 Flash call: classify intent AND extract followup context if needed."""
    now_et = datetime.now(ET)
    today     = now_et.strftime("%Y-%m-%d")
    yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    now_str   = now_et.strftime("%Y-%m-%d %H:%M %Z")
    portfolio = _get_portfolio_snapshot()

    prompt = f"""你是 Daily Intelligence Telegram 助手的统一预处理器。
现在时间（NYSE 时区）：{now_str}  |  今天={today}  |  昨天={yesterday}  |  所有时间推理以 NYSE 时区（EDT/EST）为基准

用户持仓快照：
{portfolio[:400] if portfolio else '未知'}

用户消息：{text}

输出 JSON（只输出 JSON，无其他内容），字段如下：

必填：
- "action": 以下之一：
    add_ticker / remove_ticker  （增删个股/ETF/商品/汇率）
    add_geo / remove_geo        （增删地缘政治关键词）
    add_recipient / remove_recipient
    status                      （查看系统状态）
    force_run                   （强制触发报告）
    followup                    （财经/宏观追问）
    unknown

指令类必填（action != followup）：
- "section": 适用时填 "个股与基金" / "商品期货" / "汇率" / "收件人"
- "item": 增删的具体内容（ticker大写，email原样）
- "topic": add_geo时的主题名（如 US-Iran）
- "keyword": add_geo/remove_geo时的关键词

followup类必填（action == followup）：
- "query": 精准英文搜索词（含ticker、确切日期、盘中/盘后/after-hours、事件类型）
- "search_queries": 2条英文搜索词列表，互补覆盖两个角度：
    [0] 事件/催化剂角度（同query，聚焦今日新闻、价格行为、盘后/盘前动态）
    [1] 量化/技术角度（options implied volatility、historical earnings reaction、analyst targets、technical support levels、institutional positioning）
    示例：["NVDA after-hours earnings 2026-05-20 reaction semiconductor", "NVDA options IV earnings historical pattern analyst target price support"]
- "relevant_tickers": 用户持仓中与问题最相关的ticker列表（最多3个）
- "framework_focus": 最相关的投资框架考量（如 "Dream Bucket INTC thesis" / "gold allocation" / "macro rebalancing"）
- "question_intent": 用户真实意图一句话（不是复述，是底层关切）

日期映射：今天={today} 昨天={yesterday} 盘后=after-hours"""

    try:
        resp = _deepseek_post({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 600,
            "temperature": 0,
        }, timeout=20)
        msg = resp.json()["choices"][0]["message"]
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "",
                     (msg.get("content") or msg.get("reasoning_content") or "").strip()).strip()
        result = json.loads(raw)
        logger.info(f"Unified preprocess: {result}")
        return result
    except Exception as e:
        logger.warning(f"Unified preprocess failed: {e}")
        return {"action": "unknown"}


SEARCH_MODEL       = "perplexity/sonar"                 # step 3: sonar fallback
REASONING_MODEL    = "deepseek/deepseek-v4-flash"       # step 4: primary (OR/Novita)
REASONING_FALLBACK = "x-ai/grok-4.3"                   # step 4: fallback on any failure

# ── Follow-up context builders ────────────────────────────────────────────────

_framework_cache: str = ""  # loaded once at bot start, stable across calls


def _load_framework() -> str:
    """Extract key investment framework from 金融资产信息.md. Module-level cache."""
    global _framework_cache
    if _framework_cache:
        return _framework_cache
    asset_file = OBSIDIAN / "Finance" / "金融资产信息.md"
    if not asset_file.exists():
        return ""
    text = re.sub(r"^---.*?---\s*", "", asset_file.read_text(encoding="utf-8"), flags=re.DOTALL)
    parts = []
    # 总体构架
    m = re.search(r"## 总体构架\n(.*?)(?=\n##)", text, re.DOTALL)
    if m:
        parts.append("【目标构架】\n" + m.group(1).strip()[:500])
    # Dream Bucket (2026 update)
    m2 = re.search(r"##### Dream Bucket投资逻辑\n(.*?)(?=\n#####|\n##)", text, re.DOTALL)
    if m2:
        parts.append("【Dream Bucket】\n" + m2.group(1).strip()[:350])
    _framework_cache = "\n\n".join(parts)[:900]
    return _framework_cache


def _get_portfolio_snapshot() -> str:
    """Extract IB US stock holdings from Finance/portfolio_report_latest.md."""
    try:
        latest_path = OBSIDIAN / "Finance" / "portfolio_report_latest.md"
        if not latest_path.exists():
            return ""
        text = latest_path.read_text(encoding="utf-8")

        time_m = re.search(r"\*\*生成时间[（(]美东[）)]：(.*?)\*\*", text)
        total_m = re.search(r"\*\*组合总市值：(.*?)\*\*", text)
        header = []
        if time_m:
            header.append(f"持仓时间：{time_m.group(1).strip()}")
        if total_m:
            header.append(f"总市值：{total_m.group(1).strip()}")

        # Extract only IB section (US stocks, stop at 招商银行 section)
        ib_section = re.search(r"## IB（账户 0611）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return "\n".join(header)

        holdings = re.findall(
            r"### (\w+) \w+ \d+\n.*?均价：([\d.]+).*?浮盈：[^（]+（([+\-\d.]+%)）",
            ib_section.group(1), re.DOTALL
        )
        # Skip CASH entries
        us_stocks = [(sym, cost, pnl) for sym, cost, pnl in holdings
                     if not sym.startswith("CASH") and sym.isalpha()]

        lines = header[:]
        lines.append("IB美股持仓（成本价为均价，浮盈%为报告日数据供参考，实时盈亏请结合yfinance现价计算）：")
        for sym, cost, pnl in us_stocks:
            lines.append(f"  {sym}  成本@{cost}  报告浮盈{pnl}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Portfolio snapshot failed: {e}")
        return ""


def _mempalace_context(question: str) -> str:
    """Query MemPalace bridge for question-relevant context (fail-open).
    Searches both finance (investment notes) and hermes (daily report history) rooms."""
    import urllib.request

    def _search(room: str, n: int) -> list:
        try:
            payload = json.dumps({
                "query": question, "wing": "paperview", "room": room,
                "n_results": n, "max_distance": 0.80,
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8765/mempalace/search",
                data=payload, headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read()).get("results", [])
        except Exception:
            return []

    hits = _search("finance", 2) + _search("hermes", 2)
    if not hits:
        return ""
    lines = [
        f"[{h.get('source_file','?')} sim={h.get('similarity',0):.2f}] "
        f"{h.get('text','')[:180].replace(chr(10),' ')}"
        for h in hits
    ]
    return "\n".join(lines)


# Static system prompt — identical across all follow-up calls → KV cache hit
# Framework injected here (changes rarely); portfolio/mempalace go in user msg.
def _followup_system() -> str:
    fw = _load_framework()
    base = (
        "你是 Daily Intelligence 财经情报助手。\n"
        "实际数据来源：yfinance 实时价格、NYT/BBC/FT RSS 新闻、Perplexity 实时联网搜索。\n"
        "严禁声称拥有未实现的能力（Bloomberg实时、SEC文件、期权监测等）。\n"
        "回答规则：\n"
        "1. 若问题包含具体事件前提（如【XX在某日大涨】），先用联网搜索核实该前提是否属实，再回答。\n"
        "2. 若前提有误，明确指出，不要顺着错误前提编造答案。\n"
        "3. 回答简洁直接，中文，500字以内，引用来源和日期。"
    )
    if fw:
        return base + "\n\n== 用户投资背景 ==\n" + fw
    return base


def _preprocess_question(pre: dict, question: str) -> dict:
    """Extract followup fields from unified preprocess result (no extra LLM call)."""
    return {
        "query":              pre.get("query", question),
        "search_queries":     pre.get("search_queries") or [],
        "relevant_tickers":   pre.get("relevant_tickers", []),
        "framework_focus":    pre.get("framework_focus", ""),
        "question_intent":    pre.get("question_intent", question),
    }


# P2: Aggregator domains — higher information density per extract request
_AGGREGATOR_DOMAINS = frozenset({
    "stockanalysis.com", "macrotrends.net", "finviz.com",
    "tipranks.com", "wisesheets.io", "finance.yahoo.com",
    "seekingalpha.com", "simplywall.st", "stockanalysis.com",
})


def _agg_score(url: str) -> int:
    """0 for aggregator domains (sorts first), 1 for others."""
    return 0 if any(d in (url or "") for d in _AGGREGATOR_DOMAINS) else 1


def _parallel_research(queries: list[str]) -> str:
    """Step 3 primary: Parallel.ai search + extract — raw full-text articles, no pre-summary.
    Accepts multiple queries for multi-angle coverage (same request, $0.005 total).
    Extract: $0.001/URL (3 URLs). Fail-open."""
    if not PARALLEL_API_KEY or not queries:
        return ""
    try:
        from parallel import Parallel
        client = Parallel(api_key=PARALLEL_API_KEY)

        primary_query = queries[0]

        # Search — pass all queries in one request (billed as one)
        search_resp = client.search(
            search_queries=queries,
            objective=primary_query,
        )
        results = search_resp.results or []
        if not results:
            return ""

        # Deduplicate by title similarity before selecting extract URLs
        seen_titles: set[str] = set()
        deduped: list = []
        for r in results:
            title_key = re.sub(r'\W+', '', (r.title or "").lower())[:40]
            if title_key and title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            deduped.append(r)

        # P2: Prioritise aggregator pages for extract (higher information density)
        deduped.sort(key=lambda r: _agg_score(r.url or ""))

        # Extract top 3 URLs for full text
        urls = [r.url for r in deduped[:3] if r.url]
        query_label = " | ".join(queries)
        lines = [f"[Parallel.ai 情报（搜索词：{query_label}）]"]

        if urls:
            extract_resp = client.extract(
                urls=urls,
                objective=primary_query,
            )
            seen_paragraphs: set[str] = set()
            for r in (extract_resp.results or []):
                raw = r.full_content or " ".join(r.excerpts or [])
                if not raw:
                    continue
                # Strip boilerplate: short lines, nav text, markdown links
                paras = [p.strip() for p in raw.split("\n") if len(p.strip()) > 60
                         and not p.strip().startswith(("[", "!", "*", "#", "http"))]
                # Dedup paragraphs across articles
                unique_paras = []
                for p in paras:
                    key = p[:60].lower()
                    if key not in seen_paragraphs:
                        seen_paragraphs.add(key)
                        unique_paras.append(p)
                content = "\n".join(unique_paras)[:4000]
                if not content:
                    continue
                pub = f" ({r.publish_date})" if r.publish_date else ""
                lines.append(f"\n### {r.title or r.url}{pub}")
                lines.append(content)

        # Remaining deduped results as excerpt summaries (no full extract)
        for r in deduped[3:6]:
            excerpt = " ".join(r.excerpts or [])[:300]
            if excerpt:
                lines.append(f"\n- {r.title or r.url}: {excerpt}")

        brief = "\n".join(lines)
        logger.info(f"Parallel research: {len(brief)} chars ({len(urls)} extracts, {len(results)} results, {len(queries)} queries)")
        return brief
    except Exception as e:
        logger.warning(f"Parallel research failed: {e}")
        return ""


def _detect_research_gap(question_intent: str, queries: list[str], brief: str) -> str | None:
    """P1: Single V4 Flash call — check if a 3rd search query would add meaningful coverage.
    Returns an English query string or None. Fail-open (returns None on any error).
    Cost: ~$0.0001 per call."""
    try:
        prompt = (
            f"User question: {question_intent}\n"
            f"Already searched: {'; '.join(queries)}\n\n"
            f"Research summary (first 1500 chars):\n{brief[:1500]}\n\n"
            "Is there a clear gap — missing recent price path, market reaction data, "
            "OR fundamental explanation? "
            "If yes, output ONE concise English search query (<25 words). "
            "If coverage is sufficient, output exactly: null\n"
            "Output only the query or null, nothing else."
        )
        resp = httpx.post(
            OR_BASE_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": REASONING_MODEL,
                "provider": DS_OR_PROVIDERS,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0.1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip().strip('"\'')
        if result.lower() in ("null", "none", "no", ""):
            return None
        return result if len(result) < 150 else None  # sanity-cap
    except Exception as e:
        logger.debug(f"Gap detection skipped: {e}")
        return None


def _exa_search(query: str) -> str:
    """Exa.ai fallback for Sonar: OpenAI-compatible endpoint, search+synthesis in one call.
    Free 1000 req/month. Freshness: ~4-10h for indexed content, livecrawl for breaking news."""
    if not EXA_API_KEY:
        return ""
    try:
        resp = httpx.post(
            EXA_BASE_URL,
            headers={"Authorization": f"Bearer {EXA_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "exa",
                "messages": [{"role": "user", "content": query}],
                "text": True,
                "category": "news",
            },
            timeout=30,
        )
        resp.raise_for_status()
        brief = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"Exa fallback brief: {len(brief)} chars")
        return brief
    except Exception as e:
        logger.warning(f"Exa fallback also failed: {e}")
        return ""


def _sonar_research(query: str, price_context: str = "") -> str:
    """Step 3 fallback: Sonar — search + synthesis when Parallel.ai fails.
    Fallback chain: Sonar retry → Exa model="exa"."""
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")
    system = (
        f"You are a financial news researcher. Current time: {now_str} (NYSE timezone, America/New_York). "
        "Reason in Eastern Time (EDT in summer, EST in winter) for all time references. "
        "Be comprehensive about all catalysts, drivers, and market reactions. "
        "Cite sources with dates. Do not speculate beyond what sources say."
    )
    if price_context:
        system += (
            f"\n\nReal-time price data (from yfinance, authoritative): {price_context}. "
            "Focus your search on what drove THIS specific move TODAY. "
            "If you cannot find a new catalyst for today specifically, say so explicitly — "
            "do NOT infer today's price move from historical articles."
        )
    payload = {
        "model": SEARCH_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "max_tokens": 1500,
        "temperature": 0.1,
    }
    for attempt in range(2):
        try:
            resp = _openrouter_post(payload, timeout=30)
            brief = resp.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"Sonar brief: {len(brief)} chars")
            return brief
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Sonar research attempt 1 failed ({e}), retrying in 5s...")
                time.sleep(5)
            else:
                logger.warning(f"Sonar research failed after retry ({e}), trying Exa fallback")
    return _exa_search(query)


def _finnhub_quote(ticker: str) -> dict | None:
    """GET /quote — returns {price, prev_close, change_pct} or None. Fail-open."""
    if not FINNHUB_API_KEY:
        return None
    try:
        r = httpx.get(
            f"{FINNHUB_BASE}/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=8,
        )
        d = r.json()
        if not d.get("c"):  # 0 or missing means no data for this symbol
            return None
        return {"price": d["c"], "prev_close": d["pc"], "change_pct": d["dp"], "t": d.get("t", 0)}
    except Exception as e:
        logger.warning(f"Finnhub quote {ticker}: {e}")
        return None


def _finnhub_news(tickers: list[str], days: int = 2) -> str:
    """GET /company-news — formatted fallback news string. Fail-open."""
    if not FINNHUB_API_KEY:
        return ""
    from datetime import timezone
    today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_s  = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    items: list[tuple[int, str]] = []
    seen: set[str] = set()
    for ticker in tickers[:3]:
        try:
            r = httpx.get(
                f"{FINNHUB_BASE}/company-news",
                params={"symbol": ticker, "from": from_s, "to": today_s, "token": FINNHUB_API_KEY},
                timeout=10,
            )
            for n in (r.json() or [])[:20]:
                headline = (n.get("headline") or "").strip()
                if not headline or headline in seen:
                    continue
                seen.add(headline)
                ts = n.get("datetime", 0)
                pub_et = datetime.fromtimestamp(ts, tz=ET).strftime("%m-%d %H:%M %Z") if ts else "?"
                source  = n.get("source", "Finnhub")
                summary = (n.get("summary") or "")[:100]
                line = f"[{pub_et}] {headline} ({source})"
                if summary:
                    line += f" — {summary}"
                items.append((ts, line))
        except Exception as e:
            logger.warning(f"Finnhub news {ticker}: {e}")
    if not items:
        return ""
    items.sort(key=lambda x: x[0], reverse=True)
    lines = [f"== Finnhub 新闻（过去{days * 24}h，来源 Finnhub）=="]
    lines += [item[1] for item in items[:8]]
    return "\n".join(lines)


def _is_trading_hours(now_et: "datetime") -> bool:
    """Return True when yfinance can provide real-time prices.

    yfinance covers weekday sessions 04:00-20:00 ET (pre/regular/post-market).
    Outside this window (overnight 20:00-03:50 ET, or any weekend) yfinance
    only has stale last-official-close data — use IBKR gateway instead.

    # FUTURE: once IBKR gateway is confirmed stable, remove this function
    # and always prefer IBKR, using yfinance as fallback.
    """
    if now_et.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    hm = now_et.hour * 60 + now_et.minute
    return 240 <= hm < 1200     # 04:00-20:00 ET weekdays


def _fetch_ibkr_prices(tickers: list[str], ts: str) -> str:
    """IBKR Client Portal Gateway integration — currently disabled. Returns '' unconditionally.

    Original implementation imported ibkr/quotes.py, checked gateway auth, and returned
    session-aware prices (pre/post-market including ATS/OTC). Re-enable by restoring the
    body below and shipping the ibkr/ module alongside this script.
    """
    # IBKR_DISABLED: gateway not bundled in this distribution
    return ""


def _fetch_yfinance_prices(tickers: list[str], ts: str, stale_note: bool = False) -> str:
    """Fetch prices from yfinance Ticker.info, session-aware. Returns formatted string or ''.

    stale_note=True: append '非实时' label (used when yfinance is fallback in off-hours).
    """
    try:
        import yfinance as yf
        now_et = datetime.now(ET)
        hour_min = now_et.hour * 60 + now_et.minute
        rows = []
        for ticker in tickers[:3]:
            try:
                tk = yf.Ticker(ticker)
                info = tk.info
                reg_price  = info.get("regularMarketPrice")
                post_price = info.get("postMarketPrice")
                pre_price  = info.get("preMarketPrice")
                prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")

                if 240 <= hour_min < 570:
                    price, ref, ref_label, session = pre_price or reg_price, prev_close, "昨收", "盘前"
                elif 570 <= hour_min < 960:
                    price, ref, ref_label, session = reg_price, prev_close, "昨收", "常规"
                elif 960 <= hour_min < 1200:
                    price, ref, ref_label, session = (post_price or reg_price), (reg_price or prev_close), "今收", "盘后"
                else:
                    price, ref, ref_label, session = reg_price, prev_close, "昨收", "休市"

                if not price:
                    hist = tk.history(period="2d", interval="1m", prepost=True)
                    if not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                        session += "(hist)"

                if not price:
                    continue
                price = float(price)
                note = "，非实时" if stale_note else ""
                if ref and float(ref) > 0:
                    pct = f"{(price / float(ref) - 1) * 100:+.2f}%"
                    rows.append(f"  {ticker}: ${price:.2f}（{session}，vs {ref_label} ${float(ref):.2f}，{pct}{note}）")
                else:
                    rows.append(f"  {ticker}: ${price:.2f}（{session}{note}）")
            except Exception:
                pass
        if rows:
            src = "Yahoo Finance(fallback，非实时)" if stale_note else "Yahoo Finance"
            return f"== 实时行情（{src}，{ts}）==\n" + "\n".join(rows)
    except Exception as e:
        logger.warning(f"yfinance price fetch failed: {e}")
    return ""


def _fetch_realtime_prices(tickers: list[str]) -> str:
    """Fetch latest prices with tiered data source strategy.

    Routing (IBKR disabled — _fetch_ibkr_prices returns '' unconditionally):
      Weekday 04:00-20:00 ET:
        1. yfinance (primary)  — pre/regular/post-market fields, session-aware
        2. Finnhub             — last resort, regular session only

      Overnight + weekends:
        1. yfinance            — labeled '非实时'
        2. Finnhub             — last resort
    """
    if not tickers:
        return ""
    now_et = datetime.now(ET)
    ts     = now_et.strftime("%Y-%m-%d %H:%M %Z")
    trading = _is_trading_hours(now_et)

    if trading:
        # ── Trading hours: yfinance primary ───────────────────────────────────
        result = _fetch_yfinance_prices(tickers, ts, stale_note=False)
        if result:
            return result
        logger.warning("yfinance failed during trading hours, trying IBKR fallback")
        result = _fetch_ibkr_prices(tickers, ts)
        if result:
            return result
    else:
        # ── Off-hours / weekend: IBKR primary ────────────────────────────────
        result = _fetch_ibkr_prices(tickers, ts)
        if result:
            return result
        logger.warning("IBKR unavailable off-hours, falling back to yfinance (stale)")
        result = _fetch_yfinance_prices(tickers, ts, stale_note=True)
        if result:
            return result

    # ── Last resort: Finnhub (regular session only) ───────────────────────────
    logger.warning("All price sources failed, trying Finnhub")
    # Finnhub free tier has no pre/post-market data; always stale off trading hours
    finnhub_stale = not trading
    rows = []
    for ticker in tickers[:3]:
        q = _finnhub_quote(ticker)
        if not q:
            continue
        pct = f"{q['change_pct']:+.2f}%"
        note = "，非实时" if finnhub_stale else ""
        rows.append(
            f"  {ticker}: ${q['price']:.2f}"
            f"（vs 昨收 ${q['prev_close']:.2f}，{pct}；来源 Finnhub，仅常规时段{note}）"
        )
    if not rows:
        return ""
    stale_tag = "，非实时" if finnhub_stale else ""
    return f"== 实时行情（Finnhub fallback，{ts}，不含盘前/盘后{stale_tag}）==\n" + "\n".join(rows)


def _fetch_yfinance_news(tickers: list[str], hours: int = 48) -> str:
    """Fetch recent news via yfinance.news, Finnhub as fallback. Fail-open."""
    if not tickers:
        return ""
    # ── Primary: yfinance.news ────────────────────────────────────────────────
    try:
        import yfinance as yf
        from datetime import timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        items: list[tuple[str, str]] = []
        seen_titles: set[str] = set()
        for ticker in tickers[:3]:
            try:
                news_list = yf.Ticker(ticker).news or []
                for n in news_list:
                    content = n.get("content", {})
                    title = content.get("title", "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    pub_str = content.get("pubDate", "")
                    try:
                        pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                        if pub_dt < cutoff:
                            continue
                        pub_et = pub_dt.astimezone(ET).strftime("%m-%d %H:%M %Z")
                    except Exception:
                        pub_et = pub_str[:16] if pub_str else "?"
                    provider = content.get("provider", {}).get("displayName", "YF")
                    summary = content.get("summary", "")
                    line = f"[{pub_et}] {title} ({provider})"
                    if summary:
                        line += f" — {summary[:120]}"
                    items.append((pub_str, line))
            except Exception:
                pass
        if items:
            items.sort(key=lambda x: x[0], reverse=True)
            lines = [f"== Yahoo Finance 新闻（过去{hours}h，来源 yfinance.news）=="]
            lines += [item[1] for item in items[:8]]
            return "\n".join(lines)
        logger.warning("yfinance.news returned no items, trying Finnhub fallback")
    except Exception as e:
        logger.warning(f"yfinance news fetch failed ({e}), trying Finnhub fallback")
    # ── Fallback: Finnhub company-news ────────────────────────────────────────
    return _finnhub_news(tickers, days=max(1, hours // 24))


def _llm_followup(question: str, pre: dict) -> str:
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")

    # Followup context from unified preprocess (no extra LLM call)
    ctx = _preprocess_question(pre, question)
    search_query     = ctx["query"]
    relevant_tickers  = ctx["relevant_tickers"]
    framework_focus   = ctx["framework_focus"]
    question_intent   = ctx["question_intent"]

    # Step 2: real-time price + news (yfinance, free, no budget cost)
    price_anchor = ""
    yf_news = ""
    if relevant_tickers:
        reply(f"获取实时行情及新闻（{', '.join(relevant_tickers)}）...")
        price_anchor = _fetch_realtime_prices(relevant_tickers)
        yf_news = _fetch_yfinance_news(relevant_tickers)

    # Detect if off-hours OTC/overnight data is unavailable (IBKR not used)
    _off_hours_no_otc = (
        bool(relevant_tickers)
        and not _is_trading_hours(datetime.now(ET))
        and "IBKR" not in price_anchor
    )

    # Step 3: Parallel.ai search + extract (primary) → Sonar fallback (→ Exa fallback)
    search_queries = ctx.get("search_queries") or [search_query]
    if isinstance(search_queries, list) and len(search_queries) > 0:
        search_queries = [q for q in search_queries if isinstance(q, str) and q.strip()]
    if not search_queries:
        search_queries = [search_query]
    reply(f"情报检索（{len(search_queries)}条查询）...")
    research_brief = _parallel_research(search_queries)
    research_source = "Parallel.ai"

    # P1: Adaptive 3rd query — gap detection (only when Parallel succeeded, max 1 extra query)
    if research_brief and len(search_queries) < 3:
        gap_q = _detect_research_gap(question_intent, search_queries, research_brief)
        if gap_q:
            logger.info(f"Gap detected, 3rd query: {gap_q}")
            reply("情报补充（补漏查询）...")
            extra = _parallel_research([gap_q])
            if extra:
                research_brief = research_brief + "\n\n" + extra
                search_queries = search_queries + [gap_q]

    if not research_brief:
        logger.warning("Parallel research empty, falling back to Sonar")
        price_context_for_sonar = "; ".join(
            line.strip() for line in price_anchor.splitlines() if line.startswith("  ")
        ) if price_anchor else ""
        research_brief = _sonar_research(search_query, price_context=price_context_for_sonar)
        research_source = "Sonar"
    if not research_brief:
        research_brief = "(情报检索失败)"
        research_source = "N/A"

    # Step 4: Gemini 3.5 Flash via OR flex — personalised reasoning; fallback: Grok 4.3
    reply("推理中（整合实时行情 + 情报 + 持仓框架 + 知识库）...")
    portfolio = _get_portfolio_snapshot()
    mp_ctx    = _mempalace_context(question)
    fw        = _load_framework()

    system = (
        "你是服务于特定投资者的专属财经分析师。你收到的信息包括：实时情报（原始新闻全文或搜索简报）"
        "、该投资者的实际持仓、投资框架和个人知识库上下文。\n"
        "你的任务：结合这些信息，给出有实质价值的个人化分析——不是重复新闻，"
        "而是判断这个事件对这位具体投资者的持仓逻辑、框架考量、和下一步观察点意味着什么。\n"
        "风格：情报分析报告措辞，冷静直接，中文，不设字数上限，展开分析不要截断。\n"
        "严禁用'收到'、'以下是基于...'、'好的'等对话体开场白，直接从分析内容开始。\n"
        "所有时间推理以纽约证券交易所所在时区（America/New_York，夏令时 EDT/冬令时 EST）为基准。"
    )
    if fw:
        system += f"\n\n== 用户投资框架 ==\n{fw}"

    user_parts = [f"当前时刻：{now_str}"]
    user_parts.append(f"用户问题真实意图：{question_intent}")
    if price_anchor:
        user_parts.append(
            price_anchor + "\n"
            "（价格基准：以上 yfinance 数据为唯一权威，Sonar 文章中的价格仅供参考，如有矛盾以 yfinance 为准）"
        )
    if yf_news:
        user_parts.append(yf_news)
    user_parts.append(f"== 事件情报（来源：{research_source}，搜索词：{search_query}）==\n{research_brief}")
    if portfolio:
        user_parts.append(f"== 当前持仓快照 ==\n{portfolio}")
    if relevant_tickers:
        user_parts.append(f"本次问题直接相关持仓：{', '.join(relevant_tickers)}")
    if framework_focus:
        user_parts.append(f"最相关框架考量：{framework_focus}")

    if mp_ctx:
        user_parts.append(f"== 知识库上下文（向量检索）==\n{mp_ctx}")

    user_parts.append(
        f"\n原始问题：{question}\n\n"
        "推理规则（必须遵守）：\n"
        "- 价格以 yfinance 实时行情为唯一基准；文章中的价格数字如有矛盾一律忽略\n"
        "- 若情报未找到今日明确新催化剂，直接声明动量延续并指出原始催化剂，不从历史文章推断今日走势\n"
        "- 推理前先梳理时间线：明确区分历史事件与今日盘前/盘后动态\n\n"
        "分析维度参考（自由展开，不要机械填格子）：\n"
        "- 核心驱动力与今日催化剂\n"
        "- 对我当前持仓的具体含义（结合持仓成本、框架、触发条件）\n"
        "- 知识库历史关联（有则引用，无则略过）\n"
        "- 待验证信号与信息缺口"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    # Step 4: DeepSeek V4 Flash via OR/Novita (own retry, bypasses _deepseek_post OR-flex fallback)
    # → Grok 4.3 via OR on total failure. model_label reflects actual model used.
    model_label = "V4 Flash"
    resp = None
    last_err = None
    for attempt in range(3):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"Step 4 DS retry {attempt}/2 after {wait}s...")
                time.sleep(wait)
            resp = httpx.post(
                OR_BASE_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": REASONING_MODEL, "provider": DS_OR_PROVIDERS,
                      "messages": messages,
                      "max_tokens": 8000, "temperature": 0.3},
                timeout=120,
            )
            resp.raise_for_status()
            break
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_err = e
            logger.warning(f"Step 4 DS attempt {attempt+1}: {e}")
            resp = None
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_err = e
                logger.warning(f"Step 4 DS attempt {attempt+1}: HTTP {e.response.status_code}")
                resp = None
            else:
                last_err = e
                resp = None
                break

    if resp is None:
        logger.warning(f"Step 4 DeepSeek exhausted, trying {REASONING_FALLBACK}")
        try:
            resp = _openrouter_post({
                "model": REASONING_FALLBACK,
                "messages": messages,
                "max_tokens": 8000,
                "temperature": 0.3,
            }, timeout=120)
            model_label = "Grok 4.3"
        except Exception as e2:
            return f"（LLM 调用失败：{e2}）"

    try:
        raw_content = resp.json()["choices"][0]["message"]["content"].strip()
        answer = raw_content
        _append_followup(question, search_query, research_brief, answer, now_str,
                         research_source=research_source)
        otc_note = " · 本回答未纳入场外OTC/隔夜报价" if _off_hours_no_otc else ""
        final_html = f"{html.escape(answer)}\n\n<i>{html.escape(now_str)} · V4 Flash + {html.escape(research_source)} + {html.escape(model_label)}{html.escape(otc_note)}</i>"
        reply_html(final_html)
        return ""
    except Exception as e:
        return f"（LLM 调用失败：{e}）"


def _append_followup(question: str, search_query: str,
                     research_brief: str, answer: str, now_str: str,
                     research_source: str = "Parallel.ai") -> None:
    """Append Q&A record to the monthly report file for Obsidian/MemPalace indexing."""
    try:
        today_et = datetime.now(ET).strftime("%Y-%m-%d")
        ym = today_et[:7].replace("-", "")
        path = REPORTS_DIR / f"Daily_Intel_report_{ym}.md"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        section = (
            f"\n## 追问 {now_str}\n\n"
            f"**追问：** {question}\n\n"
            f"**情报（{research_source}，搜索词：{search_query}）：**\n{research_brief}\n\n"
            f"**回答：**\n{answer}\n\n---\n"
        )
        if not path.exists():
            ym_display = today_et[:7]
            header = f"---\ndate: {ym_display}\nsource: Daily Intelligence\n---\n\n# Daily Intelligence {ym_display}\n"
            path.write_text(header + section, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as f:
                f.write(section)
        logger.info(f"Followup appended to {path.name}")
    except Exception as e:
        logger.warning(f"Failed to append followup: {e}")


# ── Command executor ──────────────────────────────────────────────────────────

def execute(cmd: dict) -> str:
    action = cmd.get("action", "unknown")

    if action in ("add_ticker", "remove_ticker"):
        section = cmd.get("section", "个股与基金")
        item = cmd.get("item", "").upper()
        if not item:
            return "未识别标的名称。"
        wl = _read_watchlist()
        if action == "add_ticker":
            new_wl, ok = _section_add(wl, section, item)
            msg = f"已添加 {item} 到【{section}】" if ok else f"{item} 已存在于【{section}】"
        else:
            new_wl, ok = _section_remove(wl, section, item)
            msg = f"已从【{section}】删除 {item}" if ok else f"{item} 不在【{section}】中"
        if ok:
            _write_watchlist(new_wl)
        return msg

    elif action == "add_geo":
        topic = cmd.get("topic", "General")
        keyword = cmd.get("keyword", "")
        if not keyword:
            return "未识别关键词。"
        wl = _read_watchlist()
        new_wl, ok = _geo_add(wl, topic, keyword)
        if ok:
            _write_watchlist(new_wl)
        return f"已添加关键词「{keyword}」到【{topic}】" if ok else f"「{keyword}」已存在"

    elif action == "remove_geo":
        keyword = cmd.get("keyword", "")
        if not keyword:
            return "未识别关键词。"
        wl = _read_watchlist()
        new_wl, ok = _geo_remove(wl, keyword)
        if ok:
            _write_watchlist(new_wl)
        return f"已删除地缘政治关键词「{keyword}」" if ok else f"「{keyword}」未找到"

    elif action == "add_recipient":
        item = cmd.get("item", "")
        if "@" not in item:
            return "未识别邮件地址。"
        wl = _read_watchlist()
        new_wl, ok = _section_add(wl, "收件人", item)
        if ok:
            _write_watchlist(new_wl)
        return f"已添加收件人 {item}" if ok else f"{item} 已存在"

    elif action == "remove_recipient":
        item = cmd.get("item", "")
        wl = _read_watchlist()
        new_wl, ok = _section_remove(wl, "收件人", item)
        if ok:
            _write_watchlist(new_wl)
        return f"已删除收件人 {item}" if ok else f"{item} 未找到"

    elif action == "status":
        reply_html(_build_status())
        return ""

    elif action == "force_run":
        now_et = datetime.now(ET)
        slot = "pm" if now_et.hour >= 18 else "am"
        python = str(Path(sys.executable))
        script = str(Path(__file__).parent / "run_finance.py")
        env = os.environ.copy()
        env["OBSIDIAN_PATH"] = str(OBSIDIAN)
        env["FINANCE_FORCE_RUN"] = "1"  # bypass dedup in run_finance.py
        subprocess.Popen([python, script], env=env)
        return f"强制运行已启动（slot={slot}），约 2 分钟后报告送达。"

    elif action == "followup":
        question = cmd.get("question", cmd.get("_raw_text", ""))
        if not question:
            return "未识别问题内容。"
        return _llm_followup(question, cmd)

    else:
        return (
            "未识别指令。支持：\n"
            "• 加/删 ticker（如「加 MSFT」）\n"
            "• 加/删地缘政治关键词\n"
            "• 加/删收件人\n"
            "• 状态\n"
            "• 强制运行\n"
            "• 直接提问（财经追问）"
        )


# ── Main long-polling loop ────────────────────────────────────────────────────

def run():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("FINANCE_TELEGRAM_BOT_TOKEN or FINANCE_TELEGRAM_CHAT_ID not set")
        sys.exit(1)

    logger.info("Finance Telegram bot started (long polling)")
    offset = load_offset()

    POLL_TIMEOUT = 30  # server-side long-poll wait (Telegram payload param)

    while True:
        try:
            # Client read timeout must exceed the server-side long-poll wait,
            # otherwise httpx aborts the read before Telegram's 30s window
            # elapses on every empty poll — this was firing on nearly every
            # cycle (34k+ "read operation timed out" over 5 days) and the
            # abandoned-but-still-pending long polls on Telegram's side then
            # collided with the next request as HTTP 409 Conflict (issue #20).
            resp = _tg(
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT, "limit": 20},
                timeout=POLL_TIMEOUT + 5,
            )
            updates = resp.get("result", [])
        except Exception as e:
            logger.warning(f"getUpdates error: {e}, retrying in 5s")
            import time; time.sleep(5)
            continue

        for update in updates:
            update_id = update.get("update_id", 0)
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            offset = update_id + 1
            save_offset(offset)

            if chat_id != str(CHAT_ID):
                logger.info(f"Ignored message from unknown chat {chat_id}")
                continue

            if not text:
                continue

            # Immediate ack before any LLM call — user sees response in < 1s
            _tg("sendMessage", {"chat_id": CHAT_ID, "text": "收到，处理中...", "parse_mode": "HTML"})

            logger.info(f"Command: {text!r}")
            cmd = _unified_preprocess(text)
            cmd["_raw_text"] = text  # pass original text for followup
            logger.info(f"Parsed: {cmd}")
            result = execute(cmd)
            if result:
                reply(result)


if __name__ == "__main__":
    run()
