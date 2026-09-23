"""Issue #87 code-only Pass 1: bounded Extract of free-source article leads."""
from __future__ import annotations

import time
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from intel_collect import _word_match
from scoring_utils import _source_confidence_tags


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
    """Resolve Finnhub's 302 Location without downloading an article body."""
    if not url.startswith("https://finnhub.io/"):
        return None
    for attempt in range(3):
        try:
            response = httpx.head(url, follow_redirects=False, timeout=8)
            location = response.headers.get("location", "")
            if response.status_code == 302 and location.startswith("https://"):
                return location
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except httpx.TransportError:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def _direct_leads(entity: dict) -> list[tuple[str, dict]]:
    names = [entity["ticker"], *(entity.get("aliases") or [])]
    leads = []
    domains = set()
    for row in sorted(entity.get("items", []), key=lambda x: x.get("published_at", ""), reverse=True):
        if row.get("url_kind") not in {"direct", "finnhub_redirect"}:
            continue
        if not any(_word_match(row.get("title", ""), name) for name in names):
            continue
        url = row.get("url", "")
        if row.get("url_kind") == "finnhub_redirect":
            url = resolve_article_url(url) or ""
        domain = (urlparse(url).hostname or "").removeprefix("www.").lower()
        if not domain or domain in domains:
            continue
        if url.startswith("https://"):
            leads.append((url, row))
            domains.add(domain)
        if len(leads) == 2:
            break
    return leads


def _search_bounds(ledger: dict, entity: dict) -> tuple[str, str]:
    day = date.fromisoformat(ledger["date"])
    move_start = (entity.get("move") or {}).get("window_start")
    start = move_start or (day - timedelta(days=2 if ledger["slot"] == "am" else 1)).isoformat()
    return start, day.isoformat()


def deepen_ledger(ledger: dict, *, search, extract, remaining) -> dict:
    """Mutate a newly built ledger; at most 3 searches and 10 Extract URLs/2cr."""
    selected = candidate_entities(ledger.get("entities", []))
    jobs = []
    urls = []
    owners = {}
    status = {}
    search_count = 0
    for entity in selected:
        ticker = entity["ticker"]
        if len(urls) >= 10:
            status[ticker] = "extract cap reached"
            continue
        leads = _direct_leads(entity)
        if not leads:
            if search_count >= 3 or remaining() < 1:
                status[ticker] = "search cap or budget exhausted"
                continue
            direction = _move_direction(entity)
            query = f"Why is {entity.get('name') or ticker} stock {direction}"
            start, end = _search_bounds(ledger, entity)
            jobs.append({"ticker": ticker, "query": query, "start_date": start, "end_date": end})
            search_count += 1
            try:
                results = search(query, start, end) or []
            except Exception:
                results = []
            leads = [(row["url"], {"id": None, "title": row.get("title", ""),
                                         "publisher_domain": urlparse(row["url"]).hostname or "",
                                         "published_at": "", "summary": row.get("content", "")})
                     for row in results[:2] if str(row.get("url", "")).startswith("https://")]
            status[ticker] = "search: found leads" if leads else "search: no leads"
        else:
            status[ticker] = "direct leads"
        for url, row in leads:
            if url not in owners and len(urls) < 10:
                urls.append(url)
                owners[url] = []
            if url in owners:
                owners[url].append((entity, row))
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
    ledger["deepen_status"] = status
    ledger["search_jobs"] = jobs
    ledger["search_count"] = search_count
    ledger["extract_url_count"] = len(urls)
    ledger["extract_success_count"] = len(successful_urls)
    return ledger
