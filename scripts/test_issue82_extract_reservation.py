"""
Issue #82: must-answer tickers reserve an Extract slot outside score_and_filter.

Reproduces the 2026-09-22 PM rerun: the INTC unexplained-move query returned
3 low-score hits, and the global scorer kept only high keyword-bonus geopolitics
(Japan / China / Iran). INTC never reached Extract.

This is not a keyword_bonus patch. Reservation must keep INTC even when the
title has no geo keyword and would lose the shared ranking.

Run: python scripts/test_issue82_extract_reservation.py
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_finance as rf

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 20, 10, tzinfo=ET)
PUB = "2026-09-22T16:00:00-04:00"

GEO = {
    "China-Taiwan": ["Taiwan", "TSMC", "strait"],
    "China-Japan": ["Japan", "Takaichi"],
    "US-Domestic": ["Trump", "White House"],
    "US-Iran": ["Iran", "Hormuz"],
}


def _hit(title, url, score, content="", source=None, published=PUB):
    return {
        "title": title,
        "url": url,
        "score": score,
        "content": content,
        "published_date": published,
        "_source_ticker": source,
    }


def _scenario_20260922_pm():
    """INTC's 3 real-shaped hits plus a geo pile that wins keyword_bonus."""
    intc = [
        _hit(
            "Packaging capacity rumor lifts chip shares",
            "https://www.reuters.com/technology/intel-packaging-2026-09-22",
            0.25,
            "shares rose over five sessions on a packaging rumor",
            "INTC",
        ),
        _hit(
            "Chip stock roundup",
            "https://randomblog.example/chip-week",
            0.15,
            "several names higher this week",
            "INTC",
        ),
        _hit(
            "Closing bell video",
            "https://clips.example/video/close-bell-2026-09-22",
            0.40,
            "watch the afternoon move",
            "INTC",
        ),
    ]
    # Distinct wording so title-dedup does not collapse the pile. Each title
    # still stacks several geo keywords (+0.05 each), which is what buried INTC.
    geo_titles = [
        "Trump hosts Takaichi as Japan rewrites its Taiwan strait playbook",
        "White House transcript covers Takaichi remarks on Taiwan missiles",
        "Iran envoy tells the assembly Hormuz tanker insurance is unchanged",
        "Fishing fleets near the Taiwan strait watch a Japan coast guard drill",
        "Pentagon briefing quotes Trump on Japan basing and the Taiwan strait",
        "Lloyd's notes Hormuz premiums after an Iran parliamentary shout",
        "Osaka exporters ask Takaichi about Taiwan semiconductor spare parts",
        "Campaign memo shows Trump tying Japan votes to a Taiwan headline",
        "Tehran radio mentions Hormuz while dismissing the Takaichi visit",
        "White House photographer logs a Japan gift and a Taiwan map",
        "Shipbrokers split Iran crude fixtures from any Hormuz closure rumor",
        "A Kyoto professor argues Takaichi misread the Taiwan fishery zone",
    ]
    geo = []
    for i, title in enumerate(geo_titles):
        geo.append(_hit(
            title,
            f"https://www.reuters.com/world/geo-{i}",
            0.55 + i * 0.01,
            title,
            None,
        ))
    amkr = _hit(
        "Packaging peer moves with the group",
        "https://www.ft.com/content/amkr-group-move",
        0.22,
        "no company-specific filing",
        "AMKR",
    )
    return intc + [amkr] + geo


def test_global_scorer_drops_intc_on_20260922_pm_fixture():
    """The fixture is the production failure: keyword_bonus buries INTC."""
    raw = _scenario_20260922_pm()
    top = rf.score_and_filter(
        raw, ["AMKR", "CL=F"], GEO, top_n=10, now=NOW,
    )
    urls = [r["url"] for r in top]
    assert not any("intel" in u or "chip-week" in u or "amkr" in u for u in urls), urls


def test_intc_best_result_is_reserved_without_keyword_bonus():
    raw = _scenario_20260922_pm()
    reserved = rf._select_reserved_results(
        raw, ["AMKR", "INTC"], now=NOW,
    )
    assert set(reserved) == {"AMKR", "INTC"}
    # Reuters beats the higher raw-score video page and the weaker blog.
    # None of these titles contain the geo keywords that buried them.
    assert reserved["INTC"]["url"] == (
        "https://www.reuters.com/technology/intel-packaging-2026-09-22"
    )
    assert reserved["AMKR"]["url"] == "https://www.ft.com/content/amkr-group-move"


def test_reserved_urls_are_removed_from_the_open_pool():
    raw = _scenario_20260922_pm()
    reserved, prescreened = rf._plan_reserved_and_open(
        raw,
        must_answer_tickers=["AMKR", "INTC", "NVDA"],
        anomaly_tickers=["AMKR", "CL=F"],
        geo_keywords=GEO,
        now=NOW,
    )
    assert "NVDA" not in reserved
    assert set(reserved) == {"AMKR", "INTC"}
    reserved_urls = {r["url"] for r in reserved.values()}
    open_urls = {r["url"] for r in prescreened}
    # Only the chosen URL is held out. The ticker's other hits may still compete.
    assert reserved_urls.isdisjoint(open_urls)
    assert "https://www.reuters.com/technology/intel-packaging-2026-09-22" not in open_urls
    assert any(u.startswith("https://www.reuters.com/world/") for u in open_urls)


def test_open_pool_prescreen_cap_is_25():
    raw = [
        _hit(
            f"alpha{i} bravo{i} charlie{i} delta{i} echo{i}",
            f"https://example.com/n-{i}",
            0.2 + i * 0.01,
            source=None,
        )
        for i in range(30)
    ]
    _, prescreened = rf._plan_reserved_and_open(
        raw, [], ["AMKR"], GEO, NOW,
    )
    assert len(prescreened) == 25


def test_empty_must_answer_subset_reserves_nothing():
    raw = _scenario_20260922_pm()
    reserved = rf._select_reserved_results(raw, ["NVDA", "QCOM"], now=NOW)
    assert reserved == {}


def test_unexplained_move_results_get_the_7day_fence_and_ticker_tag():
    job = {
        "query": "INTC stock surged 27.5% over 5 trading days reason catalyst 2026-09-22",
        "_unexplained_move_ticker": "INTC",
    }
    fresh = _hit("fresh", "https://example.com/fresh", 0.2, source=None)
    stale = _hit(
        "stale", "https://example.com/stale", 0.9, source=None,
        published="2026-08-01T12:00:00-04:00",
    )
    out = rf._annotate_job_results(job, [fresh, stale], NOW)
    assert [r["url"] for r in out] == ["https://example.com/fresh"]
    assert out[0]["_source_ticker"] == "INTC"


def test_open_discovery_results_are_tagged_with_no_ticker():
    job = {"query": "Trump Takaichi Taiwan", "search_depth": "basic"}
    hit = _hit("Trump Takaichi Taiwan strait", "https://example.com/geo", 0.5)
    out = rf._annotate_job_results(job, [hit], NOW)
    assert len(out) == 1
    assert out[0]["_source_ticker"] is None


def test_extract_batches_keep_ten_url_cap_and_cover_about_twenty():
    reserved = [f"https://example.com/r{i}" for i in range(5)]
    opened = [f"https://example.com/o{i}" for i in range(15)]
    batches = rf._extract_url_batches(reserved) + rf._extract_url_batches(opened)
    flat = [u for batch in batches for u in batch]
    assert flat == reserved + opened
    assert all(len(batch) <= 10 for batch in batches)
    assert [len(batch) for batch in batches] == [5, 10, 5]
    calls = []

    def _fake_extract(urls, query, budget):
        calls.append(list(urls))
        return [{"url": u} for u in urls]

    got = rf._extract_reserved_then_open(
        reserved, opened, "INTC Japan", {"used": 0}, _fake_extract,
    )
    assert calls == batches
    assert [r["url"] for r in got] == reserved + opened


def test_archive_labels_reserved_and_open_urls():
    import report_writers as rw
    reserved = {
        "https://www.reuters.com/technology/intel-packaging-2026-09-22": "INTC",
    }
    assert rw._candidate_origin_label(
        "https://www.reuters.com/technology/intel-packaging-2026-09-22", reserved,
    ) == "预留:INTC"
    assert rw._candidate_origin_label("https://www.reuters.com/world/geo-0", reserved) == "开放池"


def run():
    tests = [
        test_global_scorer_drops_intc_on_20260922_pm_fixture,
        test_intc_best_result_is_reserved_without_keyword_bonus,
        test_reserved_urls_are_removed_from_the_open_pool,
        test_open_pool_prescreen_cap_is_25,
        test_empty_must_answer_subset_reserves_nothing,
        test_unexplained_move_results_get_the_7day_fence_and_ticker_tag,
        test_open_discovery_results_are_tagged_with_no_ticker,
        test_extract_batches_keep_ten_url_cap_and_cover_about_twenty,
        test_archive_labels_reserved_and_open_urls,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed.append(t.__name__)
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
