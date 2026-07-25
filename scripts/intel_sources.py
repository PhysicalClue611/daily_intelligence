"""
Daily Intelligence — external intelligence source briefs
==========================================================
Seven "fetch a brief from an external source" functions extracted from
run_finance.py (issue #42, 2026-07-17) to shrink that file: Sonar macro
brief, Polymarket, Adanos X/Twitter sentiment, Reddit sentiment (Apify),
FRED liquidity snapshot, Finnhub news, Brave News.

Leaf module: does not import from run_finance.py. fetch_brave_news()'s dedup
helpers (_title_tokens_for_dedup / _token_jaccard /
_TITLE_DEDUP_THRESHOLD_NO_KEYWORD) come from scoring_utils.py, a shared leaf
module — not a deferred `from run_finance import ...` inside the function
body (the original approach here, before PR #43 review feedback pointed out
it silently assumed run_finance.py is registered in sys.modules as
"run_finance", which is false when it's run directly as the entrypoint, as
launchd does — Python registers it as "__main__" instead).
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from budget_trackers import (
    adanos_remaining, apify_remaining, brave_remaining,
    save_brave_budget, BRAVE_MONTHLY_LIMIT,
)
from scoring_utils import (
    _title_tokens_for_dedup, _token_jaccard, _TITLE_DEDUP_THRESHOLD_NO_KEYWORD,
)
import llm_config

_PROJ_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE_URL         = "https://openrouter.ai/api/v1/chat/completions"
OR_ATTRIBUTION_HEADERS = {"HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence", "X-OpenRouter-Title": "DailyIntel"}
# Sonar model/provider/token settings: llm_config stage "macro_brief" (issue #11).

POLYMARKET_BASE = "https://gamma-api.polymarket.com"
ADANOS_API_KEY  = os.getenv("ADANOS_API_KEY", "")
ADANOS_BASE     = "https://api.adanos.org"

APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN", "")
APIFY_BASE         = "https://api.apify.com/v2"
APIFY_REDDIT_ACTOR = "benthepythondev~stock-sentiment-intelligence"

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE    = "https://finnhub.io/api/v1"

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_BASE    = "https://api.search.brave.com/res/v1/news/search"

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_field(value, max_len: int = 80) -> str:
    """Collapse whitespace/newlines, strip other control characters, and
    truncate an untrusted third-party field before it's interpolated into
    markdown that gets injected straight into the Pass 1/2 prompt (issue #38).
    Polymarket/Adanos/Apify are unvalidated third-party responses — an
    embedded newline in a returned value could forge a fake `##` section
    boundary and mislead the LLM about where a section ends; other control
    bytes are stripped too since they serve no legitimate purpose in a
    one-line sentiment field. Input is bounded before the regex passes run,
    so a pathological multi-MB field doesn't get fully processed pre-cap.
    """
    raw = str(value)[: max_len * 4]
    collapsed = re.sub(r"\s+", " ", raw).strip()
    return _CONTROL_CHARS_RE.sub("", collapsed)[:max_len]


def _sanitize_ticker(value, allowed: set[str] | None = None) -> str | None:
    """Whitelist a ticker symbol echoed back from an untrusted third-party
    response (issue #38's echo-back is Apify's Reddit sentiment actor, which
    returns `ticker` per row rather than us controlling it directly). Returns
    None — caller should skip the row — if it doesn't look like a real ticker,
    or (when `allowed` is given) if it wasn't one of the tickers we actually
    requested — a well-formed but unsolicited symbol (e.g. an actor echoing
    back `AAPL` when we only asked about `NVDA`) is still not something we
    want rendered into the prompt as if we'd asked for it.

    Non-`str` input (None, bool, dict, ...) is rejected before stringifying —
    `str(None).upper()` is `"NONE"`, which *matches* the ticker shape regex,
    so validating type first is required, not cosmetic.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if not _TICKER_RE.match(v):
        return None
    if allowed is not None and v not in allowed:
        return None
    return v


def _sonar_macro_brief(
    slot: str,
    stocks: list[str],
    commodities: list[str],
    fx: list[str],
    geo_topics: list[str],
    now_et: "datetime",
    portfolio_snapshot: str = "",
    price_table: str = "",
) -> str:
    """Call Perplexity Sonar for a real-time macro intelligence snapshot.

    Query is built dynamically from the current watchlist so it evolves as holdings change.
    Returns a formatted section string for LLM prompt injection. Fail-open (returns "" on error).
    Cost: ~$0.005 (Sonar fixed search fee, billed via OpenRouter).

    Staleness hardening (2026-07-02): Sonar has synthesized macro claims that
    contradicted same-day live price data (e.g. reporting WTI "breaking above
    $100" when the actual price was ~$68) — it's a search+synthesis model, not
    a quote feed, and can surface or blend in older/less-relevant source
    material. Three mitigations: (1) `search_recency_filter: "day"` restricts
    Perplexity's underlying search to sources published in the last 24h
    (confirmed passed through by OpenRouter, not silently dropped); (2) the
    live price_table already computed earlier in the pipeline is injected as
    ground truth the model must defer to on conflict; (3) the prompt requires
    a per-claim timestamp and explicit "no update" instead of stale-but-undated
    claims.
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
        f"Be specific. No generic commentary. "
        f"For every specific number or claim (price, rate, event), state its source's "
        f"publish date/time. If you cannot find anything from the last 24 hours on a "
        f"topic, say so explicitly ('no update in past 24h') instead of reporting an "
        f"undated or older claim as if it were current."
    )

    system = (
        f"You are a macro intelligence analyst serving a personal investor. "
        f"Today is {now_str}. Reason in NYSE Eastern Time. "
        f"Focus on actionable, time-stamped developments — every factual claim must carry "
        f"a publish date/time so staleness is visible, not implied. "
        f"If portfolio snapshot is provided, tailor analysis to those holdings.\n"
        + (f"Portfolio: {portfolio_snapshot}\n" if portfolio_snapshot else "")
        + (
            f"\nLive price data as of {now_str} (authoritative — this is a direct feed, "
            f"not a search result): if any search-derived claim in your answer conflicts "
            f"with these numbers, defer to these numbers and flag the conflict explicitly "
            f"rather than presenting the older search claim as current.\n{price_table}\n"
            if price_table else ""
        )
    )

    cfg = llm_config.stage("macro_brief")
    or_payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": query},
        ],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        **({"provider": cfg["providers"]} if cfg["providers"] else {}),
        "search_recency_filter": "day",
    }
    slot_label = "开盘前" if slot == "am" else "夜盘"
    for attempt in range(2):
        try:
            resp = httpx.post(
                OR_BASE_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json", **OR_ATTRIBUTION_HEADERS},
                json=or_payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            brief = choice["message"]["content"].strip()
            usage = data.get("usage", {})
            finish_reason = choice.get("finish_reason")
            logger.info(f"Sonar macro brief ({slot_label}): {len(brief)} chars "
                        f"prompt_tokens={usage.get('prompt_tokens')} "
                        f"completion_tokens={usage.get('completion_tokens')} "
                        f"finish_reason={finish_reason} provider={data.get('provider', 'n/a')}")
            if finish_reason == "length":
                logger.warning(f"Sonar macro brief ({slot_label}): truncated by max_tokens "
                                f"(finish_reason=length) — content may be cut off before "
                                f"staleness disclaimer/timestamp requirements were satisfied")
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
                    question = _sanitize_field(m.get("question") or m.get("title") or event_title or "", max_len=120)
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
                        pairs = ", ".join(f"{_sanitize_field(o, max_len=20)}:{float(p):.0%}" for o, p in zip(outcomes, prices))
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
            parts = [f"{k}={_sanitize_field(v)}" for k, v in
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


def _reddit_sentiment_brief(tickers: list[str], budget: dict) -> str:
    """Query Apify's Stock Sentiment Intelligence actor (benthepythondev/stock-sentiment-intelligence)
    for Reddit stock sentiment (WSB/r-stocks/r-investing mentions, momentum, BULLISH/BEARISH/NEUTRAL
    signal) on watchlist tickers — issue #17 step 3. Field names below are real, verified against a
    live 1-ticker call on 2026-07-16 (not guessed, unlike Adanos's schema — see its docstring).
    Batches all tickers into a single actor run to minimize the per-run start fee. Fail-open: returns
    "" if no token configured, budget exhausted, or the API errors.

    Budget (real pay-per-event money — issue #36):
      - +1 after a successful HTTP response (2xx + body received), including empty/malformed
        payloads (run was still started and may have been billed).
      - +1 on httpx.TimeoutException after the POST was sent (client stays at 90s wall clock;
        Apify sync may still complete and bill server-side — hard cap must not under-count).
      - no +1 on ConnectError (request never left / never reached Apify).
      - no +1 on other pre-response failures (unlike Adanos free-tier, which counts first).

    Parsing (issue #37): mention_change_pct is coerced per item via float(); a dirty value
    only drops that field, never the whole multi-ticker batch.
    """
    if not tickers or not APIFY_API_TOKEN:
        return ""
    if apify_remaining(budget) <= 0:
        logger.info("Apify monthly budget exhausted, skipping Reddit sentiment")
        return ""
    requested = {t.strip().upper() for t in tickers}
    try:
        resp = httpx.post(
            f"{APIFY_BASE}/acts/{APIFY_REDDIT_ACTOR}/run-sync-get-dataset-items",
            headers={"Authorization": f"Bearer {APIFY_API_TOKEN}", "Content-Type": "application/json"},
            json={"mode": "specific_tickers", "tickers": tickers, "maxResults": len(tickers)},
            timeout=90,  # wall-clock cap for main pipeline; not Apify's 300s sync ceiling
        )
        resp.raise_for_status()
        items = resp.json()
        budget["used"] = budget.get("used", 0) + 1
        logger.debug(f"Apify Reddit sentiment raw response: {json.dumps(items)[:2000]}")
        # Parsing stays inside this try so a malformed payload (e.g. Apify returning a
        # non-list error object instead of the dataset) still hits the except below and
        # returns "" — if this loop threw uncaught, the caller's outer try/except in
        # main() would catch it too, but *before* save_apify_budget() runs, silently
        # losing the budget["used"] increment for a call that was already paid for.
        if not isinstance(items, list):
            logger.warning(f"Apify Reddit sentiment: unexpected response shape {type(items)}, expected list")
            return ""
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = _sanitize_ticker(item.get("ticker"), allowed=requested)
            if not ticker:
                continue
            change = item.get("mention_change_pct")
            mention_chg = None
            if change is not None:
                try:
                    mention_chg = f"{float(change):+.0f}%"
                except (TypeError, ValueError):
                    # Dirty actor field: omit mention_chg only; keep other fields / other tickers
                    # (issue #37 — previously f"{change:+.0f}%" raised and wiped the whole batch).
                    pass
            parts = [f"{k}={_sanitize_field(v)}" for k, v in
                     [("signal", item.get("sentiment_signal")),
                      ("mentions_24h", item.get("mentions_24h")),
                      ("mention_chg", mention_chg),
                      ("rank_chg", item.get("rank_change"))]
                     if v is not None]
            if parts:
                lines.append(f"- {ticker}: {', '.join(parts)}")
    except httpx.TimeoutException as e:
        # Request was sent; Apify may still bill. Count conservatively (issue #36).
        budget["used"] = budget.get("used", 0) + 1
        logger.warning(
            f"Apify Reddit sentiment timed out after 90s, counting budget conservatively: {e}"
        )
        return ""
    except httpx.ConnectError as e:
        logger.warning(f"Apify Reddit sentiment connect failed (not counting budget): {e}")
        return ""
    except Exception as e:
        logger.warning(f"Apify Reddit sentiment query failed: {e}")
        return ""
    if not lines:
        return ""
    return "\n## Reddit 舆情快照（Apify）\n" + "\n".join(lines) + "\n"


def _fred_latest(series_id: str, limit: int = 1) -> list[tuple[str, float]]:
    """Fetch the latest N observations for a FRED series, skipping missing
    values ('.'). Returns [(date, value), ...] newest first. Fail-open:
    returns [] on any error or if FRED_API_KEY isn't configured."""
    if not FRED_API_KEY:
        return []
    try:
        resp = httpx.get(FRED_BASE, params={
            "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
            "sort_order": "desc", "limit": limit + 5,  # pad for missing/holiday gaps
        }, timeout=15)
        resp.raise_for_status()
        result = []
        for o in resp.json().get("observations", []):
            v = o.get("value", ".")
            if v == ".":
                continue
            result.append((o["date"], float(v)))
            if len(result) >= limit:
                break
        return result
    except Exception as e:
        logger.warning(f"FRED fetch failed for {series_id}: {e}")
        return []


def fetch_liquidity_snapshot() -> str:
    """Fed liquidity plumbing snapshot — supports the tiered framework in
    Hermes/Daily Intelligence/市场见顶预警指标.md (2026-07-02). Classifies
    each series into 正常/观察/警戒. Reference only, not a liquidation
    trigger — see that doc for the reasoning and asset-specific guidance.

    Thresholds need periodic recalibration (reserve "ampleness" is relative
    to bank balance sheet size, which grows over time — a fixed dollar
    floor will drift stale; revisit yearly, see doc §一).

    SRF usage has no clean free FRED series and isn't automated here —
    stays a manual/qualitative check per the doc. Fail-open throughout.
    """
    if not FRED_API_KEY:
        return ""
    lines: list[str] = []
    tiers: list[str] = []

    reserves = _fred_latest("WRESBAL")
    if reserves:
        date, val = reserves[0]
        t = val / 1_000_000  # millions -> trillions
        tier = "警戒" if t < 2.5 else ("观察" if t < 2.9 else "正常")
        lines.append(f"- 银行准备金余额（{date}）：${t:.2f}万亿 [{tier}]")
        tiers.append(tier)

    sofr, rrp = _fred_latest("SOFR"), _fred_latest("RRPONTSYAWARD")
    if sofr and rrp:
        sdate, sval = sofr[0]
        _, rval = rrp[0]
        spread_bp = (sval - rval) * 100
        tier = "警戒" if spread_bp > 3 else ("观察" if spread_bp > 0 else "正常")
        lines.append(f"- SOFR-RRP利差（{sdate}）：{spread_bp:.1f}bp（SOFR {sval:.2f}% / RRP {rval:.2f}%）[{tier}]")
        tiers.append(tier)

    tga = _fred_latest("WTREGEN")
    if tga:
        date, val = tga[0]
        t = val / 1_000_000
        tier = "观察" if t > 0.9 else "正常"
        lines.append(f"- TGA账户余额（{date}）：${t:.2f}万亿 [{tier}]")
        tiers.append(tier)

    if not lines:
        return ""
    overall = "警戒" if "警戒" in tiers else ("观察" if "观察" in tiers else "正常")
    logger.info(f"FRED liquidity snapshot: overall={overall}")
    return (
        f"\n## 流动性水位快照（FRED，整体：{overall}）\n"
        + "\n".join(lines)
        + "\n（仅供参考背景，非清仓触发；详见 Hermes/Daily Intelligence/市场见顶预警指标.md）\n"
    )


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


def fetch_brave_news(tickers: list[str], geo_topics: list[str], budget: dict,
                      max_queries: int = 4) -> str:
    """Brave News API (issue #14): independent Western search engine (not a Google
    proxy). Query-based, so one call per ticker/topic rather than a bulk feed like
    RSS/Guardian. Hard budget-capped (see BRAVE_MONTHLY_LIMIT) since Brave dropped
    its free tier in 2026 and bills a card once the $5 prepaid credit runs out —
    fail-closed on budget exhaustion, never calls past the cap. Fail-open otherwise.
    """
    if not tickers or not BRAVE_API_KEY:
        return ""
    # Reserve a slot for the geo-topics query up front — slicing tickers to
    # max_queries and appending the geo query afterward (then re-truncating to
    # max_queries) silently dropped the geo query whenever ticker count alone
    # already filled the quota (true for both the 8-ticker AM default and any
    # PM run with >=4 movers), defeating half this function's purpose.
    if geo_topics:
        ticker_slots = max(0, max_queries - 1)
        queries = [f"{t} stock news" for t in tickers[:ticker_slots]]
        queries.append(" ".join(geo_topics[:3]) + " market impact")
    else:
        queries = [f"{t} stock news" for t in tickers[:max_queries]]

    items: list[tuple[str, str, frozenset]] = []  # (page_age iso str for sort, formatted line, tokens)
    seen_titles: set[str] = set()
    for i, query in enumerate(queries):
        if brave_remaining(budget) <= 0:
            logger.info("Brave monthly budget exhausted, skipping remaining queries")
            break
        try:
            resp = httpx.get(
                BRAVE_BASE,
                headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
                params={"q": query, "freshness": "pd", "count": 8},
                timeout=12,
            )
            resp.raise_for_status()
            # Only count the call against the hard cap once it's confirmed
            # successful — an expired key or a 429/5xx used to still burn
            # budget for zero usable results, silently exhausting the cap
            # meant to prevent unattended overage charges.
            budget["used"] = budget.get("used", 0) + 1
            for r in (resp.json().get("results") or [])[:8]:
                title = (r.get("title") or "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                tokens = _title_tokens_for_dedup(title)
                # Brave results never flow through score_and_filter()'s
                # cross-source dedup (that only sees Tavily raw_results), so
                # apply the same token-overlap check here — otherwise
                # paraphrased duplicates across Brave's own queries (the
                # exact failure mode this feature targets) go uncaught.
                if any(_token_jaccard(tokens, kept_tok) >= _TITLE_DEDUP_THRESHOLD_NO_KEYWORD
                       for _, _, kept_tok in items):
                    continue
                page_age = r.get("page_age") or ""
                age_label = r.get("age") or page_age or "?"
                source = (r.get("profile") or {}).get("name") or (r.get("meta_url") or {}).get("hostname") or "Brave"
                desc = (r.get("description") or "")[:100]
                line = f"[{age_label}] {title} ({source})"
                if desc:
                    line += f" — {desc}"
                items.append((page_age, line, tokens))
        except Exception as e:
            logger.warning(f"Brave News query '{query}' failed: {e}")
        if i < len(queries) - 1:
            time.sleep(0.2)
    save_brave_budget(budget)
    if not items:
        return ""
    items.sort(key=lambda x: x[0], reverse=True)
    lines = [f"## Brave News（独立西方搜索引擎，本月已用 {budget['used']}/{BRAVE_MONTHLY_LIMIT}）"]
    lines += [item[1] for item in items[:12]]
    logger.info(f"Brave News: {len(items)} items for {len(queries)} queries")
    return "\n".join(lines) + "\n\n"


