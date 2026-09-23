#!/usr/bin/env python3
"""Build an issue #87 Pass 0 ledger in shadow or historical replay mode.

Replay never imports the report entrypoint and has no report/Obsidian-write,
Telegram, email, Tavily, or Pass 2 path. It writes only local archives.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import intel_collect as collect
from publication_window import _unexplained_publication_window

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _move_from_row(row, multi: tuple[float | None, float | None], window_start: str | None = None) -> dict:
    d3, d5 = multi
    flags = []
    if row.is_anomaly:
        flags.append("anomaly")
    if d3 is not None and abs(d3) >= 15:
        flags.append("d3")
    if d5 is not None and abs(d5) >= 20:
        flags.append("d5")
    return {"d1": row.change_pct, "session": getattr(row, "session_change_pct", None),
            "d3": d3, "d5": d5, "flags": flags, **({"window_start": window_start} if window_start else {})}


def build_ledger(wl: dict, as_of: datetime, slot: str, *, price_rows: list = (),
                 multiday_moves: dict | None = None, window_starts: dict[str, str] | None = None,
                 held: set[str] | None = None, weights: dict[str, float] | None = None,
                 replay: bool = False, only_tickers: list[str] | None = None,
                 archive_root: Path = collect.ROOT / "archives") -> tuple[dict, Path]:
    tickers = [t for t in wl["stocks"] if t not in collect.ETFS]
    if only_tickers is not None:
        tickers = [t for t in tickers if t in only_tickers]
    configured = wl.get("entity_aliases", {})
    aliases, alias_errors = collect.resolve_aliases(tickers, configured, os.environ.get("FINNHUB_API_KEY", ""))
    rows = {row.ticker: row for row in price_rows}
    multiday_moves = multiday_moves or {}
    window_starts = window_starts or {}
    moves = {ticker: _move_from_row(rows[ticker], multiday_moves.get(ticker, (None, None)), window_starts.get(ticker))
             for ticker in tickers if ticker in rows}
    entities, macro = collect.collect(tickers, aliases, held or set(), weights or {}, moves,
                                      wl.get("geo_keywords", {}), as_of, slot, replay=replay,
                                      finnhub_key=os.environ.get("FINNHUB_API_KEY", ""),
                                      guardian_key=os.environ.get("GUARDIAN_API_KEY", ""))
    previous = {ticker: {collect.normalize_title(title) for title in
                         collect.previous_event_titles(archive_root, as_of, ticker)} for ticker in tickers}
    for entity in entities:
        for row in entity["items"]:
            row["seen_before"] = collect.normalize_title(row["title"]) in previous[entity["ticker"]]
        if entity["ticker"] in alias_errors:
            entity["coverage"]["errors"].append(alias_errors[entity["ticker"]])
    ledger = {"schema_version": 1, "date": as_of.astimezone(ET).date().isoformat(), "slot": slot,
              "as_of": as_of.isoformat(), "replay": replay, "entities": entities, "macro_digest": macro}
    path = collect.archive_ledger(ledger, archive_root)
    return ledger, path


def render_summary(ledger: dict) -> str:
    lines = [f"# Pass 0 ledger — {ledger['date']} {ledger['slot']}"]
    for entity in ledger["entities"]:
        coverage = entity["coverage"]
        lines.append(f"- {entity['ticker']}: {len(entity['items'])} items, "
                     f"move={entity['move']}; "
                     f"Finnhub={coverage['finnhub']}, Google News={coverage['google_news']}, "
                     f"RSS={coverage['rss']}, Guardian={coverage['guardian']}; "
                     f"errors={'; '.join(coverage['errors']) or 'none'}")
    lines.append(f"\nMacro items: {len(ledger['macro_digest']['items'])}")
    return "\n".join(lines) + "\n"


def _read_watchlist_rest() -> dict:
    key = os.environ.get("OBSIDIAN_API_KEY", "")
    if not key:
        raise RuntimeError("OBSIDIAN_API_KEY missing for watchlist read")
    base = os.environ.get("OBSIDIAN_REST_URL", "https://127.0.0.1:27124").rstrip("/")
    url = base + "/vault/" + quote("Hermes/Daily Intelligence/watchlist.md", safe="/")
    response = httpx.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "text/markdown"},
                         verify=False, timeout=10)
    response.raise_for_status()
    text = response.text
    section = re.search(r"^## 个股与基金\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        raise ValueError("watchlist stock section missing")
    stocks = [x.strip() for x in re.split(r"[,\s]+", section.group(1)) if x.strip()]
    geo_section = re.search(r"^## 地缘政治关键词\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    geo = {}
    if geo_section:
        for line in geo_section.group(1).splitlines():
            if ":" in line:
                topic, words = line.split(":", 1)
                geo[topic.strip()] = [w.strip() for w in words.split(",") if w.strip()]
    return {"stocks": stocks, "geo_keywords": geo, "entity_aliases": collect.parse_aliases(text)}


@lru_cache(maxsize=3)
def _read_context_rest(year_month: str) -> str:
    key = os.environ.get("OBSIDIAN_API_KEY", "")
    if not key:
        raise RuntimeError("OBSIDIAN_API_KEY missing for context-log read")
    base = os.environ.get("OBSIDIAN_REST_URL", "https://127.0.0.1:27124").rstrip("/")
    name = f"Hermes/Daily Intelligence/Daily Reports/Daily_Intel_context_{year_month}.md"
    response = httpx.get(base + "/vault/" + quote(name, safe="/"),
                         headers={"Authorization": f"Bearer {key}", "Accept": "text/markdown"},
                         verify=False, timeout=15)
    response.raise_for_status()
    return response.text


def _context_price_rows(text: str, date: str, slot: str, tickers: list[str]):
    from fetch_prices import PriceRow

    label = "开盘前简报" if slot == "am" else "夜盘收市速报"
    sections = re.findall(r"^## \[Context\] " + re.escape(date + " " + label) + r"\s*\n(.*?)(?=^## \[Context\]|\Z)",
                          text, re.M | re.S)
    if not sections:
        return []
    # The first occurrence is the original run; later FORCE_RUNs may duplicate it.
    price_section = re.search(r"^### 价格快照\s*\n(.*?)(?=^### |\Z)", sections[0], re.M | re.S)
    if not price_section:
        return []
    rows = []
    for line in price_section.group(1).splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        label_cell = cells[0]
        ticker = next((t for t in tickers if label_cell == t or f"({t})" in label_cell), None)
        if not ticker:
            continue
        try:
            price = float(re.sub(r"[^0-9.]", "", cells[1]))
            d1 = float(cells[3 if slot == "am" else 2].rstrip("%"))
            session = float(cells[2 if slot == "am" else 3].rstrip("%"))
            week = float(cells[4 if slot == "am" else 5].rstrip("%"))
        except ValueError:
            continue
        rows.append(PriceRow(ticker, label_cell, price, price, d1, week, "[!]" in cells[-1], "$",
                             session_change_pct=session, slot=slot))
    return rows


def _historical_prices(tickers: list[str], as_of: datetime):
    """Daily closes are a replay approximation; old AM premarket bars are unavailable."""
    import yfinance as yf
    from fetch_prices import PriceRow

    date = as_of.date()
    rows = []
    for ticker in tickers:
        try:
            frame = yf.download(ticker, start=str(date - timedelta(days=16)), end=str(date + timedelta(days=2)),
                                interval="1d", progress=False, auto_adjust=True)
            closes = frame["Close"]
            if getattr(closes, "ndim", 1) > 1:
                closes = closes.iloc[:, 0]
            closes = closes.dropna()
            before = closes[closes.index.date < date]
            current = closes[closes.index.date == date]
            if as_of.astimezone(ET).hour < 12:
                if len(before) < 2:
                    continue
                price, prev = float(before.iloc[-1]), float(before.iloc[-2])
            else:
                if current.empty or before.empty:
                    continue
                price, prev = float(current.iloc[-1]), float(before.iloc[-1])
            d1 = 100 * (price / prev - 1)
            rows.append(PriceRow(ticker, ticker, price, prev, d1, 0, abs(d1) >= 3, "$", slot="am" if as_of.astimezone(ET).hour < 12 else "pm"))
        except Exception as exc:
            logger.warning("Replay price %s unavailable: %s", ticker, type(exc).__name__)
    return rows


def _historical_multiday_moves(price_rows: list, as_of: datetime, slot: str) -> dict:
    """Rebuild the 3/5-session trigger from closes before the simulated run."""
    import yfinance as yf
    from fetch_prices import multiday_return_pct

    report_date = as_of.astimezone(ET).date()
    moves = {}
    for row in price_rows:
        if row.ticker == "AAOI":  # issue #80 excludes this observer from multi-day chasing
            continue
        d3, d5 = getattr(row, "change_3d_pct", None), getattr(row, "change_5d_pct", None)
        if d3 is None or d5 is None:
            try:
                frame = yf.download(row.ticker, start=str(report_date - timedelta(days=24)),
                                    end=str(report_date), interval="1d", progress=False, auto_adjust=True)
                closes = frame["Close"]
                if getattr(closes, "ndim", 1) > 1:
                    closes = closes.iloc[:, 0]
                closes = closes.dropna()
                closes = closes[closes.index.date < report_date]
                if slot == "am":
                    numerator, before = float(closes.iloc[-1]), closes.iloc[:-1]
                else:
                    numerator, before = float(row.price), closes
                d3 = multiday_return_pct(numerator, before, 3, allow_short=False)
                d5 = multiday_return_pct(numerator, before, 5, allow_short=False)
            except Exception as exc:
                logger.warning("Replay multiday %s unavailable: %s", row.ticker, type(exc).__name__)
        moves[row.ticker] = (d3, d5)
    return moves


def replay(date: str, slot: str, *, tickers: list[str] | None = None,
           archive_root: Path = collect.ROOT / "archives") -> tuple[dict, Path]:
    as_of = datetime.fromisoformat(f"{date} {'08:30' if slot == 'am' else '20:10'}").replace(tzinfo=ET)
    wl = _read_watchlist_rest()
    candidates = [t for t in wl["stocks"] if t not in collect.ETFS and (tickers is None or t in tickers)]
    try:
        prices = _context_price_rows(_read_context_rest(date[:4] + date[5:7]), date, slot, candidates)
    except Exception as exc:
        logger.warning("Historical context-log price read unavailable: %s", type(exc).__name__)
        prices = []
    price_source = "original_context_log" if prices else "daily_close_approximation"
    if not prices:
        prices = _historical_prices(candidates, as_of)
    multiday_moves = _historical_multiday_moves(prices, as_of, slot)
    window_starts = {}
    for ticker, (d3, d5) in multiday_moves.items():
        days = 5 if d5 is not None and abs(d5) >= 20 else (3 if d3 is not None and abs(d3) >= 15 else 0)
        if days:
            window_starts[ticker] = _unexplained_publication_window(date, days, slot)[0]
    ledger, path = build_ledger(wl, as_of, slot, price_rows=prices, replay=True,
                                multiday_moves=multiday_moves, window_starts=window_starts,
                                only_tickers=candidates, archive_root=archive_root)
    ledger["replay_price_source"] = price_source
    collect.archive_ledger(ledger, archive_root)
    path.with_suffix(".md").write_text(render_summary(ledger), encoding="utf-8")
    return ledger, path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", required=True, help="YYYY-MM-DD")
    parser.add_argument("--slot", choices=("am", "pm"), required=True)
    parser.add_argument("--ticker", action="append", help="Limit replay to a ticker; repeatable")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _, output = replay(args.replay, args.slot, tickers=args.ticker)
    print(output)
