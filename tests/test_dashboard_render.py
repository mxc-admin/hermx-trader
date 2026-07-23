"""Tests for dashboard.render summary cards (#11).

The EXECUTION ENGINE card reflects EXECUTOR health staleness (executor["stale"]),
NOT alert-freshness staleness (freshness["stale"]). These were crossed: the card
sourced its stale flag from the freshness dict, so it mislabeled the engine STALE
whenever alerts were merely quiet, and missed a genuinely stale executor when
alerts were flowing.
"""
from __future__ import annotations

import dashboard


def _model(*, executor, freshness):
    return {
        "okx_live": {"positions": {}},
        "active_strategies": [{"execution_mode": "demo"}],
        "executor": executor,
        "freshness": freshness,
    }


def test_execution_engine_stale_from_executor_not_freshness():
    # Executor is stale but alerts are fresh -> EXECUTION ENGINE must read STALE.
    html = dashboard.summary_cards(
        _model(executor={"ok": True, "stale": True, "error": None},
               freshness={"stale": False})
    )
    assert "EXECUTION ENGINE" in html
    assert "STALE" in html


def test_execution_engine_healthy_when_only_alerts_stale():
    # Executor is healthy; only alert freshness is stale -> the engine card must NOT
    # be mislabeled STALE (regression: it previously read from freshness["stale"]).
    html = dashboard.summary_cards(
        _model(executor={"ok": True, "stale": False, "error": None},
               freshness={"stale": True})
    )
    assert "STALE" not in html
    assert "OK" in html


def test_execution_engine_error_when_executor_not_ok():
    # Not stale, but executor reports not-ok -> ERROR (unaffected by freshness).
    html = dashboard.summary_cards(
        _model(executor={"ok": False, "stale": False, "error": "boom"},
               freshness={"stale": False})
    )
    assert "ERROR" in html
    assert "STALE" not in html


def _healthy_model(**extra):
    return {
        **_model(executor={"ok": True, "stale": False, "error": None},
                 freshness={"stale": False}),
        **extra,
    }


def test_open_positions_counts_across_env_snapshots_not_okx_live_only():
    # An HL-only open short with an empty OKX-demo snapshot must show 1 OPEN /
    # 0L / 1S — okx_live alone would mislabel it ALL FLAT.
    html = dashboard.summary_cards(_healthy_model(exch_live_by_env={
        "okx:demo": {"ok": True, "positions": {}},
        "hyperliquid:live": {"ok": True, "positions": {
            "BTCUSDT": {"inst_id": "BTC-USDT-SWAP", "pos": -0.05, "side": "SHORT"},
        }},
    }))
    assert "1 OPEN" in html
    assert "0L / 1S" in html


def test_open_positions_env_snapshot_skips_failed_envs_and_flat_rows():
    html = dashboard.summary_cards(_healthy_model(exch_live_by_env={
        "okx:demo": {"ok": True, "positions": {
            "BTCUSDT": {"pos": 0.0, "side": "FLAT"},
            "ETHUSDT": {"pos": 1.0, "side": "LONG"},
        }},
        "hyperliquid:live": {"ok": False, "error": "down", "positions": {
            "SOLUSDT": {"pos": -1.0, "side": "SHORT"},
        }},
    }))
    assert "1 OPEN" in html
    assert "1L / 0S" in html


def test_open_positions_falls_back_to_okx_live_without_env_snapshots():
    # Old model shape (no exch_live_by_env) keeps the legacy okx_live derivation.
    model = _healthy_model()
    model["okx_live"] = {"positions": {"BTCUSDT": {"pos": -0.05, "side": "SHORT"}}}
    html = dashboard.summary_cards(model)
    assert "1 OPEN" in html
    assert "0L / 1S" in html
