"""Phase A robustness gates — test-first (HERMX_ROBUSTNESS_EXECUTION_PLAN.md §Phase A).

Covers three ship-first pure-gate items, every one flag-gated OFF by default:

  A1  Pre-trade notional cap — refuse any order whose ``planned_notional_usd``
      exceeds an INDEPENDENT absolute ceiling (``capital.max_notional_usd`` per
      strategy and/or the global ``HERMX_MAX_NOTIONAL_USD`` env). Unset => no cap,
      byte-identical to today.
  A1b Silent under-execution alert — when every order leg clamps to size 0
      (sub-min notional) the strategy silently under-executes; emit a WARNING so
      the operator is not left guessing at a buried ``zero_size`` REJECT.
  A2  Global ``trading_state`` (active | reducing) — one operator toggle puts the
      whole system into close-only. A close (``close_only``) NEVER blocks.

All gates are exercised through the PRODUCTION ExecutionService via the real
``_run_execution_service`` hook wiring (``wr.execute_if_enabled``), never a
re-implemented gate body (code-quality.md anti-pattern). Offline: the executor
factory is mocked so nothing can reach a venue.
"""
from __future__ import annotations

import http.client
import importlib
import json
import logging
import os
from unittest import mock

import pytest

import execution.service as svc
from conftest import adapter_result, fake_executor, serve_dashboard as _serve


# ===========================================================================
# Helpers
# ===========================================================================

def _armed_record(
    cl: str,
    *,
    planned=1500.0,
    strategy_id="strat-A",
    actions=("OPEN_LONG",),
    close_only=False,
    include_planned=True,
) -> dict:
    """A minimal armed record for ExecutionService.execute via execute_if_enabled.

    ``planned`` sets execution_intent.planned_notional_usd; set include_planned
    False to omit the key entirely (the None/missing fail-safe path)."""
    intent = {
        "policy": "weighted_v1",
        "client_order_id": cl,
        "actions": list(actions),
    }
    if include_planned:
        intent["planned_notional_usd"] = planned
    readiness = {
        "live_execution_enabled": True,
        "symbol": "XRPUSDT",
        "signal_side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "strategy_id": strategy_id,
        "execution_intent": intent,
        "okx_fill": {"client_order_id": cl},
        "block_reason": None,
    }
    if close_only:
        readiness["close_only"] = True
    return {
        "received_at": "2026-07-03T00:00:00Z",
        "auth_healthy": True,
        "execution_readiness": readiness,
    }


def _inject_strategy(wr, monkeypatch, strategy_id, *, max_notional_usd=None):
    """Register a strategy so the pretrade ceiling resolver can read its capital cap."""
    capital = {"budget_usd": 1500}
    if max_notional_usd is not None:
        capital["max_notional_usd"] = max_notional_usd
    monkeypatch.setitem(
        wr.STRATEGIES,
        strategy_id,
        {"strategy_id": strategy_id, "asset": "XRPUSDT", "capital": capital},
    )


def _fake_ok():
    return fake_executor(adapter_result(client_order_id="cid", payload={"symbol": "XRP/USDT:USDT"}))


# ===========================================================================
# A1 — pre-trade notional cap gate
# ===========================================================================

def test_pretrade_gate_blocks_oversized_notional(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 5000.0)
    cl = "phaseastablenotionalblock0000001"
    rec = _armed_record(cl, planned=10000.0)

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["gate"] == "pretrade_notional"
    assert out["reason"].startswith("notional_exceeds_max:")
    # A blocked pre-trade gate writes NO order-journal row (returns before write-ahead).
    assert wr.latest_order_record(cl) is None


def test_pretrade_gate_passes_normal_order(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 5000.0)
    cl = "phaseastablenotionalpass00000001"
    rec = _armed_record(cl, planned=3000.0)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"
    # Passing the gate reached the write-ahead journal (PLANNED -> SUBMITTED).
    assert wr.latest_order_record(cl) is not None


def test_pretrade_gate_disabled_when_env_unset(wr, monkeypatch):
    # No env cap (inf) and no per-strategy cap -> any size passes.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastablenotionaloff000000001"
    rec = _armed_record(cl, planned=1_000_000_000.0)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_pretrade_gate_uses_min_of_config_and_env(wr, monkeypatch):
    # Config cap 500, env cap 300 -> effective ceiling 300 (min wins); planned 400 blocks.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 300.0)
    _inject_strategy(wr, monkeypatch, "strat-min", max_notional_usd=500.0)
    cl = "phaseastablenotionalmin000000001"
    rec = _armed_record(cl, planned=400.0, strategy_id="strat-min")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["gate"] == "pretrade_notional"
    assert "300.00" in out["reason"]  # ceiling is the smaller (env), not 500


def test_pretrade_gate_per_strategy_tightens_global(wr, monkeypatch):
    # Global 50000, per-strategy 4000 -> ceiling 4000 (min); planned 6000 blocks.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 50000.0)
    _inject_strategy(wr, monkeypatch, "strat-tight", max_notional_usd=4000.0)
    cl = "phaseastablenotionaltight0000001"
    rec = _armed_record(cl, planned=6000.0, strategy_id="strat-tight")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["gate"] == "pretrade_notional"
    assert "4000.00" in out["reason"]


def test_pretrade_gate_none_planned_notional_passes(wr, monkeypatch):
    # planned_notional_usd missing -> fail-safe pass (never block on unknown size).
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 100.0)
    cl = "phaseastablenotionalnone00000001"
    rec = _armed_record(cl, include_planned=False)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_pretrade_gate_zero_ceiling_treated_as_unset(wr, monkeypatch):
    # max_notional_usd=0 means UNSET (no cap), never "block everything".
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    _inject_strategy(wr, monkeypatch, "strat-zero", max_notional_usd=0)
    cl = "phaseastablenotionalzero00000001"
    rec = _armed_record(cl, planned=1_000_000_000.0, strategy_id="strat-zero")

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_pretrade_gate_zero_env_treated_as_unset(wr, monkeypatch):
    # A bad env (HERMX_MAX_NOTIONAL_USD=0) must NOT block all orders.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", 0.0)
    cl = "phaseastablenotionalenvzero00001"
    rec = _armed_record(cl, planned=1_000_000_000.0)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


# ===========================================================================
# A1b — silent under-execution (zero-size) alert
# ===========================================================================

def _zero_size_result(cl, reason="zero_size"):
    return {
        "ok": False,
        "mode": "submit_failed",
        "exchange": "ccxt",
        "elapsed_ms": 3,
        "fill_summary": {"status": "dry_run", "order_id": None, "client_order_id": cl},
        "payload": {
            "executed_orders": [
                {"action": "OPEN_LONG", "submitted": False, "status": "skipped", "reason": reason},
            ],
            "symbol": "XRP/USDT:USDT",
        },
    }


def test_zero_size_order_emits_warning_log(wr, monkeypatch, caplog):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastablezerosizewarn00000001"
    rec = _armed_record(cl)

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_zero_size_result(cl))
    with caplog.at_level(logging.WARNING):
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
            out = wr.execute_if_enabled(rec)

    # Clean terminal REJECTED (unchanged) but now the under-execution is surfaced.
    assert out["mode"] == "submit_failed"
    assert any("under_execution" in r.getMessage() for r in caplog.records)


def test_zero_size_alert_payload_reason_unchanged(wr, monkeypatch):
    # Pins the ORIGINAL alert payload shape: zero_size legs must still emit
    # stage=under_execution / reason=zero_size_below_min, byte-identical to before
    # the insufficient_balance extension.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastablezerosizepayload00001"
    rec = _armed_record(cl)

    emitted = []
    monkeypatch.setattr(wr, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_zero_size_result(cl))
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        wr.execute_if_enabled(rec)

    assert emitted == [
        (wr.RECONCILE_ALERT_MISMATCH,
         {"stage": "under_execution", "reason": "zero_size_below_min", "cl_ord_id": cl}),
    ]


def test_insufficient_balance_skip_emits_distinct_alert(wr, monkeypatch, caplog):
    # A1b extension (NAUTILUS_GAP_REMEDIATION_PLAN §0.6 Item A 1.3): the live-mode
    # insufficient_balance skip is under-execution too, and its alert must be
    # distinguishable from zero_size for the operator.
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastableinsuffbalance000001"
    rec = _armed_record(cl)

    emitted = []
    monkeypatch.setattr(wr, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_zero_size_result(cl, reason="insufficient_balance"))
    with caplog.at_level(logging.WARNING):
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
            out = wr.execute_if_enabled(rec)

    # Clean terminal REJECTED (unchanged) but the under-execution is surfaced.
    assert out["mode"] == "submit_failed"
    assert any("under_execution" in r.getMessage() for r in caplog.records)
    assert emitted == [
        (wr.RECONCILE_ALERT_MISMATCH,
         {"stage": "under_execution", "reason": "insufficient_balance", "cl_ord_id": cl}),
    ]


def test_below_instrument_min_skip_still_fires_a1b_alert(wr, monkeypatch, caplog):
    # A1b extension (NAUTILUS_GAP_REMEDIATION_PLAN §0.6 Item A 1.4): the adapter
    # now skips sub-minimum orders with reason=below_instrument_min instead of
    # zero_size. The under-execution alert must keep firing for it (it is still
    # a sub-min under-execution, so the alert reason stays zero_size_below_min).
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastablebelowinstmin00000001"
    rec = _armed_record(cl)

    emitted = []
    monkeypatch.setattr(wr, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_zero_size_result(cl, reason="below_instrument_min"))
    with caplog.at_level(logging.WARNING):
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
            out = wr.execute_if_enabled(rec)

    assert out["mode"] == "submit_failed"
    assert any("under_execution" in r.getMessage() for r in caplog.records)
    assert emitted == [
        (wr.RECONCILE_ALERT_MISMATCH,
         {"stage": "under_execution", "reason": "zero_size_below_min", "cl_ord_id": cl}),
    ]


def test_no_alert_on_normal_reject(wr, monkeypatch, caplog):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    cl = "phaseastablenormalreject00000001"
    rec = _armed_record(cl)

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_zero_size_result(cl, reason="already_long_no_pyramid"))
    with caplog.at_level(logging.WARNING):
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
            wr.execute_if_enabled(rec)

    assert not any("under_execution" in r.getMessage() for r in caplog.records)


# ===========================================================================
# A2 — global trading_state gate (control-state storage + helpers)
# ===========================================================================

def _read_control_state(module) -> dict:
    return json.loads(module.CONTROL_STATE_FILE.read_text(encoding="utf-8"))


def test_trading_state_defaults_to_active(wr):
    assert wr.get_trading_state() == "active"
    # And the default control-state carries the key (or the merge would drop it).
    assert wr.default_control_state()["trading_state"] == "active"


def test_trading_state_survives_control_state_merge(wr):
    # Regression guard: an OLD control-state.json missing trading_state must load
    # as "active" (the {k in default} merge drops unknown keys — same class of bug
    # that once dropped accounting_windows).
    state = wr.default_control_state()
    state.pop("trading_state", None)
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    assert wr.load_control_state()["trading_state"] == "active"
    assert wr.get_trading_state() == "active"


def test_set_trading_state_validates_input(wr):
    assert wr.set_trading_state("bogus") is False
    assert wr.get_trading_state() == "active"  # rejected write did not take effect
    assert wr.set_trading_state("reducing") is True
    assert wr.get_trading_state() == "reducing"
    assert _read_control_state(wr)["trading_state"] == "reducing"


def test_trading_state_persists_round_trip(wr):
    assert wr.set_trading_state("reducing") is True
    assert wr.load_control_state()["trading_state"] == "reducing"
    assert wr.clear_trading_state() is True
    assert wr.load_control_state()["trading_state"] == "active"


def test_reducing_state_blocks_reversal(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    assert wr.set_trading_state("reducing") is True
    cl = "phaseastabletradingblockrev00001"
    rec = _armed_record(cl, actions=["OPEN_LONG"])  # a reversal/open, not close_only

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["gate"] == "trading_state"
    assert out["reason"] == "trading_state_reducing:reversal_blocked"
    assert wr.latest_order_record(cl) is None  # no journal row on block


def test_reducing_state_passes_close_only(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    assert wr.set_trading_state("reducing") is True
    cl = "phaseastabletradingcloseonly0001"
    rec = _armed_record(cl, actions=["CLOSE_LONG"], close_only=True)

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    # A close is NEVER blocked by trading_state (HermX never-block-a-close invariant).
    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_active_state_passes_all(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    assert wr.get_trading_state() == "active"  # default
    cl = "phaseastabletradingactivepass001"
    rec = _armed_record(cl, actions=["OPEN_LONG"])

    fake = _fake_ok()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(rec)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


# ===========================================================================
# A2 — dashboard endpoint + /api exposure
# ===========================================================================

_DASH_TEMPLATE = {
    "schema_version": 2,
    "name": "Dash Strategy",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "timeframe": "2h",
    "budget_usd": 1500,
    "leverage": 2,
    "margin_mode": "isolated",
    "execution_mode": "demo",
    "submit_orders": True,
}


@pytest.fixture
def dash(tmp_path, monkeypatch):
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    row = dict(_DASH_TEMPLATE, strategy_id="dash-demo")
    (strategies_dir / "dash-demo.json").write_text(json.dumps(row), encoding="utf-8")

    orig_root = os.environ.get("HERMX_ROOT")
    os.environ["HERMX_ROOT"] = str(root)
    orig_secret = os.environ.get("HERMX_SECRET")
    os.environ["HERMX_SECRET"] = "test-secret"

    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)

    dash_mod.okx_live_snapshot = lambda config, simulated_trading=True: {
        "ok": True, "positions": {}, "account": {}, "error": None,
        "generated_at": "2026-07-03T00:00:00Z",
    }
    monkeypatch.setattr(dash_mod, "okx_order_history_snapshot", lambda config: {"ok": False})
    # Per-(venue,mode) strategy snapshots (dashboard/model.py:404,407) build a real
    # executor and hit the venue over HTTP whenever a strategy file exists on disk.
    # Stub them offline like okx_order_history_snapshot above.
    monkeypatch.setattr(
        dash_mod,
        "strategy_live_snapshot",
        lambda strategy_config, mode: {
            "ok": True, "positions": {}, "account": {}, "error": None,
            "generated_at": "2026-07-03T00:00:00Z",
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
        yield dash_mod
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root
        else:
            os.environ.pop("HERMX_ROOT", None)
        if orig_secret is not None:
            os.environ["HERMX_SECRET"] = orig_secret
        else:
            os.environ.pop("HERMX_SECRET", None)


def _request(port, method, path, *, token="test-secret", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if token is not None:
        headers["X-Dashboard-Token"] = token
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_trading_state_api_roundtrip(dash):
    dash_mod = dash
    # Default exposure is "active".
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    assert dash_mod.api_payload()["trading_state"] == "active"

    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/trading-state",
                                 body={"state": "reducing"})
        assert status == 200
        assert json.loads(data)["trading_state"] == "reducing"

        # GET /api reflects the new state.
        gstatus, gdata = _request(port, "GET", "/api")
        assert gstatus == 200
        assert json.loads(gdata)["trading_state"] == "reducing"

        # DELETE resets to active.
        dstatus, ddata = _request(port, "DELETE", "/api/control/trading-state")
        assert dstatus == 200
        assert json.loads(ddata)["trading_state"] == "active"

    assert dash_mod._get_trading_state() == "active"


def test_trading_state_api_rejects_invalid(dash):
    dash_mod = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/trading-state",
                             body={"state": "halted"})
    assert status == 400
    assert dash_mod._get_trading_state() == "active"


def test_trading_state_api_requires_token(dash):
    dash_mod = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/trading-state",
                             token=None, body={"state": "reducing"})
    assert status == 401
    assert dash_mod._get_trading_state() == "active"
