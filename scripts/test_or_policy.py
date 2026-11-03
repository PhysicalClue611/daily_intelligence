#!/usr/bin/env python3
"""
OpenRouter provider policy (owner rule 2026-11-03): every OpenRouter call
carries data_collection=deny, open-weight models route only to 8-bit-or-better
endpoints under a price cap, and no fallback path drops the provider block.

No pytest dependency, same style as test_llm_config.py. Run:

    ~/Daily_Intelligence/.venv/bin/python scripts/test_or_policy.py
"""
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = Path(tempfile.mkdtemp(prefix="or_policy_test_"))
CONFIG = _TMP / "llm_config.json"
os.environ["DAILY_INTEL_LLM_CONFIG"] = str(CONFIG)

import llm_config  # noqa: E402

EIGHT_BIT_PLUS = {"fp8", "bf16", "fp16", "fp32"}
OPEN_WEIGHT_PREFIXES = ("google/gemma",)


def _load(payload):
    if payload is None:
        if CONFIG.exists():
            CONFIG.unlink()
    else:
        CONFIG.write_text(json.dumps(payload), encoding="utf-8")
    llm_config.reload()


def test_enforce_policy_adds_deny_without_provider():
    out = llm_config.enforce_policy({"model": "m", "messages": []})
    assert out["provider"] == {"data_collection": "deny"}, out


def test_enforce_policy_keeps_existing_keys_and_overrides_allow():
    src = {"model": "m", "provider": {"order": ["OpenAI"], "data_collection": "allow"}}
    out = llm_config.enforce_policy(src)
    assert out["provider"] == {"order": ["OpenAI"], "data_collection": "deny"}, out
    assert src["provider"]["data_collection"] == "allow", "input must not be mutated"


def _check_open_weight_stages(stages, where):
    for name, cfg in stages.items():
        if name.startswith("_") or not isinstance(cfg, dict):
            continue
        if not str(cfg.get("model", "")).startswith(OPEN_WEIGHT_PREFIXES):
            continue
        prov = cfg.get("providers") or {}
        q = prov.get("quantizations")
        assert q and set(q) <= EIGHT_BIT_PLUS, f"{where}:{name} quantizations={q}"
        assert "max_price" in prov, f"{where}:{name} has no max_price"
        assert "OpenInference" not in prov.get("order", []), f"{where}:{name} still pins OpenInference"


def test_defaults_open_weight_stages_are_compliant():
    _load(None)
    _check_open_weight_stages({n: llm_config.stage(n) for n in llm_config.DEFAULTS}, "DEFAULTS")


def test_tracked_config_files_are_compliant():
    root = HERE.parent
    for fname in ("llm_config.json", "llm_config.example.json"):
        data = json.loads((root / fname).read_text(encoding="utf-8"))
        _check_open_weight_stages(data["stages"], fname)


def test_every_openrouter_post_goes_through_enforce_policy():
    # Static guard: each httpx.post(OR_BASE_URL, ...) in production scripts
    # must send json=llm_config.enforce_policy(...). A new call site that
    # skips it fails here instead of silently going out without deny.
    offenders = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\bOR_BASE_URL,\s*$", line) and "httpx.post(" in lines[i - 1]:
                window = "\n".join(lines[i:i + 8])
                if "json=llm_config.enforce_policy(" not in window:
                    offenders.append(f"{path.name}:{i + 1}")
    assert not offenders, f"OpenRouter posts without enforce_policy: {offenders}"


def test_call_llm_fallback_sends_deny():
    import httpx
    import llm_client
    importlib.reload(llm_client)
    llm_client.OPENROUTER_API_KEY = "test-key"
    _load(None)
    captured = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"provider": "T", "usage": {},
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append(json)
        if len(captured) == 1:
            raise httpx.ConnectError("down")
        return _Resp()

    orig_post, orig_sleep = llm_client.httpx.post, llm_client.time.sleep
    llm_client.httpx.post = fake_post
    llm_client.time.sleep = lambda s: None
    try:
        llm_client.call_llm("p", "s", max_retries=0, stage="am_calibration")
    finally:
        llm_client.httpx.post, llm_client.time.sleep = orig_post, orig_sleep
    assert len(captured) == 2, len(captured)
    assert captured[1]["model"] == llm_config.stage("am_calibration")["fallback_model"]
    for body in captured:
        assert body["provider"]["data_collection"] == "deny", body.get("provider")


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
