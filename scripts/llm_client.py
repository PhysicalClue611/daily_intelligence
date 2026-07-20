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
"""
import json
import logging
import os
import sys
import time

import httpx

# Cross-repo: canonical LLM-JSON-parsing helper lives in ~/Homepage (host-only
# path). See CLAUDE.md "JSON 解析健壮性" note for why this isn't duplicated
# locally; scripts/sas_review.py uses the same import pattern.
sys.path.insert(0, os.path.join(os.path.expanduser("~"), "Homepage"))
from llm_json_utils import parse_llm_json

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE_URL         = "https://openrouter.ai/api/v1/chat/completions"
OR_ATTRIBUTION_HEADERS = {"HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence", "X-OpenRouter-Title": "DailyIntel"}
LLM_MODEL           = "deepseek/deepseek-v4-flash"
LLM_MODEL_PASS2     = "deepseek/deepseek-v4-pro"
LLM_FALLBACK_FLASH  = "google/gemini-3.1-flash-lite"  # OR flex fallback for v4-flash
LLM_FALLBACK_PRO    = "google/gemini-3.5-flash"       # OR flex fallback for v4-pro
DS_OR_PROVIDERS     = {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}

def call_llm(prompt: str, system_prompt: str, max_retries: int = 2,
             model: str = LLM_MODEL) -> dict:
    """Call DeepSeek via OpenRouter, return parsed JSON dict. Retries on network errors.
    Pass 2 (deepseek-v4-pro) uses thinking:enabled — required by Together/Fireworks to emit content.
    Pass 1 (deepseek-v4-flash) sends NO thinking param — thinking:disabled breaks StreamLake fallback."""
    is_pass2 = (model == LLM_MODEL_PASS2)
    thinking_cfg = {"type": "enabled", "budget_tokens": 3000} if is_pass2 else None
    max_tokens = 8000 if is_pass2 else 4000
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
                    "provider": DS_OR_PROVIDERS,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
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
            logger.info(f"LLM tokens: prompt={usage.get('prompt_tokens')} "
                        f"completion={usage.get('completion_tokens')} "
                        f"provider={data.get('provider', 'n/a')}")

            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
            result = parse_llm_json(content, logger=logger)
            if not isinstance(result, dict):
                # A malformed outer object whose only cleanly-parsing substring is a
                # nested array (e.g. tavily_queries) makes parse_llm_json return that
                # array instead of the dict — treat as unparseable JSON, same as a
                # JSONDecodeError, so it's retried rather than crashing on result[...].
                raise json.JSONDecodeError(
                    f"parse_llm_json returned {type(result).__name__}, expected dict",
                    content, 0)
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
    fallback_model = LLM_FALLBACK_PRO if is_pass2 else LLM_FALLBACK_FLASH
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
                "temperature": 0.2,
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
        result = parse_llm_json(content, logger=logger)
        if not isinstance(result, dict):
            raise json.JSONDecodeError(
                f"parse_llm_json returned {type(result).__name__}, expected dict",
                content, 0)
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

