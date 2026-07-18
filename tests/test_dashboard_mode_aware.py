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
import os
from datetime import datetime, timedelta, timezone

import pytest

from conftest import _write_strategy


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


class _HealthExecutor:
    """Offline executor whose health() serves canned position rows (ok=True path)."""

    def __init__(self, positions):
        self._positions = positions

    def health(self):
        return {"ok": True, "positions": self._positions, "generated_at": "2026-07-18T00:00:00Z", "account": {}}


def test_strategy_live_snapshot_joins_hyperliquid_inst_dialects(dash, monkeypatch):
    """Adapter reports venue-format instId (SOL-USDC-SWAP); strategy file uses ccxt
    format (SOL/USDC:USDC). The unified-symbol join must match them — not report FLAT —
    while the positions dict still emits the strategy-format inst_id."""
    dash_mod, _core, root = dash
    _write_strategy(
        root / "strategies", "hl1", asset="SOLUSDC",
        instrument={"exchange": "hyperliquid", "inst_id": "SOL/USDC:USDC", "type": "swap"},
    )
    monkeypatch.setattr(dash_mod, "okx_swap_tickers", lambda: {})
    positions = [{"instId": "SOL-USDC-SWAP", "pos": -0.2, "avgPx": 150.0}]
    monkeypatch.setattr(
        dash_mod, "_dashboard_executor",
        lambda config, simulated_trading=True: (_HealthExecutor(positions), None),
    )
    _bust_caches(dash_mod)

    snap = dash_mod.strategy_live_snapshot(
        {"instrument": {"exchange": "hyperliquid", "inst_id": "SOL/USDC:USDC"}}, "demo")

    assert snap["ok"] is True
    row = snap["positions"]["SOLUSDC"]
    assert row["side"] == "SHORT"
    assert row["pos"] == -0.2
    assert row["inst_id"] == "SOL/USDC:USDC"


def test_okx_live_snapshot_native_inst_id_still_matches(dash, monkeypatch):
    """No-regression: an OKX-native instId (BTC-USDT-SWAP) on both sides still joins."""
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "s1")  # template: BTCUSDT / BTC-USDT-SWAP
    monkeypatch.setattr(dash_mod, "okx_swap_tickers", lambda: {})
    positions = [{"instId": "BTC-USDT-SWAP", "pos": 1.5, "avgPx": 60000.0}]
    monkeypatch.setattr(
        dash_mod, "_dashboard_executor",
        lambda config, simulated_trading=True: (_HealthExecutor(positions), None),
    )
    _bust_caches(dash_mod)

    snap = dash_mod.okx_live_snapshot({}, simulated_trading=True)

    assert snap["ok"] is True
    row = snap["positions"]["BTCUSDT"]
    assert row["side"] == "LONG"
    assert row["pos"] == 1.5
    assert row["inst_id"] == "BTC-USDT-SWAP"


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


def _ageout_setup(dash_mod, monkeypatch, n_rows, oldest_ms, high_water_ms):
    """Seed the ledger high-water for kucoin/live, stub the executor to return
    ``n_rows`` history rows whose oldest uTime is ``oldest_ms``, and capture any
    reconcile alert. Returns the captured-alert list."""
    import pnl_ledger  # noqa: WPS433
    import webhook_receiver  # noqa: WPS433

    pnl_ledger.append_closed_trades([
        {"exchange": "kucoin", "inst_id": "BTC/USDT:USDT", "ord_id": "hw",
         "mode": "live", "closed_at_ms": high_water_ms},
    ])

    rows = [{"instId": "BTC/USDT:USDT", "uTime": oldest_ms + i} for i in range(n_rows)]

    class _HistExecutor:
        def get_order_history_raw(self, inst_ids, limit=100):
            return rows

    monkeypatch.setattr(dash_mod, "_strategy_executor", lambda sc, mode: (_HistExecutor(), None))
    dash_mod._OKX_ORDER_HISTORY_CACHE.clear()

    captured: list[tuple] = []
    monkeypatch.setattr(
        "reconcile.alerts.emit_reconcile_alert",
        lambda kind, detail: captured.append((kind, detail)) or {},
    )
    return captured


def test_ageout_detector_fires_on_saturated_window_past_high_water(dash, monkeypatch):
    dash_mod, _core, _root = dash
    captured = _ageout_setup(dash_mod, monkeypatch, n_rows=100, oldest_ms=5000, high_water_ms=1000)
    dash_mod.strategy_order_history_snapshot({"instrument": {"exchange": "kucoin"}}, "live")
    assert len(captured) == 1
    _kind, detail = captured[0]
    assert detail["stage"] == "history_window_ageout"
    assert detail["venue"] == "kucoin"
    assert detail["mode"] == "live"
    assert detail["oldest_ms"] == 5000
    assert detail["high_water_ms"] == 1000
    assert detail["gap_ms"] == 4000


def test_no_ageout_when_window_unsaturated(dash, monkeypatch):
    dash_mod, _core, _root = dash
    captured = _ageout_setup(dash_mod, monkeypatch, n_rows=99, oldest_ms=5000, high_water_ms=1000)
    dash_mod.strategy_order_history_snapshot({"instrument": {"exchange": "kucoin"}}, "live")
    assert captured == []


def test_no_ageout_when_oldest_row_overlaps_ledger(dash, monkeypatch):
    dash_mod, _core, _root = dash
    # Saturated but oldest row (500) predates the high-water (1000) -> no gap.
    captured = _ageout_setup(dash_mod, monkeypatch, n_rows=100, oldest_ms=500, high_water_ms=1000)
    dash_mod.strategy_order_history_snapshot({"instrument": {"exchange": "kucoin"}}, "live")
    assert captured == []


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

    by_env = model["exch_live_by_env"]
    assert "okx:demo" in by_env
    assert "kucoin:demo" in by_env
    assert by_env["okx:demo"]["venue"] == "okx"
    assert by_env["kucoin:demo"]["venue"] == "kucoin"
    # Both venues' demo (sandbox) accounts were built; no live executor was ever made.
    venues_built = {venue for venue, _sim in calls}
    assert "okx" in venues_built and "kucoin" in venues_built
    assert False not in {sim for _venue, sim in calls}


# ---------------------------------------------------------------------------
# Engine verdict — model["executor"] aggregates every ACTIVE (venue, mode) env
# instead of keying off the single legacy OKX-demo snapshot.
# ---------------------------------------------------------------------------


def _iso_ago(minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _stub_model_offline(dash_mod, monkeypatch, env_snaps, legacy_ok=True):
    """Stub every network-touching snapshot seam for a full dashboard_model()
    build. ``env_snaps`` maps '{venue}:{mode}' -> the strategy_live_snapshot stub
    result for that env. The legacy okx_live_snapshot (demo) read is stubbed
    independently so tests can prove the verdict no longer keys off it."""

    def fake_env_snapshot(strategy_config, mode):
        venue = dash_mod._strategy_venue(strategy_config)
        mode_key = "live" if str(mode or "").lower() == "live" else "demo"
        snap = dict(env_snaps[f"{venue}:{mode_key}"])
        snap.setdefault("positions", {})
        snap.update({"venue": venue, "mode": mode_key})
        return snap

    monkeypatch.setattr(dash_mod, "strategy_live_snapshot", fake_env_snapshot)
    monkeypatch.setattr(
        dash_mod, "strategy_order_history_snapshot",
        lambda strategy_config, mode: {"ok": False, "rows": []},
    )
    monkeypatch.setattr(
        dash_mod, "okx_live_snapshot",
        lambda config, simulated_trading=True: {
            "ok": legacy_ok,
            "positions": {},
            "error": None if legacy_ok else "legacy_demo_down",
            "generated_at": _iso_ago() if legacy_ok else None,
        },
    )
    monkeypatch.setattr(dash_mod, "okx_order_history_snapshot", lambda config: {"ok": False})
    _bust_caches(dash_mod)


def _write_two_venue_strategies(root):
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


def test_executor_verdict_all_active_envs_healthy(dash, monkeypatch):
    dash_mod, _core, root = dash
    _write_two_venue_strategies(root)
    _stub_model_offline(dash_mod, monkeypatch, {
        "okx:demo": {"ok": True, "error": None, "generated_at": _iso_ago()},
        "kucoin:demo": {"ok": True, "error": None, "generated_at": _iso_ago()},
    })

    execu = dash_mod.dashboard_model()["executor"]

    assert execu["ok"] is True
    assert execu["healthy"] is True
    assert execu["stale"] is False
    assert execu["degraded"] is False
    assert execu["error"] is None
    # Additive per-env detail; the ExecutorHealthCard contract keys stay present.
    assert set(execu["envs"]) == {"okx:demo", "kucoin:demo"}
    assert execu["generated_at"] is not None


def test_executor_verdict_env_error_beats_healthy_legacy_demo(dash, monkeypatch):
    """THE bug: the legacy OKX-demo read is healthy while a real trading venue is
    down — the Engine widget must be red, not green."""
    dash_mod, _core, root = dash
    _write_two_venue_strategies(root)
    _stub_model_offline(dash_mod, monkeypatch, {
        "okx:demo": {"ok": True, "error": None, "generated_at": _iso_ago()},
        "kucoin:demo": {"ok": False, "error": "kucoin_unreachable", "generated_at": None},
    }, legacy_ok=True)

    execu = dash_mod.dashboard_model()["executor"]

    assert execu["ok"] is False
    assert execu["healthy"] is False
    assert execu["degraded"] is True
    # Env-prefixed so the banner names the venue that is down.
    assert execu["error"] == "kucoin:demo: kucoin_unreachable"
    assert execu["envs"]["okx:demo"]["ok"] is True
    assert execu["envs"]["kucoin:demo"]["healthy"] is False


def test_executor_verdict_one_env_stale(dash, monkeypatch):
    dash_mod, _core, root = dash
    _write_two_venue_strategies(root)
    _stub_model_offline(dash_mod, monkeypatch, {
        "okx:demo": {"ok": True, "error": None, "generated_at": _iso_ago()},
        "kucoin:demo": {"ok": True, "error": None, "generated_at": _iso_ago(minutes=10)},
    })

    execu = dash_mod.dashboard_model()["executor"]

    assert execu["healthy"] is True
    assert execu["stale"] is True
    assert execu["degraded"] is True
    assert execu["ok"] is False
    # age_seconds reports the OLDEST env read (~600s), not the fresh one.
    assert execu["age_seconds"] >= 500


def test_executor_verdict_falls_back_to_legacy_when_no_strategies(dash, monkeypatch):
    """Zero active strategies keeps today's single OKX-demo verdict, exact shape."""
    dash_mod, _core, _root = dash  # no strategy files written
    _stub_model_offline(dash_mod, monkeypatch, {}, legacy_ok=False)

    execu = dash_mod.dashboard_model()["executor"]

    assert execu["healthy"] is False
    assert execu["degraded"] is True
    assert execu["error"] == "legacy_demo_down"  # un-prefixed legacy summary
    assert "envs" not in execu


def test_executor_verdict_excludes_inactive_strategy_env(dash, monkeypatch):
    """An env used only by an inactive strategy must not taint the verdict."""
    dash_mod, _core, root = dash
    _write_two_venue_strategies(root)
    monkeypatch.setattr(
        dash_mod, "is_strategy_active",
        lambda strategy: (strategy or {}).get("strategy_id") != "kcs",
    )
    _stub_model_offline(dash_mod, monkeypatch, {
        "okx:demo": {"ok": True, "error": None, "generated_at": _iso_ago()},
        # Fetched by the by_env builder (it walks all files) but inactive: broken.
        "kucoin:demo": {"ok": False, "error": "kucoin_unreachable", "generated_at": None},
    })

    execu = dash_mod.dashboard_model()["executor"]

    assert set(execu["envs"]) == {"okx:demo"}
    assert execu["ok"] is True
    assert execu["error"] is None


def test_executor_env_health_missing_snapshot_is_error(dash):
    """A derived env with no fetched snapshot is an explicit error, never green
    and never a KeyError."""
    _dash_mod, _core, _root = dash
    from dashboard.model import executor_env_health_summary

    verdict = executor_env_health_summary({}, ["okx:demo"], now=1000.0)

    assert verdict["ok"] is False
    assert verdict["healthy"] is False
    assert verdict["degraded"] is True
    assert verdict["error"] == "okx:demo: missing_env_snapshot"
    assert verdict["envs"]["okx:demo"]["error"] == "missing_env_snapshot"


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
