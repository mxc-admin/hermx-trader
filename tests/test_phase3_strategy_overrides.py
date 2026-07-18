"""Phase 3 — per-strategy execution-mode override system.

Covers the new override path end to end:

  * webhook_receiver.set_strategy_override / clear_strategy_override / the
    load_control_state sanitization that backs them (control-state.json is the
    single shared artifact between the receiver and the dashboard);
  * build_strategy_execution_readiness reading the override live per-signal and
    forcing execution_mode (and the kill-switch interaction at the gate);
  * the dashboard.py POST/DELETE /api/control/strategy/{id} write endpoint and
    api_payload()'s effective_mode derivation.

Offline and deterministic. The receiver tests use the conftest ``wr`` fixture
(webhook_receiver reloaded against an isolated temp HERMX_ROOT, so
CONTROL_STATE_FILE lands in tmp and never touches real runtime state). The
dashboard tests reload dashboard/dashboard_core against their own temp root and
exercise the real Handler over a loopback ThreadingHTTPServer on an ephemeral
port. No network, no exchange, nothing can arm a real submission.

Flag mapping under test (must stay identical in both modules):
    pause -> {execution_mode: demo, submit_orders: False}
    demo  -> {execution_mode: demo, submit_orders: True}
    live  -> {execution_mode: live, submit_orders: True}

``submit_orders`` is the submission gate: Pause validates+ledgers but submits NO
order to either venue; Demo and Live both submit, and ``execution_mode`` selects the
sandbox vs the real account. The legacy UI labels are accepted and normalized for
backward compatibility with control-state.json written before the rename: "shadow"
was the old pause concept -> "pause"; "paper" was the sandbox-submit concept -> "demo".
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
from pathlib import Path

import pytest

from conftest import serve_dashboard as _serve



# ===========================================================================
# Helpers
# ===========================================================================

def _read_control_state(module) -> dict:
    """Read the raw control-state.json the module is bound to (no defaulting)."""
    return json.loads(module.CONTROL_STATE_FILE.read_text(encoding="utf-8"))


def _strategy_record(strategy_id: str, *, execution_mode: str, submit_orders: bool) -> dict:
    """A minimal but realistic record for build_strategy_execution_readiness.

    tv_time/strategy_id vary per-test so the derived client_order_id is unique
    (the order journal dedupes on it, which matters for the gate test).
    """
    return {
        "strategy_id": strategy_id,
        "strategy_config": {
            "strategy_id": strategy_id,
            "name": "Test Strategy",
            "asset": "BTCUSDT",
            "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
            "timeframe": "2h",
            "execution_mode": execution_mode,
            "submit_orders": submit_orders,
            "budget_usd": 1500,
            "leverage": 2,
            "margin_mode": "isolated",
        },
        "normalized": {
            "strategy_id": strategy_id,
            "symbol": "BTCUSDT",
            "side": "buy",
            "timeframe": "2h",
            "tv_time": f"2026-06-25T00:00:00Z|{strategy_id}",
            "tv_signal_price": 50000.0,
        },
    }


# ===========================================================================
# Section 1 — webhook_receiver backend functions
# ===========================================================================

@pytest.mark.parametrize(
    "mode,expected",
    [
        ("pause", {"execution_mode": "demo", "submit_orders": False}),
        ("demo", {"execution_mode": "demo", "submit_orders": True}),
        ("live", {"execution_mode": "live", "submit_orders": True}),
    ],
)
def test_set_strategy_override_roundtrip(wr, mode, expected):
    assert wr.set_strategy_override("strat-A", mode) is True

    entry = _read_control_state(wr)["strategy_overrides"]["strat-A"]
    assert entry["mode"] == mode
    assert entry["execution_mode"] == expected["execution_mode"]
    # 3-mode model stores BOTH the account (execution_mode) and the submission gate.
    assert entry["submit_orders"] == expected["submit_orders"]
    assert "set_at" in entry  # timestamped for the operator UI


def test_set_strategy_override_invalid_mode_no_write(wr):
    # File doesn't exist yet; a rejected mode must not create or mutate it.
    assert not wr.CONTROL_STATE_FILE.exists()
    assert wr.set_strategy_override("strat-A", "bogus") is False
    assert not wr.CONTROL_STATE_FILE.exists()


def test_set_strategy_override_empty_id_no_write(wr):
    assert not wr.CONTROL_STATE_FILE.exists()
    assert wr.set_strategy_override("", "demo") is False
    assert wr.set_strategy_override("   ", "demo") is False
    assert not wr.CONTROL_STATE_FILE.exists()


def test_set_strategy_override_overwrites_existing(wr):
    assert wr.set_strategy_override("strat-A", "demo") is True
    assert wr.set_strategy_override("strat-A", "live") is True

    overrides = _read_control_state(wr)["strategy_overrides"]
    assert overrides["strat-A"]["mode"] == "live"
    assert overrides["strat-A"]["execution_mode"] == "live"
    # Replaced, not appended -> still exactly one entry for the id.
    assert list(overrides.keys()) == ["strat-A"]


def test_clear_strategy_override_existing(wr):
    wr.set_strategy_override("strat-A", "demo")
    assert wr.clear_strategy_override("strat-A") is True
    assert "strat-A" not in _read_control_state(wr)["strategy_overrides"]


def test_clear_strategy_override_nonexistent(wr):
    # No override present -> returns False, does not raise, leaves state intact.
    wr.set_strategy_override("strat-A", "demo")
    assert wr.clear_strategy_override("strat-MISSING") is False
    assert "strat-A" in _read_control_state(wr)["strategy_overrides"]


def test_clear_strategy_override_empty_id(wr):
    assert wr.clear_strategy_override("") is False


def test_load_control_state_coerces_malformed_overrides(wr):
    state = wr.default_control_state()
    state["strategy_overrides"] = "bad"  # string instead of dict
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    assert wr.load_control_state()["strategy_overrides"] == {}


def test_load_control_state_missing_field_defaults_to_empty(wr):
    state = wr.default_control_state()
    state.pop("strategy_overrides", None)
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    assert wr.load_control_state()["strategy_overrides"] == {}


@pytest.mark.parametrize("legacy,expected", [("shadow", "pause"), ("pause", "pause"), ("paper", "demo")])
def test_load_control_state_remaps_legacy_label(wr, legacy, expected):
    # Backward compat: a control-state.json written before the rename may carry a
    # legacy "mode". On load the label normalizes: shadow -> pause, paper -> demo,
    # pause stays pause. Only the display label is touched, not execution_mode.
    state = wr.default_control_state()
    state["strategy_overrides"] = {
        "strat-A": {"mode": legacy, "execution_mode": "demo", "set_at": "x"},
    }
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    entry = wr.load_control_state()["strategy_overrides"]["strat-A"]
    assert entry["mode"] == expected
    assert entry["execution_mode"] == "demo"


@pytest.mark.parametrize(
    "legacy,expected_mode,expected_flags",
    [
        ("shadow", "pause", {"execution_mode": "demo", "submit_orders": False}),
        ("paper", "demo", {"execution_mode": "demo", "submit_orders": True}),
    ],
)
def test_set_strategy_override_accepts_legacy_label(wr, legacy, expected_mode, expected_flags):
    # Legacy input labels normalize on write: shadow -> pause, paper -> demo, and the
    # corresponding execution_mode/submit_orders flags are stored.
    assert wr.set_strategy_override("strat-A", legacy) is True
    entry = _read_control_state(wr)["strategy_overrides"]["strat-A"]
    assert entry["mode"] == expected_mode
    assert entry["execution_mode"] == expected_flags["execution_mode"]
    assert entry["submit_orders"] == expected_flags["submit_orders"]


# ===========================================================================
# Section 2 — build_strategy_execution_readiness override behavior
# ===========================================================================

def test_readiness_demo_override_keeps_submit_sandboxed(wr):
    # File says demo; demo override -> demo mode, submission ON (both modes submit),
    # sandboxed.
    wr.set_strategy_override("strat-A", "demo")
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-A", execution_mode="demo", submit_orders=True)
    )
    assert rd["execution_mode"] == "demo"
    assert rd["live_execution_enabled"] is True
    assert rd["simulated_trading"] is True


def test_readiness_live_override_forces_live(wr):
    # File says demo + submit; live override -> live mode, submission ON, not sandboxed.
    wr.set_strategy_override("strat-A", "live")
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-A", execution_mode="demo", submit_orders=True)
    )
    assert rd["execution_mode"] == "live"
    assert rd["live_execution_enabled"] is True
    assert rd["simulated_trading"] is False


def test_readiness_demo_override_downgrades_live_file(wr):
    # File says live + submit; demo override downgrades the venue to sandbox.
    wr.set_strategy_override("strat-A", "demo")
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-A", execution_mode="live", submit_orders=True)
    )
    assert rd["execution_mode"] == "demo"
    assert rd["simulated_trading"] is True
    assert rd["live_execution_enabled"] is True


def test_readiness_pause_override_disables_submission(wr):
    # File says demo + submit; pause override -> demo account, submission OFF.
    wr.set_strategy_override("strat-A", "pause")
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-A", execution_mode="demo", submit_orders=True)
    )
    assert rd["execution_mode"] == "demo"
    assert rd["live_execution_enabled"] is False
    assert rd["simulated_trading"] is True


def test_readiness_no_override_uses_strategy_file(wr):
    # No override for this id -> the strategy file values pass through unchanged.
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-A", execution_mode="demo", submit_orders=True)
    )
    assert rd["execution_mode"] == "demo"
    assert rd["live_execution_enabled"] is True
    assert rd["simulated_trading"] is True

    # submit_orders=False in the file is Pause: validates+ledgers but submits NO order
    # (live_execution_enabled False), still sandbox-routed.
    rd_off = wr.build_strategy_execution_readiness(
        _strategy_record("strat-B", execution_mode="demo", submit_orders=False)
    )
    assert rd_off["live_execution_enabled"] is False
    assert rd_off["simulated_trading"] is True


def test_live_override_still_blocked_by_kill_switch(wr, monkeypatch):
    """A live override builds a live, non-simulated readiness, but Gate 3 in the
    ExecutionService refuses it while HERMX_LIVE_TRADING is off (fail-closed)."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    wr.set_strategy_override("strat-LIVE", "live")
    rd = wr.build_strategy_execution_readiness(
        _strategy_record("strat-LIVE", execution_mode="demo", submit_orders=False)
    )
    # Readiness reflects the live override...
    assert rd["execution_mode"] == "live"
    assert rd["simulated_trading"] is False
    assert rd["live_execution_enabled"] is True

    # ...but the gate blocks and never builds an executor (kill switch off).
    from unittest import mock

    record = {"received_at": "2026-06-25T00:00:00Z", "execution_readiness": rd}
    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(record)

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


# ===========================================================================
# Section 3 — dashboard.py POST/DELETE endpoint + api_payload effective_mode
# ===========================================================================

STRATEGY_TEMPLATE = {
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


def _write_strategy(strategies_dir: Path, strategy_id: str, **overrides) -> None:
    row = dict(STRATEGY_TEMPLATE)
    row["strategy_id"] = strategy_id
    row.update(overrides)
    (strategies_dir / f"{strategy_id}.json").write_text(json.dumps(row), encoding="utf-8")


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """dashboard + dashboard_core reloaded against a fresh temp HERMX_ROOT, with
    one active demo strategy on disk and the executor stubbed offline."""
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    _write_strategy(strategies_dir, "dash-demo", execution_mode="demo", submit_orders=True)

    orig_root = os.environ.get("HERMX_ROOT")
    os.environ["HERMX_ROOT"] = str(root)

    # dashboard_core/dashboard read DASH_AUTH_TOKEN from HERMX_SECRET at import
    # time. run.sh exports a real secret before pytest, so force the test value
    # here (setdefault in conftest won't override an already-set env var).
    orig_secret = os.environ.get("HERMX_SECRET")
    os.environ["HERMX_SECRET"] = "test-secret"

    import dashboard_core as core
    importlib.reload(core)
    import dashboard as dash_mod
    importlib.reload(dash_mod)

    # Offline executor stub so api_payload()/dashboard_model() never touch network.
    # Accepts the Phase-0 simulated_trading kwarg: dashboard_model reads a separate
    # snapshot for live strategies, so the stub must mirror the real 2-arg signature.
    dash_mod.okx_live_snapshot = lambda config, simulated_trading=True: {
        "ok": True, "positions": {}, "account": {}, "error": None,
        "generated_at": "2026-06-25T00:00:00Z",
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
            "generated_at": "2026-06-25T00:00:00Z",
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
        yield dash_mod, strategies_dir, root
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
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


def _bust(dash_mod):
    dash_mod._MODEL_CACHE["expires_at"] = 0.0
    dash_mod._MODEL_CACHE["model"] = None


def test_post_valid_mode_writes_override(dash, monkeypatch):
    dash_mod, _strategies_dir, _root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/strategy/dash-demo",
                                 body={"mode": "live"})
    assert status == 200
    assert json.loads(data)["mode"] == "live"

    overrides = dash_mod._load_control_state()["strategy_overrides"]
    assert overrides["dash-demo"]["mode"] == "live"
    assert overrides["dash-demo"]["execution_mode"] == "live"


def test_post_live_mode_locked_returns_403_when_kill_switch_engaged(dash, monkeypatch):
    # Server-side live lock: the UI disables the Live pill client-side, but the
    # endpoint itself must refuse a live override while HERMX_LIVE_TRADING is off.
    dash_mod, _strategies_dir, _root = dash
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/strategy/dash-demo",
                                 body={"mode": "live"})
    assert status == 403
    assert "HERMX_LIVE_TRADING" in json.loads(data)["error"]
    assert dash_mod._load_control_state().get("strategy_overrides", {}) == {}

    # pause/demo overrides stay writable while locked.
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/dash-demo",
                             body={"mode": "demo"})
    assert status == 200


def test_delete_clears_override(dash):
    dash_mod, _strategies_dir, _root = dash
    dash_mod._set_strategy_override("dash-demo", "live")
    assert "dash-demo" in dash_mod._load_control_state()["strategy_overrides"]

    with _serve(dash_mod) as port:
        status, _ = _request(port, "DELETE", "/api/control/strategy/dash-demo")
    assert status == 200
    assert "dash-demo" not in dash_mod._load_control_state().get("strategy_overrides", {})


def test_post_invalid_mode_returns_400(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/dash-demo",
                             body={"mode": "bogus"})
    assert status == 400
    assert dash_mod._load_control_state().get("strategy_overrides", {}) == {}


def test_post_unknown_strategy_returns_404(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/no-such-strategy",
                             body={"mode": "demo"})
    assert status == 404


def test_post_without_token_returns_401(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/dash-demo",
                             token=None, body={"mode": "demo"})
    assert status == 401
    # Auth failed before any write.
    assert dash_mod._load_control_state().get("strategy_overrides", {}) == {}


def test_delete_without_token_returns_401(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "DELETE", "/api/control/strategy/dash-demo", token=None)
    assert status == 401


@pytest.mark.parametrize(
    "execution_mode,submit_orders,expected",
    [
        ("demo", False, "pause"),  # submit_orders False -> Pause regardless of account
        ("live", False, "pause"),
        ("demo", True, "demo"),
        ("live", True, "live"),
    ],
)
def test_api_payload_effective_mode_from_file(dash, execution_mode, submit_orders, expected):
    dash_mod, strategies_dir, _root = dash
    _write_strategy(strategies_dir, "eff", execution_mode=execution_mode, submit_orders=submit_orders)
    _bust(dash_mod)

    payload = dash_mod.api_payload()
    row = next(s for s in payload["strategies"] if s["strategy_id"] == "eff")
    assert row["effective_mode"] == expected


def test_api_payload_effective_mode_defaults_to_demo(dash, monkeypatch):
    # With no override and no execution_mode on the row, effective_mode falls back
    # to "demo" (the 2-mode default).
    dash_mod, _strategies_dir, _root = dash
    monkeypatch.setattr(dash_mod, "active_strategies",
                        lambda *a, **k: [{"strategy_id": "off"}])
    _bust(dash_mod)

    payload = dash_mod.api_payload()
    row = next(s for s in payload["strategies"] if s["strategy_id"] == "off")
    assert row["effective_mode"] == "demo"


def test_api_payload_effective_mode_override_wins(dash):
    # Override takes precedence over the strategy file's own mode.
    dash_mod, _strategies_dir, _root = dash
    dash_mod._set_strategy_override("dash-demo", "live")  # file is demo
    _bust(dash_mod)

    payload = dash_mod.api_payload()
    row = next(s for s in payload["strategies"] if s["strategy_id"] == "dash-demo")
    assert row["effective_mode"] == "live"
    assert payload["strategy_overrides"]["dash-demo"]["mode"] == "live"


def test_api_payload_remaps_legacy_shadow_override_to_pause(dash):
    # A legacy control-state.json override with "mode": "shadow" surfaces as "pause"
    # (shadow was the old pause concept) in both the per-strategy effective_mode and
    # the raw strategy_overrides payload.
    dash_mod, _strategies_dir, _root = dash
    dash_mod.CONTROL_STATE_FILE.write_text(json.dumps({
        "strategy_overrides": {
            "dash-demo": {"mode": "shadow", "execution_mode": "demo", "set_at": "x"},
        },
    }), encoding="utf-8")
    _bust(dash_mod)

    payload = dash_mod.api_payload()
    row = next(s for s in payload["strategies"] if s["strategy_id"] == "dash-demo")
    assert row["effective_mode"] == "pause"
    assert payload["strategy_overrides"]["dash-demo"]["mode"] == "pause"


# ===========================================================================
# Section 4 — Phase 3 accounting windows (receiver storage + helpers)
# ===========================================================================

def test_set_accounting_start_roundtrip(wr):
    assert wr.set_accounting_start("strat-A", 1704067200000) is True
    entry = _read_control_state(wr)["accounting_windows"]["strat-A"]
    assert entry["accounting_start_at"] == 1704067200000
    assert "set_at" in entry
    assert wr.accounting_start_for("strat-A") == 1704067200000


def test_set_accounting_start_none_clears(wr):
    wr.set_accounting_start("strat-A", 1704067200000)
    # None clears the window (delegates to clear_accounting_start).
    assert wr.set_accounting_start("strat-A", None) is True
    assert "strat-A" not in _read_control_state(wr).get("accounting_windows", {})
    assert wr.accounting_start_for("strat-A") is None


def test_clear_accounting_start(wr):
    wr.set_accounting_start("strat-A", 999)
    assert wr.clear_accounting_start("strat-A") is True
    assert wr.clear_accounting_start("strat-A") is False  # idempotent no-op
    assert wr.accounting_start_for("strat-A") is None


def test_set_accounting_start_invalid_rejected(wr):
    assert wr.set_accounting_start("", 100) is False
    assert wr.set_accounting_start("strat-A", "not-an-int") is False
    assert wr.set_accounting_start("strat-A", -5) is False
    assert not wr.CONTROL_STATE_FILE.exists() or \
        "strat-A" not in _read_control_state(wr).get("accounting_windows", {})


def test_accounting_windows_does_not_disturb_overrides(wr):
    # Accounting window is additive: setting it leaves strategy_overrides intact.
    wr.set_strategy_override("strat-A", "live")
    wr.set_accounting_start("strat-A", 500)
    state = _read_control_state(wr)
    assert state["strategy_overrides"]["strat-A"]["mode"] == "live"
    assert state["accounting_windows"]["strat-A"]["accounting_start_at"] == 500


def test_load_control_state_preserves_accounting_windows(wr):
    # Regression: load_control_state()'s "keep only default keys" merge must NOT drop
    # accounting_windows (it is re-attached explicitly, like strategy_overrides).
    state = wr.default_control_state()
    state["accounting_windows"] = {"strat-A": {"accounting_start_at": 42, "set_at": "x"}}
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    loaded = wr.load_control_state()
    assert loaded["accounting_windows"]["strat-A"]["accounting_start_at"] == 42


def test_load_control_state_coerces_malformed_accounting_windows(wr):
    state = wr.default_control_state()
    state["accounting_windows"] = "bad"  # not a dict
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    assert wr.load_control_state()["accounting_windows"] == {}


# ===========================================================================
# Section 5 — Phase 3 accounting windows (dashboard endpoint + api_payload)
# ===========================================================================

def test_post_sets_accounting_start_at(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/strategy/dash-demo",
                                 body={"accounting_start_at": 1704067200000})
    assert status == 200
    assert json.loads(data)["accounting_start_at"] == 1704067200000
    windows = dash_mod._load_control_state()["accounting_windows"]
    assert windows["dash-demo"]["accounting_start_at"] == 1704067200000


def test_post_accounting_start_null_clears(dash):
    dash_mod, _strategies_dir, _root = dash
    dash_mod._set_accounting_start("dash-demo", 500)
    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/strategy/dash-demo",
                                 body={"accounting_start_at": None})
    assert status == 200
    assert json.loads(data)["accounting_start_at"] is None
    assert "dash-demo" not in dash_mod._load_control_state().get("accounting_windows", {})


def test_post_mode_and_accounting_together(dash, monkeypatch):
    dash_mod, _strategies_dir, _root = dash
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    with _serve(dash_mod) as port:
        status, data = _request(port, "POST", "/api/control/strategy/dash-demo",
                                 body={"mode": "live", "accounting_start_at": 123})
    assert status == 200
    resp = json.loads(data)
    assert resp["mode"] == "live"
    assert resp["accounting_start_at"] == 123
    state = dash_mod._load_control_state()
    assert state["strategy_overrides"]["dash-demo"]["mode"] == "live"
    assert state["accounting_windows"]["dash-demo"]["accounting_start_at"] == 123


def test_post_accounting_start_invalid_returns_400(dash):
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/dash-demo",
                             body={"accounting_start_at": "nope"})
    assert status == 400
    assert dash_mod._load_control_state().get("accounting_windows", {}) == {}


def test_post_accounting_start_rejects_bool(dash):
    # bool is an int subclass; a JSON `true` must not be accepted as an epoch.
    dash_mod, _strategies_dir, _root = dash
    with _serve(dash_mod) as port:
        status, _ = _request(port, "POST", "/api/control/strategy/dash-demo",
                             body={"accounting_start_at": True})
    assert status == 400


def test_delete_leaves_accounting_window(dash):
    # DELETE clears the mode override only; the accounting window is orthogonal.
    dash_mod, _strategies_dir, _root = dash
    dash_mod._set_strategy_override("dash-demo", "live")
    dash_mod._set_accounting_start("dash-demo", 777)
    with _serve(dash_mod) as port:
        status, _ = _request(port, "DELETE", "/api/control/strategy/dash-demo")
    assert status == 200
    state = dash_mod._load_control_state()
    assert "dash-demo" not in state.get("strategy_overrides", {})
    assert state["accounting_windows"]["dash-demo"]["accounting_start_at"] == 777


def test_api_payload_exposes_accounting_start_and_strategy_pnl(dash):
    dash_mod, _strategies_dir, _root = dash
    dash_mod._set_accounting_start("dash-demo", 1704067200000)
    _bust(dash_mod)
    payload = dash_mod.api_payload()
    row = next(s for s in payload["strategies"] if s["strategy_id"] == "dash-demo")
    assert row["accounting_start_at"] == 1704067200000
    pnl = row["strategy_pnl"]
    # No ledger rows in the offline fixture -> zeros, budget passthrough, no error.
    assert pnl["closed_order_count"] == 0
    assert pnl["accounting_start_at"] == 1704067200000
    assert pnl["budget_usd"] == 1500  # from the STRATEGY_TEMPLATE budget
    assert payload["accounting_windows"]["dash-demo"]["accounting_start_at"] == 1704067200000
