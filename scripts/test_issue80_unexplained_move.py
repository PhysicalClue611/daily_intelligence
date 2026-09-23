"""Issue #80: a multi-day move still gets its own catalyst query when today is quiet.

Frozen from the 2026-09-22 run. Yahoo's daily bulk that evening still ended on
2026-09-21 (issue #69). Official adjusted closes through that session:

    09-14 97.190002   09-15 97.139999   09-16 101.050003
    09-17 108.800003  09-18 108.599998  09-21 121.779999

Context log the same day: AM premarket -1.28% (not an anomaly), 5-day +25.30%;
PM close $123.86, day +1.71% (not an anomaly), 5-day +27.51%.
3-day AM is (121.779999/101.050003-1) = +20.51% so the 3-day rule fires.
3-day PM is (123.86/108.800003-1) = +13.84%, under 15%, so the 5-day rule fires.

No pytest. Run:
  .venv/bin/python scripts/test_issue80_unexplained_move.py
"""
from __future__ import annotations

import inspect
import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_finance as rf
from fetch_prices import PriceRow, fetch_prices

TODAY = "2026-09-22"
REPORT = date(2026, 9, 22)
_ET = "America/New_York"

# Adjusted daily closes, trading days only. No 09-22 bar: that is the #69 shape.
_INTC = [
    ("2026-09-10", 100.320000),
    ("2026-09-11", 102.940002),
    ("2026-09-14", 97.190002),
    ("2026-09-15", 97.139999),
    ("2026-09-16", 101.050003),
    ("2026-09-17", 108.800003),
    ("2026-09-18", 108.599998),
    ("2026-09-21", 121.779999),
]
_NVDA = [
    ("2026-09-10", 218.360001),
    ("2026-09-11", 218.289993),
    ("2026-09-14", 210.960007),
    ("2026-09-15", 212.169998),
    ("2026-09-16", 213.899994),
    ("2026-09-17", 219.339996),
    ("2026-09-18", 222.270004),
    ("2026-09-21", 227.380005),
]
_PM_CLOSE = 123.86  # context log 2026-09-22 20:10 ET, intraday regular close
_AM_PREV = 121.779999
_AM_PREMARKET = _AM_PREV * (1 - 1.28 / 100)


def _series(rows: list[tuple[str, float]]) -> pd.Series:
    idx = pd.to_datetime([d for d, _ in rows])
    return pd.Series([p for _, p in rows], index=idx, dtype=float)


def _daily_frame() -> pd.DataFrame:
    idx = pd.to_datetime([d for d, _ in _INTC])
    close = pd.DataFrame(
        {"INTC": [p for _, p in _INTC], "NVDA": [p for _, p in _NVDA]},
        index=idx,
    )
    return pd.concat({"Close": close, "Open": close.copy()}, axis=1)


def _intraday(slot: str) -> pd.DataFrame:
    if slot == "am":
        bars = {
            "INTC": [("08:00", _AM_PREMARKET, _AM_PREMARKET)],
            "NVDA": [("08:00", 227.0, 227.0)],
        }
    else:
        bars = {
            "INTC": [("09:30", 120.0, 120.2), ("16:00", 123.0, _PM_CLOSE)],
            "NVDA": [("09:30", 227.0, 227.2), ("16:00", 228.0, 228.79)],
        }
    times = sorted({t for rows in bars.values() for t, _, _ in rows})
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-09-22 {t}", tz=_ET) for t in times])
    close_cols, open_cols = {}, {}
    for ticker, rows in bars.items():
        by_time = {t: (o, c) for t, o, c in rows}
        close_cols[ticker] = [by_time[t][1] for t in times]
        open_cols[ticker] = [by_time[t][0] for t in times]
    close = pd.DataFrame(close_cols, index=idx)
    return pd.concat({"Close": close, "Open": pd.DataFrame(open_cols, index=idx)}, axis=1)


def _fetch(slot: str):
    mock_yf = MagicMock()

    def fake_download(*_a, **kwargs):
        if kwargs.get("interval") == "1m":
            return _intraday(slot)
        return _daily_frame()

    mock_yf.download.side_effect = fake_download
    rows = fetch_prices(
        stocks=["INTC", "NVDA"],
        commodities=[],
        fx=[],
        thresholds={"stock_pct": 3.0},
        slot=slot,
        report_date=REPORT,
        _yf=mock_yf,
        _sleep=lambda _s: None,
    )
    return {r.ticker: r for r in rows}


def _row(ticker, change_pct, week=0.0, change_3d=0.0, change_5d=None, anomaly=False, price=100.0):
    return PriceRow(
        ticker=ticker,
        display=ticker,
        price=price,
        prev_close=100.0,
        change_pct=change_pct,
        week_change_pct=week,
        is_anomaly=anomaly,
        unit="$",
        change_3d_pct=change_3d,
        change_5d_pct=change_5d if change_5d is not None else week,
    )


def test_am_20260922_intc_is_quiet_today_but_3day_move_is_queued():
    rows = _fetch("am")
    intc = rows["INTC"]
    assert intc.is_anomaly is False
    assert abs(intc.change_pct - (-1.28)) < 0.02
    assert abs(intc.change_3d_pct - 20.5146) < 0.02
    assert abs(intc.week_change_pct - 25.3004) < 0.02

    moves = rf._compute_multiday_moves(list(rows.values()), slot="am")
    jobs = rf._unexplained_move_search_jobs(
        list(rows.values()),
        moves,
        covered_tickers=set(),
        run_slot="am",
        today_et=TODAY,
        query_days=2,
    )
    assert [j["_unexplained_move_ticker"] for j in jobs] == ["INTC"]
    assert jobs[0]["query"] == (
        "INTC stock surged 20.5% over 3 trading days reason catalyst 2026-09-22"
    )
    assert jobs[0]["search_depth"] == "basic"
    assert jobs[0]["max_results"] == 15
    assert "resolved" not in jobs[0]
    # 3 trading sessions before the 09-21 close, plus 2 calendar days.
    # Must not collapse to query_days=2, and must not inherit a same-day range.
    assert jobs[0]["start_date"] == "2026-09-14"
    assert jobs[0]["end_date"] == "2026-09-22"
    assert jobs[0]["days"] >= 8


def test_pm_20260922_intc_uses_5day_window_when_3day_is_under_15():
    rows = _fetch("pm")
    intc = rows["INTC"]
    assert intc.is_anomaly is False
    assert abs(intc.change_pct - 1.7082) < 0.02
    assert abs(intc.change_3d_pct - 13.8419) < 0.02
    assert abs(intc.week_change_pct - 27.5063) < 0.02

    moves = rf._compute_multiday_moves(
        list(rows.values()),
        closes_daily={"INTC": _series(_INTC), "NVDA": _series(_NVDA)},
        report_date=REPORT,
        slot="pm",
    )
    assert abs(moves["INTC"][0] - 13.8419) < 0.02
    assert abs(moves["INTC"][1] - 27.5063) < 0.02
    jobs = rf._unexplained_move_search_jobs(
        list(rows.values()),
        moves,
        covered_tickers=set(),
        run_slot="pm",
        today_et=TODAY,
        query_days=1,
    )
    assert jobs[0]["_unexplained_move_ticker"] == "INTC"
    assert jobs[0]["query"] == (
        "INTC stock surged 27.5% over 5 trading days reason catalyst 2026-09-22"
    )
    # 5 sessions before 09-22 is 09-15; buffer pushes the published-date start to 09-13.
    assert jobs[0]["start_date"] == "2026-09-13"
    assert jobs[0]["end_date"] == "2026-09-22"
    assert jobs[0]["days"] > 1


def test_pm_ignores_a_stale_today_bar_and_uses_the_intraday_close():
    """Batch last row must not be treated as today's close (issue #69)."""
    stale = _series(_INTC + [("2026-09-22", _AM_PREV)])
    row = _row("INTC", 1.71, price=_PM_CLOSE)
    moves = rf._compute_multiday_moves(
        [row],
        closes_daily={"INTC": stale},
        report_date=REPORT,
        slot="pm",
    )
    assert abs(moves["INTC"][1] - 27.5063) < 0.02
    assert abs(moves["INTC"][1] - 25.30) > 1.0


def test_anomaly_covered_ticker_is_not_queued_again():
    rows = [
        _row("INTC", 1.71, week=27.5, change_3d=13.8),
        _row("AMKR", 1.0, week=22.0, change_3d=10.0),
    ]
    moves = {"INTC": (13.8, 27.5), "AMKR": (10.0, 22.0)}
    anomaly_jobs = rf._anomaly_search_jobs(
        [_row("INTC", 11.86, anomaly=True)],
        run_slot="pm",
        today_et=TODAY,
        query_days=1,
    )
    covered = set(rf._anomaly_tickers_from_jobs(anomaly_jobs))
    jobs = rf._unexplained_move_search_jobs(
        rows, moves, covered_tickers=covered, run_slot="pm",
        today_et=TODAY, query_days=1,
    )
    tickers = [j["_unexplained_move_ticker"] for j in jobs]
    assert tickers == ["AMKR"]
    assert "dropped" not in jobs[0]["query"]
    assert "surged 22.0% over 5 trading days" in jobs[0]["query"]


def test_max_jobs_is_2_and_largest_abs_move_wins():
    rows = [
        _row("NVDA", 0.5, change_3d=16.0),
        _row("QCOM", -0.4, change_3d=-28.0),
        _row("AMKR", 0.2, change_3d=18.0),
    ]
    moves = {"NVDA": (16.0, 4.0), "QCOM": (-28.0, -10.0), "AMKR": (18.0, 9.0)}
    jobs = rf._unexplained_move_search_jobs(
        rows, moves, covered_tickers=set(), run_slot="am",
        today_et=TODAY, query_days=3, max_jobs=2,
    )
    assert [j["_unexplained_move_ticker"] for j in jobs] == ["QCOM", "AMKR"]
    assert jobs[0]["query"].startswith("QCOM stock dropped 28.0% over 3 trading days")
    assert len(jobs) == 2


def test_excludes_commodity_fx_index_etf_and_aaoi():
    names = ["AAOI", "QQQM", "VOO", "EWJ", "GC=F", "CL=F", "^TNX",
             "USDCNY=X", "USDJPY=X", "DX-Y.NYB", "INTC"]
    rows = [_row(t, 0.1, week=40.0, change_3d=30.0) for t in names]
    moves = {t: (30.0, 40.0) for t in names}
    jobs = rf._unexplained_move_search_jobs(
        rows, moves, covered_tickers=set(), run_slot="pm",
        today_et=TODAY, query_days=2,
    )
    assert [j["_unexplained_move_ticker"] for j in jobs] == ["INTC"]


def test_publication_window_overrides_last_report_range():
    """The search loop's last-report default must not replace this job's dates."""
    row = _row("INTC", 1.71, week=27.5, change_3d=13.8, change_5d=27.5, price=_PM_CLOSE)
    moves = {"INTC": (13.8, 27.5)}
    jobs = rf._unexplained_move_search_jobs(
        [row], moves, covered_tickers=set(), run_slot="pm",
        today_et=TODAY, query_days=1,
    )
    start, end, days = rf._job_search_bounds(
        jobs[0], default_start="2026-09-22", default_end="2026-09-22", default_days=1,
    )
    assert start == "2026-09-13"
    assert end == "2026-09-22"
    assert days > 1
    payload_uses_dates = bool(start and end)
    assert payload_uses_dates


def test_quiet_book_still_runs_when_a_multiday_move_is_eligible():
    row = _row("INTC", -1.28, change_3d=20.5, change_5d=25.3)
    moves = rf._compute_multiday_moves([row], slot="am")
    jobs = rf._unexplained_move_search_jobs(
        [row], moves, covered_tickers=set(), run_slot="am", today_et=TODAY, query_days=1,
    )
    assert jobs and jobs[0]["_unexplained_move_ticker"] == "INTC"
    assert rf._should_skip_no_signal(False, [], jobs) is False
    assert rf._should_skip_no_signal(False, [], []) is True
    src = inspect.getsource(rf._main_body)
    build = src.find("unexplained_jobs = _unexplained_move_search_jobs")
    gate = src.find("_should_skip_no_signal")
    assert build != -1 and gate != -1 and build < gate


def test_short_history_is_not_labeled_as_a_full_window():
    from fetch_prices import multiday_return_pct
    before = pd.Series(
        [80.0, 90.0],
        index=pd.to_datetime(["2026-09-18", "2026-09-21"]),
    )
    assert multiday_return_pct(100.0, before, 5, allow_short=True) == 25.0
    assert multiday_return_pct(100.0, before, 3, allow_short=False) is None
    assert multiday_return_pct(100.0, before, 5, allow_short=False) is None

    # 4 prior closes: 3-day anchor exists and is small; a 5-day fallback to
    # the oldest close would be +102% and must not become a 5-day trigger.
    closes = pd.Series(
        [50.0, 98.0, 99.0, 100.0],
        index=pd.to_datetime(["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"]),
    )
    row = _row("INTC", 1.0, week=102.0, change_3d=None, change_5d=None, price=101.0)
    moves = rf._compute_multiday_moves(
        [row], closes_daily={"INTC": closes}, report_date=REPORT, slot="pm",
    )
    assert moves["INTC"][0] is not None and abs(moves["INTC"][0]) < 15
    assert moves["INTC"][1] is None
    jobs = rf._unexplained_move_search_jobs(
        [row], moves, covered_tickers=set(), run_slot="pm", today_et=TODAY, query_days=1,
    )
    assert jobs == []


def test_pass1_prompt_names_unexplained_tickers():
    assert "{unexplained_move_note}" in rf.USER_PROMPT_TEMPLATE
    note = rf._build_unexplained_move_note(["INTC"])
    filled = rf.USER_PROMPT_TEMPLATE.format(
        date=TODAY,
        now_str="2026-09-22 08:30 EDT",
        last_report_date="2026-09-21",
        query_days=2,
        triggered_geo_topics="none",
        pm_afterhours_note="",
        price_data_label="premarket",
        price_table="x",
        price_missing_note="",
        news_text="",
        finnhub_news_section="",
        brave_news_section="",
        sonar_macro_section="",
        social_sentiment_section="",
        tavily_section="",
        kb_section="",
        calibration_notes="",
        verifiable_signals_rule="",
        anomaly_tickers_note="",
        unexplained_move_note=note,
    )
    assert "INTC" in filled
    assert "不需要你重复建议同名 ticker" in note


def run():
    tests = [
        test_am_20260922_intc_is_quiet_today_but_3day_move_is_queued,
        test_pm_20260922_intc_uses_5day_window_when_3day_is_under_15,
        test_pm_ignores_a_stale_today_bar_and_uses_the_intraday_close,
        test_anomaly_covered_ticker_is_not_queued_again,
        test_max_jobs_is_2_and_largest_abs_move_wins,
        test_excludes_commodity_fx_index_etf_and_aaoi,
        test_publication_window_overrides_last_report_range,
        test_quiet_book_still_runs_when_a_multiday_move_is_eligible,
        test_short_history_is_not_labeled_as_a_full_window,
        test_pass1_prompt_names_unexplained_tickers,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  ok  {test.__name__}")
        except Exception as exc:
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
