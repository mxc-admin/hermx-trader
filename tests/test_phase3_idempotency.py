from __future__ import annotations

import importlib
from unittest import mock

import webhook_receiver as wr


def test_stable_client_order_id_is_deterministic_and_alnum():
    identity = "duo_raw|XRPUSDT|buy|30m|2026-06-25T00:00:00Z|sig-1"
    a = wr.stable_client_order_id(identity, role="open")
    b = wr.stable_client_order_id(identity, role="open")
    c = wr.stable_client_order_id(identity, role="close")

    assert a == b
    assert a != c
    assert len(a) <= 32
    assert a.isalnum()


def test_durable_signal_dedupe_survives_reload(wr, wr_root):
    normalized = {
        "strategy_id": "duo_raw",
        "symbol": "XRPUSDT",
        "action": "buy",
        "timeframe": "30m",
        "tv_time": "2026-06-25T00:00:00Z",
        "signal_id": "sig-fixed-1",
    }

    dup_first, meta_first = wr.check_and_mark_signal(normalized, "2026-06-25T00:00:01Z")
    assert dup_first is False
    assert meta_first["first_seen_at"] == "2026-06-25T00:00:01Z"

    dup_second, _ = wr.check_and_mark_signal(normalized, "2026-06-25T00:00:02Z")
    assert dup_second is True

    reloaded = importlib.reload(wr)
    dup_after_reload, meta_after_reload = reloaded.check_and_mark_signal(normalized, "2026-06-25T00:00:03Z")
    assert dup_after_reload is True
    assert meta_after_reload["first_seen_at"] == "2026-06-25T00:00:01Z"


def test_execute_blocks_duplicate_cl_ord_id_from_order_journal(wr, monkeypatch):
    monkeypatch.setenv("HERMX_LIVE_TRADING", "1")
    monkeypatch.setattr(wr, "SECRET", "phase3-secret")

    cl = "mxcdup0000000000000000000000001"
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1000.0, "policy": "duo_raw"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_FILLED, intent=intent, prev_state=wr.ORDER_STATE_SUBMITTED)

    record = {
        "received_at": "2026-06-25T00:10:00Z",
        "auth_healthy": True,
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {
                "policy": "duo_raw",
                "planned_notional_usd": 1000.0,
                "client_order_id": cl,
                "actions": ["OPEN_LONG"],
            },
            "okx_fill": {"client_order_id": cl},
        },
    }

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(record)

    create_mock.assert_not_called()  # duplicate clOrdId blocks before any submit
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "duplicate_cl_ord_id"
    assert out["existing_state"] == wr.ORDER_STATE_FILLED
