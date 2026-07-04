"""Phase 0 — Demo/live account separation (PNL_MASTER_PLAN.md §Phase 0).

Verifies the dashboard reads the OKX account that matches each strategy's mode:
demo/pause -> sandbox (simulated_trading=True), live -> real venue
(simulated_trading=False, still gated by HERMX_LIVE_TRADING). Fixes issues #2/#3.

Offline and deterministic: no network, no real OKX. The executor seam
(``_dashboard_executor``) is stubbed with a recorder so ``okx_live_snapshot`` and
``dashboard_model`` run for real but never open a socket. ``CcxtExecutor`` builds
its client lazily, so the one test that constructs a real executor
(``_dashboard_executor``) also touches no network — it only inspects config.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


STRATEGY_TEMPLATE = {
    "schema_version": 2,
    "name": "Test Strategy",
    "asset": "BTCUSDT",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "timeframe": "2h",
    "chart_type": "heikin_ashi",
    "budget_usd": 1500,
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


class _FakeExecutor:
    """Offline stand-in for CcxtExecutor. health() never opens a socket; ok=False so
    okx_live_snapshot skips the mark_prices() network fetch."""

    def __init__(self, simulated_trading: bool) -> None:
        self.simulated_trading = simulated_trading

    def health(self) -> dict:
        return {"ok": False, "error": "stub_offline", "positions": []}


@pytest.fixture
def dash(tmp_path):
    """dashboard reloaded against a fresh temp HERMX_ROOT (mirrors test_phase4)."""
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "strategies").mkdir(parents=True, exist_ok=True)

    orig_hermx_root = os.environ.get("HERMX_ROOT")
    orig_live = os.environ.get("HERMX_LIVE_TRADING")
    # ROOT resolves solely from HERMX_ROOT (dashboard.py:38, dashboard_core.py:11,
    # webhook_receiver.py:131). Pin it to the temp root so a HERMX_ROOT already in
    # the environment can't win and break isolation.
    os.environ["HERMX_ROOT"] = str(root)
    os.environ.pop("HERMX_LIVE_TRADING", None)  # disarmed by default (fail-closed)

    import dashboard_core as core  # noqa: WPS433
    importlib.reload(core)
    import dashboard as dash_mod  # noqa: WPS433
    importlib.reload(dash_mod)

    try:
        yield dash_mod, core, root
    finally:
        if orig_hermx_root is not None:
            os.environ["HERMX_ROOT"] = orig_hermx_root
        else:
            os.environ.pop("HERMX_ROOT", None)
        if orig_live is not None:
            os.environ["HERMX_LIVE_TRADING"] = orig_live
        else:
            os.environ.pop("HERMX_LIVE_TRADING", None)
        importlib.reload(dash_mod)


def _bust_caches(dash_mod) -> None:
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None
    dash_mod._OKX_LIVE_CACHE.clear()


def _recorder(dash_mod, monkeypatch):
    """Stub _dashboard_executor with a recorder of the simulated_trading arg."""
    calls: list[bool] = []

    def fake(config, simulated_trading=True):
        calls.append(bool(simulated_trading))
        return _FakeExecutor(bool(simulated_trading)), None

    monkeypatch.setattr(dash_mod, "_dashboard_executor", fake)
    return calls


# ---------------------------------------------------------------------------
# _dashboard_executor: honors simulated_trading in the config it hands the adapter.
# ---------------------------------------------------------------------------

def test_dashboard_executor_respects_simulated_trading(dash):
    dash_mod, _core, _root = dash

    ex_demo, err_demo = dash_mod._dashboard_executor({})
    assert err_demo is None, err_demo
    assert ex_demo.execution_cfg["simulated_trading"] is True
    # Venue still pinned to OKX (the shadow-config-removal default), not the "ccxt"
    # backend name.
    assert ex_demo.execution_cfg["ccxt_exchange"] == "okx"

    ex_live, err_live = dash_mod._dashboard_executor({}, simulated_trading=False)
    assert err_live is None, err_live
    assert ex_live.execution_cfg["simulated_trading"] is False


# ---------------------------------------------------------------------------
# okx_live_snapshot: per-mode cache keys + fail-closed live read.
# ---------------------------------------------------------------------------

def test_live_snapshot_uses_sandbox_when_no_live_strategies(dash, monkeypatch):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1", execution_mode="demo")
    calls = _recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    model = dash_mod.dashboard_model()

    # No live executor was ever built (calls also include the order-history snapshot,
    # which reads demo); the invariant is that nothing connected to the live venue.
    assert False not in calls
    by_mode = model["okx_live_by_mode"]
    assert by_mode["demo"]["mode"] == "demo"
    assert by_mode["demo"]["simulated_trading"] is True
    # With no live strategy the live slot reuses the demo snapshot object (identity).
    assert by_mode["live"] is by_mode["demo"]


def test_live_snapshot_fetches_live_when_strategy_live(dash, monkeypatch):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1", execution_mode="live", submit_orders=True)
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")  # armed -> live read proceeds
    calls = _recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    model = dash_mod.dashboard_model()

    # Both accounts fetched: demo (True) and live (False).
    assert True in calls and False in calls
    by_mode = model["okx_live_by_mode"]
    assert by_mode["demo"]["simulated_trading"] is True
    assert by_mode["live"]["mode"] == "live"
    assert by_mode["live"]["simulated_trading"] is False


def test_live_snapshot_fail_closed_when_not_armed(dash, monkeypatch, capsys):
    dash_mod, _core, _root = dash
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)  # disarmed
    calls = _recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    snap = dash_mod.okx_live_snapshot({}, simulated_trading=False)

    # Fell back to the demo account; never built a live executor.
    assert snap["mode"] == "demo"
    assert snap["simulated_trading"] is True
    assert calls == [True]
    # A warning was logged to stderr (never silently reads the wrong account).
    assert "HERMX_LIVE_TRADING" in capsys.readouterr().err


def test_live_snapshot_separate_cache_keys(dash, monkeypatch):
    """Demo and live snapshots do not share a cache slot."""
    dash_mod, _core, _root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    _recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    demo = dash_mod.okx_live_snapshot({}, simulated_trading=True)
    live = dash_mod.okx_live_snapshot({}, simulated_trading=False)

    assert demo["mode"] == "demo"
    assert live["mode"] == "live"
    assert "snapshot:demo" in dash_mod._OKX_LIVE_CACHE
    assert "snapshot:live" in dash_mod._OKX_LIVE_CACHE


# ---------------------------------------------------------------------------
# strategy_card: picks the snapshot matching the strategy's effective mode.
# ---------------------------------------------------------------------------

def _by_mode_snapshots():
    return {
        "demo": {
            "ok": True,
            "positions": {"BTCUSDT": {"side": "LONG", "upl": 10.0, "realized_pnl": 5.0, "last": 100.0}},
        },
        "live": {
            "ok": True,
            "positions": {"BTCUSDT": {"side": "SHORT", "upl": -99.0, "realized_pnl": -50.0, "last": 200.0}},
        },
    }


def test_strategy_card_picks_demo_for_demo_mode(dash):
    dash_mod, _core, _root = dash
    strat = {
        "asset": "BTCUSDT", "effective_mode": "demo", "timeframe": "2h",
        "capital": {"budget_usd": 1000}, "instrument": {},
    }
    html = dash_mod.strategy_card(strat, {}, [], _by_mode_snapshots())
    # Demo LONG position + demo mark price 100, not the live SHORT / 200.
    assert "LONG" in html
    assert "100.0000" in html
    assert "200.0000" not in html


def test_strategy_card_picks_live_for_live_mode(dash):
    dash_mod, _core, _root = dash
    strat = {
        "asset": "BTCUSDT", "effective_mode": "live", "timeframe": "2h",
        "capital": {"budget_usd": 1000}, "instrument": {},
    }
    html = dash_mod.strategy_card(strat, {}, [], _by_mode_snapshots())
    # Live SHORT position + live mark price 200, not the demo LONG / 100.
    assert "SHORT" in html
    assert "200.0000" in html
    assert "100.0000" not in html


def test_control_state_override_changes_snapshot_source(dash):
    """Toggling a demo strategy to live (via control-state override) flips which
    account snapshot strategy_card reads from."""
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1", execution_mode="demo")
    by_mode = _by_mode_snapshots()
    strat = {"strategy_id": "s1", "execution_mode": "demo"}

    # No override: effective mode demo -> demo snapshot.
    mode_before = dash_mod._effective_strategy_mode(strat, {})
    assert mode_before == "demo"
    assert dash_mod._snapshot_for_mode(by_mode, mode_before) is by_mode["demo"]

    # Override to live: effective mode live -> live snapshot.
    assert dash_mod._set_strategy_override("s1", "live") is True
    overrides = dash_mod._load_control_state().get("strategy_overrides") or {}
    mode_after = dash_mod._effective_strategy_mode(strat, overrides)
    assert mode_after == "live"
    assert dash_mod._snapshot_for_mode(by_mode, mode_after) is by_mode["live"]


# ---------------------------------------------------------------------------
# Phase 0.5 — per-strategy (venue, mode) environment resolution.
# Each strategy is independent: its venue comes from its own instrument block and
# its account (demo/live) from its effective mode. No cross-contamination.
# ---------------------------------------------------------------------------

def _env_recorder(dash_mod, monkeypatch):
    """Stub _dashboard_executor recording (venue, simulated_trading) per build."""
    calls: list[tuple] = []

    def fake(config, simulated_trading=True):
        venue = ((config or {}).get("execution") or {}).get("ccxt_exchange")
        calls.append((venue, bool(simulated_trading)))
        return _FakeExecutor(bool(simulated_trading)), None

    monkeypatch.setattr(dash_mod, "_dashboard_executor", fake)
    return calls


def test_strategy_venue_resolution(dash):
    dash_mod, _core, _root = dash
    # Missing venue -> okx default (legacy behavior preserved).
    assert dash_mod._strategy_venue({}) == "okx"
    # Case-insensitive, from instrument.exchange.
    assert dash_mod._strategy_venue({"instrument": {"exchange": "KuCoin"}}) == "kucoin"
    # execution.ccxt_exchange overrides instrument.exchange.
    assert dash_mod._strategy_venue(
        {"execution": {"ccxt_exchange": "bybit"}, "instrument": {"exchange": "okx"}}
    ) == "bybit"
    # "ccxt" is a backend name, not a venue -> falls back to okx.
    assert dash_mod._strategy_venue({"instrument": {"exchange": "ccxt"}}) == "okx"


def test_strategy_executor_kucoin_demo(dash):
    """A KuCoin demo strategy gets a KuCoin demo executor (venue + sandbox)."""
    dash_mod, _core, _root = dash
    strat = {"instrument": {"exchange": "kucoin", "inst_id": "BTC/USDT:USDT"}}
    ex, err = dash_mod._strategy_executor(strat, "demo")
    assert err is None, err
    assert ex.execution_cfg["ccxt_exchange"] == "kucoin"
    assert ex.execution_cfg["simulated_trading"] is True


def test_strategy_executor_okx_live(dash):
    """An OKX live strategy gets an OKX live executor (venue + real account)."""
    dash_mod, _core, _root = dash
    strat = {"instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP"}}
    ex, err = dash_mod._strategy_executor(strat, "live")
    assert err is None, err
    assert ex.execution_cfg["ccxt_exchange"] == "okx"
    assert ex.execution_cfg["simulated_trading"] is False


def test_strategy_live_snapshot_tags_venue_and_caches_per_env(dash, monkeypatch):
    dash_mod, _core, _root = dash
    calls = _env_recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    snap = dash_mod.strategy_live_snapshot({"instrument": {"exchange": "kucoin"}}, "demo")

    assert snap["venue"] == "kucoin"
    assert snap["mode"] == "demo"
    assert snap["simulated_trading"] is True
    assert ("kucoin", True) in calls
    # Cache key is namespaced by (venue, mode) so venues never share a slot.
    assert "snapshot:kucoin:demo" in dash_mod._OKX_LIVE_CACHE


def test_strategy_live_snapshot_fail_closed_when_not_armed(dash, monkeypatch, capsys):
    dash_mod, _core, _root = dash
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)  # disarmed
    calls = _env_recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    snap = dash_mod.strategy_live_snapshot({"instrument": {"exchange": "kucoin"}}, "live")

    # Degraded to the demo account; never built a live executor for kucoin.
    assert snap["venue"] == "kucoin"
    assert snap["mode"] == "demo"
    assert snap["simulated_trading"] is True
    assert calls == [("kucoin", True)]
    assert "HERMX_LIVE_TRADING" in capsys.readouterr().err


def test_order_history_reconcile_uses_strategy_venue_and_mode(dash, monkeypatch):
    """Ledger reconcile is fed the strategy's OWN (venue, mode), never okx/demo."""
    dash_mod, _core, _root = dash
    import pnl_ledger  # noqa: WPS433

    recorded: list[tuple] = []

    def fake_reconcile(rows, exchange_id, mode):
        recorded.append((exchange_id, mode))
        return 0

    monkeypatch.setattr(pnl_ledger, "reconcile_from_order_history", fake_reconcile)

    class _HistExecutor:
        def get_order_history_raw(self, inst_ids, limit=100):
            return [{"instId": "BTC/USDT:USDT"}]

    monkeypatch.setattr(dash_mod, "_strategy_executor", lambda sc, mode: (_HistExecutor(), None))
    dash_mod._OKX_ORDER_HISTORY_CACHE.clear()

    snap = dash_mod.strategy_order_history_snapshot({"instrument": {"exchange": "kucoin"}}, "live")

    assert snap["ok"] is True
    assert snap["venue"] == "kucoin"
    assert snap["mode"] == "live"
    assert recorded == [("kucoin", "live")]


def test_snapshot_for_env_prefers_venue_mode_then_falls_back(dash):
    dash_mod, _core, _root = dash
    by_env = {"okx:demo": {"tag": "okx-demo"}, "kucoin:live": {"tag": "kc-live"}}
    assert dash_mod._snapshot_for_env(by_env, {}, "okx", "demo")["tag"] == "okx-demo"
    assert dash_mod._snapshot_for_env(by_env, {}, "kucoin", "live")["tag"] == "kc-live"
    # No per-env hit -> fall back to the legacy mode-only map.
    by_mode = {"demo": {"tag": "legacy-demo"}, "live": {"tag": "legacy-live"}}
    assert dash_mod._snapshot_for_env({}, by_mode, "okx", "live")["tag"] == "legacy-live"


def test_dashboard_model_builds_per_env_map_no_cross_contamination(dash, monkeypatch):
    """Two strategies on different venues each resolve their own (venue, mode) env."""
    dash_mod, _core, root = dash
    _write_strategy(
        root / "strategies", "okxs", asset="BTCUSDT",
        instrument={"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        execution_mode="demo",
    )
    _write_strategy(
        root / "strategies", "kcs", asset="ETHUSDT",
        instrument={"exchange": "kucoin", "inst_id": "ETH/USDT:USDT", "type": "swap"},
        execution_mode="demo",
    )
    calls = _env_recorder(dash_mod, monkeypatch)
    _bust_caches(dash_mod)

    model = dash_mod.dashboard_model()

    by_env = model["okx_live_by_env"]
    assert "okx:demo" in by_env
    assert "kucoin:demo" in by_env
    assert by_env["okx:demo"]["venue"] == "okx"
    assert by_env["kucoin:demo"]["venue"] == "kucoin"
    # Both venues' demo (sandbox) accounts were built; no live executor was ever made.
    venues_built = {venue for venue, _sim in calls}
    assert "okx" in venues_built and "kucoin" in venues_built
    assert False not in {sim for _venue, sim in calls}


def test_toggle_switches_venue_and_mode(dash):
    """Toggling a strategy demo->live flips which (venue, mode) snapshot it reads."""
    dash_mod, _core, root = dash
    _write_strategy(
        root / "strategies", "s1",
        instrument={"exchange": "kucoin", "inst_id": "ETH/USDT:USDT"},
        execution_mode="demo",
    )
    by_env = {
        "kucoin:demo": {"tag": "kc-demo"},
        "kucoin:live": {"tag": "kc-live"},
    }
    strat = {"strategy_id": "s1", "execution_mode": "demo",
             "instrument": {"exchange": "kucoin"}}

    mode_before = dash_mod._effective_strategy_mode(strat, {})
    assert dash_mod._snapshot_for_env(
        by_env, {}, dash_mod._strategy_venue(strat), mode_before
    )["tag"] == "kc-demo"

    assert dash_mod._set_strategy_override("s1", "live") is True
    overrides = dash_mod._load_control_state().get("strategy_overrides") or {}
    mode_after = dash_mod._effective_strategy_mode(strat, overrides)
    assert mode_after == "live"
    assert dash_mod._snapshot_for_env(
        by_env, {}, dash_mod._strategy_venue(strat), mode_after
    )["tag"] == "kc-live"
