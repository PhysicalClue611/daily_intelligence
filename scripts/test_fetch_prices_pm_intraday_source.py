"""
Regression tests for issue #69: PM "today's close" must come from intraday
1m data, not the daily bulk's last row.

At 20:10 ET (PM report run time), yfinance's daily bulk (period=8d,
interval=1d) sometimes hasn't landed today's row yet — the old code silently
treated the batch's last row (= yesterday) as "today", producing
price == prev_close and change_pct == 0.00% for every ticker while the
correct data was sitting unused in the already-fetched intraday
(period=2d, interval=1m, prepost=True) download.

No pytest — plain assertions, same style as test_fetch_prices_yfinance_noise.py.

Run:  .venv/bin/python scripts/test_fetch_prices_pm_intraday_source.py
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_prices as fp  # noqa: E402
from fetch_prices import fetch_prices  # noqa: E402

_ET = "America/New_York"


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, name: str | None = None) -> list[str]:
        return [r.getMessage() for r in self.records if name is None or r.name == name]


def _attach(logger_name: str):
    log = logging.getLogger(logger_name)
    previous = log.level
    log.setLevel(logging.DEBUG)
    cap = _Cap()
    log.addHandler(cap)
    return log, cap, previous


def _detach(log, cap, previous):
    log.removeHandler(cap)
    log.setLevel(previous)


# ── fixtures ──────────────────────────────────────────────────────────────────

# Daily bulk: 8 business days ending 2026-09-02 (Wed) — simulates the exact
# #69 bug precondition: report_date is 2026-09-03, but the batch's last row
# is still 2026-09-02 (yesterday). No "today" row exists in the batch at all.
_DAILY_CLOSES = {
    "INTC": [90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0],
    "AMKR": [40.0, 41.0, 42.0, 43.0, 44.0, 45.0, 46.0, 47.0],
}
_REPORT_DATE = pd.Timestamp("2026-09-03").date()


def _daily_frame():
    idx = pd.bdate_range(end="2026-09-02", periods=8)
    close = pd.DataFrame(_DAILY_CLOSES, index=idx)
    open_ = close.copy()  # Open unused by the PM path now; only shape matters
    return pd.concat({"Close": close, "Open": open_}, axis=1)


def _intraday_frame(bars: dict[str, list[tuple[str, float, float]]]) -> pd.DataFrame:
    """bars: {ticker: [(HH:MM, open, close), ...]} on 2026-09-03, tz=America/New_York."""
    all_times = sorted({t for rows in bars.values() for t, _, _ in rows})
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"2026-09-03 {t}", tz=_ET) for t in all_times]
    )
    close_cols, open_cols = {}, {}
    for ticker, rows in bars.items():
        by_time = {t: (o, c) for t, o, c in rows}
        close_cols[ticker] = [by_time.get(t, (float("nan"), float("nan")))[1] for t in all_times]
        open_cols[ticker] = [by_time.get(t, (float("nan"), float("nan")))[0] for t in all_times]
    close = pd.DataFrame(close_cols, index=idx)
    open_ = pd.DataFrame(open_cols, index=idx)
    return pd.concat({"Close": close, "Open": open_}, axis=1)


def _run_pm(daily_df, intraday_df):
    mock_yf = MagicMock()

    def fake_download(*_a, **kwargs):
        if kwargs.get("interval") == "1m":
            return intraday_df
        return daily_df

    mock_yf.download.side_effect = fake_download
    return fetch_prices(
        stocks=["INTC", "AMKR"],
        commodities=[],
        fx=[],
        thresholds={"stock_pct": 3.0},
        slot="pm",
        report_date=_REPORT_DATE,
        _yf=mock_yf,
        _sleep=lambda _s: None,
    )


# ── scenario 1: daily bulk missing today, intraday has it ─────────────────────


def test_pm_uses_intraday_close_when_daily_bulk_lacks_today():
    daily_df = _daily_frame()
    intraday_df = _intraday_frame({
        # 04:00 premarket bar included on purpose (review finding, PR #70): with
        # prepost=True this bar sorts before the 09:30 RTH open, so if the RTH filter
        # regressed to unbounded "<=16:00", .iloc[0] would wrongly pick 170.0 as "today's
        # open" instead of the real RTH open (97.0).
        "INTC": [("04:00", 170.0, 170.5), ("09:30", 97.0, 97.5), ("16:00", 104.0, 105.0), ("16:30", 105.0, 106.0)],
        "AMKR": [("04:00", 80.0, 80.2), ("09:30", 47.0, 47.2), ("16:00", 48.0, 49.0), ("16:30", 49.0, 49.5)],
    })
    rows = _run_pm(daily_df, intraday_df)
    by_ticker = {r.ticker: r for r in rows}

    assert set(by_ticker) == {"INTC", "AMKR"}

    intc = by_ticker["INTC"]
    assert intc.price == 105.0            # today's regular-session close, from intraday
    assert intc.prev_close == 97.0        # daily bulk's last (=yesterday) row
    assert round(intc.change_pct, 4) == round((105.0 - 97.0) / 97.0 * 100, 4)
    assert intc.change_pct != 0.0         # the #69 bug produced 0.00% here
    # session_change_pct must use the 09:30 RTH open (97.0), NOT the 04:00 premarket
    # open (170.0) that also satisfies "time <= 16:00".
    assert round(intc.session_change_pct, 4) == round((105.0 - 97.0) / 97.0 * 100, 4)
    assert intc.afterhours_price == 106.0
    assert round(intc.afterhours_pct, 4) == round((106.0 - 105.0) / 105.0 * 100, 4)
    # week_change_pct: 5 trading days back from _closes_prev (excludes any "today" row)
    # _closes_prev = [90,91,92,93,94,95,96,97]; iloc[-5] = 93.0
    assert round(intc.week_change_pct, 4) == round((105.0 - 93.0) / 93.0 * 100, 4)


# ── scenario 2: both daily bulk AND intraday lack today ───────────────────────


def test_pm_skips_ticker_and_warns_when_both_sources_lack_today():
    daily_df = _daily_frame()
    # Intraday present but with NO bars on 2026-09-03 at all (simulates the
    # theoretical double-miss edge case) — reuse a frame dated one day earlier.
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-02 09:30", tz=_ET)])
    close = pd.DataFrame({"INTC": [97.5], "AMKR": [47.2]}, index=idx)
    intraday_df = pd.concat({"Close": close, "Open": close.copy()}, axis=1)

    our_log, our_cap, our_prev = _attach(fp.logger.name)
    try:
        rows = _run_pm(daily_df, intraday_df)
    finally:
        _detach(our_log, our_cap, our_prev)

    # Neither ticker got a "today" close from any source — must not fall back
    # to any stale value (no row emitted, no invented price).
    assert rows == []
    msgs = our_cap.messages()
    for ticker in ("INTC", "AMKR"):
        assert any(
            f"{ticker} PM: no intraday regular-session close available, price unavailable" in m
            for m in msgs
        ), f"missing hard-failure WARNING for {ticker}: {msgs}"


# ── scenario 3: whole-table miss must NOT trigger a Finnhub refill for PM ─────


def test_pm_stays_empty_on_whole_table_miss_even_with_finnhub_available():
    """Review finding (PR #70): if every ticker hits the per-ticker intraday-miss
    hard-fail, `rows` ends up empty and the function-level `if not rows:` tail must
    not silently refill via Finnhub for slot="pm" — that would defeat the
    no-silent-substitute contract and skip run_finance.py's price-citation ban.
    """
    daily_df = _daily_frame()
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-02 09:30", tz=_ET)])
    close = pd.DataFrame({"INTC": [97.5], "AMKR": [47.2]}, index=idx)
    intraday_df = pd.concat({"Close": close, "Open": close.copy()}, axis=1)

    def finnhub_would_refill(*_a, **_k):
        raise AssertionError(
            "Finnhub fallback must not be called for a PM whole-table intraday miss"
        )

    with patch.object(fp, "_fetch_prices_finnhub", side_effect=finnhub_would_refill):
        rows = _run_pm(daily_df, intraday_df)

    assert rows == []


if __name__ == "__main__":
    tests = [
        test_pm_uses_intraday_close_when_daily_bulk_lacks_today,
        test_pm_skips_ticker_and_warns_when_both_sources_lack_today,
        test_pm_stays_empty_on_whole_table_miss_even_with_finnhub_available,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
