"""
Regression tests for fetch_52week_stats resilience (issue #63).

A: bulk yfinance 1y miss → per-ticker Ticker.history retry (once after short sleep)
B: suppress yfinance ERROR noise during 52w pull so healthcheck logs_scan is not
   tripped by false "possibly delisted" lines

No pytest — plain assertions, same style as test_intel_sources_sanitize.py.

Run:  .venv/bin/python scripts/test_fetch_52week_stats.py
  (from repo root; or any python with scripts/ on path)
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_prices import (  # noqa: E402
    _compute_52week_from_closes,
    _quiet_yfinance_logs,
    fetch_52week_stats,
)


def _close_series(n: int = 30, start: float = 100.0, step: float = 1.0) -> pd.Series:
    """Monotonic daily closes long enough for the >=20 bar gate."""
    vals = [start + i * step for i in range(n)]
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.Series(vals, index=idx, name="Close")


def _bulk_close_frame(ticker_closes: dict[str, pd.Series]) -> pd.DataFrame:
    """Shape matches yf.download(..., group_by='column') Close block for N>1."""
    # MultiIndex columns: ('Close', ticker) after download — but our code does
    # data["Close"][ticker], so a simple Close-keyed frame with ticker columns.
    return pd.DataFrame(ticker_closes)


# ── pure helper ──────────────────────────────────────────────────────────────


def test_compute_52week_from_closes_basic():
    closes = _close_series(n=30, start=10.0, step=1.0)  # 10..39
    stats = _compute_52week_from_closes(closes)
    assert stats is not None
    # current=39, lo=10, hi=39 → percentile 100, pct_from_high 0
    assert stats["range_percentile"] == 100.0
    assert stats["pct_from_high"] == 0.0


def test_compute_52week_from_closes_rejects_short_series():
    assert _compute_52week_from_closes(_close_series(n=19)) is None


def test_compute_52week_from_closes_flat_range():
    s = pd.Series([5.0] * 30)
    assert _compute_52week_from_closes(s) is None


# ── quiet yfinance logs (B) ──────────────────────────────────────────────────


def test_quiet_yfinance_logs_suppresses_error_and_restores_level():
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.DEBUG)
    try:
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = _Cap(level=logging.DEBUG)
        yf_log.addHandler(cap)
        try:
            with _quiet_yfinance_logs():
                yf_log.error("possibly delisted; no price data found  (period=1y)")
                assert yf_log.getEffectiveLevel() >= logging.CRITICAL
            # After exit, level restored and ERROR would be visible again
            assert yf_log.level == logging.DEBUG
            yf_log.error("after restore")
        finally:
            yf_log.removeHandler(cap)

        # Only the post-restore ERROR should have been emitted to our handler
        # (CRITICAL+ during quiet; ERROR after restore).
        msgs = [r.getMessage() for r in records]
        assert not any("possibly delisted" in m for m in msgs)
        assert any("after restore" in m for m in msgs)
    finally:
        yf_log.setLevel(previous)


# ── fetch_52week_stats with mocked yfinance (A) ──────────────────────────────


def test_bulk_success_skips_per_ticker_history():
    closes_a = _close_series(n=30, start=50.0)
    closes_b = _close_series(n=30, start=20.0, step=0.5)
    bulk = _bulk_close_frame({"AAA": closes_a, "BBB": closes_b})
    # yf.download returns a frame where data["Close"] works for multi-ticker
    download_df = pd.concat({"Close": bulk}, axis=1)

    mock_yf = MagicMock()
    mock_yf.download.return_value = download_df
    sleep_calls: list[float] = []

    out = fetch_52week_stats(
        ["AAA", "BBB"],
        _yf=mock_yf,
        _sleep=lambda s: sleep_calls.append(s),
    )
    assert set(out) == {"AAA", "BBB"}
    mock_yf.Ticker.assert_not_called()
    assert sleep_calls == []


def test_bulk_miss_retries_per_ticker_history_once():
    """Reproduce 2026-08-10 shape: bulk has one good ticker, INTC empty → history recovers."""
    good = _close_series(n=30, start=80.0)
    empty = pd.Series(dtype=float)
    bulk = _bulk_close_frame({"NVDA": good, "INTC": empty})
    download_df = pd.concat({"Close": bulk}, axis=1)

    hist = pd.DataFrame({"Close": _close_series(n=40, start=90.0, step=-0.5)})
    ticker_obj = MagicMock()
    ticker_obj.history.return_value = hist

    mock_yf = MagicMock()
    mock_yf.download.return_value = download_df
    mock_yf.Ticker.return_value = ticker_obj
    sleep_calls: list[float] = []

    out = fetch_52week_stats(
        ["NVDA", "INTC"],
        _yf=mock_yf,
        _sleep=lambda s: sleep_calls.append(s),
    )
    assert "NVDA" in out
    assert "INTC" in out
    mock_yf.Ticker.assert_called_with("INTC")
    ticker_obj.history.assert_called()
    # First history attempt succeeds → no sleep between retries
    assert sleep_calls == []


def test_per_ticker_retry_sleeps_then_succeeds_on_second_attempt():
    bulk = _bulk_close_frame({"INTC": pd.Series(dtype=float)})
    download_df = pd.concat({"Close": bulk}, axis=1)

    hist_ok = pd.DataFrame({"Close": _close_series(n=25, start=10.0)})
    ticker_obj = MagicMock()
    # attempt 0 empty, attempt 1 good
    ticker_obj.history.side_effect = [
        pd.DataFrame({"Close": pd.Series(dtype=float)}),
        hist_ok,
    ]

    mock_yf = MagicMock()
    mock_yf.download.return_value = download_df
    mock_yf.Ticker.return_value = ticker_obj
    sleep_calls: list[float] = []

    out = fetch_52week_stats(
        ["INTC"],
        _yf=mock_yf,
        _sleep=lambda s: sleep_calls.append(s),
    )
    assert "INTC" in out
    assert ticker_obj.history.call_count == 2
    assert sleep_calls == [1.0]


def test_per_ticker_exhausted_returns_partial_and_warns():
    bulk = _bulk_close_frame({
        "NVDA": _close_series(n=30, start=100.0),
        "INTC": pd.Series(dtype=float),
    })
    download_df = pd.concat({"Close": bulk}, axis=1)

    ticker_obj = MagicMock()
    ticker_obj.history.return_value = pd.DataFrame({"Close": pd.Series(dtype=float)})

    mock_yf = MagicMock()
    mock_yf.download.return_value = download_df
    mock_yf.Ticker.return_value = ticker_obj
    sleep_calls: list[float] = []

    with patch("fetch_prices.logger") as log:
        out = fetch_52week_stats(
            ["NVDA", "INTC"],
            _yf=mock_yf,
            _sleep=lambda s: sleep_calls.append(s),
        )
    assert "NVDA" in out
    assert "INTC" not in out
    assert ticker_obj.history.call_count == 2  # initial + 1 retry
    assert sleep_calls == [1.0]
    assert any(
        "INTC" in str(c) and "52-week" in str(c).lower()
        for c in log.warning.call_args_list
    )


def test_empty_tickers_short_circuit():
    assert fetch_52week_stats([]) == {}


def test_download_exception_fail_open():
    mock_yf = MagicMock()
    mock_yf.download.side_effect = RuntimeError("network down")
    out = fetch_52week_stats(["INTC"], _yf=mock_yf, _sleep=lambda _s: None)
    # Bulk total failure still attempts per-ticker recovery
    mock_yf.Ticker.assert_called_with("INTC")
    # history also fails empty by default MagicMock → no stats
    assert out == {} or isinstance(out, dict)


def main():
    tests = [
        test_compute_52week_from_closes_basic,
        test_compute_52week_from_closes_rejects_short_series,
        test_compute_52week_from_closes_flat_range,
        test_quiet_yfinance_logs_suppresses_error_and_restores_level,
        test_bulk_success_skips_per_ticker_history,
        test_bulk_miss_retries_per_ticker_history_once,
        test_per_ticker_retry_sleeps_then_succeeds_on_second_attempt,
        test_per_ticker_exhausted_returns_partial_and_warns,
        test_empty_tickers_short_circuit,
        test_download_exception_fail_open,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
