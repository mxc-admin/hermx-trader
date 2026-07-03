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

    # Remove one strategy file — its card must disappear (D5: file presence is the
    # only gate now; every strategy file is active).
    (strategies_dir / "eth_strat.json").unlink()
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    active = [s["strategy_id"] for s in model["active_strategies"]]
    assert active == ["btc_strat"]
    assert "ETHUSDT" not in dash_mod.render()

    # Removing the last strategy file clears the board.
    (strategies_dir / "btc_strat.json").unlink()
    _bust_cache(dash_mod)
    model = dash_mod.dashboard_model()
    assert model["active_strategies"] == []


def test_active_strategy_helpers(dash):
    dash_mod, _core, _root = dash
    # 2-mode model: every strategy with a valid file is active, regardless of the
    # (now-ignored) submit_orders field. execution_mode decides sandbox vs live.
    assert dash_mod.is_strategy_active({"execution_mode": "live"}) is True
    assert dash_mod.is_strategy_active({"execution_mode": "demo"}) is True
    assert dash_mod.is_strategy_active({}) is True


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
    # A numeric offset in HERMX_DASH_TZ shifts the rendered wall-clock (the former
    # separate HERMX_DASH_TZ_OFFSET_HOURS flag was merged into HERMX_DASH_TZ).
    os.environ["HERMX_DASH_TZ"] = "-5"
    assert core.display_time("2026-06-27T12:00:00Z") == "2026-06-27 07:00"
    os.environ.pop("HERMX_DASH_TZ", None)


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
        "stage": "strategy_match",
        "signal_id": "sig-1",
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
    # strategy-alerts were consolidated into pipeline.jsonl (stage="strategy_match").
    (root / "logs" / "pipeline.jsonl").write_text(json.dumps(alert) + "\n", encoding="utf-8")

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
# The 2-control model: kill_switch_engaged is the inverse of the global
# HERMX_LIVE_TRADING switch (live_trading_enabled()), and `armed` is true only
# when at least one loaded strategy runs execution_mode="live" AND the global
# switch is on. The dead config-flag chain (execution.enabled/submit_orders,
# risk.allow_live_execution) is gone.
# ---------------------------------------------------------------------------

def test_arm_live_trading_unset_engages_kill_switch(dash, monkeypatch):
    dash_mod, _core, root = dash
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    _write_strategy(root / "strategies", "btc_live", execution_mode="live")

    arm = dash_mod.health_payload()["arm"]
    # Fail-closed default: an unset switch means live trading is DISABLED.
    assert arm["kill_switch_engaged"] is True
    assert arm["live_trading_enabled"] is False
    assert arm["live_strategies"] == 1
    # A live strategy is present but the global switch is off => NOT armed.
    assert arm["armed"] is False


def test_arm_live_trading_false_engages_kill_switch(dash, monkeypatch):
    dash_mod, _core, root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")
    _write_strategy(root / "strategies", "btc_live", execution_mode="live")

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is True
    assert arm["live_trading_enabled"] is False
    assert arm["armed"] is False


def test_arm_live_trading_true_with_live_strategy_arms(dash, monkeypatch):
    dash_mod, _core, root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    _write_strategy(root / "strategies", "btc_live", execution_mode="live")

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is False
    assert arm["live_trading_enabled"] is True
    assert arm["live_strategies"] == 1
    # Global switch on + a live strategy loaded => armed.
    assert arm["armed"] is True


def test_arm_requires_a_live_strategy(dash, monkeypatch):
    dash_mod, _core, root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")  # switch on
    # Only demo strategies loaded — nothing to arm even with the switch on.
    _write_strategy(root / "strategies", "btc_demo", execution_mode="demo")
    _write_strategy(root / "strategies", "eth_demo", asset="ETHUSDT", execution_mode="demo")

    arm = dash_mod.health_payload()["arm"]
    assert arm["kill_switch_engaged"] is False
    assert arm["live_trading_enabled"] is True
    assert arm["demo_strategies"] == 2
    assert arm["live_strategies"] == 0
    assert arm["armed"] is False


# ---------------------------------------------------------------------------
# Order / reconcile / operator observability panels (read-only).
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_open_orders_panel_folds_non_terminal_only(dash):
    dash_mod, _core, root = dash
    _write_jsonl(root / "logs" / "order-journal.jsonl", [
        {"schema_version": 1, "seq": 0, "ts": "2026-06-28T00:00:00Z", "cl_ord_id": "mxc-a",
         "state": "PLANNED", "prev_state": None, "intent": {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP"}, "detail": {}},
        {"schema_version": 1, "seq": 1, "ts": "2026-06-28T00:00:01Z", "cl_ord_id": "mxc-a",
         "state": "SUBMITTED", "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP"}, "detail": {}},
        {"schema_version": 1, "seq": 2, "ts": "2026-06-28T00:00:02Z", "cl_ord_id": "mxc-b",
         "state": "FILLED", "prev_state": "SUBMITTED", "intent": {"symbol": "ETHUSDT"}, "detail": {}},
    ])

    open_orders, stats = dash_mod.order_journal_open_orders()
    by_cl = {r["cl_ord_id"]: r["state"] for r in open_orders}
    assert by_cl == {"mxc-a": "SUBMITTED"}   # latest non-terminal per clOrdId; FILLED excluded
    assert stats["read"] == 3


def test_open_orders_panel_surfaces_corrupt_count(dash):
    dash_mod, _core, root = dash
    path = root / "logs" / "order-journal.jsonl"
    good = {"schema_version": 1, "seq": 0, "ts": "2026-06-28T00:00:00Z", "cl_ord_id": "mxc-a",
            "state": "UNKNOWN", "prev_state": "SUBMITTED", "intent": {"symbol": "BTCUSDT"}, "detail": {}}
    good2 = {"schema_version": 1, "seq": 2, "ts": "2026-06-28T00:00:02Z", "cl_ord_id": "mxc-c",
             "state": "PLANNED", "prev_state": None, "intent": {"symbol": "XRPUSDT"}, "detail": {}}
    path.write_text(json.dumps(good) + "\n" + "{ broken mid line\n" + json.dumps(good2) + "\n", encoding="utf-8")

    open_orders, stats = dash_mod.order_journal_open_orders()
    assert {r["cl_ord_id"] for r in open_orders} == {"mxc-a", "mxc-c"}
    assert stats["skipped"] >= 1  # corrupt mid-tail line counted, not hidden


def test_open_orders_panel_reads_sealed_records_via_checkpoint(dash):
    # After the order journal rotates, an order whose LATEST record sealed into a
    # segment lives only in the verified checkpoint's index_records -- the live segment
    # no longer carries it. The panel must merge the checkpoint or it goes blind to
    # those open orders. Here mxc-sealed exists ONLY in the checkpoint; mxc-live only
    # in the live tail; mxc-both has a stale checkpoint record superseded by the tail.
    dash_mod, _core, root = dash
    logs = root / "logs"
    checkpoint = {
        "schema_version": 1,
        "checkpoint_version": 1,
        "last_seq": 10,
        "index_records": [
            {"schema_version": 1, "seq": 4, "ts": "2026-06-28T00:00:04Z", "cl_ord_id": "mxc-sealed",
             "state": "SUBMITTED", "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP"}, "detail": {}},
            {"schema_version": 1, "seq": 5, "ts": "2026-06-28T00:00:05Z", "cl_ord_id": "mxc-both",
             "state": "PLANNED", "prev_state": None, "intent": {"symbol": "ETHUSDT"}, "detail": {}},
            {"schema_version": 1, "seq": 6, "ts": "2026-06-28T00:00:06Z", "cl_ord_id": "mxc-done",
             "state": "FILLED", "prev_state": "SUBMITTED", "intent": {"symbol": "XRPUSDT"}, "detail": {}},
        ],
        "origins": [["mxc-sealed", 4, "2026-06-28T00:00:04Z"], ["mxc-both", 5, "2026-06-28T00:00:05Z"]],
    }
    (logs / "order-journal.checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    # Live segment: a fresh order, plus a newer record for mxc-both that wins the merge.
    _write_jsonl(logs / "order-journal.jsonl", [
        {"schema_version": 1, "seq": 11, "ts": "2026-06-28T00:00:11Z", "cl_ord_id": "mxc-both",
         "state": "SUBMITTED", "prev_state": "PLANNED", "intent": {"symbol": "ETHUSDT"}, "detail": {}},
        {"schema_version": 1, "seq": 12, "ts": "2026-06-28T00:00:12Z", "cl_ord_id": "mxc-live",
         "state": "PLANNED", "prev_state": None, "intent": {"symbol": "SOLUSDT"}, "detail": {}},
    ])

    open_orders, stats = dash_mod.order_journal_open_orders()
    by_cl = {r["cl_ord_id"]: r["state"] for r in open_orders}
    # Sealed-only open order is visible; FILLED checkpoint record is excluded;
    # mxc-both reflects the newer live-tail record, not the stale checkpoint one.
    assert by_cl == {"mxc-sealed": "SUBMITTED", "mxc-both": "SUBMITTED", "mxc-live": "PLANNED"}
    assert stats["read"] == 2                 # live-segment rows
    assert stats["checkpoint_records"] == 3   # records merged from the checkpoint index


def test_open_orders_panel_without_checkpoint_reads_live_only(dash):
    # No checkpoint file (pre-rotation box): degrade to live-segment-only, unchanged.
    dash_mod, _core, root = dash
    _write_jsonl(root / "logs" / "order-journal.jsonl", [
        {"schema_version": 1, "seq": 0, "ts": "2026-06-28T00:00:00Z", "cl_ord_id": "mxc-a",
         "state": "SUBMITTED", "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT"}, "detail": {}},
    ])
    open_orders, stats = dash_mod.order_journal_open_orders()
    assert {r["cl_ord_id"] for r in open_orders} == {"mxc-a"}
    assert stats["checkpoint_records"] == 0


def test_reconcile_and_operator_panels_render_in_html(dash):
    dash_mod, _core, root = dash
    # reconcile + operator alerts were consolidated into alerts.jsonl, tagged by kind.
    _write_jsonl(root / "logs" / "alerts.jsonl", [
        {"ts": "2026-06-28T00:00:03Z", "kind": "reconcile", "alert": "RECONCILE_MISMATCH",
         "detail": {"stage": "startup_open_order", "cl_ord_id": "mxc-a", "symbol": "BTCUSDT", "reason": "not_found"}},
        {"ts": "2026-06-28T00:00:04Z", "kind": "operator", "alert": "PLANNED_ORDER_ABANDONED", "severity": "warning",
         "detail": {"cl_ord_id": "mxc-x", "reason": "never_submitted", "age_s": 400}},
    ])

    html = dash_mod.render()
    assert "Order &amp; Reconcile State" in html
    assert "RECONCILE_MISMATCH" in html
    assert "PLANNED_ORDER_ABANDONED" in html
    # Read-only: tab-nav <button>s exist, but no submit/execute/cancel ACTION controls.
    for forbidden in ("<form", "submit_order", "/execute", "cancel_order", 'method="post"'):
        assert forbidden not in html.lower(), f"dashboard must stay read-only, found {forbidden!r}"


def test_api_payload_exposes_order_reconcile_operator(dash):
    dash_mod, _core, root = dash
    _write_jsonl(root / "logs" / "order-journal.jsonl", [
        {"schema_version": 1, "seq": 0, "ts": "2026-06-28T00:00:00Z", "cl_ord_id": "mxc-a",
         "state": "SUBMITTED", "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT"}, "detail": {}},
    ])
    _bust_cache(dash_mod)
    api = dash_mod.api_payload()
    assert "open_orders" in api and "reconcile_alerts" in api and "operator_alerts" in api
    assert len(api["open_orders"]["rows"]) == 1
    assert "stats" in api["open_orders"]
