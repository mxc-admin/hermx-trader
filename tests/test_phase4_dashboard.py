"""Phase 4 — Dashboard Reliability tests (REFACTOR_PLAN.md:353-374, fixes D1-D8).

Offline and deterministic: no network, no real OKX. The dashboard is exercised
as a READ-ONLY consumer — every test points SHADOW_ROOT at an isolated temp dir
(honoring the conftest harness conventions) and monkeypatches the executor seam /
the network ticker helper. Nothing here can place an order or arm submission.
"""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Harness: load dashboard_core + dashboard bound to a populated temp SHADOW_ROOT.
# Both modules resolve SHADOW_ROOT (ROOT/LOGS/STRATEGIES_DIR) at import time, so
# we set the env then importlib.reload() — mirroring tests/conftest.py's `wr`.
# ---------------------------------------------------------------------------

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


@pytest.fixture
def dash(tmp_path):
    """dashboard + dashboard_core reloaded against a fresh temp SHADOW_ROOT."""
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "strategies").mkdir(parents=True, exist_ok=True)

    orig_root = os.environ.get("SHADOW_ROOT")
    os.environ["SHADOW_ROOT"] = str(root)

    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)

    # Default-safe offline executor stub: ok, flat, fresh. Individual tests
    # override this to simulate failures / staleness.
    def _fresh_okx_live(config):
        return {
            "ok": True,
            "positions": {},
            "account": {},
            "error": None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    dash_mod.okx_live_snapshot = _fresh_okx_live
    dash_mod._MODEL_CACHE["expires_at"] = 0.0  # always rebuild
    dash_mod._MODEL_CACHE["model"] = None

    try:
        yield dash_mod, core, root
    finally:
        if orig_root is not None:
            os.environ["SHADOW_ROOT"] = orig_root
        else:
            os.environ.pop("SHADOW_ROOT", None)
        os.environ.pop("HERMX_DASH_TZ", None)
        os.environ.pop("HERMX_DASH_TZ_OFFSET_HOURS", None)


def _bust_cache(dash_mod):
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None


# ---------------------------------------------------------------------------
# D1 / D2 — bounded, corrupt-tolerant ledger reads.
# ---------------------------------------------------------------------------

def test_bounded_corrupt_ledger_read(dash):
    _dash, core, root = dash
    ledger = root / "logs" / "big.jsonl"
    lines = []
    for i in range(5000):
        lines.append(json.dumps({"i": i}))
    # A corrupt line that lands inside the tail window, then more good lines,
    # then a truncated final line (writer mid-append).
    lines.append("{ this is not valid json")          # corrupt, mid-tail
    for i in range(50):
        lines.append(json.dumps({"j": i}))
    lines.append('{"partial": 12')                     # truncated final line
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows, stats = core.read_jsonl_stats(ledger, limit=100)

    # Bounded: never materializes the whole 5051-line file.
    assert len(rows) <= 100
    # The corrupt mid-tail line is surfaced, not hidden.
    assert stats["skipped"] >= 1
    # The torn final line is tolerated as a truncated tail, not counted corrupt.
    assert stats["truncated_tail"] is True
    # Older content beyond the window is reported.
    assert stats["more"] is True
    # No exception, and the rows we did parse are valid dicts.
    assert all(isinstance(r, dict) for r in rows)


def test_giant_single_line_does_not_oom(dash):
    _dash, core, root = dash
    ledger = root / "logs" / "huge_line.jsonl"
    # One ~2MB line with no newline: reader must return gracefully, not blow up.
    ledger.write_text("{" + "x" * (2 * 1024 * 1024), encoding="utf-8")
    rows, stats = core.read_jsonl_stats(ledger, limit=50)
    assert rows == []
    assert stats["exists"] is True


def test_missing_ledger_is_empty(dash):
    _dash, core, root = dash
    rows, stats = core.read_jsonl_stats(root / "logs" / "nope.jsonl", limit=10)
    assert rows == []
    assert stats["exists"] is False


# ---------------------------------------------------------------------------
# D3 — surface executor failures (never a silent flat view).
# ---------------------------------------------------------------------------

def test_executor_error_surfaced_in_model_and_banner(dash):
    dash_mod, _core, _root = dash

    def _broken_executor(config):
        return {"ok": False, "positions": {}, "error": "okx_health_failed", "generated_at": None}

    dash_mod.okx_live_snapshot = _broken_executor
    _bust_cache(dash_mod)

    model = dash_mod.dashboard_model()
    execu = model["executor"]
    assert execu["healthy"] is False
    assert execu["degraded"] is True
    assert execu["error"] == "okx_health_failed"

    # /api exposes the executor error explicitly.
    payload = dash_mod.api_payload()
    assert payload["executor"]["error"] == "okx_health_failed"
    assert payload["executor"]["degraded"] is True

    # Rendered HTML shows an explicit banner, not a silent flat view.
    html = dash_mod.render()
    assert "EXECUTOR ERROR" in html


def test_executor_stale_when_no_timestamp(dash):
    dash_mod, _core, _root = dash

    def _ok_no_ts(config):
        return {"ok": True, "positions": {}, "error": None, "generated_at": None}

    dash_mod.okx_live_snapshot = _ok_no_ts
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    # ok but unprovable freshness -> degraded/stale, never silently "healthy".
    assert model["executor"]["stale"] is True
    assert model["executor"]["degraded"] is True


def test_executor_stale_when_old_timestamp(dash):
    dash_mod, _core, _root = dash
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    def _ok_old(config):
        return {"ok": True, "positions": {}, "error": None, "generated_at": old}

    dash_mod.okx_live_snapshot = _ok_old
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    assert model["executor"]["healthy"] is True
    assert model["executor"]["stale"] is True
    assert "STALE" in dash_mod.render()


# ---------------------------------------------------------------------------
# D5 — dynamic, strategy-file-driven cards (no code change).
# ---------------------------------------------------------------------------

def test_cards_appear_and_disappear_with_strategy_files(dash):
    dash_mod, _core, root = dash
    strategies_dir = root / "strategies"

    # Start with a single active strategy.
    _write_strategy(strategies_dir, "btc_strat", asset="BTCUSDT")
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    active = [s["strategy_id"] for s in model["active_strategies"]]
    assert active == ["btc_strat"]
    assert "BTCUSDT" in dash_mod.render()
    assert "ETHUSDT" not in dash_mod.render()

    # Add a new active strategy file — a card must appear with NO code change.
    _write_strategy(strategies_dir, "eth_strat", asset="ETHUSDT")
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    active = sorted(s["strategy_id"] for s in model["active_strategies"])
    assert active == ["btc_strat", "eth_strat"]
    html = dash_mod.render()
    assert "BTCUSDT" in html and "ETHUSDT" in html

    # Disable one via submit_orders=false — its card must disappear.
    _write_strategy(strategies_dir, "eth_strat", asset="ETHUSDT", submit_orders=False)
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    active = [s["strategy_id"] for s in model["active_strategies"]]
    assert active == ["btc_strat"]
    assert "ETHUSDT" not in dash_mod.render()

    # Disabling the last active strategy clears the board.
    _write_strategy(strategies_dir, "btc_strat", asset="BTCUSDT", submit_orders=False)
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    assert model["active_strategies"] == []


def test_active_strategy_helpers(dash):
    dash_mod, _core, _root = dash
    # A strategy renders a card iff it is permitted to submit orders.
    assert dash_mod.is_strategy_active({"submit_orders": True}) is True
    assert dash_mod.is_strategy_active({"submit_orders": False}) is False
    assert dash_mod.is_strategy_active({}) is False


# ---------------------------------------------------------------------------
# D4 — /api contract has no silently-empty policy field.
# ---------------------------------------------------------------------------

def test_api_contract_no_empty_policy_field(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "btc_strat", asset="BTCUSDT")
    _bust_cache(dash_mod)
    payload = dash_mod.api_payload()

    # The legacy always-empty policy payload is gone.
    assert "policies" not in payload
    # Required contractual fields are present and meaningfully populated.
    for key in ("generated_at", "strategies", "executor", "ledger_health", "freshness"):
        assert key in payload
    assert payload["strategies"], "strategies must not be silently empty when a strategy file is active"
    assert payload["executor"]  # non-empty health verdict

    # health endpoint no longer leaks the empty expected_policies field.
    assert "expected_policies" not in dash_mod.health_payload()


# ---------------------------------------------------------------------------
# D6 / D7 — datetime tolerance, configurable tz, freshness.
# ---------------------------------------------------------------------------

def test_naive_timestamp_parses_as_utc(dash):
    _dash, core, _root = dash
    dt = core.parse_dt("2026-06-27T12:00:00")  # no tz
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)
    # Renders a real time, not "-".
    assert core.display_time("2026-06-27T12:00:00") != "-"


def test_display_timezone_configurable(dash):
    _dash, core, _root = dash
    # Default UTC.
    assert core.display_time("2026-06-27T12:00:00Z") == "2026-06-27 12:00"
    # Offset env shifts the rendered wall-clock.
    os.environ["HERMX_DASH_TZ_OFFSET_HOURS"] = "-5"
    assert core.display_time("2026-06-27T12:00:00Z") == "2026-06-27 07:00"
    os.environ.pop("HERMX_DASH_TZ_OFFSET_HOURS", None)


def test_freshness_stale_and_fresh(dash):
    dash_mod, _core, _root = dash
    now = 1_000_000.0
    fresh_iso = datetime.fromtimestamp(now - 5, timezone.utc).isoformat()
    stale_iso = datetime.fromtimestamp(now - 600, timezone.utc).isoformat()

    fresh = dash_mod.freshness_summary(
        {"strategy_alerts": [{"received_at": fresh_iso}], "okx_live": {}, "generated_at": fresh_iso},
        now=now,
    )
    assert fresh["stale"] is False
    assert fresh["age_seconds"] == pytest.approx(5, abs=1)

    stale = dash_mod.freshness_summary(
        {"strategy_alerts": [{"received_at": stale_iso}], "okx_live": {}, "generated_at": stale_iso},
        now=now,
    )
    assert stale["stale"] is True

    no_data = dash_mod.freshness_summary({"strategy_alerts": [], "okx_live": {}, "generated_at": fresh_iso}, now=now)
    assert no_data["stale"] is True
    assert no_data["no_data"] is True


def test_stale_cache_renders_badge(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "btc_strat", asset="BTCUSDT")
    # Seed a strategy-alert ledger row with an OLD received_at so data is stale.
    old_iso = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    alert = {
        "received_at": old_iso,
        "normalized": {
            "strategy_id": "btc_strat",
            "symbol": "BTCUSDT",
            "side": "buy",
            "timeframe": "2h",
            "tv_time": old_iso,
            "tv_signal_price": 50000,
        },
        "strategy_config": {"name": "Test"},
    }
    (root / "logs" / "strategy-alerts.jsonl").write_text(json.dumps(alert) + "\n", encoding="utf-8")

    # Executor read is also old -> stale.
    dash_mod.okx_live_snapshot = lambda cfg: {"ok": True, "positions": {}, "error": None, "generated_at": old_iso}
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    assert model["freshness"]["stale"] is True
    assert "STALE" in dash_mod.render()


# ---------------------------------------------------------------------------
# D8 — shared canonical_timeframe (receiver and dashboard agree).
# ---------------------------------------------------------------------------

def test_shared_canonical_timeframe_agreement(dash):
    dash_mod, _core, _root = dash
    from hermx_shared import canonical_timeframe as shared_ct
    import webhook_receiver

    cases = ["30", "30min", "30mins", "60", "1h", "1hr", "120", "2h", "2hour",
             "180", "3h", "240", "4hour", "  2H ", "weird-tf", ""]
    for value in cases:
        expected = shared_ct(value)
        assert webhook_receiver.canonical_timeframe(value) == expected
        assert dash_mod.canonical_timeframe(value) == expected

    # And the dashboard's name is literally the shared implementation (no copy).
    assert dash_mod.canonical_timeframe is shared_ct


# ---------------------------------------------------------------------------
# Structured-model assertion (preferred stable contract over HTML golden).
# ---------------------------------------------------------------------------

def test_model_contract_keys(dash):
    dash_mod, _core, root = dash
    _write_strategy(root / "strategies", "btc_strat", asset="BTCUSDT")
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    for key in ("config", "loaded", "okx_live", "strategies", "active_strategies",
                "strategy_alerts", "generated_at", "executor", "ledger_health", "freshness"):
        assert key in model
    # No legacy `sim` key remains (dead policy path removed, D4).
    assert "sim" not in model


# ---------------------------------------------------------------------------
# Read-only "arm" status in /health (Hermes Agent operator interface).
# kill_switch_engaged mirrors webhook_receiver.submit_kill_switch_armed(); the
# armed_summary is the AND of (NOT engaged) and the three config gates.
# ---------------------------------------------------------------------------

def _armed_config():
    """A config whose three gates are all live (so only the kill switch varies)."""
    return {
        "execution": {"enabled": True, "submit_orders": True},
        "risk": {"allow_live_execution": True},
    }


def test_arm_kill_switch_unset_is_not_engaged(dash, monkeypatch):
    dash_mod, _core, _root = dash
    monkeypatch.delenv("HERMX_SUBMIT_ENABLED", raising=False)
    monkeypatch.setattr(dash_mod, "shadow_config", _armed_config)

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is False
    # Unset kill switch + all gates live => fully armed.
    assert arm["armed_summary"] is True
    assert arm["submit_orders"] is True
    assert arm["execution_enabled"] is True
    assert arm["allow_live_execution"] is True


def test_arm_kill_switch_false_is_engaged(dash, monkeypatch):
    dash_mod, _core, _root = dash
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "false")
    monkeypatch.setattr(dash_mod, "shadow_config", _armed_config)

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is True
    # Engaged kill switch vetoes the summary even though every config gate is live.
    assert arm["armed_summary"] is False


def test_arm_kill_switch_true_is_not_engaged(dash, monkeypatch):
    dash_mod, _core, _root = dash
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "true")
    monkeypatch.setattr(dash_mod, "shadow_config", _armed_config)

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is False
    assert arm["armed_summary"] is True


def test_arm_summary_requires_all_gates(dash, monkeypatch):
    dash_mod, _core, _root = dash
    monkeypatch.delenv("HERMX_SUBMIT_ENABLED", raising=False)  # not engaged

    # Each single gate falsy must drop armed_summary to False (AND-logic).
    cases = [
        {"execution": {"enabled": True, "submit_orders": False},
         "risk": {"allow_live_execution": True}},
        {"execution": {"enabled": False, "submit_orders": True},
         "risk": {"allow_live_execution": True}},
        {"execution": {"enabled": True, "submit_orders": True},
         "risk": {"allow_live_execution": False}},
    ]
    for cfg in cases:
        monkeypatch.setattr(dash_mod, "shadow_config", lambda cfg=cfg: cfg)
        arm = dash_mod.health_payload()["arm"]
        assert arm["kill_switch_engaged"] is False
        assert arm["armed_summary"] is False
