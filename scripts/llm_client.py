"""
Daily Intelligence — LLM call core (OpenRouter/DeepSeek)
=============================================================
call_llm(): retry + OR-flex-fallback wrapper around OpenRouter's chat
completions endpoint. Extracted from run_finance.py (issue #42 follow-up,
per PR #43 review feedback, 2026-07-17) into a true leaf module so
calibration.py's _evaluate_am_predictions() can import it directly instead
of a deferred `from run_finance import call_llm` inside the function body
— that pattern silently assumed run_finance.py is registered in
sys.modules under the name "run_finance", which is false when it's run
directly as the entrypoint (`python run_finance.py`, as launchd does):
Python registers it as `__main__` instead, so the deferred import would
trigger a second, distinct execution of the entire module on first call.

system_prompt has no default (unlike the pre-split version, which defaulted
to run_finance.py's SYSTEM_PROMPT constant) — that constant is a
report-generation-specific prompt template that doesn't belong in a
generic LLM-calling leaf module. All 3 call sites already pass it
explicitly or were updated to.

Model/provider/thinking/token choices are no longer literals here: they come
from llm_config.py stages ("report_pass1"/"report_pass2"), which are runtime
-overridable via llm_config.json (issue #11). This also retires the old
`is_pass2 = (model == LLM_MODEL_PASS2)` inference, which coupled "which
thinking config do I use" to string equality against a constant — that would
have broken the moment pass 2's model was reconfigured to anything else.
"""
import json
import logging
import os
import re
import sys
import time

import httpx

import llm_config

# Cross-repo: canonical LLM-JSON-parsing helper lives in ~/Homepage (host-only
# path). See CLAUDE.md "JSON 解析健壮性" note for why this isn't duplicated
# locally; scripts/sas_review.py uses the same import pattern.
sys.path.insert(0, os.path.join(os.path.expanduser("~"), "Homepage"))
from llm_json_utils import parse_llm_json

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE_URL         = "https://openrouter.ai/api/v1/chat/completions"
OR_ATTRIBUTION_HEADERS = {"HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence", "X-OpenRouter-Title": "DailyIntel"}

def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing ``` fence if present, else return unchanged.

    parse_json=False callers ask the model for raw markdown with no fence and
    no JSON wrapper, but nothing stops a model from adding one anyway (it's
    common training-data behavior) — this is the same unwrapping
    parse_llm_json already does for the JSON path, kept minimal here since
    there's no JSON structure to repair, just a possible fence to peel off."""
    stripped = text.strip()
    m = re.match(r"^```(?:\w+)?\n(.*)\n```$", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _unwrap_legacy_json_report_md(text: str) -> str:
    """Defensive: parse_json=False stages ask for bare markdown, but a model
    with months of "output JSON with a report_md field" habit (report_pass2's
    prior contract, before issue #60) can still regress to that shape.
    Unwrapping only this one exact, narrow shape — a top-level object whose
    only signal is a non-empty report_md string — avoids shipping a raw JSON
    dump as the report if it happens; anything else (parse failure, wrong
    shape) passes through unchanged rather than being force-interpreted."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    if isinstance(obj, dict) and isinstance(obj.get("report_md"), str) and obj["report_md"].strip():
        logger.warning("LLM parse_json=False completion regressed to a JSON wrapper "
                        "— unwrapped report_md field instead of shipping raw JSON")
        return obj["report_md"].strip()
    return text


def _resolve_content(msg: dict, finish_reason: str | None) -> str:
    """Extract the model's visible answer without promoting partial
    chain-of-thought as if it were the answer (PR #62 review; mirrors
    telegram_commands.py's _parse_step4_response, PR #56). finish_reason==
    "length" with empty content is the issue #53/#59/#60 reasoning-exhausted
    -the-budget shape — reasoning_content there holds a partial chain of
    thought, not an answer, and promoting it silently ships garbage (for
    report_pass2, as the daily report itself) with no retry and no fallback,
    since the caller sees a non-empty result and treats it as success. A
    non-length empty content (some providers genuinely put the final answer
    under reasoning_content even without exhausting the budget) is still
    fine to fall back to."""
    content = (msg.get("content") or "").strip()
    if content:
        return content
    if finish_reason == "length":
        return ""
    return (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()


def call_llm(prompt: str, system_prompt: str, max_retries: int = 2,
             stage: str = "report_pass1", parse_json: bool = True) -> dict:
    """Call the configured model for `stage` via OpenRouter, return a result dict.

    Retries on network/5xx/429 errors, then falls back to the stage's
    fallback_model on OpenRouter flex. See llm_config.DEFAULTS for the
    per-stage model, provider routing, thinking budget and token limits.

    When parse_json is True (default), the completion is parsed as JSON
    (repair-on-failure via parse_llm_json) and returned as that dict. When
    False, the raw completion text is returned under result["text"] with no
    JSON parsing attempted — for stages (report_pass2) whose payload is
    free-form markdown that would otherwise have to be escaped into, and
    survive being truncated inside, a JSON string (issue #60): a truncation
    mid-string when JSON-wrapped destroys the whole payload, the same
    truncation in plain text only loses the tail."""
    cfg = llm_config.stage(stage)
    model = cfg["model"]
    thinking_cfg = cfg.get("thinking")
    reasoning_cfg = cfg.get("reasoning")
    max_tokens = cfg["max_tokens"]
    providers = cfg.get("providers")
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"LLM retry {attempt}/{max_retries} after {wait}s...")
                time.sleep(wait)
            resp = httpx.post(
                OR_BASE_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    **OR_ATTRIBUTION_HEADERS,
                },
                json={
                    "model": model,
                    **({"provider": providers} if providers else {}),
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": cfg["temperature"],
                    **({"thinking": thinking_cfg} if thinking_cfg else {}),
                    **({"reasoning": reasoning_cfg} if reasoning_cfg else {}),
                },
                timeout=180,
            )
            resp.raise_for_status()
            content = ""
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                # HTTP body itself is not valid JSON (truncated/error page) — retryable
                last_error = e
                logger.warning(f"LLM attempt {attempt+1}: malformed HTTP JSON body: {e}")
                continue
            usage = data.get("usage", {})
            choice = data["choices"][0]
            logger.info(f"LLM tokens [{stage}/{model}]: prompt={usage.get('prompt_tokens')} "
                        f"completion={usage.get('completion_tokens')} "
                        f"reasoning={usage.get('completion_tokens_details', {}).get('reasoning_tokens')} "
                        f"finish_reason={choice.get('finish_reason')} "
                        f"provider={data.get('provider', 'n/a')}")
            if choice.get("finish_reason") == "length":
                logger.warning(f"LLM [{stage}]: hit max_tokens={max_tokens} (finish_reason=length) — "
                               f"output truncated; raise max_tokens in llm_config.json if this recurs")

            msg = choice["message"]
            content = _resolve_content(msg, choice.get("finish_reason"))
            if parse_json:
                result = parse_llm_json(content, logger=logger)
                if not isinstance(result, dict):
                    # A malformed outer object whose only cleanly-parsing substring is a
                    # nested array (e.g. tavily_queries) makes parse_llm_json return that
                    # array instead of the dict — treat as unparseable JSON, same as a
                    # JSONDecodeError, so it's retried rather than crashing on result[...].
                    raise json.JSONDecodeError(
                        f"parse_llm_json returned {type(result).__name__}, expected dict",
                        content, 0)
            else:
                text = _strip_code_fence(content)
                text = _unwrap_legacy_json_report_md(text)
                if not text:
                    # Empty text is the free-form-payload equivalent of an
                    # unparseable JSON body — retry the same way, rather than
                    # returning an empty report_md as if it were a success.
                    # (Also reached when finish_reason=="length" and content
                    # was empty: _resolve_content refuses to promote partial
                    # reasoning as the answer, so content is "" here too.)
                    raise json.JSONDecodeError("empty completion text", content, 0)
                result = {"text": text}
            result["_llm_meta"] = {
                "model": model,
                "provider": data.get("provider", "n/a"),
                "attempts": attempt + 1,
                "fallback": False,
            }
            return result
        except json.JSONDecodeError as e:
            # Model output wasn't parseable JSON (escaping error or truncation) — retryable
            last_error = e
            logger.warning(f"LLM attempt {attempt+1}: returned invalid JSON: {e}\nContent: {content[:500]}")
            continue
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500 or e.response.status_code == 429:
                last_error = e
                logger.warning(f"LLM attempt {attempt+1}: HTTP {e.response.status_code}")
            else:
                # Deliberate: a non-429 4xx here returns immediately, skipping
                # the OR-flex fallback below (PR #62 review). This is a known,
                # accepted tradeoff for stages with a hard provider pin
                # (allow_fallbacks=False — report_pass2/tg_followup, issue
                # #60): with a soft pin, OpenRouter itself absorbs most
                # "model not available on this provider"-type failures
                # internally before ever surfacing a 4xx to us; with a hard
                # pin, that class of failure surfaces here as a real 4xx
                # instead. For report_pass2 the safety net is Pass 1's
                # already-generated report_md (the AM/PM report still ships,
                # just without the Tavily-integrated Pass 2 pass); for
                # tg_followup the fallback_model already gets a separate,
                # explicit retry in telegram_commands.py's own logic on top
                # of this. Not routing non-429 4xx through flex here as well
                # was an existing behavior for every stage before this PR;
                # broadening it is a larger change to this shared function's
                # semantics than this PR's scope, and is deliberately left
                # for a separate change if it proves to matter in practice.
                logger.error(f"LLM HTTP {e.response.status_code}: {e}")
                return {}
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = e
            logger.warning(f"LLM attempt {attempt+1}: {e}")
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {}

    logger.error(f"LLM exhausted {max_retries+1} attempts, last error: {last_error}")

    # OR flex fallback (gemini via OpenRouter, service_tier=flex)
    fallback_model = cfg.get("fallback_model")
    if not fallback_model:
        logger.error(f"LLM [{stage}]: no fallback_model configured, giving up")
        return {}
    logger.warning(f"OR primary unavailable, trying OR flex fallback: {fallback_model}")
    try:
        resp = httpx.post(
            OR_BASE_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json", **OR_ATTRIBUTION_HEADERS},
            json={
                "model": fallback_model,
                "service_tier": "flex",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": cfg["temperature"],
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        logger.info(f"OR flex tokens: prompt={usage.get('prompt_tokens')} "
                    f"completion={usage.get('completion_tokens')} "
                    f"provider={data.get('provider', 'n/a')}")
        choice = data["choices"][0]
        msg = choice["message"]
        content = _resolve_content(msg, choice.get("finish_reason"))
        if parse_json:
            result = parse_llm_json(content, logger=logger)
            if not isinstance(result, dict):
                raise json.JSONDecodeError(
                    f"parse_llm_json returned {type(result).__name__}, expected dict",
                    content, 0)
        else:
            text = _strip_code_fence(content)
            text = _unwrap_legacy_json_report_md(text)
            if not text:
                raise json.JSONDecodeError("empty completion text", content, 0)
            result = {"text": text}
        logger.info(f"OR flex fallback succeeded: {fallback_model}")
        result["_llm_meta"] = {
            "model": fallback_model,
            "provider": data.get("provider", "n/a"),
            "fallback": True,
            "primary_attempts": max_retries + 1,
        }
        return result
    except json.JSONDecodeError as e:
        logger.error(f"OR flex fallback returned invalid JSON: {e}")
    except Exception as e:
        logger.error(f"OR flex fallback failed: {e}")
    return {}

