"""Issue #101: watchlist edits must keep the next header, one recipient per line, and write atomically."""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import telegram_commands as tc

ORIGINAL = """## 个股与基金
INTC, NVDA

## 商品期货
GC=F

## 汇率
USDCNY=X

## 地缘政治关键词
US-Iran: Iran, IRGC
China-Taiwan: Taiwan

## 异动阈值
stock_pct: 3.0

## 收件人
a@x.com
"""

OTHER_SECTIONS = ("商品期货", "汇率", "地缘政治关键词", "异动阈值", "收件人")


def _section_body(text: str, title: str) -> str:
    marker = f"## {title}\n"
    start = text.index(marker) + len(marker)
    rest = text[start:]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


class SectionEditTest(unittest.TestCase):
    def test_each_edit_keeps_every_other_section_verbatim(self):
        edited, ok = tc._section_add(ORIGINAL, "个股与基金", "MSFT")
        self.assertTrue(ok)
        self.assertEqual(_section_body(edited, "个股与基金"), "INTC, NVDA, MSFT\n")
        for title in OTHER_SECTIONS:
            self.assertEqual(_section_body(edited, title), _section_body(ORIGINAL, title))
            self.assertIn(f"## {title}\n", edited)

        edited, ok = tc._section_add(ORIGINAL, "商品期货", "SI=F")
        self.assertTrue(ok)
        self.assertEqual(edited, ORIGINAL.replace("GC=F", "GC=F, SI=F"))

        edited, ok = tc._section_add(ORIGINAL, "汇率", "USDJPY=X")
        self.assertTrue(ok)
        self.assertEqual(edited, ORIGINAL.replace("USDCNY=X", "USDCNY=X, USDJPY=X"))

        edited, ok = tc._geo_add(ORIGINAL, "US-Iran", "Hormuz")
        self.assertTrue(ok)
        self.assertEqual(edited, ORIGINAL.replace("US-Iran: Iran, IRGC", "US-Iran: Iran, IRGC, Hormuz"))
        self.assertIn("## 异动阈值\nstock_pct: 3.0\n", edited)

        edited, ok = tc._geo_remove(ORIGINAL, "IRGC")
        self.assertTrue(ok)
        self.assertIn("## 异动阈值\n", edited)
        self.assertEqual(_section_body(edited, "异动阈值"), _section_body(ORIGINAL, "异动阈值"))

        edited, ok = tc._section_add(ORIGINAL, "收件人", "b@y.com")
        self.assertTrue(ok)
        self.assertEqual(edited, ORIGINAL.replace("a@x.com\n", "a@x.com\nb@y.com\n"))
        for title in ("个股与基金", "商品期货", "汇率", "地缘政治关键词", "异动阈值"):
            self.assertEqual(_section_body(edited, title), _section_body(ORIGINAL, title))

    def test_add_then_remove_is_byte_for_byte_the_original(self):
        cases = (
            ("section", "个股与基金", "MSFT"),
            ("section", "商品期货", "SI=F"),
            ("section", "汇率", "USDJPY=X"),
            ("section", "收件人", "b@y.com"),
            ("geo", "US-Iran", "Hormuz"),
        )
        for kind, section, item in cases:
            if kind == "geo":
                added, ok = tc._geo_add(ORIGINAL, section, item)
                self.assertTrue(ok)
                removed, ok = tc._geo_remove(added, item)
            else:
                added, ok = tc._section_add(ORIGINAL, section, item)
                self.assertTrue(ok)
                removed, ok = tc._section_remove(added, section, item)
            self.assertTrue(ok)
            self.assertEqual(removed, ORIGINAL)

    def test_second_recipient_parses_as_its_own_address(self):
        edited, ok = tc._section_add(ORIGINAL, "收件人", "b@y.com")
        self.assertTrue(ok)
        import run_finance as rf
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchlist.md"
            path.write_text(edited, encoding="utf-8")
            old = rf.WATCHLIST_PATH
            rf.WATCHLIST_PATH = path
            try:
                loaded = rf.load_watchlist()
            finally:
                rf.WATCHLIST_PATH = old
        self.assertEqual(loaded["recipients"], ["a@x.com", "b@y.com"])

    def test_last_section_has_no_following_header_to_keep(self):
        last = "## 商品期货\nGC=F\n"
        edited, ok = tc._section_add(last, "商品期货", "CL=F")
        self.assertTrue(ok)
        self.assertEqual(edited, "## 商品期货\nGC=F, CL=F\n")
        back, ok = tc._section_remove(edited, "商品期货", "CL=F")
        self.assertTrue(ok)
        self.assertEqual(back, last)

        recipients = "## 收件人\na@x.com\n"
        edited, ok = tc._section_add(recipients, "收件人", "b@y.com")
        self.assertTrue(ok)
        self.assertEqual(edited, "## 收件人\na@x.com\nb@y.com\n")
        back, ok = tc._section_remove(edited, "收件人", "b@y.com")
        self.assertTrue(ok)
        self.assertEqual(back, recipients)


class AtomicWriteTest(unittest.TestCase):
    def test_empty_text_does_not_replace_a_non_empty_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchlist.md"
            path.write_text(ORIGINAL, encoding="utf-8")
            old = tc.WATCHLIST_PATH
            tc.WATCHLIST_PATH = path
            try:
                with self.assertLogs(tc.logger, level=logging.ERROR):
                    tc._write_watchlist("")
                self.assertEqual(path.read_text(encoding="utf-8"), ORIGINAL)
                with self.assertLogs(tc.logger, level=logging.ERROR):
                    tc._write_watchlist("   \n")
                self.assertEqual(path.read_text(encoding="utf-8"), ORIGINAL)
            finally:
                tc.WATCHLIST_PATH = old

    def test_non_empty_write_replaces_and_reads_back(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchlist.md"
            path.write_text(ORIGINAL, encoding="utf-8")
            old = tc.WATCHLIST_PATH
            tc.WATCHLIST_PATH = path
            updated = ORIGINAL.replace("INTC, NVDA", "INTC, NVDA, MSFT")
            try:
                tc._write_watchlist(updated)
            finally:
                tc.WATCHLIST_PATH = old
            self.assertEqual(path.read_text(encoding="utf-8"), updated)


if __name__ == "__main__":
    unittest.main()
