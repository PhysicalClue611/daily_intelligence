"""
Issue #76: PM Finnhub short-circuit swallowed anomaly Tavily jobs, and
fetch_finnhub_news pooled every ticker into one global top-15.

2026-09-21 PM: 6 anomalies, Finnhub covered 5, INTC +11.86% got no
attribution query. A quieter ticker's headlines also lose a global top-15.

Run: python scripts/test_issue76_finnhub_skip.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget_trackers as bt
import intel_sources as ins
import run_finance as rf
from fetch_prices import PriceRow

TODAY = "2026-09-21"


def _row(ticker, change_pct):
    return PriceRow(
        ticker=ticker,
        display=ticker,
        price=100.0,
        prev_close=100.0,
        change_pct=change_pct,
        week_change_pct=0.0,
        is_anomaly=True,
        unit="$",
        slot="pm",
    )


def _pm_anomalies():
    # Six movers. |change| order: INTC, QCOM, TSLA, AMKR, AAOI, NVDA.
    return [
        _row("NVDA", 2.1),
        _row("AAOI", 3.4),
        _row("AMKR", 4.2),
        _row("TSLA", -5.5),
        _row("QCOM", 6.8),
        _row("INTC", 11.86),
    ]


def test_pm_finnhub_coverage_still_queues_top3_anomaly_jobs():
    # 2026-09-21 PM had a non-empty Finnhub section and still must queue jobs.
    # The old gate lived in _main_body; it must not come back.
    import inspect
    body = inspect.getsource(rf._main_body)
    assert "skipping anomaly Tavily query" not in body
    assert "finnhub_covers" not in body
    jobs = rf._queue_anomaly_search_jobs(
        _pm_anomalies(),
        run_slot="pm",
        today_et=TODAY,
        query_days=1,
    )
    queries = [j["query"] for j in jobs]
    assert len(jobs) == 3, queries
    assert queries[0].startswith("INTC stock surge 11.9% afterhours reason 2026-09-21")
    assert queries[1].startswith("QCOM stock surge 6.8% afterhours reason 2026-09-21")
    assert queries[2].startswith("TSLA stock drop 5.5% afterhours reason 2026-09-21")
    assert all(j.get("_anomaly_query") for j in jobs)


def test_finnhub_keeps_five_per_ticker_and_cross_ticker_dedup():
    """5 tickers x 10 headlines. Four tickers are newer, so a global top-15
    drops AAOI entirely. AAOI's oldest-of-its-own item must survive, and a
    headline shared with INTC must appear once."""
    shared = "Intel AUO Micro LED packaging shared wire"
    payloads = {}
    base_new = 1_800_000_000
    base_old = 1_700_000_000
    for i, ticker in enumerate(["INTC", "QCOM", "TSLA", "AMKR"]):
        rows = []
        for n in range(10):
            headline = shared if (ticker == "INTC" and n == 0) else f"{ticker} headline {n}"
            rows.append({
                "headline": headline,
                "datetime": base_new + i * 100 + (10 - n),
                "source": "Reuters",
                "summary": f"sum {ticker} {n}",
            })
        payloads[ticker] = rows
    aaoi_rows = []
    for n in range(10):
        aaoi_rows.append({
            "headline": shared if n == 0 else f"AAOI headline {n}",
            "datetime": base_old + (10 - n),
            "source": "Digitimes",
            "summary": f"aaoi {n}",
        })
    payloads["AAOI"] = aaoi_rows

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _get(url, params=None, timeout=None):
        return _Resp(payloads[params["symbol"]])

    orig_get = ins.httpx.get
    orig_sleep = ins.time.sleep
    orig_key = ins.FINNHUB_API_KEY
    ins.httpx.get = _get
    ins.time.sleep = lambda *_a, **_k: None
    ins.FINNHUB_API_KEY = "test-key"
    try:
        text = ins.fetch_finnhub_news(
            ["INTC", "QCOM", "TSLA", "AMKR", "AAOI"], hours=8,
        )
    finally:
        ins.httpx.get = orig_get
        ins.time.sleep = orig_sleep
        ins.FINNHUB_API_KEY = orig_key

    assert text.count("[AAOI]") == 5, text
    assert "AAOI headline 4" in text  # 5th newest AAOI item; a global top-15 drops it
    assert text.count(shared) == 1
    assert "[INTC]" in text.split(shared)[0] or shared in text
    intc_line = next(ln for ln in text.splitlines() if shared in ln)
    assert "[INTC]" in intc_line


def test_dedup_does_not_burn_a_headline_the_first_ticker_drops():
    """INTC fetches SHARED but it ranks outside INTC's newest 5. QCOM's
    newest item is the same headline and must still be injected."""
    shared = "Shared wire outside first ticker top 5"
    intc = []
    for n in range(10):
        intc.append({
            "headline": shared if n == 7 else f"INTC only {n}",
            "datetime": 1_800_000_000 - n,
            "source": "Reuters",
            "summary": "x",
        })
    qcom = [{
        "headline": shared,
        "datetime": 1_700_000_000,
        "source": "Reuters",
        "summary": "y",
    }]

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _get(url, params=None, timeout=None):
        return _Resp(intc if params["symbol"] == "INTC" else qcom)

    orig_get, orig_sleep, orig_key = ins.httpx.get, ins.time.sleep, ins.FINNHUB_API_KEY
    ins.httpx.get = _get
    ins.time.sleep = lambda *_a, **_k: None
    ins.FINNHUB_API_KEY = "test-key"
    try:
        text = ins.fetch_finnhub_news(["INTC", "QCOM"], hours=8)
    finally:
        ins.httpx.get, ins.time.sleep, ins.FINNHUB_API_KEY = orig_get, orig_sleep, orig_key

    assert text.count(shared) == 1, text
    assert "[QCOM]" in next(ln for ln in text.splitlines() if shared in ln)
    assert "INTC only 4" in text
    assert "INTC only 5" not in text


def test_tavily_daily_limit_is_25_and_remaining_uses_it():
    assert bt.TAVILY_DAILY_LIMIT == 25
    assert bt.budget_remaining({"used": 3}) == 22
    assert bt.budget_remaining({"used": 25}) == 0
    assert bt.budget_remaining({"used": 30}) == 0


def run():
    tests = [
        test_pm_finnhub_coverage_still_queues_top3_anomaly_jobs,
        test_finnhub_keeps_five_per_ticker_and_cross_ticker_dedup,
        test_dedup_does_not_burn_a_headline_the_first_ticker_drops,
        test_tavily_daily_limit_is_25_and_remaining_uses_it,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        raise SystemExit(f"{len(failed)} failed: {failed}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    run()
