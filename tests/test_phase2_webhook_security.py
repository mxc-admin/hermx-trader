from __future__ import annotations

import base64
import json
import queue
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import dashboard as dash
import webhook_receiver as wr


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


def test_query_secret_rejected_header_required(wr, monkeypatch):
    monkeypatch.setattr(wr, "SECRET", "s3cr3t")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook?secret=s3cr3t", {"source": "tradingview"})
        assert status == 401
        assert body.get("error") in {"forbidden", "missing_webhook_secret"}

        status_ok, body_ok = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
            headers={"X-Webhook-Secret": "s3cr3t"},
        )
        assert status_ok == 200
        assert body_ok.get("status") == "queued"
    finally:
        _stop(server, thread)


def test_hmac_replay_window_boundary(wr, monkeypatch):
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
    monkeypatch.setattr(wr, "SECRET", "s3cr3t")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
    monkeypatch.setattr(wr, "HERMX_MAX_BODY_BYTES", 8)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview", "extra": "abc"},
            headers={"X-Webhook-Secret": "s3cr3t"},
        )
        assert status == 413
        assert body.get("error") == "payload_too_large"
    finally:
        _stop(server, thread)


def test_rate_limit_rejects_excess_requests(wr, monkeypatch):
    monkeypatch.setattr(wr, "SECRET", "s3cr3t")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
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
            headers={"X-Webhook-Secret": "s3cr3t", "CF-Connecting-IP": "1.2.3.4"},
        )
        status2, body2 = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
            headers={"X-Webhook-Secret": "s3cr3t", "CF-Connecting-IP": "1.2.3.4"},
        )
        assert status1 == 200
        assert status2 == 429
        assert body2.get("error") == "rate_limited"
    finally:
        _stop(server, thread)


def test_missing_secret_fails_closed(wr, monkeypatch):
    monkeypatch.setattr(wr, "SECRET", "")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", {"source": "tradingview"})
        assert status == 401
        assert body.get("error") == "missing_webhook_secret"
    finally:
        _stop(server, thread)


def test_blank_secret_blocks_submission_regardless_of_strategy(wr, monkeypatch):
    # A blank HERMX_SECRET must 401 at the handler BEFORE the alert is ever enqueued --
    # even for a fully-armed strategy alert. Auth precedes (and therefore blocks) the
    # entire submission pipeline; strategy config can never override a missing secret.
    monkeypatch.setattr(wr, "SECRET", "")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
    monkeypatch.setattr(wr, "CONFIG", {"execution": {"exchange": "ccxt"}, "risk": {"allow_live_execution": True}})
    q = queue.Queue(maxsize=10)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    armed_alert = {
        "source": "tradingview",
        "strategy_id": "btcusdt_duo_base_dev_2h",
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "2h",
    }
    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(server.server_address[1], "/webhook", armed_alert,
                             headers={"X-Webhook-Secret": "anything"})
        assert status == 401
        assert body.get("error") == "missing_webhook_secret"
        assert q.qsize() == 0  # nothing was enqueued => nothing can be submitted
    finally:
        _stop(server, thread)


def test_hmac_replay_window_enforced(wr, monkeypatch):
    monkeypatch.setattr(wr, "SECRET", "s3cr3t")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", True)
    monkeypatch.setattr(wr, "HERMX_WEBHOOK_HMAC_KEY", "hmac-key")
    monkeypatch.setattr(wr, "HERMX_REPLAY_WINDOW_SECONDS", 5.0)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=10))

    payload = {"source": "tradingview"}
    raw = json.dumps(payload).encode("utf-8")
    stale_ts = "1000"
    stale_sig = wr.compute_webhook_hmac(stale_ts, raw, "hmac-key")

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            payload,
            headers={
                "X-Webhook-Secret": "s3cr3t",
                "X-Webhook-Timestamp": stale_ts,
                "X-Webhook-Signature": stale_sig,
            },
        )
        assert status == 401
        assert body.get("error") in {"hmac_replay_window", "hmac_timestamp_invalid", "hmac_mismatch"}
    finally:
        _stop(server, thread)


def test_queue_full_returns_503_and_drops(wr, monkeypatch):
    monkeypatch.setattr(wr, "SECRET", "s3cr3t")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)
    q = queue.Queue(maxsize=1)
    q.put(({"seed": True}, "ts"))
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    server, thread = _serve(wr.Handler)
    try:
        status, body = _post(
            server.server_address[1],
            "/webhook",
            {"source": "tradingview"},
            headers={"X-Webhook-Secret": "s3cr3t"},
        )
        assert status == 503
        assert body.get("error") == "queue_full"
    finally:
        _stop(server, thread)


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
