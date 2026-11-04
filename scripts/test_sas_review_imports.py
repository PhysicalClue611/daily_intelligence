import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import intel_sources
import run_finance
import sas_review


class SasReviewImportsTests(unittest.TestCase):
    def test_run_finance_references_exist(self):
        source = Path(sas_review.__file__).read_text(encoding="utf-8")
        names = set(re.findall(r"\brf\.([A-Za-z_][A-Za-z_0-9]*)", source))
        missing = sorted(name for name in names if not hasattr(run_finance, name))
        self.assertFalse(missing, f"Missing run_finance references: {', '.join(missing)}")

    def test_finnhub_news_reaches_review_prompt(self):
        marker = "FINNHUB_NEWS_TEST_MARKER"
        with (
            patch.object(sas_review, "_fetch_earnings_anchor", return_value={"epsActual": 1.0}),
            patch.object(sas_review.rf, "_compute_holding_signals", return_value="holding signals"),
            patch.object(sas_review.seu, "get_insider_buys", return_value=[]),
            patch.object(sas_review.seu, "get_risk_factor_diff_input", return_value=None),
            patch.object(sas_review, "_load_candidate_entries", return_value=""),
            patch.object(sas_review, "_fetch_tavily_context", return_value=""),
            patch.object(sas_review.rf, "_load_personal_context", return_value="personal context"),
            patch.object(sas_review, "_load_sas_rubric", return_value="rubric"),
            patch.object(sas_review, "_call_sas_review_llm", return_value={"scores": {}}) as llm,
            patch.object(sas_review, "write_sas_review_file"),
            patch.object(sas_review, "send_review_email"),
            patch.object(sas_review.rf, "send_telegram_alert"),
            patch.object(intel_sources, "fetch_finnhub_news", return_value=marker) as news,
        ):
            sas_review.run_ticker_review("INTC", trigger_reason="手工指定")

        news.assert_called_once_with(["INTC"], hours=sas_review.FINNHUB_NEWS_LOOKBACK_HOURS)
        llm.assert_called_once()
        self.assertIn(marker, llm.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
