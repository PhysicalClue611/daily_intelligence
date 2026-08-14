"""
Regression tests for fetch_prices yfinance ERROR noise (issue #67).

#63 only quieted yfinance during fetch_52week_stats (period=1y). The 2026-08-13
AM healthcheck trip was period=8d + period=2d on the main price path.

No pytest — plain assertions, same style as test_fetch_52week_stats.py.

Run:  .venv/bin/python scripts/test_fetch_prices_yfinance_noise.py
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_prices as fp  # noqa: E402
from fetch_prices import (  # noqa: E402
    PriceRow,
    _fetch_intraday_data,
    fetch_prices,
)


def _ohlc_frame(ticker_closes: dict[str, list[float]], start="2026-08-06") -> pd.DataFrame:
    """Shape matches yf.download(..., group_by='column') for N>=1: data['Close'][t]."""
    n = len(next(iter(ticker_closes.values())))
    idx = pd.date_range(start, periods=n, freq="B")
    close = pd.DataFrame(
        {t: vals for t, vals in ticker_closes.items()},
        index=idx,
    )
    # Open ≈ prior close; unused by slot=daily except as a parallel series
    open_ = close.copy()
    return pd.concat({"Close": close, "Open": open_}, axis=1)


def _empty_ohlc(tickers: list[str], n: int = 5) -> pd.DataFrame:
    return _ohlc_frame({t: [float("nan")] * n for t in tickers})


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, name: str | None = None) -> list[str]:
        out = []
        for r in self.records:
            if name is None or r.name == name:
                out.append(r.getMessage())
        return out


def _attach(logger_name: str) -> tuple[logging.Logger, _Cap, int]:
    log = logging.getLogger(logger_name)
    previous = log.level
    log.setLevel(logging.DEBUG)
    cap = _Cap()
    log.addHandler(cap)
    return log, cap, previous


def _detach(log: logging.Logger, cap: _Cap, previous: int) -> None:
    log.removeHandler(cap)
    log.setLevel(previous)


# ── daily bulk: suppress yfinance ERROR ──────────────────────────────────────


_TWO = {"INTC": [90.0, 91.0, 92.0, 93.0, 94.0], "AMKR": [50.0, 51.0, 52.0, 53.0, 54.0]}


def _run_daily(download_fn, *, sleep_fn=lambda _s: None, finnhub=None):
    mock_yf = MagicMock()
    mock_yf.download.side_effect = download_fn
    kwargs = dict(
        stocks=["INTC", "AMKR"],
        commodities=[],
        fx=[],
        thresholds={"stock_pct": 3.0},
        slot="daily",
        _sleep=sleep_fn,
        _yf=mock_yf,
    )
    if finnhub is None:
        return fetch_prices(**kwargs), mock_yf
    with patch.object(fp, "_finnhub_single_ticker", side_effect=finnhub):
        return fetch_prices(**kwargs), mock_yf


def test_daily_download_does_not_emit_yfinance_error():
    yf_log, yf_cap, yf_prev = _attach("yfinance")
    try:

        def fake_download(*_a, **_k):
            yf_log.error(
                "$INTC: possibly delisted; no price data found  (period=8d)"
            )
            return _ohlc_frame(_TWO)

        rows, _ = _run_daily(fake_download)
        assert {r.ticker for r in rows} == {"INTC", "AMKR"}
        assert not any("possibly delisted" in m for m in yf_cap.messages())
        assert not any(r.levelno >= logging.ERROR for r in yf_cap.records)
    finally:
        _detach(yf_log, yf_cap, yf_prev)


def test_daily_bulk_retries_once_when_incomplete():
    calls: list[int] = []
    slept: list[float] = []

    def fake_download(*_a, **_k):
        calls.append(1)
        logging.getLogger("yfinance").error(
            "['INTC']: possibly delisted; no price data found  (period=8d)"
        )
        if len(calls) == 1:
            return _empty_ohlc(["INTC", "AMKR"])
        return _ohlc_frame(_TWO)

    yf_log, yf_cap, yf_prev = _attach("yfinance")
    try:
        rows, _ = _run_daily(fake_download, sleep_fn=lambda s: slept.append(s))
        assert len(calls) == 2
        assert slept == [1.0]
        assert {r.ticker: r.price for r in rows} == {"INTC": 94.0, "AMKR": 54.0}
        assert not any("possibly delisted" in m for m in yf_cap.messages())
    finally:
        _detach(yf_log, yf_cap, yf_prev)


def test_daily_bulk_incomplete_after_retry_logs_our_warning_not_yfinance_error():
    def fallback(ticker, *_a, **_k):
        return PriceRow(
            ticker=ticker,
            display=ticker,
            price=100.0,
            prev_close=97.0,
            change_pct=3.09,
            week_change_pct=0.0,
            is_anomaly=True,
            unit="$",
            slot="daily",
        )

    calls: list[int] = []

    def fake_download(*_a, **_k):
        calls.append(1)
        logging.getLogger("yfinance").error(
            "['INTC']: possibly delisted; no price data found  (period=8d)"
        )
        return _empty_ohlc(["INTC", "AMKR"])

    yf_log, yf_cap, yf_prev = _attach("yfinance")
    our_log, our_cap, our_prev = _attach(fp.logger.name)
    try:
        rows, _ = _run_daily(fake_download, finnhub=fallback)
        assert len(calls) == 2
        assert {r.ticker for r in rows} == {"INTC", "AMKR"}
        assert all(r.price == 100.0 for r in rows)
        assert not any("possibly delisted" in m for m in yf_cap.messages())
        our_msgs = our_cap.messages()
        assert any("yfinance daily bulk" in m and "INTC" in m for m in our_msgs)
        assert any(
            r.levelno == logging.WARNING and "yfinance daily bulk" in r.getMessage()
            for r in our_cap.records
        )
    finally:
        _detach(yf_log, yf_cap, yf_prev)
        _detach(our_log, our_cap, our_prev)


def test_mixed_bulk_keeps_first_yahoo_rows_when_retry_worse():
    """Production shape: one Close series dead, the rest valid.

    A worse second bulk must not wipe AMKR's first Yahoo rows (Finnhub cannot
    fill futures/FX; even for stocks we should not throw away a good first hit).
    """
    calls: list[int] = []
    finnhub_tickers: list[str] = []

    def fake_download(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            return _ohlc_frame({
                "INTC": [float("nan")] * 5,
                "AMKR": [50.0, 51.0, 52.0, 53.0, 54.0],
            })
        return _empty_ohlc(["INTC", "AMKR"])

    def fallback(ticker, *_a, **_k):
        finnhub_tickers.append(ticker)
        return PriceRow(
            ticker=ticker,
            display=ticker,
            price=100.0,
            prev_close=97.0,
            change_pct=3.09,
            week_change_pct=0.0,
            is_anomaly=True,
            unit="$",
            slot="daily",
        )

    rows, _ = _run_daily(fake_download, finnhub=fallback)
    by_ticker = {r.ticker: r.price for r in rows}
    assert len(calls) == 2
    assert by_ticker["AMKR"] == 54.0
    assert by_ticker["INTC"] == 100.0
    assert finnhub_tickers == ["INTC"]


def test_mixed_bulk_merges_retry_recovery_without_dropping_first_hits():
    """Retry recovers INTC but loses AMKR — keep both via column merge."""
    calls: list[int] = []

    def fake_download(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            return _ohlc_frame({
                "INTC": [float("nan")] * 5,
                "AMKR": [50.0, 51.0, 52.0, 53.0, 54.0],
            })
        return _ohlc_frame({
            "INTC": [90.0, 91.0, 92.0, 93.0, 94.0],
            "AMKR": [float("nan")] * 5,
        })

    rows, _ = _run_daily(fake_download)
    assert len(calls) == 2
    assert {r.ticker: r.price for r in rows} == {"INTC": 94.0, "AMKR": 54.0}


def test_complete_daily_bulk_does_not_retry():
    calls: list[int] = []
    slept: list[float] = []

    def fake_download(*_a, **_k):
        calls.append(1)
        return _ohlc_frame(_TWO)

    rows, _ = _run_daily(fake_download, sleep_fn=lambda s: slept.append(s))
    assert len(calls) == 1
    assert slept == []
    assert len(rows) == 2


# ── intraday (period=2d) ─────────────────────────────────────────────────────


def test_intraday_download_does_not_emit_yfinance_error():
    yf_log, yf_cap, yf_prev = _attach("yfinance")
    try:

        def fake_download(*_a, **_k):
            yf_log.error(
                "['ORCL']: possibly delisted; no price data found  (period=2d)"
            )
            idx = pd.date_range("2026-08-12 09:30", periods=3, freq="min")
            close = pd.DataFrame({"ORCL": [100.0, 100.1, 100.2]}, index=idx)
            return pd.concat({"Close": close, "Open": close.copy()}, axis=1)

        mock_yf = MagicMock()
        mock_yf.download.side_effect = fake_download
        data = _fetch_intraday_data(["ORCL"], mock_yf)
        assert data is not None
        assert not any("possibly delisted" in m for m in yf_cap.messages())
        assert not any(r.levelno >= logging.ERROR for r in yf_cap.records)
    finally:
        _detach(yf_log, yf_cap, yf_prev)


def test_intraday_empty_logs_our_warning():
    our_log, our_cap, our_prev = _attach(fp.logger.name)
    yf_log, yf_cap, yf_prev = _attach("yfinance")
    try:

        def fake_download(*_a, **_k):
            yf_log.error("$VOO: possibly delisted; no price data found  (period=2d)")
            return pd.DataFrame()

        mock_yf = MagicMock()
        mock_yf.download.side_effect = fake_download
        data = _fetch_intraday_data(["VOO"], mock_yf)
        assert data is None
        assert not any("possibly delisted" in m for m in yf_cap.messages())
        assert any("Intraday download" in m for m in our_cap.messages())
    finally:
        _detach(our_log, our_cap, our_prev)
        _detach(yf_log, yf_cap, yf_prev)


if __name__ == "__main__":
    tests = [
        test_daily_download_does_not_emit_yfinance_error,
        test_daily_bulk_retries_once_when_incomplete,
        test_daily_bulk_incomplete_after_retry_logs_our_warning_not_yfinance_error,
        test_mixed_bulk_keeps_first_yahoo_rows_when_retry_worse,
        test_mixed_bulk_merges_retry_recovery_without_dropping_first_hits,
        test_complete_daily_bulk_does_not_retry,
        test_intraday_download_does_not_emit_yfinance_error,
        test_intraday_empty_logs_our_warning,
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
