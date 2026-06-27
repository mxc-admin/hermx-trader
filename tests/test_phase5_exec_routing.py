"""Phase 5 (P5-06/P5-07 cutover): submission ALWAYS routes through ExecutionService.

The legacy inline subprocess path and HERMX_EXEC_API backend selection are gone.
CCXT (via ExecutionService -> ExecutorFactory) is the sole write path. These cover
the routing decision and the fail-closed posture at the route boundary; deeper
gate/idempotency/journal behavior is exercised by the shared service code below.
"""
from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _armed_config() -> dict:
    return {
        "execution": {"enabled": True, "submit_orders": True, "simulated_trading": True, "force_ipv4": True, "exchange": "ccxt"},
        "risk": {"allow_live_execution": True},
    }


def _record(*, live_execution_enabled=True, auth_healthy=True):
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "auth_healthy": auth_healthy,
        "execution_readiness": {
            "live_execution_enabled": live_execution_enabled,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "okx_inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": "cid-p5"},
            "okx_fill": {"client_order_id": "cid-p5"},
            "block_reason": None,
        },
    }


def test_submission_always_routes_through_service(monkeypatch):
    """No backend toggle: every armed submission goes through ExecutionService."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")
    assert wr.ExecutionService is not None and wr.ExecutorFactory is not None
    assert wr.ExecutorFactory.available() == ["ccxt"]

    sentinel = {"ok": True, "mode": "routed-via-service"}
    service_spy = mock.Mock(return_value=sentinel)
    monkeypatch.setattr(wr, "_execute_okx_via_service", service_spy)
    rec = _record()

    out = wr.execute_okx_if_enabled(rec)

    service_spy.assert_called_once_with(rec)
    assert out is sentinel


def test_fails_closed_when_service_unavailable(monkeypatch):
    """ExecutionService missing => NEVER submit; not_submitted/execution_unavailable."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")
    monkeypatch.setattr(wr, "ExecutionService", None)
    service_spy = mock.Mock()
    monkeypatch.setattr(wr, "_execute_okx_via_service", service_spy)

    out = wr.execute_okx_if_enabled(_record())

    service_spy.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "execution_unavailable"


def test_fails_closed_when_no_backend_registered(monkeypatch):
    """ccxt import failed => empty registry => fail closed, never submit."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")
    service_spy = mock.Mock()
    monkeypatch.setattr(wr, "_execute_okx_via_service", service_spy)
    monkeypatch.setattr(wr.ExecutorFactory, "available", classmethod(lambda cls: []))

    out = wr.execute_okx_if_enabled(_record())

    service_spy.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "execution_unavailable"


def test_kill_switch_blocks_real_path(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "false")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_okx_if_enabled(_record())

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert "kill switch" in (out.get("reason") or "").lower()


def test_gate_false_blocks_real_path(monkeypatch):
    monkeypatch.setattr(
        wr,
        "CONFIG",
        {"execution": {"enabled": True, "submit_orders": False, "exchange": "ccxt"}, "risk": {"allow_live_execution": True}},
    )
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_okx_if_enabled(_record())

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
