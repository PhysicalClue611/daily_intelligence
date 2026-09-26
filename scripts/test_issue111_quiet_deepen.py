"""Issue #111 contract probes; no paid calls or pytest dependency."""
import copy
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intel_deepen import quiet_candidates, macro_leads, deepen_intel_snapshot
from intel_render import render_intel_snapshot_context
from intel_collect import fetch_yahoo_rss
from budget_trackers import load_manual_budget, save_manual_budget
from run_finance import run_credit_cap
import budget_trackers as bt
import run_finance as rf

FIXTURE = next(root / "archives/202609/2026-09-25-am-intel-snapshot.json"
               for root in Path(__file__).resolve().parents
               if (root / "archives/202609/2026-09-25-am-intel-snapshot.json").exists())


def test_quiet_selection():
    snap = json.loads(FIXTURE.read_text())
    chosen = quiet_candidates(snap["entities"], [], {})
    assert [e["ticker"] for e, _ in chosen] == ["INTC", "AMKR", "PLTR"]
    assert all(reason.startswith("near_d5") for _, reason in chosen)
    movers = [{"ticker": "INTC"}]
    assert "AMKR" in [e["ticker"] for e, _ in quiet_candidates(snap["entities"], movers, {})]
    e = {"ticker": "X", "held": True, "move": {}, "items": [
        {"url_kind": "sec_filing", "seen_before": False}]}
    assert quiet_candidates([e], [], {})[0][1] == "new_8k"
    e["items"] = [{"seen_before": False}] * 12
    assert quiet_candidates([e], [], {"X": [4, 5, 6, 4, 5]})[0][1].startswith("news_spike")
    assert quiet_candidates([e], [], {}) == []


def test_macro_and_priority():
    macro = {"items": [
        {"id": str(i), "title": "Taiwan event", "summary": "", "url": f"https://{host}/a{i}",
         "url_kind": "direct", "publisher_domain": host, "published_at": f"2026-09-25T{20-i:02}:00:00+00:00",
         "seen_before": False}
        for i, host in enumerate(["news.google.com", "ft.com", "cnbc.com", "theguardian.com", "marketwatch.com"])]}
    leads = macro_leads(macro, {"Taiwan": ["Taiwan"]})
    assert [x[0].split("/")[2] for x in leads] == ["cnbc.com", "theguardian.com"]
    snap = {"date": "2026-09-25", "slot": "am", "entities": [
        {"ticker": "A", "name": "A", "held": True, "aliases": ["Alpha"],
         "move": {"d1": 5, "flags": ["anomaly"]}, "items": [
             {"id": "a", "title": "Alpha news", "url": "https://alpha.com/a", "url_kind": "direct",
              "published_at": "2026-09-25", "seen_before": False},
             {"id": "a2", "title": "Alpha report", "url": "https://second.com/a", "url_kind": "direct",
              "published_at": "2026-09-25", "seen_before": False}], "fulltext": []},
        {"ticker": "Q", "name": "Q", "held": True, "aliases": ["Quiet"],
         "move": {"d5": 10, "flags": []}, "items": [
             {"id": "q", "title": "Quiet news", "url": "https://quiet.com/q", "url_kind": "direct",
              "published_at": "2026-09-25", "seen_before": False}], "fulltext": []}],
        "macro_digest": macro}
    calls = []
    def extract(urls, query, chunks):
        calls.append(urls)
        return [{"url": u, "raw_content": "Article analysis of company performance and financial outlook. " * 10} for u in urls]
    out = deepen_intel_snapshot(snap, search=lambda *args: [], extract=extract,
                                remaining=lambda: 1, slot="am", geo_keywords={"Taiwan": ["Taiwan"]})
    assert out["entities"][0]["fulltext"]
    assert len(out["macro_digest"]["fulltext"]) == 2
    assert out["entities"][1]["fulltext"]


def test_budget_and_batch():
    assert run_credit_cap("am", True, 25) == 13
    assert run_credit_cap("am", False, 25) == 0
    assert run_credit_cap("pm", False, 7) == 18
    assert run_credit_cap("pm", True, 25) == 12
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manual.json"
        with patch("budget_trackers.MANUAL_BUDGET_PATH", path):
            b = load_manual_budget()
            b["used"] += 13
            save_manual_budget(b)
            assert load_manual_budget()["used"] == 13
        assert not (Path(tmp) / "daily.json").exists()
    snap = {"date": "2026-09-25", "slot": "pm", "entities": [], "macro_digest": {"items": []}}
    for i in range(10):
        snap["entities"].append({"ticker": f"T{i}", "name": f"T{i}", "held": True,
            "aliases": [f"Company{i}"], "move": {"d5": 10, "d1": 5, "flags": ["anomaly"] if i < 5 else []},
            "fulltext": [],
            "items": [{"id": f"{i}-{j}", "title": f"Company{i} news", "url": f"https://site{i}-{j}.com/story",
                       "url_kind": "direct", "published_at": "2026-09-25", "seen_before": False}
                      for j in range(3)]})
    batches = []
    def extract(urls, query, chunks):
        batches.append(urls)
        return []
    deepen_intel_snapshot(snap, search=lambda *args: [], extract=extract,
                          remaining=lambda: 12, slot="pm", geo_keywords={})
    assert len(batches) >= 2 and all(len(batch) <= 20 for batch in batches)


def test_manual_accounting_and_search_order():
    with tempfile.TemporaryDirectory() as tmp:
        daily_path, manual_path, monthly_path = (Path(tmp) / name for name in
                                                   ("daily.json", "manual.json", "monthly.json"))
        class Response:
            def json(self): return {"results": [], "organic_results": []}
        extraction_batches = []
        def request(method, url, **kwargs):
            if url.endswith("/extract"):
                extraction_batches.append((kwargs["json"]["urls"], kwargs["json"]["chunks_per_source"]))
            return Response()
        with patch.object(bt, "BUDGET_PATH", daily_path), \
             patch.object(bt, "MANUAL_BUDGET_PATH", manual_path), \
             patch.object(bt, "SERPAPI_BUDGET_PATH", monthly_path), \
             patch.object(rf, "TAVILY_API_KEY", "fixture"), \
             patch.object(rf, "SERPAPI_API_KEY", "fixture"), \
             patch.object(rf, "_request_with_retry", side_effect=request):
            bt.save_budget({"date": bt.load_budget()["date"], "used": 25})
            original = daily_path.read_bytes()
            manual = {"used": 0, "_manual": True, "_limit": 13, "_persisted_used": 0}
            rf.tavily_search("fixture", manual)
            urls = [f"https://site{i}.example/story" for i in range(25)]
            rf.tavily_extract(urls[:20], "fixture", manual)
            rf.tavily_extract(urls[20:], "fixture", manual)
            assert [len(batch) for batch, _ in extraction_batches] == [20, 5]
            assert all(chunks == 3 for _, chunks in extraction_batches)
            assert manual["used"] == 6 and bt.load_manual_budget()["used"] == 6
            assert daily_path.read_bytes() == original
            monthly = bt.load_serpapi_budget()
            rf.serpapi_search("fixture", monthly)
            assert monthly["used"] == 1 and bt.load_serpapi_budget()["used"] == 1
    entities = [
        {"ticker": "A", "name": "Alpha", "held": True, "move": {"d1": 5, "flags": ["anomaly"]},
         "items": [], "fulltext": []},
        {"ticker": "Q", "name": "Quiet", "held": True, "move": {"d5": -10, "flags": []},
         "items": [], "fulltext": []},
    ]
    searched = []
    deepen_intel_snapshot({"date": "2026-09-25", "slot": "am", "entities": entities,
                           "macro_digest": {"items": []}},
                          search=lambda query, *args: searched.append(query) or [],
                          extract=lambda *args: [], remaining=lambda: 13)
    assert ["Alpha" in searched[0], "Quiet" in searched[1]] == [True, True]
    assert searched[1] == "Quiet Q news"


def test_render_and_yahoo():
    rows = [{"title": f"News {i}", "summary": "summary", "publisher_domain": "example.com",
             "published_at": "2026-09-25", "seen_before": i % 2 == 0} for i in range(30)]
    snap = {"entities": [{"ticker": "Q", "held": True, "move": {}, "items": rows,
                          "fulltext": [{"url": "https://example.com", "text": "body"}], "coverage": {}},
                         {"ticker": "H", "held": True, "move": {}, "items": rows, "fulltext": [], "coverage": {}}],
            "macro_digest": {"items": []}}
    text = render_intel_snapshot_context(snap, {})
    assert "News 24" in text and "News 25" not in text.split("### H")[0]
    assert "News 11" in text.split("### H")[1] and "News 12" not in text.split("### H")[1]
    assert "summary" in text.split("### H")[1]
    news = {"content": {"title": "Intel launch", "summary": "A chip", "pubDate": "2026-09-25T12:00:00Z",
                        "canonicalUrl": {"url": "https://cnbc.com/intel"}, "provider": {"displayName": "CNBC"}}}
    class Ticker:
        def __init__(self, ticker): assert ticker == "INTC"
        def get_news(self, count): assert count == 20; return [news]
    with patch("yfinance.Ticker", Ticker):
        got = fetch_yahoo_rss("INTC", datetime(2026, 9, 25, tzinfo=timezone.utc),
                              datetime(2026, 9, 26, tzinfo=timezone.utc), ["Intel"])
    assert len(got) == 1 and got[0]["publisher_domain"] == "cnbc.com"


if __name__ == "__main__":
    test_quiet_selection()
    test_macro_and_priority()
    test_budget_and_batch()
    test_manual_accounting_and_search_order()
    test_render_and_yahoo()
    print("issue #111: 5 contract probes passed")
