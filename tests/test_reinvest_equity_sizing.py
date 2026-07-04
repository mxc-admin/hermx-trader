"""Reinvest equity sizing — the money-path twin of Phase 5's dynamic budget.

``capital.reinvest`` (schema default True, also when the key is ABSENT) makes
``build_strategy_execution_readiness`` size off equity = seed ``budget_usd`` +
durable realized net P&L from the closed-trade ledger, scoped to the strategy's
account mode (demo|live) and its accounting window — the exact "Effective
budget" the dashboard card shows. ``reinvest: false`` pins sizing to the fixed
seed. Equity <= 0 arms the ExecutionService ``equity_stop`` gate, which blocks
NEW OPENS only — a ``close_only`` record always passes (never block a close).

Offline: the ledger resolves under HERMX_ROOT (the populated temp root the
``wr`` fixture binds), so seeding ``closed-trades.jsonl`` there exercises the
real ``net_realized_for_strategy`` read path with no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from conftest import adapter_result, fake_executor


# ===========================================================================
# Helpers
# ===========================================================================

def _ledger_row(strategy_id, *, ord_id, mode, net, closed_at_ms):
    return {
        "schema_version": 2, "exchange": "okx", "inst_id": "BTC-USDT-SWAP",
        "ord_id": ord_id, "mode": mode, "strategy_id": strategy_id, "side": "sell",
        "pnl_gross": net, "fee_cost": 0.0, "net_realized_pnl": net,
        "closed_at_ms": closed_at_ms,
    }


def _seed_ledger(root: Path, rows):
    (root / "closed-trades.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _record(strategy_id: str, *, capital: dict, execution_mode: str = "demo") -> dict:
    """A minimal but realistic record for build_strategy_execution_readiness.

    tv_time/strategy_id vary per-test so the derived client_order_id is unique."""
    return {
        "strategy_id": strategy_id,
        "strategy_config": {
            "strategy_id": strategy_id,
            "name": "Reinvest Test Strategy",
            "asset": "BTCUSDT",
            "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
            "timeframe": "2h",
            "execution_mode": execution_mode,
            "submit_orders": True,
            "capital": capital,
            "leverage": 2,
            "margin_mode": "isolated",
        },
        "normalized": {
            "strategy_id": strategy_id,
            "symbol": "BTCUSDT",
            "side": "buy",
            "timeframe": "2h",
            "tv_time": f"2026-07-04T00:00:00Z|{strategy_id}",
            "tv_signal_price": 50000.0,
        },
    }


def _armed_record(cl: str, *, equity_usd=None, include_equity=True, close_only=False) -> dict:
    """A minimal armed record for ExecutionService.execute via execute_if_enabled.

    ``equity_usd`` sets readiness.equity_usd; include_equity=False omits the key
    entirely (fixed-sizing / failed-ledger-read shape, the fail-safe pass path)."""
    readiness = {
        "live_execution_enabled": True,
        "symbol": "XRPUSDT",
        "signal_side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "strategy_id": "strat-eq",
        "execution_intent": {
            "policy": "weighted_v1",
            "client_order_id": cl,
            "actions": ["CLOSE_SHORT", "OPEN_LONG"] if not close_only else ["CLOSE_LONG"],
            "planned_notional_usd": 0.0 if (equity_usd is not None and equity_usd <= 0) else 1500.0,
        },
        "okx_fill": {"client_order_id": cl},
        "block_reason": None,
    }
    if include_equity:
        readiness["equity_usd"] = equity_usd
    if close_only:
        readiness["close_only"] = True
    return {
        "received_at": "2026-07-04T00:00:00Z",
        "auth_healthy": True,
        "execution_readiness": readiness,
    }


def _fake_ok():
    return fake_executor(adapter_result(client_order_id="cid", payload={"symbol": "XRP/USDT:USDT"}))


# ===========================================================================
# Readiness sizing — equity compounding
# ===========================================================================

def test_compounded_sizing_with_ledger_history(wr, wr_root):
    sid = "reinvest-compound"
    _seed_ledger(wr_root, [
        _ledger_row(sid, ord_id="1", mode="demo", net=150.0, closed_at_ms=100),
        _ledger_row(sid, ord_id="2", mode="demo", net=100.0, closed_at_ms=200),
    ])
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True})
    )
    # equity 1500 + 250 = 1750; notional = 1750 * leverage 2.
    assert rd["equity_usd"] == 1750.0
    assert rd["reinvest"] is True
    assert rd["budget_seed_usd"] == 1500.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3500.0
    assert rd["target_notional_usd"] == rd["execution_intent"]["planned_notional_usd"]


def test_reinvest_absent_defaults_to_compounding(wr, wr_root):
    sid = "reinvest-absent"
    _seed_ledger(wr_root, [_ledger_row(sid, ord_id="1", mode="demo", net=250.0, closed_at_ms=100)])
    rd = wr.build_strategy_execution_readiness(_record(sid, capital={"budget_usd": 1500}))
    # Key absent -> schema default True -> compounding applies.
    assert rd["reinvest"] is True
    assert rd["equity_usd"] == 1750.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3500.0


def test_reinvest_false_uses_fixed_seed_sizing(wr, wr_root):
    sid = "reinvest-off"
    _seed_ledger(wr_root, [_ledger_row(sid, ord_id="1", mode="demo", net=250.0, closed_at_ms=100)])
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": False})
    )
    # Fixed sizing: ledger history is ignored and no equity is resolved (gate unarmed).
    assert rd["reinvest"] is False
    assert rd["equity_usd"] is None
    assert rd["execution_intent"]["planned_notional_usd"] == 3000.0


def test_empty_ledger_equity_equals_seed(wr, wr_root):
    rd = wr.build_strategy_execution_readiness(
        _record("reinvest-first-order", capital={"budget_usd": 1500, "reinvest": True})
    )
    # First-ever order: no ledger -> realized 0 -> equity == seed, sizing unchanged.
    assert rd["equity_usd"] == 1500.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3000.0


def test_negative_equity_clamps_notional_to_zero(wr, wr_root):
    sid = "reinvest-depleted"
    _seed_ledger(wr_root, [_ledger_row(sid, ord_id="1", mode="demo", net=-2000.0, closed_at_ms=100)])
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True})
    )
    # Depleted equity is reported as-is (arms the gate) but never a negative notional.
    assert rd["equity_usd"] == -500.0
    assert rd["execution_intent"]["planned_notional_usd"] == 0.0


def test_ledger_read_failure_falls_back_to_seed(wr, wr_root, monkeypatch):
    sid = "reinvest-ledger-broken"
    _seed_ledger(wr_root, [_ledger_row(sid, ord_id="1", mode="demo", net=250.0, closed_at_ms=100)])

    def _boom(*args, **kwargs):
        raise OSError("ledger unreadable")

    monkeypatch.setattr("strategy.readiness.net_realized_for_strategy", _boom)
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True})
    )
    # Fail-safe: seed sizing, equity_usd=None so the equity_stop gate cannot fire.
    assert rd["equity_usd"] is None
    assert rd["execution_intent"]["planned_notional_usd"] == 3000.0


# ===========================================================================
# Mode isolation — demo and live ledgers never leak into each other
# ===========================================================================

def test_demo_strategy_ignores_live_ledger_rows(wr, wr_root):
    sid = "reinvest-mode-demo"
    _seed_ledger(wr_root, [
        _ledger_row(sid, ord_id="d", mode="demo", net=10.0, closed_at_ms=100),
        _ledger_row(sid, ord_id="l", mode="live", net=999.0, closed_at_ms=200),
    ])
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True}, execution_mode="demo")
    )
    # Only the demo close counts: 1500 + 10, never + 999.
    assert rd["equity_usd"] == 1510.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3020.0


def test_live_strategy_ignores_demo_ledger_rows(wr, wr_root):
    sid = "reinvest-mode-live"
    _seed_ledger(wr_root, [
        _ledger_row(sid, ord_id="d", mode="demo", net=999.0, closed_at_ms=100),
        _ledger_row(sid, ord_id="l", mode="live", net=40.0, closed_at_ms=200),
    ])
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True}, execution_mode="live")
    )
    # Only the live close counts: 1500 + 40, never + 999.
    assert rd["equity_usd"] == 1540.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3080.0


# ===========================================================================
# Accounting window — pre-reset history is locked out of equity
# ===========================================================================

def test_accounting_window_scopes_equity(wr, wr_root):
    sid = "reinvest-window"
    _seed_ledger(wr_root, [
        _ledger_row(sid, ord_id="old", mode="demo", net=500.0, closed_at_ms=100),
        _ledger_row(sid, ord_id="new", mode="demo", net=25.0, closed_at_ms=300),
    ])
    import control_state
    assert control_state.set_accounting_start(sid, 200) is True
    rd = wr.build_strategy_execution_readiness(
        _record(sid, capital={"budget_usd": 1500, "reinvest": True})
    )
    # Window starts at 200 -> the 500.0 pre-reset close is locked out: 1500 + 25.
    assert rd["equity_usd"] == 1525.0
    assert rd["execution_intent"]["planned_notional_usd"] == 3050.0


# ===========================================================================
# equity_stop gate — blocks new opens, never closes, fail-safe on unknown
# ===========================================================================

def test_equity_stop_blocks_new_open(wr):
    cl = "reinvestequitystopblock000000001"
    rec = _armed_record(cl, equity_usd=-25.0)

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["gate"] == "equity_stop"
    assert out["reason"].startswith("equity_depleted:")
    # A blocked equity stop writes NO order-journal row (returns before write-ahead).
    assert wr.latest_order_record(cl) is None


def test_equity_stop_zero_equity_blocks(wr):
    cl = "reinvestequitystopzero0000000001"
    rec = _armed_record(cl, equity_usd=0.0)

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["gate"] == "equity_stop"


def test_equity_stop_never_blocks_close_only(wr):
    cl = "reinvestequitystopcloseok0000001"
    rec = _armed_record(cl, equity_usd=-25.0, close_only=True)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    # Never block a close: depleted equity must not trap an operator flatten.
    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"


def test_equity_stop_positive_equity_passes(wr):
    cl = "reinvestequitystoppass0000000001"
    rec = _armed_record(cl, equity_usd=1750.0)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"
    assert wr.latest_order_record(cl) is not None


def test_equity_stop_absent_equity_passes(wr):
    # Fixed sizing / failed ledger read -> no equity_usd key -> fail-safe pass.
    cl = "reinvestequitystopabsent00000001"
    rec = _armed_record(cl, include_equity=False)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_equity_stop_none_equity_passes(wr):
    # equity_usd explicitly None (readiness fail-safe shape) -> never block.
    cl = "reinvestequitystopnone0000000001"
    rec = _armed_record(cl, equity_usd=None)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"
