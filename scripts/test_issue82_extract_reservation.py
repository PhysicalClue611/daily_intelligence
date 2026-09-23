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
        raw, ["AMKR", "INTC", "NVDA"], GEO, NOW,
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
    _, prescreened = rf._plan_reserved_and_open(raw, [], GEO, NOW)
    assert len(prescreened) == 25


def test_empty_must_answer_subset_reserves_nothing():
    raw = _scenario_20260922_pm()
    reserved = rf._select_reserved_results(raw, ["NVDA", "QCOM"], now=NOW)
    assert reserved == {}


def test_unexplained_move_results_get_the_7day_fence_and_ticker_tag():
    job = {
        "query": "INTC stock surged 27.5% over 5 trading days reason catalyst 2026-09-22",
        "_unexplained_move_ticker": "INTC",
        "_must_answer_ticker": "INTC",
    }
    fresh = _hit("fresh", "https://example.com/fresh", 0.2, source=None)
    stale = _hit(
        "stale", "https://example.com/stale", 0.9, source=None,
        published="2026-08-01T12:00:00-04:00",
    )
    out = rf._annotate_job_results(job, [fresh, stale], NOW, ["INTC"])
    assert [r["url"] for r in out] == ["https://example.com/fresh"]
    assert out[0]["_source_ticker"] == "INTC"


def test_fence_reads_must_answer_list_not_category_flags():
    """A new must-answer category needs no extra flag in the fence."""
    job = {"query": "NEWCO catalyst", "_must_answer_ticker": "NEWCO"}
    fresh = _hit("fresh", "https://example.com/fresh-new", 0.2)
    stale = _hit(
        "stale", "https://example.com/stale-new", 0.9,
        published="2026-08-01T12:00:00-04:00",
    )
    out = rf._annotate_job_results(job, [fresh, stale], NOW, ["NEWCO"])
    assert [r["url"] for r in out] == ["https://example.com/fresh-new"]
    assert out[0]["_source_ticker"] == "NEWCO"

    flagged = {
        "_anomaly_query": True,
        "_anomaly_ticker": "INTC",
        "_unexplained_move_ticker": "INTC",
    }
    both = rf._annotate_job_results(flagged, [dict(fresh), dict(stale)], NOW, ["AMKR"])
    assert [r["url"] for r in both] == [
        "https://example.com/fresh-new",
        "https://example.com/stale-new",
    ]
    assert all(r["_source_ticker"] is None for r in both)


def test_open_discovery_results_are_tagged_with_no_ticker():
    job = {"query": "Trump Takaichi Taiwan", "search_depth": "basic"}
    hit = _hit("Trump Takaichi Taiwan strait", "https://example.com/geo", 0.5)
    out = rf._annotate_job_results(job, [hit], NOW, ["INTC"])
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
        reserved, opened, "INTC", "Japan", {"used": 0}, _fake_extract,
    )
    assert calls == batches
    assert [r["url"] for r in got] == reserved + opened


INTC_URL = "https://www.reuters.com/technology/intel-packaging-2026-09-22"
GEO_URL = "https://www.reuters.com/world/geo-0"


def _capture_extract(anomaly, reserved, geo, open_urls):
    calls = []

    def _fake(urls, query, budget):
        calls.append((list(urls), query))
        return [{"url": u, "query": query} for u in urls]

    rf._extract_search_results(
        reserved, open_urls, anomaly, geo, {"used": 0}, _fake,
    )
    return calls


def test_quiet_multiday_extract_query_names_intc():
    """The Extract query is the must-answer list, not the anomaly-symbol list."""
    calls = _capture_extract(
        ["INTC"],
        {"INTC": {"url": INTC_URL, "score": 0.25}},
        "Japan Taiwan Iran",
        [GEO_URL],
    )
    assert calls[0] == ([INTC_URL], "INTC")
    assert "INTC" in calls[1][1]
    assert "Japan" in calls[1][1]
    assert calls[0][1] != calls[1][1]


def test_mixed_anomaly_and_unexplained_extract_queries():
    """Four must-answer names all survive. The old anomaly[:3] slice dropped the fourth."""
    amkr_url = "https://www.ft.com/content/amkr-group-move"
    calls = _capture_extract(
        ["AMKR", "QCOM", "TSLA", "INTC"],
        {
            "AMKR": {"url": amkr_url, "score": 0.22},
            "INTC": {"url": INTC_URL, "score": 0.25},
        },
        "Japan Taiwan Iran Hormuz " + ("padding " * 30),
        [GEO_URL],
    )
    def _query_for(url):
        matched = [query for urls, query in calls if url in urls]
        assert matched, url
        return matched[0]

    intc_q = _query_for(INTC_URL)
    geo_q = _query_for(GEO_URL)
    assert intc_q == "AMKR QCOM TSLA INTC"
    assert "Japan" not in intc_q
    assert geo_q.startswith("AMKR QCOM TSLA INTC")
    assert "Japan" in geo_q
    assert "CL=F" not in intc_q
    assert "CL=F" not in geo_q
    assert intc_q != geo_q
    assert len(geo_q) <= len("AMKR QCOM TSLA INTC ") + 80


def test_open_pool_keyword_bonus_uses_the_must_answer_list():
    raw = [
        _hit("INTC foundry shipment", "https://example.com/intc-note", 0.10, "INTC catalyst"),
        _hit("ordinary session recap", "https://example.com/recap", 0.12, "indices mixed"),
    ]
    _, prescreened = rf._plan_reserved_and_open(raw, ["INTC"], GEO, NOW)
    assert prescreened[0]["url"] == "https://example.com/intc-note"


def test_job_builders_feed_the_one_must_answer_list():
    from fetch_prices import PriceRow

    amkr = PriceRow(
        ticker="AMKR", display="AMKR", price=10.0, prev_close=9.0,
        change_pct=6.0, week_change_pct=1.0, is_anomaly=True, unit="$", slot="pm",
    )
    quiet = PriceRow(
        ticker="INTC", display="INTC", price=10.0, prev_close=10.0,
        change_pct=1.0, week_change_pct=27.5, is_anomaly=False, unit="$", slot="pm",
    )
    anomaly_jobs = rf._anomaly_search_jobs([amkr], "pm", "2026-09-22", 1)
    unexplained_jobs = rf._unexplained_move_search_jobs(
        [quiet], {"INTC": (10.0, 27.5)}, set(), "pm", "2026-09-22", 1,
    )
    assert rf._must_answer_tickers(
        anomaly_jobs + unexplained_jobs + [{"query": "Trump Takaichi"}]
    ) == ["AMKR", "INTC"]


def test_must_answer_list_ignores_category_fields():
    jobs = [
        {"_anomaly_ticker": "AMKR", "_anomaly_query": True, "_must_answer_ticker": "AMKR"},
        {"_unexplained_move_ticker": "INTC", "_must_answer_ticker": "INTC"},
        {"query": "rotation", "_rotation_ticker": "NVDA"},
        {"_anomaly_ticker": "QCOM", "_unexplained_move_ticker": "TSLA"},
    ]
    assert rf._must_answer_tickers(jobs) == ["AMKR", "INTC"]


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
        test_fence_reads_must_answer_list_not_category_flags,
        test_open_discovery_results_are_tagged_with_no_ticker,
        test_extract_batches_keep_ten_url_cap_and_cover_about_twenty,
        test_quiet_multiday_extract_query_names_intc,
        test_mixed_anomaly_and_unexplained_extract_queries,
        test_open_pool_keyword_bonus_uses_the_must_answer_list,
        test_job_builders_feed_the_one_must_answer_list,
        test_must_answer_list_ignores_category_fields,
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
