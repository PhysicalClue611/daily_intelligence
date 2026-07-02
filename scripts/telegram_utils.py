"""
telegram_utils.py — Shared Telegram Bot API call with connect-retry.

api.telegram.org is flaky at the TLS-handshake stage through this host's
network path, independent of request type or how long the call would run
(~5-25% instant ConnectError on fresh connections; see docs/PITFALLS.md
#74/#76). Every caller of the Telegram API — polling, sending, alerting —
needs to absorb this the same way: retry once immediately on connect-level
failure (it fails fast, ~3s, so there's nothing to back off from), and only
warn if the call is still failing after retries. A retry that recovers is
not a problem worth flagging at WARNING — it's the designed-for case.
"""
import logging

import httpx

logger = logging.getLogger(__name__)


def call_telegram(
    bot_token: str,
    endpoint: str,
    payload: dict,
    timeout: float = 10,
    max_connect_retries: int = 2,
) -> dict:
    """POST to the Telegram Bot API, retrying transient connect errors.

    Returns the parsed JSON response, or {} if the call ultimately failed.
    Logs INFO if a retry recovered the call, WARNING only when every attempt
    (including retries) failed.
    """
    last_err: Exception | None = None
    for attempt in range(max_connect_retries + 1):
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{bot_token}/{endpoint}",
                json=payload,
                timeout=timeout,
            )
            if attempt > 0:
                logger.info(f"Telegram API {endpoint} recovered after {attempt} retry(ies)")
            return resp.json()
        except httpx.ConnectError as e:
            last_err = e
            continue
        except Exception as e:
            logger.warning(f"Telegram API {endpoint} failed: {e}")
            return {}
    logger.warning(f"Telegram API {endpoint} failed after {max_connect_retries + 1} attempts: {last_err}")
    return {}
