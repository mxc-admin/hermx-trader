"""Phase 5 — Dynamic budget.

``effective_budget = budget_usd (seed, from strategy JSON) + closed_net_pnl (ledger)``.
The seed stays in the strategy file (Decision ⑤A: layer the dynamic part on top); the
realized layer is summed from the durable closed-trade ledger at render time, scoped to
the strategy's account mode and accounting window — never from the live position's
``realized_pnl`` (which resets to 0 on FLAT).

Offline: the ledger resolves under HERMX_ROOT (the temp root the ``dash`` fixture binds),
so seeding ``closed-trades.jsonl`` there exercises the real read path with no network.
"""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


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


@pytest.fixture
def dash(tmp_path):
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "strategies").mkdir(parents=True, exist_ok=True)
    orig_root = os.environ.get("HERMX_ROOT")
    os.environ["HERMX_ROOT"] = str(root)

    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)

    def _fresh(config):
        return {"ok": True, "positions": {}, "account": {}, "error": None,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    dash_mod.okx_live_snapshot = _fresh
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None
    try:
        yield dash_mod, core, root
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root
        else:
            os.environ.pop("HERMX_ROOT", None)


def _snapshots(upl_demo=0.0, upl_live=0.0):
    return {
        "demo": {"ok": True, "positions": {"BTCUSDT": {"side": "LONG", "upl": upl_demo, "last": 100.0}}},
        "live": {"ok": True, "positions": {"BTCUSDT": {"side": "SHORT", "upl": upl_live, "last": 200.0}}},
    }


def _strat(**overrides):
    base = {"strategy_id": "s1", "asset": "BTCUSDT", "effective_mode": "demo",
            "timeframe": "2h", "capital": {"budget_usd": 1000}, "instrument": {}}
    base.update(overrides)
    return base


# --- effective budget = seed + ledger realized -----------------------------

def test_effective_budget_is_seed_plus_ledger_net(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="1", mode="demo", net=15.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="2", mode="demo", net=10.0, closed_at_ms=200),
    ])
    html = dash_mod.strategy_card(_strat(), {}, [], _snapshots(upl_demo=5.0))
    # seed 1000 + realized 25 = effective 1025; equity adds upl 5 = 1030.
    assert "Seed budget" in html and "$1,000" in html
    assert "Realized P&amp;L" in html and "$25.00" in html
    assert "Effective budget" in html and "$1,025.00" in html
    assert "$1,030.00" in html  # total equity = seed + realized + upl


def test_effective_budget_absent_ledger_equals_seed(dash):
    dash_mod, _core, _root = dash
    html = dash_mod.strategy_card(_strat(), {}, [], _snapshots())
    # No ledger -> realized 0 -> effective budget == seed.
    assert "$1,000.00" in html  # effective budget = seed
    assert "$0.00" in html      # realized P&L zero


# --- mode scoping -----------------------------------------------------------

def test_dynamic_budget_mode_scopes_ledger(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="d", mode="demo", net=10.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="l", mode="live", net=99.0, closed_at_ms=200),
    ])
    # Demo strategy -> only the demo close counts: 1000 + 10 = 1010, NOT 1109.
    html = dash_mod.strategy_card(_strat(effective_mode="demo"), {}, [], _snapshots())
    assert "$1,010.00" in html
    assert "$1,099.00" not in html


def test_dynamic_budget_live_mode_reads_live_rows(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="d", mode="demo", net=10.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="l", mode="live", net=40.0, closed_at_ms=200),
    ])
    html = dash_mod.strategy_card(_strat(effective_mode="live"), {}, [], _snapshots())
    # Live strategy -> only the live close: 1000 + 40 = 1040.
    assert "$1,040.00" in html
    assert "$1,010.00" not in html


# --- accounting window scoping ---------------------------------------------

def test_dynamic_budget_window_filters_pre_reset(dash):
    dash_mod, _core, root = dash
    _seed_ledger(root, [
        _ledger_row("s1", ord_id="old", mode="demo", net=500.0, closed_at_ms=100),
        _ledger_row("s1", ord_id="new", mode="demo", net=7.0, closed_at_ms=300),
    ])
    # Accounting window starts at 200 -> the 500.0 pre-reset close is locked out.
    html = dash_mod.strategy_card(_strat(accounting_start_at=200), {}, [], _snapshots())
    assert "$1,007.00" in html
    assert "$1,507.00" not in html
