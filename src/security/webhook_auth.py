#!/usr/bin/env python3
"""Pure webhook auth / rate-limit / HMAC helpers (Phase 1 step 3 extraction).

Behavior-preserving extraction of the security logic that used to live inline in
``webhook_receiver``. Every function here is a pure helper: all configuration
(secret, HMAC key, windows, limits) and mutable state (rate-limit buckets, lock)
are passed in as parameters rather than read from module globals, and any clock /
parse / alert collaborators are injected as callables. ``webhook_receiver`` keeps
thin wrappers that read its own module globals and delegate here, so the existing
public symbols stay monkeypatch-friendly for the test harness.

This module MUST NOT import ``webhook_receiver`` (no circular imports).
"""
from __future__ import annotations

import hashlib
import hmac
import stat
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


def webhook_auth_config_healthy(secret: str, require_hmac: bool, hmac_key: str) -> bool:
    """True when the webhook secret (and, if required, the HMAC key) is present."""
    if not secret:
        return False
    if require_hmac and not hmac_key:
        return False
    return True


def env_file_permissions_healthy(root: Path, path: Path | None = None) -> bool:
    """True when the ``.env`` file (default ``root/.env``) is not group/other readable."""
    target = path or (root / ".env")
    if not target.exists():
        return True
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return False
    return (mode & 0o077) == 0


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    """Best-effort client IP, preferring Cloudflare / proxy forwarding headers."""
    forwarded = (handler.headers.get("CF-Connecting-IP") or "").strip()
    if forwarded:
        return forwarded
    xff = (handler.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",", 1)[0].strip()
    if getattr(handler, "client_address", None):
        return str(handler.client_address[0])
    return "unknown"


def rate_limit_key(handler: BaseHTTPRequestHandler) -> str:
    """Rate-limit bucket key: always the client IP (via ``client_ip``).

    The previous ``X-Webhook-Key-Id`` branch was attacker-controlled -- a caller could
    rotate that header per request to mint a fresh bucket and bypass the sliding window.
    The receiver binds 127.0.0.1, so only the reverse proxy reaches it and the true
    client IP arrives in CF-Connecting-IP / X-Forwarded-For, which ``client_ip`` prefers.
    Per-key limits would require a trusted/verified key id, which we do not have.
    """
    return f"ip:{client_ip(handler)}"


def rate_limit_allow(
    source_key: str,
    buckets: dict,
    lock,
    window_seconds: float,
    max_requests: int,
    now_seconds: float | None = None,
) -> tuple[bool, dict]:
    """Sliding-window rate-limit check + record, mutating ``buckets`` under ``lock``."""
    now = time.time() if now_seconds is None else float(now_seconds)
    window = max(1.0, window_seconds)
    limit = max(1, max_requests)
    with lock:
        events = buckets.get(source_key, [])
        events = [ts for ts in events if now - ts <= window]
        allowed = len(events) < limit
        if allowed:
            events.append(now)
        buckets[source_key] = events
        return allowed, {
            "source_key": source_key,
            "window_seconds": window,
            "limit": limit,
            "count": len(events),
        }


def parse_replay_timestamp(value: str, parse_tv_time_fn: Callable) -> float | None:
    """Parse an epoch-seconds (or TradingView time) replay timestamp to a float."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        dt = parse_tv_time_fn(text)
        if dt is None:
            return None
        return dt.timestamp()


def compute_webhook_hmac(timestamp: str, body: bytes, key: str) -> str:
    """HMAC-SHA256 over ``timestamp || body`` keyed by ``key`` (hex digest)."""
    return hmac.new(key.encode("utf-8"), timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def verify_webhook_hmac(
    headers,
    body: bytes,
    require_hmac: bool,
    hmac_key: str,
    replay_window_seconds: float,
    parse_replay_timestamp_fn: Callable,
    compute_hmac_fn: Callable,
    now_seconds: float | None = None,
) -> tuple[bool, str]:
    """Validate the X-Webhook-Timestamp / X-Webhook-Signature pair (with replay window)."""
    if not require_hmac:
        return True, "hmac_not_required"
    if not hmac_key:
        return False, "hmac_key_missing"
    timestamp = (headers.get("X-Webhook-Timestamp") or "").strip()
    signature = (headers.get("X-Webhook-Signature") or "").strip()
    if not timestamp or not signature:
        return False, "hmac_header_missing"
    ts_value = parse_replay_timestamp_fn(timestamp)
    if ts_value is None:
        return False, "hmac_timestamp_invalid"
    now = time.time() if now_seconds is None else float(now_seconds)
    if abs(now - ts_value) > max(1.0, replay_window_seconds):
        return False, "hmac_replay_window"
    provided = signature.split("=", 1)[1] if signature.lower().startswith("sha256=") else signature
    expected = compute_hmac_fn(timestamp, body, hmac_key)
    if not hmac.compare_digest(provided, expected):
        return False, "hmac_mismatch"
    return True, "ok"


def authenticate_webhook_request(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    secret: str,
    client_ip_fn: Callable,
    verify_hmac_fn: Callable,
    auth_failure_alert_fn: Callable,
) -> tuple[bool, int, str]:
    """Authenticate a webhook request: shared secret then HMAC, alerting on failures."""
    client_ip_value = client_ip_fn(handler)
    path = urlparse(handler.path).path
    if not secret:
        auth_failure_alert_fn(path, client_ip_value)
        return False, 401, "missing_webhook_secret"
    provided = (handler.headers.get("X-Webhook-Secret") or "").strip()
    if not hmac.compare_digest(provided, secret):
        auth_failure_alert_fn(path, client_ip_value)
        return False, 401, "forbidden"
    ok, reason = verify_hmac_fn(handler.headers, body)
    if not ok:
        auth_failure_alert_fn(path, client_ip_value)
        return False, 401, reason
    return True, 200, "ok"
