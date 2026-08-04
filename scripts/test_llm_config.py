#!/usr/bin/env python3
"""
Regression tests for llm_config.py (issue #11).

No pytest dependency — plain asserts, same style as
test_intel_sources_sanitize.py / test_run_finance_semantic_filter.py.

The point of these is the fail-safe contract: this config file is edited by
hand, outside code review, and sits in front of the unattended AM/PM report
path. Every way a hand-edit can go wrong (missing file, truncated JSON, wrong
type, typo'd key, out-of-range number) must degrade to the in-code defaults
rather than break a production run. Run:

    ~/Daily_Intelligence/.venv/bin/python scripts/test_llm_config.py
"""
import importlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = Path(tempfile.mkdtemp(prefix="llm_config_test_"))
CONFIG = _TMP / "llm_config.json"
os.environ["DAILY_INTEL_LLM_CONFIG"] = str(CONFIG)

import llm_config  # noqa: E402  (must follow the env var above)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _load(payload, *, raw_text=None):
    """Write a config file (or delete it when payload is None) and reload.

    Returns captured log records so tests can assert on the audit trail — the
    log is what tells you whether a given process picked up an edit; git
    history (the file is tracked) tells you what changed and when, but not
    whether any particular run actually saw it.
    """
    if payload is None and raw_text is None:
        if CONFIG.exists():
            CONFIG.unlink()
    else:
        CONFIG.write_text(raw_text if raw_text is not None else json.dumps(payload),
                          encoding="utf-8")
    llm_config.reload()
    handler = _CaptureHandler()
    llm_config.logger.addHandler(handler)
    llm_config.logger.setLevel(logging.INFO)
    try:
        llm_config._stages()
    finally:
        llm_config.logger.removeHandler(handler)
    return handler.records


def _levels(records, level):
    return [m for lv, m in records if lv == level]


def test_defaults_when_file_missing():
    _load(None)
    assert llm_config.model("report_pass1") == "google/gemma-4-31b-it"
    assert llm_config.model("am_calibration") == "google/gemma-4-31b-it"
    assert llm_config.model("report_pass2") == "deepseek/deepseek-v4-pro"
    assert llm_config.model("semantic_filter") == "google/gemma-4-31b-it"
    assert llm_config.stage("tg_followup")["fallback_model"] == "x-ai/grok-4.5"


def test_valid_override_applies_and_is_logged():
    records = _load({"stages": {"tg_followup": {"model": "x-ai/grok-4.5"}}})
    assert llm_config.model("tg_followup") == "x-ai/grok-4.5"
    infos = _levels(records, "INFO")
    assert any("tg_followup.model" in m and "grok-4.5" in m for m in infos), infos
    # Untouched fields keep their defaults.
    assert llm_config.stage("tg_followup")["max_tokens"] == 12000


def test_flat_form_without_stages_wrapper():
    _load({"semantic_filter": {"model": "google/gemini-3.1-flash-lite"}})
    assert llm_config.model("semantic_filter") == "google/gemini-3.1-flash-lite"


def test_malformed_json_falls_back_to_defaults():
    records = _load(None, raw_text='{"stages": {"tg_followup": {"model": ')
    assert llm_config.model("tg_followup") == "deepseek/deepseek-v4-flash"
    assert any("not valid JSON" in m for m in _levels(records, "ERROR"))


def test_non_object_root_falls_back():
    records = _load(None, raw_text='["deepseek/deepseek-v4-flash"]')
    assert llm_config.model("tg_followup") == "deepseek/deepseek-v4-flash"
    assert _levels(records, "ERROR")


def test_unknown_stage_and_unknown_field_are_ignored():
    records = _load({"stages": {
        "tg_folowup": {"model": "x-ai/grok-4.5"},          # typo'd stage
        "tg_followup": {"modle": "x-ai/grok-4.5"},         # typo'd field
    }})
    assert llm_config.model("tg_followup") == "deepseek/deepseek-v4-flash"
    warnings = _levels(records, "WARNING")
    assert any("unknown stage" in m for m in warnings), warnings
    assert any("unknown field" in m for m in warnings), warnings


def test_invalid_values_fall_back_per_field_not_per_file():
    records = _load({"stages": {"tg_followup": {
        "model": "grok-4.5",                 # no vendor prefix -> rejected
        "max_tokens": 20000,                 # valid -> applied
    }}})
    cfg = llm_config.stage("tg_followup")
    assert cfg["model"] == "deepseek/deepseek-v4-flash"  # bad field reverted
    assert cfg["max_tokens"] == 20000                    # good field still applied
    assert any("tg_followup.model" in m and "invalid" in m
               for m in _levels(records, "WARNING"))


def test_numeric_and_structural_bounds():
    for field, bad in [
        ("max_tokens", 0),
        ("max_tokens", -1),
        ("max_tokens", 4000.5),
        ("max_tokens", True),          # bool is an int subclass — must not pass
        ("temperature", 5),
        ("temperature", "0.2"),
        ("thinking", {"type": "on"}),
        ("thinking", {"type": "enabled", "budget_tokens": 0}),
        ("thinking", {"type": "enabled", "budget_tokens": "3000"}),
        ("providers", {"order": []}),
        ("providers", {"order": "DigitalOcean"}),
        ("providers", {"order": ["X"], "allow_fallbacks": "yes"}),
        ("fallback_reasoning", {"effort": "extreme"}),
        ("gateway", "anthropic-direct"),
    ]:
        default = llm_config.DEFAULTS["tg_followup"].get(field)
        records = _load({"stages": {"tg_followup": {field: bad}}})
        got = llm_config.stage("tg_followup")[field]
        assert got == default, f"{field}={bad!r} should have been rejected, got {got!r}"
        assert any(f"tg_followup.{field}" in m for m in _levels(records, "WARNING")), \
            f"no warning logged for {field}={bad!r}"


def test_valid_structural_values_accepted():
    _load({"stages": {
        "tg_followup": {
            "thinking": {"type": "enabled", "budget_tokens": 5000},
            "providers": {"order": ["DeepSeek"], "allow_fallbacks": False},
            "fallback_reasoning": {"effort": "high"},
        },
        "report_pass1": {"providers": None, "thinking": None},
    }})
    cfg = llm_config.stage("tg_followup")
    assert cfg["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert cfg["providers"] == {"order": ["DeepSeek"], "allow_fallbacks": False}
    assert cfg["fallback_reasoning"] == {"effort": "high"}
    assert llm_config.stage("report_pass1")["providers"] is None


def test_thinking_budget_without_headroom_reverts_both_fields():
    # max_tokens and thinking.budget_tokens each pass field-level validation
    # independently, but together they recreate issue #53's starvation.
    # Neither field alone is "invalid", so only a cross-field check catches it.
    records = _load({"stages": {"tg_followup": {"max_tokens": 3200}}})
    cfg = llm_config.stage("tg_followup")
    assert cfg["max_tokens"] == llm_config.DEFAULTS["tg_followup"]["max_tokens"]
    assert cfg["thinking"] == llm_config.DEFAULTS["tg_followup"]["thinking"]
    assert any("thinking.budget_tokens" in m and "tg_followup" in m
               for m in _levels(records, "WARNING"))


def test_thinking_budget_with_headroom_is_accepted():
    _load({"stages": {"tg_followup": {"max_tokens": 20000}}})
    assert llm_config.stage("tg_followup")["max_tokens"] == 20000


def test_thinking_budget_check_skipped_when_thinking_disabled():
    # report_pass1 has no thinking config — an aggressively small max_tokens
    # there is a different (legitimate) tuning choice, not a starvation risk.
    records = _load({"stages": {"report_pass1": {"max_tokens": 50}}})
    assert llm_config.stage("report_pass1")["max_tokens"] == 50
    assert not any("thinking.budget_tokens" in m for m in _levels(records, "WARNING"))


def test_provider_pass_through_unknown_keys():
    # Dropping unrecognized OpenRouter provider keys (e.g. a privacy pin like
    # data_collection) while still logging "override applied" for order/
    # allow_fallbacks makes an edit look like it took effect when part of it
    # silently vanished.
    _load({"stages": {"report_pass1": {
        "providers": {"order": ["DeepSeek"], "allow_fallbacks": True,
                      "data_collection": "deny", "quantizations": ["fp16"]}}}})
    providers = llm_config.stage("report_pass1")["providers"]
    assert providers["data_collection"] == "deny"
    assert providers["quantizations"] == ["fp16"]
    assert providers["order"] == ["DeepSeek"]


def test_stage_returns_are_independent_deep_copies():
    # Multiple stages' "providers" default share the same object literal
    # (_DS_PROVIDERS) in DEFAULTS. A shallow copy would let mutating one
    # stage's returned providers dict corrupt every other stage that still
    # points at the same default — persisting for the life of the
    # long-running Telegram bot process.
    # report_pass1 no longer defaults to _DS_PROVIDERS (issue #59 — it's
    # gemma now, providers is null); report_pass2 and tg_followup still do.
    _load(None)
    a = llm_config.stage("report_pass2")
    a["providers"]["order"].append("Mutated")
    b = llm_config.stage("tg_followup")  # also defaults to the shared _DS_PROVIDERS
    assert "Mutated" not in b["providers"]["order"]
    c = llm_config.stage("report_pass2")
    assert "Mutated" not in c["providers"]["order"]


def test_underscore_keys_ignored_without_warning():
    records = _load({"_note": "a comment", "stages": {
        "tg_followup": {"_why": "note", "model": "x-ai/grok-4.5"}}})
    assert llm_config.model("tg_followup") == "x-ai/grok-4.5"
    assert not any("unknown" in m for m in _levels(records, "WARNING"))


def test_mutation_of_returned_dict_does_not_leak():
    _load(None)
    cfg = llm_config.stage("tg_followup")
    cfg["model"] = "mutated/model"
    assert llm_config.model("tg_followup") == "deepseek/deepseek-v4-flash"


def test_unknown_stage_name_raises():
    _load(None)
    try:
        llm_config.stage("no_such_stage")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown stage name must raise, not silently degrade")


def test_edited_file_picked_up_without_restart():
    # The Telegram bot is a long-running daemon; a config-only edit must take
    # effect without restarting it (that is the whole point of issue #11).
    _load({"stages": {"tg_followup": {"model": "x-ai/grok-4.5"}}})
    assert llm_config.model("tg_followup") == "x-ai/grok-4.5"
    CONFIG.write_text(json.dumps(
        {"stages": {"tg_followup": {"model": "deepseek/deepseek-v4-pro"}}}), encoding="utf-8")
    # No reload() call here — mtime/size change alone must invalidate the cache.
    os.utime(CONFIG, (0, 0))  # force a distinct mtime even on a fast filesystem
    assert llm_config.model("tg_followup") == "deepseek/deepseek-v4-pro"


def test_shipped_example_config_is_valid_and_matches_defaults():
    # The example file is the user-facing documentation of the schema; if it
    # drifts from DEFAULTS, copying it becomes an accidental config change.
    example = Path(__file__).resolve().parent.parent / "llm_config.example.json"
    records = _load(None, raw_text=example.read_text(encoding="utf-8"))
    assert not _levels(records, "WARNING"), _levels(records, "WARNING")
    assert not _levels(records, "ERROR"), _levels(records, "ERROR")
    assert not _levels(records, "INFO"), \
        f"example config differs from DEFAULTS: {_levels(records, 'INFO')}"


def test_llm_client_reads_stage_config():
    # llm_client.call_llm() must resolve its model from the stage, not from a
    # constant — the old code inferred pass2 by string-comparing the model.
    import llm_client
    importlib.reload(llm_client)
    _load({"stages": {"report_pass2": {"model": "x-ai/grok-4.5"}}})
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"provider": "T", "usage": {},
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": '{"ok": true}'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Resp()

    orig = llm_client.httpx.post
    llm_client.httpx.post = fake_post
    try:
        out = llm_client.call_llm("p", system_prompt="s", stage="report_pass2")
    finally:
        llm_client.httpx.post = orig
    assert out.get("ok") is True
    assert captured["model"] == "x-ai/grok-4.5"
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 3000}
    assert captured["max_tokens"] == 8000


def test_calibration_uses_its_own_stage_not_report_pass1():
    # issue #59: calibration.py used to hardcode stage="report_pass1" (its
    # justification was "same model/budget as the main report", but that
    # coupling meant a future report_pass1 tuning change would silently also
    # change this unrelated judgement call). Verify it now resolves against
    # its own "am_calibration" stage — overriding report_pass1 alone must not
    # affect it, and overriding am_calibration must.
    import llm_client
    import calibration
    importlib.reload(llm_client)
    importlib.reload(calibration)
    calibration.OPENROUTER_API_KEY = "test-key"
    _load({"stages": {
        "report_pass1": {"model": "x-ai/grok-4.5"},
        "am_calibration": {"model": "google/gemini-3.5-flash"},
    }})
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"provider": "T", "usage": {},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                        "verdicts": [], "knowledge_entry": "", "worth_surfacing": False, "surface_blurb": "",
                    })}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Resp()

    orig = llm_client.httpx.post
    llm_client.httpx.post = fake_post
    try:
        out = calibration._evaluate_am_predictions("- some signal", "price table", "news", "2026-08-04")
    finally:
        llm_client.httpx.post = orig
    assert out.get("worth_surfacing") is False
    assert captured["model"] == "google/gemini-3.5-flash", captured.get("model")


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
