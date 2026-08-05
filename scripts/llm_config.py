"""
Daily Intelligence — per-stage LLM selection config (issue #11)
=============================================================
Every LLM call site in this project used to hardcode its model/provider as a
module constant, spread across llm_client.py, run_finance.py, intel_sources.py
and telegram_commands.py. Changing a model therefore meant a code change ->
PR -> review -> merge, which is disproportionate for what is really a tuning
knob (see issue #11).

This module centralises those choices into named *stages* whose values can be
overridden at runtime by an optional JSON file (llm_config.json, tracked in
git — see the note below on why) without touching code. The in-code DEFAULTS
below remain the source of truth: a missing, unreadable, malformed or
partially-invalid config file degrades to the defaults rather than breaking
the pipeline, because these call sites sit in the AM/PM report path that runs
unattended twice a trading day.

Why llm_config.json is git-tracked, not gitignored: it was gitignored in an
earlier version of this PR, copying the pattern used for tg_offset.json and
the budget-tracker counters — but those are machine-written ephemeral state
(losing one just resets a counter to 0), and this is a hand-edited,
deliberate configuration decision (which model, which fallback), the same
category as watchlist.md, not tg_offset.json. Unlike watchlist.md it holds no
personal/sensitive data, so there's no reason to keep it out of git. Tracking
it costs nothing — "edit it and it takes effect without a PR" is a property
of the loader re-reading the file at runtime, not of whether git happens to
track it — and it buys a real audit trail (`git log llm_config.json`) plus
protection against silently losing a deliberate choice back to DEFAULTS if
the file is ever deleted.

Design notes
------------
- Fail-safe, per field: a bad `model` in one stage falls back to that stage's
  default model and logs a warning; it does not invalidate the whole file or
  the other stages.
- Every effective override is logged at INFO on load ("stage.field: A -> B").
  This is still worth having even though the file is git-tracked: git log
  tells you *what* changed and *when it was committed*, not whether a given
  process actually picked it up on a given run (a stale in-memory cache, or
  an edit made after the process last read the file, wouldn't show up any
  other way).
- Stage schemas are closed: a stage only accepts the keys present in its own
  DEFAULTS entry. Unknown stages and unknown keys are warned about and ignored,
  which catches typos (`"modle"`) that would otherwise silently do nothing.
- Reads are mtime+size cached, so the long-running Telegram bot picks up an
  edited config on its next call without a restart, while the per-run report
  scripts pay a single stat() per process.

Why gap detection and followup reasoning are separate stages
------------------------------------------------------------
telegram_commands.py used one REASONING_MODEL constant for both the Step 4
deep-reasoning call (max_tokens=8000) and _detect_research_gap()'s throwaway
yes/no judgement (max_tokens=60). Enabling thinking on a shared constant would
have reproduced the issue #53 production crash on the second one: reasoning
tokens eat the completion budget, `content` comes back null. They are distinct
stages here precisely so per-stage knobs cannot leak across budgets.
"""
import copy
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJ_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("DAILY_INTEL_LLM_CONFIG", str(_PROJ_DIR / "llm_config.json")))

# Gateways this codebase knows how to talk to. Kept as a set so a config that
# names an unimplemented gateway is rejected at load rather than producing a
# confusing HTTP error at call time.
KNOWN_GATEWAYS = {"openrouter"}

# google/gemma-4-31b-it routing on OpenRouter (issue #60). Real calls across
# this project's gemma stages have been observed landing on several different
# providers (Friendli, Crusoe, Novita, OpenInference, OpenInference-bf16...)
# with no explicit pin — OpenInference standardized on as the preferred
# provider for consistency, allow_fallbacks=true so a real outage still
# degrades to another provider rather than failing the call outright.
_GEMMA_PROVIDERS = {"order": ["OpenInference"], "allow_fallbacks": True}

DEFAULTS: dict[str, dict] = {
    # ── Report pipeline (run_finance.py / calibration.py via llm_client.py) ──
    "report_pass1": {
        # Not deepseek-v4-flash (issue #59): the comment this replaced claimed
        # "Flash defaults to no-thinking when the key is absent entirely" —
        # false. Two real production failures on 2026-08-03 (AM report: 2
        # back-to-back finish_reason=length before a third attempt scraped
        # by; PM calibration reuse of this same stage: 3/3 failed, rescued
        # only by fallback_model) showed it burns the full max_tokens budget
        # on hidden reasoning with no thinking key sent at all — the same
        # failure shape issue #53 fixed for tg_preprocess/tg_gap_detect/
        # semantic_filter, just never applied here. Verified live
        # (2026-08-03/04): google/gemma-4-31b-it reproduced
        # reasoning_tokens=0, finish_reason=stop across 6/6 real calls on
        # both this stage's actual prompt shape and am_calibration's, using
        # today's real price/news/signals data, while deepseek-v4-flash
        # failed 4/4 on the identical reconstructed prompts (confirming the
        # test faithfully reproduced the production failure). Output quality
        # checked, not just structural JSON validity: correct 4-section
        # report_md, correct [!]-ticker identification, schema-compliant
        # 可验证信号/tavily_queries. providers is _GEMMA_PROVIDERS (not the
        # DeepSeek DigitalOcean/Venice pin) because gemma isn't routed
        # through those.
        "gateway": "openrouter",
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        "max_tokens": 4000,
        "temperature": 0.2,
        "fallback_model": "google/gemini-3.1-flash-lite",  # OR service_tier=flex
    },
    # AM-prediction calibration (calibration.py's _evaluate_am_predictions,
    # PM slot only). Split out from report_pass1 (issue #59) rather than
    # reusing that stage the way the old code did — same reasoning as "Why
    # gap detection and followup reasoning are separate stages" above: a
    # future tuning change to report_pass1's budget/model for the main
    # report would otherwise silently also change this unrelated judgement
    # call's cost/behavior. Defaults currently mirror report_pass1's because
    # both were validated together against the same real 2026-08-03 PM
    # incident data (see report_pass1's comment) — gemma-4-31b-it reproduced
    # reasoning_tokens=0, finish_reason=stop, and correctly-reasoned
    # verdicts/knowledge_entry output on the real signals-vs-actuals prompt
    # that failed 3/3 in production that day.
    "am_calibration": {
        "gateway": "openrouter",
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        "max_tokens": 4000,
        "temperature": 0.2,
        "fallback_model": "google/gemini-3.1-flash-lite",
    },
    "report_pass2": {
        # openai/gpt-5.6-luna (non-pro) + reasoning.effort=high, not
        # deepseek-v4-pro+thinking (issue #60): a real side-by-side
        # comparison on 2026-08-03 PM's actual data (real price table, real
        # holdings, real Sonar/Tavily context) surfaced a genuine correctness
        # bug in deepseek-v4-pro's output, not just a formatting/stability
        # difference — it categorized the same ORCL/CACI fact as "生态位验证"
        # in the sas_candidates field but then treated it as "认知提升-战略
        # 节点解锁" in the 持仓异动核对 section of the same response, giving a
        # concrete buy recommendation the prompt's own rules explicitly say
        # that category cannot justify. gpt-5.6-luna's output on the
        # identical prompt stayed internally consistent and correctly
        # declined to treat it as sufficient (see issue #60 comments for the
        # full excerpt). Also independently noted: deepseek-v4-pro
        # mischaracterized SPCX as an ETF in that same run, gpt-5.6-luna did
        # not. max_tokens 8000->16000 and provider pinned to OpenAI only,
        # matching the same treatment already applied to tg_followup.
        "gateway": "openrouter",
        "model": "openai/gpt-5.6-luna",
        "providers": {"order": ["OpenAI"], "allow_fallbacks": False},
        "thinking": None,
        "reasoning": {"effort": "high"},
        "max_tokens": 16000,
        "temperature": 0.2,
        "fallback_model": "google/gemini-3.5-flash",
    },
    "semantic_filter": {
        "gateway": "openrouter",
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        "max_tokens": 200,
        "temperature": 0.0,
        "fallback_model": "google/gemini-3.1-flash-lite",
    },
    "macro_brief": {
        "gateway": "openrouter",
        "model": "perplexity/sonar",
        "providers": {"order": ["Perplexity"], "allow_fallbacks": False},
        "max_tokens": 1500,
        "temperature": 0.1,
    },
    # ── Telegram pipeline (telegram_commands.py) ──
    "tg_preprocess": {
        # Not deepseek-v4-flash: at temperature=0 on this stage's actual
        # prompt, 3 back-to-back real calls for the identical simple command
        # ("删关键词 US-Iran blockade") produced 3 different outcomes — a
        # hallucinated action value outside the enum ("remove_keyword"),
        # complete truncation (finish_reason=length, reasoning=600/600,
        # content=""), and a mid-JSON truncation landing on the "unknown"
        # fallback — none of them the correct "remove_geo". A separate case
        # ("如果高通被制裁我该怎么办") got the schema's "query" field replaced
        # with a rambling Chinese reply instead of an English search string in
        # one of two reps. docs/PITFALLS.md#55 already raised max_tokens once
        # for this exact truncation shape; the fix didn't hold because
        # deepseek-v4-flash's implicit reasoning volume on this prompt is
        # itself unstable, not because the budget was too tight one time.
        # google/gemma-4-31b-it reproduced correct, schema-compliant,
        # zero-reasoning-token output on every rep across 5 distinct command
        # types (2026-07-25) — consistent with its 210/210 result across the
        # full no-reasoning eval suite (Obsidian "LLM-No-Reasoning-eval设计与
        # 实现" §15). One observed trade-off: on a cross-ticker risk question,
        # gemma's relevant_tickers stayed narrower (QCOM only) where deepseek
        # sometimes (not consistently — see above) pulled in correlated
        # holdings (QCOM, INTC, NVDA). Judged an acceptable trade against
        # correctly executing basic add/remove commands, which is this
        # stage's primary job.
        "gateway": "openrouter",
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        # 600 is deliberate headroom over the JSON schema this stage emits;
        # it was raised once already after truncation (docs/PITFALLS.md#55).
        # With gemma's reasoning_tokens=0 there is no longer a variable
        # consuming that headroom before the JSON itself.
        "max_tokens": 600,
        "temperature": 0.0,
        "fallback_model": "google/gemini-3.1-flash-lite",
    },
    "tg_gap_detect": {
        "gateway": "openrouter",
        # Not deepseek-v4-flash: verified live (2026-07-25) that it burns the
        # entire 60-token budget on hidden reasoning on this exact prompt shape
        # even with no thinking key sent — finish_reason=length,
        # reasoning_tokens=60, content=None. That is issue #53's failure mode,
        # here caught by this function's own try/except so it fails open
        # silently instead of crashing the bot, but the practical effect was
        # that gap detection never actually ran; every call returned None.
        # google/gemma-4-31b-it (already validated for the structurally
        # identical semantic_filter judgement call, PR #54) reproduces clean
        # on this prompt: finish_reason=stop, reasoning_tokens=0.
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        "max_tokens": 60,
        "temperature": 0.1,
        "fallback_model": None,
    },
    "tg_research": {
        "gateway": "openrouter",
        "model": "perplexity/sonar",
        "max_tokens": 1500,
        "temperature": 0.1,
    },
    "tg_followup": {
        # Not deepseek-v4-flash+thinking (issue #60): budget_tokens=3000 is a
        # soft hint, not an enforced ceiling — a real reconstructed-prompt
        # test (2026-08-04, real 07-29 INTC/PLTR/SPCX followup) reproduced
        # 1/3 reps burning straight through max_tokens=12000 on hidden
        # reasoning (reasoning=11999/12000, content_len=0), the same failure
        # shape issue #53/#59 fixed elsewhere. Low production risk (0 real
        # failures observed, fallback_model absorbs it), but the same fix
        # already validated for report_pass1/am_calibration doesn't carry
        # over here: this stage needs genuine reasoning depth for open-ended
        # portfolio analysis (issue #11 showed gemma-4-31b-it's no-reasoning
        # output is materially worse here), so the fix is a model swap, not
        # dropping reasoning. openai/gpt-5.6-luna (non-pro) + reasoning.effort
        # =high verified live on the real 07-29 prompt: 7/7 finish_reason=
        # stop, reasoning 34-95% of a 12000 budget (one rep at 94.7%, tight)
        # and 59-66% of 16000 — max_tokens raised to 16000 alongside the swap
        # for headroom. Content quality checked, not just structural success:
        # concrete actionable priority ordering, and one rep caught a real
        # data-consistency edge case (portfolio snapshot timestamped after
        # the analysis point) unprompted. provider pinned to OpenAI only,
        # matching the same model's validated config in the sister project
        # (Obsidian "LLM-Reasoning-eval设计与实现", clip_processor.py Stage 2)
        # — avoids routing to a provider that might not honor `reasoning`.
        "gateway": "openrouter",
        "model": "openai/gpt-5.6-luna",
        "providers": {"order": ["OpenAI"], "allow_fallbacks": False},
        "thinking": None,
        "reasoning": {"effort": "high"},
        "max_tokens": 16000,
        "temperature": 0.3,
        "fallback_model": "x-ai/grok-4.5",
        # OpenRouter's unified reasoning param, applied to the fallback model
        # only ("medium" is an effort level, not a separate model slug).
        "fallback_reasoning": {"effort": "medium"},
        "fallback_max_tokens": 8000,
    },
    # SAS candidate-evidence extraction (issue #32's sas_candidates, issue
    # #60): split out of report_pass2 rather than sharing its JSON envelope,
    # so report_pass2's report_md — the largest payload of any stage in this
    # project and one of the two that has actually hit finish_reason=length
    # in production (issue #59) — can drop the JSON wrapper entirely. A
    # truncation mid-string inside a JSON-wrapped report_md loses the whole
    # report; the same truncation in plain markdown only loses the tail.
    # google/gemma-4-31b-it verified live (2026-08-04) on this stage's real
    # extraction task: 9/9 across 3 real adversarial batches built from real
    # portfolio + real archived SAS候选证据日志.md ground truth — exact
    # reproduction of the one real candidate logged for 2026-08-03 PM,
    # correct rejection of a non-held ticker's dramatic earnings beat, and
    # correct precision on two deliberately hard traps (generic earnings
    # beat vs a specific previously-doubted target actually confirmed;
    # genuine open-market insider buying vs a scheduled 10b5-1 sale
    # disguised as "an insider transaction"). All 9 reasoning_tokens=0,
    # finish_reason=stop, across 4 different OR providers.
    "sas_candidate_extract": {
        "gateway": "openrouter",
        "model": "google/gemma-4-31b-it",
        "providers": _GEMMA_PROVIDERS,
        "max_tokens": 800,
        "temperature": 0.0,
        "fallback_model": "google/gemini-3.1-flash-lite",
    },
}

# ── Field validators ─────────────────────────────────────────────────────────
# Each returns (ok, value). `value` is only meaningful when ok is True.


def _v_gateway(v):
    return (isinstance(v, str) and v.lower() in KNOWN_GATEWAYS), (v.lower() if isinstance(v, str) else v)


def _v_model(v):
    # Every OpenRouter slug is "vendor/model", optionally prefixed with "~"
    # for an always-latest alias. Rejecting slugs without "/" catches the most
    # likely edit mistake (a bare model name, or a display label).
    return (isinstance(v, str) and bool(v.strip()) and "/" in v), (v.strip() if isinstance(v, str) else v)


def _v_model_or_none(v):
    if v is None:
        return True, None
    return _v_model(v)


def _v_providers(v):
    if v is None:
        return True, None
    if not isinstance(v, dict):
        return False, v
    order = v.get("order")
    if not isinstance(order, list) or not order or not all(isinstance(x, str) and x.strip() for x in order):
        return False, v
    allow = v.get("allow_fallbacks", True)
    if not isinstance(allow, bool):
        return False, v
    out = {"order": [x.strip() for x in order], "allow_fallbacks": allow}
    # Pass through any other OpenRouter provider-routing keys verbatim
    # (data_collection, ignore, only, quantizations, sort, ...) instead of
    # silently dropping them. Without this, a hand-edited privacy pin like
    # {"data_collection": "deny"} would vanish while the load still logs
    # "override applied" for order/allow_fallbacks — easy to misread as
    # "the edit took effect" (PR #56 review).
    for k, val in v.items():
        if k not in ("order", "allow_fallbacks"):
            out[k] = val
    return True, out


def _v_thinking(v):
    if v is None:
        return True, None
    if not isinstance(v, dict) or v.get("type") not in ("enabled", "disabled"):
        return False, v
    out = {"type": v["type"]}
    if "budget_tokens" in v:
        budget = v["budget_tokens"]
        if not isinstance(budget, int) or isinstance(budget, bool) or not (0 < budget <= 100_000):
            return False, v
        out["budget_tokens"] = budget
    return True, out


def _v_reasoning(v):
    if v is None:
        return True, None
    if not isinstance(v, dict) or v.get("effort") not in ("low", "medium", "high"):
        return False, v
    return True, {"effort": v["effort"]}


def _v_max_tokens(v):
    return (isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 200_000), v


def _v_temperature(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 2.0), (
        float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    )


_VALIDATORS = {
    "gateway": _v_gateway,
    "model": _v_model,
    "fallback_model": _v_model_or_none,
    "providers": _v_providers,
    "thinking": _v_thinking,
    "reasoning": _v_reasoning,
    "fallback_reasoning": _v_reasoning,
    "max_tokens": _v_max_tokens,
    "fallback_max_tokens": _v_max_tokens,
    "temperature": _v_temperature,
}

# thinking.budget_tokens and max_tokens are independent fields that each
# pass field-level validation individually, but a hand edit that sets one
# without the other (e.g. "max_tokens": 500 against the default
# budget_tokens: 3000) recreates issue #53's starvation for any stage with
# thinking enabled, with no WARNING from _VALIDATORS since neither field is
# individually out of range. This is the cross-field check field-level
# validation can't express (PR #56 review).
_MIN_THINKING_HEADROOM = 500


def _enforce_thinking_budget(stage_name: str, cfg: dict) -> None:
    thinking = cfg.get("thinking")
    if not thinking or thinking.get("type") != "enabled":
        return
    budget = thinking.get("budget_tokens")
    if budget is None or cfg["max_tokens"] >= budget + _MIN_THINKING_HEADROOM:
        return
    default = DEFAULTS[stage_name]
    logger.warning(
        f"LLM config: stage '{stage_name}' has thinking.budget_tokens={budget} "
        f"leaving < {_MIN_THINKING_HEADROOM} tokens of headroom under "
        f"max_tokens={cfg['max_tokens']} — reverting both fields to defaults "
        f"(max_tokens={default['max_tokens']}, thinking={default.get('thinking')!r}) "
        f"to avoid issue #53-style budget exhaustion")
    cfg["max_tokens"] = default["max_tokens"]
    cfg["thinking"] = copy.deepcopy(default.get("thinking"))


_UNSET = object()
_cache: dict = {"key": _UNSET, "stages": None}
_lock = threading.Lock()


def _read_raw() -> dict:
    """Return the parsed config file, or {} when absent/unusable (never raises)."""
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.error(f"LLM config unreadable at {CONFIG_PATH} ({e}) — using built-in defaults")
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        # ERROR, not warning: the user edited this file expecting it to take
        # effect, and it silently did not.
        logger.error(f"LLM config is not valid JSON ({CONFIG_PATH}: {e}) — using built-in defaults")
        return {}
    if not isinstance(raw, dict):
        logger.error(f"LLM config must be a JSON object, got {type(raw).__name__} — using built-in defaults")
        return {}
    # Allow an optional "stages" wrapper so the file can carry sibling metadata
    # (comments, _note fields) without them being mistaken for stage names.
    stages = raw.get("stages", raw)
    if not isinstance(stages, dict):
        logger.error("LLM config 'stages' must be a JSON object — using built-in defaults")
        return {}
    return stages


def _build() -> dict[str, dict]:
    """Merge validated overrides onto DEFAULTS, logging every effective change."""
    # deepcopy, not dict(): several stages' "providers" default points at the
    # same shared _GEMMA_PROVIDERS object literal. A shallow copy here still
    # shares that nested dict across every stage that hasn't overridden it —
    # fine for the read-only call sites that exist today, but a future
    # in-place mutation (or a careless test) would poison DEFAULTS for the
    # life of the long-running Telegram bot process (PR #56 review nit).
    merged = {name: copy.deepcopy(cfg) for name, cfg in DEFAULTS.items()}
    raw = _read_raw()
    for stage_name, override in raw.items():
        if stage_name.startswith("_"):
            continue  # convention for comment keys
        if stage_name not in merged:
            logger.warning(f"LLM config: unknown stage '{stage_name}' ignored "
                           f"(known: {', '.join(sorted(merged))})")
            continue
        if not isinstance(override, dict):
            logger.warning(f"LLM config: stage '{stage_name}' must be an object, "
                           f"got {type(override).__name__} — using defaults for it")
            continue
        target = merged[stage_name]
        for field, value in override.items():
            if field.startswith("_"):
                continue
            if field not in target:
                logger.warning(f"LLM config: stage '{stage_name}' has unknown field "
                               f"'{field}' ignored (known: {', '.join(sorted(target))})")
                continue
            ok, cleaned = _VALIDATORS[field](value)
            if not ok:
                logger.warning(f"LLM config: stage '{stage_name}.{field}' value {value!r} is invalid "
                               f"— keeping default {target[field]!r}")
                continue
            if cleaned != target[field]:
                logger.info(f"LLM config override: {stage_name}.{field}: "
                            f"{target[field]!r} -> {cleaned!r}")
                target[field] = cleaned

    for stage_name, cfg in merged.items():
        _enforce_thinking_budget(stage_name, cfg)
    return merged


def _stages() -> dict[str, dict]:
    try:
        st = CONFIG_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _lock:
        if _cache["key"] is not _UNSET and _cache["key"] == key and _cache["stages"] is not None:
            return _cache["stages"]
        stages = _build()
        _cache["key"] = key
        _cache["stages"] = stages
        return stages


def reload() -> None:
    """Drop the cache so the next lookup re-reads the file. For tests."""
    with _lock:
        _cache["key"] = _UNSET
        _cache["stages"] = None


def stage(name: str) -> dict:
    """Return a copy of the effective config for `name`.

    Unknown names raise KeyError: stage names are written by this codebase, not
    by the user, so a miss is a programming error and must not silently degrade
    to some arbitrary model.
    """
    stages = _stages()
    if name not in stages:
        raise KeyError(f"unknown LLM stage '{name}' (known: {', '.join(sorted(stages))})")
    return copy.deepcopy(stages[name])


def model(name: str) -> str:
    """Effective primary model slug for a stage."""
    return stage(name)["model"]


def describe(name: str) -> str:
    """Short human-readable summary for status messages, e.g.
    'deepseek/deepseek-v4-flash (thinking) -> x-ai/grok-4.5'."""
    cfg = stage(name)
    out = cfg["model"]
    thinking = cfg.get("thinking")
    if thinking and thinking.get("type") == "enabled":
        out += " (thinking)"
    elif cfg.get("reasoning"):
        out += f" (reasoning:{cfg['reasoning']['effort']})"
    fb = cfg.get("fallback_model")
    if fb:
        out += f" -> {fb}"
    return out
