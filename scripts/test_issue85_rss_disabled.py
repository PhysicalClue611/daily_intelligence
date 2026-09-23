#!/usr/bin/env python3
"""Issue #85: five low-relevance general-news RSS sources are disabled.

Run: .venv/bin/python scripts/test_issue85_rss_disabled.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_news as fn

DISABLED_NAMES = {"NYT", "AP", "WSJ", "BBC", "AlJazeera"}
KEPT_NAMES = {"FT", "CNBC", "MarketWatch", "ForeignPolicy", "SeekingAlpha", "Reuters", "Digitimes"}


def test_disabled_sources_not_fetched():
    active = {n for n, _ in fn.RSS_FEEDS}
    assert not (active & DISABLED_NAMES), active & DISABLED_NAMES


def test_kept_sources_still_fetched():
    active = {n for n, _ in fn.RSS_FEEDS}
    assert KEPT_NAMES <= active, KEPT_NAMES - active


def test_disabled_urls_kept_for_restore():
    names = {n for n, _ in fn.RSS_FEEDS_DISABLED}
    assert names == DISABLED_NAMES, names
    urls = {u for _, u in fn.RSS_FEEDS_DISABLED}
    assert "https://www.aljazeera.com/xml/rss/all.xml" in urls
    assert len(fn.RSS_FEEDS_DISABLED) == 8  # NYT x3, BBC x2, AP, WSJ, Al Jazeera


def test_no_overlap_between_active_and_disabled():
    active = {u for _, u in fn.RSS_FEEDS}
    disabled = {u for _, u in fn.RSS_FEEDS_DISABLED}
    assert not (active & disabled)


if __name__ == "__main__":
    failed = 0
    for name, fnc in list(globals().items()):
        if name.startswith("test_") and callable(fnc):
            try:
                fnc()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
