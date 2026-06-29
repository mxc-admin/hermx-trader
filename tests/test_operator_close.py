"""P0 -- operator-instructed close (POST /api/close + execute_operator_close).

A close is a RISK-REDUCING flatten an operator triggers out-of-band. It routes
through the SAME controlled ExecutionService as a normal submit, but the readiness
``close_only`` flag bypasses exactly two gates -- the global HERMX_LIVE_TRADING kill
switch and the per-symbol pause -- because both exist to stop NEW risk and a close
only reduces it. Every other gate (submit_orders arming, idempotency, auth, watchdog)
still applies.

These tests are fully offline: the executor is mocked via ``ExecutorFactory.create``
so the only way the submit call is reached is by passing the gate chain; no
exchange/network is touched. The ``wr`` fixture gives each test an isolated temp
order journal so submitting tests do not collide on the deterministic close id.
"""
from __future__ import annotations

import io
import json
from unittest import mock


def _armed_config() -> dict:
    # exchange "ccxt" matches the registered adapter backend (mirrors the kill-switch
    # suite). The executor itself is mocked, so no venue is touched.
    return {"execution": {"exchange": "ccxt"}}


def _strategy(*, submit_orders: bool = True, execution_mode: str = "live",
              sid: str = "btcusdt_duo_base_dev_2h") -> dict:
    return {
        "strategy_id": sid,
        "submit_orders": submit_orders,
        "execution_mode": execution_mode,
        "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        "leverage": 2,
        "margin_mode": "isolated",
        "asset": "BTCUSDT",
    }


def _adapter_ok() -> dict:
    return {
        "ok": True,
        "mode": "submit_enabled",
        "exchange": "ccxt",
        "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {},
    }


def _fake_executor():
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_adapter_ok())
    return fake


def _make_handler(wr, *, token=None, body=None, raw=None, content_length=None):
    """Construct a Handler without the socket-bound BaseHTTPRequestHandler.__init__,
    stub its request inputs, and capture _send(status, body) calls."""
    handler = wr.Handler.__new__(wr.Handler)
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Dashboard-Token"] = token
    if raw is None:
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
    headers["Content-Length"] = str(len(raw) if content_length is None else content_length)
    handler.headers = headers
    handler.rfile = io.BytesIO(raw)
    captured: list[tuple[int, dict]] = []
    handler._send = lambda status, payload: captured.append((status, payload))
    return handler, captured


# ---------------------------------------------------------------------------
# 1. Kill-switch bypass -- the critical regression test.
# ---------------------------------------------------------------------------

def test_close_bypasses_kill_switch_when_live_trading_disabled(wr, monkeypatch):
    """A LIVE-mode close succeeds even with HERMX_LIVE_TRADING unset. Without the
    close_only bypass this exact record would be blocked ``live_trading_disabled``."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_operator_close("BTCUSDT", _strategy(execution_mode="live"),
                                        operator="telegram", reason="operator_instructed_close")

    create_mock.assert_called_once()
    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"
    assert out["_cl_ord_id"].startswith("operator_close_BTCUSDT_btcusdt_duo_base_dev_2h_")


# ---------------------------------------------------------------------------
# 2. submit_orders gate still applies.
# ---------------------------------------------------------------------------

def test_close_blocked_when_submit_orders_false(wr, monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_operator_close("BTCUSDT", _strategy(submit_orders=False, execution_mode="demo"))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"


# ---------------------------------------------------------------------------
# 3. Auth -- X-Dashboard-Token must equal HERMX_SECRET (constant-time).
# ---------------------------------------------------------------------------

def test_close_rejects_missing_token(wr):
    handler, captured = _make_handler(wr, token=None, body={"symbol": "BTCUSDT", "strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 401
    assert payload["ok"] is False


def test_close_rejects_wrong_token(wr):
    handler, captured = _make_handler(wr, token="not-the-secret", body={"symbol": "BTCUSDT", "strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 401
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# 4. Validation -- symbol and strategy_id are required.
# ---------------------------------------------------------------------------

def test_close_requires_symbol(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 400
    assert payload["error"] == "missing_symbol"


def test_close_requires_strategy_id(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"symbol": "BTCUSDT"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 400
    assert payload["error"] == "missing_strategy_id"


# ---------------------------------------------------------------------------
# 5. Strategy not found -> 404.
# ---------------------------------------------------------------------------

def test_close_unknown_strategy_returns_404(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"symbol": "BTCUSDT", "strategy_id": "does_not_exist"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 404
    assert payload["error"] == "unknown_strategy_id"


# ---------------------------------------------------------------------------
# 6. Idempotency -- a repeat close (same deterministic cl_ord_id) is refused.
# ---------------------------------------------------------------------------

def test_close_is_idempotent_on_duplicate_cl_ord_id(wr, monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    strategy = _strategy(execution_mode="demo")  # demo: sandbox submit, no kill-switch concern

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        first = wr.execute_operator_close("BTCUSDT", strategy)
        second = wr.execute_operator_close("BTCUSDT", strategy)

    assert first["ok"] is True
    assert first["mode"] == "submit_enabled"
    assert second["mode"] == "not_submitted"
    assert second["reason"] == "duplicate_cl_ord_id"
    # Only the first close ever reached the executor.
    fake.execute.assert_called_once()
