"""
Regression tests for _semantic_relevance_filter() (run_finance.py), issue #53 / PR #54.

No pytest dependency — httpx.post is monkeypatched with a fake response object so
these run without any real network/API call. Covers the exact production failure
shape that triggered the model switch (message.content is None because the model
silently spent its whole max_tokens budget on hidden reasoning tokens) plus the
skip-vs-dual-failure meta distinction raised in PR #54 review: an empty dict {}
must only mean "LLM was actually tried twice and both failed", not "never called".

Run: python scripts/test_run_finance_semantic_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_finance as rf

SAMPLE_RESULTS = [
    {"title": "NVDA beats on datacenter demand", "url": "https://a.example/1", "score": 0.9, "content": "..."},
    {"title": "Unrelated celebrity gossip", "url": "https://a.example/2", "score": 0.1, "content": "..."},
]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_key(monkeypatch_value=True):
    rf.OPENROUTER_API_KEY = "fake-key-for-test" if monkeypatch_value else ""


def test_skips_without_calling_llm_when_no_results():
    _patch_key(True)
    filtered, meta = rf._semantic_relevance_filter([], [], [])
    assert filtered == []
    assert meta == {"skipped": "no_results"}


def test_skips_without_calling_llm_when_no_api_key():
    _patch_key(False)
    filtered, meta = rf._semantic_relevance_filter(SAMPLE_RESULTS, [], [])
    assert filtered == SAMPLE_RESULTS[:10]
    assert meta == {"skipped": "no_api_key"}
    _patch_key(True)  # restore for subsequent tests


def test_none_content_falls_through_to_flex_then_fail_open():
    # The exact production shape from the 2026-07-22 incident: primary response has
    # message.content = None (hidden reasoning burned the whole max_tokens budget),
    # and this test's flex mock also returns None content, forcing full fail-open.
    _patch_key(True)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        return _FakeResponse({
            "provider": "TestProvider",
            "usage": {"prompt_tokens": 500, "completion_tokens": 80,
                      "completion_tokens_details": {"reasoning_tokens": 80}},
            "choices": [{"finish_reason": "length", "message": {"content": None}}],
        })

    orig_post = rf.httpx.post
    rf.httpx.post = fake_post
    try:
        filtered, meta = rf._semantic_relevance_filter(SAMPLE_RESULTS, ["NVDA"], [])
    finally:
        rf.httpx.post = orig_post

    assert isinstance(filtered, list)
    assert filtered == SAMPLE_RESULTS[:10]  # fail-open: script-scored order preserved
    assert meta == {}  # real dual-failure, not a skip
    assert len(calls) == 2  # primary attempted, then flex attempted


def test_none_content_primary_but_flex_succeeds():
    _patch_key(True)
    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse({
                "provider": "TestProvider",
                "usage": {"prompt_tokens": 500, "completion_tokens": 80,
                          "completion_tokens_details": {"reasoning_tokens": 80}},
                "choices": [{"finish_reason": "length", "message": {"content": None}}],
            })
        return _FakeResponse({
            "provider": "FlexProvider",
            "usage": {"prompt_tokens": 500, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop", "message": {"content": "[0, 1]"}}],
        })

    orig_post = rf.httpx.post
    rf.httpx.post = fake_post
    try:
        filtered, meta = rf._semantic_relevance_filter(SAMPLE_RESULTS, ["NVDA"], [])
    finally:
        rf.httpx.post = orig_post

    assert [r["url"] for r in filtered] == [SAMPLE_RESULTS[0]["url"], SAMPLE_RESULTS[1]["url"]]
    assert meta == {"provider": "FlexProvider", "fallback": True,
                    "model": rf.llm_config.stage("semantic_filter")["fallback_model"]}


def test_reasoning_content_key_used_when_content_missing():
    # Some providers put the answer under reasoning_content instead of content —
    # already-handled fallback chain (content or reasoning_content or reasoning or "").
    _patch_key(True)

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({
            "provider": "TestProvider",
            "usage": {"prompt_tokens": 500, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop",
                        "message": {"content": None, "reasoning_content": "[1, 0]"}}],
        })

    orig_post = rf.httpx.post
    rf.httpx.post = fake_post
    try:
        filtered, meta = rf._semantic_relevance_filter(SAMPLE_RESULTS, ["NVDA"], [])
    finally:
        rf.httpx.post = orig_post

    assert [r["url"] for r in filtered] == [SAMPLE_RESULTS[1]["url"], SAMPLE_RESULTS[0]["url"]]
    assert meta == {"provider": "TestProvider", "fallback": False}


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
