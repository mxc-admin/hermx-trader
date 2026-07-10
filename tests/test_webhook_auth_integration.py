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


def _all_persisted_text(wr) -> str:
    """Concatenated text of every durable ledger that could carry an alert payload --
    the raw-webhooks WAL (intake + downstream ``webhook`` rows) and the pipeline
    ledger (every worker-phase event). Used by the leak test to prove ``secret_key``
    reaches ZERO persisted rows across all of them."""
    text = []
    for path in (wr.RAW_WEBHOOK_LEDGER, wr.PIPELINE_LEDGER):
        if path.exists():
            text.append(path.read_text(encoding="utf-8"))
    return "\n".join(text)


def test_unauthenticated_post_rejected_and_no_wal_row(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "action": "buy"}

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
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "action": "buy"}

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
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "action": "buy"}

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


# --- Body ``secret_key`` transport (default for TradingView-native alerts) -----------
#
# TradingView's webhook alert action cannot send custom HTTP headers on any plan, so the
# ``secret_key`` JSON body field is the DEFAULT authentication transport for a direct
# alert. These drive the real Handler over loopback with NO ``X-Webhook-Secret`` header.


def test_body_secret_key_authenticated_post_accepted_and_wal_row_written(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "action": "buy", "secret_key": "test-secret"}

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", payload)  # no header
        assert status == 200
        assert body.get("status") == "queued"
        rows = _intake_rows(wr)
        assert len(rows) == 1
        assert rows[0]["received_at"] == body["received_at"]
    finally:
        _stop(server, thread)


def test_wrong_body_secret_key_rejected_and_no_wal_row(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {"source": "tradingview", "symbol": "BTCUSDT", "action": "buy", "secret_key": "wrong-secret"}

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", payload)  # no header
        assert status == 401
        assert body.get("ok") is False
        assert _intake_rows(wr) == []
    finally:
        _stop(server, thread)


def test_body_secret_key_never_persisted_across_intake_and_downstream(wr, monkeypatch):
    """MANDATORY leak test. A valid body-authenticated alert carrying ``secret_key``
    (top-level AND nested under ``extras``) must never appear in ANY persisted row --
    not the intake WAL row, not the downstream ``webhook`` raw row, not any pipeline
    event. Exercises the real HTTP intake redaction and then drives the same payload
    through the worker phase (process_payload_async) to generate the downstream rows.
    """
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    payload = {
        "source": "tradingview",
        "symbol": "BTCUSDT",
        "action": "buy",
        "timeframe": "1h",
        "tv_signal_price": "100.5",
        "tv_time": "2026-07-09T00:00:00Z",
        "secret_key": "test-secret",
        "extras": {"secret_key": "test-secret", "note": "keep-me"},
    }

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", payload)  # no header
        assert status == 200
        intake = _intake_rows(wr)
        assert len(intake) == 1
        # The intake pop redacted the live payload before the WAL write; the harmless
        # nested ``extras.note`` survives so we know we scrubbed narrowly, not broadly.
        assert intake[0]["payload"].get("secret_key") is None
        assert intake[0]["payload"].get("extras", {}).get("secret_key") is None
        # Drive the same (already-redacted) payload through the worker phase to emit the
        # downstream ``webhook`` raw row + pipeline events.
        wr.process_payload_async(intake[0]["payload"], intake[0]["received_at"])
    finally:
        _stop(server, thread)

    blob = _all_persisted_text(wr)
    assert blob, "expected persisted rows to scan"
    assert "secret_key" not in blob
    assert "test-secret" not in blob
    # Narrow-scrub sanity: legitimate extras content is preserved.
    assert "keep-me" in blob


def test_writer_scrub_backstops_a_forgetful_future_call_site(wr):
    """Belt-and-suspenders: even if a FUTURE persist call site forgets the intake pop
    and hands a payload that still contains ``secret_key`` (top-level and nested) to the
    worker phase, the writer-level scrub in record_raw_webhook / record_pipeline_event
    must strip it so nothing leaks into a durable ledger."""
    leaky_payload = {
        "source": "tradingview",
        "symbol": "ETHUSDT",
        "action": "sell",
        "timeframe": "1h",
        "tv_signal_price": "50.0",
        "tv_time": "2026-07-09T00:00:00Z",
        "secret_key": "test-secret",
        "extras": {"secret_key": "test-secret", "note": "keep-me"},
    }
    # No intake pop here -- simulate a call site that bypasses do_POST's redaction.
    wr.process_payload_async(leaky_payload, "2026-07-09T00:00:00.000000+00:00")

    blob = _all_persisted_text(wr)
    assert blob, "expected persisted rows to scan"
    assert "secret_key" not in blob
    assert "test-secret" not in blob
    assert "keep-me" in blob


# --- Non-dict JSON body guard --------------------------------------------------------
#
# A syntactically valid but non-object JSON body (list / string / number / bool) parses
# fine but has no ``.get()``/``.pop()``. Without a guard it would reach
# authenticate_webhook_request / the intake pop and raise AttributeError -> 500. It must
# instead be a clean 400 with no WAL row, BEFORE auth runs.

def test_non_dict_json_body_rejected_cleanly_no_crash(wr, monkeypatch):
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    server, thread = _serve(wr.Handler)
    try:
        port = server.server_address[1]
        for non_dict in ([1, 2, 3], "x", 5, True):
            status, body = _post(port, "/webhook", non_dict)  # json.dumps -> non-object body
            assert status == 400, f"expected 400 for {non_dict!r}, got {status}"
            assert body.get("ok") is False
            assert body.get("error") == "invalid_payload"
        # The guard runs before the intake record -> nothing persisted.
        assert _intake_rows(wr) == []
    finally:
        _stop(server, thread)
