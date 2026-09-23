"""Offline PR2 contracts: recent coverage and prompt state."""
import sys
import tempfile
import unittest
from string import Formatter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from recent_coverage import build_recent_coverage_section
from pass2_context import changed_background, current_state, read_previous_ledger_state


class RecentCoverageTest(unittest.TestCase):
    def _write(self, root: Path, month: str, text: str) -> None:
        (root / f"Daily_Intel_report_{month}.md").write_text(text, encoding="utf-8")

    def test_only_entity_blocks_and_600_char_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, "202609", """## 2026-09-17 开盘前简报
## 要点
Intel and SK Hynix are discussing an Ohio arrangement.\n\n
Unrelated macro paragraph about yields.\n\n
- INTC later clarified no contract was signed.\n
- Nvidia announced a separate product.\n
| 标的 | 变化 |
| INTC | +3.4% |
| NVDA | +2.0% |
""")
            result = build_recent_coverage_section(root, "2026-09-18", "am", ["INTC"],
                                                   {"INTC": ["Intel", "英特尔"]})
            self.assertIn("SK Hynix", result)
            self.assertIn("no contract", result)
            self.assertIn("| INTC |", result)
            self.assertNotIn("Unrelated macro", result)
            self.assertNotIn("Nvidia announced", result)
            self.assertLessEqual(len(result.split("### INTC\n", 1)[1]), 600)

    def test_excludes_current_slot_and_keeps_first_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, "202609", """## 2026-09-17 开盘前简报
Intel first AM report.\n\n
## 2026-09-17 开盘前简报
Intel duplicate rerun.\n\n
## 2026-09-17 夜盘收市速报
Intel PM report.\n\n
## 2026-09-18 开盘前简报
Intel current slot must be excluded.
""")
            result = build_recent_coverage_section(root, "2026-09-18", "am", ["INTC"],
                                                   {"INTC": ["Intel"]})
            self.assertIn("first AM", result)
            self.assertIn("PM report", result)
            self.assertNotIn("duplicate rerun", result)
            self.assertNotIn("current slot", result)

    def test_cross_month_and_chinese_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, "202608", "## 2026-08-31 夜盘收市速报\n英特尔与友达洽谈先进封装。\n")
            self._write(root, "202609", "## 2026-09-01 开盘前简报\nINTC 盘前上涨。\n")
            result = build_recent_coverage_section(root, "2026-09-01", "pm", ["INTC"],
                                                   {"INTC": ["Intel", "英特尔"]})
            self.assertIn("英特尔与友达", result)
            self.assertIn("[08-31 PM]", result)
            self.assertIn("[09-01 AM]", result)

    def test_no_prior_mention_is_empty_and_latin_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, "202609", "## 2026-09-17 开盘前简报\nArtificial intelligence broadens.\n")
            result = build_recent_coverage_section(root, "2026-09-18", "am", ["INTC"],
                                                   {"INTC": ["Intel"]})
            self.assertEqual(result, "")

    def test_am_uses_five_prior_sessions_and_pm_adds_today_am(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, "202609", """## 2026-09-14 开盘前简报
Intel earliest of five prior sessions.

## 2026-09-21 开盘前简报
Intel current morning update.

## 2026-09-21 夜盘收市速报
Intel current evening update.
""")
            am = build_recent_coverage_section(root, "2026-09-21", "am", ["INTC"], {"INTC": ["Intel"]})
            pm = build_recent_coverage_section(root, "2026-09-21", "pm", ["INTC"], {"INTC": ["Intel"]})
            self.assertIn("earliest of five", am)
            self.assertNotIn("current morning", am)
            self.assertIn("earliest of five", pm)
            self.assertIn("current morning", pm)
            self.assertNotIn("current evening", pm)


class BackgroundChangeTest(unittest.TestCase):
    def test_only_changed_liquidity_crossing_and_new_extrema(self):
        previous = {"liquidity_tier": "正常", "weights": {"INTC": 14.8},
                    "range_extrema": {"INTC": {"close": 49.0, "high": 50.0, "low": 20.0}}}
        current = {"liquidity_tier": "观察", "weights": {"INTC": 15.2},
                   "range_extrema": {"INTC": {"close": 51.0, "high": 51.0, "low": 20.0}}}
        liquidity, holding = changed_background("FRED 观察", current, previous)
        self.assertEqual(liquidity, "FRED 观察")
        self.assertIn("15.2%", holding)
        self.assertIn("52周新高", holding)
        self.assertEqual(changed_background("FRED 观察", current, current), ("", ""))

    def test_first_snapshot_and_no_data_do_not_fabricate_changes(self):
        current = {"liquidity_tier": "正常", "weights": {"INTC": 7.0},
                   "range_extrema": {"INTC": {"high": 50.0, "low": 20.0}}}
        self.assertEqual(changed_background("FRED 正常", current, {}), ("", ""))
        self.assertEqual(changed_background("", {}, current), ("", ""))

    def test_previous_state_comes_from_latest_earlier_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "202609").mkdir()
            old = {"as_of": "2026-09-17T08:30:00-04:00",
                   "context_state": {"liquidity_tier": "正常"}}
            new = {"as_of": "2026-09-18T08:30:00-04:00",
                   "context_state": {"liquidity_tier": "观察"}}
            (root / "202609" / "2026-09-17-am-ledger.json").write_text(__import__('json').dumps(old))
            (root / "202609" / "2026-09-18-am-ledger.json").write_text(__import__('json').dumps(new))
            from datetime import datetime
            before = datetime.fromisoformat("2026-09-18T09:00:00-04:00")
            self.assertEqual(read_previous_ledger_state(root, before)["liquidity_tier"], "观察")

    def test_temporary_missing_sources_do_not_erase_previous_state(self):
        previous = {"liquidity_tier": "观察", "weights": {"INTC": 14.8},
                    "range_extrema": {"INTC": {"close": 48.0, "high": 50.0, "low": 20.0}}}
        self.assertEqual(current_state("", {}, {}, previous), previous)


class PromptContractTest(unittest.TestCase):
    def test_runtime_layer_a_replaces_old_actionability_line(self):
        import run_finance as rf
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "layer_a.md"
            path.write_text('原则\n- 结论必须可操作：给出具体价格区间、触发条件或观察信号；不给模糊的"持续关注"\n', encoding="utf-8")
            old = rf._LAYER_A_PATH
            try:
                rf._LAYER_A_PATH = path
                result = rf._load_layer_a()
            finally:
                rf._LAYER_A_PATH = old
            self.assertIn("只在出现可操作事实时给结论", result)
            self.assertNotIn("结论必须可操作", result)
            self.assertIn("原则", result)

    def test_pass2_skeleton_and_no_boilerplate_triggers(self):
        import run_finance as rf
        template = rf.USER_PROMPT_TEMPLATE_P2
        self.assertIn("{recent_coverage_section}", template)
        self.assertIn("{ledger_section}", template)
        self.assertNotIn("{news_text}", template)
        self.assertIn("## 持仓与观察标的", template)
        self.assertIn("## 仓位（仅出现", template)
        self.assertNotIn("一条都不命中", template)
        self.assertNotIn("独立域名佐证", template)
        self.assertNotIn("驱动因素归类（能力圈内外）", template)
        self.assertIn("FRED 档位变化或52周新高/新低，也允许写仓位小节", template)
        fields = {name: "" for _, name, _, _ in Formatter().parse(template) if name}
        fields.update(date="2026-09-18", ledger_section="### INTC\n- [news.example] Intel new deal",
                      recent_coverage_section="## 近 5 个交易日已报道（同一标的）\n### INTC\n[09-17 PM] Intel update",
                      verifiable_signals_rule="## 可验证信号")
        rendered = template.format(**fields)
        self.assertIn("Intel new deal", rendered)
        self.assertIn("[09-17 PM] Intel update", rendered)
        self.assertIn("## 可验证信号", rendered)


if __name__ == "__main__":
    unittest.main()
