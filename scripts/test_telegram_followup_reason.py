#!/usr/bin/env python3
"""
Regression tests for telegram_commands._followup_reason() (issue #11, updated
for the openai/gpt-5.6-luna + reasoning.effort=high switch, issue #60).

No pytest dependency — plain asserts, same style as the other test_*.py here.

What these protect:
  - reasoning.effort=high is sent on the TG follow-up call's primary request
    (deepseek-v4-flash's thinking.budget_tokens turned out to be a soft hint,
    not an enforced ceiling — 1/3 real reps burned through max_tokens on
    hidden reasoning even with it set, issue #60) and, critically, is NOT
    enabled on tg_gap_detect's 60-token budget — that combination is what
    crashed production in issue #53.
  - `content: null` (the shape of that crash) degrades to a user-visible
    message instead of raising AttributeError on .strip() and killing the
    polling loop — including when finish_reason=="length" and a non-empty
    reasoning_content holds a partial chain of thought: that must never be
    promoted and returned as if it were the answer (PR #56's original fix
    for this, still enforced here after the model swap).
  - The fallback goes to the configured model with OpenRouter's reasoning
    effort param attached, and the reported model label follows it.

Run: ~/Daily_Intelligence/.venv/bin/python scripts/test_telegram_followup_reason.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the config loader at an empty temp dir so these tests exercise the
# built-in defaults regardless of whether a real llm_config.json exists.
_TMP = Path(tempfile.mkdtemp(prefix="tg_followup_test_"))
os.environ["DAILY_INTEL_LLM_CONFIG"] = str(_TMP / "llm_config.json")
os.environ.setdefault("FINANCE_TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("FINANCE_TELEGRAM_CHAT_ID", "0")

import llm_config  # noqa: E402
import telegram_commands as tc  # noqa: E402

MESSAGES = [{"role": "user", "content": "why is INTC up"}]


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _ok_payload(content="持仓含义分析...", finish="stop", **extra):
    msg = {"content": content}
    msg.update(extra.pop("message_extra", {}))
    return {
        "provider": "TestProvider",
        "usage": {"prompt_tokens": 1200, "completion_tokens": 400,
                  "completion_tokens_details": {"reasoning_tokens": 900}},
        "choices": [{"finish_reason": finish, "message": msg}],
        **extra,
    }


def _run_with(fake_post):
    orig = tc.httpx.post
    tc.httpx.post = fake_post
    try:
        return tc._followup_reason(MESSAGES)
    finally:
        tc.httpx.post = orig


def test_primary_payload_has_reasoning_enabled():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        captured["_timeout"] = timeout
        return _FakeResponse(_ok_payload())

    answer, label = _run_with(fake_post)
    assert answer == "持仓含义分析..."
    assert label == "openai/gpt-5.6-luna"
    assert "thinking" not in captured           # DeepSeek-only param, not sent for this model
    assert captured["reasoning"] == {"effort": "high"}
    assert captured["max_tokens"] == 16000, captured["max_tokens"]
    assert captured["provider"] == {"order": ["OpenAI"], "allow_fallbacks": False}
    # Reasoning latency: a 120s timeout would push most calls to the fallback.
    assert captured["_timeout"] == 180


def test_gap_detect_stage_never_gets_thinking():
    # The old code shared one REASONING_MODEL constant between this 60-token
    # probe and the Step 4 call. Enabling thinking on a shared constant would
    # have reproduced issue #53 here; separate stages are what prevent it.
    gap = llm_config.stage("tg_gap_detect")
    assert gap.get("thinking") is None
    assert gap["max_tokens"] == 60
    assert "thinking" not in llm_config.stage("tg_preprocess")


def test_null_content_with_length_finish_is_reported_not_crashed():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(_ok_payload(content=None, finish="length"))

    try:
        _run_with(fake_post)
    except tc._FollowupError as e:
        assert "token 预算" in str(e), str(e)
        assert "max_tokens" in str(e)
    else:
        raise AssertionError("null content + finish_reason=length must raise _FollowupError")


def test_null_content_without_length_finish_reports_empty_answer():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(_ok_payload(content=None, finish="stop"))

    try:
        _run_with(fake_post)
    except tc._FollowupError as e:
        assert "空回答" in str(e), str(e)
    else:
        raise AssertionError("empty answer must raise _FollowupError")


def test_reasoning_content_used_when_content_missing():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(_ok_payload(
            content=None, message_extra={"reasoning_content": "只有思考内容"}))

    answer, _ = _run_with(fake_post)
    assert answer == "只有思考内容"


def test_length_finish_never_promotes_partial_chain_of_thought():
    # PR #56 review bug: with thinking enabled, the realistic budget-exhaustion
    # shape is content=null/"" + finish_reason=length + a non-empty
    # reasoning_content holding the partial CoT. An earlier version of this
    # code did `content or reasoning_content`, which made that CoT text
    # truthy and returned it as the "answer" — the user got raw chain-of-
    # thought, and the budget-exhausted error path never ran. Must raise the
    # budget message instead, on every attempt (primary and fallback both
    # return this same shape here, so the final error must still be the
    # budget message, not a differently-worded generic one).
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(_ok_payload(
            content=None, finish="length",
            message_extra={"reasoning_content": "...partial chain of thought, not an answer..."}))

    try:
        _run_with(fake_post)
    except tc._FollowupError as e:
        assert "思考" not in str(e)
        assert "token 预算" in str(e), str(e)
    else:
        raise AssertionError("must not return partial CoT as if it were the answer")


def test_empty_primary_content_falls_back_before_giving_up():
    # PR #56 review bug: primary returning HTTP 200 with empty/budget-
    # exhausted content used to raise immediately without trying the
    # configured fallback — the same failure mode a transport error triggers
    # fallback for, but this path skipped it entirely.
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        if json["model"] == "openai/gpt-5.6-luna":
            return _FakeResponse(_ok_payload(content=None, finish="length"))
        return _FakeResponse(_ok_payload(content="fallback 给出的正常回答"))

    answer, label = _run_with(fake_post)
    assert answer == "fallback 给出的正常回答"
    assert label == "x-ai/grok-4.5"
    assert calls == ["openai/gpt-5.6-luna", "x-ai/grok-4.5"]  # no wasted retries


def test_fallback_uses_configured_model_with_reasoning_effort():
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json["model"] == "openai/gpt-5.6-luna":
            raise tc.httpx.ConnectError("primary down")
        return _FakeResponse(_ok_payload(content="grok 的回答"))

    answer, label = _run_with(fake_post)
    assert answer == "grok 的回答"
    assert label == "x-ai/grok-4.5", label
    fb = calls[-1]
    assert fb["model"] == "x-ai/grok-4.5"
    assert fb["reasoning"] == {"effort": "medium"}
    assert fb["max_tokens"] == 8000          # fallback_max_tokens, not the primary's 16000
    assert "thinking" not in fb              # thinking is a DeepSeek-side param
    assert "provider" not in fb              # no provider pin on the fallback
    assert len(calls) == 4                   # 3 primary attempts, then fallback


def test_total_failure_raises_user_facing_error():
    def fake_post(url, headers=None, json=None, timeout=None):
        raise tc.httpx.ConnectError("everything is down")

    try:
        _run_with(fake_post)
    except tc._FollowupError as e:
        assert "LLM 调用失败" in str(e)
    else:
        raise AssertionError("total failure must raise _FollowupError")


def test_config_override_changes_the_call():
    cfg_path = Path(os.environ["DAILY_INTEL_LLM_CONFIG"])
    cfg_path.write_text(
        '{"stages": {"tg_followup": {"model": "x-ai/grok-4.5", '
        '"thinking": null, "max_tokens": 4000}}}', encoding="utf-8")
    llm_config.reload()
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _FakeResponse(_ok_payload())

    try:
        _, label = _run_with(fake_post)
        assert label == "x-ai/grok-4.5"
        assert captured["model"] == "x-ai/grok-4.5"
        assert "thinking" not in captured
        assert captured["max_tokens"] == 4000
    finally:
        cfg_path.unlink()
        llm_config.reload()


def test_preprocess_defaults_to_gemma_not_deepseek():
    # Live-verified 2026-07-25 on this stage's actual prompt: deepseek-v4-flash
    # at temperature=0 gave 3 different broken outcomes across 3 reps of the
    # identical simple command ("删关键词 US-Iran blockade") — a hallucinated
    # action value outside the enum, full truncation (content=""), and a
    # mid-JSON truncation landing on "unknown". gemma-4-31b-it reproduced
    # correct, schema-compliant output on every rep across 5 command types.
    assert llm_config.model("tg_preprocess") == "google/gemma-4-31b-it"


def test_preprocess_handles_markdown_fenced_json():
    # gemma wraps its output in ```json fences even when told not to
    # (observed on every real call) — parse_llm_json must strip them; this
    # guards against a future regression silently breaking every command.
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "OpenInference",
            "usage": {"prompt_tokens": 300, "completion_tokens": 30},
            "choices": [{"finish_reason": "stop", "message": {
                "content": '```json\n{"action": "add_ticker", "section": "个股与基金", "item": "MSFT"}\n```'
            }}],
        })

    orig = tc.httpx.post
    tc.httpx.post = fake_post
    try:
        result = tc._unified_preprocess("加 MSFT")
    finally:
        tc.httpx.post = orig
    assert result == {"action": "add_ticker", "section": "个股与基金", "item": "MSFT"}


def test_gap_detect_defaults_to_gemma_not_deepseek():
    # Live-verified 2026-07-25: deepseek-v4-flash burns the whole 60-token
    # budget on hidden reasoning on this exact prompt shape (finish_reason=
    # length, reasoning_tokens=60, content=None) even with no thinking key
    # sent — issue #53's failure mode, previously masked here because this
    # function's own except swallows it silently and returns None (treated
    # as "no gap"), meaning gap detection never actually worked, only
    # appeared to fail open cleanly.
    assert llm_config.model("tg_gap_detect") == "google/gemma-4-31b-it"


def test_gap_detect_null_content_is_logged_not_raised():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "DeepSeek",
            "usage": {"prompt_tokens": 200, "completion_tokens": 60,
                      "completion_tokens_details": {"reasoning_tokens": 60}},
            "choices": [{"finish_reason": "length", "message": {"content": None}}],
        })

    orig = tc.httpx.post
    tc.httpx.post = fake_post
    try:
        result = tc._detect_research_gap("why is INTC up", ["INTC catalyst"], "some brief")
    finally:
        tc.httpx.post = orig
    assert result is None  # fails open; the point is it doesn't raise


def test_gap_detect_returns_query_on_gap_found():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "OpenInference",
            "usage": {"prompt_tokens": 200, "completion_tokens": 12},
            "choices": [{"finish_reason": "stop",
                        "message": {"content": '"INTC foundry customer confirmation"'}}],
        })

    orig = tc.httpx.post
    tc.httpx.post = fake_post
    try:
        result = tc._detect_research_gap("why is INTC up", ["INTC catalyst"], "some brief")
    finally:
        tc.httpx.post = orig
    assert result == "INTC foundry customer confirmation"


def test_gap_detect_null_returns_none():
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "OpenInference",
            "usage": {"prompt_tokens": 200, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {"content": "null"}}],
        })

    orig = tc.httpx.post
    tc.httpx.post = fake_post
    try:
        result = tc._detect_research_gap("why is INTC up", ["INTC catalyst"], "some brief")
    finally:
        tc.httpx.post = orig
    assert result is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
