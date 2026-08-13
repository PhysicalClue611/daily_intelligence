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

Issue #65: a KeepAlive long-poll that called httpx.post() every 30s leaked
~350–450MB over hours (MALLOC_NANO/TINY never returned to the OS). Top-level
httpx.post already does `with Client()`, so this is not an unclosed-client
bug — it is per-request Client+TLS construction fragmenting libmalloc in a
process that never exits. One process-lifetime Client, rebuilt on persistent
transport failure or after CLIENT_MAX_AGE_SEC, is the actual fix.
"""
import logging
import time

import httpx

logger = logging.getLogger(__name__)

CLIENT_MAX_AGE_SEC = 6 * 3600
REBUILD_AFTER_FAILURES = 3

_client: httpx.Client | None = None
_client_created_at: float = 0.0
_client_failures: int = 0


def reset_telegram_client() -> None:
    """Close and drop the process-level client. Safe to call when none exists."""
    global _client, _client_created_at, _client_failures
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _client_created_at = 0.0
    _client_failures = 0


def _telegram_client() -> httpx.Client | None:
    """Test/debug accessor for the live Client (may be None)."""
    return _client


def _force_client_age(seconds: float) -> None:
    """Backdate the live Client's creation time. Tests only."""
    global _client_created_at
    if _client is None:
        raise RuntimeError("no telegram client to age")
    _client_created_at = time.time() - seconds


def _get_client() -> httpx.Client:
    global _client, _client_created_at, _client_failures
    now = time.time()
    if (
        _client is not None
        and _client_created_at
        and (now - _client_created_at) >= CLIENT_MAX_AGE_SEC
    ):
        reset_telegram_client()
    if _client is None:
        _client = httpx.Client(timeout=10.0)
        _client_created_at = now
        _client_failures = 0
    return _client


def _telegram_post(url: str, payload: dict, timeout: float) -> httpx.Response:
    global _client_failures
    client = _get_client()
    try:
        resp = client.post(url, json=payload, timeout=timeout)
        _client_failures = 0
        return resp
    except httpx.TransportError:
        _client_failures += 1
        if _client_failures >= REBUILD_AFTER_FAILURES:
            reset_telegram_client()
        raise


def call_telegram(
    bot_token: str,
    endpoint: str,
    payload: dict,
    timeout: float = 10,
    max_connect_retries: int = 2,
) -> dict:
    """POST to the Telegram Bot API, retrying transient transport errors.

    Returns the parsed JSON response, or {} if the call ultimately failed.
    Logs INFO if a retry recovered the call, WARNING only when every attempt
    (including retries) failed.

    Retries httpx.TransportError (ConnectError, ConnectTimeout, ReadError,
    …) — ConnectTimeout is a sibling of ConnectError, not a subclass
    (issue #58 / docs/PITFALLS.md #87). HTTP 4xx/other exceptions are not
    retried.
    """
    url = f"https://api.telegram.org/bot{bot_token}/{endpoint}"
    last_err: Exception | None = None
    for attempt in range(max_connect_retries + 1):
        try:
            resp = _telegram_post(url, payload, timeout)
            if attempt > 0:
                logger.info(f"Telegram API {endpoint} recovered after {attempt} retry(ies)")
            return resp.json()
        except httpx.TransportError as e:
            last_err = e
            continue
        except Exception as e:
            logger.warning(f"Telegram API {endpoint} failed: {e}")
            return {}
    logger.warning(f"Telegram API {endpoint} failed after {max_connect_retries + 1} attempts: {last_err}")
    return {}


def poll_telegram(
    bot_token: str,
    offset: int,
    poll_timeout: int = 30,
    client_timeout: float | None = None,
) -> list:
    """Single getUpdates. The caller's loop is the retry (issue #25).

    `client_timeout` defaults to poll_timeout+5 so the HTTP read timeout
    outlives Telegram's long-poll wait (docs/PITFALLS.md #74).
    """
    if client_timeout is None:
        client_timeout = poll_timeout + 5
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    resp = _telegram_post(
        url,
        {"offset": offset, "timeout": poll_timeout, "limit": 20},
        client_timeout,
    )
    return resp.json().get("result", [])
