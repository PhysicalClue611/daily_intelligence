"""
Issue #72: Digitimes RSS + per-ticker anomaly queries + 7d fence + rotation skip.

Reproduces the 2026-09-21 AM failure: INTC +5.47% and CL=F -7.09% were merged
into one `... stock news earnings` query; rotation also searched INTC.

Run: python scripts/test_issue72_anomaly_search.py
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_news as fn
import run_finance as rf
from fetch_prices import PriceRow

ET = ZoneInfo("America/New_York")
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
        slot="am",
    )


def test_digitimes_feed_registered():
    urls = [u for _, u in fn.RSS_FEEDS]
    names = [n for n, _ in fn.RSS_FEEDS]
    assert "Digitimes" in names
    assert "https://www.digitimes.com/rss/daily.xml" in urls


def test_anomaly_jobs_are_per_ticker_no_earnings_anchor():
    # Real 2026-09-21 movers: CL=F abs 7.09 > INTC 5.47; a smaller third
    # should still get its own query; a fourth must be dropped (top 3).
    anomalies = [
        _row("INTC", 5.47),
        _row("CL=F", -7.09),
        _row("AMKR", 3.10),
        _row("QCOM", 2.20),
    ]
    jobs = rf._anomaly_search_jobs(
        anomalies, run_slot="am", today_et=TODAY, query_days=2,
        finnhub_covers=False,
    )
    queries = [j["query"] for j in jobs]
    assert len(jobs) == 3
    assert queries[0].startswith("CL=F stock drop 7.1% premarket reason 2026-09-21")
    assert queries[1].startswith("INTC stock surge 5.5% premarket reason 2026-09-21")
    assert queries[2].startswith("AMKR stock surge 3.1% premarket reason 2026-09-21")
    assert all("earnings" not in q for q in queries)
    assert not any("INTC" in q and "CL=F" in q for q in queries)
    assert all(j["days"] == 2 for j in jobs)
    assert all(j["search_depth"] == "basic" for j in jobs)


def test_anomaly_jobs_cap_days_at_7():
    jobs = rf._anomaly_search_jobs(
        [_row("INTC", 5.47)], run_slot="am", today_et=TODAY,
        query_days=30, finnhub_covers=False,
    )
    assert jobs[0]["days"] == 7


def test_anomaly_jobs_pm_afterhours_and_skip_when_finnhub():
    jobs = rf._anomaly_search_jobs(
        [_row("INTC", -4.2)], run_slot="pm", today_et=TODAY,
        query_days=1, finnhub_covers=False,
    )
    assert "afterhours" in jobs[0]["query"]
    assert "drop" in jobs[0]["query"]
    skipped = rf._anomaly_search_jobs(
        [_row("INTC", -4.2)], run_slot="pm", today_et=TODAY,
        query_days=1, finnhub_covers=True,
    )
    assert skipped == []


def test_rotation_skips_when_ticker_already_an_anomaly(caplog=None):
    orig = rf._get_core_holding_tickers
    # 2026-09-21 ordinal % 3 == 2, so put INTC at index 2.
    rf._get_core_holding_tickers = lambda: ["NVDA", "QCOM", "INTC"]
    try:
        job = rf._rotation_search_job(TODAY, anomaly_tickers={"INTC"})
        assert job is None
        job2 = rf._rotation_search_job(TODAY, anomaly_tickers={"NVDA"})
        assert job2 is not None
        assert job2["_rotation_ticker"] == "INTC"
    finally:
        rf._get_core_holding_tickers = orig


def test_pass1_prompt_tells_model_not_to_requery_anomalies():
    assert "{anomaly_tickers_note}" in rf.USER_PROMPT_TEMPLATE
    filled = rf.USER_PROMPT_TEMPLATE.format(
        date=TODAY,
        now_str="2026-09-21 05:30 EDT",
        last_report_date="2026-09-18",
        query_days=1,
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
        anomaly_tickers_note="以下标的已被系统识别为今日异动并自动生成追因查询，不需要你重复建议同名 ticker 的查询：INTC, CL=F",
    )
    assert "INTC" in filled
    assert "不需要你重复建议同名 ticker" in filled


def test_score_and_filter_drops_stale_published_results():
    now = datetime.now(ET)
    stale = (now - timedelta(days=20)).isoformat()
    fresh = (now - timedelta(hours=6)).isoformat()
    results = [
        {
            "title": "INTC Q2 earnings recap",
            "url": "https://example.com/old",
            "score": 0.9,
            "content": "INTC earnings",
            "published_date": stale,
        },
        {
            "title": "INTC packaging talks today",
            "url": "https://example.com/new",
            "score": 0.5,
            "content": "INTC AUO",
            "published_date": fresh,
        },
        {
            "title": "INTC undated wire",
            "url": "https://example.com/nodate",
            "score": 0.4,
            "content": "INTC",
        },
    ]
    kept = rf.score_and_filter(results, ["INTC"], {}, top_n=15)
    urls = {r["url"] for r in kept}
    assert "https://example.com/old" not in urls
    assert "https://example.com/new" in urls
    assert "https://example.com/nodate" in urls


def run():
    tests = [
        test_digitimes_feed_registered,
        test_anomaly_jobs_are_per_ticker_no_earnings_anchor,
        test_anomaly_jobs_cap_days_at_7,
        test_anomaly_jobs_pm_afterhours_and_skip_when_finnhub,
        test_rotation_skips_when_ticker_already_an_anomaly,
        test_pass1_prompt_tells_model_not_to_requery_anomalies,
        test_score_and_filter_drops_stale_published_results,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed.append(t.__name__)
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
