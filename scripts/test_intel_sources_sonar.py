"""
Regression tests for _sonar_macro_brief() (intel_sources.py), issue #55.

No pytest dependency — httpx.post is monkeypatched with a fake response object,
same pattern as test_run_finance_semantic_filter.py. Covers the observability
gap flagged in issue #55: a successful response with finish_reason="length"
(truncated by max_tokens) previously produced only a generic "N chars" info
log with no way to tell it had been cut off. Now it must also log usage/
finish_reason and emit an explicit truncation warning.

Run: python scripts/test_intel_sources_sonar.py
"""
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intel_sources as isrc

NOW_ET = datetime(2026, 7, 23, 8, 0, tzinfo=ZoneInfo("America/New_York"))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _patch_key():
    isrc.OPENROUTER_API_KEY = "fake-key-for-test"


def _with_capture(fn):
    handler = _CapturingHandler()
    isrc.logger.addHandler(handler)
    isrc.logger.setLevel(logging.DEBUG)
    try:
        result = fn()
    finally:
        isrc.logger.removeHandler(handler)
    return result, handler.records


def test_normal_response_logs_finish_reason_and_returns_brief():
    _patch_key()

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "Perplexity",
            "usage": {"prompt_tokens": 300, "completion_tokens": 250},
            "choices": [{"finish_reason": "stop",
                         "message": {"content": "WTI at $68.58 as of 2026-07-23 06:00 ET."}}],
        })

    orig_post = isrc.httpx.post
    isrc.httpx.post = fake_post
    try:
        (section,), records = _with_capture(lambda: (
            isrc._sonar_macro_brief("am", ["INTC"], [], [], [], NOW_ET),
        ))
    finally:
        isrc.httpx.post = orig_post

    assert "WTI at $68.58" in section
    assert any("finish_reason=stop" in r and "provider=Perplexity" in r for r in records)
    assert not any("truncated" in r for r in records)


def test_truncated_response_emits_explicit_warning():
    _patch_key()

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "Perplexity",
            "usage": {"prompt_tokens": 300, "completion_tokens": 1500},
            "choices": [{"finish_reason": "length",
                         "message": {"content": "Partial content cut off mid-sen"}}],
        })

    orig_post = isrc.httpx.post
    isrc.httpx.post = fake_post
    try:
        (section,), records = _with_capture(lambda: (
            isrc._sonar_macro_brief("pm", ["INTC"], [], [], [], NOW_ET),
        ))
    finally:
        isrc.httpx.post = orig_post

    assert "Partial content cut off mid-sen" in section  # fail-open: still returns what it got
    assert any("finish_reason=length" in r for r in records)
    assert any("truncated by max_tokens" in r for r in records)


def test_max_tokens_bumped_to_1500():
    # issue #55: 800 was too tight (observed finish_reason=length in production).
    _patch_key()
    captured_payload = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_payload.update(json)
        return _FakeResponse({
            "provider": "Perplexity",
            "usage": {"prompt_tokens": 300, "completion_tokens": 100},
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        })

    orig_post = isrc.httpx.post
    isrc.httpx.post = fake_post
    try:
        isrc._sonar_macro_brief("am", ["INTC"], [], [], [], NOW_ET)
    finally:
        isrc.httpx.post = orig_post

    assert captured_payload["max_tokens"] == 1500


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
