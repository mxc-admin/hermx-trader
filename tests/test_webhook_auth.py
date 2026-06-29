"""Unit tests for the pure security helpers in ``security.webhook_auth``.

Focus: the ``trust_forwarding`` gate on ``client_ip`` / ``rate_limit_key`` that
prevents a spoofable CF-Connecting-IP / X-Forwarded-For header from minting fresh
rate-limit buckets when no trusted reverse proxy fronts the receiver.
"""
from __future__ import annotations

from security.webhook_auth import client_ip, rate_limit_key


class _FakeHeaders:
    """Minimal case-insensitive header lookup matching ``BaseHTTPRequestHandler.headers``."""

    def __init__(self, headers: dict | None = None) -> None:
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def get(self, name, default=None):
        return self._headers.get(name.lower(), default)


class _FakeHandler:
    def __init__(self, headers: dict | None = None, client_address=("203.0.113.7", 54321)) -> None:
        self.headers = _FakeHeaders(headers)
        self.client_address = client_address


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
