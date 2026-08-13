"""
Regression tests for telegram_utils process-level httpx.Client (issue #65)
and ConnectTimeout retry (issue #58).

No pytest — plain asserts, same style as the other test_*.py here.

What these protect:
  - getUpdates / sendMessage share one Client instead of constructing a
    Client+TLS stack every 30s (the leak that took a KeepAlive daemon to
    350–450MB phys_footprint).
  - poll_telegram is a single shot (issue #25 / pitfall #79): the outer
    loop is the retry. In-call retries stay on the send path only.
  - Long-poll client timeout stays POLL_TIMEOUT+5 (pitfall #74).
  - Consecutive transport failures rebuild the Client (stale TLS after
    the Shadowrocket flake in pitfalls #74/#76).
  - call_telegram retries ConnectTimeout, not just ConnectError (#58).

Run: ~/Daily_Intelligence/.venv/bin/python scripts/test_telegram_utils.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

import telegram_utils as tu
import telegram_commands as tc


TOKEN = "test-token"


class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {"ok": True, "result": []}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=None, response=self)


class _FakeClient:
    constructed = 0

    def __init__(self, *args, **kwargs):
        _FakeClient.constructed += 1
        self.args = args
        self.kwargs = kwargs
        self.posts = []
        self.closed = False
        self._side_effects = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return _FakeResponse()

    def close(self):
        self.closed = True


def _install_fake_client(factory=None):
    tu.reset_telegram_client()
    _FakeClient.constructed = 0
    orig = tu.httpx.Client

    def _factory(*a, **k):
        client = (factory or _FakeClient)(*a, **k)
        return client

    tu.httpx.Client = _factory
    return orig


def _restore(orig):
    tu.httpx.Client = orig
    tu.reset_telegram_client()
    _FakeClient.constructed = 0


def test_call_telegram_reuses_one_client_across_calls():
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "sendMessage", {"chat_id": "1", "text": "a"})
        tu.call_telegram(TOKEN, "sendMessage", {"chat_id": "1", "text": "b"})
        assert _FakeClient.constructed == 1
        client = tu._telegram_client()
        assert len(client.posts) == 2
        assert client.posts[0]["json"]["text"] == "a"
        assert client.posts[1]["json"]["text"] == "b"
    finally:
        _restore(orig)


def test_poll_reuses_same_client_as_send():
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "sendMessage", {"chat_id": "1", "text": "hi"})
        updates = tu.poll_telegram(TOKEN, offset=7, poll_timeout=30)
        assert updates == []
        assert _FakeClient.constructed == 1
        client = tu._telegram_client()
        assert len(client.posts) == 2
        poll = client.posts[1]
        assert poll["json"]["offset"] == 7
        assert poll["json"]["timeout"] == 30
        assert poll["timeout"] == 35
        assert poll["url"].endswith("/getUpdates")
    finally:
        _restore(orig)


def test_poll_does_not_retry_transport_error():
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "getMe", {})  # create the client
        client = tu._telegram_client()
        client._side_effects = [httpx.ConnectError("eof")]
        raised = False
        try:
            tu.poll_telegram(TOKEN, offset=0)
        except httpx.ConnectError:
            raised = True
        assert raised
        # one failed poll only — no in-call retry
        assert len(client.posts) == 2  # getMe + one getUpdates
    finally:
        _restore(orig)


def test_call_telegram_retries_connect_timeout():
    """Issue #58: ConnectTimeout is a sibling of ConnectError, must retry."""
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "getMe", {})
        client = tu._telegram_client()
        client._side_effects = [
            httpx.ConnectTimeout("handshake"),
            _FakeResponse({"ok": True, "result": {"id": 1}}),
        ]
        out = tu.call_telegram(TOKEN, "sendMessage", {"chat_id": "1", "text": "x"})
        assert out == {"ok": True, "result": {"id": 1}}
        # getMe + failed send + retry send
        assert len(client.posts) == 3
    finally:
        _restore(orig)


def test_consecutive_transport_failures_rebuild_client():
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "getMe", {})
        first = tu._telegram_client()
        first._side_effects = [
            httpx.ConnectError("1"),
            httpx.ConnectError("2"),
            httpx.ConnectError("3"),
        ]
        for _ in range(3):
            try:
                tu.poll_telegram(TOKEN, offset=0)
            except httpx.ConnectError:
                pass
        assert first.closed
        assert _FakeClient.constructed == 1  # rebuilt lazily on next use
        tu.poll_telegram(TOKEN, offset=0)
        assert _FakeClient.constructed == 2
        second = tu._telegram_client()
        assert second is not first
        assert not second.closed
    finally:
        _restore(orig)


def test_aged_client_is_rebuilt_before_next_request():
    orig = _install_fake_client()
    try:
        tu.call_telegram(TOKEN, "getMe", {})
        first = tu._telegram_client()
        tu._force_client_age(tu.CLIENT_MAX_AGE_SEC + 1)
        tu.call_telegram(TOKEN, "getMe", {})
        assert first.closed
        assert _FakeClient.constructed == 2
        assert tu._telegram_client() is not first
    finally:
        _restore(orig)


def test_run_uses_poll_helper_not_bare_httpx_post():
    import inspect
    src = inspect.getsource(tc.run)
    assert "poll_telegram" in src
    assert "httpx.post" not in src


def test_process_recycles_after_24h_only():
    start = 1_000_000.0
    assert not tc._process_due_for_recycle(start, start + 86400 - 1)
    assert tc._process_due_for_recycle(start, start + 86400)
    assert tc._process_due_for_recycle(start, start + 86400 + 10)


if __name__ == "__main__":
    tests = [
        test_call_telegram_reuses_one_client_across_calls,
        test_poll_reuses_same_client_as_send,
        test_poll_does_not_retry_transport_error,
        test_call_telegram_retries_connect_timeout,
        test_consecutive_transport_failures_rebuild_client,
        test_aged_client_is_rebuilt_before_next_request,
        test_run_uses_poll_helper_not_bare_httpx_post,
        test_process_recycles_after_24h_only,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
