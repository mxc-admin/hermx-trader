"""Phase 4 — Strategy / Portfolio P&L API contracts.

Verifies the dashboard exposes durable, ledger-backed P&L in the shape the React UI
consumes: a per-strategy ``strategy_pnl`` object and a top-level ``portfolio`` roll-up.

Offline and deterministic. The ledger resolves its path from HERMX_ROOT (the temp
root the ``dash`` fixture binds), so seeding ``closed-trades.jsonl`` under that root
is all it takes to exercise the real read/aggregate path — no network, no real OKX.
"""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


STRATEGY_TEMPLATE = {
    "schema_version": 2,
    "name": "Test Strategy",
    "asset": "BTCUSDT",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "timeframe": "2h",
    "budget_usd": 1000,
    "leverage": 2,
    "margin_mode": "isolated",
    "execution_mode": "demo",
    "submit_orders": True,
    "status": "active_demo",
}


def _write_strategy(strategies_dir: Path, strategy_id: str, **overrides) -> Path:
    row = dict(STRATEGY_TEMPLATE)
    row["strategy_id"] = strategy_id
    row.update(overrides)
    path = strategies_dir / f"{strategy_id}.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    return path


def _ledger_row(strategy_id, *, ord_id, mode="demo", gross, fee, closed_at_ms,
                inst_id="BTC-USDT-SWAP", exchange="okx"):
    return {
        "schema_version": 2,
        "exchange": exchange,
        "inst_id": inst_id,
        "ord_id": ord_id,
        "mode": mode,
        "strategy_id": strategy_id,
        "side": "sell",
        "pnl_gross": gross,
        "fee_cost": fee,
        "net_realized_pnl": gross + fee,
        "closed_at_ms": closed_at_ms,
    }


def _seed_ledger(root: Path, rows):
    path = root / "closed-trades.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """dashboard reloaded against a fresh temp HERMX_ROOT (also the ledger dir)."""
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "strategies").mkdir(parents=True, exist_ok=True)

    orig_root = os.environ.get("HERMX_ROOT")
    os.environ["HERMX_ROOT"] = str(root)

    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)

    def _fresh_okx_live(config):
        return {
            "ok": True, "positions": {}, "account": {}, "error": None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    dash_mod.okx_live_snapshot = _fresh_okx_live
    monkeypatch.setattr(dash_mod, "okx_order_history_snapshot", lambda config: {"ok": False})
    # Per-(venue,mode) strategy snapshots (dashboard/model.py:404,407) build a real
    # executor and hit the venue over HTTP whenever a strategy file exists on disk.
    # Stub them offline like okx_order_history_snapshot above.
    monkeypatch.setattr(
        dash_mod,
        "strategy_live_snapshot",
        lambda strategy_config, mode: {
            "ok": True, "positions": {}, "account": {}, "error": None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "venue": dash_mod._strategy_venue(strategy_config),
            "mode": "live" if str(mode or "").lower() == "live" else "demo",
            "simulated_trading": str(mode or "").lower() != "live",
        },
    )
    monkeypatch.setattr(
        dash_mod,
        "strategy_order_history_snapshot",
        lambda strategy_config, mode: {"ok": False, "rows": []},
    )
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None

    try:
        yield dash_mod, core, root
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root
        else:
            os.environ.pop("HERMX_ROOT", None)


def _bust(dash_mod):
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None


# --- _strategy_pnl_contract: shape + ledger sums ---------------------------

def test_strategy_pnl_contract_has_phase4_keys(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1")
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="1", gross=10.0, fee=-0.5, closed_at_ms=100),
        _ledger_row("s1", ord_id="2", gross=5.0, fee=-0.25, closed_at_ms=300),
    ])
    strategy = {"strategy_id": "s1", "asset": "BTCUSDT", "execution_mode": "demo",
                "budget_usd": 1000, "instrument": {"exchange": "okx"}}
    pnl = dash_mod._strategy_pnl_contract(strategy, None, {}, {})

    # Phase-4 contract keys present and correctly derived from the ledger.
    assert pnl["strategy_id"] == "s1"
    assert pnl["venue"] == "okx"
    assert pnl["mode"] == "demo"
    assert pnl["realized_gross"] == pytest.approx(15.0)
    assert pnl["fees"] == pytest.approx(-0.75)
    assert pnl["realized_net"] == pytest.approx(14.25)
    assert pnl["upl"] == pytest.approx(0.0)
    assert pnl["total_net"] == pytest.approx(14.25)
    assert pnl["trade_count"] == 2
    assert pnl["last_close_at_ms"] == 300
    assert pnl["accounting_start_at"] is None
    # Phase-3 aliases still present (back-compat).
    assert pnl["closed_net_pnl_usd"] == pytest.approx(14.25)
    assert pnl["closed_order_count"] == 2


def test_strategy_pnl_contract_total_net_includes_upl(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [_ledger_row("s1", ord_id="1", gross=10.0, fee=0.0, closed_at_ms=100)])
    strategy = {"strategy_id": "s1", "asset": "BTCUSDT", "execution_mode": "demo",
                "budget_usd": 1000, "instrument": {"exchange": "okx"}}
    by_env = {"okx:demo": {"positions": {"BTCUSDT": {"upl": 3.5}}}}
    pnl = dash_mod._strategy_pnl_contract(strategy, None, by_env, {})
    assert pnl["realized_net"] == pytest.approx(10.0)
    assert pnl["upl"] == pytest.approx(3.5)
    assert pnl["total_net"] == pytest.approx(13.5)


def test_strategy_pnl_contract_window_filters_old_rows(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="old", gross=100.0, fee=0.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="new", gross=7.0, fee=0.0, closed_at_ms=300),
    ])
    strategy = {"strategy_id": "s1", "asset": "BTCUSDT", "execution_mode": "demo",
                "budget_usd": 1000, "instrument": {"exchange": "okx"}}
    pnl = dash_mod._strategy_pnl_contract(strategy, 200, {}, {})
    # Only the post-window close counts; the pre-reset 100.0 is locked out.
    assert pnl["realized_net"] == pytest.approx(7.0)
    assert pnl["trade_count"] == 1
    assert pnl["last_close_at_ms"] == 300
    assert pnl["accounting_start_at"] == 200


def test_strategy_pnl_contract_mode_scopes_rows(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="d", mode="demo", gross=4.0, fee=0.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="l", mode="live", gross=99.0, fee=0.0, closed_at_ms=200),
    ])
    strategy = {"strategy_id": "s1", "asset": "BTCUSDT", "execution_mode": "demo",
                "budget_usd": 1000, "instrument": {"exchange": "okx"}}
    pnl = dash_mod._strategy_pnl_contract(strategy, None, {}, {})
    assert pnl["mode"] == "demo"
    assert pnl["realized_net"] == pytest.approx(4.0)  # live row excluded
    assert pnl["trade_count"] == 1


def test_strategy_pnl_contract_absent_ledger_is_zero(dash):
    dash_mod, _core, root = dash
    strategy = {"strategy_id": "ghost", "asset": "BTCUSDT", "execution_mode": "demo",
                "budget_usd": 500, "instrument": {"exchange": "okx"}}
    pnl = dash_mod._strategy_pnl_contract(strategy, None, {}, {})
    assert pnl["realized_net"] == 0.0
    assert pnl["trade_count"] == 0
    assert pnl["last_close_at_ms"] is None
    assert pnl["total_net"] == 0.0


# --- portfolio_contract: aggregation ---------------------------------------

def test_portfolio_contract_aggregates(dash):
    dash_mod, _core, _root = dash
    pnls = [
        {"realized_net": 10.0, "realized_gross": 11.0, "fees": -1.0, "upl": 2.0,
         "trade_count": 3},
        {"realized_net": 5.0, "realized_gross": 5.0, "fees": 0.0, "upl": -1.0,
         "trade_count": 2},
        {"realized_net": 0.0, "realized_gross": 0.0, "fees": 0.0, "upl": 0.0,
         "trade_count": 0},  # untouched strategy: not counted
    ]
    port = dash_mod.portfolio_contract(pnls)
    assert port["realized_net"] == pytest.approx(15.0)
    assert port["realized_gross"] == pytest.approx(16.0)
    assert port["fees"] == pytest.approx(-1.0)
    assert port["upl"] == pytest.approx(1.0)
    assert port["total_net"] == pytest.approx(16.0)
    assert port["trade_count"] == 5
    assert port["strategies"] == 2  # only the two with data


def test_portfolio_contract_counts_upl_only_strategy(dash):
    dash_mod, _core, _root = dash
    # A strategy with an open position but no closes still counts as active.
    port = dash_mod.portfolio_contract([
        {"realized_net": 0.0, "trade_count": 0, "upl": 4.0},
    ])
    assert port["strategies"] == 1
    assert port["upl"] == pytest.approx(4.0)


def test_portfolio_contract_empty(dash):
    dash_mod, _core, _root = dash
    port = dash_mod.portfolio_contract([])
    assert port == {
        "realized_net": 0.0, "realized_gross": 0.0, "fees": 0.0, "upl": 0.0,
        "total_net": 0.0, "trade_count": 0, "strategies": 0,
        "unattributed": {"count": 0, "net_realized_pnl": 0.0, "mode": "all"},
    }


def test_portfolio_contract_discloses_unattributed_rows(dash):
    dash_mod, _core, root = dash
    # Pre-attribution history: reconciled rows carry strategy_id=None, so they are
    # invisible to every per-strategy sum — the portfolio must disclose them.
    _seed_ledger(root, [
        _ledger_row(None, ord_id="u1", gross=10.0, fee=-1.0, closed_at_ms=100),
        _ledger_row(None, ord_id="u2", gross=-3.0, fee=-0.5, closed_at_ms=200),
        _ledger_row("s1", ord_id="a1", gross=5.0, fee=-0.5, closed_at_ms=300),
    ])
    port = dash_mod.portfolio_contract([])
    unattr = port["unattributed"]
    assert unattr["count"] == 2
    assert unattr["net_realized_pnl"] == pytest.approx(10.0 - 1.0 - 3.0 - 0.5)
    assert unattr["mode"] == "all"


# --- api_payload integration -----------------------------------------------

def test_api_payload_includes_portfolio_and_strategy_pnl(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1")
    _write_strategy(root / "strategies", "s2")
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="1", gross=10.0, fee=-0.5, closed_at_ms=100),
        _ledger_row("s2", ord_id="2", gross=20.0, fee=-1.0, closed_at_ms=200),
    ])
    _bust(dash_mod)
    payload = dash_mod.api_payload()

    assert "portfolio" in payload
    port = payload["portfolio"]
    assert port["realized_net"] == pytest.approx(10.0 - 0.5 + 20.0 - 1.0)
    assert port["trade_count"] == 2
    assert port["strategies"] == 2

    for s in payload["strategies"]:
        assert "strategy_pnl" in s
        pnl = s["strategy_pnl"]
        assert pnl["strategy_id"] == s["strategy_id"]
        assert "realized_net" in pnl and "total_net" in pnl and "trade_count" in pnl


# --- positions contract (Positions-First) ------------------------------------

def _leg_row(strategy_id, *, ord_id, leg_kind, side, qty, px, ts, mode="demo",
             gross=None, fee=None, net=None, inst_id="BTC-USDT-SWAP", exchange="okx"):
    return {
        "schema_version": 4, "leg_kind": leg_kind, "exchange": exchange,
        "inst_id": inst_id, "ord_id": ord_id, "mode": mode,
        "strategy_id": strategy_id, "side": side, "filled_qty": qty,
        "avg_px": px, "pnl_gross": gross, "fee_cost": fee,
        "fee_currency": "USDT", "net_realized_pnl": net, "closed_at_ms": ts,
    }


def test_api_payload_includes_positions(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1")
    _seed_ledger(root, [
        _leg_row("s1", ord_id="o1", leg_kind="open", side="buy", qty=1.0,
                 px=50000.0, ts=100),
        _leg_row("s1", ord_id="c1", leg_kind="close", side="sell", qty=1.0,
                 px=51000.0, ts=200, gross=10.0, fee=-0.5, net=9.5),
    ])
    _bust(dash_mod)
    payload = dash_mod.api_payload()

    assert "positions" in payload
    pos = payload["positions"]
    assert set(pos) == {"open", "closed", "drift"}
    assert pos["open"] == []  # stubbed env snapshots carry no live positions
    assert len(pos["closed"]) == 1
    closed = pos["closed"][0]
    assert closed["strategy_id"] == "s1"
    assert closed["realized_pnl_net"] == pytest.approx(9.5)
    assert closed["opened_at_ms"] == 100
    assert closed["closed_at_ms"] == 200
    # Identity: sum of closed position net == the strategy card's ledger figure.
    card = next(s for s in payload["strategies"] if s["strategy_id"] == "s1")
    assert closed["realized_pnl_net"] == pytest.approx(
        card["strategy_pnl"]["realized_net"]
    )
    assert isinstance(pos["drift"]["count"], int)


def test_positions_contract_joins_venue_open_row(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _leg_row("s1", ord_id="o1", leg_kind="open", side="buy", qty=1.0,
                 px=50000.0, ts=100),
    ])
    by_env = {"okx:demo": {"ok": True, "positions": {"BTCUSDT": {
        "inst_id": "BTC-USDT-SWAP", "pos": 1.0, "avg_px": 50100.0,
        "upl": 12.5, "mark_px": 50200.0, "notional_usd": 50200.0,
    }}}}
    pos = dash_mod.positions_contract([], by_env)
    assert len(pos["open"]) == 1
    row = pos["open"][0]
    assert row["qty"] == pytest.approx(1.0)  # venue is truth for qty
    assert row["upl"] == pytest.approx(12.5)
    assert row["strategy_id"] == "s1"  # enriched from the ledger open episode
    assert row["opened_at_ms"] == 100
    assert pos["drift"]["count"] == 0


def test_positions_contract_flags_drift_observe_only(dash):
    dash_mod, _core, root = dash
    # Ledger says open, venue (healthy read) says flat -> one drift row, no action.
    _seed_ledger(root, [
        _leg_row("s1", ord_id="o1", leg_kind="open", side="buy", qty=1.0,
                 px=50000.0, ts=100),
    ])
    by_env = {"okx:demo": {"ok": True, "positions": {}}}
    pos = dash_mod.positions_contract([], by_env)
    assert pos["drift"]["count"] == 1
    assert pos["drift"]["rows"][0]["kind"] == "ledger_open_venue_flat"


def test_positions_contract_skips_drift_for_unhealthy_env(dash):
    dash_mod, _core, root = dash
    # A dead executor read must NOT read as "venue flat" drift.
    _seed_ledger(root, [
        _leg_row("s1", ord_id="o1", leg_kind="open", side="buy", qty=1.0,
                 px=50000.0, ts=100),
    ])
    by_env = {"okx:demo": {"ok": False, "positions": {}}}
    pos = dash_mod.positions_contract([], by_env)
    assert pos["drift"]["count"] == 0
    assert pos["open"] == []


def test_positions_contract_absent_ledger_is_empty(dash):
    dash_mod, _core, _root = dash
    pos = dash_mod.positions_contract([], {})
    assert pos == {"open": [], "closed": [], "drift": {"count": 0, "rows": []}}
