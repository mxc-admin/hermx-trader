from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _armed_config() -> dict:
    # Phase A: no config arming flags -- the per-strategy submit flag arms paper submission.
    return {"execution": {"exchange": "ccxt"}}


def _armed_record(cl: str = "cid") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "auth_healthy": True,
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {
                "policy": "weighted_v1",
                "planned_notional_usd": 1500.0,
                "client_order_id": cl,
                "actions": ["OPEN_LONG"],
            },
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }


def test_submit_timeout_maps_to_unknown(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setattr(wr, "SECRET", "phase3-secret")
    monkeypatch.setattr(wr, "HERMX_SUBMIT_TIMEOUT_SECONDS", 1.0)

    cl = "mxcstabletimeoutid0000000000000001"
    # The CCXT adapter maps a ccxt client timeout to mode "submit_timeout"; the
    # service maps that to a first-class UNKNOWN outcome (invariant 5).
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value={
        "ok": False, "mode": "submit_timeout", "exchange": "ccxt", "elapsed_ms": 1000,
        "fill_summary": {"status": "submit_timeout", "order_id": None, "client_order_id": cl},
        "payload": {"error": "RequestTimeout"},
    })
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)
    out = wr.execute_if_enabled(_armed_record(cl))

    assert out["mode"] == "submit_timeout"
    # UNKNOWN, not a failure: the order journal records UNKNOWN and the order stays
    # open for reconciliation.
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states[-1] == wr.ORDER_STATE_UNKNOWN


def test_watchdog_pause_blocks_submission(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setattr(wr, "SECRET", "phase3-secret")
    wr._set_watchdog_submission_paused(True, "watchdog_degraded")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(_armed_record("mxcstablewatchdogid000000000000001"))

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert "watchdog" in str(out.get("reason") or "").lower()


def test_watchdog_loop_enters_degraded_state(monkeypatch):
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_STALE_SECONDS", 1.0)
    monkeypatch.setattr(wr, "HERMX_QUEUE_LAG_SLO_SECONDS", 9999.0)
    monkeypatch.setattr(wr, "_WORKER_NAMES", ["shadow-policy-worker-1"])
    monkeypatch.setattr(wr, "_WORKER_HEARTBEATS", {"shadow-policy-worker-1": 0.0})
    monkeypatch.setattr(wr, "_RESOLVER_HEARTBEAT", None)
    wr._set_watchdog_submission_paused(False, "")

    def _stop(_seconds):
        raise RuntimeError("stop-loop")

    try:
        wr.liveness_watchdog_loop(stop_event=None, sleep=_stop)
    except RuntimeError as exc:
        assert str(exc) == "stop-loop"

    ok, reason = wr._watchdog_submission_state()
    assert ok is False
    assert reason == "watchdog_degraded"


def test_watchdog_queue_lag_triggers_pause(monkeypatch):
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_STALE_SECONDS", 9999.0)
    monkeypatch.setattr(wr, "HERMX_QUEUE_LAG_SLO_SECONDS", 1.0)
    monkeypatch.setattr(wr, "_WORKER_NAMES", ["shadow-policy-worker-1"])
    monkeypatch.setattr(wr, "_WORKER_HEARTBEATS", {"shadow-policy-worker-1": wr.time.time()})
    monkeypatch.setattr(wr, "_queue_oldest_age_seconds", lambda: 5.0)
    wr._set_watchdog_submission_paused(False, "")

    def _stop(_seconds):
        raise RuntimeError("stop-loop")

    try:
        wr.liveness_watchdog_loop(stop_event=None, sleep=_stop)
    except RuntimeError:
        pass

    ok, reason = wr._watchdog_submission_state()
    assert ok is False
    assert reason == "watchdog_degraded"


def test_watchdog_recovery_clears_pause(monkeypatch):
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(wr, "HERMX_WATCHDOG_STALE_SECONDS", 9999.0)
    monkeypatch.setattr(wr, "HERMX_QUEUE_LAG_SLO_SECONDS", 9999.0)
    monkeypatch.setattr(wr, "_WORKER_NAMES", ["shadow-policy-worker-1"])
    monkeypatch.setattr(wr, "_WORKER_HEARTBEATS", {"shadow-policy-worker-1": wr.time.time()})
    monkeypatch.setattr(wr, "_queue_oldest_age_seconds", lambda: 0.0)
    wr._set_watchdog_submission_paused(True, "watchdog_degraded")

    def _stop(_seconds):
        raise RuntimeError("stop-loop")

    try:
        wr.liveness_watchdog_loop(stop_event=None, sleep=_stop)
    except RuntimeError:
        pass

    ok, reason = wr._watchdog_submission_state()
    assert ok is True
    assert reason == ""
