"""Unit tests for the pure security helpers in ``security.webhook_auth``.

Focus: the ``trust_forwarding`` gate on ``client_ip`` / ``rate_limit_key`` that
prevents a spoofable CF-Connecting-IP / X-Forwarded-For header from minting fresh
rate-limit buckets when no trusted reverse proxy fronts the receiver.
"""
from __future__ import annotations

import threading

from security.webhook_auth import (
    authenticate_webhook_request,
    client_ip,
    rate_limit_allow,
    rate_limit_key,
)


class _FakeHeaders:
    """Minimal case-insensitive header lookup matching ``BaseHTTPRequestHandler.headers``.

    Mirrors ``BaseHTTPRequestHandler.headers`` semantics: an ABSENT header returns the
    default (``None``), while a present-but-empty header returns ``""`` -- the exact
    distinction the header-absent vs header-present-but-wrong auth branch relies on.
    """

    def __init__(self, headers: dict | None = None) -> None:
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def get(self, name, default=None):
        return self._headers.get(name.lower(), default)


class _FakeHandler:
    def __init__(self, headers: dict | None = None, client_address=("203.0.113.7", 54321), path="/webhook") -> None:
        self.headers = _FakeHeaders(headers)
        self.client_address = client_address
        self.path = path


def test_client_ip_ignores_forwarding_headers_when_untrusted():
    handler = _FakeHandler(
        headers={"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8, 9.10.11.12"},
        client_address=("203.0.113.7", 54321),
    )
    assert client_ip(handler, trust_forwarding=False) == "203.0.113.7"


def test_client_ip_respects_cf_connecting_ip_when_trusted():
    handler = _FakeHandler(
        headers={"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"},
        client_address=("203.0.113.7", 54321),
    )
    assert client_ip(handler, trust_forwarding=True) == "1.2.3.4"
    # Default keeps existing behavior (trust on).
    assert client_ip(handler) == "1.2.3.4"


def test_client_ip_uses_xff_first_hop_when_trusted_and_no_cf():
    handler = _FakeHandler(
        headers={"X-Forwarded-For": "5.6.7.8, 9.10.11.12"},
        client_address=("203.0.113.7", 54321),
    )
    assert client_ip(handler, trust_forwarding=True) == "5.6.7.8"


def test_rate_limit_key_untrusted_uses_raw_client_address():
    handler = _FakeHandler(
        headers={"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"},
        client_address=("203.0.113.7", 54321),
    )
    assert rate_limit_key(handler, trust_forwarding=False) == "ip:203.0.113.7"


def test_rate_limit_key_trusted_prefers_forwarding_header():
    handler = _FakeHandler(
        headers={"CF-Connecting-IP": "1.2.3.4"},
        client_address=("203.0.113.7", 54321),
    )
    assert rate_limit_key(handler, trust_forwarding=True) == "ip:1.2.3.4"
    # Spoofed header must not change the bucket when forwarding is untrusted.
    assert rate_limit_key(handler, trust_forwarding=False) == "ip:203.0.113.7"


# --- authenticate_webhook_request: header + body ``secret_key`` transports ----------
#
# ``secret_key`` in the JSON body is the DEFAULT auth transport for direct
# TradingView-native alerts (their webhook action cannot send custom HTTP headers on any
# plan). The ``X-Webhook-Secret`` header remains for relay/proxy setups. These tests pin
# the precedence and every fail-closed edge, including the critical regression guard that
# a present-but-wrong header NEVER falls through to the body.

_SECRET = "test-secret"


def _auth(handler, *, secret=_SECRET, body_secret=None, hmac_result=(True, "ok")):
    """Drive authenticate_webhook_request with recording collaborators.

    Returns ``(result_tuple, alert_calls)`` where ``result_tuple`` is
    ``(ok, status, reason)`` and ``alert_calls`` counts auth-failure alerts fired.
    """
    alerts: list = []
    result = authenticate_webhook_request(
        handler,
        b"{}",
        secret,
        lambda h: "1.2.3.4",
        lambda headers, body: hmac_result,
        lambda path, ip: alerts.append((path, ip)),
        body_secret=body_secret,
    )
    return result, alerts


def test_auth_header_present_and_correct_authenticates():
    handler = _FakeHandler(headers={"X-Webhook-Secret": _SECRET})
    (ok, status, reason), alerts = _auth(handler)
    assert (ok, status, reason) == (True, 200, "ok")
    assert alerts == []


def test_auth_header_present_but_wrong_is_401_even_when_body_secret_correct():
    # Critical regression guard: a present-but-wrong header must NOT fall through to the
    # body ``secret_key`` transport, even when the body value is the correct secret.
    handler = _FakeHandler(headers={"X-Webhook-Secret": "nope"})
    (ok, status, reason), alerts = _auth(handler, body_secret=_SECRET)
    assert (ok, status, reason) == (False, 401, "forbidden")
    assert len(alerts) == 1


def test_auth_header_present_but_empty_is_401():
    # An empty-string header counts as PRESENT (not absent), so it must match -> 401.
    handler = _FakeHandler(headers={"X-Webhook-Secret": ""})
    (ok, status, reason), _ = _auth(handler, body_secret=_SECRET)
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_header_absent_body_secret_correct_authenticates():
    handler = _FakeHandler(headers={})  # no X-Webhook-Secret
    (ok, status, reason), alerts = _auth(handler, body_secret=_SECRET)
    assert (ok, status, reason) == (True, 200, "ok")
    assert alerts == []


def test_auth_header_absent_body_secret_wrong_is_401():
    handler = _FakeHandler(headers={})
    (ok, status, reason), _ = _auth(handler, body_secret="wrong")
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_header_absent_and_no_body_secret_is_401():
    handler = _FakeHandler(headers={})
    (ok, status, reason), _ = _auth(handler, body_secret=None)
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_non_string_body_secret_fails_closed():
    # A numeric secret_key must never blow up hmac.compare_digest -> treated as no-match.
    handler = _FakeHandler(headers={})
    (ok, status, reason), _ = _auth(handler, body_secret=12345)
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_secret_unset_is_missing_webhook_secret_even_with_correct_body():
    # Fail-closed: server ``HERMX_SECRET`` unset -> 401 missing_webhook_secret regardless
    # of what the caller sends in the body.
    handler = _FakeHandler(headers={})
    (ok, status, reason), _ = _auth(handler, secret="", body_secret="anything")
    assert (ok, status, reason) == (False, 401, "missing_webhook_secret")


def test_auth_body_path_still_enforces_hmac_when_required():
    # The body transport does NOT bypass HMAC: a valid body secret with a failing HMAC
    # check is still rejected with the HMAC reason.
    handler = _FakeHandler(headers={})
    (ok, status, reason), alerts = _auth(
        handler, body_secret=_SECRET, hmac_result=(False, "hmac_header_missing")
    )
    assert (ok, status, reason) == (False, 401, "hmac_header_missing")
    assert len(alerts) == 1


# --- Non-ASCII secrets must not crash compare_digest --------------------------------
#
# hmac.compare_digest raises TypeError when a str operand contains non-ASCII characters.
# A non-ASCII header/body secret (e.g. "café") must therefore be encoded to bytes so the
# comparison fails CLOSED (forbidden) instead of raising a TypeError -> 500.

def test_auth_non_ascii_header_secret_fails_closed_without_typeerror():
    handler = _FakeHandler(headers={"X-Webhook-Secret": "café"})  # non-ASCII, wrong secret
    (ok, status, reason), _ = _auth(handler)  # secret is ASCII _SECRET
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_non_ascii_body_secret_fails_closed_without_typeerror():
    handler = _FakeHandler(headers={})  # header absent -> body path
    (ok, status, reason), _ = _auth(handler, body_secret="café")  # non-ASCII, wrong
    assert (ok, status, reason) == (False, 401, "forbidden")


def test_auth_non_ascii_secret_matches_authenticates():
    # A correct non-ASCII secret still authenticates (behavior preserved for any UTF-8 value).
    handler = _FakeHandler(headers={"X-Webhook-Secret": "café"})
    (ok, status, reason), _ = _auth(handler, secret="café")
    assert (ok, status, reason) == (True, 200, "ok")


# --- rate_limit_allow reclaims fully-expired buckets --------------------------------
#
# An idle sender's bucket key would otherwise linger forever (the per-key window filter
# only runs when THAT key sends again). Fully-expired buckets must be reclaimed on any
# call, while a sender with an in-window event is left untouched.

def test_rate_limit_reclaims_idle_bucket_and_keeps_active_untouched():
    buckets: dict = {}
    lock = threading.Lock()
    kw = dict(window_seconds=60, max_requests=100)
    # idle sender: a single event at t=1000 and never returns.
    rate_limit_allow("ip:idle", buckets, lock, now_seconds=1000.0, **kw)
    # active sender: stays warm with events at t=1000 and t=1050 (both in-window at 1050).
    rate_limit_allow("ip:active", buckets, lock, now_seconds=1000.0, **kw)
    rate_limit_allow("ip:active", buckets, lock, now_seconds=1050.0, **kw)
    # At 1050 idle's event is only 50s old (<=60) -> not yet reclaimed.
    assert buckets["ip:idle"] == [1000.0]
    assert buckets["ip:active"] == [1000.0, 1050.0]

    # A new request at t=1065: idle's only event (65s old) is fully expired -> reclaimed;
    # active still has an in-window event at 1050 (15s old) -> its bucket is left untouched.
    rate_limit_allow("ip:newcomer", buckets, lock, now_seconds=1065.0, **kw)
    assert "ip:idle" not in buckets
    assert buckets["ip:active"] == [1000.0, 1050.0]
    assert buckets["ip:newcomer"] == [1065.0]


def test_rate_limit_active_sender_window_behavior_preserved():
    # Regression guard: reclaim must not weaken the sliding window for an active sender.
    buckets: dict = {}
    lock = threading.Lock()
    kw = dict(window_seconds=60, max_requests=3)
    for i in range(3):
        allowed, _ = rate_limit_allow("ip:x", buckets, lock, now_seconds=1000.0 + i, **kw)
        assert allowed is True
    # 4th request inside the window is blocked and the bucket is retained (not reclaimed).
    allowed, _ = rate_limit_allow("ip:x", buckets, lock, now_seconds=1003.0, **kw)
    assert allowed is False
    assert len(buckets["ip:x"]) == 3
