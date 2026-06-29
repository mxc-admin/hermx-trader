"""Partial multi-leg submission must NOT be journalled as a flat REJECTED (H3).

When the close leg of a reversal reaches the venue but the open leg fails, the
adapter returns ``ok=False, mode="submit_partial"``. The venue already moved the
position (the close executed), so the outcome is UNCERTAIN -- the order journal must
record UNKNOWN (needs reconciliation), never a terminal REJECTED that would corrupt
position math by implying nothing happened.

Fully offline: the executor is mocked, so no exchange/network is touched.
"""
from __future__ import annotations

from unittest import mock



def _armed_config() -> dict:
    return {"execution": {"exchange": "ccxt"}}


def _armed_record(cl: str = "mxc-xrpusdt-buy-partial0000000de") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }


def _partial_adapter_result() -> dict:
    """Adapter result for a close-submitted / open-failed reversal."""
    return {
        "ok": False,
        "mode": "submit_partial",
        "exchange": "ccxt",
        "elapsed_ms": 7,
        "fill_summary": {
            "status": "submit_partial",
            "order_id": "close-ord-1",
            "client_order_id": None,
            "position_after_order": {"side": "flat", "contracts": 0.0},
        },
        "payload": {
            "executed_orders": [
                {"action": "CLOSE_SHORT", "submitted": True, "status": "submitted", "order": {"id": "close-ord-1"}},
                {"action": "OPEN_LONG", "submitted": True, "status": "rejected", "error": "insufficient_margin"},
            ],
        },
    }


def test_submit_partial_records_unknown_not_rejected(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-partial0000000de"

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_partial_adapter_result())
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    result = wr.execute_if_enabled(_armed_record(cl))

    fake.execute.assert_called_once()
    assert result["mode"] == "submit_partial"
    assert result["ok"] is False

    # The journal must record UNKNOWN (needs reconciliation), not a terminal REJECTED.
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]

    # UNKNOWN is non-terminal: the order stays OPEN for reconciliation.
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["cl_ord_id"] == cl
    assert open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


def test_submit_partial_emits_operator_alert_when_reconcile_enabled(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-partial-alert00de"
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")
    # No reconciliation executor available => tentative UNKNOWN is kept; the partial
    # alert is still emitted so an operator notices the half-executed reversal.
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: None)

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_partial_adapter_result())
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    wr.execute_if_enabled(_armed_record(cl))

    alerts = wr.read_jsonl_tolerant(wr.ALERTS_LEDGER)
    partial = [a for a in alerts if a.get("kind") == "reconcile" and a["detail"].get("stage") == "post_submit_partial"]
    assert len(partial) == 1
    assert partial[0]["detail"]["cl_ord_id"] == cl
    assert partial[0]["detail"]["reason"] == "submit_partial"


def test_adapter_exception_with_reconcile_enabled_does_not_raise_unbound(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-exc000000000de"
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: None)

    fake = mock.Mock()
    fake.execute = mock.Mock(side_effect=RuntimeError("boom-create-order"))
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    result = wr.execute_if_enabled(_armed_record(cl))  # was: UnboundLocalError

    assert result["ok"] is False
    assert result["mode"] == "submit_exception"
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]
