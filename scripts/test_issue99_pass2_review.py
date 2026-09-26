"""Issue #99: 15% crossings, Pass 2 wording, truncation downgrade, snapshot meta."""
import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from pass2_context import changed_background


ET = ZoneInfo("America/New_York")
PASS2_META_KEYS = (
    "model", "provider", "attempts", "fallback",
    "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "finish_reason", "effort_used",
)


class WeightAndExtremaTest(unittest.TestCase):
    def test_qqqm_never_crosses_and_stock_crossings_need_a_previous_weight(self):
        _, up = changed_background(
            "", {"weights": {"INTC": 16}}, {"weights": {"INTC": 14}},
        )
        self.assertIn("INTC", up)
        self.assertIn("超过15%", up)

        _, first = changed_background("", {"weights": {"INTC": 16}}, {})
        self.assertNotIn("INTC", first)
        self.assertNotIn("超过15%", first)

        _, down = changed_background(
            "", {"weights": {"INTC": 14}}, {"weights": {"INTC": 16}},
        )
        self.assertIn("降至15%及以下", down)

        for previous in ({}, {"weights": {"QQQM": 10}}, {"weights": {"QQQM": 20}}):
            current_weight = 14 if previous.get("weights", {}).get("QQQM") == 20 else 19
            _, holding = changed_background(
                "", {"weights": {"QQQM": current_weight}}, previous,
            )
            self.assertNotIn("QQQM", holding)

    def test_new_extrema_require_a_previous_print_for_that_ticker(self):
        current = {"weights": {}, "range_extrema": {
            "INTC": {"close": 60.0, "high": 60.0, "low": 20.0},
        }}
        _, missing = changed_background("", current, {})
        self.assertNotIn("52周新高", missing)
        previous = {"range_extrema": {"INTC": {"close": 50.0, "high": 55.0, "low": 20.0}}}
        _, seen = changed_background("", current, previous)
        self.assertIn("52周新高", seen)


class PromptWordingTest(unittest.TestCase):
    def test_prompt_and_pm_note_state_the_new_rules(self):
        import run_finance as rf
        template = rf.USER_PROMPT_TEMPLATE_P2
        self.assertIn("材料里没人提出的推论", template)
        self.assertIn("同一标的只说一次", template)
        self.assertIn("或近一周事件正在发酵并伴随价格变动的标的", template)
        self.assertIn("不复述价格表数字", template)
        self.assertIn("已排期的供给事件", template)
        self.assertNotIn("分别说明日内表现与盘后", template)
        now = datetime(2026, 9, 23, 20, 10, tzinfo=ET)
        note = rf._pm_afterhours_note("pm", now)
        self.assertIn("20:10", note)
        self.assertIn("只在盘后走势与日内方向相反、或盘后变动达到异动阈值时说明", note)
        self.assertIn("盘后无成交只在对异动标的有意义时注明", note)
        self.assertNotIn("分别说明日内表现与盘后", note)
        self.assertEqual(rf._pm_afterhours_note("am", now), "")


class TruncationDowngradeTest(unittest.TestCase):
    def _client(self):
        import importlib
        import llm_client
        importlib.reload(llm_client)
        return llm_client

    def _install(self, llm_client, responses):
        calls = []

        class _Resp:
            def __init__(self, content, finish, usage=None):
                self._content, self._finish, self._usage = content, finish, usage or {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"provider": "OpenAI", "usage": self._usage,
                        "choices": [{"finish_reason": self._finish,
                                     "message": {"content": self._content}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            content, finish, usage = responses[len(calls) - 1]
            return _Resp(content, finish, usage)

        llm_client.httpx.post = fake_post
        llm_client.time.sleep = lambda *_a, **_k: None
        return calls

    def test_length_downgrades_effort_once_then_falls_back(self):
        llm_client = self._client()
        import llm_config
        usage = {"prompt_tokens": 11, "completion_tokens": 22,
                 "completion_tokens_details": {"reasoning_tokens": 17}}
        fallback = llm_config.stage("report_pass2")["fallback_model"]
        calls = self._install(llm_client, [
            ("# Report\n\n半句，", "length", usage),
            ("# Report\n\n仍然半句，", "length", usage),
            ("# Report\n\n完整降级正文。", "stop", usage),
        ])
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Handler()
        logger = logging.getLogger("llm_client")
        logger.addHandler(handler)
        before = llm_config.stage("report_pass2")["reasoning"]["effort"]
        try:
            out = llm_client.call_llm(
                "p", system_prompt="s", stage="report_pass2",
                parse_json=False, max_retries=2,
            )
        finally:
            logger.removeHandler(handler)
        self.assertEqual(before, "xhigh")
        self.assertEqual(llm_config.stage("report_pass2")["reasoning"]["effort"], "xhigh")
        self.assertEqual(out["text"], "# Report\n\n完整降级正文。")
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(calls[0]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls[1]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls[1]["reasoning"]["effort"], "high")
        first = dict(calls[0])
        second = dict(calls[1])
        self.assertEqual(first.pop("reasoning")["effort"], "xhigh")
        self.assertEqual(second.pop("reasoning")["effort"], "high")
        self.assertEqual(first, second)
        self.assertEqual(calls[2]["model"], fallback)
        self.assertTrue(any("downgrading once to high" in line for line in records))
        self.assertTrue(any("entering fallback" in line for line in records))
        self.assertTrue(out["_llm_meta"]["fallback"])

    def test_downgraded_stop_keeps_meta_and_does_not_call_fallback(self):
        llm_client = self._client()
        usage = {"prompt_tokens": 11, "completion_tokens": 22,
                 "completion_tokens_details": {"reasoning_tokens": 17}}
        calls = self._install(llm_client, [
            ("# Report\n\n半句，", "length", usage),
            ("# Report\n\n降档后写完。", "stop", usage),
        ])
        out = llm_client.call_llm(
            "p", system_prompt="s", stage="report_pass2",
            parse_json=False, max_retries=2,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["text"], "# Report\n\n降档后写完。")
        meta = out["_llm_meta"]
        for key in PASS2_META_KEYS:
            self.assertIn(key, meta)
        self.assertEqual(meta["effort_used"], "high")
        self.assertTrue(meta["truncation_downgrade"])
        self.assertFalse(meta["fallback"])
        self.assertEqual(meta["finish_reason"], "stop")
        self.assertEqual(meta["prompt_tokens"], 11)
        self.assertEqual(meta["completion_tokens"], 22)
        self.assertEqual(meta["reasoning_tokens"], 17)


class SnapshotPass2Test(unittest.TestCase):
    def test_archive_stores_success_meta_and_fallback_reason(self):
        import run_finance as rf
        from intel_collect import archive_intel_snapshot
        meta = {
            "model": "openai/gpt-6-luna",
            "provider": "OpenAI",
            "attempts": 2,
            "fallback": False,
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "reasoning_tokens": 17,
            "finish_reason": "stop",
            "effort_used": "high",
            "truncation_downgrade": True,
        }
        snap = {"date": "2026-09-23", "slot": "pm", "entities": []}
        rf._attach_pass2(snap, {"text": "ok", "_llm_meta": meta})
        with tempfile.TemporaryDirectory() as temp:
            path = archive_intel_snapshot(snap, Path(temp))
            saved = json.loads(path.read_text(encoding="utf-8"))
        for key in PASS2_META_KEYS:
            self.assertIn(key, saved["pass2"])
        self.assertEqual(saved["pass2"]["effort_used"], "high")
        failed = {"date": "2026-09-23", "slot": "am", "entities": []}
        rf._attach_pass2(failed, {}, reason="Pass 2 returned no report text")
        self.assertEqual(failed["pass2"], {
            "fallback_summary": True,
            "reason": "Pass 2 returned no report text",
        })


if __name__ == "__main__":
    unittest.main()
