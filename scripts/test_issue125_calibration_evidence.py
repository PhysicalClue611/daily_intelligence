"""Synthetic regression coverage for ticker-selected calibration evidence."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import calibration


class CalibrationEvidenceTests(unittest.TestCase):
    def build(self, signals, snapshot, sonar="", watchlist=None):
        return calibration._build_calibration_evidence(
            signals, snapshot, sonar, watchlist or []
        )

    def test_section_split_and_signal_order(self):
        snapshot = (
            "## 标的事实与来源（供分析）\n"
            "### INTC（持有）\nintel evidence\n"
            "### PLTR（观察）\npalantir evidence\n"
            "### ORCL（持有）\noracle evidence\n"
            "无异动观察标的：AMD\n"
            "## 宏观与地缘线索\nmacro evidence\n"
        )
        evidence = self.build("**ORCL**：first; INTC second", snapshot, "sonar evidence")
        self.assertNotIn("### PLTR（", evidence)
        self.assertNotIn("无异动观察标的：", evidence)
        self.assertLess(evidence.index("### ORCL（"), evidence.index("### INTC（"))
        self.assertLess(evidence.index("### INTC（"), evidence.index("## Sonar 宏观"))
        self.assertTrue(evidence.endswith("## 宏观与地缘线索\nmacro evidence\n"))

    def test_raw_headings_do_not_split_fulltext(self):
        section = (
            "### PLTR（持有）\n  正文[url] article\n"
            "### CEL-SCI reports results\nraw subheading\n"
            "## Another heading\nlast evidence\n"
        )
        snapshot = section + "### ORCL（观察）\nother\n"
        self.assertEqual(self.build("PLTR", snapshot), section)

    def test_ticker_boundaries_and_bold(self):
        snapshot = "### INTC（持有）\nintel\n### AMD（观察）\namd\n### PLTR（持有）\npltr\n"
        evidence = self.build("INTCX XINTC AMDX XAMD **PLTR**：", snapshot)
        self.assertEqual(evidence, "### PLTR（持有）\npltr\n")

    def test_per_section_and_total_caps(self):
        sections = [f"### {ticker}（持有）\n" + char * 9000
                    for ticker, char in [("INTC", "i"), ("PLTR", "p"), ("ORCL", "o")]]
        snapshot = "\n".join(sections) + "\n## 宏观与地缘线索\n" + "m" * 4000
        self.assertEqual(self.build("INTC", snapshot.split("\n## 宏观")[0]), sections[0][:8000])
        evidence = self.build("INTC PLTR ORCL", snapshot, "s" * 4000)
        expected = "\n".join(section[:8000] for section in sections)[:24000]
        self.assertEqual(evidence, expected)
        self.assertEqual(len(evidence), 24000)
        self.assertTrue(evidence.startswith(sections[0][:8000]))

    def test_no_named_ticker_only_sonar_and_macro(self):
        snapshot = "### PLTR（持有）\ncompany\n## 宏观与地缘线索\nmacro\n"
        self.assertEqual(self.build("WTI原油", snapshot, "s" * 4000),
                         "## Sonar 宏观\n" + "s" * 3000 + "\n## 宏观与地缘线索\nmacro\n")
        self.assertEqual(self.build("WTI", "### PLTR（持有）\ncompany\n"), "")

    def test_named_watchlist_ticker_without_section(self):
        evidence = self.build("AMD INTC PLTR AMDX", "### PLTR（持有）\nnews\n",
                              watchlist=["INTC", "AMD", "PLTR", "AMDX"])
        self.assertTrue(evidence.startswith("以下被点名标的在今日情报快照中无条目：AMD、INTC、AMDX\n"))
        self.assertIn("### PLTR（", evidence)

    def test_end_to_end_prompt_keeps_late_evidence_and_rules(self):
        marker = "PLTR revenue 2.31B reported"
        snapshot = "### INTC（持有）\n" + "i" * 4000 + "\n### PLTR（观察）\n" + "p" * 3500 + marker
        self.assertGreater(snapshot.index(marker), 3000)
        with tempfile.TemporaryDirectory() as tmp:
            monthly = Path(tmp) / "monthly.md"
            monthly.write_text("## 2026-11-02 开盘前简报\n\n## 可验证信号\n- **PLTR**：是否发布业绩\n", encoding="utf-8")
            with patch.object(calibration, "_monthly_path", return_value=monthly), \
                 patch.object(calibration, "OPENROUTER_API_KEY", "synthetic-test-key"), \
                 patch.object(calibration, "call_llm", return_value={"knowledge_entry": "lesson", "verdicts": []}) as llm, \
                 patch.object(calibration, "_write_calibration_knowledge") as knowledge, \
                 patch.object(calibration, "_write_calibration_metrics") as metrics:
                result = calibration.evaluate_am_calibration(
                    "2026-11-02", "pm", "prices", snapshot, "sonar", "report sentinel",
                    watchlist_tickers=["PLTR"]
                )
                self.assertEqual(result, "report sentinel")
                prompt = llm.call_args.args[0]
                self.assertIn(marker, prompt)
                for rule in ["## 情报证据（按断言点名的标的挑选）", "条件成立判 hit，不成立判 miss",
                             "有段落但未见相关报道", 'reasoning 以"所给来源内未见"开头',
                             '只有该标的被列为"无条目"、且无法凭价格表判定时，才判 inconclusive']:
                    self.assertIn(rule, prompt)
                self.assertNotIn("report sentinel", prompt)
                knowledge.assert_called_once()
                metrics.assert_called_once()

    def test_builder_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            monthly = Path(tmp) / "monthly.md"
            monthly.write_text("## 2026-11-02 开盘前简报\n\n## 可验证信号\n- PLTR：业绩\n", encoding="utf-8")
            with patch.object(calibration, "_monthly_path", return_value=monthly), \
                 patch.object(calibration, "_build_calibration_evidence", side_effect=RuntimeError("synthetic failure")), \
                 patch.object(calibration, "call_llm") as llm, \
                 patch.object(calibration, "_write_calibration_knowledge") as knowledge, \
                 patch.object(calibration, "_write_calibration_metrics") as metrics:
                self.assertEqual(calibration.evaluate_am_calibration(
                    "2026-11-02", "pm", "prices", "snapshot", "sonar", "original",
                    watchlist_tickers=["PLTR"]), "original")
                llm.assert_not_called()
                knowledge.assert_not_called()
                metrics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
