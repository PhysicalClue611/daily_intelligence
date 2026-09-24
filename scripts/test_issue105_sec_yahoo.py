"""Issue #105: SEC 8-K filings and per-ticker Yahoo RSS in Pass 0. No network."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).parent))

import intel_collect as collect
from intel_deepen import _direct_leads
from intel_render import coverage_line, render_intel_snapshot_context


AS_OF = datetime(2026, 9, 21, 21, tzinfo=timezone.utc)
SINCE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K  - Current report</title>
    <link href="https://www.sec.gov/Archives/edgar/data/50863/000119312526346806/0001193125-26-346806-index.htm" rel="alternate" type="text/html"/>
    <updated>2026-09-21T16:05:07-04:00</updated>
    <summary type="html">Filed: 2026-09-21 AccNo: 0001193125-26-346806 Size: 12 KB Item 1.01: Entry into a Material Definitive Agreement Item 3.02: Unregistered Sales of Equity Securities</summary>
    <filing-type>8-K</filing-type>
    <accession-number>0001193125-26-346806</accession-number>
    <filing-date>2026-09-21</filing-date>
  </entry>
  <entry>
    <title>8-K  - Current report</title>
    <link href="https://www.sec.gov/Archives/edgar/data/50863/000005086326000001/0000050863-26-000001-index.htm" rel="alternate"/>
    <updated>2026-08-01T16:05:07-04:00</updated>
    <summary type="html">Item 8.01: Other Events</summary>
    <filing-type>8-K</filing-type>
    <accession-number>0000050863-26-000001</accession-number>
    <filing-date>2026-08-01</filing-date>
  </entry>
</feed>
"""
YAHOO = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Intel signs AUO partnership</title>
    <link>https://www.benzinga.com/news/intel-auo</link>
    <pubDate>Mon, 21 Sep 2026 16:00:00 +0000</pubDate>
    <description>Packaging deal</description>
  </item>
  <item>
    <title>Old Intel story outside the window</title>
    <link>https://finance.yahoo.com/news/old-intel</link>
    <pubDate>Tue, 01 Sep 2026 12:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def _response(content: bytes):
    return type("Response", (), {"content": content, "raise_for_status": lambda self: None})()


class Issue105Test(unittest.TestCase):
    def test_sec_8k_parses_in_window_filing_and_drops_older_one(self):
        seen = {}

        def fake(url, **kwargs):
            seen["url"] = url
            seen["params"] = kwargs.get("params")
            seen["headers"] = kwargs.get("headers")
            return _response(ATOM)

        with patch.dict("os.environ", {"FINANCE_FROM_ADDRESS": "tester@example.com"}), \
             patch.object(collect, "_request", side_effect=fake):
            rows = collect.fetch_sec_8k("INTC", SINCE, AS_OF, "0000050863")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "SEC 8-K")
        self.assertEqual(row["publisher_domain"], "sec.gov")
        self.assertEqual(row["url_kind"], "sec_filing")
        self.assertEqual(row["url"], "https://www.sec.gov/Archives/edgar/data/50863/000119312526346806/0001193125-26-346806-index.htm")
        self.assertIn("1.01", row["title"])
        self.assertIn("3.02", row["title"])
        self.assertNotIn("8.01", row["title"])
        self.assertIn("0001193125-26-346806", row["title"])
        self.assertEqual(seen["params"]["CIK"], "0000050863")
        self.assertEqual(seen["params"]["type"], "8-K")
        self.assertEqual(seen["params"]["output"], "atom")
        self.assertEqual(seen["params"]["dateb"], "20260922")
        self.assertIn("tester@example.com", seen["headers"]["User-Agent"])
        self.assertNotIn("8.01", row["summary"])

    def test_sec_8k_request_failure_raises_for_the_caller_to_isolate(self):
        def fake(*_args, **_kwargs):
            raise httpx.ConnectError("down")

        with patch.object(collect, "_request", side_effect=fake):
            with self.assertRaises(httpx.ConnectError):
                collect.fetch_sec_8k("INTC", SINCE, AS_OF, "0000050863")

    def test_yahoo_rss_keeps_window_and_real_article_domain(self):
        def fake(url, **kwargs):
            self.assertEqual(url, "https://feeds.finance.yahoo.com/rss/2.0/headline")
            self.assertEqual(kwargs["params"]["s"], "INTC")
            self.assertIn("Mozilla/5.0", kwargs["headers"]["User-Agent"])
            return _response(YAHOO)

        with patch.object(collect, "_request", side_effect=fake):
            rows = collect.fetch_yahoo_rss("INTC", SINCE, AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "Yahoo RSS")
        self.assertEqual(rows[0]["url_kind"], "direct")
        self.assertEqual(rows[0]["publisher_domain"], "benzinga.com")
        self.assertEqual(rows[0]["title"], "Intel signs AUO partnership")

    def test_yahoo_title_merges_with_finnhub_and_keeps_both_domains(self):
        now = datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
        finnhub = collect.item("Intel signs AUO partnership", "", "Finnhub", "finnhub.io",
                               "https://finnhub.io/api/news?id=1", "finnhub_redirect", now)
        yahoo = collect.item("Intel signs AUO partnership", "Packaging deal", "Yahoo RSS",
                             "benzinga.com", "https://www.benzinga.com/news/intel-auo", "direct", now)
        entity = collect.assemble_entity("INTC", "Intel", ["Intel"], True, None, {},
                                         {"finnhub": [finnhub], "yahoo_rss": [yahoo]}, [], now)
        self.assertEqual(entity["coverage"]["finnhub"], 1)
        self.assertEqual(entity["coverage"]["yahoo_rss"], 1)
        self.assertEqual(entity["coverage"]["sec_8k"], 0)
        self.assertEqual(len(entity["items"]), 1)
        self.assertEqual(set(entity["items"][0]["publisher_domains"]), {"finnhub.io", "benzinga.com"})

    def test_missing_cik_is_coverage_only(self):
        now = AS_OF
        story = collect.item("Intel news", "", "Finnhub", "a.com", "https://a.com/1", "direct", now)
        with patch.object(collect, "resolve_ciks", return_value={"INTC": ""}), \
             patch.object(collect, "fetch_sec_8k", side_effect=AssertionError("fetched without CIK")), \
             patch.object(collect, "fetch_finnhub", return_value=[story]), \
             patch.object(collect, "fetch_google_news", return_value=[]), \
             patch.object(collect, "fetch_yahoo_rss", return_value=[]), \
             patch.object(collect, "fetch_rss_pool", return_value=([], [])), \
             patch.object(collect, "fetch_guardian_pool", return_value=([], [])):
            entities, _ = collect.collect(["INTC"], {"INTC": ["Intel"]}, set(), {}, {}, {}, now, "pm")
        self.assertEqual(entities[0]["items"][0]["title"], "Intel news")
        self.assertIn("sec_8k: CIK not found", entities[0]["coverage"]["errors"])
        self.assertEqual(entities[0]["coverage"]["finnhub"], 1)

    def test_sec_request_failure_does_not_drop_other_sources(self):
        now = AS_OF
        story = collect.item("Intel news", "", "Finnhub", "a.com", "https://a.com/1", "direct", now)

        def broken(*_args):
            raise httpx.ConnectError("down")

        with patch.object(collect, "resolve_ciks", return_value={"INTC": "0000050863"}), \
             patch.object(collect, "fetch_sec_8k", side_effect=broken), \
             patch.object(collect, "fetch_finnhub", return_value=[story]), \
             patch.object(collect, "fetch_google_news", return_value=[]), \
             patch.object(collect, "fetch_yahoo_rss", return_value=[]), \
             patch.object(collect, "fetch_rss_pool", return_value=([], [])), \
             patch.object(collect, "fetch_guardian_pool", return_value=([], [])):
            entities, _ = collect.collect(["INTC"], {"INTC": ["Intel"]}, set(), {}, {}, {}, now, "pm")
        self.assertEqual(entities[0]["coverage"]["finnhub"], 1)
        self.assertTrue(any(err.startswith("sec_8k:") for err in entities[0]["coverage"]["errors"]))

    def test_replay_fetches_sec_and_skips_yahoo(self):
        now = AS_OF
        filing = collect.item("8-K Item 1.01 0001193125-26-346806", "Item 1.01", "SEC 8-K",
                              "sec.gov", "https://www.sec.gov/Archives/x-index.htm", "sec_filing", now)
        with patch.object(collect, "resolve_ciks", return_value={"INTC": "0000050863"}), \
             patch.object(collect, "fetch_sec_8k", return_value=[filing]) as sec, \
             patch.object(collect, "fetch_yahoo_rss", side_effect=AssertionError("replay fetched Yahoo")), \
             patch.object(collect, "fetch_finnhub", return_value=[]), \
             patch.object(collect, "fetch_google_news", return_value=[]), \
             patch.object(collect, "fetch_rss_pool", side_effect=AssertionError("replay fetched RSS")), \
             patch.object(collect, "fetch_guardian_pool", return_value=([], [])):
            entities, _ = collect.collect(["INTC"], {"INTC": ["Intel"]}, set(), {}, {}, {}, now, "pm", replay=True)
        sec.assert_called_once()
        self.assertEqual(entities[0]["coverage"]["sec_8k"], 1)
        self.assertIn("yahoo_rss: skipped in replay", entities[0]["coverage"]["errors"])
        self.assertEqual(entities[0]["items"][0]["url_kind"], "sec_filing")

    def test_render_marks_filing_items_and_coverage(self):
        now = AS_OF
        filing = collect.item("8-K Item 1.01, 3.02 0001193125-26-346806",
                              "Item 1.01: agreement", "SEC 8-K", "sec.gov",
                              "https://www.sec.gov/Archives/x-index.htm", "sec_filing", now)
        entity = collect.assemble_entity("INTC", "Intel", ["Intel"], True, None,
                                         {"d1": 4.0, "flags": ["anomaly"]},
                                         {"sec_8k": [filing]}, [], now)
        text = render_intel_snapshot_context(
            {"entities": [entity], "macro_digest": {"items": []}}, {})
        self.assertIn("公司公告（8-K Item 1.01、3.02）", text)
        self.assertIn("0001193125-26-346806", text)
        self.assertIn("SEC 8-K 1", coverage_line(entity))

    def test_sec_filing_stays_out_of_extract_and_yahoo_direct_is_eligible(self):
        now = AS_OF
        filing = collect.item("8-K Item 1.01 0001193125-26-346806", "", "SEC 8-K", "sec.gov",
                              "https://www.sec.gov/Archives/x-index.htm", "sec_filing", now)
        yahoo = collect.item("Intel raises forecast", "", "Yahoo RSS", "benzinga.com",
                             "https://www.benzinga.com/news/intel-forecast", "direct", now)
        leads = _direct_leads({"ticker": "INTC", "aliases": ["Intel"], "items": [filing, yahoo]})
        urls = [url for url, _row in leads]
        self.assertEqual(urls, ["https://www.benzinga.com/news/intel-forecast"])

    def test_cik_map_pads_and_remembers_misses(self):
        body = {"0": {"cik_str": 50863, "ticker": "INTC", "title": "INTEL CORP"}}

        def fake(url, **_kwargs):
            self.assertEqual(url, "https://www.sec.gov/files/company_tickers.json")
            return type("Response", (), {"json": lambda self: body, "raise_for_status": lambda self: None})()

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cik_cache.json"
            with patch.object(collect, "_request", side_effect=fake) as requested:
                first = collect.resolve_ciks(["INTC", "ZZZZ"], path)
                second = collect.resolve_ciks(["INTC", "ZZZZ"], path)
            self.assertEqual(first, {"INTC": "0000050863", "ZZZZ": ""})
            self.assertEqual(second, first)
            self.assertEqual(requested.call_count, 1)
            self.assertEqual(json.loads(path.read_text())["ZZZZ"], "")


if __name__ == "__main__":
    unittest.main()
