"""PR1 contracts for the shadow Pass 0 ledger (no network or paid calls)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import intel_collect as collect
import intel_triage as triage
from eval.evaluate_issue87 import score_case
from intel_pass0 import _context_price_rows


class PassZeroTest(unittest.TestCase):
    def test_word_boundaries_and_short_alias_case(self):
        aliases = {"INTC": ["Intel", "英特尔"], "ARM": ["Arm"], "AUO": ["AUO"]}
        self.assertEqual(collect.match_entities("Intel and AUO agree on packaging", aliases), {"INTC", "AUO"})
        self.assertEqual(collect.match_entities("Arm shares rise", aliases), {"ARM"})
        self.assertEqual(collect.match_entities("farm platform Warner", aliases), set())
        self.assertEqual(collect.match_entities("ARM shares rise", aliases), set())

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

    def test_invalid_triage_preserves_items(self):
        ledger = {"ticker": "INTC", "name": "Intel", "items": [{"id": "i1", "title": "Intel AUO deal"}],
                  "move": {"flags": ["anomaly"]}}
        with patch.object(triage, "call_llm", return_value={"move_status": "unexplained", "events": "bad"}):
            result = triage.triage_entity(ledger, [])
        self.assertEqual(result["move_status"], "triage_failed")
        self.assertEqual(result["items"][0]["title"], "Intel AUO deal")

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

    def test_valid_triage_primary_refs(self):
        ledger = {"ticker": "INTC", "name": "Intel", "aliases": ["Intel"],
                  "items": [{"id": "i1", "title": "Intel AUO deal"}], "move": {"flags": ["anomaly"]}}
        answer = {"events": [{"id": "e1", "headline": "Intel AUO partnership", "date": "2026-09-21",
                              "type": "partnership", "company_specific": True, "item_ids": ["i1"],
                              "seen_before": False, "need_fulltext": True}],
                  "move_status": "explained", "primary_event_ids": ["e1"], "gap_query": ""}
        with patch.object(triage, "call_llm", return_value=answer):
            result = triage.triage_entity(ledger, [])
        self.assertEqual(result["move_status"], "explained")
        self.assertEqual(result["primary_event_ids"], ["e1"])

    def test_acceptance_scores_only_primary_event_and_non_unexplained(self):
        case = {"date": "2026-09-21", "slot": "am", "ticker": "INTC", "driver": "AUO", "pattern": "AUO"}
        entity = {"ticker": "INTC", "items": [{"title": "AUO deal", "summary": ""}],
                  "events": [{"id": "e1", "headline": "Unrelated"}, {"id": "e2", "headline": "AUO deal"}],
                  "primary_event_ids": ["e1"], "move_status": "partial", "coverage": {}}
        scored = score_case(case, {"entities": [entity]})
        self.assertTrue(scored["collection_hit"])
        self.assertFalse(scored["triage_hit"])
        entity["primary_event_ids"] = ["e2"]
        entity["move_status"] = "unexplained"
        self.assertFalse(score_case(case, {"entities": [entity]})["triage_hit"])
        entity["move_status"] = "partial"
        self.assertTrue(score_case(case, {"entities": [entity]})["triage_hit"])

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
