"""Webhook intake hardening.

The X-Webhook-Secret / query-secret header auth was removed from the receiver:
the handler no longer calls authenticate_webhook_request, and inbound alerts are
gated by strategy_id validation (strategy_engine.require_strategy_id) instead of a
shared secret. These tests cover the controls that remain at the HTTP edge --
payload-size cap, rate limiting, queue-full back-pressure -- plus the strategy_id
gate that now decides whether an alert is processed or quarantined. The HMAC
helpers in security/webhook_auth.py still exist (unit-tested below) even though
do_POST no longer invokes them. Dashboard /api token auth is unchanged.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import dashboard as dash

RECEIVED_AT = "2026-06-22T00:00:00Z"


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _post(port: int, path: str, payload: dict, headers: dict | None = None):
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


def _get(port: int, path: str, headers: dict | None = None):
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def test_hmac_replay_window_boundary(wr, monkeypatch):
    # Unit test of the HMAC helper still shipped in security/webhook_auth.py.
    # do_POST no longer calls it, but the function's window math must stay correct.
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", True)
    monkeypatch.setattr(wr, "HERMX_WEBHOOK_HMAC_KEY", "hmac-key")
    monkeypatch.setattr(wr, "HERMX_REPLAY_WINDOW_SECONDS", 5.0)

    body = b"{}"
    headers = {"X-Webhook-Timestamp": "1000", "X-Webhook-Signature": ""}
    headers["X-Webhook-Signature"] = wr.compute_webhook_hmac("1000", body, "hmac-key")

    ok_edge, reason_edge = wr.verify_webhook_hmac(headers, body, now_seconds=1005.0)
    ok_out, reason_out = wr.verify_webhook_hmac(headers, body, now_seconds=1005.1)

    assert ok_edge is True
    assert reason_edge == "ok"
    assert ok_out is False
    assert reason_out == "hmac_replay_window"


def test_payload_size_cap_returns_413(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_MAX_BODY_BYTES", 8)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview", "extra": "abc"},
        )
        assert status == 413
        assert body.get("error") == "payload_too_large"
    finally:
        _stop(server, thread)


def test_rate_limit_rejects_excess_requests(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(wr, "HERMX_RATE_LIMIT_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))
    wr._RATE_LIMIT_BUCKETS.clear()

    server, thread = _serve(wr.Handler)
    try:
        status1, _ = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
            headers={"CF-Connecting-IP": "1.2.3.4"},
        )
        status2, body2 = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
            headers={"CF-Connecting-IP": "1.2.3.4"},
        )
        assert status1 == 200
        assert status2 == 429
        assert body2.get("error") == "rate_limited"
    finally:
        _stop(server, thread)


def test_queue_full_returns_503_and_drops(wr, monkeypatch):
    q = queue.Queue(maxsize=1)
    q.put(({"seed": True}, "ts"))
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
        )
        assert status == 503
        assert body.get("error") == "queue_full"
    finally:
        _stop(server, thread)


def test_alert_without_strategy_id_quarantined_when_required(wr, monkeypatch):
    # With strategy_engine.require_strategy_id armed, an alert carrying no
    # strategy_id is routed to the strategy-alert quarantine path (202) and never
    # processed -- this gate replaces the removed shared-secret auth.
    monkeypatch.setitem(wr.STRATEGY_ENGINE, "require_strategy_id", True)
    payload = {
        "source": "tradingview",
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "2h",
        "tv_time": RECEIVED_AT,
    }
    status, record = wr.build_record(payload, RECEIVED_AT)
    assert status == 202
    assert record["mode"] == "strategy_alert_quarantine"
    assert record["quarantined"] is True
    assert record["reason"] == "missing_strategy_id_required"


def test_alert_without_strategy_id_not_blocked_when_not_required(wr):
    # Corpus default require_strategy_id=False: a non-strategy alert is NOT
    # quarantined for a missing id (the gate is off).
    payload = {
        "source": "tradingview",
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "2h",
        "tv_time": RECEIVED_AT,
    }
    _status, record = wr.build_record(payload, RECEIVED_AT)
    assert record.get("reason") != "missing_strategy_id_required"


def test_dashboard_api_requires_auth(monkeypatch):
    monkeypatch.setattr(dash, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dash, "DASH_AUTH_TOKEN", "dash-token")
    monkeypatch.setattr(
        dash,
        "dashboard_model",
        lambda: {
            "generated_at": "2026-01-01T00:00:00Z",
            "loaded": {"historical_count": 0, "backfill_count": 0, "live_count": 0, "backfill": {}},
            "sim": {key: {"stats": {}} for key, _ in dash.POLICIES},
            "strategies": [],
            "strategy_alerts": [],
            "okx_live": {"ok": False},
            "okx_executions": [],
        },
    )

    server, thread = _serve(dash.Handler)
    try:
        status, _ = _get(server.server_address[1], "/api")
        assert status == 401

        status_ok, _ = _get(server.server_address[1], "/api", headers={"Authorization": "Bearer dash-token"})
        assert status_ok == 200

        basic = base64.b64encode(b"user:dash-token").decode("ascii")
        status_basic, _ = _get(server.server_address[1], "/api", headers={"Authorization": f"Basic {basic}"})
        assert status_basic == 200
    finally:
        _stop(server, thread)
