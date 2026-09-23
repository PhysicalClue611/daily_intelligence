"""Offline contracts for issue #87 ledger-driven report switch."""
import sys
import unittest
import os
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from intel_deepen import candidate_entities, deepen_ledger, resolve_article_url
from intel_render import render_ledger_context, render_fallback_report, should_report, filter_social_lines
from fetch_news import _classify_topics


def entity(ticker, *, d1=0, d3=None, d5=None, held=True, items=None):
    return {"ticker": ticker, "name": ticker, "held": held, "weight_pct": 10.0,
            "move": {"d1": d1, "d3": d3, "d5": d5,
                     "flags": (["anomaly"] if abs(d1) >= 3 else []) + (["d3"] if d3 is not None and abs(d3) >= 15 else [])},
            "coverage": {"finnhub": 4, "google_news": 2, "rss": 0, "guardian": 0, "errors": []},
            "aliases": [ticker], "items": items or [], "fulltext": []}


def item(title, domain, url, kind="direct", when="2026-09-21T12:00:00+00:00"):
    return {"id": title, "title": title, "summary": "", "publisher_domain": domain,
            "publisher_domains": [domain], "url": url, "url_kind": kind, "published_at": when,
            "seen_before": False}


class SwitchTest(unittest.TestCase):
    def test_unarchived_pass0_can_be_enriched_before_first_write(self):
        import intel_pass0 as pass0
        with patch.object(pass0.collect, "resolve_aliases", return_value=({"INTC": ["Intel", "INTC"]}, {})), \
             patch.object(pass0.collect, "collect", return_value=([entity("INTC", d1=5)], {"items": [], "geo_topics_hit": []})), \
             patch.object(pass0.collect, "archive_ledger", side_effect=AssertionError("early archive")):
            ledger, path = pass0.build_ledger({"stocks": ["INTC"]},
                                               datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
                                               "am", archive=False, archive_root=Path("/tmp/scratch"))
        self.assertEqual(ledger["entities"][0]["ticker"], "INTC")
        self.assertEqual(path.name, "2026-09-21-am-ledger.json")

    def test_geo_topic_matching_uses_boundaries(self):
        self.assertEqual(_classify_topics("Warner reports", "", {"war": ["war"]}), [])
        self.assertEqual(_classify_topics("War escalates", "", {"war": ["War"]}), ["war"])

    def test_main_uses_ledger_and_only_active_llm_stages(self):
        import run_finance as rf
        from fetch_prices import PriceRow
        row = PriceRow("INTC", "Intel", 100, 90, 11, 25, True, "$", slot="pm")
        ledger = {"date": "2026-09-21", "slot": "pm", "as_of": "2026-09-21T23:00:00-04:00",
                  "entities": [entity("INTC", d1=11, items=[item("INTC Intel deal", "news.example", "https://news.example/a")])],
                  "macro_digest": {"items": [], "geo_topics_hit": []}}
        stages, prompts, written, alerts = [], [], [], []
        fail = {"pass2": False}
        def fake_llm(prompt, *, stage, **kwargs):
            stages.append(stage)
            prompts.append(prompt)
            return ({"text": "" if fail["pass2"] else "# [Daily_Intel] 2026-09-21 开盘前简报\n## 持仓与观察标的\nINTC 更新"}
                    if stage == "report_pass2" else {"sas_candidates": []})
        replacements = {
            "_monthly_dedup": False, "load_watchlist": {"stocks": ["INTC"], "commodities": [], "fx": [],
                "geo_keywords": {}, "thresholds": {}, "recipients": ["fixture-recipient"], "entity_aliases": {"INTC": ["Intel"]}},
            "load_budget": {"used": 0}, "load_serpapi_budget": {"used": 0},
            "fetch_prices": [row], "format_price_table": "INTC +11%",
            "_get_core_holding_tickers": [], "_get_portfolio_weights": {},
            "build_ledger": (ledger, Path("/tmp/ledger.json")),
            "get_finance_context": "", "_sonar_macro_brief": "", "load_adanos_budget": {"used": 0},
            "_polymarket_brief": "", "_adanos_x_sentiment": "", "save_adanos_budget": None,
            "load_apify_budget": {"used": 0}, "_reddit_sentiment_brief": "", "save_apify_budget": None,
            "fetch_liquidity_snapshot": "", "build_recent_coverage_section": "",
            "read_previous_ledger_state": {}, "_load_personal_context": "",
            "_load_recent_calibration_notes": "", "write_sas_candidate_log": None,
            "archive_ledger": None, "_mempalace_add_daily_drawer": None,
            "write_context_log": None, "send_report": True, "send_telegram_report": True,
            "finance_footer": "",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FINANCE_FORCE_DATE": "2026-09-21", "FINANCE_FORCE_SLOT": "pm"}))
            for name, value in replacements.items():
                stack.enter_context(patch.object(rf, name, return_value=value))
            stack.enter_context(patch.object(rf, "deepen_ledger", side_effect=lambda ledger, **_: ledger))
            stack.enter_context(patch.object(rf, "call_llm", side_effect=fake_llm))
            stack.enter_context(patch.object(rf, "evaluate_am_calibration", side_effect=lambda *args: args[-1]))
            stack.enter_context(patch.object(rf, "write_report", side_effect=lambda *args: written.append(args[2])))
            stack.enter_context(patch.object(rf, "send_telegram_alert", side_effect=lambda text: alerts.append(text)))
            rf._main_body()
            fail["pass2"] = True
            rf._main_body()
        self.assertEqual(stages, ["report_pass2", "sas_candidate_extract"] * 2)
        self.assertIn("按标的收集的情报账本", prompts[0])
        self.assertNotIn("过去24小时新闻（RSS）", prompts[0])
        self.assertIn("Pass 2 失败", alerts[0])
        self.assertIn("线索待核实", written[1])

    def test_candidates_sort_and_limit_five(self):
        entities = [entity(f"T{i}", d1=float(i+3)) for i in range(7)]
        entities += [entity("QUIET", d1=0), entity("MULTI", d1=0, d3=-21)]
        self.assertEqual([e["ticker"] for e in candidate_entities(entities)],
                         ["MULTI", "T6", "T5", "T4", "T3"])

    def test_redirect_reads_only_302_location(self):
        class Response:
            status_code = 302
            headers = {"location": "https://publisher.example/article"}
        with patch("intel_deepen.httpx.head", return_value=Response()) as head, \
             patch("intel_deepen.httpx.get", side_effect=AssertionError("downloaded article")):
            self.assertEqual(resolve_article_url("https://finnhub.io/api/news?id=1"),
                             "https://publisher.example/article")
        self.assertFalse(head.call_args.kwargs["follow_redirects"])

    def test_paid_request_retries_transient_failure_before_accounting(self):
        import httpx
        import run_finance as rf
        attempts = []
        class Response:
            def raise_for_status(self):
                pass
        def method(*_args, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.ConnectError("temporary")
            return Response()
        with patch.object(rf.time, "sleep"):
            self.assertIsInstance(rf._request_with_retry(method, "https://example.test"), Response)
        self.assertEqual(len(attempts), 2)

    def test_deepen_caps_search_extract_and_uses_direct_links_first(self):
        direct = item("INTC Intel deal", "a.example", "https://a.example/1")
        duplicate = item("INTC Intel followup", "a.example", "https://a.example/2")
        other = item("INTC Intel results", "b.example", "https://b.example/3")
        entities = [entity("INTC", d1=10, items=[direct, duplicate, other])]
        entities += [entity(f"T{i}", d1=9-i) for i in range(4)]
        ledger = {"date": "2026-09-21", "slot": "pm", "entities": entities,
                  "macro_digest": {"items": [], "geo_topics_hit": []}}
        searched, extracted = [], []
        def search(query, start, end):
            searched.append((query, start, end))
            return [{"title": query, "url": f"https://search{len(searched)}.example/a", "content": "lead"}]
        def extract(urls, query):
            extracted.extend(urls)
            return [{"url": url, "chunks": [{"content": "Full article about company."}]} for url in urls]
        result = deepen_ledger(ledger, search=search, extract=extract, remaining=lambda: 25)
        self.assertLessEqual(len(searched), 3)
        self.assertLessEqual(len(extracted), 10)
        self.assertIn("https://a.example/1", extracted)
        self.assertIn("https://b.example/3", extracted)
        self.assertNotIn("https://a.example/2", extracted)
        self.assertEqual(len(result["entities"][0]["fulltext"]), 2)

    def test_budget_exhaustion_stops_spend_and_keeps_items(self):
        ledger = {"date": "2026-09-21", "slot": "am", "entities": [entity("INTC", d1=6)],
                  "macro_digest": {"items": [], "geo_topics_hit": []}}
        with patch("intel_deepen.httpx.head", side_effect=AssertionError("no links")):
            result = deepen_ledger(ledger, search=lambda *_: self.fail("spent search"),
                                   extract=lambda *_: self.fail("spent extract"), remaining=lambda: 0)
        self.assertEqual(result["entities"][0]["items"], [])
        self.assertIn("budget", result["deepen_status"]["INTC"])

    def test_multiday_direction_uses_triggered_move(self):
        e = entity("INTC", d1=1.0, d3=-18.0)
        ledger = {"date": "2026-09-21", "slot": "pm", "entities": [e]}
        queries = []
        deepen_ledger(ledger, search=lambda query, *_: queries.append(query) or [],
                      extract=lambda *_: [], remaining=lambda: 25)
        self.assertIn("stock down", queries[0])

    def test_ledger_render_caps_and_fallback_reports_coverage(self):
        rows = [item(f"INTC story {i}", f"s{i}.example", f"https://s{i}.example/x") for i in range(30)]
        ledger = {"date": "2026-09-21", "slot": "pm", "entities": [entity("INTC", d1=8, items=rows), entity("NVDA", held=False)],
                  "macro_digest": {"items": [], "geo_topics_hit": []}}
        text = render_ledger_context(ledger, {})
        self.assertIn("INTC", text)
        self.assertNotIn("INTC story 25", text)
        self.assertIn("无异动观察标的：NVDA", text)
        fallback = render_fallback_report(ledger, "夜盘收市速报")
        self.assertIn("Finnhub 4", fallback)
        self.assertIn("INTC story 0", fallback)
        self.assertTrue(should_report(ledger))
        self.assertFalse(should_report({"entities": [entity("INTC")], "macro_digest": {"items": []}}))
        self.assertFalse(should_report({"entities": [entity("INTC")], "macro_digest": {"items": [item("generic", "x", "https://x/a")], "geo_topics_hit": []}}))
        self.assertTrue(should_report({"entities": [entity("INTC")], "macro_digest": {"items": [], "geo_topics_hit": ["Middle East"]}}))
        macro_only = {"date": "2026-09-21", "entities": [entity("INTC")],
                      "macro_digest": {"items": [item("Oil route risk", "news.example", "https://news.example/x")],
                                       "geo_topics_hit": ["oil"]}}
        self.assertIn("Oil route risk", render_fallback_report(macro_only, "夜盘收市速报"))

    def test_social_only_entity_lines_one_per_ticker(self):
        result = filter_social_lines("- INTC crowd bearish\n- INTC forum noise\n- NVDA bullish", [entity("INTC", d1=5)])
        self.assertIn("INTC crowd bearish", result)
        self.assertNotIn("forum noise", result)
        self.assertNotIn("NVDA", result)


if __name__ == "__main__":
    unittest.main()
