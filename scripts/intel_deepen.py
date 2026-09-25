"""Issue #87 code-only Pass 1: bounded Extract of free-source article leads."""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from intel_collect import _word_match
from intel_render import matching_topics
from scoring_utils import _source_confidence_tags

logger = logging.getLogger(__name__)

RUN_CREDIT_CAP = {"am": 13, "pm": None}
MANUAL_CREDIT_CAP = {"am": 13, "pm": 12}
SEARCH_CAP = 5
QUIET_MAX = 5
QUIET_URLS = 3
NEAR_D3 = 8.0
NEAR_D5 = 9.0
NEWS_SPIKE_RATIO = 2.0
NEWS_SPIKE_MIN = 8
MACRO_TOPICS = 3
MACRO_PER_TOPIC = 3
EXTRACT_BATCH_URLS = 20
PAYWALL_DOMAINS = {"ft.com", "wsj.com", "barrons.com"}
NON_ARTICLE_DOMAINS = {"news.google.com"}


def _move_strength(entity: dict) -> float:
    move = entity.get("move") or {}
    keys = [key for flag, key in (("anomaly", "d1"), ("d3", "d3"), ("d5", "d5"))
            if flag in move.get("flags", [])]
    return max((abs(float(move.get(key) or 0)) for key in keys), default=0)


def _move_direction(entity: dict) -> str:
    move = entity.get("move") or {}
    keys = [key for flag, key in (("anomaly", "d1"), ("d3", "d3"), ("d5", "d5"))
            if flag in move.get("flags", [])]
    if not keys:
        keys = [key for key in ("d3", "d5", "d1") if move.get(key) is not None]
    primary = max((float(move.get(key) or 0) for key in keys), key=abs, default=0)
    return "up" if primary >= 0 else "down"


def candidate_entities(entities: list[dict]) -> list[dict]:
    """All price anomaly or #80 threshold names, strongest first, cap five."""
    candidates = [e for e in entities if set((e.get("move") or {}).get("flags", [])) & {"anomaly", "d3", "d5"}]
    return sorted(candidates, key=_move_strength, reverse=True)[:5]


def resolve_article_url(url: str) -> str | None:
    """Resolve Finnhub's 302 Location without downloading an article body.

    Uses GET, not HEAD: Finnhub answers HEAD with "302 Location: /" and only GET
    carries the article URL (issue #106). The response is streamed and closed
    after the headers, so no body is read."""
    if not url.startswith("https://finnhub.io/"):
        return None
    for attempt in range(3):
        try:
            with httpx.stream("GET", url, follow_redirects=False, timeout=8) as response:
                status = response.status_code
                location = response.headers.get("location", "")
            if status == 302 and location.startswith("https://"):
                return location
            if status in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except httpx.TransportError:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def _direct_leads(entity: dict, limit: int = 2, cache: dict | None = None,
                  skip_seen: bool = False) -> list[tuple[str, dict]]:
    """Up to `limit` title-matching article links with distinct landing domains.

    Finnhub 302 links are resolved one request at a time (headers-only GET); `cache` keeps results
    so the Extract top-up pass does not resolve the same link twice. Each call
    logs how many redirects it resolved and how long it took (2026-09-23 PM
    spent 76s here with no log line)."""
    cache = {} if cache is None else cache
    names = [entity["ticker"], *(entity.get("aliases") or [])]
    leads = []
    domains = set()
    resolved = attempted = 0
    started = time.monotonic()
    for row in sorted(entity.get("items", []), key=lambda x: x.get("published_at", ""), reverse=True):
        if row.get("url_kind") not in {"direct", "finnhub_redirect"}:
            continue
        if skip_seen and row.get("seen_before"):
            continue
        if not any(_word_match(row.get("title", ""), name) for name in names):
            continue
        url = row.get("url", "")
        if row.get("url_kind") == "finnhub_redirect":
            if url not in cache:
                attempted += 1
                cache[url] = resolve_article_url(url) or ""
                resolved += bool(cache[url])
            url = cache[url]
        domain = (urlparse(url).hostname or "").removeprefix("www.").lower()
        if not domain or domain in domains:
            continue
        if url.startswith("https://"):
            leads.append((url, row))
            domains.add(domain)
        if len(leads) == limit:
            break
    logger.info("Deepen direct leads %s: %d leads (limit %d), Finnhub redirects resolved %d/%d, %.1fs",
                entity["ticker"], len(leads), limit, resolved, attempted, time.monotonic() - started)
    return leads


def quiet_candidates(entities: list[dict], movers: list[dict],
                     history_counts: dict[str, list[int]]) -> list[tuple[dict, str]]:
    """Choose at most five quiet stocks from independent, ordered signals."""
    mover_names = {e["ticker"] for e in movers}
    ranked = []
    for entity in entities:
        ticker = entity["ticker"]
        if ticker in mover_names or ticker in {"QQQM", "VOO", "EWJ", "SGOL"}:
            continue
        move = entity.get("move") or {}
        d3, d5 = move.get("d3"), move.get("d5")
        near = [(abs(float(value)) / threshold, key, float(value))
                for key, value, threshold in (("d3", d3, NEAR_D3), ("d5", d5, NEAR_D5))
                if value is not None and abs(float(value)) >= threshold]
        fresh = sum(not row.get("seen_before") for row in entity.get("items", []))
        prior = history_counts.get(ticker) or []
        baseline = statistics.median(prior) if prior else None
        if near:
            strength, key, value = max(near)
            tier, reason = 0, f"near_{key} {value:+.1f}%"
        elif any(row.get("url_kind") == "sec_filing" and not row.get("seen_before")
                 for row in entity.get("items", [])):
            tier, strength, reason = 1, 1, "new_8k"
        elif (baseline is not None and fresh >= NEWS_SPIKE_MIN and
              fresh >= NEWS_SPIKE_RATIO * baseline):
            tier = 2
            strength = fresh / baseline if baseline else float("inf")
            reason = f"news_spike {strength:.1f}x" if baseline else "news_spike from_zero"
        else:
            continue
        ranked.append((tier, not bool(entity.get("held")), -strength, ticker, entity, reason))
    ranked.sort(key=lambda row: row[:4])
    return [(entity, reason) for _, _, _, _, entity, reason in ranked[:QUIET_MAX]]


def macro_leads(macro_digest: dict, geo_keywords: dict[str, list[str]]) -> list[tuple[str, dict, str]]:
    """Select recent direct articles from the three most populated topics."""
    rows = macro_digest.get("items") or []
    topics = sorted(geo_keywords, key=lambda topic: (-sum(topic in matching_topics(row, geo_keywords)
                                                         for row in rows), topic))[:MACRO_TOPICS]
    leads = []
    for topic in topics:
        domains = set()
        for row in sorted(rows, key=lambda row: row.get("published_at", ""), reverse=True):
            if len(domains) >= MACRO_PER_TOPIC:
                break
            if row.get("seen_before") or row.get("url_kind") != "direct":
                continue
            if topic not in matching_topics(row, geo_keywords):
                continue
            url = row.get("url", "")
            domain = (urlparse(url).hostname or "").removeprefix("www.").lower()
            if (not url.startswith("https://") or not domain or domain in domains or
                    any(domain == bad or domain.endswith("." + bad)
                        for bad in PAYWALL_DOMAINS | NON_ARTICLE_DOMAINS)):
                continue
            domains.add(domain)
            leads.append((url, row, topic))
    return leads


def _search_bounds(intel_snapshot: dict, entity: dict) -> tuple[str, str]:
    day = date.fromisoformat(intel_snapshot["date"])
    move_start = (entity.get("move") or {}).get("window_start")
    start = move_start or (day - timedelta(days=2 if intel_snapshot["slot"] == "am" else 1)).isoformat()
    return start, day.isoformat()


def deepen_intel_snapshot(intel_snapshot: dict, *, search, extract, remaining,
                          slot: str | None = None, geo_keywords: dict | None = None,
                          history_counts: dict | None = None,
                          run_credit_cap: int | None = None) -> dict:
    """Select and extract mover, macro, then quiet evidence within this run's credits."""
    slot = slot or intel_snapshot.get("slot", "am")
    geo_keywords = geo_keywords or {}
    movers = candidate_entities(intel_snapshot.get("entities", []))
    quiet = quiet_candidates(intel_snapshot.get("entities", []), movers, history_counts or {})
    intel_snapshot["quiet_selected"] = [{"ticker": e["ticker"], "reason": reason}
                                         for e, reason in quiet]
    intel_snapshot["run_credit_cap"] = run_credit_cap
    jobs, status, spare_search = [], {}, {}
    owners: dict[str, list[tuple[dict | None, dict, str | None]]] = {}
    layers: list[list[str]] = [[], [], []]
    cache: dict[str, str] = {}
    search_count = 0

    def add(layer: int, url: str, entity: dict | None, row: dict, topic: str | None = None):
        if url not in owners:
            owners[url] = []
            layers[layer].append(url)
        owners[url].append((entity, row, topic))

    def search_for(entity: dict, reserve_urls: int) -> list[tuple[str, dict]]:
        nonlocal search_count
        ticker = entity["ticker"]
        if search_count >= SEARCH_CAP or remaining() <= math.ceil(reserve_urls / 5):
            status[ticker] = status.get(ticker, "") + "; search cap or budget exhausted"
            return []
        query = f"Why is {entity.get('name') or ticker} stock {_move_direction(entity)}"
        start, end = _search_bounds(intel_snapshot, entity)
        jobs.append({"ticker": ticker, "query": query, "start_date": start, "end_date": end})
        search_count += 1
        try:
            results = search(query, start, end) or []
        except Exception:
            results = []
        found = [(row["url"], {"id": None, "title": row.get("title", ""),
                              "publisher_domain": urlparse(row["url"]).hostname or "",
                              "published_at": "", "summary": row.get("content", "")})
                 for row in results if str(row.get("url", "")).startswith("https://")]
        spare_search[ticker] = found[2:]
        status[ticker] = status.get(ticker, "") + ("; search: found leads" if found else "; search: no leads")
        return found[:2]

    for entity in movers:
        ticker = entity["ticker"]
        leads = _direct_leads(entity, cache=cache)
        status[ticker] = "direct leads" if leads else ""
        if not leads:
            leads = search_for(entity, len(layers[0]))
        for url, row in leads:
            add(0, url, entity, row)

    macro = intel_snapshot.get("macro_digest") or {}
    macro.setdefault("fulltext", [])
    for url, row, topic in macro_leads(macro, geo_keywords):
        add(1, url, None, row, topic)

    for entity, reason in quiet:
        ticker = entity["ticker"]
        status[ticker] = "quiet: " + reason
        leads = _direct_leads(entity, limit=QUIET_URLS, cache=cache, skip_seen=True)
        if not leads:
            leads = search_for(entity, len(layers[0]) + len(layers[1]))
        for url, row in leads:
            add(2, url, entity, row)

    urls = list(dict.fromkeys(url for layer in layers for url in layer))
    affordable = max(0, int(remaining())) * 5
    urls = urls[:affordable]
    target = min(affordable, math.ceil(len(urls) / 5) * 5)
    topped_up = 0
    for entity in movers:
        if len(urls) >= target:
            break
        known = {url for url in urls if any(owner is entity for owner, _, _ in owners[url])}
        extra = [lead for lead in _direct_leads(entity, limit=len(known) + 1, cache=cache)
                 if lead[0] not in known]
        extra += spare_search.get(entity["ticker"], [])
        for url, row in extra:
            if len(urls) >= target:
                break
            if url in urls:
                continue
            urls.append(url)
            owners[url] = [(entity, row, None)]
            topped_up += 1
    for entity, _ in quiet:
        if len(urls) >= target:
            break
        for url, row in spare_search.get(entity["ticker"], [])[:1]:
            if len(urls) >= target or url in urls:
                continue
            urls.append(url)
            owners[url] = [(entity, row, None)]
            topped_up += 1
    if topped_up:
        logger.info("Deepen Extract top-up: +%d URLs to %d (same credit tier)", topped_up, len(urls))

    results = []
    for start in range(0, len(urls), EXTRACT_BATCH_URLS):
        batch = urls[start:start + EXTRACT_BATCH_URLS]
        if remaining() < math.ceil(len(batch) / 5):
            break
        try:
            results.extend(extract(batch, "financial company event evidence") or [])
        except Exception as exc:
            logger.warning("Deepen Extract batch failed: %s", type(exc).__name__)

    candidates = [{**row, "published_date": row.get("published_at"),
                   "content": row.get("summary", "")}
                  for e in movers + [e for e, _ in quiet] for row in e.get("items", [])]
    candidates += [{**lead, "url": url, "published_date": lead.get("published_at"),
                    "content": lead.get("summary", "")}
                   for url, owned in owners.items() for _, lead, _ in owned]
    successful_urls = set()
    for result in results:
        url = result.get("url", "")
        if url not in owners or url not in urls:
            continue
        chunks = result.get("chunks") or []
        body = " ".join(str(chunk.get("content") or "") for chunk in chunks[:2]).strip()
        body = (body or str(result.get("raw_content") or ""))[:1200]
        if not body:
            continue
        successful_urls.add(url)
        for entity, lead, topic in owners[url]:
            chunk = {"item_id": lead.get("id"), "url": url, "text": body,
                     "confidence_tags": _source_confidence_tags(url, body, candidates, [])}
            if entity is None:
                macro["fulltext"].append({**chunk, "topic": topic})
            else:
                entity.setdefault("fulltext", []).append(chunk)
                status[entity["ticker"]] += "; extracted"
    intel_snapshot["deepen_status"] = status
    intel_snapshot["search_jobs"] = jobs
    intel_snapshot["search_count"] = search_count
    intel_snapshot["extract_url_count"] = len(urls)
    intel_snapshot["extract_topup_count"] = topped_up
    intel_snapshot["extract_success_count"] = len(successful_urls)
    intel_snapshot["quiet_url_count"] = sum(url in urls for url in layers[2])
    intel_snapshot["macro_url_count"] = sum(url in urls for url in layers[1])
    return intel_snapshot
