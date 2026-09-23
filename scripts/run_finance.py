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

from fetch_prices import (
    fetch_prices, format_price_table, get_anomalies, fetch_52week_stats,
    multiday_return_pct,
)
from publication_window import _unexplained_publication_window
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
    TAVILY_DAILY_LIMIT, SERPAPI_MONTHLY_LIMIT, ADANOS_MONTHLY_LIMIT,
    APIFY_MONTHLY_LIMIT,
)
from intel_sources import (
    _sonar_macro_brief, _polymarket_brief, _adanos_x_sentiment,
    _reddit_sentiment_brief, fetch_liquidity_snapshot,
)
from report_writers import (
    write_context_log,
    _monthly_dedup,
    _mempalace_add_daily_drawer, write_report,
    send_telegram_report, send_telegram_alert,
    _fmt_llm_meta, finance_footer,
    REPORTS_DIR,
)
from recent_coverage import build_recent_coverage_section
from pass2_context import current_state, changed_background, read_previous_intel_snapshot_state
from intel_pass0 import build_intel_snapshot
from intel_collect import archive_intel_snapshot
from intel_deepen import deepen_intel_snapshot
from intel_render import (
    emergency_intel_snapshot, should_report, render_intel_snapshot_context,
    render_fallback_report, filter_social_lines,
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
import llm_config


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
# Model/provider selection per pipeline stage lives in llm_config.py.
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
    from intel_collect import parse_aliases
    return dict(
        stocks=stocks, commodities=commodities, fx=fx,
        geo_keywords=geo_keywords, thresholds=thresholds, recipients=recipients,
        entity_aliases=parse_aliases(text),
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


def serpapi_search(query: str, budget: dict,
                   start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    if not SERPAPI_API_KEY:
        return []
    if serpapi_remaining(budget) <= 0:
        logger.warning("SerpApi monthly budget exhausted")
        return []
    try:
        params = {"q": query, "api_key": SERPAPI_API_KEY, "num": 5, "engine": "google"}
        if start_date and end_date:
            params["tbs"] = f"cdr:1,cd_min:{start_date},cd_max:{end_date}"
        resp = _request_with_retry(httpx.get,
            "https://serpapi.com/search.json",
            params=params,
            timeout=15,
        )
        results = resp.json().get("organic_results", [])[:5]
        budget["used"] += 1
        save_serpapi_budget(budget)
        logger.info(f"SerpApi used [{budget['used']}/{SERPAPI_MONTHLY_LIMIT}]")
        return [{"title": r.get("title", ""), "url": r.get("link", ""), "content": r.get("snippet", "")} for r in results]
    except Exception as e:
        logger.warning(f"SerpApi error: {e}")
        return []


# ── Tavily search ────────────────────────────────────────────────────────────

def _request_with_retry(method, url: str, **kwargs):
    """Retry transient transport, 429, and 5xx failures before budget accounting."""
    for attempt in range(3):
        try:
            response = method(url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            transient = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in (429, 500, 502, 503, 504)
            if not transient or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))

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
        resp = _request_with_retry(httpx.post, "https://api.tavily.com/search", json=payload, timeout=30)
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
        resp = _request_with_retry(httpx.post,
            "https://api.tavily.com/extract",
            json={
                "api_key": TAVILY_API_KEY,
                "urls": batch,
                "query": query,
                "chunks_per_source": chunks_per_source,
            },
            timeout=45,
        )
        results = resp.json().get("results", [])
        budget["used"] += extract_cost
        save_budget(budget)
        logger.info(f"Tavily Extract [{budget['used']}/{TAVILY_DAILY_LIMIT}]: "
                    f"{n} URLs → {len(results)} extracted ({extract_cost}cr)")
        return results
    except Exception as e:
        logger.warning(f"Tavily extract failed (non-fatal): {e}")
        return []


# ── Personal context helpers (for Pass 2 injection) ──────────────────────────

_framework_cache: str = ""


def _load_framework() -> str:
    """Extract operative rules from Investment Operating Manual v1.0.md (issue #30).

    Pulls two sections verbatim from the canonical Obsidian manual so edits there
    propagate to Pass 2 without code changes: Section 6 (Portfolio Construction, incl. the
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
        ib_section = re.search(r"## IB（账户[^）]*）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return "\n".join(header)
        holdings = re.findall(
            r"### (\w+) \w+ \w+\n.*?均价：([\d.]+).*?浮盈：[^（]+（([+\-\d.]+%)）",
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
        ib_section = re.search(r"## IB（账户[^）]*）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return []
        tickers = re.findall(r"### (\w+) \w+ \w+", ib_section.group(1))
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
        ib_section = re.search(r"## IB（账户[^）]*）(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not ib_section:
            return {}
        holdings = re.findall(
            r"### (\w+) \w+ \w+\n.*?市值：([\d.]+)\s*浮盈",
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


def _compute_holding_signals(stats: dict | None = None) -> str:
    """Issue #33: pure-computation signals for Manual 7.4 (股价相对位置) and the
    position-size-overload reduce-trigger in Section 6 (position share > 15%) —
    code-computed ground truth, not LLM estimation from prose.
    Fail-open at every step; returns "" if nothing computable."""
    core_tickers = _get_core_holding_tickers()
    weights = _get_portfolio_weights()
    if not core_tickers and not weights:
        return ""
    if stats is None:
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


def _load_personal_context(background_signals: str | None = None,
                           holding_stats: dict | None = None) -> str:
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
    if background_signals is not None:
        signals = background_signals
    elif holding_stats is not None:
        signals = _compute_holding_signals(holding_stats)
    else:
        signals = _compute_holding_signals()
    if signals:
        parts.append(signals)
    fw = _load_framework()
    if fw:
        parts.append(fw)
    return "\n\n".join(parts)


# ── LLM call ─────────────────────────────────────────────────────────────────

# Pass 1 system prompt: lightweight, instructs structured JSON output + search task generation
# Pass 2 system prompt (Layer A): platform-generic analyst persona, Portfonia-reusable
_LAYER_A_PATH = OBSIDIAN / "Hermes/Daily Intelligence/Layer_A_Prompt.md"
_LAYER_A_FALLBACK = """你是一名专业财经情报分析师，服务于秉持价值投资、长期持仓、低交易频率理念的投资者。
分析原则：区分结构性催化剂与情绪性波动；只在出现可操作事实时给结论，没有就不写，不为凑结论而设价格触发线；明确区分事实与推测；不重复新闻原文。
输出语言：中文。所有时间推理以 America/New_York 为基准。"""


def _load_layer_a() -> str:
    """Read Layer A system prompt from Obsidian file (strips YAML frontmatter). Falls back to inline default."""
    try:
        if _LAYER_A_PATH.exists():
            raw = _LAYER_A_PATH.read_text(encoding="utf-8")
            # Strip YAML frontmatter (--- ... ---)
            text = re.sub(r"^---.*?---\s*", "", raw, flags=re.DOTALL).strip()
            if text:
                # The private Layer A still carries the old instruction. Replace
                # that one sentence at load time; leave the user's other rules intact.
                return text.replace(
                    '结论必须可操作：给出具体价格区间、触发条件或观察信号；不给模糊的"持续关注"',
                    '只在出现可操作事实时给结论；没有就不写，不为凑结论而设价格触发线',
                )
    except Exception as e:
        logger.warning(f"Layer A prompt load failed: {e}")
    return _LAYER_A_FALLBACK


SYSTEM_PROMPT_P2 = _load_layer_a() + (
    "\n能力圈外的变量只陈述事实，不推导操作。"
)

# AM-only instruction (issue #10): ask for a short list of falsifiable claims
# that the PM run can mechanically check against actual EOD data that evening.
# Kept out of the PM prompt — the point is to test the morning's calls against
# what actually happened, not to have every report predict itself.
VERIFIABLE_SIGNALS_INSTRUCTION_P2 = (
    "可验证信号（仅开盘前简报要求）：report_md 结尾追加\"## 可验证信号\"小节，2-4条，每条必须是可被今晚"
    "收盘后核验的具体条件-结果断言（价格阈值/事件是否发生），不写模糊定性描述——这些会在今晚 PM 报告生成前"
    "被核验，核验结果沉淀为知识库供未来报告参考"
)

# Pass 2 prompt template: free-form analysis with personal context (Layer B injected at call site)
USER_PROMPT_TEMPLATE_P2 = """今日日期（ET）：{date}
当前时间：{now_str}
{pm_afterhours_note}
## 价格数据（{price_data_label}）
{price_table}
{price_missing_note}
{intel_snapshot_section}
{sonar_macro_section}{social_sentiment_section}{liquidity_section}{kb_section}{calibration_notes}{recent_coverage_section}
## 实际持仓与框架
{personal_context}

根据所给事实直接分析事件、传导路径和持仓含义，不复述情报收集过程。对需要解释的价格异动，直接证据可写“已知原因”并附来源；仅有线索时写“线索待核实”，单一来源的强断言在相关句内标一次“未证实”；没有可靠线索时写“未找到原因”，只在这种情形下简短写明该标的来源覆盖。检索失败且无条目时写“未能完成检索”，不能声称已查遍。价格变化本身不是原因；不凭空归因于情绪、资金流或风格轮动，也不因没找到线索就断言没有公司级催化。只写新闻相对“此前已报道”及近五个交易日报告新增的事实；无进展时省略，或一句“延续 MM-DD 已报道的<事件>，今日无新进展”。不复述信源独立域名数量。

仓位建议仅在下列事实命中时提出，并指出具体新证据：认知提升（战略节点首次商业化、竞争格局结构变化、此前被怀疑的管理层承诺获证实）；Alpha 大幅兑现（预期差评分下降超过3分、未来 Alpha 潜力低于5分且无新催化）；更高赔率机会（候选潜力高2分以上且战略空间同量级）；价格被动上涨致单一仓位跨过15%。若注入了 FRED 档位变化或52周新高/新低，也允许写仓位小节，但只陈述本次背景变化，不把它当成单独的交易指令。上述事实或背景变化均未出现时省略仓位小节，不逐股声明“无加减仓依据”，不复述标准原文。

输出骨架（空节省略）：
# [Daily_Intel] {date} 开盘前简报
## 要点（可选，最多3条）
## 持仓与观察标的
## 宏观与地缘（只写对持仓有传导的新事实）
## 仓位（仅出现上述例外时）
{verifiable_signals_rule}

正文按主题写自然段：一个主题一个段落，信息充足时每段约150–200字，段落之间空一行。不要把一段拆成每行一二十字的短句，也不要用连续的项目符号代替分析。标题、可选的最多3条要点和开盘前简报末尾的可验证信号清单可保留其既定格式；事实不足时写短或省略，不为凑字数补空话。

这是一份私人投资分析。来源和检索范围只用于内部判断，不成为正文的叙述对象；每个主题直接说明事实、投资含义和下一观察点，不写检索步骤、证据缺口清单或自我免责。只有会改变判断的关键不确定性才简短说明一次。除异动原因未找到或检索失败需要交代范围的情形，正文不出现后台数据结构名或检索范围术语。

有话则长，无话则短；不要套话或凭空设价格触发线。直接输出 Markdown 正文，不要 JSON、代码围栏或附言。
"""

# SAS候选证据提取：独立于 Pass 2 report_md 的第二次调用（issue #60）。原先与 report_md 共享
# 同一个 JSON 信封——report_md 是全项目里最大的单次 payload，也是两个真实撞过 finish_reason=
# length 的 stage 之一（issue #59），JSON 包裹意味着截断发生在字符串中途会把已经写好的大半份
# 报告一并作废。拆开后 report_md 直接输出纯文本（见上方模板），sas_candidates 改为单独一次
# gemma-4-31b-it 调用（llm_config "sas_candidate_extract" stage），复用 Pass 2 已经组装好的
# 同一份上下文（价格表/新闻/持仓），零额外抓取成本。
SAS_CANDIDATE_PROMPT_TEMPLATE = """今日日期（ET）：{date}
{pm_afterhours_note}
## 价格数据（{price_data_label}）
{price_table}
{price_missing_note}
{intel_snapshot_section}
== 持仓 ==
{personal_context}

## 任务
你是 SAS 候选证据提取器：识别命中以下信号类别、且必须涉及"IB美股持仓"中实际持有标的的条目，
不涉及实际持仓的标的（哪怕新闻本身很重大，如仅为 watchlist 观察标的）一律不输出：
- 内部人在公开市场的自主买入（注意区分主动买入 vs 预设计划卖出/税务规划性质的交易）
- 资本配置方向的持续性（R&D/Capex占收入比在下跌期是否维持或提升）
- 生态位置的第三方验证（其他公司/客户选择在其平台上构建）
- 监管文件语言的季度间变化
- 公司自身"市场曾怀疑后被证实"的历史先例
- 企业解锁了一个之前不确定的战略节点（如产品从内测进入商业化、新市场首次产生可计量收入）
- 竞争格局出现了有利于企业的结构性变化（如主要竞争对手退出、监管为企业构建护城河）
- 管理层兑现了此前市场明确怀疑的具体承诺（仅"超预期"不够，必须是此前被质疑的具体目标/节点
  被证实）

命中的追加一条记录，含 ticker/category/fact 三个字段；category 只能取以下字符串之一：
"内部人增持"/"资本配置持续性"/"生态位验证"/"监管语言变化"/"历史先例"/
"认知提升-战略节点解锁"/"认知提升-竞争格局变化"/"认知提升-管理层兑现承诺"。
fact 为一句话事实摘要（含关键数字/来源，不超过80字）。宁可漏判也不要把"普通业绩超预期/
普通上涨"当成命中——这只是记录候选证据供未来复审，本身不代表应该操作。

请输出以下JSON（不要附加任何其他文字）：
{{"sas_candidates": [{{"ticker": "...", "category": "...", "fact": "..."}}]}}
无命中时 sas_candidates 留空数组，但仍必须保留外层 {{"sas_candidates": [...]}} 对象结构，
不要只输出裸数组 []。
"""





# ── TG-only run status message ──────────────────────────────────────────────

def build_status_message(today_et: str, slot_label: str, budget: dict,
                         serpapi_budget: dict, tavily_used_before: int,
                         serpapi_used_before: int, intel_snapshot: dict,
                         sonar_macro_section: str, polymarket_section: str,
                         adanos_section: str, adanos_budget: dict,
                         reddit_section: str, apify_budget: dict,
                         llm_meta_p2: dict) -> str:
    """TG-only status aligned with intelligence snapshot coverage and code-only deepening."""
    lines = [f"**Daily_Intel 运行状态** · {today_et} {slot_label}", "",
             f"Tavily今日剩余: {budget_remaining(budget)}/{TAVILY_DAILY_LIMIT}（本次用 {budget['used'] - tavily_used_before}）"]
    serpapi_used_run = serpapi_budget["used"] - serpapi_used_before
    if serpapi_used_run:
        lines.append(f"SerpApi本月已用: {serpapi_budget['used']}/{SERPAPI_MONTHLY_LIMIT}（本次用 {serpapi_used_run}）")
    entities = intel_snapshot.get("entities", [])
    totals = {key: sum((e.get("coverage") or {}).get(key, 0) for e in entities)
              for key in ("finnhub", "google_news", "rss", "guardian")}
    errors = list(dict.fromkeys(err for e in entities for err in (e.get("coverage") or {}).get("errors", [])))
    lines += ["", "情报来源:",
              f"- Pass 0: {len(entities)} 标的；Finnhub {totals['finnhub']}、Google News {totals['google_news']}、RSS {totals['rss']}、Guardian {totals['guardian']}",
              f"- 来源错误: {'；'.join(errors[:5]) if errors else '无'}",
              f"- Pass 1（代码）: 搜索 {intel_snapshot.get('search_count', 0)}，Extract {intel_snapshot.get('extract_success_count', 0)}/{intel_snapshot.get('extract_url_count', 0)} URL"]
    for ticker, status in (intel_snapshot.get("deepen_status") or {}).items():
        lines.append(f"  {ticker}: {status}")
    lines.append(f"- Sonar宏观快照: {'成功' if sonar_macro_section else '失败/跳过'}")
    lines.append(f"- Polymarket: {'成功' if polymarket_section else '无相关市场/跳过'}")
    if ADANOS_API_KEY:
        lines.append(f"- Adanos: {'成功' if adanos_section else '无数据/跳过'}（本月 {adanos_budget['used']}/{ADANOS_MONTHLY_LIMIT}）")
    if APIFY_API_TOKEN:
        lines.append(f"- Reddit: {'成功' if reddit_section else '无数据/跳过'}（本月 {apify_budget['used']}/{APIFY_MONTHLY_LIMIT}）")
    lines += ["", "LLM/Provider:"]
    if sonar_macro_section:
        lines.append(f"- 宏观快照（{llm_config.model('macro_brief')}）: OR")
    lines.append(f"- Pass 2（{llm_config.model('report_pass2')}）: {_fmt_llm_meta(llm_meta_p2)}")
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
        return serpapi_search(query, serpapi_budget, start_date, end_date)
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


# Issue #80. Commodities, FX, and index ETFs never reached 15%/20% in the
# 9-month backtest. AAOI is a watchlist observer, not an IB holding, and
# its own 3-day >=15% rate was 51/186 — same thresholds would fire most weeks.
# Same-day anomaly search still covers AAOI.
_EXCLUDED_UNEXPLAINED_MOVE_TICKERS = frozenset({
    "GC=F", "CL=F", "^TNX",
    "USDCNY=X", "USDJPY=X", "DX-Y.NYB",
    "QQQM", "VOO", "EWJ",
    "AAOI",
})


def _closes_before_report(series, report_date):
    """Daily closes strictly before report_date. Drops a stale 'today' bar."""
    import pandas as pd
    s = series.dropna()
    if len(s) == 0:
        return s
    if getattr(s.index, "tz", None) is not None:
        s = s.copy()
        s.index = s.index.tz_convert("America/New_York").tz_localize(None)
    report_ts = pd.Timestamp(report_date)
    return s[s.index.normalize() < report_ts]


def _compute_multiday_moves(
    price_rows: list,
    closes_daily: dict | None = None,
    report_date=None,
    slot: str = "pm",
) -> dict[str, tuple[float | None, float | None]]:
    """{ticker: (pct_3d, pct_5d)} for names this mechanism is allowed to chase.

    None means that window does not have enough prior sessions. Do not fill
    it from week_change_pct: that field still falls back to the oldest close
    for the price table. No persisted 'already explained' flag.
    """
    out: dict[str, tuple[float | None, float | None]] = {}
    for r in price_rows:
        if r.ticker in _EXCLUDED_UNEXPLAINED_MOVE_TICKERS:
            continue
        if closes_daily and report_date is not None and r.ticker in closes_daily:
            pre = _closes_before_report(closes_daily[r.ticker], report_date)
            if slot == "am":
                if len(pre) < 2:
                    continue
                numerator = float(pre.iloc[-1])
                before = pre.iloc[:-1]
            else:
                numerator = float(r.price)
                before = pre
            pct_3d = multiday_return_pct(numerator, before, 3, allow_short=False)
            pct_5d = multiday_return_pct(numerator, before, 5, allow_short=False)
        else:
            pct_3d = getattr(r, "change_3d_pct", None)
            pct_5d = getattr(r, "change_5d_pct", None)
            pct_3d = None if pct_3d is None else float(pct_3d)
            pct_5d = None if pct_5d is None else float(pct_5d)
        out[r.ticker] = (pct_3d, pct_5d)
    return out


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
        f"请在【持仓与观察标的】中依据所给事实分别说明日内表现与盘后延续/反转情况，"
        f"无盘后数据时注明\"盘后无成交\"。"
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

    # 5. The free-source intelligence snapshot is now the only article collector. It replaces
    # duplicate RSS/Finnhub/Brave reads and the LLM-generated Pass 1 draft.
    multiday_moves = _compute_multiday_moves(price_rows, slot=run_slot)
    windows = {}
    for ticker, (d3, d5) in multiday_moves.items():
        days = 5 if d5 is not None and abs(d5) >= 20 else (3 if d3 is not None and abs(d3) >= 15 else 0)
        if days:
            windows[ticker] = _unexplained_publication_window(today_et, days, run_slot)[0]
    try:
        intel_snapshot, _ = build_intel_snapshot(
            wl, now_et, run_slot, price_rows=price_rows, multiday_moves=multiday_moves,
            window_starts=windows, held=set(_get_core_holding_tickers()),
            weights=_get_portfolio_weights(), archive=False,
        )
    except Exception as exc:
        logger.warning("Pass 0 intelligence snapshot collection failed: %s", exc)
        intel_snapshot = emergency_intel_snapshot(today_et, run_slot, now_et, price_rows, wl, multiday_moves,
                                  f"collector: {type(exc).__name__}",
                                  held=set(_get_core_holding_tickers()), weights=_get_portfolio_weights(),
                                  window_starts=windows)
    if not should_report(intel_snapshot):
        logger.info("No entity move, company item, or macro item; skipping")
        return
    anomaly_ticker_syms = [r.ticker for r in anomalies]
    kb_context = get_finance_context(
        anomaly_tickers=anomaly_ticker_syms,
        geo_topics=list(wl["geo_keywords"].keys()),
    )
    kb_section = f"\n## 个人知识库上下文\n{kb_context}\n" if kb_context else ""

    # Sonar, social, and FRED collection stay in place; only their injection
    # is scoped to entities in the intelligence snapshot and changed background states.
    sonar_macro_section = _sonar_macro_brief(
        slot=run_slot, stocks=wl["stocks"], commodities=wl["commodities"],
        fx=wl["fx"], geo_topics=list(wl["geo_keywords"].keys()), now_et=now_et,
        portfolio_snapshot=kb_context[:400] if kb_context else "", price_table=price_table,
    )
    polymarket_section = ""
    adanos_section = ""
    adanos_budget = {"year_month": "", "used": 0}
    reddit_section = ""
    apify_budget = {"year_month": "", "used": 0}
    try:
        adanos_budget = load_adanos_budget()
        polymarket_section = _polymarket_brief(list(wl["geo_keywords"].keys()))
        social_tickers = list(dict.fromkeys(
            anomaly_ticker_syms + [t for t in wl["stocks"] if t not in anomaly_ticker_syms]
        ))[:4]
        adanos_section = _adanos_x_sentiment(social_tickers, adanos_budget)
        save_adanos_budget(adanos_budget)
        apify_budget = load_apify_budget()
        reddit_section = _reddit_sentiment_brief(social_tickers, apify_budget)
        save_apify_budget(apify_budget)
    except Exception as exc:
        logger.warning("Social sentiment step failed: %s", exc)
    liquidity_section = ""
    try:
        liquidity_section = fetch_liquidity_snapshot()
    except Exception as exc:
        logger.warning("Liquidity snapshot step failed: %s", exc)

    # 6. Code-only Pass 1. Search and Extract budgets are enforced both by the
    # planner and the existing HTTP budget helpers; no LLM call occurs here.
    try:
        intel_snapshot = deepen_intel_snapshot(
            intel_snapshot,
            search=lambda query, start, end: _do_search(
                query, budget, serpapi_budget, search_depth="basic", max_results=2,
                start_date=start, end_date=end,
            ),
            extract=lambda urls, query: tavily_extract(urls, query, budget),
            remaining=lambda: budget_remaining(budget),
        )
    except Exception as exc:
        logger.warning("Pass 1 deepening failed, keeping free-source intelligence snapshot: %s", exc)
        intel_snapshot["deepen_status"] = {"error": type(exc).__name__}
    previous_context_state = read_previous_intel_snapshot_state(_PROJ_DIR / "archives", now_et)
    try:
        archive_intel_snapshot(intel_snapshot)
    except OSError as exc:
        logger.warning("Intelligence snapshot archive failed: %s", exc)
    intel_snapshot_section = render_intel_snapshot_context(intel_snapshot, wl["geo_keywords"])
    social_section = filter_social_lines(
        polymarket_section + adanos_section + reddit_section, intel_snapshot["entities"]
    )

    # 7. Pass 2 runs even when no paid search result exists. A failed or empty
    # completion still produces a deterministic report and a Telegram alert.
    coverage_tickers = [e["ticker"] for e in intel_snapshot["entities"]
                        if set((e.get("move") or {}).get("flags", [])) & {"anomaly", "d3", "d5"}]
    try:
        recent_coverage_section = build_recent_coverage_section(
            REPORTS_DIR, today_et, run_slot, coverage_tickers, wl["entity_aliases"]
        )
    except (OSError, ValueError) as exc:
        logger.warning("Recent report coverage unavailable: %s", exc)
        recent_coverage_section = ""
    core_tickers = _get_core_holding_tickers()
    stats = fetch_52week_stats(core_tickers) if core_tickers else {}
    context_state = current_state(liquidity_section, _get_portfolio_weights(), stats,
                                  previous_context_state)
    changed_liquidity, holding_background = changed_background(
        liquidity_section, context_state, previous_context_state
    )
    personal_context = _load_personal_context(background_signals=holding_background)
    sas_personal_context = _load_personal_context(holding_stats=stats)
    calibration_notes = _load_recent_calibration_notes() if run_slot == "am" else ""
    prompt2 = USER_PROMPT_TEMPLATE_P2.format(
        date=today_et, now_str=now_et.strftime("%Y-%m-%d %H:%M %Z"),
        pm_afterhours_note=pm_afterhours_note, price_data_label=price_data_label,
        price_table=price_table, price_missing_note=price_missing_note,
        intel_snapshot_section=intel_snapshot_section, sonar_macro_section=sonar_macro_section,
        social_sentiment_section=social_section, liquidity_section=changed_liquidity,
        kb_section=kb_section, calibration_notes=calibration_notes,
        recent_coverage_section=recent_coverage_section,
        personal_context=personal_context,
        verifiable_signals_rule=VERIFIABLE_SIGNALS_INSTRUCTION_P2 if run_slot == "am" else "",
    )
    try:
        result2 = call_llm(prompt2, stage="report_pass2", system_prompt=SYSTEM_PROMPT_P2,
                           parse_json=False)
    except Exception as exc:
        logger.warning("Pass 2 failed, using intelligence snapshot summary: %s", exc)
        result2 = {}
    if not isinstance(result2, dict):
        result2 = {}
    llm_meta_p2 = result2.get("_llm_meta", {})
    raw_report = result2.get("text")
    report_md = raw_report.strip() if isinstance(raw_report, str) else ""
    pass2_succeeded = bool(report_md)
    if not report_md:
        report_md = render_fallback_report(intel_snapshot, slot_label)
        send_telegram_alert(f"[!] Daily_Intel {today_et} {slot_label} Pass 2 失败；已发送代码生成的情报摘要。")

    # Independent SAS extraction retains its JSON output schema, now sourced
    # from the same intelligence snapshot rather than the removed discovery pool.
    try:
        sas_prompt = SAS_CANDIDATE_PROMPT_TEMPLATE.format(
            date=today_et, pm_afterhours_note=pm_afterhours_note,
            price_data_label=price_data_label, price_table=price_table,
            price_missing_note=price_missing_note, intel_snapshot_section=intel_snapshot_section,
            personal_context=sas_personal_context,
        )
        sas_result = call_llm(sas_prompt, stage="sas_candidate_extract",
                              system_prompt="你是一名严格遵循规则的候选证据提取器，只输出JSON，不输出任何其他文字。")
        write_sas_candidate_log(today_et, slot_label, sas_result.get("sas_candidates", []))
    except Exception as exc:
        logger.warning("SAS candidate extraction failed (non-fatal): %s", exc)

    if run_slot == "pm":
        report_md = re.sub(r"^# \[Daily_Intel\] .+", f"# [Daily_Intel] {today_et} {slot_label}",
                           report_md, flags=re.MULTILINE)
    report_md = evaluate_am_calibration(
        today_et, run_slot, price_table, intel_snapshot_section, sonar_macro_section, report_md
    )
    write_report(today_et, slot_label, report_md, budget)
    if pass2_succeeded:
        intel_snapshot["context_state"] = context_state
        try:
            archive_intel_snapshot(intel_snapshot)
        except OSError as exc:
            logger.warning("Intelligence snapshot context state save failed: %s", exc)
    _mempalace_add_daily_drawer(today_et, run_slot, report_md)
    write_context_log(today_et, slot_label, now_et, price_table, [],
                      (intel_snapshot.get("macro_digest") or {}).get("geo_topics_hit", []),
                      sonar_macro_section, intel_snapshot.get("search_jobs", []), intel_snapshot=intel_snapshot)

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
        intel_snapshot,
        sonar_macro_section, polymarket_section, adanos_section, adanos_budget,
        reddit_section, apify_budget,
        llm_meta_p2,
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
