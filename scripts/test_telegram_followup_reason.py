#!/usr/bin/env python3
"""
Regression tests for telegram_commands._followup_reason() (issue #11).

No pytest dependency — plain asserts, same style as the other test_*.py here.

What these protect:
  - Thinking is enabled on the TG follow-up call (the change this PR makes)
    and, critically, is NOT enabled on tg_gap_detect's 60-token budget — that
    combination is what crashed production in issue #53.
  - `content: null` (the shape of that crash) degrades to a user-visible
    message instead of raising AttributeError on .strip() and killing the
    polling loop.
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


def test_primary_payload_has_thinking_enabled():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        captured["_timeout"] = timeout
        return _FakeResponse(_ok_payload())

    answer, label = _run_with(fake_post)
    assert answer == "持仓含义分析..."
    assert label == "deepseek/deepseek-v4-flash"
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 3000}
    assert captured["max_tokens"] == 12000, captured["max_tokens"]
    assert captured["provider"] == {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}
    # Thinking latency: a 120s timeout would push most calls to the fallback.
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


def test_fallback_uses_configured_model_with_reasoning_effort():
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json["model"] == "deepseek/deepseek-v4-flash":
            raise tc.httpx.ConnectError("primary down")
        return _FakeResponse(_ok_payload(content="grok 的回答"))

    answer, label = _run_with(fake_post)
    assert answer == "grok 的回答"
    assert label == "x-ai/grok-4.5", label
    fb = calls[-1]
    assert fb["model"] == "x-ai/grok-4.5"
    assert fb["reasoning"] == {"effort": "medium"}
    assert fb["max_tokens"] == 8000          # fallback_max_tokens, not the primary's 12000
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
