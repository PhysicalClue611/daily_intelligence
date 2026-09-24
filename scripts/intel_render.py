"""Bounded intelligence snapshot inputs and deterministic fallback for issue #87 Pass 2."""
from __future__ import annotations

import re

from intel_collect import _word_match

_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_ITEM_NUMBER = re.compile(r"\d+\.\d+")


def emergency_intel_snapshot(today: str, slot: str, as_of, price_rows: list,
                     watchlist: dict, multiday_moves: dict, error: str,
                     held: set[str] | None = None, weights: dict | None = None,
                     window_starts: dict[str, str] | None = None) -> dict:
    """Keep a failed collector from suppressing a price-anomaly report."""
    rows = {row.ticker: row for row in price_rows}
    entities = []
    for ticker in watchlist.get("stocks", []):
        if ticker in {"QQQM", "VOO", "EWJ", "SGOL"}:
            continue
        row = rows.get(ticker)
        d3, d5 = multiday_moves.get(ticker, (None, None))
        flags = (["anomaly"] if row and row.is_anomaly else [])
        if d3 is not None and abs(d3) >= 15:
            flags.append("d3")
        if d5 is not None and abs(d5) >= 20:
            flags.append("d5")
        entities.append({"ticker": ticker, "name": ticker, "aliases": watchlist.get("entity_aliases", {}).get(ticker, [ticker]),
                         "held": ticker in (held or set()), "weight_pct": (weights or {}).get(ticker),
                         "move": {"d1": row.change_pct if row else None, "d3": d3, "d5": d5, "flags": flags,
                                  **({"window_start": window_starts[ticker]}
                                     if window_starts and ticker in window_starts else {})},
                         "coverage": {"finnhub": 0, "google_news": 0, "rss": 0, "guardian": 0,
                                      "sec_8k": 0, "yahoo_rss": 0, "errors": [error]},
                         "items": [], "fulltext": []})
    return {"schema_version": 1, "date": today, "slot": slot, "as_of": as_of.isoformat(),
            "entities": entities, "macro_digest": {"items": [], "geo_topics_hit": []}}


def _has_move(entity: dict) -> bool:
    return bool(set((entity.get("move") or {}).get("flags", [])) & {"anomaly", "d3", "d5"})


def should_report(intel_snapshot: dict) -> bool:
    return (any(_has_move(e) or e.get("items") for e in intel_snapshot.get("entities", []))
            or bool((intel_snapshot.get("macro_digest") or {}).get("geo_topics_hit")))


def filing_headline(row: dict) -> str | None:
    """Company filing label for Pass 2. Item numbers stay numeric."""
    if row.get("source") != "SEC 8-K" and row.get("url_kind") != "sec_filing":
        return None
    title = row.get("title") or ""
    nums = list(dict.fromkeys(_ITEM_NUMBER.findall(title)))
    label = "公司公告（8-K Item " + "、".join(nums) + "）" if nums else "公司公告（8-K）"
    accession = _ACCESSION.search(title)
    return f"{label} {accession.group(0)}" if accession else label


def coverage_line(entity: dict) -> str:
    coverage = entity.get("coverage") or {}
    errors = coverage.get("errors") or []
    return (f"Finnhub {coverage.get('finnhub', 0)}、Google News {coverage.get('google_news', 0)}、"
            f"RSS {coverage.get('rss', 0)}、Guardian {coverage.get('guardian', 0)}、"
            f"SEC 8-K {coverage.get('sec_8k', 0)}、Yahoo RSS {coverage.get('yahoo_rss', 0)}"
            + (f"；错误：{'; '.join(errors)}" if errors else ""))


def render_intel_snapshot_context(intel_snapshot: dict, geo_keywords: dict[str, list[str]]) -> str:
    """All movers, quiet holdings with up to 8 titles, macro latest 8/topic, 40 total."""
    lines = ["## 标的事实与来源（供分析）"]
    quiet_observers = []
    for e in intel_snapshot.get("entities", []):
        move = e.get("move") or {}
        active = _has_move(e)
        rows = e.get("items") or []
        if not active and not e.get("held"):
            quiet_observers.append(e["ticker"])
            continue
        if not active and not rows:
            continue
        price_bits = [f"当日 {move.get('d1', 0):+.1f}%"] if move.get("d1") is not None else []
        for key, label in (("d3", "3日"), ("d5", "5日")):
            if move.get(key) is not None:
                price_bits.append(f"{label} {move[key]:+.1f}%")
        role = "持有" if e.get("held") else "观察"
        lines.append(f"### {e['ticker']}（{role}）{'、'.join(price_bits)}")
        lines.append(f"检索范围（仅供核查）：{coverage_line(e)}")
        limit = 25 if active else 8
        for row in rows[:limit]:
            shown = filing_headline(row) or row.get("title", "")
            if active:
                summary = str(row.get("summary") or "")[:200]
                lines.append(f"- {row.get('published_at', '')[:16]} [{row.get('publisher_domain', '')}] "
                             f"{shown}" + (f"；{summary}" if summary else "")
                             + ("（此前已报道）" if row.get("seen_before") else ""))
            else:
                lines.append(f"- [{row.get('publisher_domain', '')}] {shown}")
        for chunk in e.get("fulltext", []):
            lines.append(f"  正文[{chunk.get('url', '')}] {chunk.get('confidence_tags', '')}: {chunk.get('text', '')}")
    if quiet_observers:
        lines.append("无异动观察标的：" + "、".join(quiet_observers))
    macro = (intel_snapshot.get("macro_digest") or {}).get("items", [])
    chosen, seen = [], set()
    for topic, aliases in geo_keywords.items():
        count = 0
        for row in sorted(macro, key=lambda r: r.get("published_at", ""), reverse=True):
            if count >= 8 or len(chosen) >= 40:
                break
            if (row.get("id") not in seen and
                    any(_word_match(row.get("title", "") + " " + row.get("summary", ""), alias)
                        for alias in aliases)):
                chosen.append((topic, row))
                seen.add(row.get("id"))
                count += 1
    if chosen:
        lines.append("## 宏观与地缘线索")
        for topic, row in chosen:
            lines.append(f"- {topic} [{row.get('publisher_domain', '')}] {row.get('title', '')}")
    return "\n".join(lines) + "\n"


def render_fallback_report(intel_snapshot: dict, slot_label: str) -> str:
    lines = [f"# [Daily_Intel] {intel_snapshot['date']} {slot_label}", "", "## 持仓与观察标的"]
    for e in intel_snapshot.get("entities", []):
        if not (_has_move(e) or e.get("items")):
            continue
        move = e.get("move") or {}
        errors = (e.get("coverage") or {}).get("errors") or []
        lead = (filing_headline(e["items"][0]) or e["items"][0].get("title", "")) if e.get("items") else ""
        reason = (f"线索待核实：{lead}（{e['items'][0].get('publisher_domain', '')}）"
                  if e.get("items") else ("未能完成检索" if errors else "未找到原因"))
        lines.append(f"- {e['ticker']}：当日 {float(move.get('d1') or 0):+.1f}%；" + reason
                     + f"；覆盖：{coverage_line(e)}。")
    if len(lines) == 3:
        lines.append("- 公司层面暂无可报告事项。")
    macro = intel_snapshot.get("macro_digest") or {}
    if macro.get("geo_topics_hit") and macro.get("items"):
        lines.append("\n## 宏观与地缘")
        for row in sorted(macro["items"], key=lambda r: r.get("published_at", ""), reverse=True)[:8]:
            lines.append(f"- {row.get('title', '')}（{row.get('publisher_domain', '')}）")
    return "\n".join(lines) + "\n"


def filter_social_lines(section: str, entities: list[dict]) -> str:
    """At most one matching source line per displayed ticker."""
    selected = [e for e in entities if _has_move(e) or e.get("items")]
    lines = []
    for e in selected:
        names = [e["ticker"], *(e.get("aliases") or [])]
        match = next((line.strip() for line in section.splitlines()
                      if line.lstrip().startswith("-") and any(_word_match(line, name) for name in names)), None)
        if match and match not in lines:
            lines.append(match)
    return "## 相关标的社交舆情（每标的一行）\n" + "\n".join(lines) + "\n" if lines else ""
