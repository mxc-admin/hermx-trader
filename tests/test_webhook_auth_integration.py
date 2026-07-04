"""do_POST authentication is wired end-to-end (L11 regression).

authenticate_webhook_request lives in security/webhook_auth.py but for a while had
ZERO call sites in the receiver -- HERMX_REQUIRE_HMAC / HERMX_SECRET were no-ops and
anyone who could reach the port could inject a trading signal. These tests drive the
real Handler over loopback and assert the auth gate runs BEFORE the intake WAL row is
written: an unauthenticated POST gets 401 and leaves raw-webhooks.jsonl empty, while a
correctly-authenticated POST gets 200 and records the intake row. The HMAC-required
path is exercised too, since HERMX_REQUIRE_HMAC=true must actually be enforced.
"""
from __future__ import annotations

import json
import queue
import time
from http.client import HTTPConnection

from conftest import _serve, _stop


def _post(port, path, payload, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if headers:
        req_headers.update(headers)
    conn.request("POST", path, body=body, headers=req_headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        parsed = {}
    return resp.status, parsed


def _intake_rows(wr):
    """The intake WAL rows currently written to raw-webhooks.jsonl (may be empty)."""
    path = wr.RAW_WEBHOOK_LEDGER
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return [r for r in rows if r.get("phase") == "intake"]


def test_unauthenticated_post_rejected_and_no_wal_row(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "side": "buy"}

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", payload)
        assert status == 401
        assert body.get("ok") is False
        # The auth gate runs before the intake record -> no WAL row for the drop.
        assert _intake_rows(wr) == []
    finally:
        _stop(server, thread)


def test_authenticated_post_accepted_and_wal_row_written(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "side": "buy"}

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            payload,
            headers={"X-Webhook-Secret": "test-secret"},
        )
        assert status == 200
        assert body.get("status") == "queued"
        rows = _intake_rows(wr)
        assert len(rows) == 1
        assert rows[0]["received_at"] == body["received_at"]
    finally:
        _stop(server, thread)


def test_hmac_required_path_enforced(wr, monkeypatch):
    # With HERMX_REQUIRE_HMAC armed, the shared secret alone is not enough: a request
    # missing a valid signature is rejected, a correctly-signed one is accepted.
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", True)
    monkeypatch.setattr(wr, "HERMX_WEBHOOK_HMAC_KEY", "hmac-key")
    monkeypatch.setattr(wr, "HERMX_REPLAY_WINDOW_SECONDS", 300.0)
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "side": "buy"}

    server, thread = _serve(wr.Handler)
    try:
        port = server.server_address[1]

        # Correct secret but no HMAC signature -> 401.
        status_missing, _ = _post(port, "/webhook", payload, headers={"X-Webhook-Secret": "test-secret"})
        assert status_missing == 401

        # Correct secret + valid signature -> 200.
        ts = str(int(time.time()))
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = wr.compute_webhook_hmac(ts, body_bytes, "hmac-key")
        status_ok, body_ok = _post(
            port,
            "/webhook",
            payload,
            headers={
                "X-Webhook-Secret": "test-secret",
                "X-Webhook-Timestamp": ts,
                "X-Webhook-Signature": sig,
            },
        )
        assert status_ok == 200
        assert body_ok.get("status") == "queued"
    finally:
        _stop(server, thread)
