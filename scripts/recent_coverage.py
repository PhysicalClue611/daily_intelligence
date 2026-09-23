"""Bounded, entity-specific context from recent monthly reports for Pass 2."""

import re
from datetime import date, timedelta
from pathlib import Path

from fetch_prices import DISPLAY_NAMES
from intel_collect import _word_match

_HEADER = re.compile(r"^## (\d{4}-\d{2}-\d{2}) (开盘前简报|夜盘收市速报)\s*$", re.M)
_FOLLOWUP = re.compile(r"^## 追问\b", re.M)


def _trading_dates(today: date) -> set[str]:
    """Last five NYSE sessions, including today when it is a session."""
    try:
        import exchange_calendars as xcals
        calendar = xcals.get_calendar("XNYS")
        start = today - timedelta(days=18)
        sessions = calendar.sessions_in_range(start.isoformat(), today.isoformat())
        return {day.date().isoformat() for day in sessions[-5:]}
    except Exception:
        dates = []
        cursor = today
        while len(dates) < 5:
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        return set(dates)


def _entity_names(ticker: str, aliases: dict[str, list[str]]) -> list[str]:
    label = DISPLAY_NAMES.get(ticker, "")
    chinese = re.sub(r"\([^)]*\)", "", label).strip()
    names = [ticker, *aliases.get(ticker, [])]
    if re.search(r"[\u3400-\u9fff]", chinese):
        names.append(chinese)
    return list(dict.fromkeys(name for name in names if name))


def _matching_blocks(body: str, names: list[str]) -> list[str]:
    blocks = []
    for paragraph in re.split(r"\n\s*\n", body):
        lines = paragraph.splitlines()
        current = []
        for line in lines:
            # Lists and table rows are independent evidence units even without blank lines.
            if re.match(r"\s*(?:[-*+]\s|\|)", line):
                if current:
                    blocks.append(" ".join(current).strip())
                    current = []
                blocks.append(line.strip())
            elif line.startswith("## ") or line.startswith("### "):
                if current:
                    blocks.append(" ".join(current).strip())
                    current = []
            else:
                current.append(line.strip())
        if current:
            blocks.append(" ".join(current).strip())
    return [block for block in blocks if block and any(_word_match(block, name) for name in names)]


def build_recent_coverage_section(report_dir: Path, today: str, slot: str,
                                  tickers: list[str], aliases: dict[str, list[str]]) -> str:
    """Return at most 600 characters per ticker; empty when no prior mention exists."""
    if not tickers:
        return ""
    dates = _trading_dates(date.fromisoformat(today))
    files = sorted({Path(report_dir) / f"Daily_Intel_report_{day[:7].replace('-', '')}.md"
                    for day in dates})
    reports = []
    seen = set()
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        headings = list(_HEADER.finditer(text))
        for index, heading in enumerate(headings):
            day, label = heading.groups()
            report_slot = "am" if label == "开盘前简报" else "pm"
            key = (day, report_slot)
            if key in seen or day not in dates or day > today or (day == today and
                    (report_slot == slot or (slot == "am" and report_slot == "pm"))):
                continue
            seen.add(key)
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            body = text[heading.end():end]
            followup = _FOLLOWUP.search(body)
            if followup:
                body = body[:followup.start()]
            reports.append((day, report_slot, body))
    reports.sort(key=lambda item: (item[0], item[1]), reverse=True)
    sections = []
    for ticker in dict.fromkeys(tickers):
        names = _entity_names(ticker, aliases)
        entries = []
        used = 0
        for day, report_slot, body in reports:
            for block in _matching_blocks(body, names):
                entry = f"[{day[5:]} {report_slot.upper()}] {block}"
                remaining = 600 - used - (1 if entries else 0)
                if remaining <= 0:
                    break
                entries.append(entry[:remaining])
                used += min(len(entry), remaining) + (1 if len(entries) > 1 else 0)
            if used >= 600:
                break
        if entries:
            sections.append(f"### {ticker}\n" + "\n".join(entries))
    return "## 近 5 个交易日已报道（同一标的）\n" + "\n".join(sections) + "\n" if sections else ""
