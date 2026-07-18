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
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

from fetch_prices import fetch_prices, format_price_table, get_anomalies, fetch_52week_stats
from fetch_news import fetch_rss, fetch_guardian_news, format_news_for_prompt
from memory_context_finance import get_finance_context

from finance_email import send_report

# ── Split modules (issue #42, 2026-07-17): budget trackers, external intel
# source briefs, report writers, and AM calibration were extracted out of this
# file to shrink it. Re-imported here (rather than referenced via submodule
# attribute access) so `import run_finance as rf; rf.load_budget(...)` etc.
# keeps working unchanged for sas_review.py's cross-module access pattern
# (only load_budget/save_budget/budget_remaining are actually accessed that
# way today — the rest are re-imported because run_finance.py's own code
# calls them directly, not for hypothetical future rf.* access).
from budget_trackers import (
    load_budget, save_budget, budget_remaining,
    load_serpapi_budget, save_serpapi_budget, serpapi_remaining,
    load_adanos_budget, save_adanos_budget,
    load_apify_budget, save_apify_budget,
    load_brave_budget,
    TAVILY_DAILY_LIMIT, SERPAPI_MONTHLY_LIMIT, ADANOS_MONTHLY_LIMIT,
    APIFY_MONTHLY_LIMIT, BRAVE_MONTHLY_LIMIT,
)
from intel_sources import (
    _sonar_macro_brief, _polymarket_brief, _adanos_x_sentiment,
    _reddit_sentiment_brief, fetch_liquidity_snapshot,
    fetch_finnhub_news, fetch_brave_news,
)
from report_writers import (
    write_context_log, write_extract_archive,
    _monthly_dedup, get_last_report_date,
    _mempalace_add_daily_drawer, write_report,
    send_telegram_report, send_telegram_alert,
    _fmt_llm_meta, finance_footer,
)
from calibration import (
    write_sas_candidate_log, _load_recent_calibration_notes, evaluate_am_calibration,
)
# Shared leaf modules (PR #43 review follow-up, 2026-07-17): call_llm and the
# title-dedup/source-confidence heuristics used to live here and be reached
# from intel_sources.py/report_writers.py/calibration.py via a deferred
# `from run_finance import ...` inside the consuming function. That silently
# assumed run_finance.py is registered in sys.modules under the name
# "run_finance" — false when it's run directly as the entrypoint (as launchd
# does): Python registers it as "__main__", so the deferred import triggered
# a second, full execution of this module on first call. Moving these into
# true leaf modules (imported by both run_finance.py and the modules that
# need them) removes the assumption entirely.
from llm_client import call_llm
from scoring_utils import (
    _title_tokens_for_dedup, _title_keyword_hits, _token_jaccard,
    _TITLE_DEDUP_THRESHOLD, _TITLE_DEDUP_THRESHOLD_NO_KEYWORD,
    _TITLE_DEDUP_STRICT_NO_DATE_THRESHOLD, _TITLE_DEDUP_WINDOW_HOURS,
    _DEDUP_STOPWORDS, _source_confidence_tags, build_keyword_set,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# httpx's own request logger (propagates to root at INFO) logs the full request
# URL, and several APIs here (Finnhub, Guardian) put the key in the query
# string — without this, keys get written in plaintext to the log file
# (issue #21).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

OBSIDIAN    = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))

# Project-local paths (self-contained within ~/Daily_Intelligence/)
_PROJ_DIR        = Path(os.path.dirname(os.path.abspath(__file__))).parent
WATCHLIST_PATH   = OBSIDIAN / "Hermes/Daily Intelligence/watchlist.md"
LOCK_FILE        = _PROJ_DIR / "run_finance.lock"  # Prevents concurrent duplicate runs

TAVILY_API_KEY      = os.getenv("TAVILY_API_KEY", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE_URL         = "https://openrouter.ai/api/v1/chat/completions"
OR_ATTRIBUTION_HEADERS = {"HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence", "X-OpenRouter-Title": "DailyIntel"}
LLM_MODEL           = "deepseek/deepseek-v4-flash"
LLM_MODEL_PASS2     = "deepseek/deepseek-v4-pro"
SEMANTIC_FILTER_MODEL = "deepseek/deepseek-v4-flash"
LLM_FALLBACK_FLASH  = "google/gemini-3.1-flash-lite"  # OR flex fallback for v4-flash
LLM_FALLBACK_PRO    = "google/gemini-3.5-flash"       # OR flex fallback for v4-pro
DS_OR_PROVIDERS     = {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}
SONAR_MODEL         = "perplexity/sonar"             # Macro brief (real-time search)
EXA_API_KEY         = os.getenv("EXA_API_KEY", "")
EXA_BASE_URL        = "https://api.exa.ai/chat/completions"

SERPAPI_API_KEY       = os.getenv("SERPAPI_API_KEY", "")

# Social sentiment sources (issue C4): Polymarket (free, no auth) + Adanos X/Twitter (free tier, keyed)
ADANOS_API_KEY         = os.getenv("ADANOS_API_KEY", "")

# Reddit sentiment via Apify's "Stock Sentiment Intelligence" actor (issue #17 step 3):
# Reddit's own API is free only for non-commercial use with a 100 req/min ceiling; this
# purpose-built actor (WSB/r-stocks/r-investing mentions + BULLISH/BEARISH/NEUTRAL signal)
# sidesteps that. Pay-per-event: ~$0.001/result + $0.00005/run, verified with a real 1-ticker
# call on 2026-07-16 ($0.00105). One run batches all watchlist tickers, so
# APIFY_MONTHLY_LIMIT runs/month stays far under the $5 one-time free credit
# (60 runs × ~4 tickers ≈ $0.25/mo at these rates).
APIFY_API_TOKEN      = os.getenv("APIFY_API_TOKEN", "")

# Brave News API (issue #14): independent Western search engine, not a Google
# proxy like Serper/SerpApi. No longer free as of 2026 — $5/mo prepaid credit
# then metered billing on file, so the monthly cap here is a hard stop, not a
# soft warning, to avoid unattended overage charges.
BRAVE_API_KEY       = os.getenv("BRAVE_API_KEY", "")

ET = ZoneInfo("America/New_York")

# Note: TAVILY_DAILY_LIMIT / SERPAPI_MONTHLY_LIMIT / ADANOS_MONTHLY_LIMIT /
# APIFY_MONTHLY_LIMIT / BRAVE_MONTHLY_LIMIT are re-exported from
# budget_trackers.py in the import block above (not re-defined here) so this
# file's own display code (build_status_message, log lines) can never drift
# from what's actually enforced — see PR #43 review, a prior local copy of
# these five constants risked exactly that silent desync.

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

# Video/aggregator path patterns (issue #19 direction 4): these pages are prone
# to caption-stacking with no per-caption timestamp (see _detect_low_structure),
# so a small scoring penalty lets the plain-article version of the same story
# naturally outrank the video/gallery version instead of needing a hard exclude.
_VIDEO_PATH_RE = re.compile(r"/(?:video|watch|gallery|live-blog)/", re.IGNORECASE)


def score_and_filter(
    results: list[dict],
    anomaly_tickers: list[str],
    geo_keywords: dict[str, list[str]],
    top_n: int = 8,
) -> list[dict]:
    """Score, deduplicate (exact URL + near-duplicate title within a time window),
    and return top_n search results by composite score.

    Composite = tavily_score
              + domain_bonus  (trusted financial/news source: +0.15)
              + recency_bonus (≤24h: +0.10; ≤72h: +0.05)
              + keyword_bonus (anomaly ticker or geo keyword in title/content: +0.05 each)
              + video_penalty (video/gallery/watch path: -0.08, issue #19 direction 4)

    Title dedup: different outlets covering the same wire story get different
    URLs but paraphrased headlines. Exact-URL dedup misses this almost
    entirely — a token-overlap match on titles that share a tracked ticker/geo
    keyword, within a 24h publish window, catches it without needing
    embeddings or an extra LLM call (see the module-level comment above
    _TITLE_DEDUP_THRESHOLD for why character-level similarity was rejected).
    Genuinely different angles on the same broader event (e.g. the
    geopolitical act itself vs. the market's price reaction to it) score low
    on token overlap and are correctly kept as separate, non-duplicate items.

    `geo_keywords` takes the curated topic→keyword dict from watchlist.md
    (e.g. "US-Iran": ["Iran", "nuclear", "Strait of Hormuz", ...]), not a bare
    list of topic labels — splitting a label like "US-Iran" into ["us","iran"]
    both misses real synonym anchors (a "Hormuz"-only headline never matched
    an "iran"-only one, confirmed missed in the 2026-07-15 PM production run)
    and introduces short-token false positives ("us" matching inside "focus").
    """
    # build_keyword_set() (scoring_utils.py) does the same lowering + multi-word
    # phrase word-splitting described above — shared with the corroboration
    # fingerprint (_extract_key_phrases) as of issue #19 follow-up so both keyword
    # anchoring paths stay in sync.
    keywords = build_keyword_set(anomaly_tickers, geo_keywords)

    now = datetime.now(ET)
    scored: list[tuple[float, dict, "datetime | None"]] = []
    seen: set[str] = set()

    for r in results:
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        s = float(r.get("score") or 0)
        domain = url.split("/")[2] if "//" in url else ""
        domain_bonus = 0.15 if any(d in domain for d in _TRUSTED_DOMAINS) else 0
        video_penalty = -0.08 if _VIDEO_PATH_RE.search(url) else 0

        recency_bonus = 0.0
        pub_dt = None
        pub = r.get("published_date", "")
        if pub:
            try:
                from dateutil import parser as _dp
                pub_dt = _dp.parse(pub).astimezone(ET)
                age_h = (now - pub_dt).total_seconds() / 3600
                recency_bonus = 0.10 if age_h <= 24 else (0.05 if age_h <= 72 else 0)
            except Exception:
                pass

        text = (r.get("title", "") + " " + (r.get("content") or "")).lower()
        kw_bonus = 0.05 * sum(1 for k in keywords if k and k in text)

        scored.append((s + domain_bonus + recency_bonus + kw_bonus + video_penalty, r, pub_dt))

    scored.sort(key=lambda x: x[0], reverse=True)

    kept: list[tuple[float, dict]] = []
    kept_meta: list[tuple[frozenset, frozenset, "datetime | None"]] = []
    dup_count = 0
    for score, r, pub_dt in scored:
        title = r.get("title", "")
        tokens = _title_tokens_for_dedup(title)
        kw_hits = _title_keyword_hits(title.lower(), keywords)
        is_dup = False
        for kept_tokens, kept_kw_hits, kept_dt in kept_meta:
            if kw_hits and kept_kw_hits:
                if not (kw_hits & kept_kw_hits):
                    continue  # different tracked topics — never compare
                threshold = _TITLE_DEDUP_THRESHOLD
            elif not kw_hits and not kept_kw_hits:
                threshold = _TITLE_DEDUP_THRESHOLD_NO_KEYWORD  # no anchor, need a stronger bar
            else:
                continue  # one hit a keyword, the other didn't — different category
            jac = _token_jaccard(tokens, kept_tokens)
            if jac < threshold:
                continue
            if pub_dt and kept_dt:
                if abs((pub_dt - kept_dt).total_seconds()) > _TITLE_DEDUP_WINDOW_HOURS * 3600:
                    continue  # confirmed too far apart in time — probably unrelated
            elif jac < _TITLE_DEDUP_STRICT_NO_DATE_THRESHOLD:
                # can't confirm publish-time proximity either way — require
                # near-exact wording before deduping instead of an unbounded match
                continue
            is_dup = True
            break
        if is_dup:
            dup_count += 1
            continue
        kept.append((score, r))
        kept_meta.append((tokens, kw_hits, pub_dt))

    top = [r for _, r in kept[:top_n]]
    if dup_count:
        logger.info(f"score_and_filter: dropped {dup_count} near-duplicate-title result(s) (cross-source dedup)")
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
                **OR_ATTRIBUTION_HEADERS,
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
                     "Content-Type": "application/json", **OR_ATTRIBUTION_HEADERS},
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



def format_extract_results(
    results: list[dict],
    candidates: list[dict] | None = None,
    extra_keywords: list[str] | None = None,
) -> str:
    """Format Extract results (full chunks, up to 600 chars each).

    Each source gets a confidence tag line (structure type / date confidence /
    rule-based corroboration count) so Pass 2 can hedge language on claims that
    are single-source and/or from undated caption-listing pages, instead of
    stating them as settled fact (see issue #19 — a Reuters video-hub caption
    with no date of its own was reported as a confirmed "signing set for Friday").
    `candidates` is the broader pre-extract search result pool (has published_date
    and title/content for corroboration matching); pass score_and_filter's output.
    `extra_keywords` is build_keyword_set()'s output — plugs the corroboration
    fingerprint's single-token/all-caps entity gap (issue #19 follow-up).
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
        lines.append(f"    [{_source_confidence_tags(url, full_text, candidates, extra_keywords)}]")
        if chunks:
            for i, chunk in enumerate(chunks[:2]):
                text = (chunk.get("content") or "")[:600]
                if text:
                    lines.append(f"    [{i+1}] {text}")
        elif raw:
            lines.append(f"    {raw[:800]}")
    return "\n".join(lines)


# ── Personal context helpers (for Pass 2 injection) ──────────────────────────

_framework_cache: str = ""


def _load_framework() -> str:
    """Extract operative rules from Investment Operating Manual v1.0.md (issue #30).

    Pulls three sections verbatim from the canonical Obsidian manual so edits there
    propagate to Pass 2 without code changes: Section 2 (能力边界, used for the
    capability-boundary tagging rule), Section 6 (Portfolio Construction, incl. the
    认知提升/减仓条件 checklists), Section 7.4 (Expectation Gap internal signal list).
    Module-level cache. Replaces the prior 金融资产信息.md excerpt.
    """
    global _framework_cache
    if _framework_cache:
        return _framework_cache
    manual_file = OBSIDIAN / "Finance" / "Investment Operating Manual v1.0.md"
    if not manual_file.exists():
        return ""
    text = re.sub(r"^---.*?---\s*", "", manual_file.read_text(encoding="utf-8"), flags=re.DOTALL)
    parts = []
    m = re.search(r"2\. 能力边界.*?\n\n(.*?)(?=\n\s*3\. Alpha 的来源)", text, re.DOTALL)
    if m:
        parts.append("【能力边界：以下变量属于能力圈外，不构成操作依据】\n" + m.group(1).strip()[:500])
    m2 = re.search(r"6\. Portfolio Construction\n\n(.*?)(?=\n\s*7\. Strategic Alpha Score)", text, re.DOTALL)
    if m2:
        parts.append("【Portfolio Construction：认知提升标准 / 减仓条件】\n" + m2.group(1).strip()[:1600])
    m3 = re.search(r"7\.4 Expectation Gap.*?\n\n(.*?)(?=\n\s*7\.5 Alpha Potential)", text, re.DOTALL)
    if m3:
        parts.append("【Expectation Gap 内部信号清单】\n" + m3.group(1).strip()[:1200])
    _framework_cache = "\n\n".join(parts)[:3200]
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


# Beta layer (QQQM/VOO) + defensive layer (EWJ/SGOL/BOXX) + cash — excluded from the
# "core individual holding" set per Investment Operating Manual Section 1's three-tier
# structure (issue #33). What's left is the ~25% active-Alpha layer these signals target.
_CORE_HOLDING_EXCLUDE = {"QQQM", "VOO", "EWJ", "SGOL", "BOXX", "CASH"}


def _get_core_holding_tickers() -> list[str]:
    """Active individual-stock tickers from the IB holdings snapshot (issue #33)."""
    try:
        latest_path = OBSIDIAN / "Finance" / "portfolio_report_latest.md"
        if not latest_path.exists():
            return []
        text = latest_path.read_text(encoding="utf-8")
        ib_section = re.search(r"## IB（账户 0611）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return []
        tickers = re.findall(r"### (\w+) \w+ \d+", ib_section.group(1))
        return [t for t in tickers if t.isalpha() and t not in _CORE_HOLDING_EXCLUDE]
    except Exception as e:
        logger.debug(f"Core holding ticker parse failed: {e}")
        return []


def _get_portfolio_weights() -> dict[str, float]:
    """Ticker → % of total portfolio market value (issue #33), for Manual Section 6
    condition C (single position > 15%). Parses per-holding 市值 + total 组合总市值(USD)
    from portfolio_report_latest.md — pure arithmetic, no LLM estimation needed."""
    try:
        latest_path = OBSIDIAN / "Finance" / "portfolio_report_latest.md"
        if not latest_path.exists():
            return {}
        text = latest_path.read_text(encoding="utf-8")
        total_m = re.search(r"组合总市值：.*?/\s*([\d,]+)\s*USD", text)
        if not total_m:
            return {}
        total_usd = float(total_m.group(1).replace(",", ""))
        if not total_usd:
            return {}
        ib_section = re.search(r"## IB（账户 0611）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return {}
        holdings = re.findall(
            r"### (\w+) \w+ \d+\n.*?市值：([\d.]+)\s*浮盈",
            ib_section.group(1), re.DOTALL
        )
        return {
            sym: round(float(mv) / total_usd * 100, 1)
            for sym, mv in holdings
            if sym.isalpha() and sym != "CASH"
        }
    except Exception as e:
        logger.debug(f"Portfolio weights parse failed: {e}")
        return {}


def _compute_holding_signals() -> str:
    """Issue #33: pure-computation signals for Manual 7.4 (股价相对位置) and the
    position-size-overload reduce-trigger in Section 6 (position share > 15%) —
    code-computed ground truth, not LLM estimation from prose.
    Fail-open at every step; returns "" if nothing computable."""
    core_tickers = _get_core_holding_tickers()
    weights = _get_portfolio_weights()
    if not core_tickers and not weights:
        return ""
    stats = fetch_52week_stats(core_tickers) if core_tickers else {}
    lines = ["【持仓计算信号：以下为代码直接计算的既定事实，非LLM估算，仓位是否结构性超载（占比是否超过15%）请直接读取此处】"]
    all_tickers = core_tickers or list(weights.keys())
    for ticker in all_tickers:
        parts = []
        s = stats.get(ticker)
        if s:
            parts.append(f"52周区间百分位{s['range_percentile']}%（距历史高点{s['pct_from_high']}%）")
        w = weights.get(ticker)
        if w is not None:
            flag = "，已超过15%阈值（构成仓位结构性超载）" if w > 15 else ""
            parts.append(f"占组合{w}%{flag}")
        if parts:
            lines.append(f"- {ticker}：" + "；".join(parts))
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


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
    signals = _compute_holding_signals()
    if signals:
        parts.append(signals)
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

# AM-only instruction (issue #10): ask for a short list of falsifiable claims
# that the PM run can mechanically check against actual EOD data that evening.
# Kept out of the PM prompt — the point is to test the morning's calls against
# what actually happened, not to have every report predict itself.
VERIFIABLE_SIGNALS_INSTRUCTION_P1 = (
    "4. report_md 结尾追加\"## 可验证信号\"小节，2-4条，每条必须是可被今晚收盘后核验的具体条件-结果断言"
    "（如\"WTI跌破$65→通胀预期继续下修\"、\"Fed官员今日确认/否认降息\"），不写模糊定性描述（如\"值得关注\"）"
)
VERIFIABLE_SIGNALS_INSTRUCTION_P2 = (
    "可验证信号（仅开盘前简报要求）：report_md 结尾追加\"## 可验证信号\"小节，2-4条，每条必须是可被今晚"
    "收盘后核验的具体条件-结果断言（价格阈值/事件是否发生），不写模糊定性描述——这些会在今晚 PM 报告生成前"
    "被核验，核验结果沉淀为知识库供未来报告参考"
)

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

{finnhub_news_section}{brave_news_section}{sonar_macro_section}{social_sentiment_section}{tavily_section}{kb_section}{calibration_notes}
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
{verifiable_signals_rule}
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

{finnhub_news_section}{brave_news_section}{sonar_macro_section}{social_sentiment_section}{tavily_section}{kb_section}{calibration_notes}
---

== 持仓与框架 ==
{personal_context}

== 分析要求 ==
必须覆盖（以下每一条都独立成立，不依赖其他条目的编号，直接按内容判断即可）：

**价格异动含义**：今日价格异动（[!]标记标的）的驱动力，以及对该持仓逻辑的具体含义

**地缘政治传导**：地缘政治动态对持仓的潜在传导路径（有则写，无则省略）

**宏观信号与仓位暴露**：宏观信号（利率、商品、汇率）与仓位暴露的关系

**信源置信度处理**：[Tavily Extract] 每条来源前标注了 [信源类型 | 发布时间 | 交叉印证] 标签。
   写入具体断言（尤其含日期、协议签署、人事变动等强论断）前先看该来源标签：
   - 标"单一信源"或"视频/聚合页疑似caption堆叠"或"发布时间：未知"的，正文必须用
     "未证实"/"单一信源，待核实"等措辞明确降级，不得以确定语气写成既成事实
   - 标"约N天前"且 N 较大的，需提示"可能已被后续事件覆盖"
   - 有"N个独立域名佐证"（N≥1）的可正常按事实陈述
   快变的宏观/市场行情下，传统媒体报道常滞后于现状，未标注可靠时间戳的信息尤其容易过时，
   宁可标注不确定，也不要把孤证当结论

**驱动因素归类（能力圈内外）**：对每个异动/驱动因素，先判断它是否属于以下五类——利率与宏观周期、
   财报超预期或不及预期、资金流向与风格轮动、技术分析与短期timing、情绪驱动的价格波动（这五类是
   下方"持仓与框架"中列出的能力圈外变量，个人投资者对其既无信息优势也无影响力）。属于这五类的，
   只客观陈述事实，并显式加注"（不构成操作依据）"，不得据此给出隐含的加减仓暗示；不属于这五类的
   （如战略执行进展、竞争格局变化、产品/商业化里程碑），可以讨论其对持仓逻辑的含义，但这类讨论同样
   不直接构成操作依据——是否真正触发加仓或减仓，必须满足下面"持仓异动核对"里列出的具体事实标准，
   不能仅因为"值得讨论"就单独给出仓位建议

**流动性水位分级响应**：若上方注入了"流动性水位快照"（FRED），只作为背景参考，不单独触发操作建议——
   它是对回撤应对框架的补充信号，不是替代。整体标记【正常】：不提及或一笔带过。标记【观察】：可以
   提示"流动性边际收紧，保持现有仓位，暂不启动避险交易"，不建议减仓。标记【警戒】：可以提示"优先
   评估高贝塔个股暴露"，但核心仓位（QQQM/VOO）仍按回撤框架的价格/回撤幅度决定操作，不得因这一项
   信号单独建议清仓或大幅减仓

**持仓异动核对（唯一允许给出加减仓建议的依据来源）**：涉及"IB美股持仓"快照中实际持有标的（非仅
   观察用的watchlist标的）的异动，须逐条核对以下具体标准，命中的写明命中哪条+对应事实依据；全部
   不命中，必须写"不构成加/减仓依据"，不得给出与下列标准无关的仓位建议：
   - 认知提升（加仓依据，需满足其一，且必须基于新出现的可验证事实而非价格变动本身）：企业解锁了
     一个之前不确定的战略节点（如产品从内测进入商业化、新市场首次产生可计量收入）；竞争格局出现了
     有利于企业的结构性变化（如主要竞争对手退出、监管为企业构建护城河）；管理层兑现了此前市场明确
     怀疑的承诺（如量产节点、利润率或份额目标在财报中被证实）
   - Alpha大幅兑现（减仓依据）：预期差较建仓时明显收窄（评分下降超过3分），且未来Alpha潜力评分
     转弱（低于5分），且找不到能让预期差重新扩大的具体催化剂
   - 出现更高赔率机会（减仓依据）：识别到另一候选标的，其未来Alpha潜力评分比当前持仓高2分以上，
     且两者战略空间量级相当（同量级TAM）
   - 仓位结构性超载（减仓依据）：并非主动加仓，而是价格被动上涨导致单一标的占组合比例超过15%——
     这个占比数字直接读取下方【持仓计算信号】里代码算好的值，不要自己从持仓文本估算
   上面"驱动因素归类"里讨论的战略含义仅作理解背景，不能替代这里的逐条核对结果

**SAS候选证据标注**（不写入 report_md 正文，只填 JSON 字段，与前面的仓位建议判断相互独立）：若某条
   新闻/事件命中以下五类内部信号之一——内部人在公开市场的自主买入、资本配置方向的持续性（R&D/Capex
   占收入比在下跌期是否维持或提升）、生态位置的第三方验证（其他公司/客户选择在其平台上构建）、监管
   文件语言的季度间变化、公司自身"市场曾怀疑后被证实"的历史先例——或命中上面"持仓异动核对"里认知
   提升的三条标准之一，且涉及"IB美股持仓"中实际持有的标的，则在 sas_candidates 数组追加一条记录，
   含 ticker/category/fact 三个字段；category 只能取以下字符串之一："内部人增持"/"资本配置持续性"/
   "生态位验证"/"监管语言变化"/"历史先例"/"认知提升-战略节点解锁"/"认知提升-竞争格局变化"/
   "认知提升-管理层兑现承诺"。fact 为一句话事实摘要（含关键数字/来源，不超过80字）。无命中则留空
   数组。这只是记录候选证据供未来复审，本身不代表应该操作，不影响上面仓位建议的判断
{verifiable_signals_rule}

可选覆盖：其他值得关注的市场要闻（无实质内容可省略）

格式要求：价格数据已在上方表格，正文自由展开，有话则长，无话则短，省略废话

请输出以下JSON（不要附加任何其他文字）：
{{
  "report_md": "# [Daily_Intel] {date} 开盘前简报\\n\\n...",
  "sas_candidates": [{{"ticker": "...", "category": "...", "fact": "..."}}]
}}
"""





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
    brave_news_section: str,
    brave_budget: dict,
    sonar_macro_section: str,
    polymarket_section: str,
    adanos_section: str,
    adanos_budget: dict,
    reddit_section: str,
    apify_budget: dict,
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
    if BRAVE_API_KEY:
        lines.append(
            f"- Brave News: {'成功' if brave_news_section else '无数据/跳过'}"
            f"（本月已用 {brave_budget['used']}/{BRAVE_MONTHLY_LIMIT}）"
        )
    lines.append(f"- Sonar宏观快照: {'成功' if sonar_macro_section else '失败/跳过'}")
    lines.append(f"- Polymarket预测市场: {'成功' if polymarket_section else '无相关市场/跳过'}")
    if ADANOS_API_KEY:
        lines.append(
            f"- Adanos X舆情: {'成功' if adanos_section else '无数据/跳过'}"
            f"（本月已用 {adanos_budget['used']}/{ADANOS_MONTHLY_LIMIT}）"
        )
    if APIFY_API_TOKEN:
        lines.append(
            f"- Reddit舆情(Apify): {'成功' if reddit_section else '无数据/跳过'}"
            f"（本月已用 {apify_budget['used']}/{APIFY_MONTHLY_LIMIT}）"
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


# ── Core-holding cognitive-upgrade rotation query (issue #33) ─────────────────
# AM/PM search triggering is otherwise entirely anomaly/geo-driven — a core
# holding that isn't moving never gets a proactive check for the Manual
# Section 6 cognitive-upgrade fact types (product/commercialization milestones,
# competitive-landscape shifts, management delivering on doubted promises).
# One rotation query/day, deterministic by date (no rotation-state file to
# maintain), appended after the anomaly/geo/LLM-suggested jobs so it only
# consumes leftover Tavily budget rather than competing with real signals.

_COGNITIVE_UPGRADE_LOOKBACK_DAYS = 30


def _build_cognitive_upgrade_query(ticker: str, today_et: str) -> str:
    year = today_et[:4]
    return (
        f"{ticker} product commercialization milestone OR competitive landscape "
        f"change OR management guidance confirmed {year}"
    )


def _rotation_search_job(today_et: str) -> dict | None:
    """Pick one core holding for today via date.toordinal() % N — self-correcting
    if the holding list changes, no persisted state to go stale."""
    core_tickers = _get_core_holding_tickers()
    if not core_tickers:
        return None
    from datetime import date as _date
    try:
        idx = _date.fromisoformat(today_et).toordinal() % len(core_tickers)
    except ValueError:
        return None
    ticker = core_tickers[idx]
    return {
        "query": _build_cognitive_upgrade_query(ticker, today_et),
        "search_depth": "basic",
        "days": _COGNITIVE_UPGRADE_LOOKBACK_DAYS,
        "max_results": 10,
        # Explicit None overrides the loop's default "since last report" window
        # (usually ~1 day) — this query needs a 30-day lookback, not yesterday's news.
        "start_date": None,
        "end_date": None,
        "_rotation_ticker": ticker,  # for logging only
    }


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

    # 6b2. Brave News (issue #14) — independent Western search engine, budget-capped
    # (see BRAVE_MONTHLY_LIMIT note: Brave dropped its free tier in 2026, hard stop
    # to avoid unattended card charges). Wrapped in try/except like the social
    # sentiment step so a budget-file I/O hiccup can't take down the rest of the run.
    brave_news_section = ""
    brave_budget = {"year_month": "", "used": 0}
    try:
        brave_budget = load_brave_budget()
        brave_news_section = fetch_brave_news(
            _finnhub_tickers, list(wl["geo_keywords"].keys()), brave_budget,
        )
    except Exception as e:
        logger.warning(f"Brave News step failed, continuing without it: {e}")

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
        price_table=price_table,
    )

    # 6d. Social sentiment — Polymarket (free, prediction-market odds) + Adanos X/Twitter
    # (free tier, capped monthly budget). Anomaly tickers prioritized for Adanos, same as Finnhub.
    # Wrapped in its own try/except so any unexpected failure here (e.g. budget file I/O)
    # degrades to empty sections instead of taking down the rest of the run.
    polymarket_section = ""
    adanos_section = ""
    adanos_budget = {"year_month": "", "used": 0}
    reddit_section = ""
    apify_budget = {"year_month": "", "used": 0}
    try:
        adanos_budget = load_adanos_budget()
        polymarket_section = _polymarket_brief(list(wl["geo_keywords"].keys()))
        _social_tickers = list(dict.fromkeys(
            anomaly_ticker_syms + [t for t in wl["stocks"] if t not in anomaly_ticker_syms]
        ))[:4]
        adanos_section = _adanos_x_sentiment(_social_tickers, adanos_budget)
        save_adanos_budget(adanos_budget)
        apify_budget = load_apify_budget()
        reddit_section = _reddit_sentiment_brief(_social_tickers, apify_budget)
        save_apify_budget(apify_budget)
    except Exception as e:
        logger.warning(f"Social sentiment step failed, continuing without it: {e}")

    # 6e. Liquidity plumbing snapshot (FRED) — supports 市场见顶预警指标.md.
    # Same reasoning as above: isolated try/except so a FRED hiccup can't
    # take down the run. Folded into the same social_sentiment_section slot
    # (both are optional macro-context sections) rather than adding a new
    # template variable to both prompts.
    liquidity_section = ""
    try:
        liquidity_section = fetch_liquidity_snapshot()
    except Exception as e:
        logger.warning(f"Liquidity snapshot step failed, continuing without it: {e}")

    social_sentiment_section = polymarket_section + adanos_section + reddit_section + liquidity_section
    logger.info(
        f"Social sentiment: polymarket={'yes' if polymarket_section else 'no'}, "
        f"adanos={'yes' if adanos_section else 'no'} "
        f"(budget {adanos_budget['used']}/{ADANOS_MONTHLY_LIMIT}), "
        f"reddit={'yes' if reddit_section else 'no'} "
        f"(budget {apify_budget['used']}/{APIFY_MONTHLY_LIMIT})"
    )

    # 7b. First LLM pass — analyze and generate search tasks

    # AM-only: recent AM-calibration knowledge (issue #10), read directly from
    # Obsidian (see _load_recent_calibration_notes docstring for why this
    # bypasses MemPalace). No-op most of the time until entries accumulate.
    calibration_notes = _load_recent_calibration_notes() if run_slot == "am" else ""

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
        brave_news_section=brave_news_section,
        sonar_macro_section=sonar_macro_section,
        social_sentiment_section=social_sentiment_section,
        tavily_section="",
        kb_section=kb_section,
        calibration_notes=calibration_notes,
        verifiable_signals_rule=VERIFIABLE_SIGNALS_INSTRUCTION_P1 if run_slot == "am" else "",
    )
    result = call_llm(prompt, system_prompt=SYSTEM_PROMPT)
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

    # Issue #33: one core-holding cognitive-upgrade rotation query/day, appended
    # last so it only spends leftover Tavily budget (anomaly/geo/LLM queries above
    # take priority — this is a proactive fill-in, not a real signal yet).
    rotation_job = _rotation_search_job(today_et)
    if rotation_job:
        all_search_jobs.append(rotation_job)
        logger.info(f"Issue #33 rotation query: {rotation_job['_rotation_ticker']}")

    # 9. Layer 1 — Discovery: run all basic searches, accumulate raw results
    tavily_section = ""
    raw_results: list[dict] = []
    filtered: list[dict] = []        # populated in Layer 2b; needed for archive writer
    extract_results: list[dict] = [] # populated in Layer 3; needed for archive writer
    corroboration_keywords: list[str] = []  # populated below; needed for archive writer
    for job in all_search_jobs:
        if budget_remaining(budget) < 1:
            logger.info("Tavily budget exhausted, stopping search")
            break
        results = _do_search(
            job["query"], budget, serpapi_budget,
            days=job.get("days", query_days),
            search_depth="basic",
            max_results=job.get("max_results", 12),
            start_date=job.get("start_date", search_start),
            end_date=job.get("end_date", search_end),
        )
        raw_results.extend(results)

    if raw_results:
        # Layer 2a — Script pre-screen: score + deduplicate → top 15 candidates
        prescreened = score_and_filter(
            raw_results,
            anomaly_ticker_syms,
            wl["geo_keywords"],
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

        corroboration_keywords = build_keyword_set(anomaly_ticker_syms, wl["geo_keywords"])
        if extract_results:
            tavily_section = format_extract_results(
                extract_results, candidates=prescreened, extra_keywords=corroboration_keywords
            )
            logger.info(f"Using Extract chunks for Pass 2 ({len(extract_results)} sources)")
        elif filtered:
            tavily_section = format_tavily_results(filtered)
            logger.info(f"Extract unavailable, using search summaries ({len(filtered)} results)")

    # 9b. Archive cleaned Extract full text to local disk (outside Obsidian, never mined)
    write_extract_archive(
        today_et, run_slot, now_et, all_search_jobs, filtered, extract_results,
        extra_keywords=corroboration_keywords,
    )

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
            brave_news_section=brave_news_section,
            sonar_macro_section=sonar_macro_section,
            social_sentiment_section=social_sentiment_section,
            tavily_section=tavily_section,
            kb_section=kb_section,
            calibration_notes=calibration_notes,
            personal_context=personal_context,
            verifiable_signals_rule=VERIFIABLE_SIGNALS_INSTRUCTION_P2 if run_slot == "am" else "",
        )
        result2 = call_llm(prompt2, model=LLM_MODEL_PASS2, system_prompt=SYSTEM_PROMPT_P2)
        llm_meta_p2 = result2.get("_llm_meta", {})
        report_md = result2.get("report_md", report_md)
        write_sas_candidate_log(today_et, slot_label, result2.get("sas_candidates", []))

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

    # 10b. AM prediction calibration (PM slot only, issue #10) — see function
    # docstring for design notes. Fail-open: never raises.
    report_md = evaluate_am_calibration(
        today_et, run_slot, price_table, finnhub_news_section, sonar_macro_section, report_md
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
        brave_news_section, brave_budget,
        sonar_macro_section, polymarket_section, adanos_section, adanos_budget,
        reddit_section, apify_budget,
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
