#!/usr/bin/env python3
"""RSS news fetcher — aggregates financial and geopolitical headlines.

Uses httpx for fetching (handles modern TLS) + feedparser for parsing.
Feeds are verified to work from the host machine.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Daily_Intel/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# RSS feeds — verified working on macOS host; unverified feeds are attempted fail-open.
# DNS/TLS-blocked sources (confirmed): Reuters, AP, WSJ, Guardian.
RSS_FEEDS = [
    # NYT — verified
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"),
    # BBC — verified
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    # FT — verified
    ("FT",  "https://www.ft.com/world?format=rss"),
    # CNBC — fast financial/market news
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    # MarketWatch — market-data-driven financial reporting
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    # Foreign Policy — geopolitical depth, US foreign policy strategy
    ("ForeignPolicy", "https://foreignpolicy.com/feed/"),
    # Al Jazeera — Middle East / non-Western geopolitical perspective
    ("AlJazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    # Seeking Alpha — individual stock analysis, institutional perspective
    ("SeekingAlpha", "https://seekingalpha.com/market_currents.xml"),
    # Google News RSS — indirect Reuters/AP/WSJ via Google index (<1h lag)
    ("Reuters", "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en"),
    ("AP",      "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"),
    ("WSJ",     "https://news.google.com/rss/search?q=site:wsj.com&hl=en-US&gl=US&ceid=US:en"),
]


@dataclass
class NewsItem:
    title: str
    summary: str
    url: str
    source: str
    published: datetime
    topics: list[str] = field(default_factory=list)  # matched geopolitics topics


def _parse_date(entry) -> Optional[datetime]:
    """Try multiple date fields, return UTC datetime or None."""
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if not val:
            continue
        try:
            return parsedate_to_datetime(val).astimezone(timezone.utc)
        except Exception:
            pass
    # feedparser parsed_time struct
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if t:
        try:
            return datetime(*t[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _source_name(url: str) -> str:
    return url.split("/")[2].replace("feeds.bbci.co.uk", "BBC").replace("rss.nytimes.com", "NYT").replace("www.ft.com", "FT") if "/" in url else url


def _classify_topics(title: str, summary: str, geo_keywords: dict[str, list[str]]) -> list[str]:
    """Return list of geopolitics topic keys that match this article."""
    text = (title + " " + summary).lower()
    matched = []
    for topic, keywords in geo_keywords.items():
        if any(kw.lower() in text for kw in keywords):
            matched.append(topic)
    return matched


def fetch_rss(
    hours: int,
    geo_keywords: dict[str, list[str]],
    max_per_feed: int = 30,
) -> list[NewsItem]:
    """
    Fetch RSS feeds published within `hours` hours.
    Classifies each item against geo_keywords topics.
    Returns deduplicated list sorted by recency.
    """
    try:
        import feedparser
    except ImportError:
        logger.error("feedparser not installed — run: uv pip install feedparser")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_titles: set[str] = set()
    items: list[NewsItem] = []

    with httpx.Client(headers=_RSS_HEADERS, follow_redirects=True, timeout=12) as client:
        for source, feed_url in RSS_FEEDS:
            try:
                resp = client.get(feed_url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries:
                    if count >= max_per_feed:
                        break
                    pub = _parse_date(entry)
                    if pub and pub < cutoff:
                        continue  # too old

                    title = (entry.get("title") or "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)

                    if "news.google.com" in feed_url:
                        summary = ""
                    else:
                        summary = re.sub(r"<[^>]+>", "", entry.get("summary") or "").strip()
                        summary = summary[:300]

                    topics = _classify_topics(title, summary, geo_keywords)
                    items.append(NewsItem(
                        title=title,
                        summary=summary,
                        url=entry.get("link") or "",
                        source=source,
                        published=pub or datetime.now(timezone.utc),
                        topics=topics,
                    ))
                    count += 1

            except Exception as e:
                logger.warning(f"RSS feed failed ({feed_url}): {e}")
            time.sleep(0.3)

    # Sort newest first
    items.sort(key=lambda x: x.published, reverse=True)
    logger.info(f"RSS: fetched {len(items)} items from {len(RSS_FEEDS)} feeds")
    return items


def fetch_guardian_news(
    hours: int,
    geo_keywords: dict[str, list[str]],
    api_key: str | None = None,
    max_results: int = 20,
) -> list[NewsItem]:
    """Fetch recent articles from The Guardian Open Platform API."""
    if not api_key:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    url = "https://content.guardianapis.com/search"
    params = {
        "api-key": api_key,
        "order-by": "newest",
        "page-size": max_results,
        "section": "business|world|politics|us-news",
        "show-fields": "trailText",
    }
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json().get("response", {}).get("results", [])
    except Exception as e:
        logger.warning(f"Guardian API failed: {e}")
        return []

    items = []
    for article in data:
        pub_str = article.get("webPublicationDate", "")
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if pub < cutoff:
            continue
        title = article.get("webTitle", "").strip()
        summary = (article.get("fields") or {}).get("trailText", "")
        summary = re.sub(r"<[^>]+>", "", summary)[:300]
        topics = _classify_topics(title, summary, geo_keywords)
        items.append(NewsItem(
            title=title,
            summary=summary,
            url=article.get("webUrl", ""),
            source="Guardian",
            published=pub,
            topics=topics,
        ))
    logger.info(f"Guardian API: fetched {len(items)} items")
    return items


def format_news_for_prompt(items: list[NewsItem], geo_keywords: dict[str, list[str]]) -> str:
    """
    Format news into structured text for LLM consumption.
    Groups by geopolitics topic first, then uncategorized market news.
    """
    if not items:
        return "_无最新新闻_"

    sections: dict[str, list[NewsItem]] = {topic: [] for topic in geo_keywords}
    market_items: list[NewsItem] = []

    for item in items:
        placed = False
        for topic in item.topics:
            if topic in sections:
                sections[topic].append(item)
                placed = True
        if not placed:
            market_items.append(item)

    parts = []

    # Geopolitics sections
    for topic, topic_items in sections.items():
        if not topic_items:
            continue
        parts.append(f"[{topic}]")
        for it in topic_items[:5]:
            ts = it.published.strftime("%m-%d %H:%M UTC")
            parts.append(f"  • [{ts}] {it.title} ({it.source})")
            if it.summary:
                parts.append(f"    {it.summary[:200]}")

    # General market/business news (cap at 20)
    if market_items:
        parts.append("[市场财经]")
        for it in market_items[:20]:
            ts = it.published.strftime("%m-%d %H:%M UTC")
            parts.append(f"  • [{ts}] {it.title} ({it.source})")

    return "\n".join(parts)
