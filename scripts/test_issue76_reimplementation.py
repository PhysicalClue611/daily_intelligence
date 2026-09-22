"""Issue #76 regressions: PM anomaly coverage, Finnhub fairness, and budget."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget_trackers as bt
import intel_sources as sources
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


def test_pm_anomaly_jobs_exist_and_identify_actual_top_three():
    anomalies = [
        _row("INTC", 11.86),
        _row("QCOM", 9.0),
        _row("TSLA", 8.0),
        _row("NVDA", 7.0),
        _row("AMKR", 6.0),
        _row("AAOI", 5.0),
    ]
    jobs = rf._anomaly_search_jobs(
        anomalies, run_slot="pm", today_et=TODAY, query_days=2,
    )
    assert [job["_anomaly_ticker"] for job in jobs] == ["INTC", "QCOM", "TSLA"]
    assert all("afterhours" in job["query"] for job in jobs)


def test_fourth_anomaly_is_not_marked_covered_and_rotation_can_search_it():
    anomalies = [
        _row("INTC", 11.86),
        _row("QCOM", 9.0),
        _row("TSLA", 8.0),
        _row("NVDA", 7.0),
    ]
    jobs = rf._anomaly_search_jobs(
        anomalies, run_slot="pm", today_et=TODAY, query_days=2,
    )
    covered = rf._anomaly_tickers_from_jobs(jobs)
    note = rf._build_anomaly_tickers_note(covered)

    assert covered == ["INTC", "QCOM", "TSLA"]
    assert "INTC" in note
    assert "NVDA" not in note

    with patch.object(rf, "_get_core_holding_tickers", return_value=["NVDA"]):
        rotation = rf._rotation_search_job(TODAY, anomaly_tickers=set(covered))
    assert rotation is not None
    assert rotation["_rotation_ticker"] == "NVDA"


def test_finnhub_keeps_five_per_ticker_and_deduplicates_across_tickers():
    base_ts = 1_800_000_000

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(_url, params, timeout):
        ticker = params["symbol"]
        payload = [
            {
                "headline": f"{ticker} headline {i}",
                "datetime": base_ts - i,
                "source": "Test",
                "summary": f"summary {i}",
            }
            for i in range(10)
        ]
        if ticker in {"AAA", "BBB"}:
            payload.insert(0, {
                "headline": "shared wire story",
                "datetime": base_ts + 10,
                "source": "Test",
                "summary": "same story",
            })
        return Response(payload)

    with (
        patch.object(sources, "FINNHUB_API_KEY", "test-key"),
        patch.object(sources.httpx, "get", side_effect=fake_get),
        patch.object(sources.time, "sleep"),
    ):
        section = sources.fetch_finnhub_news(["AAA", "BBB", "CCC", "DDD", "EEE"])

    story_lines = [line for line in section.splitlines() if line.startswith("[")]
    assert len(story_lines) == 25
    assert sum("shared wire story" in line for line in story_lines) == 1
    assert all(sum(f"][{ticker}]" in line for line in story_lines) == 5
               for ticker in ["AAA", "BBB", "CCC", "DDD", "EEE"])


def test_tavily_daily_limit_is_25():
    assert bt.TAVILY_DAILY_LIMIT == 25


def run():
    tests = [
        test_pm_anomaly_jobs_exist_and_identify_actual_top_three,
        test_fourth_anomaly_is_not_marked_covered_and_rotation_can_search_it,
        test_finnhub_keeps_five_per_ticker_and_deduplicates_across_tickers,
        test_tavily_daily_limit_is_25,
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
