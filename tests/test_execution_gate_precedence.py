"""Gate-precedence money-safety tests, asserted through the EXECUTOR/FACTORY seam.

Post P5-06/P5-07 cutover the only write path is ExecutionService -> CcxtExecutor.
There is no subprocess submit anymore, so the seam these tests assert against is
``ExecutorFactory.create`` returning an executor whose ``.execute`` is the single
submit call. The invariant is unchanged: if ANY gate (or the kill switch) blocks,
that submit is NEVER reached; when every gate passes, it is called exactly once.
"""
from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _armed_config() -> dict:
    # Phase A: no config arming flags. The per-strategy submit flag (surfaced as
    # readiness.live_execution_enabled) plus auth + watchdog health is the whole gate.
    return {"execution": {"exchange": "ccxt"}}


def _record(*, live_execution_enabled=True, auth_healthy=True):
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "auth_healthy": auth_healthy,
        "execution_readiness": {
            "live_execution_enabled": live_execution_enabled,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": "cid"},
            "okx_fill": {"client_order_id": "cid"},
            "block_reason": None,
        },
    }


def _adapter_ok() -> dict:
    """A normalized successful adapter result (BaseExecutor.normalized_result shape)."""
    return {
        "ok": True,
        "mode": "submit_enabled",
        "exchange": "ccxt",
        "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": "cid"},
        "payload": {"symbol": "XRP/USDT:USDT"},
    }


def _fake_executor():
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_adapter_ok())
    return fake


def test_any_gate_false_means_not_submitted(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setattr(wr, "SECRET", "phase2-test-secret")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)

    # Phase A gate inputs: the per-strategy submit flag (readiness.live_execution_enabled,
    # the equivalent of strategy.submit_orders=false) and the auth-health gate. If EITHER
    # is false the executor is never built and nothing submits.
    cases = [
        _record(live_execution_enabled=False),
        _record(auth_healthy=False),
    ]

    for rec in cases:
        fake = _fake_executor()
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
            out = wr.execute_okx_if_enabled(rec)
        # The blocked gate returns BEFORE the executor is ever built or called.
        create_mock.assert_not_called()
        fake.execute.assert_not_called()
        assert out["mode"] == "not_submitted"


def test_all_gates_true_can_submit(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setattr(wr, "SECRET", "phase2-test-secret")
    monkeypatch.setattr(wr, "HERMX_REQUIRE_HMAC", False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_okx_if_enabled(_record())

    # Every gate affirmative => the single executor submit is invoked exactly once.
    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"
