"""Alert Outcome join (Phase B): strategy_id stamped on execution pipeline writes,
alert<->execution correlated by EXACT ``received_at`` string match.

Everything runs through production paths: intake via ``wr.build_record`` (which
builds readiness, routes through ExecutionService / the fail-closed fallback and
writes the pipeline rows), then the dashboard model's ``strategy_alert_rows``
reads the same pipeline.jsonl. The only mocked seam is ``ExecutorFactory.create``
(the single venue-submit boundary, same seam as test_execution_gate_precedence).

Go-forward contract: new execution rows carry ``strategy_id``; historical rows
without one still join on the received_at key alone; an unmatched alert fails
open to outcome=None (rendered as a dash, never "orphan").
"""
from __future__ import annotations

import importlib
import json
from unittest import mock

from conftest import adapter_result, fake_executor, load_alert

RECEIVED_AT = "2026-06-22T00:00:00.123456+00:00"
SID = "btcusdt_duo_base_dev_2h"


def _pipeline_rows(root, stage):
    path = root / "logs" / "pipeline.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("stage") == stage]


def _dashboard_for(root):
    """Reload dashboard bound to the wr fixture's HERMX_ROOT (already in env)."""
    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)
    return dash_mod


def _filled_executor():
    result = adapter_result(
        client_order_id="cid-1",
        payload={
            "symbol": "BTC/USDT:USDT",
            "inst_id": "BTC-USDT-SWAP",
            "target_direction": "long",
            "executed_orders": [
                {
                    "action": "OPEN_LONG",
                    "submitted": True,
                    "status": "filled",
                    "order": {"id": "o1", "clientOrderId": "cid-1", "status": "closed"},
                }
            ],
        },
    )
    result["fill_summary"]["status"] = "filled"
    return fake_executor(result)


def test_execution_row_stamps_strategy_id_and_preserves_received_at(wr, wr_root):
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=_filled_executor()):
        status, record = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert status == 200

    exec_rows = _pipeline_rows(wr_root, "execution")
    assert len(exec_rows) == 1
    row = exec_rows[0]
    # The intake received_at string passes through VERBATIM (join key), never a
    # new clock read.
    assert row["received_at"] == RECEIVED_AT
    assert row["strategy_id"] == SID


def test_blocked_fallback_row_stamps_strategy_id(wr, wr_root, monkeypatch):
    # Execution surface unavailable -> the fail-closed not_submitted row (written
    # by _execute_authoritative, not the service) must carry the same stamps.
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    status, _record = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert status == 200

    exec_rows = _pipeline_rows(wr_root, "execution")
    assert len(exec_rows) == 1
    assert exec_rows[0]["received_at"] == RECEIVED_AT
    assert exec_rows[0]["strategy_id"] == SID
    assert exec_rows[0]["okx_execution"]["mode"] == "not_submitted"


def test_alert_outcome_filled_via_received_at_join(wr, wr_root):
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=_filled_executor()):
        wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)

    dash_mod = _dashboard_for(wr_root)
    rows = [r for r in dash_mod.strategy_alert_rows() if r["strategy_id"] == SID]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FILLED"


def test_alert_outcome_blocked(wr, wr_root, monkeypatch):
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)

    dash_mod = _dashboard_for(wr_root)
    rows = [r for r in dash_mod.strategy_alert_rows() if r["strategy_id"] == SID]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "BLOCKED"


def test_alert_outcome_no_fill_when_submitted_without_fill(wr, wr_root):
    # Adapter acknowledged the submit but no order reached filled/closed.
    fake = fake_executor(adapter_result(client_order_id="cid-2", payload={
        "symbol": "BTC/USDT:USDT",
        "target_direction": "long",
        "executed_orders": [
            {"action": "OPEN_LONG", "submitted": True, "status": "submitted",
             "order": {"id": "o2", "clientOrderId": "cid-2", "status": "open"}}
        ],
    }))
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)

    dash_mod = _dashboard_for(wr_root)
    rows = [r for r in dash_mod.strategy_alert_rows() if r["strategy_id"] == SID]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "NO FILL"


def test_historical_alert_without_execution_row_fails_open(wr, wr_root):
    # A historical strategy_match row with no correlated execution row (predates
    # this feature, or the exec row rotated away) must yield outcome=None.
    historical = {
        "ts": "2025-01-01T00:00:00+00:00",
        "stage": "strategy_match",
        "signal_id": "hist-1",
        "received_at": "2025-01-01T00:00:00.000001+00:00",
        "normalized": {"strategy_id": SID, "symbol": "BTCUSDT", "action": "buy",
                       "timeframe": "2h", "tv_time": "2025-01-01T00:00:00Z"},
        "strategy_config": {"name": "Hist"},
    }
    with (wr_root / "logs" / "pipeline.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(historical) + "\n")

    dash_mod = _dashboard_for(wr_root)
    rows = [r for r in dash_mod.strategy_alert_rows() if r["strategy_id"] == SID]
    assert len(rows) == 1
    assert rows[0]["outcome"] is None


def test_stamped_strategy_id_mismatch_refuses_join(wr, wr_root):
    # Same received_at but the outcome row is stamped with a DIFFERENT strategy_id
    # (adversarial/corrupt row): the join must refuse rather than mislabel.
    shared_key = "2025-06-01T00:00:00.000002+00:00"
    alert = {
        "ts": shared_key, "stage": "strategy_match", "signal_id": "x1",
        "received_at": shared_key,
        "normalized": {"strategy_id": SID, "symbol": "BTCUSDT", "action": "buy",
                       "timeframe": "2h", "tv_time": "2025-06-01T00:00:00Z"},
        "strategy_config": {"name": "X"},
    }
    exec_row = {
        "ts": shared_key, "stage": "execution", "signal_id": "x1",
        "received_at": shared_key, "strategy_id": "some_other_strategy",
        "okx_execution": {"ok": True, "mode": "not_submitted", "reason": "gate"},
    }
    with (wr_root / "logs" / "pipeline.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(alert) + "\n")
        fh.write(json.dumps(exec_row) + "\n")

    dash_mod = _dashboard_for(wr_root)
    rows = [r for r in dash_mod.strategy_alert_rows() if r["strategy_id"] == SID]
    assert len(rows) == 1
    assert rows[0]["outcome"] is None
