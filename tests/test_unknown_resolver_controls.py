"""Phase 1 task 6 + task 2 remainder tests.

Covers:
  - periodic UNKNOWN/SUBMITTED resolver convergence and timeout handling,
  - per-symbol pause artifact persistence and submit-path blocking,
  - concrete operator alert transport hooks for queue/auth/reconcile,
  - startup trailing-line quarantine sweep and durable append semantics.
"""
from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _norm_order(state, *, cl_ord_id="cl-1", ord_id="ord-1", inst_id="XRP-USDT-SWAP", acc=0.0, avg=None):
    return {
        "exchange": "okx_demo",
        "inst_id": inst_id,
        "ord_id": ord_id,
        "cl_ord_id": cl_ord_id,
        "state": state,
        "acc_fill_sz": acc,
        "avg_px": avg,
        "ord_type": "market",
        "side": "buy",
        "pos_side": "net",
        "ts": "2026-06-26T00:00:00Z",
        "raw": {},
    }


class StubExecutor:
    def __init__(self, *, order):
        self._order = order

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        return self._order

    def get_open_orders(self, inst_id=None):
        return []

    def get_order_history_archive(self, inst_id=None, limit=100):
        return []

    def get_positions(self, inst_id=None):
        return []



def _armed_config() -> dict:
    return {
        "execution": {"enabled": True, "submit_orders": True, "simulated_trading": True, "force_ipv4": True},
        "risk": {"allow_live_execution": True},
    }



def _armed_record(cl: str = "mxc-xrpusdt-buy-abc0123456789de") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "okx_inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }



def test_unknown_resolver_converges_to_terminal(wr):
    cl = "mxc-xrpusdt-buy-resolver-terminal"
    intent = {
        "symbol": "XRPUSDT",
        "side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "planned_notional_usd": 1500.0,
        "policy": "weighted_v1",
    }
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_UNKNOWN, intent=intent, prev_state=wr.ORDER_STATE_SUBMITTED)

    summary = wr.resolve_unknown_orders_once(executor=StubExecutor(order=_norm_order("filled", cl_ord_id=cl, acc=10.0)))

    assert summary["checked"] == 1
    assert summary["resolved"] == 1
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    assert records[-1]["state"] == wr.ORDER_STATE_FILLED
    assert records[-1]["detail"]["unknown_resolver"] is True



def test_unknown_resolver_timeout_pauses_symbol_and_alerts(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-resolver-timeout"
    intent = {
        "symbol": "XRPUSDT",
        "side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "planned_notional_usd": 1500.0,
        "policy": "weighted_v1",
    }
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_UNKNOWN, intent=intent, prev_state=wr.ORDER_STATE_SUBMITTED)

    monkeypatch.setattr(wr, "UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS", 1.0)
    summary = wr.resolve_unknown_orders_once(
        executor=StubExecutor(order=_norm_order("live", cl_ord_id=cl)),
        now_ts="2099-01-01T00:00:00Z",
    )

    assert summary["expired"] == 1
    control = wr.load_control_state()
    assert control["symbol_pauses"]["XRPUSDT"]["paused"] is True

    op_alerts = wr.read_jsonl_tolerant(wr.OPERATOR_ALERT_LEDGER)
    assert any(a["alert"] == wr.RECONCILE_ALERT_RESOLVER_TIMEOUT for a in op_alerts)



def test_symbol_pause_blocks_submit_path(wr, monkeypatch):
    wr.pause_symbol("XRPUSDT", "manual pause test")
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")

    with mock.patch.object(wr.subprocess, "run") as run_mock:
        result = wr.execute_okx_if_enabled(_armed_record())

    run_mock.assert_not_called()
    assert result["mode"] == "not_submitted"
    assert result["reason"] == "symbol_paused"



def test_queue_and_auth_alert_transport(wr, monkeypatch):
    monkeypatch.setattr(wr, "QUEUE_SATURATION_ALERT_DEPTH", 2)
    assert wr.maybe_emit_queue_saturation_alert(3) is True
    wr.emit_auth_failure_alert("/webhook", "127.0.0.1")

    alerts = wr.read_jsonl_tolerant(wr.OPERATOR_ALERT_LEDGER)
    kinds = [a["alert"] for a in alerts]
    assert wr.ALERT_QUEUE_SATURATION in kinds
    assert wr.ALERT_AUTH_FAILURE in kinds



def test_startup_quarantine_partial_ledgers(wr):
    path = wr.LOG_DIR / "custom-ledger.jsonl"
    path.write_text('{"ok":true}\n{"bad":', encoding="utf-8")

    summary = wr.startup_quarantine_partial_ledgers([path])

    assert summary["checked"] == 1
    assert path.name in summary["quarantined"]
    assert (wr.LOG_DIR / "custom-ledger.jsonl.corrupt").exists()



def test_append_jsonl_uses_fsync(wr, monkeypatch):
    calls = []

    def fake_fsync(_fd):
        calls.append(1)

    monkeypatch.setattr(wr.os, "fsync", fake_fsync)
    wr.append_jsonl(wr.LOG_DIR / "durable-ledger.jsonl", {"k": "v"})
    assert calls
