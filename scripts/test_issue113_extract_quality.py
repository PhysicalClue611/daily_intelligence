"""Issue #113 offline contract probes."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recent_coverage import recent_extracted_urls, normalize_url
from intel_deepen import clean_body, body_acceptable, quiet_candidates, deepen_intel_snapshot, _direct_leads
from intel_render import render_intel_snapshot_context
from run_finance import USER_PROMPT_TEMPLATE_P2

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "archives/202609/2026-09-25-pm-intel-snapshot.json").exists())


def test_history_and_selection():
    assert normalize_url("https://a.test/story?utm_source=x&id=1#part") == "https://a.test/story?id=1"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "202609"
        p.mkdir()
        for day, url in [("24", "https://a.test/story?utm_source=x#part"), ("25", "https://b.test/now")]:
            (p / f"2026-09-{day}-am-intel-snapshot.json").write_text(json.dumps({"entities": [{"fulltext": [{"url": url}]}]}))
        assert recent_extracted_urls(Path(tmp), "2026-09-25", "pm") == {"https://a.test/story", "https://b.test/now"}
        (p / "2026-09-25-am-intel-snapshot.json").write_text("broken")
        assert recent_extracted_urls(Path(tmp), "2026-09-25", "pm") == set()
    e = {"ticker": "X", "held": True, "move": {}, "items": [{"seen_before": False}] * 12}
    assert quiet_candidates([e], [], {"X": [4]}) == []
    assert quiet_candidates([e], [], {"X": [4] * 5})[0][1].startswith("news_spike")


def test_extract_and_prompt():
    body = "Company signed a major supply agreement. " * 15
    e = {"ticker": "QCOM", "name": "Qualcomm", "aliases": ["Qualcomm"], "held": True,
         "move": {"d1": 5, "flags": ["anomaly"]}, "items": [
             {"id": "one", "title": "Qualcomm supply agreement", "url": "https://example.com/one",
              "url_kind": "direct", "published_at": "2026-09-25", "seen_before": False}], "fulltext": []}
    snap = {"date": "2026-09-25", "slot": "pm", "entities": [e]}
    calls = []
    def extract(urls, query, chunks_per_source):
        calls.append((urls, query, chunks_per_source))
        return [{"url": url, "raw_content": body} for url in urls]
    deepen_intel_snapshot(snap, search=lambda *args: [], extract=extract, remaining=lambda: 5,
                          skip_urls={"https://other.test/stale"})
    assert calls and calls[0][2] == 3 and "Qualcomm QCOM" in calls[0][1]
    assert snap["macro_digest"]["fulltext"] == []
    assert snap["deepen_status"]["QCOM"] == "direct leads; extracted"
    assert "本次抓到正文" in render_intel_snapshot_context(snap, {})
    assert "近一周事件" in USER_PROMPT_TEMPLATE_P2 and "公司级实质事件" in USER_PROMPT_TEMPLATE_P2


def test_dedup_search_and_order():
    rows = [
        {"id": "old", "title": "Acme older", "url": "https://first.test/a?utm_source=x#part",
         "url_kind": "direct", "published_at": "2026-09-25", "seen_before": True},
        {"id": "new", "title": "Acme newest", "url": "https://second.test/a",
         "url_kind": "direct", "published_at": "2026-09-25", "seen_before": False},
        {"id": "y", "title": "Acme Yahoo", "url": "https://finance.yahoo.com/a",
         "url_kind": "direct", "published_at": "2026-09-26", "seen_before": False},
    ]
    e = {"ticker": "ACME", "name": "Acme", "aliases": ["Acme"], "held": True,
         "move": {"d5": 10, "flags": []}, "items": rows, "fulltext": []}
    assert [row[0] for row in _direct_leads(e, limit=3)] == [rows[0]["url"], rows[1]["url"], rows[2]["url"]]
    snap = {"date": "2026-09-25", "slot": "pm", "entities": [e]}
    calls = []
    deepen_intel_snapshot(snap, search=lambda *args: [],
                          extract=lambda urls, query, chunks: calls.extend(urls) or [],
                          remaining=lambda: 3, skip_urls={"https://first.test/a"})
    assert snap["extract_dedup_skipped"] == 1
    assert rows[0]["url"] not in calls
    assert "Acme ACME news" not in [job["query"] for job in snap["search_jobs"]]

    e = {"ticker": "ACME", "name": "Acme", "aliases": ["Acme"], "held": True,
         "move": {"d5": 10, "flags": []}, "items": [], "fulltext": []}
    snap = {"date": "2026-09-25", "slot": "pm", "entities": [e]}
    picked = []
    def search(query, start, end):
        assert query == "Acme ACME news"
        return [{"url": f"https://test{i}.com/a", "title": "Acme news",
                 "published_date": published} for i, published in enumerate(
                     ["2026-09-16", "2026-09-17", None, "2026-09-25"])]
    deepen_intel_snapshot(snap, search=search,
                          extract=lambda urls, query, chunks: picked.extend(urls) or [],
                          remaining=lambda: 5)
    assert "https://test0.com/a" not in picked
    assert "https://test2.com/a" not in picked  # undated is retained behind dated leads, never top-up
    assert picked[:2] == ["https://test1.com/a", "https://test3.com/a"]


def test_quality_calibration():
    snap = json.loads((ROOT / "archives/202609/2026-09-25-pm-intel-snapshot.json").read_text())
    rows = [(x["url"], x["text"]) for e in snap["entities"] for x in e.get("fulltext", [])]
    rows += [(x["url"], x["text"]) for x in snap["macro_digest"].get("fulltext", [])]
    assert len(rows) == 20
    assert sum(body_acceptable(clean_body(url, body)) for url, body in rows) >= 9
    for url, body in rows:
        accepted = body_acceptable(clean_body(url, body))
        if (("finance.yahoo.com" in url and "spacex-spcx-wins" not in url) or
                "foreignpolicy.com" in url or "marketwatch.com" in url):
            assert not accepted, url
        if ("spacex-spcx-wins" in url or "qualcomm-rallies" in url or "trump-xi-summit" in url or "saudi-arabia-oil" in url or
                "stock-market-today-sept-25-tesla" in url):
            assert accepted, url


if __name__ == "__main__":
    test_history_and_selection()
    test_extract_and_prompt()
    test_dedup_search_and_order()
    test_quality_calibration()
    print("issue #113: contract probes passed")
