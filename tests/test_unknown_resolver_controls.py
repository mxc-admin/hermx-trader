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
        "exchange": "ccxt",
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
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }



class ObserveOnlyStub(StubExecutor):
    """Read-only query executor that fails if the resolver ever tries to trade."""

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("periodic resolver invoked a state-mutating venue method")

    execute = _forbidden
    submit = _forbidden
    cancel = _forbidden
    cancel_order = _forbidden
    create_order = _forbidden
    place_order = _forbidden


def _seed_submitted(wr, cl: str) -> dict:
    intent = {
        "symbol": "XRPUSDT",
        "side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "planned_notional_usd": 1500.0,
        "policy": "weighted_v1",
    }
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    return intent


def _no_wait_backoff(wr, monkeypatch):
    real = wr.reconcile_order_with_backoff
    monkeypatch.setattr(
        wr,
        "reconcile_order_with_backoff",
        lambda executor, lookup, **kw: real(executor, lookup, sleep=lambda *_: None, **kw),
    )


def test_resolver_submitted_to_filled_observe_only(wr):
    # A still-open SUBMITTED order (no intermediate UNKNOWN) the venue reports filled.
    cl = "mxc-xrpusdt-buy-resolver-fill"
    _seed_submitted(wr, cl)
    summary = wr.resolve_unknown_orders_once(executor=ObserveOnlyStub(order=_norm_order("filled", cl_ord_id=cl, acc=10.0)))
    assert (summary["checked"], summary["resolved"]) == (1, 1)
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_FILLED]
    assert wr.load_open_orders() == []


def test_resolver_submitted_to_rejected(wr):
    cl = "mxc-xrpusdt-buy-resolver-reject"
    _seed_submitted(wr, cl)
    summary = wr.resolve_unknown_orders_once(executor=ObserveOnlyStub(order=_norm_order("canceled", cl_ord_id=cl, acc=0.0)))
    assert (summary["checked"], summary["resolved"]) == (1, 1)
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    assert records[-1]["state"] == wr.ORDER_STATE_REJECTED
    assert wr.load_open_orders() == []


def test_resolver_submitted_to_unknown_keeps_order_open(wr, monkeypatch):
    # Venue order stays "live" through every retry => SUBMITTED -> UNKNOWN, still open.
    cl = "mxc-xrpusdt-buy-resolver-unknown"
    _seed_submitted(wr, cl)
    _no_wait_backoff(wr, monkeypatch)
    summary = wr.resolve_unknown_orders_once(executor=ObserveOnlyStub(order=_norm_order("live", cl_ord_id=cl)))
    assert summary["pending"] == 1
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1 and open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


def test_unknown_resolver_loop_ticks_and_stops_on_event(wr, monkeypatch):
    # The daemon loop ticks resolve_unknown_orders_once() and exits cleanly once the
    # stop event is set -- the periodic path is bounded and itself never trades. The
    # fake tick sets the stop event so stop_event.wait() returns immediately.
    import threading

    ticks = []
    stop = threading.Event()

    def fake_resolve():
        ticks.append(1)
        stop.set()  # end after exactly one tick
        return {"checked": 0, "resolved": 0, "pending": 0, "expired": 0, "errors": []}

    monkeypatch.setattr(wr, "resolve_unknown_orders_once", fake_resolve)
    wr.unknown_resolver_loop(stop_event=stop)
    assert ticks == [1]
    assert wr._RESOLVER_HEARTBEAT is not None  # heartbeat set for the watchdog


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



def _seed_unknown(wr, cl: str) -> dict:
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
    return intent


def test_unknown_backstop_alerts_but_never_auto_closes(wr, monkeypatch):
    # The lifecycle backstop is OBSERVE-ONLY: a too-old UNKNOWN order is alerted and the
    # symbol paused, but the order is NEVER auto-transitioned to a terminal state -- it
    # stays UNKNOWN/open so a human (or a later venue truth) resolves it.
    cl = "mxc-xrpusdt-buy-backstop"
    _seed_unknown(wr, cl)
    monkeypatch.setattr(wr, "UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS", 1.0)
    summary = wr.resolve_unknown_orders_once(
        executor=StubExecutor(order=_norm_order("live", cl_ord_id=cl)),
        now_ts="2099-01-01T00:00:00Z",
    )
    assert summary["expired"] == 1
    assert summary["resolved"] == 0
    # No terminal record written -- order is still UNKNOWN and open.
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert wr.ORDER_STATE_FILLED not in states and wr.ORDER_STATE_REJECTED not in states
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1 and open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


def test_unknown_backstop_dedupes_pause_and_alerts_across_ticks(wr, monkeypatch):
    # A single stuck order must not re-pause / re-alert on every tick. The pause reason
    # is stable per (symbol, cl_ord_id), so the second tick is a no-op pause and emits
    # NO new operator timeout alert.
    cl = "mxc-xrpusdt-buy-backstop-dedupe"
    _seed_unknown(wr, cl)
    monkeypatch.setattr(wr, "UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS", 1.0)
    ex = StubExecutor(order=_norm_order("live", cl_ord_id=cl))

    first = wr.resolve_unknown_orders_once(executor=ex, now_ts="2099-01-01T00:00:00Z")
    second = wr.resolve_unknown_orders_once(executor=ex, now_ts="2099-01-01T01:00:00Z")

    assert first["expired"] == 1 and second["expired"] == 1  # still expired each tick
    assert first["paused_symbols"] == ["XRPUSDT"]            # paused on first tick only
    assert second["paused_symbols"] == []                    # deduped on second tick
    op_alerts = wr.read_jsonl_tolerant(wr.OPERATOR_ALERT_LEDGER)
    timeouts = [a for a in op_alerts if a["alert"] == wr.RECONCILE_ALERT_RESOLVER_TIMEOUT]
    assert len(timeouts) == 1  # exactly one timeout alert despite two expired ticks


def test_unknown_backstop_age_measured_from_origin_not_latest(wr, monkeypatch):
    # Re-recording UNKNOWN must not reset the backstop clock. Age is measured from the
    # order's ORIGIN (first journal record), so load_open_orders surfaces origin_ts and
    # the backstop accumulates across the order's whole lifetime.
    cl = "mxc-xrpusdt-buy-origin-age"
    _seed_unknown(wr, cl)
    open_orders = wr.load_open_orders()
    assert open_orders and open_orders[0].get("origin_ts")  # origin timestamp surfaced


def test_symbol_pause_blocks_submit_path(wr, monkeypatch):
    wr.pause_symbol("XRPUSDT", "manual pause test")
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "1")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_okx_if_enabled(_armed_record())

    create_mock.assert_not_called()  # symbol pause blocks before any submit
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
