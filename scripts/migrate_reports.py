#!/usr/bin/env python3
"""
migrate_reports.py — One-time migration of old daily report files into monthly format.

Reads all YYYY-MM-DD.md / YYYY-MM-DD-pm.md files in REPORTS_DIR,
strips their frontmatter, and appends them as sections into the
corresponding Daily_Intel_report_YYYYMM.md monthly file.

Safe to re-run: checks for existing section headers before appending.
"""
import os
import re
import sys
from pathlib import Path

_HOME = os.path.expanduser("~")
_OBSIDIAN = Path(os.getenv(
    "OBSIDIAN_PATH",
    os.path.join(_HOME, "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview"),
))
REPORTS_DIR = _OBSIDIAN / "Hermes/Daily Intelligence/Daily Reports"


def _monthly_path(date_str: str) -> Path:
    ym = date_str[:7].replace("-", "")
    return REPORTS_DIR / f"Daily_Intel_report_{ym}.md"


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL).strip()


def migrate():
    # Find all old-format daily files
    old_files = sorted([
        f for f in REPORTS_DIR.glob("20??-??-??.md")
        if not f.name.startswith("Daily_Intel")
    ] + [
        f for f in REPORTS_DIR.glob("20??-??-??-pm.md")
    ])

    if not old_files:
        print("No old-format files found.")
        return

    print(f"Found {len(old_files)} files to migrate:")
    for f in old_files:
        print(f"  {f.name}")
    print()

    migrated = 0
    for filepath in old_files:
        name = filepath.stem  # e.g. "2026-04-29" or "2026-04-29-pm"
        is_pm = name.endswith("-pm")
        date_str = name[:-3] if is_pm else name  # strip "-pm"
        slot_label = "夜盘动向" if is_pm else "开盘前简报"

        monthly = _monthly_path(date_str)
        ym_display = date_str[:7]

        # Check if already migrated
        if monthly.exists():
            existing = monthly.read_text(encoding="utf-8")
            if f"## {date_str} {slot_label}" in existing:
                print(f"  SKIP  {filepath.name} → already in {monthly.name}")
                continue

        # Read and strip frontmatter
        raw = filepath.read_text(encoding="utf-8")
        content = _strip_frontmatter(raw)

        section = (
            f"\n## {date_str} {slot_label}\n\n"
            f"{content}\n\n---\n"
        )

        if not monthly.exists():
            header = (
                f"---\ndate: {ym_display}\n"
                f"source: Daily Intelligence\n---\n\n"
                f"# Daily Intelligence {ym_display}\n"
            )
            monthly.write_text(header + section, encoding="utf-8")
            print(f"  CREATE {filepath.name} → {monthly.name} [{slot_label}]")
        else:
            with monthly.open("a", encoding="utf-8") as f:
                f.write(section)
            print(f"  APPEND {filepath.name} → {monthly.name} [{slot_label}]")

        migrated += 1

    print(f"\nDone. {migrated} reports migrated.")


if __name__ == "__main__":
    migrate()
