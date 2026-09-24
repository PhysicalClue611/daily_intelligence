"""Issue #87 code-only Pass 1: bounded Extract of free-source article leads."""
from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from intel_collect import _word_match
from scoring_utils import _source_confidence_tags

logger = logging.getLogger(__name__)


def _move_strength(entity: dict) -> float:
    move = entity.get("move") or {}
    keys = [key for flag, key in (("anomaly", "d1"), ("d3", "d3"), ("d5", "d5"))
            if flag in move.get("flags", [])]
    return max((abs(float(move.get(key) or 0)) for key in keys), default=0)


def _move_direction(entity: dict) -> str:
    move = entity.get("move") or {}
    keys = [key for flag, key in (("anomaly", "d1"), ("d3", "d3"), ("d5", "d5"))
            if flag in move.get("flags", [])]
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


def _direct_leads(entity: dict, limit: int = 2, cache: dict | None = None) -> list[tuple[str, dict]]:
    """Up to `limit` title-matching article links with distinct landing domains.

    Finnhub 302 links are resolved one HEAD at a time; `cache` keeps results
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


def _search_bounds(intel_snapshot: dict, entity: dict) -> tuple[str, str]:
    day = date.fromisoformat(intel_snapshot["date"])
    move_start = (entity.get("move") or {}).get("window_start")
    start = move_start or (day - timedelta(days=2 if intel_snapshot["slot"] == "am" else 1)).isoformat()
    return start, day.isoformat()


def deepen_intel_snapshot(intel_snapshot: dict, *, search, extract, remaining) -> dict:
    """Mutate a newly built intelligence snapshot; at most 3 searches and 10 Extract URLs/2cr."""
    selected = candidate_entities(intel_snapshot.get("entities", []))
    jobs = []
    urls = []
    owners = {}
    status = {}
    search_count = 0
    cache: dict[str, str] = {}
    spare_search: dict[str, list[tuple[str, dict]]] = {}
    for entity in selected:
        ticker = entity["ticker"]
        if len(urls) >= 10:
            status[ticker] = "extract cap reached"
            continue
        leads = _direct_leads(entity, cache=cache)
        if not leads:
            if search_count >= 3 or remaining() < 1:
                status[ticker] = "search cap or budget exhausted"
                continue
            direction = _move_direction(entity)
            query = f"Why is {entity.get('name') or ticker} stock {direction}"
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
            leads, spare_search[ticker] = found[:2], found[2:]
            status[ticker] = "search: found leads" if leads else "search: no leads"
        else:
            status[ticker] = "direct leads"
        for url, row in leads:
            if url not in owners and len(urls) < 10:
                urls.append(url)
                owners[url] = []
            if url in owners:
                owners[url].append((entity, row))
    # Extract bills ceil(urls / 5) credits, so 7 URLs cost the same 2cr as 10.
    # Fill up to the next multiple of five with each mover's next distinct
    # direct link or spare search result, strongest move first.
    target = min(10, math.ceil(len(urls) / 5) * 5)
    topped_up = 0
    for entity in selected:
        if len(urls) >= target:
            break
        known = {url for url, owned in owners.items() if any(e is entity for e, _ in owned)}
        extra = []
        if status.get(entity["ticker"]) in {"direct leads", "extract cap reached"}:
            extra = [lead for lead in _direct_leads(entity, limit=len(known) + 1, cache=cache)
                     if lead[0] not in known]
        extra += spare_search.get(entity["ticker"], [])
        for url, row in extra:
            if len(urls) >= target:
                break
            if url in owners:
                continue
            urls.append(url)
            owners[url] = [(entity, row)]
            topped_up += 1
    if topped_up:
        logger.info("Deepen Extract top-up: +%d URLs to %d (same credit tier)", topped_up, len(urls))
    # This function calls Extract once, at most 10 URLs = 2 credits. Existing
    # budget helpers perform their own final preflight and accounting.
    affordable = min(len(urls), max(0, int(remaining())) * 5)
    urls = urls[:affordable]
    results = []
    if urls:
        try:
            results = extract(urls, "financial company event evidence") or []
        except Exception:
            results = []
    successful_urls = set()
    for result in results:
        url = result.get("url", "")
        if url not in owners or url not in urls:
            continue
        chunks = result.get("chunks") or []
        body = " ".join(str(chunk.get("content") or "") for chunk in chunks[:2]).strip()
        if not body:
            body = str(result.get("raw_content") or "")[:1200]
        if not body:
            continue
        successful_urls.add(url)
        candidates = [{**row, "published_date": row.get("published_at"),
                       "content": row.get("summary", "")}
                      for e in selected for row in e.get("items", [])]
        candidates += [{**lead, "url": candidate_url,
                        "published_date": lead.get("published_at"),
                        "content": lead.get("summary", "")}
                       for candidate_url, owned in owners.items() for _, lead in owned]
        for entity, lead in owners[url]:
            entity["fulltext"].append({"item_id": lead.get("id"), "url": url,
                                       "text": body[:1200],
                                       "confidence_tags": _source_confidence_tags(url, body, candidates, [])})
            status[entity["ticker"]] = "extracted"
    intel_snapshot["deepen_status"] = status
    intel_snapshot["search_jobs"] = jobs
    intel_snapshot["search_count"] = search_count
    intel_snapshot["extract_url_count"] = len(urls)
    intel_snapshot["extract_topup_count"] = topped_up
    intel_snapshot["extract_success_count"] = len(successful_urls)
    return intel_snapshot
