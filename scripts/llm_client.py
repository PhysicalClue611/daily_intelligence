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
            content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
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
                if not text:
                    # Empty text is the free-form-payload equivalent of an
                    # unparseable JSON body — retry the same way, rather than
                    # returning an empty report_md as if it were a success.
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
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
        if parse_json:
            result = parse_llm_json(content, logger=logger)
            if not isinstance(result, dict):
                raise json.JSONDecodeError(
                    f"parse_llm_json returned {type(result).__name__}, expected dict",
                    content, 0)
        else:
            text = _strip_code_fence(content)
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

