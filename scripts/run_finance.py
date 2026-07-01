#!/usr/bin/env python3
"""
Daily Intelligence
==================
每个 NYSE 交易日开盘前 1 小时运行（launchd 5:30 AM PT）。
从 Obsidian watchlist.md 读取监控配置，输出中文情报简报。

隔离说明
--------
本脚本与 china-intel（run_intel.py）完全隔离：
  - 独立收件人列表（来自 watchlist.md，非 intel_config.yaml）
  - 独立 Tavily 预算（finance_tavily_budget.json，上限 10次/日）
  - 独立 Obsidian 路径（Hermes/Daily Intelligence/Daily Reports/）
  - 不使用 seen_urls.json / article_cache.json / fetch_log.json

运行环境：宿主机（非容器），直接访问 Yahoo Finance 网络。
"""
import sys, os

# ── Environment detection: host vs container ─────────────────────────────────
_HOME = os.path.expanduser("~")
_IN_CONTAINER = os.path.exists("/opt/data")

if _IN_CONTAINER:
    _OBSIDIAN_ROOT = "/opt/obsidian"
    _INTEL_SCRIPTS = "/opt/data/skills/intel/china-intel/scripts"
else:
    _OBSIDIAN_ROOT = os.path.join(
        _HOME,
        "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview"
    )
    _INTEL_SCRIPTS = os.path.join(_HOME, "MI")

sys.path.insert(0, _INTEL_SCRIPTS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
_DI_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_DI_ENV_FILE)

import fcntl
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

from fetch_prices import fetch_prices, format_price_table, get_anomalies
from fetch_news import fetch_rss, fetch_guardian_news, format_news_for_prompt
from memory_context_finance import get_finance_context

from finance_email import send_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

OBSIDIAN    = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))

# Project-local paths (self-contained within ~/Daily_Intelligence/)
_PROJ_DIR        = Path(os.path.dirname(os.path.abspath(__file__))).parent
WATCHLIST_PATH   = OBSIDIAN / "Hermes/Daily Intelligence/watchlist.md"
REPORTS_DIR      = OBSIDIAN / "Hermes/Daily Intelligence/Daily Reports"
BUDGET_PATH      = _PROJ_DIR / "finance_tavily_budget.json"
ARCHIVE_DIR      = _PROJ_DIR / "archives"  # Extract full-text archive (outside Obsidian, never mined)
LOCK_FILE        = _PROJ_DIR / "run_finance.lock"  # Prevents concurrent duplicate runs

TAVILY_API_KEY      = os.getenv("TAVILY_API_KEY", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE_URL         = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL           = "deepseek/deepseek-v4-flash"
LLM_MODEL_PASS2     = "deepseek/deepseek-v4-pro"
SEMANTIC_FILTER_MODEL = "deepseek/deepseek-v4-flash"
LLM_FALLBACK_FLASH  = "google/gemini-3.1-flash-lite"  # OR flex fallback for v4-flash
LLM_FALLBACK_PRO    = "google/gemini-3.5-flash"       # OR flex fallback for v4-pro
DS_OR_PROVIDERS     = {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}
SONAR_MODEL         = "perplexity/sonar"             # Macro brief (real-time search)
EXA_API_KEY         = os.getenv("EXA_API_KEY", "")
EXA_BASE_URL        = "https://api.exa.ai/chat/completions"

TAVILY_DAILY_LIMIT    = 20
SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY", "")
SERPAPI_MONTHLY_LIMIT = 250
SERPAPI_BUDGET_PATH   = _PROJ_DIR / "finance_serpapi_budget.json"
FINNHUB_API_KEY       = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE          = "https://finnhub.io/api/v1"

# Social sentiment sources (issue C4): Polymarket (free, no auth) + Adanos X/Twitter (free tier, keyed)
POLYMARKET_BASE        = "https://gamma-api.polymarket.com"
ADANOS_API_KEY         = os.getenv("ADANOS_API_KEY", "")
ADANOS_BASE            = "https://api.adanos.org"
ADANOS_MONTHLY_LIMIT   = 200  # free tier is 250/mo; leave headroom for TG follow-ups/manual reruns
ADANOS_BUDGET_PATH     = _PROJ_DIR / "finance_adanos_budget.json"

ET = ZoneInfo("America/New_York")

# ── Watchlist parser ─────────────────────────────────────────────────────────

def _parse_list(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,\s]+", value) if v.strip()]


def load_watchlist() -> dict:
    """
    Parse watchlist.md sections into a structured config dict.
    Returns:
      stocks, commodities, fx: list[str]
      geo_keywords: dict[str, list[str]]   topic → keyword list
      thresholds: dict
      recipients: list[str]
    """
    if not WATCHLIST_PATH.exists():
        logger.error(f"watchlist.md not found: {WATCHLIST_PATH}")
        sys.exit(1)

    text = WATCHLIST_PATH.read_text(encoding="utf-8")
    # Strip YAML frontmatter
    text = re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL)

    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"

    def get(key: str) -> str:
        return sections.get(key, "").strip()

    # Tickers
    stocks      = _parse_list(get("个股与基金"))
    commodities = _parse_list(get("商品期货"))
    fx          = _parse_list(get("汇率"))

    # Geopolitics keywords: "TopicName: kw1, kw2, kw3"
    geo_keywords: dict[str, list[str]] = {}
    for line in get("地缘政治关键词").splitlines():
        if ":" in line:
            topic, kws = line.split(":", 1)
            geo_keywords[topic.strip()] = [k.strip() for k in kws.split(",") if k.strip()]

    # Thresholds
    thresholds = {"stock_pct": 3.0, "commodity_pct": 2.0, "fx_pct": 1.0, "tnx_bps": 10}
    for line in get("异动阈值").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                thresholds[k.strip()] = float(v.strip())
            except ValueError:
                pass

    # Recipients
    recipients = [
        line.strip()
        for line in get("收件人").splitlines()
        if "@" in line
    ]

    logger.info(f"Watchlist loaded: {len(stocks)} stocks, {len(commodities)} commodities, "
                f"{len(fx)} fx, {len(geo_keywords)} geo topics, {len(recipients)} recipients")
    return dict(
        stocks=stocks, commodities=commodities, fx=fx,
        geo_keywords=geo_keywords, thresholds=thresholds, recipients=recipients,
    )


# ── NYSE trading day check ───────────────────────────────────────────────────

def is_nyse_trading_day() -> bool:
    """Return True if today (ET) is a NYSE session."""
    try:
        import exchange_calendars as xcals
        import pandas as pd
        nyse = xcals.get_calendar("XNYS")
        # is_session requires a timezone-naive date string or Timestamp
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        result = nyse.is_session(today_str)
        logger.info(f"NYSE calendar check: {today_str} → trading={result}")
        return bool(result)
    except Exception as e:
        logger.warning(f"exchange_calendars unavailable ({e}), falling back to weekday check")
        return datetime.now().weekday() < 5  # Mon–Fri


# ── Tavily budget ────────────────────────────────────────────────────────────

def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def load_budget() -> dict:
    today = _today_et()
    if BUDGET_PATH.exists():
        try:
            data = json.loads(BUDGET_PATH.read_text())
            if data.get("date") == today:
                return data
        except Exception:
            pass
    return {"date": today, "used": 0}


def save_budget(budget: dict) -> None:
    BUDGET_PATH.write_text(json.dumps(budget))


def budget_remaining(budget: dict) -> int:
    return max(0, TAVILY_DAILY_LIMIT - budget.get("used", 0))


# ── SerpApi monthly budget ────────────────────────────────────────────────────

def _this_month_et() -> str:
    return datetime.now(ET).strftime("%Y-%m")

def load_serpapi_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(SERPAPI_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_serpapi_budget(budget: dict) -> None:
    SERPAPI_BUDGET_PATH.write_text(json.dumps(budget))


# ── Adanos monthly budget ─────────────────────────────────────────────────────

def load_adanos_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(ADANOS_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_adanos_budget(budget: dict) -> None:
    ADANOS_BUDGET_PATH.write_text(json.dumps(budget))

def adanos_remaining(budget: dict) -> int:
    return max(0, ADANOS_MONTHLY_LIMIT - budget.get("used", 0))

def serpapi_remaining(budget: dict) -> int:
    return max(0, SERPAPI_MONTHLY_LIMIT - budget.get("used", 0))

def serpapi_search(query: str, budget: dict) -> list[dict]:
    if not SERPAPI_API_KEY:
        return []
    if serpapi_remaining(budget) <= 0:
        logger.warning("SerpApi monthly budget exhausted")
        return []
    try:
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": SERPAPI_API_KEY, "num": 5, "engine": "google"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("organic_results", [])[:5]
        budget["used"] += 1
        save_serpapi_budget(budget)
        logger.info(f"SerpApi used [{budget['used']}/{SERPAPI_MONTHLY_LIMIT}]")
        return [{"title": r.get("title", ""), "url": r.get("link", ""), "content": r.get("snippet", "")} for r in results]
    except Exception as e:
        logger.warning(f"SerpApi error: {e}")
        return []


# ── Tavily search ────────────────────────────────────────────────────────────

def tavily_search(query: str, budget: dict, days: int = 1,
                  search_depth: str = "basic", max_results: int = 12,
                  start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """Single Tavily search call; returns list of {title, url, content, score, published_date}.
    advanced costs 2 credits; basic costs 1.
    Use start_date/end_date (YYYY-MM-DD) for precise time filtering instead of days."""
    if not TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY not set, skipping Tavily")
        return []
    credits = 2 if search_depth == "advanced" else 1
    if budget_remaining(budget) < credits:
        logger.warning(f"Tavily budget insufficient for {search_depth} search (need {credits})")
        return []

    payload: dict = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "topic": "news",
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False,
    }
    if start_date and end_date:
        payload["start_published_date"] = start_date
        payload["end_published_date"]   = end_date
    else:
        payload["days"] = days

    try:
        resp = httpx.post("https://api.tavily.com/search", json=payload, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        budget["used"] += credits
        save_budget(budget)
        logger.info(f"Tavily [{budget['used']}/{TAVILY_DAILY_LIMIT}] ({search_depth}, {credits}cr): "
                    f"'{query}' → {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Tavily search failed: {e}")
        return []


def tavily_extract(urls: list[str], query: str, budget: dict,
                   chunks_per_source: int = 2) -> list[dict]:
    """Batch-extract full content chunks from known URLs.
    Cost: 1 credit per 5 URLs (ceiling), so 5=1cr, 10=2cr.
    Always send multiples of 5 for best credit efficiency.
    Returns list of {url, chunks: [{content}], raw_content}."""
    if not TAVILY_API_KEY or not urls:
        return []
    # Round up to nearest 5 for credit efficiency
    batch = urls[:10]  # cap at 10 (= 2cr max)
    n = len(batch)
    extract_cost = math.ceil(n / 5)   # 1-5 → 1cr, 6-10 → 2cr
    if budget_remaining(budget) < extract_cost:
        logger.warning(f"Tavily budget insufficient for extract "
                       f"({n} URLs needs {extract_cost}cr, have {budget_remaining(budget)}cr)")
        return []
    try:
        resp = httpx.post(
            "https://api.tavily.com/extract",
            json={
                "api_key": TAVILY_API_KEY,
                "urls": batch,
                "query": query,
                "chunks_per_source": chunks_per_source,
            },
            timeout=45,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        budget["used"] += extract_cost
        save_budget(budget)
        logger.info(f"Tavily Extract [{budget['used']}/{TAVILY_DAILY_LIMIT}]: "
                    f"{n} URLs → {len(results)} extracted ({extract_cost}cr)")
        return results
    except Exception as e:
        logger.warning(f"Tavily extract failed (non-fatal): {e}")
        return []


# Trusted financial/news domains for scoring bonus
_TRUSTED_DOMAINS = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "apnews.com", "cnbc.com", "marketwatch.com", "politico.com",
    "barrons.com", "seekingalpha.com", "axios.com", "thestreet.com",
]


def score_and_filter(
    results: list[dict],
    anomaly_tickers: list[str],
    geo_topics: list[str],
    top_n: int = 8,
) -> list[dict]:
    """Score and deduplicate search results; return top_n by composite score.

    Composite = tavily_score
              + domain_bonus  (trusted financial/news source: +0.15)
              + recency_bonus (≤24h: +0.10; ≤72h: +0.05)
              + keyword_bonus (anomaly ticker or geo keyword in title/content: +0.05 each)
    """
    keywords = [t.lower() for t in anomaly_tickers]
    for topic in geo_topics:
        keywords += [k.strip().lower() for k in topic.replace("-", " ").split()]
    keywords = list(set(keywords))

    now = datetime.now(ET)
    scored: list[tuple[float, dict]] = []
    seen: set[str] = set()

    for r in results:
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        s = float(r.get("score") or 0)
        domain = url.split("/")[2] if "//" in url else ""
        domain_bonus = 0.15 if any(d in domain for d in _TRUSTED_DOMAINS) else 0

        recency_bonus = 0.0
        pub = r.get("published_date", "")
        if pub:
            try:
                from dateutil import parser as _dp
                age_h = (now - _dp.parse(pub).astimezone(ET)).total_seconds() / 3600
                recency_bonus = 0.10 if age_h <= 24 else (0.05 if age_h <= 72 else 0)
            except Exception:
                pass

        text = (r.get("title", "") + " " + (r.get("content") or "")).lower()
        kw_bonus = 0.05 * sum(1 for k in keywords if k and k in text)

        scored.append((s + domain_bonus + recency_bonus + kw_bonus, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [r for _, r in scored[:top_n]]
    logger.info(f"score_and_filter: {len(results)} → {len(top)} results (top_n={top_n})")
    return top


def _haiku_relevance_filter(
    results: list[dict],
    anomaly_tickers: list[str],
    geo_topics: list[str],
    portfolio_tickers: list[str] | None = None,
    top_n: int = 10,
) -> list[dict]:
    """Semantically rank pre-screened search results using DeepSeek V4 Flash direct.

    Considers upstream/downstream supply chains, sector-wide regulatory impacts,
    and macro drivers — not just direct ticker name mentions.
    Fail-open: returns script-scored top_n on any error.
    Cost: ~$0.000035 per call (DeepSeek direct, ~22x cheaper than Haiku/Bedrock).
    """
    if not results or not OPENROUTER_API_KEY:
        return results[:top_n]

    ptickers = ", ".join(portfolio_tickers[:15]) if portfolio_tickers else "INTC NVDA QCOM TSLA AMKR"
    items_text = []
    for i, r in enumerate(results):
        title   = (r.get("title") or "").strip()
        url     = (r.get("url") or "")[:70]
        score   = r.get("score", 0)
        snippet = (r.get("content") or "")[:130].replace("\n", " ")
        items_text.append(f"[{i}] sc={score:.2f} | {title}\n    {url}\n    {snippet}")

    prompt = f"""You are a financial intelligence analyst. Rank these {len(results)} articles by relevance to today's market monitoring situation.

Context:
- Anomaly tickers (moved significantly today): {', '.join(anomaly_tickers) if anomaly_tickers else 'none'}
- Active geopolitical topics: {', '.join(geo_topics) if geo_topics else 'none'}
- Portfolio tickers: {ptickers}

Relevance criteria (in priority order):
1. Direct catalyst for anomaly tickers (earnings beat/miss, product launch, deal, regulatory action, analyst upgrade/downgrade)
2. Supply chain impact: upstream component suppliers, downstream OEM customers, foundry partners, competitor reactions
3. Sector-wide shifts: export controls, tariff changes, industry capacity rebalancing that explain the move
4. Geopolitical transmission: sanctions, conflict escalation, trade negotiations with quantifiable market impact
5. Macro signals directly linked to portfolio exposure (Fed policy, bond yields, FX moves, commodity supply shocks)

NOT relevant: general market sentiment, unrelated sectors, repeated/duplicate coverage, opinion without new facts.

Articles:
{chr(10).join(items_text)}

Return ONLY a JSON array of exactly {top_n} indices (or fewer if less than {top_n} are relevant), best first:
[i1, i2, ...]"""

    try:
        resp = httpx.post(
            OR_BASE_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": SEMANTIC_FILTER_MODEL,
                "provider": DS_OR_PROVIDERS,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0,
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\[[\d,\s]+\]', content)
        if m:
            indices = json.loads(m.group())
            filtered = [results[i] for i in indices if isinstance(i, int) and 0 <= i < len(results)]
            if filtered:
                logger.info(f"Semantic filter: {len(results)} → {len(filtered)} results "
                            f"(supply-chain + semantic ranking, OR/DigitalOcean)")
                return filtered
        logger.warning(f"Semantic filter: unexpected output: {content[:80]}")
    except Exception as e:
        logger.warning(f"Semantic filter failed ({e}), trying OR flex fallback...")

    # OR flex fallback for semantic filter
    try:
        resp = httpx.post(
            OR_BASE_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": LLM_FALLBACK_FLASH,
                "service_tier": "flex",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\[[\d,\s]+\]', content)
        if m:
            indices = json.loads(m.group())
            filtered = [results[i] for i in indices if isinstance(i, int) and 0 <= i < len(results)]
            if filtered:
                logger.info(f"Semantic filter OR flex: {len(results)} → {len(filtered)} results")
                return filtered
    except Exception as e:
        logger.warning(f"Semantic filter OR flex also failed: {e}")

    return results[:top_n]


def format_tavily_results(results: list[dict]) -> str:
    """Format search results (250-char snippets). Used as fallback when extract unavailable."""
    if not results:
        return ""
    lines = ["[Tavily搜索摘要]"]
    for r in results:
        lines.append(f"  • {r.get('title', '')} — {r.get('url', '')}")
        content = (r.get("content") or "")[:300]
        if content:
            lines.append(f"    {content}")
    return "\n".join(lines)


_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
_STRONG_TOKEN_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?%|\$\d[\d,\.]*|\b\d{4}\b")
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _extract_key_phrases(text: str) -> set[str]:
    """Cheap rule-based fact fingerprint: proper-noun phrases + weekday/date/number
    tokens. Used only for rule-based cross-source corroboration (issue #19) —
    no extra API/LLM cost. Expect false negatives on paraphrased claims; a miss
    here means 'no rule-based match found', not proof a claim is uncorroborated."""
    if not text:
        return set()
    phrases = set(_PHRASE_RE.findall(text))
    tokens = set(m.lower() for m in _STRONG_TOKEN_RE.findall(text))
    words = set(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    tokens |= (words & _WEEKDAYS)
    return phrases | tokens


def _detect_low_structure(text: str) -> bool:
    """Heuristic for video-hub / caption-listing pages (issue #19): they pack many
    bare timestamp prefixes ('01:32 ...') with few real sentences, unlike article
    prose. Such pages often surface an isolated caption with no reliable date of
    its own, which an LLM can otherwise mistake for a fresh, dated claim."""
    if not text or len(text) < 40:
        return False
    ts_count = len(_TIMESTAMP_RE.findall(text))
    sentence_count = text.count(". ") + text.count("。")
    return ts_count >= 3 and ts_count > sentence_count


def _lookup_published_date(url: str, candidates: list[dict]) -> tuple[str, float | None]:
    """Resolve a published date for `url` from the pre-extract search result pool —
    Tavily's /extract response carries no date field, only /search results do."""
    for r in candidates:
        if r.get("url") == url:
            pub = r.get("published_date", "")
            if not pub:
                return "", None
            try:
                from dateutil import parser as _dp
                age_h = (datetime.now(ET) - _dp.parse(pub).astimezone(ET)).total_seconds() / 3600
                return pub, age_h
            except Exception:
                return pub, None
    return "", None


def _compute_corroboration(url: str, text: str, candidates: list[dict]) -> int:
    """Rule-based cross-source check (issue #19): count distinct OTHER domains in the
    broader candidate pool that share at least one key phrase with this result.
    Zero cost (no extra API/LLM call), but also a rough heuristic — 0 means 'no
    overlap found by this rule', not a confirmed single-source claim."""
    own_domain = url.split("/")[2] if "//" in url else url
    own_phrases = _extract_key_phrases(text)
    if not own_phrases:
        return 0
    matched_domains: set[str] = set()
    for r in candidates:
        curl = r.get("url", "")
        cdomain = curl.split("/")[2] if "//" in curl else curl
        if not cdomain or cdomain == own_domain:
            continue
        ctext = (r.get("title", "") + " " + (r.get("content") or ""))
        if own_phrases & _extract_key_phrases(ctext):
            matched_domains.add(cdomain)
    return len(matched_domains)


def _source_confidence_tags(url: str, full_text: str, candidates: list[dict]) -> str:
    """Build the '[信源类型/发布时间/交叉印证]' tag line for one Extract source.
    Shared by format_extract_results() (LLM-facing) and write_extract_archive()
    (audit trail) so both reflect the same confidence signals. See issue #19."""
    pub_date, age_h = _lookup_published_date(url, candidates)
    tags = []
    if _detect_low_structure(full_text):
        tags.append("信源类型: 视频/聚合页疑似caption堆叠-无独立时间戳，谨慎对待")
    if not pub_date:
        tags.append("发布时间: 未知")
    elif age_h is not None and age_h > 72:
        tags.append(f"发布时间: {pub_date}（约{age_h / 24:.0f}天前，警惕过时/已被后续事件覆盖）")
    else:
        tags.append(f"发布时间: {pub_date}")
    corroboration = _compute_corroboration(url, full_text, candidates)
    tags.append(
        f"交叉印证: {corroboration}个独立域名有重叠信息佐证" if corroboration > 0
        else "交叉印证: 未发现其他信源佐证（单一信源）"
    )
    return " | ".join(tags)


def format_extract_results(results: list[dict], candidates: list[dict] | None = None) -> str:
    """Format Extract results (full chunks, up to 600 chars each).

    Each source gets a confidence tag line (structure type / date confidence /
    rule-based corroboration count) so Pass 2 can hedge language on claims that
    are single-source and/or from undated caption-listing pages, instead of
    stating them as settled fact (see issue #19 — a Reuters video-hub caption
    with no date of its own was reported as a confirmed "signing set for Friday").
    `candidates` is the broader pre-extract search result pool (has published_date
    and title/content for corroboration matching); pass score_and_filter's output.
    """
    if not results:
        return ""
    candidates = candidates or []
    lines = ["[Tavily Extract — 全文片段]"]
    for r in results:
        url = r.get("url", "")
        chunks = r.get("chunks") or []
        raw = r.get("raw_content", "")
        full_text = " ".join((c.get("content") or "") for c in chunks) or raw

        lines.append(f"\n  来源: {url}")
        lines.append(f"    [{_source_confidence_tags(url, full_text, candidates)}]")
        if chunks:
            for i, chunk in enumerate(chunks[:2]):
                text = (chunk.get("content") or "")[:600]
                if text:
                    lines.append(f"    [{i+1}] {text}")
        elif raw:
            lines.append(f"    {raw[:800]}")
    return "\n".join(lines)


# ── Context log + Extract archive writers ────────────────────────────────────

def _context_log_path(date_str: str) -> Path:
    ym = date_str[:7].replace("-", "")
    return REPORTS_DIR / f"Daily_Intel_context_{ym}.md"


def write_context_log(
    date_str: str,
    slot_label: str,
    now_et: "datetime",
    price_table: str,
    news_items: list,
    triggered_geo_topics: list,
    sonar_macro_section: str,
    all_search_jobs: list,
) -> None:
    """Append structured context snapshot to monthly context log in Obsidian (gets mined).
    Contains: price table, geo-triggered news headlines, Sonar macro, search queries.
    Does NOT contain Tavily Extract full text (see write_extract_archive). Fail-open.
    """
    try:
        ym_display = date_str[:7]
        path = _context_log_path(date_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []

        # Monthly file header (only on first write)
        if not path.exists():
            lines.append(f"---\ndate: {ym_display}\nsource: Daily Intelligence context log\n---\n")
            lines.append(f"# Daily Intelligence Context Log {ym_display}\n")

        lines.append(f"\n## [Context] {date_str} {slot_label}")
        lines.append(f"_运行时间: {now_et.strftime('%Y-%m-%d %H:%M %Z')}_\n")

        # Price snapshot
        lines.append("### 价格快照")
        lines.append(price_table.strip())
        lines.append("")

        # Triggered news (geo-matched items only, not all 300)
        triggered_items = [item for item in news_items if item.topics]
        if triggered_items:
            lines.append("### 触发新闻（命中地缘/异动相关）")
            for item in triggered_items[:30]:
                ts = item.published.strftime("%m-%d %H:%M UTC")
                topics_str = ", ".join(item.topics)
                lines.append(f"- [{ts}] [{topics_str}] {item.title} ({item.source})")
            lines.append("")

        # Sonar macro snapshot (strip section header if present)
        if sonar_macro_section and sonar_macro_section.strip():
            lines.append("### 宏观快照（Sonar）")
            sonar_clean = re.sub(r"^#+\s+[^\n]+\n", "", sonar_macro_section, count=1).strip()
            lines.append(sonar_clean[:2000])
            lines.append("")

        # Search queries used
        if all_search_jobs:
            lines.append("### 搜索任务")
            for job in all_search_jobs:
                lines.append(f"- `{job.get('query', '')}` (days={job.get('days', 1)})")
            lines.append("")

        lines.append("---\n")

        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"Context log written → {path.name}")
    except Exception as e:
        logger.warning(f"Context log write failed (non-fatal): {e}")


def write_extract_archive(
    date_str: str,
    slot: str,
    now_et: "datetime",
    all_search_jobs: list,
    filtered: list,
    extract_results: list,
) -> None:
    """Write cleaned Tavily Extract full text to local archive outside Obsidian.
    Never mined by MemPalace. Preserves original intelligence for audit/mid-term review.
    Cleaning: lines < 60 chars stripped (nav/ads/links). Fail-open.
    """
    if not extract_results and not filtered:
        return
    try:
        ym = date_str[:7].replace("-", "")
        out_dir = ARCHIVE_DIR / ym
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{date_str}-{slot}-extract.md"

        lines: list[str] = []
        lines.append(f"# Extract Archive: {date_str} {slot.upper()}")
        lines.append(f"_生成时间: {now_et.strftime('%Y-%m-%d %H:%M %Z')}_\n")

        # Search queries
        if all_search_jobs:
            lines.append("## 搜索任务")
            for job in all_search_jobs:
                lines.append(f"- {job.get('query', '')}")
            lines.append("")

        # Layer 2b filtered candidates (with score + URL)
        if filtered:
            lines.append("## 搜索结果候选（Layer 2b 过滤后，进入 Extract 的10条）")
            for i, r in enumerate(filtered, 1):
                title = (r.get("title") or r.get("url") or "")[:100]
                url = r.get("url", "")
                score = float(r.get("score") or 0)
                lines.append(f"{i}. [score:{score:.2f}] {title}")
                lines.append(f"   {url}")
            lines.append("")

        # Extract full text (cleaned)
        if extract_results:
            lines.append("## Extract 全文")
            for r in extract_results:
                url = r.get("url", "")
                chunks = r.get("chunks") or []
                raw = r.get("raw_content", "")
                full_text = " ".join((c.get("content") or "") for c in chunks) or raw
                lines.append(f"\n### {url}")
                lines.append(f"[{_source_confidence_tags(url, full_text, filtered)}]\n")
                if chunks:
                    for chunk in chunks:
                        text = chunk.get("content") or ""
                        # Strip lines < 60 chars (navigation, ads, single-word fragments)
                        clean = "\n".join(
                            ln for ln in text.splitlines() if len(ln.strip()) >= 60
                        ).strip()
                        if clean:
                            lines.append(clean)
                            lines.append("")
                elif raw:
                    clean = "\n".join(
                        ln for ln in raw.splitlines() if len(ln.strip()) >= 60
                    ).strip()
                    if clean:
                        lines.append(clean[:8000])  # cap raw fallback
                        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Extract archive written → archives/{ym}/{path.name}")
    except Exception as e:
        logger.warning(f"Extract archive write failed (non-fatal): {e}")


# ── Personal context helpers (for Pass 2 injection) ──────────────────────────

_framework_cache: str = ""


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
    m = re.search(r"## 总体构架\n(.*?)(?=\n##)", text, re.DOTALL)
    if m:
        parts.append("【目标构架】\n" + m.group(1).strip()[:500])
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
        ib_section = re.search(r"## IB（账户 0611）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return "\n".join(header)
        holdings = re.findall(
            r"### (\w+) \w+ \d+\n.*?均价：([\d.]+).*?浮盈：[^（]+（([+\-\d.]+%)）",
            ib_section.group(1), re.DOTALL
        )
        us_stocks = [(sym, cost, pnl) for sym, cost, pnl in holdings
                     if not sym.startswith("CASH") and sym.isalpha()]
        lines = header[:]
        lines.append("IB美股持仓（成本价为均价，浮盈%为报告日数据供参考）：")
        for sym, cost, pnl in us_stocks:
            lines.append(f"  {sym}  成本@{cost}  报告浮盈{pnl}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Portfolio snapshot failed: {e}")
        return ""


def _load_personal_context() -> str:
    """Combine portfolio snapshot + investment framework for Pass 2 injection (Layer B)."""
    parts = [
        "【重要】实际持仓以下方「IB美股持仓」快照为唯一依据。"
        "价格表中未出现在持仓快照里的标的（包括 watchlist 监控标的）均为观察标的，没有持仓，"
        "不应出现「守住仓位」「你已建仓」「持仓含义」等表述。"
        "框架文本中提到的「计划建仓」「为建仓做准备」等表述描述的是未来计划，不是当前持仓。"
    ]
    portfolio = _get_portfolio_snapshot()
    if portfolio:
        parts.append(portfolio)
    fw = _load_framework()
    if fw:
        parts.append(fw)
    return "\n\n".join(parts)


# ── LLM call ─────────────────────────────────────────────────────────────────

# Pass 1 system prompt: lightweight, instructs structured JSON output + search task generation
SYSTEM_PROMPT = """你是一名服务于个人投资者的金融情报分析师。
用户持有多只美股及ETF，同时关注黄金、原油、债券收益率、汇率和地缘政治风险。
你的任务是每日整理关键情报并生成搜索指令，不做深入推理，仅呈现事实与异动，供用户自行判断。
输出语言：中文。风格：简洁、直接、数据优先。
所有时间推理均以纽约证券交易所所在时区（America/New_York，夏令时 EDT/冬令时 EST）为基准。"""

# Pass 2 system prompt (Layer A): platform-generic analyst persona, Portfonia-reusable
_LAYER_A_PATH = OBSIDIAN / "Hermes/Daily Intelligence/Layer_A_Prompt.md"
_LAYER_A_FALLBACK = """你是一名专业财经情报分析师，服务于秉持价值投资、长期持仓、低交易频率理念的投资者。
分析原则：区分结构性催化剂与情绪性波动；结论必须可操作；明确区分事实与推测；不重复新闻原文。
输出语言：中文。所有时间推理以 America/New_York 为基准。"""


def _load_layer_a() -> str:
    """Read Layer A system prompt from Obsidian file (strips YAML frontmatter). Falls back to inline default."""
    try:
        if _LAYER_A_PATH.exists():
            raw = _LAYER_A_PATH.read_text(encoding="utf-8")
            # Strip YAML frontmatter (--- ... ---)
            text = re.sub(r"^---.*?---\s*", "", raw, flags=re.DOTALL).strip()
            if text:
                return text
    except Exception as e:
        logger.warning(f"Layer A prompt load failed: {e}")
    return _LAYER_A_FALLBACK


SYSTEM_PROMPT_P2 = _load_layer_a()

USER_PROMPT_TEMPLATE = """今日日期（ET）：{date}
当前时间：{now_str}
上次报告：{last_report_date}
默认搜索窗口：{query_days} 天（自上次报告起）
今日 RSS 命中地缘政治主题：{triggered_geo_topics}
{pm_afterhours_note}
## 价格数据（{price_data_label}）
{price_table}
{price_missing_note}
## 过去24小时新闻（RSS）
{news_text}

{finnhub_news_section}{sonar_macro_section}{social_sentiment_section}{tavily_section}{kb_section}
---

## 搜索任务约束（填写 tavily_queries 时遵守）
- 只为"今日 RSS 命中地缘政治主题"中列出的主题生成查询，未命中的主题不生成
- 所有查询统一 search_depth="basic"（系统自动在 Extract 层补充全文深度，无需 advanced）
- 单次 tavily_queries 总条数不超过 4 条
- days 默认使用上方搜索窗口值，宏观趋势背景可用 days=3
- max_results 统一填 12
- 对单个持仓 ticker 的个股查询，在 query 中加 site:stockanalysis.com 或 site:macrotrends.net 可显著提升数据密度（例："NVDA site:stockanalysis.com"）

请输出以下JSON（不要附加任何其他文字）：
{{
  "report_md": "# [Daily_Intel] YYYY-MM-DD 开盘前简报\\n\\n...",
  "tavily_queries": []
}}

规则：
1. report_md 分四节：【价格异动】【地缘政治】【市场要闻】【简评】
   - 【价格异动】：仅列出[!]标记标的，说明幅度和可能原因（基于新闻）；
     若为夜盘报告且价格表含"盘后涨跌"数据，则在每个[!]标的下分别列出
     「日内涨跌」和「盘后截止{now_str}涨跌」，并结合 Finnhub 即时新闻说明盘后驱动因素
   - 【地缘政治】：按主题分段，无动态则注明"无新进展"
   - 【市场要闻】：其他重要财经新闻，最多5条
   - 【简评】：不超过3句，点出今日最需关注的1-2个信号
2. tavily_queries：为需要更多背景的事件生成搜索对象数组，每项格式：
   {{"query": "英文搜索词", "search_depth": "basic", "days": N, "max_results": 12}}
3. 严格JSON格式，report_md内换行用\\n
"""

# Pass 2 prompt template: free-form analysis with personal context (Layer B injected at call site)
USER_PROMPT_TEMPLATE_P2 = """今日日期（ET）：{date}
当前时间：{now_str}
上次报告：{last_report_date}
{pm_afterhours_note}
## 价格数据（{price_data_label}）
{price_table}
{price_missing_note}
## 过去24小时新闻（RSS）
{news_text}

{finnhub_news_section}{sonar_macro_section}{social_sentiment_section}{tavily_section}{kb_section}
---

== 持仓与框架 ==
{personal_context}

== 分析要求 ==
必须覆盖：
① 今日价格异动（[!]标记标的）的驱动力，以及对该持仓逻辑的具体含义
② 地缘政治动态对持仓的潜在传导路径（有则写，无则省略）
③ 宏观信号（利率、商品、汇率）与仓位暴露的关系
④ 信源置信度处理：[Tavily Extract] 每条来源前标注了 [信源类型 | 发布时间 | 交叉印证] 标签。
   写入具体断言（尤其含日期、协议签署、人事变动等强论断）前先看该来源标签：
   - 标"单一信源"或"视频/聚合页疑似caption堆叠"或"发布时间：未知"的，正文必须用
     "未证实"/"单一信源，待核实"等措辞明确降级，不得以确定语气写成既成事实
   - 标"约N天前"且 N 较大的，需提示"可能已被后续事件覆盖"
   - 有"N个独立域名佐证"（N≥1）的可正常按事实陈述
   快变的宏观/市场行情下，传统媒体报道常滞后于现状，未标注可靠时间戳的信息尤其容易过时，
   宁可标注不确定，也不要把孤证当结论

可选覆盖：其他值得关注的市场要闻（无实质内容可省略）

格式要求：价格数据已在上方表格，正文自由展开，有话则长，无话则短，省略废话

请输出以下JSON（不要附加任何其他文字）：
{{
  "report_md": "# [Daily_Intel] {date} 开盘前简报\\n\\n..."
}}
"""


def _sonar_macro_brief(
    slot: str,
    stocks: list[str],
    commodities: list[str],
    fx: list[str],
    geo_topics: list[str],
    now_et: "datetime",
    portfolio_snapshot: str = "",
) -> str:
    """Call Perplexity Sonar for a real-time macro intelligence snapshot.

    Query is built dynamically from the current watchlist so it evolves as holdings change.
    Returns a formatted section string for LLM prompt injection. Fail-open (returns "" on error).
    Cost: ~$0.005 (Sonar fixed search fee, billed via OpenRouter).
    """
    if not OPENROUTER_API_KEY:
        return ""

    # Ticker → sector context (grows with watchlist over time)
    _TICKER_CTX: dict[str, str] = {
        "INTC": "Intel (CPU/foundry, Apple deal)",
        "NVDA": "NVIDIA (GPU/AI accelerators)",
        "QCOM": "Qualcomm (mobile AI chips, China export risk)",
        "TSLA": "Tesla (EV, Musk policy exposure)",
        "AMKR": "Amkor (advanced chip packaging)",
        "ORCL": "Oracle (cloud/AI infrastructure)",
        "QQQM": "Nasdaq-100 ETF",
        "VOO":  "S&P 500 ETF",
        "EWJ":  "Japan ETF",
        "SGOL": "Gold ETF",
        "GC=F": "Gold futures",
        "CL=F": "WTI crude oil",
        "^TNX": "10-year Treasury yield",
        "USDCNY=X": "USD/CNY",
        "USDJPY=X": "USD/JPY",
        "DX-Y.NYB": "USD index",
    }
    holdings = [_TICKER_CTX.get(t, t) for t in (stocks + commodities + fx)[:14]]
    geo_str  = "; ".join(geo_topics) if geo_topics else "global macro"
    now_str  = now_et.strftime("%Y-%m-%d %H:%M %Z")

    if slot == "am":
        focus = (
            f"Pre-market intelligence as of {now_str}. "
            f"What are the most significant macro/geopolitical developments from the past 12 hours "
            f"that could affect today's US market open? "
        )
    else:
        focus = (
            f"End-of-day & after-hours intelligence as of {now_str}. "
            f"What macro/geopolitical developments drove today's US session and "
            f"what is building after-hours / overnight that matters for tomorrow? "
        )

    query = (
        f"{focus}"
        f"Portfolio exposure: {', '.join(holdings)}. "
        f"Active monitored themes: {geo_str}. "
        f"Deliver: (1) single most market-moving development with specific numbers, "
        f"(2) direct sector impact on semiconductors/EV/energy/bonds/FX, "
        f"(3) one forward-looking signal to watch in the next 12-24 hours. "
        f"Be specific. No generic commentary."
    )

    system = (
        f"You are a macro intelligence analyst serving a personal investor. "
        f"Today is {now_str}. Reason in NYSE Eastern Time. "
        f"Focus on actionable, time-stamped developments. "
        f"If portfolio snapshot is provided, tailor analysis to those holdings.\n"
        + (f"Portfolio: {portfolio_snapshot}\n" if portfolio_snapshot else "")
    )

    or_payload = {
        "model": SONAR_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": query},
        ],
        "max_tokens": 800,
        "temperature": 0.1,
        "provider": {"order": ["Perplexity"], "allow_fallbacks": False},
    }
    slot_label = "开盘前" if slot == "am" else "夜盘"
    for attempt in range(2):
        try:
            resp = httpx.post(
                OR_BASE_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json=or_payload,
                timeout=30,
            )
            resp.raise_for_status()
            brief = resp.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"Sonar macro brief ({slot_label}): {len(brief)} chars")
            return f"\n## Sonar 宏观快照（{slot_label}，实时）\n{brief}\n"
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Sonar macro brief attempt 1 failed ({e}), retrying in 5s...")
                time.sleep(5)
            else:
                logger.warning(f"Sonar macro brief failed after retry (non-fatal): {e}")
    return ""


def _polymarket_brief(geo_topics: list[str], max_topics: int = 4) -> str:
    """Query Polymarket's public Gamma API (/public-search) for prediction-market odds on
    watchlist-relevant geo/macro themes. Free, no auth, no quota. Fail-open (returns "" on error).

    Prediction-market prices are a probability signal that RSS/Sonar can't provide directly
    (e.g. odds of a Fed rate decision or a geopolitical outcome), see issue C4.
    """
    if not geo_topics:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for topic in geo_topics[:max_topics]:
        try:
            resp = httpx.get(
                f"{POLYMARKET_BASE}/public-search",
                params={"q": topic, "limit_per_type": 3},
                headers={"User-Agent": "daily-intelligence/1.0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"Polymarket raw response for '{topic}': {json.dumps(data)[:2000]}")
            # /public-search returns top-level "events"; the tradeable markets (with
            # outcomePrices/outcomes) are nested under event["markets"], not on the event
            # itself. There's no top-level "markets" key in practice; keep the fallback
            # in case the API ever returns a flat market match directly.
            events = data.get("events") or data.get("markets") or []
            found_for_topic = 0
            for ev in events[:3]:
                if found_for_topic >= 2 or ev.get("closed"):
                    continue
                event_title = (ev.get("title") or "").strip()
                sub_markets = ev.get("markets") or [ev]
                for m in sub_markets[:3]:
                    if m.get("closed"):
                        continue
                    question = (m.get("question") or m.get("title") or event_title or "").strip()
                    if not question or question in seen:
                        continue
                    raw_prices = m.get("outcomePrices")
                    raw_outcomes = m.get("outcomes")
                    try:
                        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                    except (TypeError, json.JSONDecodeError):
                        prices, outcomes = None, None
                    if prices and outcomes:
                        pairs = ", ".join(f"{o}:{float(p):.0%}" for o, p in zip(outcomes, prices))
                        lines.append(f"- [{topic}] {question} → {pairs}")
                        seen.add(question)
                        found_for_topic += 1
                        break
        except Exception as e:
            logger.warning(f"Polymarket query failed for '{topic}': {e}")
            continue
    if not lines:
        return ""
    return "\n## Polymarket 预测市场快照\n" + "\n".join(lines[:6]) + "\n"


def _adanos_x_sentiment(tickers: list[str], budget: dict) -> str:
    """Query Adanos (api.adanos.org) for X/Twitter stock sentiment (buzz/bullish ratio/mentions/trend)
    on watchlist tickers. Free tier capped at ADANOS_MONTHLY_LIMIT calls/month — mutates
    budget['used'] in place per call so the caller can persist it. Fail-open: returns "" if no
    key configured, budget exhausted, or the API errors (response schema is best-effort parsed
    since Adanos is an undocumented third-party aggregator, see issue C4).
    """
    if not tickers or not ADANOS_API_KEY:
        return ""
    lines: list[str] = []
    for ticker in tickers:
        if adanos_remaining(budget) <= 0:
            logger.info("Adanos monthly budget exhausted, skipping remaining tickers")
            break
        try:
            resp = httpx.get(
                f"{ADANOS_BASE}/x/stocks/v1/stock/{ticker}",
                headers={"X-API-Key": ADANOS_API_KEY},
                timeout=10,
            )
            budget["used"] = budget.get("used", 0) + 1
            logger.debug(f"Adanos raw response for {ticker} (status {resp.status_code}): {resp.text[:2000]}")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            d = resp.json()
            data = d.get("data", d)
            buzz     = data.get("buzz_score", data.get("buzz"))
            bullish  = data.get("bullish_ratio", data.get("sentiment_score", data.get("sentiment")))
            mentions = data.get("mentions", data.get("mention_count"))
            trend    = data.get("trend", data.get("trend_direction"))
            parts = [f"{k}={v}" for k, v in
                     [("buzz", buzz), ("bullish", bullish), ("mentions", mentions), ("trend", trend)]
                     if v is not None]
            if parts:
                lines.append(f"- {ticker}: {', '.join(parts)}")
        except Exception as e:
            logger.warning(f"Adanos X sentiment query failed for {ticker}: {e}")
            continue
    if not lines:
        return ""
    return "\n## X/Twitter 舆情快照（Adanos）\n" + "\n".join(lines) + "\n"


def fetch_finnhub_news(tickers: list[str], hours: int = 24) -> str:
    """Fetch recent company news from Finnhub for watchlist tickers. Fail-open.
    Returns a formatted section string ready for prompt injection."""
    if not tickers or not FINNHUB_API_KEY:
        return ""
    from datetime import timezone
    today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_s  = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
    items: list[tuple[int, str]] = []
    seen: set[str] = set()
    for ticker in tickers[:8]:
        try:
            r = None
            for _attempt in range(2):
                try:
                    r = httpx.get(
                        f"{FINNHUB_BASE}/company-news",
                        params={"symbol": ticker, "from": from_s, "to": today_s, "token": FINNHUB_API_KEY},
                        timeout=8,
                    )
                    break
                except (httpx.ConnectTimeout, httpx.ReadTimeout) as _e:
                    if _attempt == 0:
                        time.sleep(3)
                    else:
                        raise
            for n in (r.json() or [])[:15]:
                headline = (n.get("headline") or "").strip()
                if not headline or headline in seen:
                    continue
                seen.add(headline)
                ts = n.get("datetime", 0)
                pub_et = datetime.fromtimestamp(ts, tz=ET).strftime("%m-%d %H:%M %Z") if ts else "?"
                source  = n.get("source", "Finnhub")
                summary = (n.get("summary") or "")[:100]
                line = f"[{pub_et}][{ticker}] {headline} ({source})"
                if summary:
                    line += f" — {summary}"
                items.append((ts, line))
            time.sleep(0.05)  # stay within 60 req/min
        except Exception as e:
            logger.warning(f"Finnhub news {ticker}: {e}")
    if not items:
        return ""
    items.sort(key=lambda x: x[0], reverse=True)
    lines = [f"## Finnhub 即时新闻（ticker定向，过去{hours}h，来源 Finnhub）"]
    lines += [item[1] for item in items[:15]]
    logger.info(f"Finnhub news: {len(items)} items for {len(tickers)} tickers")
    return "\n".join(lines) + "\n\n"


def call_llm(prompt: str, max_retries: int = 2, model: str = LLM_MODEL,
             system_prompt: str = SYSTEM_PROMPT) -> dict:
    """Call DeepSeek via OpenRouter, return parsed JSON dict. Retries on network errors.
    Pass 2 (deepseek-v4-pro) uses thinking:enabled — required by Together/Fireworks to emit content.
    Pass 1 (deepseek-v4-flash) sends NO thinking param — thinking:disabled breaks StreamLake fallback."""
    is_pass2 = (model == LLM_MODEL_PASS2)
    thinking_cfg = {"type": "enabled", "budget_tokens": 3000} if is_pass2 else None
    max_tokens = 8000 if is_pass2 else 4000
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"LLM retry {attempt}/{max_retries} after {wait}s...")
                time.sleep(wait)
            resp = httpx.post(
                OR_BASE_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "provider": DS_OR_PROVIDERS,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    **({"thinking": thinking_cfg} if thinking_cfg else {}),
                },
                timeout=180,
            )
            resp.raise_for_status()
            content = ""
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                # HTTP body itself is not valid JSON (truncated/error page) — retryable
                last_error = e
                logger.warning(f"LLM attempt {attempt+1}: malformed HTTP JSON body: {e}")
                continue
            usage = data.get("usage", {})
            logger.info(f"LLM tokens: prompt={usage.get('prompt_tokens')} "
                        f"completion={usage.get('completion_tokens')} "
                        f"provider={data.get('provider', 'n/a')}")

            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            json_str = json_match.group(1) if json_match else content.strip()
            json_str = re.sub(r'^[^{]*', '', json_str)
            json_str = re.sub(r'[^}]*$', '', json_str)
            result = json.loads(json_str)
            result["_llm_meta"] = {
                "model": model,
                "provider": data.get("provider", "n/a"),
                "attempts": attempt + 1,
                "fallback": False,
            }
            return result
        except json.JSONDecodeError as e:
            # Model output wasn't parseable JSON (escaping error or truncation) — retryable
            last_error = e
            logger.warning(f"LLM attempt {attempt+1}: returned invalid JSON: {e}\nContent: {content[:500]}")
            continue
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_error = e
                logger.warning(f"LLM attempt {attempt+1}: HTTP {e.response.status_code}")
            else:
                logger.error(f"LLM HTTP {e.response.status_code}: {e}")
                return {}
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = e
            logger.warning(f"LLM attempt {attempt+1}: {e}")
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {}

    logger.error(f"LLM exhausted {max_retries+1} attempts, last error: {last_error}")

    # OR flex fallback (gemini via OpenRouter, service_tier=flex)
    fallback_model = LLM_FALLBACK_PRO if is_pass2 else LLM_FALLBACK_FLASH
    logger.warning(f"OR primary unavailable, trying OR flex fallback: {fallback_model}")
    try:
        resp = httpx.post(
            OR_BASE_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": fallback_model,
                "service_tier": "flex",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        logger.info(f"OR flex tokens: prompt={usage.get('prompt_tokens')} "
                    f"completion={usage.get('completion_tokens')} "
                    f"provider={data.get('provider', 'n/a')}")
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        json_str = json_match.group(1) if json_match else content.strip()
        json_str = re.sub(r'^[^{]*', '', json_str)
        json_str = re.sub(r'[^}]*$', '', json_str)
        result = json.loads(json_str)
        logger.info(f"OR flex fallback succeeded: {fallback_model}")
        result["_llm_meta"] = {
            "model": fallback_model,
            "provider": data.get("provider", "n/a"),
            "fallback": True,
            "primary_attempts": max_retries + 1,
        }
        return result
    except json.JSONDecodeError as e:
        logger.error(f"OR flex fallback returned invalid JSON: {e}")
    except Exception as e:
        logger.error(f"OR flex fallback failed: {e}")
    return {}


# ── Monthly file helpers ──────────────────────────────────────────────────────

def _monthly_path(date_str: str) -> Path:
    """Return path for the monthly report file, e.g. Daily_Intel_report_202604.md"""
    ym = date_str[:7].replace("-", "")  # "2026-04" → "202604"
    return REPORTS_DIR / f"Daily_Intel_report_{ym}.md"


def _monthly_dedup(date_str: str, slot_label: str) -> bool:
    """Return True if this slot's report already exists in the monthly file."""
    p = _monthly_path(date_str)
    if not p.exists():
        return False
    return f"## {date_str} {slot_label}" in p.read_text(encoding="utf-8")


def get_last_report_date() -> str:
    """Find the most recent report date across all monthly files."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    monthly_files = sorted(REPORTS_DIR.glob("Daily_Intel_report_*.md"), reverse=True)
    for mf in monthly_files:
        text = mf.read_text(encoding="utf-8")
        dates = re.findall(r"^## (\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
        if dates:
            return max(dates)
    return "N/A（首次运行）"


# ── MemPalace drawer writer ───────────────────────────────────────────────────

def _mempalace_add_daily_drawer(date_str: str, slot: str, report_md: str) -> None:
    """Push today's report as a per-day MemPalace drawer. Fail-open."""
    try:
        ym = date_str[:7].replace("-", "")
        source_file = f"Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_{ym}.md"
        slot_label = "am" if slot == "am" else "pm"
        content = f"日期：{date_str} {slot_label}\n\n{report_md}"
        resp = httpx.post(
            "http://localhost:8765/mempalace/add_drawer",
            json={
                "wing": "paperview",
                "room": "finance",
                "content": content,
                "source_file": source_file,
                "added_by": "daily-intel",
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        reason = result.get("reason", "new")
        logger.info(f"MemPalace drawer {result.get('drawer_id','?')} [{reason}]")
    except Exception as e:
        logger.warning(f"MemPalace add_drawer skipped: {e}")


# ── Obsidian writer ───────────────────────────────────────────────────────────

def write_report(today_et: str, slot_label: str, markdown: str, budget: dict) -> None:
    """Append report to monthly file Daily_Intel_report_YYYYMM.md."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _monthly_path(today_et)
    ym_display = today_et[:7]  # "2026-04"

    section = (
        f"\n## {today_et} {slot_label}\n"
        f"_Tavily: {budget.get('used', 0)}/{TAVILY_DAILY_LIMIT}_\n\n"
        f"{markdown}\n\n---\n"
    )

    if not path.exists():
        header = f"---\ndate: {ym_display}\nsource: Daily Intelligence\n---\n\n# Daily Intelligence {ym_display}\n"
        path.write_text(header + section, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(section)
    logger.info(f"Report appended: {path} [{today_et} {slot_label}]")


# ── Telegram sender ──────────────────────────────────────────────────────────

_TG_LIMIT = 4096


def _md_to_tg_html(md: str) -> str:
    """Convert report markdown to Telegram-compatible HTML (subset: b, code)."""
    import html as _html
    lines = []
    for line in md.split("\n"):
        if line.startswith("# "):
            line = f"<b>{_html.escape(line[2:])}</b>"
        elif line.startswith("## "):
            line = f"<b>{_html.escape(line[3:])}</b>"
        elif line.startswith("### "):
            line = f"<b>{_html.escape(line[4:])}</b>"
        elif line.startswith("---"):
            line = "─────────────"
        else:
            line = _html.escape(line)
            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        lines.append(line)
    return "\n".join(lines)


def _tg_chunks(text: str, limit: int = _TG_LIMIT) -> list[str]:
    """Split text into chunks ≤ limit, breaking on newlines where possible."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def send_telegram_report(report_md: str, subject: str) -> bool:
    """Send finance report via Telegram bot. Returns True if all chunks sent."""
    token = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured, skipping")
        return False

    # report_md already starts with "# [Daily_Intel] ..." — don't add subject again
    body_html = _md_to_tg_html(report_md)
    chunks = _tg_chunks(body_html)

    success = True
    for i, chunk in enumerate(chunks):
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info(f"Telegram sent chunk {i+1}/{len(chunks)}")
        except Exception as e:
            logger.warning(f"Telegram send failed (chunk {i+1}): {e}")
            success = False
    return success


def send_telegram_alert(text: str) -> bool:
    """Send a short plaintext failure alert to Telegram. Fail-open — never raises."""
    token = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


# ── Email footer (finance-specific) ──────────────────────────────────────────

def _ibkr_auth_note() -> str:
    """IBKR gateway integration — currently disabled. Returns '' unconditionally.

    Original implementation checked Client Portal Gateway auth status and added
    a footer warning when the session had expired. Re-enable by restoring the
    body below and shipping the ibkr/ module alongside this script.
    """
    # IBKR_DISABLED: gateway not bundled in this distribution
    return ""


def _fmt_llm_meta(meta: dict) -> str:
    """Format call_llm()'s _llm_meta into a short human-readable provider/retry summary."""
    if not meta:
        return "调用失败（已发送告警）"
    if meta.get("fallback"):
        return (f"OR flex fallback → {meta['model']} via {meta.get('provider', 'n/a')}"
                f"（主模型 {meta.get('primary_attempts', '?')} 次尝试均失败）")
    provider = meta.get("provider", "n/a")
    attempts = meta.get("attempts", 1)
    if attempts > 1:
        return f"OR/{provider}（第 {attempts} 次尝试成功）"
    return f"OR/{provider}"


def finance_footer(date_str: str, budget: dict) -> str:
    ibkr_note = _ibkr_auth_note()
    return (
        f"\n\n---\n"
        f"_Daily_Intel · {date_str} ET_\n"
        f"{ibkr_note}"
    )


# ── TG-only run status message ──────────────────────────────────────────────

def build_status_message(
    today_et: str,
    slot_label: str,
    budget: dict,
    serpapi_budget: dict,
    tavily_used_before: int,
    serpapi_used_before: int,
    news_items: list,
    guardian_enabled: bool,
    finnhub_tickers: list,
    finnhub_news_section: str,
    sonar_macro_section: str,
    polymarket_section: str,
    adanos_section: str,
    adanos_budget: dict,
    all_search_jobs: list,
    raw_results: list,
    filtered: list,
    extract_results: list,
    tavily_section: str,
    llm_meta_p1: dict,
    llm_meta_p2: dict,
) -> str:
    """Build a separate status report (Tavily/SerpApi usage, intel sources, LLM/Provider list)
    sent to TG only — kept out of the email/Obsidian report body."""
    providers = "/".join(DS_OR_PROVIDERS["order"])
    tavily_used_run = budget["used"] - tavily_used_before
    serpapi_used_run = serpapi_budget["used"] - serpapi_used_before

    lines = [
        f"**Daily_Intel 运行状态** · {today_et} {slot_label}",
        "",
        f"Tavily今日剩余: {budget_remaining(budget)}/{TAVILY_DAILY_LIMIT}（本次用 {tavily_used_run}）",
    ]
    if serpapi_used_run:
        lines.append(
            f"SerpApi本月已用: {serpapi_budget['used']}/{SERPAPI_MONTHLY_LIMIT}（本次用 {serpapi_used_run}）"
        )

    lines += ["", "情报来源:"]
    lines.append(f"- RSS{'+Guardian' if guardian_enabled else ''}: {len(news_items)} 条")
    lines.append(
        f"- Finnhub即时新闻: 已注入 {len(finnhub_tickers)} ticker" if finnhub_news_section
        else "- Finnhub即时新闻: 无数据/未触发"
    )
    lines.append(f"- Sonar宏观快照: {'成功' if sonar_macro_section else '失败/跳过'}")
    lines.append(f"- Polymarket预测市场: {'成功' if polymarket_section else '无相关市场/跳过'}")
    if ADANOS_API_KEY:
        lines.append(
            f"- Adanos X舆情: {'成功' if adanos_section else '无数据/跳过'}"
            f"（本月已用 {adanos_budget['used']}/{ADANOS_MONTHLY_LIMIT}）"
        )
    if all_search_jobs:
        line = f"- Tavily/SerpApi搜索: {len(all_search_jobs)} 任务, {len(raw_results)} 条原始结果"
        if filtered:
            line += f" → 筛选 {len(filtered)} 条"
        lines.append(line)
        if extract_results:
            lines.append(f"- Tavily Extract: {len(extract_results)} 篇全文")
    else:
        lines.append("- Tavily/SerpApi搜索: 未触发")

    lines += ["", "LLM/Provider:"]
    lines.append(f"- Pass 1（{LLM_MODEL}）: {_fmt_llm_meta(llm_meta_p1)}")
    if raw_results:
        lines.append(f"- 语义过滤（{SEMANTIC_FILTER_MODEL}）: OR/{providers}")
    if sonar_macro_section:
        lines.append(f"- 宏观快照（{SONAR_MODEL}）: OR")
    if tavily_section:
        lines.append(f"- Pass 2（{LLM_MODEL_PASS2}）: {_fmt_llm_meta(llm_meta_p2)}")

    return "\n".join(lines)


# ── Search helper ────────────────────────────────────────────────────────────

def _do_search(query: str, budget: dict, serpapi_budget: dict,
               days: int = 1, search_depth: str = "basic",
               max_results: int = 12,
               start_date: str | None = None,
               end_date: str | None = None) -> list[dict]:
    """Run Tavily search with SerpApi fallback. Returns results list."""
    credits = 2 if search_depth == "advanced" else 1
    if budget_remaining(budget) >= credits:
        results = tavily_search(query, budget, days=days,
                                search_depth=search_depth, max_results=max_results,
                                start_date=start_date, end_date=end_date)
        if results:
            return results
    if serpapi_remaining(serpapi_budget) > 0:
        return serpapi_search(query, serpapi_budget)
    return []


# ── Concurrency lock ──────────────────────────────────────────────────────────

def _acquire_lock():
    """Acquire exclusive process lock; exits immediately if another instance is running.
    Returns the open file handle (must stay open to hold the lock).
    """
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except IOError:
        fd.close()
        logger.info("Another run_finance instance is already running (lock held), exiting")
        sys.exit(0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _lock_fd = _acquire_lock()
    try:
        _main_body()
    finally:
        _lock_fd.close()
        LOCK_FILE.unlink(missing_ok=True)


def _main_body():
    # 0. Handle forced overrides (for manual re-runs)
    force_date = os.getenv("FINANCE_FORCE_DATE", "")
    force_run  = os.getenv("FINANCE_FORCE_RUN", "")
    force_slot = os.getenv("FINANCE_FORCE_SLOT", "")

    # 1. Trading day check (skip if force_date or force_run is set)
    if not force_date and not force_run and not is_nyse_trading_day():
        logger.info("Non-trading day, exiting")
        sys.exit(0)

    if force_date:
        # Determine hour from forced slot (default to am if not set)
        _slot = force_slot if force_slot in ("am", "pm") else "am"
        _hour = 8 if _slot == "am" else 23
        now_et = datetime.strptime(force_date, "%Y-%m-%d").replace(hour=_hour, tzinfo=ET)
        logger.info(f"FORCE_DATE={force_date} FORCE_SLOT={_slot}")
    else:
        now_et = datetime.now(ET)
    today_et = now_et.strftime("%Y-%m-%d")

    if force_slot in ("am", "pm"):
        run_slot = force_slot
    else:
        run_slot = "pm" if now_et.hour >= 18 else "am"
    slot_label = "开盘前简报" if run_slot == "am" else "夜盘收市速报"
    logger.info(f"=== Daily_Intel run: {today_et} {slot_label} ===")

    # 1b. Duplicate guard — check monthly file for this slot's section header
    if _monthly_dedup(today_et, slot_label) and not force_run:
        logger.info(f"Report already exists for {today_et} {slot_label}, exiting")
        sys.exit(0)

    # 2. Load watchlist
    wl = load_watchlist()
    if not wl["recipients"]:
        logger.error("No recipients in watchlist.md, aborting")
        sys.exit(1)

    # 3. Load Tavily budget
    budget = load_budget()
    logger.info(f"Tavily budget: {budget['used']}/{TAVILY_DAILY_LIMIT} used today")
    serpapi_budget = load_serpapi_budget()
    logger.info(f"SerpApi budget: {serpapi_budget['used']}/{SERPAPI_MONTHLY_LIMIT} used this month")
    tavily_used_before = budget["used"]
    serpapi_used_before = serpapi_budget["used"]

    # 4. Fetch prices (slot-aware: AM=premarket, PM=close+afterhours)
    price_data_label = (
        "盘前数据（昨日全日↑↓=昨收vs前收，与Yahoo Finance口径一致；盘前↑↓=盘前价vs昨收）" if run_slot == "am"
        else "收盘+盘后数据（日内↑↓=今收vs前收，与Yahoo Finance口径一致；vs今开=纯盘中涨跌；盘后↑↓=盘后价vs今收）"
    )
    pm_afterhours_note = (
        f"注意（夜盘报告）：价格表【盘后涨跌】列反映收盘后截至 {now_et.strftime('%H:%M %Z')} 的最新运行状态。"
        f"Finnhub 即时新闻已聚焦过去8小时异动标的。"
        f"请在【价格异动】中分别说明日内表现与盘后延续/反转情况，无盘后数据时注明\"盘后无成交\"。"
        if run_slot == "pm" else ""
    )
    price_rows = fetch_prices(
        stocks=wl["stocks"],
        commodities=wl["commodities"],
        fx=wl["fx"],
        thresholds=wl["thresholds"],
        slot=run_slot,
        report_date=now_et.date(),
    )
    price_table = format_price_table(price_rows, slot=run_slot)
    anomalies = get_anomalies(price_rows)
    logger.info(f"Prices: {len(price_rows)} tickers, {len(anomalies)} anomalies")

    # Detect tickers that yfinance + Finnhub both failed to fetch — warn LLM not to hallucinate prices
    _all_wl_tickers = set(wl["stocks"] + wl["commodities"] + wl["fx"])
    _fetched = {r.ticker for r in price_rows}
    _failed  = sorted(_all_wl_tickers - _fetched)
    if _failed:
        logger.warning(f"Price data missing for: {_failed}")
    price_missing_note = (
        f"[!] 以下标的价格数据获取失败，报告中**不得引用**其具体价格数字"
        f"（盘前涨幅、昨收、当前价等）：{', '.join(_failed)}\n"
        if _failed else ""
    )

    # 5. Fetch RSS news (last 24h since last report)
    news_items = fetch_rss(hours=24, geo_keywords=wl["geo_keywords"])
    guardian_key = os.environ.get("GUARDIAN_API_KEY")
    if guardian_key:
        guardian_items = fetch_guardian_news(hours=24, geo_keywords=wl["geo_keywords"], api_key=guardian_key)
        news_items = sorted(news_items + guardian_items, key=lambda x: x.published, reverse=True)
    news_text = format_news_for_prompt(news_items, wl["geo_keywords"])

    # Code-level skip: if no price anomalies AND no geo/macro RSS hits, exit early
    triggered_geo_topics = sorted(set(t for item in news_items for t in item.topics))
    has_anomaly = len(anomalies) > 0
    geo_topics_str = ", ".join(triggered_geo_topics) if triggered_geo_topics else "（无）"
    logger.info(f"Triggered geo topics: {geo_topics_str}")

    if not has_anomaly and not triggered_geo_topics:
        logger.info("No price anomalies, no geo/macro RSS hits — skipping")
        sys.exit(0)

    # 6. Fetch personal knowledge base context (fail-open)
    anomaly_ticker_syms = [r.ticker for r in anomalies] if anomalies else []
    kb_context = get_finance_context(
        anomaly_tickers=anomaly_ticker_syms,
        geo_topics=list(wl["geo_keywords"].keys()),
    )
    kb_section = f"\n## 个人知识库上下文\n{kb_context}\n" if kb_context else ""

    # 7. Compute query window (needed by 6b and LLM prompt)
    last_date = get_last_report_date()
    try:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
        query_days = max(1, min(3, (datetime.now(ET).date() - last_dt).days + 1))
    except (ValueError, TypeError):
        query_days = 1
    logger.info(f"Query window: {query_days} day(s) since last report ({last_date})")

    # 6b. Finnhub ticker-specific news (free, no quota cost)
    # AM: anomaly tickers first + watchlist fill-up, cap 8, window = query_days * 24h (up to 48h)
    # PM: anomaly tickers only (AH movers matter most), cap 5, window = 8h (covers AH 4 PM–midnight)
    #     Keeping cap at 5 for PM avoids unnecessary calls: 5 calls << 60 req/min free limit
    if run_slot == "pm":
        _finnhub_tickers = anomaly_ticker_syms[:5]  # only movers; no filler for PM
        _finnhub_hours = 8                           # AH window: last 8h before midnight
    else:
        _finnhub_tickers = list(dict.fromkeys(
            anomaly_ticker_syms + [t for t in wl["stocks"] if t not in anomaly_ticker_syms]
        ))[:8]
        _finnhub_hours = min(query_days * 24, 48)    # 24h normally, up to 48h after weekend
    finnhub_news_section = fetch_finnhub_news(_finnhub_tickers, hours=_finnhub_hours)
    logger.info(f"Finnhub news: slot={run_slot}, tickers={_finnhub_tickers}, hours={_finnhub_hours}")

    # 6c. Sonar macro brief — real-time multi-source synthesis (AM + PM)
    # Query is built dynamically from watchlist; evolves as holdings change.
    sonar_macro_section = _sonar_macro_brief(
        slot=run_slot,
        stocks=wl["stocks"],
        commodities=wl["commodities"],
        fx=wl["fx"],
        geo_topics=list(wl["geo_keywords"].keys()),
        now_et=now_et,
        portfolio_snapshot=kb_context[:400] if kb_context else "",
    )

    # 6d. Social sentiment — Polymarket (free, prediction-market odds) + Adanos X/Twitter
    # (free tier, capped monthly budget). Anomaly tickers prioritized for Adanos, same as Finnhub.
    # Wrapped in its own try/except so any unexpected failure here (e.g. budget file I/O)
    # degrades to empty sections instead of taking down the rest of the run.
    polymarket_section = ""
    adanos_section = ""
    adanos_budget = {"year_month": "", "used": 0}
    try:
        adanos_budget = load_adanos_budget()
        polymarket_section = _polymarket_brief(list(wl["geo_keywords"].keys()))
        _adanos_tickers = list(dict.fromkeys(
            anomaly_ticker_syms + [t for t in wl["stocks"] if t not in anomaly_ticker_syms]
        ))[:4]
        adanos_section = _adanos_x_sentiment(_adanos_tickers, adanos_budget)
        save_adanos_budget(adanos_budget)
    except Exception as e:
        logger.warning(f"Social sentiment step failed, continuing without it: {e}")
    social_sentiment_section = polymarket_section + adanos_section
    logger.info(
        f"Social sentiment: polymarket={'yes' if polymarket_section else 'no'}, "
        f"adanos={'yes' if adanos_section else 'no'} "
        f"(budget {adanos_budget['used']}/{ADANOS_MONTHLY_LIMIT})"
    )

    # 7b. First LLM pass — analyze and generate search tasks

    prompt = USER_PROMPT_TEMPLATE.format(
        date=today_et,
        now_str=now_et.strftime("%Y-%m-%d %H:%M %Z"),
        last_report_date=last_date,
        query_days=query_days,
        triggered_geo_topics=geo_topics_str,
        pm_afterhours_note=pm_afterhours_note,
        price_data_label=price_data_label,
        price_table=price_table,
        price_missing_note=price_missing_note,
        news_text=news_text,
        finnhub_news_section=finnhub_news_section,
        sonar_macro_section=sonar_macro_section,
        social_sentiment_section=social_sentiment_section,
        tavily_section="",
        kb_section=kb_section,
    )
    result = call_llm(prompt)
    llm_meta_p1 = result.get("_llm_meta", {})

    # 8. Build search job list — all basic (Extract provides the depth)
    # AM anomaly: downgraded to basic (saves 1cr vs old advanced; Extract compensates)
    # PM anomaly: skipped when Finnhub AH news available
    all_search_jobs: list[dict] = []

    # Precise date range for Tavily (replaces days=N)
    search_start = last_date if last_date != "N/A（首次运行）" else None
    search_end   = today_et

    if anomalies:
        finnhub_covers_anomalies = run_slot == "pm" and bool(finnhub_news_section)
        if not finnhub_covers_anomalies:
            anomaly_q = " ".join(r.ticker for r in anomalies[:5]) + " stock news earnings"
            all_search_jobs.append({
                "query": anomaly_q,
                "search_depth": "basic",  # Extract will provide depth
                "days": query_days,
                "max_results": 15,
            })
        else:
            logger.info("PM slot: skipping anomaly Tavily query — Finnhub AH news available")

    for qobj in result.get("tavily_queries", []):
        if not isinstance(qobj, dict) or not qobj.get("query"):
            continue
        # All queries forced to basic — Extract handles depth
        all_search_jobs.append({**qobj, "search_depth": "basic"})

    # 9. Layer 1 — Discovery: run all basic searches, accumulate raw results
    tavily_section = ""
    raw_results: list[dict] = []
    filtered: list[dict] = []        # populated in Layer 2b; needed for archive writer
    extract_results: list[dict] = [] # populated in Layer 3; needed for archive writer
    for job in all_search_jobs:
        if budget_remaining(budget) < 1:
            logger.info("Tavily budget exhausted, stopping search")
            break
        results = _do_search(
            job["query"], budget, serpapi_budget,
            days=job.get("days", query_days),
            search_depth="basic",
            max_results=job.get("max_results", 12),
            start_date=search_start,
            end_date=search_end,
        )
        raw_results.extend(results)

    if raw_results:
        # Layer 2a — Script pre-screen: score + deduplicate → top 15 candidates
        prescreened = score_and_filter(
            raw_results,
            anomaly_ticker_syms,
            list(wl["geo_keywords"].keys()),
            top_n=15,
        )

        # Layer 2b — Haiku semantic ranking: supply-chain aware → top 10
        # Considers upstream/downstream/macro, not just direct ticker mentions
        filtered = _haiku_relevance_filter(
            prescreened,
            anomaly_ticker_syms,
            list(wl["geo_keywords"].keys()),
            portfolio_tickers=wl["stocks"],
            top_n=10,
        )

        # Layer 3 — Extract: 10 URLs = 2cr (1cr per 5 URLs)
        extract_urls = [r["url"] for r in filtered[:10] if r.get("url")]
        extract_cost = math.ceil(len(extract_urls) / 5) if extract_urls else 0
        if extract_urls and budget_remaining(budget) >= extract_cost:
            extract_q = (
                " ".join(anomaly_ticker_syms[:3])
                + " " + geo_topics_str[:80]
            ).strip()
            extract_results = tavily_extract(extract_urls, extract_q, budget)

        if extract_results:
            tavily_section = format_extract_results(extract_results, candidates=prescreened)
            logger.info(f"Using Extract chunks for Pass 2 ({len(extract_results)} sources)")
        elif filtered:
            tavily_section = format_tavily_results(filtered)
            logger.info(f"Extract unavailable, using search summaries ({len(filtered)} results)")

    # 9b. Archive cleaned Extract full text to local disk (outside Obsidian, never mined)
    write_extract_archive(today_et, run_slot, now_et, all_search_jobs, filtered, extract_results)

    # 10. If Tavily added new data, do a second LLM pass to incorporate it
    # Pass 2 uses SYSTEM_PROMPT_P2 (Layer A) + personal context (Layer B) for portfolio-aware analysis.
    report_md = result.get("report_md", "")
    llm_meta_p2 = {}
    if tavily_section:
        personal_context = _load_personal_context()
        prompt2 = USER_PROMPT_TEMPLATE_P2.format(
            date=today_et,
            now_str=now_et.strftime("%Y-%m-%d %H:%M %Z"),
            last_report_date=last_date,
            pm_afterhours_note=pm_afterhours_note,
            price_data_label=price_data_label,
            price_table=price_table,
            price_missing_note=price_missing_note,
            news_text=news_text,
            finnhub_news_section=finnhub_news_section,
            sonar_macro_section=sonar_macro_section,
            social_sentiment_section=social_sentiment_section,
            tavily_section=tavily_section,
            kb_section=kb_section,
            personal_context=personal_context,
        )
        result2 = call_llm(prompt2, model=LLM_MODEL_PASS2, system_prompt=SYSTEM_PROMPT_P2)
        llm_meta_p2 = result2.get("_llm_meta", {})
        report_md = result2.get("report_md", report_md)

    if not report_md:
        logger.warning("Empty report_md, skipping")
        send_telegram_alert(
            f"[!] Daily_Intel {today_et} {slot_label} 生成失败：Pass 1/Pass 2 均未返回有效 report_md，"
            f"本次报告未发送。详见 /tmp/daily_intelligence.log"
        )
        sys.exit(0)

    # Fix report title for PM slot (LLM always writes 开盘前简报 regardless of slot)
    if run_slot == "pm":
        report_md = re.sub(
            r"^# \[Daily_Intel\] .+",
            f"# [Daily_Intel] {today_et} {slot_label}",
            report_md,
            flags=re.MULTILINE,
        )

    # 11. Write to Obsidian monthly file
    write_report(today_et, slot_label, report_md, budget)
    _mempalace_add_daily_drawer(today_et, run_slot, report_md)

    # 11b. Write context log to Obsidian (price table + triggered news + Sonar + queries)
    write_context_log(today_et, slot_label, now_et, price_table,
                      news_items, triggered_geo_topics, sonar_macro_section, all_search_jobs)

    # 12. Send email
    footer = finance_footer(today_et, budget)
    subject = f"[Daily_Intel] {today_et} {slot_label}"
    sent = send_report(
        subject=subject,
        markdown_body=report_md + footer,
        recipients=wl["recipients"],
    )
    if sent:
        logger.info(f"Email sent → {wl['recipients']}")
    else:
        logger.error("Email send failed")

    # 13. Send Telegram report
    send_telegram_report(report_md + footer, subject)

    # 13b. Send TG-only run status (Tavily/SerpApi usage, intel sources, LLM/Provider list)
    status_md = build_status_message(
        today_et, slot_label, budget, serpapi_budget,
        tavily_used_before, serpapi_used_before,
        news_items, bool(guardian_key), _finnhub_tickers, finnhub_news_section,
        sonar_macro_section, polymarket_section, adanos_section, adanos_budget,
        all_search_jobs, raw_results, filtered, extract_results,
        tavily_section, llm_meta_p1, llm_meta_p2,
    )
    send_telegram_report(status_md, "")

    logger.info(f"=== Done. Tavily used today: {budget['used']}/{TAVILY_DAILY_LIMIT} ===")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        logger.exception("Unhandled exception in main()")
        send_telegram_alert(
            f"[!] Daily_Intel 运行崩溃：{type(e).__name__}: {e}\n详见 /tmp/daily_intelligence.log"
        )
        sys.exit(1)
