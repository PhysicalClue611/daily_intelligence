"""Issue #87 Pass 0: free source collection and per-entity intelligence snapshots.

Leaf module: no report entrypoint imports. Network failures are recorded per
source; the old report path never depends on this shadow result.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import httpx

from fetch_news import RSS_FEEDS, NewsItem
from intel_sources import FINNHUB_BASE
from scoring_utils import _title_tokens_for_dedup, _token_jaccard
from sec_edgar_utils import sec_user_agent

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
ETFS = {"QQQM", "VOO", "EWJ", "SGOL"}
SUFFIXES = re.compile(r"\s+(?:Corp(?:oration)?|Inc(?:orporated)?|Holdings?|Ltd|Limited|PLC|Co)\.?$", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DailyIntel/1.0)"}
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}
ENTITY_SOURCES = ("finnhub", "google_news", "rss", "guardian", "sec_8k", "yahoo_rss")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_ATOM_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
CIK_CACHE = ROOT / "cik_cache.json"
_GOOGLE_NEWS_LOCK = threading.Lock()
_last_google_news_request = 0.0
_SEC_LOCK = threading.Lock()
_last_sec_request = 0.0


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clean(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", str(value or ""))).strip()[:limit]


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.").lower()


def item(title: str, summary: str, source: str, publisher_domain: str,
         url: str, url_kind: str, published_at: datetime) -> dict:
    return {"id": "", "title": _clean(title, 1000), "summary": _clean(summary, 300),
            "source": source, "publisher_domain": publisher_domain, "publisher_domains": [publisher_domain] if publisher_domain else [],
            "url": url, "url_kind": url_kind, "published_at": _utc(published_at).isoformat()}


def _pace_sec() -> None:
    """SEC fair-access cap is 10 requests/second, including retries."""
    global _last_sec_request
    with _SEC_LOCK:
        wait = 0.12 - (time.monotonic() - _last_sec_request)
        if _last_sec_request and wait > 0:
            time.sleep(wait)
        _last_sec_request = time.monotonic()


def _sec_headers() -> dict:
    return {"User-Agent": sec_user_agent(),
            "Accept": "application/atom+xml, application/json;q=0.9, */*;q=0.8"}


def _request(url: str, *, params: dict | None = None, timeout: float = 8,
             headers: dict | None = None) -> httpx.Response:
    global _last_google_news_request
    last = None
    for attempt in range(3):
        try:
            if url == "https://news.google.com/rss/search":
                # Pace actual HTTP attempts, including retries, across all callers.
                with _GOOGLE_NEWS_LOCK:
                    delay = 1.0 - (time.monotonic() - _last_google_news_request)
                    if delay > 0:
                        time.sleep(delay)
                    _last_google_news_request = time.monotonic()
            if "sec.gov" in url:
                _pace_sec()
            response = httpx.get(url, params=params, headers=headers or HEADERS, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise last


def _word_match(text: str, word: str) -> bool:
    if not word.strip():
        return False
    boundary = (r"(?<![A-Za-z0-9])" + re.escape(word.strip()) + r"(?![A-Za-z0-9])"
                if not re.search(r"[A-Za-z0-9]", word) else r"\b" + re.escape(word.strip()) + r"\b")
    return re.search(boundary, text,
                     0 if len(word.strip()) <= 4 else re.I) is not None


def match_entities(text: str, aliases: dict[str, list[str]]) -> set[str]:
    return {ticker for ticker, names in aliases.items() if any(_word_match(text, name) for name in names)}


def match_geo_topics(text: str, geo_keywords: dict[str, list[str]]) -> list[str]:
    return [topic for topic, words in geo_keywords.items() if any(_word_match(text, word) for word in words)]


def parse_aliases(watchlist_text: str) -> dict[str, list[str]]:
    section = re.search(r"^## 实体别名\s*\n(.*?)(?=^## |\Z)", watchlist_text, re.M | re.S)
    result = {}
    if section:
        for line in section.group(1).splitlines():
            if ":" not in line:
                continue
            ticker, values = line.split(":", 1)
            names = [v.strip() for v in values.split(",") if v.strip()]
            if re.fullmatch(r"[A-Z]{1,5}", ticker.strip()) and names:
                result[ticker.strip()] = names
    return result


def resolve_aliases(tickers: list[str], configured: dict[str, list[str]], api_key: str,
                    cache_path: Path = ROOT / "entity_alias_cache.json") -> tuple[dict[str, list[str]], dict[str, str]]:
    try:
        cache = json.loads(cache_path.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    aliases, errors = {}, {}
    changed = False
    for ticker in tickers:
        if ticker in configured:
            aliases[ticker] = list(dict.fromkeys([*configured[ticker], ticker]))
            continue
        name = cache.get(ticker, "")
        if not name and api_key:
            try:
                data = _request(f"{FINNHUB_BASE}/stock/profile2", params={"symbol": ticker, "token": api_key}).json()
                name = SUFFIXES.sub("", _clean(data.get("name"), 120)).strip()
                if name:
                    cache[ticker] = name
                    changed = True
            except Exception as exc:
                errors[ticker] = f"profile2: {type(exc).__name__}"
        aliases[ticker] = list(dict.fromkeys([name, ticker] if name else [ticker]))
    if changed:
        _atomic_json(cache_path, cache)
    return aliases, errors


def _published(entry) -> datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            return _utc(parsedate_to_datetime(raw))
        except (TypeError, ValueError):
            pass
    return None


def _in_window(published: datetime, since: datetime, as_of: datetime) -> bool:
    return since <= _utc(published) <= _utc(as_of)


def fetch_finnhub(ticker: str, since: datetime, as_of: datetime, api_key: str) -> list[dict]:
    if not api_key:
        raise ValueError("FINNHUB_API_KEY missing")
    data = _request(f"{FINNHUB_BASE}/company-news", params={
        "symbol": ticker, "from": since.date().isoformat(), "to": as_of.date().isoformat(), "token": api_key,
    }).json()
    if not isinstance(data, list):
        raise ValueError("Finnhub company-news returned non-list")
    out = []
    for row in data:
        try:
            published = datetime.fromtimestamp(int(row["datetime"]), timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if _in_window(published, since, as_of) and row.get("headline"):
            url = row.get("url") or ""
            out.append(item(row["headline"], row.get("summary", ""), "Finnhub",
                            _domain(url) or _clean(row.get("source"), 80), url,
                            "finnhub_redirect" if _domain(url) == "finnhub.io" else "direct", published))
    return out


def fetch_google_news(name: str, since: datetime, as_of: datetime) -> list[dict]:
    query = f'"{name}" stock after:{since.date().isoformat()} before:{(as_of.date() + timedelta(days=1)).isoformat()}'
    url = "https://news.google.com/rss/search"
    response = _request(url, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=12)
    feed = feedparser.parse(response.content)
    out = []
    for entry in feed.entries:
        published = _published(entry)
        if not published or not _in_window(published, since, as_of):
            continue
        source = entry.get("source") or {}
        publisher = _domain(source.get("href", "")) or _clean(source.get("title", ""), 80)
        out.append(item(entry.get("title", ""), "", "Google News", publisher,
                        entry.get("link", ""), "google_news", published))
    return out


def resolve_ciks(tickers: list[str], cache_path: Path = CIK_CACHE) -> dict[str, str]:
    """Ticker to 10-digit CIK. One SEC company_tickers download fills the cache."""
    try:
        cache = json.loads(Path(cache_path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    missing = [ticker for ticker in tickers if ticker not in cache]
    if missing:
        data = _request(SEC_TICKERS_URL, headers=_sec_headers(), timeout=30).json()
        rows = data.values() if isinstance(data, dict) else data
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ticker") or "").upper()
            if not symbol or symbol in cache:
                continue
            try:
                cache[symbol] = f"{int(row['cik_str']):010d}"
            except (KeyError, TypeError, ValueError):
                continue
        for ticker in missing:
            cache.setdefault(ticker, "")
        _atomic_json(Path(cache_path), cache)
    return {ticker: str(cache.get(ticker) or "") for ticker in tickers}


def _entry_time(entry) -> datetime | None:
    raw = entry.get("updated") or entry.get("published")
    if raw:
        try:
            return _utc(datetime.fromisoformat(str(raw)))
        except ValueError:
            parsed = _published(entry)
            if parsed:
                return parsed
    filed = str(entry.get("filing-date") or "")
    if filed:
        try:
            return datetime.fromisoformat(filed).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _form_items(entry) -> list[str]:
    text = " ".join(str(entry.get(key) or "") for key in ("summary", "items-desc"))
    return list(dict.fromkeys(re.findall(r"\d+\.\d+", text)))


def _accession(entry) -> str:
    acc = str(entry.get("accession-number") or "")
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
        return acc
    match = re.search(r"\d{10}-\d{2}-\d{6}", str(entry.get("summary") or ""))
    return match.group(0) if match else ""


def fetch_sec_8k(ticker: str, since: datetime, as_of: datetime, cik: str) -> list[dict]:
    if not cik:
        raise LookupError(f"CIK not found for {ticker}")
    # dateb one day past as_of keeps a same-day filing on the page; the window drops the rest.
    response = _request(SEC_ATOM_URL, params={
        "action": "getcompany", "CIK": cik, "type": "8-K", "count": "40", "output": "atom",
        "dateb": (_utc(as_of) + timedelta(days=1)).strftime("%Y%m%d"),
    }, headers=_sec_headers(), timeout=20)
    out = []
    for entry in feedparser.parse(response.content).entries:
        form = str(entry.get("filing-type") or entry.get("title") or "")
        if "8-K" not in form:
            continue
        published = _entry_time(entry)
        if not published or not _in_window(published, since, as_of):
            continue
        nums = _form_items(entry)
        acc = _accession(entry)
        title = ("8-K Item " + ", ".join(nums)) if nums else "8-K"
        if acc:
            title = f"{title} {acc}"
        link = entry.get("link") or entry.get("filing-href") or ""
        out.append(item(title, entry.get("summary", ""), "SEC 8-K", "sec.gov", link, "sec_filing", published))
    return out


def fetch_yahoo_rss(ticker: str, since: datetime, as_of: datetime) -> list[dict]:
    response = _request(YAHOO_RSS_URL, params={"s": ticker, "region": "US", "lang": "en-US"},
                        headers=YAHOO_HEADERS, timeout=12)
    out = []
    for entry in feedparser.parse(response.content).entries:
        published = _published(entry)
        if not published or not _in_window(published, since, as_of) or not entry.get("title"):
            continue
        link = entry.get("link", "")
        out.append(item(entry["title"], entry.get("summary", ""), "Yahoo RSS",
                        _domain(link), link, "direct", published))
    return out


def fetch_rss_pool(since: datetime, as_of: datetime) -> tuple[list[dict], list[str]]:
    def one(source: str, url: str) -> tuple[list[dict], str | None]:
        try:
            feed = feedparser.parse(_request(url, timeout=12).content)
            found = []
            for entry in feed.entries:
                published = _published(entry)
                if published and _in_window(published, since, as_of) and entry.get("title"):
                    link = entry.get("link", "")
                    found.append(item(entry["title"], entry.get("summary", ""), "RSS", _domain(link) or source,
                                      link, "direct", published))
            return found, None
        except Exception as exc:
            return [], f"rss:{source}: {type(exc).__name__}"
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=len(RSS_FEEDS)) as pool:
        futures = [pool.submit(one, source, url) for source, url in RSS_FEEDS]
        for future in futures:
            found, error = future.result()
            rows.extend(found)
            if error:
                errors.append(error)
    return rows, errors


def fetch_guardian_pool(since: datetime, as_of: datetime, api_key: str) -> tuple[list[dict], list[str]]:
    if not api_key:
        return [], ["guardian: GUARDIAN_API_KEY missing"]
    try:
        rows = []
        page = 1
        while True:
            data = _request("https://content.guardianapis.com/search", params={
                "api-key": api_key, "from-date": since.date().isoformat(), "to-date": as_of.date().isoformat(),
                "order-by": "newest", "page-size": 200, "page": page,
                "section": "business|world|politics|us-news", "show-fields": "trailText",
            }, timeout=12).json()["response"]
            for row in data.get("results", []):
                try:
                    published = datetime.fromisoformat(row["webPublicationDate"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if _in_window(published, since, as_of):
                    url = row.get("webUrl", "")
                    rows.append(item(row.get("webTitle", ""), (row.get("fields") or {}).get("trailText", ""),
                                     "Guardian", _domain(url), url, "direct", published))
            if page >= int(data.get("pages", 1)):
                break
            page += 1
        return rows, []
    except Exception as exc:
        return [], [f"guardian: {type(exc).__name__}"]


def deduplicate(rows: list[dict]) -> list[dict]:
    result = []
    for row in sorted(rows, key=lambda r: r["published_at"], reverse=True):
        tokens = _title_tokens_for_dedup(row["title"])
        previous = next((old for old in result if _token_jaccard(tokens, _title_tokens_for_dedup(old["title"])) >= 0.8), None)
        if previous is None:
            copy = {**row, "publisher_domains": list(row.get("publisher_domains", []))}
            result.append(copy)
        else:
            previous["publisher_domains"] = list(dict.fromkeys(previous["publisher_domains"] + row.get("publisher_domains", [])))
    for index, row in enumerate(result, 1):
        row["id"] = f"i{index}"
    return result


def normalize_title(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.casefold()))


def assemble_entity(ticker: str, name: str, aliases: list[str], held: bool, weight_pct: float | None,
                    move: dict, source_items: dict[str, list[dict]], errors: dict[str, str] | list[str],
                    as_of: datetime) -> dict:
    error_list = [f"{key}: {value}" for key, value in errors.items()] if isinstance(errors, dict) else list(errors)
    coverage = {source: len([row for row in source_items.get(source, [])
                             if datetime.fromisoformat(row["published_at"]) <= _utc(as_of)])
                for source in ENTITY_SOURCES}
    coverage["errors"] = error_list
    rows = [row for group in source_items.values() for row in group
            if datetime.fromisoformat(row["published_at"]) <= _utc(as_of)]
    return {"ticker": ticker, "name": name, "aliases": aliases, "held": held, "weight_pct": weight_pct,
            "move": move, "coverage": coverage, "items": deduplicate(rows), "fulltext": []}


def _atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def archive_intel_snapshot(intel_snapshot: dict, root: Path = ROOT / "archives") -> Path:
    date, slot = intel_snapshot["date"], intel_snapshot["slot"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or slot not in ("am", "pm"):
        raise ValueError("invalid intelligence snapshot date or slot")
    path = root / (date[:4] + date[5:7]) / f"{date}-{slot}-intel-snapshot.json"
    _atomic_json(path, intel_snapshot)
    return path


def archived_intel_snapshot_paths(root: Path) -> list[Path]:
    """Newest first, preferring the current name over a same-slot legacy archive."""
    root = Path(root)
    paths = [*root.glob("*/*-intel-snapshot.json"), *root.glob("*/*-ledger.json")]
    return sorted(paths, key=lambda path: (path.name[:13], path.name.endswith("-intel-snapshot.json")),
                  reverse=True)


def previous_event_titles(root: Path, before: datetime, ticker: str) -> list[str]:
    for path in archived_intel_snapshot_paths(root):
        try:
            data = json.loads(path.read_text())
            if datetime.fromisoformat(data["as_of"]) >= before:
                continue
        except (OSError, ValueError, KeyError):
            continue
        return [row.get("title", "") for entity in data.get("entities", [])
                if entity.get("ticker") == ticker for row in entity.get("items", [])]
    return []


def collect(tickers: list[str], aliases: dict[str, list[str]], held: set[str], weights: dict[str, float],
            moves: dict[str, dict], geo_keywords: dict[str, list[str]], as_of: datetime, slot: str,
            *, replay: bool = False, finnhub_key: str = "", guardian_key: str = "") -> tuple[list[dict], dict]:
    """Fetch sources concurrently, then assign shared news by entity boundary."""
    as_of = _utc(as_of)
    default_since = as_of - timedelta(hours=36 if slot == "am" else 24)
    since_by_ticker = {ticker: min(default_since, datetime.fromisoformat(moves[ticker]["window_start"]).replace(tzinfo=timezone.utc))
                       if moves.get(ticker, {}).get("window_start") else default_since for ticker in tickers}
    pool_since = min(since_by_ticker.values(), default=default_since)
    errors = {ticker: [] for ticker in tickers}
    source = {ticker: {name: [] for name in ENTITY_SOURCES} for ticker in tickers}
    spans = {"sec_8k": [], "yahoo_rss": []}
    span_lock = threading.Lock()

    def timed(name, fn, *args):
        started = time.monotonic()
        try:
            return fn(*args)
        finally:
            with span_lock:
                spans[name].append((started, time.monotonic()))

    try:
        ciks = timed("sec_8k", resolve_ciks, tickers) if tickers else {}
    except Exception as exc:
        ciks = {}
        message = f"sec_8k: {type(exc).__name__}"
        for ticker in tickers:
            errors[ticker].append(message)
    # Separate pools keep a Finnhub backlog from bunching Google News.
    # HTTP attempts are paced in _request().
    with (ThreadPoolExecutor(max_workers=5) as finnhub_pool,
          ThreadPoolExecutor(max_workers=3) as google_pool,
          ThreadPoolExecutor(max_workers=4) as sec_pool,
          ThreadPoolExecutor(max_workers=4) as yahoo_pool):
        futures = {}
        for ticker in tickers:
            futures[(ticker, "finnhub")] = finnhub_pool.submit(
                fetch_finnhub, ticker, since_by_ticker[ticker], as_of, finnhub_key)
            futures[(ticker, "google_news")] = google_pool.submit(
                fetch_google_news, aliases[ticker][0], since_by_ticker[ticker], as_of)
            if not any(err.startswith("sec_8k:") for err in errors[ticker]):
                cik = ciks.get(ticker, "")
                if not cik:
                    errors[ticker].append("sec_8k: CIK not found")
                else:
                    futures[(ticker, "sec_8k")] = sec_pool.submit(
                        timed, "sec_8k", fetch_sec_8k, ticker, since_by_ticker[ticker], as_of, cik)
            if not replay:
                futures[(ticker, "yahoo_rss")] = yahoo_pool.submit(
                    timed, "yahoo_rss", fetch_yahoo_rss, ticker, since_by_ticker[ticker], as_of)
        for (ticker, kind), future in futures.items():
            try:
                source[ticker][kind] = future.result()
            except Exception as exc:
                errors[ticker].append(f"{kind}: {type(exc).__name__}")
    def _wall(name: str) -> float:
        rows = spans[name]
        if not rows:
            return 0.0
        return max(end for _, end in rows) - min(start for start, _ in rows)
    logger.info("Pass 0 sec_8k %.1fs", _wall("sec_8k"))
    if replay:
        logger.info("Pass 0 yahoo_rss skipped in replay")
    else:
        logger.info("Pass 0 yahoo_rss %.1fs", _wall("yahoo_rss"))
    pooled, rss_errors = ([], ["rss: skipped in replay"]) if replay else fetch_rss_pool(pool_since, as_of)
    guardian, guardian_errors = fetch_guardian_pool(pool_since, as_of, guardian_key)
    yahoo_errors = ["yahoo_rss: skipped in replay"] if replay else []
    pool_errors = rss_errors + guardian_errors + yahoo_errors
    pooled.extend(guardian)
    macro_items = []
    for row in pooled:
        matches = match_entities(row["title"] + " " + row["summary"], aliases)
        if matches:
            kind = "guardian" if row["source"] == "Guardian" else "rss"
            for ticker in matches:
                if _in_window(datetime.fromisoformat(row["published_at"]), since_by_ticker[ticker], as_of):
                    source[ticker][kind].append(row)
        elif _in_window(datetime.fromisoformat(row["published_at"]), default_since, as_of):
            macro_items.append(row)
    entities = [assemble_entity(ticker, aliases[ticker][0], aliases[ticker], ticker in held, weights.get(ticker),
                                moves.get(ticker, {}), source[ticker], errors[ticker] + pool_errors, as_of)
                for ticker in tickers]
    macro = {"geo_topics_hit": sorted({topic for row in macro_items
              for topic in match_geo_topics(row["title"] + " " + row["summary"], geo_keywords)}),
             "items": deduplicate(macro_items), "coverage": {"rss": len([r for r in macro_items if r["source"] == "RSS"]),
                         "guardian": len([r for r in macro_items if r["source"] == "Guardian"]), "errors": pool_errors}}
    return entities, macro
