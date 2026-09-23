"""PR1 contracts for the shadow Pass 0 ledger (no network or paid calls)."""
import ast
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import intel_collect as collect
import intel_pass0 as pass0
import run_finance as finance
from eval.evaluate_issue87 import score_case
from intel_pass0 import _context_price_rows, build_ledger


class PassZeroTest(unittest.TestCase):
    def test_shadow_failure_is_caught_before_existing_search_path(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        moves = {"INTC": (16.0, 21.0)}
        with patch.object(pass0, "build_ledger", side_effect=RuntimeError("free source failed")), \
             patch.object(finance, "_get_core_holding_tickers", return_value=[]), \
             patch.object(finance, "_get_portfolio_weights", return_value={}), \
             patch.object(finance.logger, "warning") as warning:
            finance._run_shadow_ledger({"stocks": ["INTC"]}, now, "pm", [], moves, "2026-09-21")
        self.assertEqual(moves, {"INTC": (16.0, 21.0)})
        self.assertIn("Pass0 shadow ledger failed", warning.call_args.args[0])

    def test_shadow_modules_have_no_paid_service_import_or_call(self):
        for name in ("intel_collect.py", "intel_pass0.py"):
            tree = ast.parse((Path(__file__).parent / name).read_text())
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for alias in node.names}
            self.assertFalse(names & {"call_llm", "tavily_search", "tavily_extract", "openrouter"})
            self.assertFalse(modules & {"llm_client", "tavily"})
    def test_word_boundaries_and_short_alias_case(self):
        aliases = {"INTC": ["Intel", "英特尔"], "ARM": ["Arm"], "AUO": ["AUO"]}
        self.assertEqual(collect.match_entities("Intel and AUO agree on packaging", aliases), {"INTC", "AUO"})
        self.assertEqual(collect.match_entities("Arm shares rise", aliases), {"ARM"})
        self.assertEqual(collect.match_entities("farm platform Warner", aliases), set())
        self.assertEqual(collect.match_entities("ARM shares rise", aliases), set())
        self.assertEqual(collect.match_entities("英特尔与友达洽谈先进封装", aliases), {"INTC"})
        self.assertEqual(collect.match_entities("关于英特尔的消息", aliases), {"INTC"})
        self.assertEqual(collect.match_entities("英特尔发布财报", aliases), {"INTC"})

    def test_google_news_actual_request_starts_are_spaced(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        tickers = [f"T{i}" for i in range(6)]
        aliases = {ticker: [f"Company{i}"] for i, ticker in enumerate(tickers)}
        release = threading.Event()
        starts = []
        def finnhub(*_args):
            release.wait(7)
            return []
        def http_get(*_args, **_kwargs):
            starts.append(time.monotonic())
            return type("Response", (), {"content": b"<rss><channel></channel></rss>",
                                          "raise_for_status": lambda self: None})()
        timer = threading.Timer(5.2, release.set)
        timer.start()
        try:
            with patch.object(collect, "fetch_finnhub", side_effect=finnhub), \
                 patch.object(collect.httpx, "get", side_effect=http_get), \
                 patch.object(collect, "fetch_rss_pool", return_value=([], [])), \
                 patch.object(collect, "fetch_guardian_pool", return_value=([], [])):
                collect.collect(tickers, aliases, set(), {}, {}, {}, now, "pm")
        finally:
            release.set()
            timer.cancel()
        self.assertEqual(len(starts), len(tickers))
        self.assertTrue(all(b - a >= 0.95 for a, b in zip(starts, starts[1:])), starts)

    def test_multiday_pool_fetches_earliest_window_then_filters_per_entity(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        old = now - timedelta(days=5)
        row = collect.item("Intel funding news", "", "RSS", "news.example", "u", "direct", old)
        guardian = collect.item("Intel old Guardian item", "", "Guardian", "guardian.example", "v", "direct", old)
        bounds = []
        def rss(since, _as_of):
            bounds.append(("rss", since))
            return [row], []
        def guardian_pool(since, _as_of, _key):
            bounds.append(("guardian", since))
            return [guardian], []
        aliases = {"INTC": ["Intel"], "PLTR": ["Palantir"]}
        moves = {"INTC": {"window_start": old.date().isoformat()}}
        with patch.object(collect, "fetch_finnhub", return_value=[]), \
             patch.object(collect, "fetch_google_news", return_value=[]), \
             patch.object(collect, "fetch_rss_pool", side_effect=rss), \
             patch.object(collect, "fetch_guardian_pool", side_effect=guardian_pool):
            entities, _ = collect.collect(["INTC", "PLTR"], aliases, set(), {}, moves, {}, now, "pm")
        self.assertEqual({kind for kind, _ in bounds}, {"rss", "guardian"})
        self.assertTrue(all(since <= old for _, since in bounds))
        self.assertEqual(entities[0]["coverage"]["rss"], 1)
        self.assertEqual(entities[0]["coverage"]["guardian"], 1)
        self.assertEqual(len(entities[1]["items"]), 0)

    def test_replay_fetches_guardian_but_skips_rss(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        story = collect.item("Intel and AUO deal", "", "Guardian", "theguardian.com", "u", "direct", now)
        with patch.object(collect, "fetch_finnhub", return_value=[]), \
             patch.object(collect, "fetch_google_news", return_value=[]), \
             patch.object(collect, "fetch_rss_pool", side_effect=AssertionError("replay fetched RSS")), \
             patch.object(collect, "fetch_guardian_pool", return_value=([story], [])) as guardian:
            entities, _ = collect.collect(["INTC"], {"INTC": ["Intel"]}, set(), {}, {}, {}, now, "pm", replay=True)
        guardian.assert_called_once()
        self.assertEqual(entities[0]["coverage"]["guardian"], 1)
        self.assertIn("rss: skipped in replay", entities[0]["coverage"]["errors"])

    def test_replay_passes_multiday_window_to_ledger(self):
        from fetch_prices import PriceRow
        row = PriceRow("INTC", "Intel", 100, 90, 11, 25, True, "$", slot="pm")
        with tempfile.TemporaryDirectory() as root, \
             patch.object(pass0, "_read_watchlist_rest", return_value={"stocks": ["INTC"]}), \
             patch.object(pass0, "_read_context_rest", return_value=""), \
             patch.object(pass0, "_historical_prices", return_value=[row]), \
             patch.object(pass0, "_historical_multiday_moves", return_value={"INTC": (16.0, 25.0)}), \
             patch.object(pass0, "build_ledger", return_value=({"date": "2026-09-21", "slot": "pm", "entities": [],
                                                                  "macro_digest": {"items": []}}, Path(root) / "ledger.json")) as build, \
             patch.object(collect, "archive_ledger"):
            pass0.replay("2026-09-21", "pm", tickers=["INTC"], archive_root=Path(root))
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["multiday_moves"]["INTC"], (16.0, 25.0))
        self.assertLess(kwargs["window_starts"]["INTC"], "2026-09-18")

    def test_replay_multiday_uses_only_prior_completed_closes(self):
        import pandas as pd
        from fetch_prices import PriceRow
        dates = pd.to_datetime(["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
                                "2026-09-18", "2026-09-21"])
        frame = pd.DataFrame({"Close": [100, 100, 100, 100, 120, 1000]}, index=dates)
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        row = PriceRow("INTC", "Intel", 130, 120, 8.3, 30, True, "$", slot="pm")
        with patch("yfinance.download", return_value=frame):
            pm = pass0._historical_multiday_moves([row], now, "pm")
            am = pass0._historical_multiday_moves([row], now, "am")
        self.assertAlmostEqual(pm["INTC"][0], 30.0)
        self.assertAlmostEqual(pm["INTC"][1], 30.0)
        self.assertAlmostEqual(am["INTC"][0], 20.0)
        self.assertIsNone(am["INTC"][1])

    def test_dedup_keeps_sources_and_coverage_raw_counts(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        items = [
            collect.item("Intel signs AUO partnership", "", "Finnhub", "a.com", "https://a.com/1", "direct", now),
            collect.item("Intel signs AUO partnership!", "", "Google News", "b.com", "https://news.google.com/1", "google_news", now),
        ]
        deduped = collect.deduplicate(items)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(set(deduped[0]["publisher_domains"]), {"a.com", "b.com"})

    def test_replay_cutoff_and_rss_skip_coverage(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        later = datetime(2026, 9, 21, 13, tzinfo=timezone.utc)
        sources = {"finnhub": [collect.item("old", "", "Finnhub", "a.com", "u", "direct", now),
                               collect.item("future", "", "Finnhub", "a.com", "v", "direct", later)]}
        ledger = collect.assemble_entity("INTC", "Intel", ["Intel"], False, None, {}, sources,
                                         {"rss": "skipped in replay"}, now)
        self.assertEqual([i["title"] for i in ledger["items"]], ["old"])
        self.assertEqual(ledger["coverage"]["finnhub"], 1)
        self.assertTrue(any("rss: skipped in replay" in e for e in ledger["coverage"]["errors"]))

    def test_shadow_build_only_collects_and_marks_seen_titles(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        story = collect.item("Intel signs AUO partnership!", "", "Finnhub", "a.com", "u", "direct", now)
        previous = {"as_of": "2026-09-20T12:00:00+00:00", "date": "2026-09-20", "slot": "pm",
                    "entities": [{"ticker": "INTC", "items": [{"title": "Intel signs AUO partnership"}]}]}
        with tempfile.TemporaryDirectory() as root:
            collect.archive_ledger(previous, Path(root))
            with patch("llm_client.call_llm", side_effect=AssertionError("shadow mode called LLM")), \
                 patch.object(collect, "resolve_aliases", return_value=({"INTC": ["Intel", "INTC"]}, {})), \
                 patch.object(collect, "collect", return_value=([collect.assemble_entity(
                     "INTC", "Intel", ["Intel", "INTC"], True, 4.0, {}, {"finnhub": [story]}, [], now)],
                     {"items": [], "geo_topics_hit": [], "coverage": {}})):
                ledger, _ = build_ledger({"stocks": ["INTC"]}, now, "am", archive_root=Path(root))
        entity = ledger["entities"][0]
        self.assertTrue(entity["items"][0]["seen_before"])
        self.assertFalse(set(entity) & {"events", "move_status", "primary_event_ids", "gap_query"})

    def test_atomic_archive(self):
        with tempfile.TemporaryDirectory() as root:
            path = collect.archive_ledger({"date": "2026-09-21", "slot": "am", "entities": []}, Path(root))
            self.assertEqual(json.loads(path.read_text())["slot"], "am")
            self.assertFalse(list(Path(root).rglob("*.tmp")))

    def test_finnhub_keeps_all_in_window_and_truncates_only_summary(self):
        now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        rows = [{"datetime": int(now.timestamp()), "headline": f"Intel story {n}",
                 "summary": "x" * 500, "url": "https://finnhub.io/api/news?id=1"}
                for n in range(23)]
        rows.append({"datetime": int(now.timestamp()) + 3600, "headline": "future"})
        response = type("Response", (), {"json": lambda self: rows})()
        with patch.object(collect, "_request", return_value=response):
            found = collect.fetch_finnhub("INTC", now.replace(hour=11), now, "test")
        self.assertEqual(len(found), 23)
        self.assertTrue(all(len(row["summary"]) == 300 for row in found))
        self.assertEqual(found[0]["url_kind"], "finnhub_redirect")

    def test_acceptance_scores_collection_only(self):
        case = {"date": "2026-09-21", "slot": "am", "ticker": "INTC", "driver": "AUO", "pattern": "AUO"}
        entity = {"ticker": "INTC", "items": [{"title": "AUO deal", "summary": ""}],
                  "coverage": {}}
        scored = score_case(case, {"entities": [entity]})
        self.assertTrue(scored["collection_hit"])
        self.assertNotIn("triage_hit", scored)

    def test_replay_uses_original_am_premarket_move(self):
        context = """## [Context] 2026-09-21 开盘前简报
_运行时间: 2026-09-21 08:30 EDT_
### 价格快照
| 标的 | 昨收价 | 昨日全日↑↓（vs前收）| 盘前↑↓（vs昨收）| 5日↑↓ | 异动 |
|---|---|---|---|---|---|
| 英特尔(INTC) | $108.60 | -0.18% | +5.47% | +5.50% | [!] |
### 搜索任务
"""
        rows = _context_price_rows(context, "2026-09-21", "am", ["INTC"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].change_pct, 5.47)
        self.assertTrue(rows[0].is_anomaly)


if __name__ == "__main__":
    unittest.main()
