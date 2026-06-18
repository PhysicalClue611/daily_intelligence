#!/usr/bin/env python3
"""
Backfill existing monthly reports into MemPalace per-day drawers.

Usage:
    python backfill_drawers.py          # dry-run (print what would be written)
    python backfill_drawers.py --write  # actually write to MemPalace

Safety:
    - Sequential, single-threaded — no concurrent ChromaDB writes
    - 2s sleep between writes to allow WAL flush
    - Idempotent: bridge returns already_exists for duplicates, script skips them
    - Dry-run by default: pass --write to commit
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

BRIDGE_URL = "http://localhost:8765"
_OBSIDIAN = Path(
    os.environ.get(
        "OBSIDIAN_PATH",
        Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview",
    )
)
REPORTS_DIR = _OBSIDIAN / "Hermes/Daily Intelligence/Daily Reports"
WRITE_SLEEP = 2.0  # seconds between writes — gives ChromaDB WAL time to flush

# Matches: ## 2026-05-28 开盘前简报  OR  ## 2026-05-28 夜盘动向
_SECTION_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}) (开盘前简报|夜盘动向)\s*$", re.MULTILINE
)


def check_bridge() -> bool:
    try:
        r = httpx.get(f"{BRIDGE_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception as e:
        print(f"[ERROR] Bridge unreachable: {e}")
        return False


def parse_sections(text: str, source_file_rel: str) -> list[dict]:
    """Split monthly report text into per-day/slot sections."""
    sections = []
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        date_str = m.group(1)
        slot_title = m.group(2)
        slot = "am" if "开盘前" in slot_title else "pm"
        # Content: from this match to next match start (or EOF)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        # Prepend structured header for better embedding
        content = f"日期：{date_str} {slot}\n\n{section_text}"
        sections.append({
            "date": date_str,
            "slot": slot,
            "source_file": source_file_rel,
            "content": content,
        })
    return sections


def add_drawer(section: dict, dry_run: bool) -> str:
    """POST one section to bridge. Returns status string."""
    if dry_run:
        preview = section["content"][:80].replace("\n", " ")
        return f"[DRY-RUN] {section['date']} {section['slot']}  {preview}…"

    try:
        resp = httpx.post(
            f"{BRIDGE_URL}/mempalace/add_drawer",
            json={
                "wing": "paperview",
                "room": "finance",
                "content": section["content"],
                "source_file": section["source_file"],
                "added_by": "backfill",
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        reason = result.get("reason", "new")
        drawer_id = result.get("drawer_id", "?")
        return f"[{reason.upper():13s}] {section['date']} {section['slot']}  {drawer_id}"
    except Exception as e:
        return f"[ERROR        ] {section['date']} {section['slot']}  {e}"


def main():
    dry_run = "--write" not in sys.argv

    if dry_run:
        print("DRY-RUN mode — pass --write to commit\n")
    else:
        print("WRITE mode — writing to MemPalace\n")
        if not check_bridge():
            sys.exit(1)

    report_files = sorted(REPORTS_DIR.glob("Daily_Intel_report_??????.md"))
    if not report_files:
        print(f"No report files found in {REPORTS_DIR}")
        sys.exit(0)

    all_sections = []
    for path in report_files:
        rel = f"Hermes/Daily Intelligence/Daily Reports/{path.name}"
        text = path.read_text(encoding="utf-8")
        sections = parse_sections(text, rel)
        print(f"{path.name}: {len(sections)} sections found")
        all_sections.extend(sections)

    print(f"\nTotal sections: {len(all_sections)}\n")

    new_count = skipped_count = error_count = 0
    for section in all_sections:
        status = add_drawer(section, dry_run)
        print(status)
        if "[NEW" in status or "[new" in status.lower():
            new_count += 1
        elif "ALREADY_EXISTS" in status.upper():
            skipped_count += 1
        elif "ERROR" in status:
            error_count += 1
        else:
            new_count += 1

        if not dry_run:
            time.sleep(WRITE_SLEEP)

    print(f"\nDone. new={new_count} skipped={skipped_count} errors={error_count}")


if __name__ == "__main__":
    main()
