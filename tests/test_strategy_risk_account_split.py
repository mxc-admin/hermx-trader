"""Split strategy controls — account (execution_mode) vs risk (risk_state).

The LOCKED design under test:

  strategy_overrides[sid] = {
      "execution_mode": "demo"|"live",   # ACCOUNT — only account controls write it
      "risk_state": "active"|"reduce",   # RISK — only risk controls write it
      "set_at": iso, "mode": <derived display>,
  }

Money invariants (each has a dedicated regression test below):
  1. No risk action ever writes execution_mode — pausing a LIVE strategy must not
     lie its later operator close onto the DEMO venue (the historical landmine).
  2. Never-block-a-close: risk_state=reduce and global trading_state=reducing block
     opens/reversals only; close_only submissions always pass.
  3. Migration never arms: a legacy on-disk pause can only become risk reduce.
  4. Both writers (control_state.py + dashboard.py) speak the same model.

Offline and deterministic: the `wr` fixture isolates control-state.json in a tmp
HERMX_ROOT; every submit is mocked at ExecutorFactory.create.
"""
from __future__ import annotations

import json
from unittest import mock

import execution.service as svc
from conftest import fake_executor as _fake_executor

from reconcile.executor_select import active_venue_modes


def _read_control_state(module) -> dict:
    return json.loads(module.CONTROL_STATE_FILE.read_text(encoding="utf-8"))


def _strategy(sid="strat-split", execution_mode="live"):
    return {
        "strategy_id": sid,
        "execution_mode": execution_mode,
        "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        "leverage": 2,
        "margin_mode": "isolated",
        "asset": "BTCUSDT",
    }


def _signal_record(sid, *, execution_mode="demo", tv_time="2026-07-18T00:00:00Z"):
    return {
        "strategy_id": sid,
        "strategy_config": {
            "strategy_id": sid,
            "name": "Split Strategy",
            "asset": "BTCUSDT",
            "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
            "timeframe": "2h",
            "execution_mode": execution_mode,
            "submit_orders": True,
            "budget_usd": 1500,
            "leverage": 2,
            "margin_mode": "isolated",
        },
        "normalized": {
            "strategy_id": sid,
            "symbol": "BTCUSDT",
            "side": "buy",
            "timeframe": "2h",
            "tv_time": f"{tv_time}|{sid}",
            "tv_signal_price": 50000.0,
        },
    }


# ===========================================================================
# Landmine 1 — LIVE account + risk reduce: operator close stays on the LIVE venue.
# ===========================================================================

def test_live_account_plus_risk_reduce_close_readiness_stays_live(wr):
    assert wr.set_strategy_override("strat-split", "live") is True
    assert wr.set_strategy_risk("strat-split", "reduce") is True

    rd = wr.build_operator_close_readiness("BTCUSDT", _strategy(), "opcls0000000000000000000000001")
    # THE landmine: the old pause rewrote execution_mode to demo, so this close
    # would have flattened the DEMO account while the LIVE position stayed open.
    assert rd["execution_mode"] == "live"
    assert rd["simulated_trading"] is False
    assert rd["close_only"] is True


def test_pause_via_compat_mode_does_not_rewrite_live_account(wr):
    # Landmine 6: POST mode=pause after mode=live must keep execution_mode=live.
    assert wr.set_strategy_override("strat-split", "live") is True
    assert wr.set_strategy_override("strat-split", "pause") is True

    entry = _read_control_state(wr)["strategy_overrides"]["strat-split"]
    assert entry["execution_mode"] == "live"
    assert entry["risk_state"] == "reduce"
    assert entry["mode"] == "pause"  # display label only


def test_risk_actions_never_touch_account_and_vice_versa(wr):
    assert wr.set_strategy_account("strat-split", "live") is True
    assert wr.set_strategy_risk("strat-split", "reduce") is True
    entry = _read_control_state(wr)["strategy_overrides"]["strat-split"]
    assert (entry["execution_mode"], entry["risk_state"]) == ("live", "reduce")

    # Account flip preserves the risk posture.
    assert wr.set_strategy_account("strat-split", "demo") is True
    entry = _read_control_state(wr)["strategy_overrides"]["strat-split"]
    assert (entry["execution_mode"], entry["risk_state"]) == ("demo", "reduce")

    # Risk re-arm preserves the account.
    assert wr.set_strategy_risk("strat-split", "active") is True
    entry = _read_control_state(wr)["strategy_overrides"]["strat-split"]
    assert (entry["execution_mode"], entry["risk_state"]) == ("demo", "active")


# ===========================================================================
# Landmine 2 — live + reduce stays in the reconcile (venue, mode) domain.
# ===========================================================================

def test_live_plus_reduce_keeps_live_in_active_venue_modes(wr, monkeypatch):
    monkeypatch.setattr(wr, "STRATEGIES", {"strat-split": _strategy(execution_mode="live")})
    assert active_venue_modes() == {("okx", False)}

    assert wr.set_strategy_risk("strat-split", "reduce") is True
    # Risk-off must NOT drop the live account from the drift/reconcile domain: the
    # open live position still needs watching exactly while we are reducing.
    assert active_venue_modes() == {("okx", False)}


# ===========================================================================
# Landmines 3/4 — reduce blocks the open at the gate; close_only always passes.
# ===========================================================================

def test_risk_reduce_blocks_open_signal_at_gate(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    assert wr.set_strategy_risk("strat-split", "reduce") is True

    rd = wr.build_strategy_execution_readiness(_signal_record("strat-split"))
    assert rd["risk_state"] == "reduce"
    assert rd["live_execution_enabled"] is True  # armed; the GATE blocks, not the flag

    record = {"received_at": "2026-07-18T00:00:00Z", "execution_readiness": rd}
    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        out = wr.execute_if_enabled(record)

    create_mock.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["gate"] == "strategy_risk_state"
    assert out["reason"] == "risk_state_reduce:open_blocked"


def test_risk_reduce_passes_operator_close(wr, monkeypatch):
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    assert wr.set_strategy_override("strat-split", "live") is True
    assert wr.set_strategy_risk("strat-split", "reduce") is True

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_operator_close("BTCUSDT", _strategy(), operator="test",
                                        reason="risk_reduce_close")

    # Never-block-a-close: risk reduce + kill switch off still flattens, on LIVE.
    create_mock.assert_called_once()
    fake.execute.assert_called_once()
    sent = fake.execute.call_args[0][0]
    assert sent["execution_mode"] == "live"
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"


def test_global_reducing_and_risk_reduce_close_still_passes(wr, monkeypatch):
    # Landmine 7: both the global and the per-strategy risk gates engaged -- a
    # close must still pass every gate (belt-and-braces on the invariant).
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    assert wr.set_trading_state("reducing") is True
    assert wr.set_strategy_risk("strat-split", "reduce") is True

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_operator_close("BTCUSDT", _strategy(execution_mode="demo"))

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_risk_active_open_passes_gate(wr, monkeypatch):
    monkeypatch.setattr(svc, "HERMX_MAX_NOTIONAL_USD_ENV", float("inf"))
    assert wr.set_strategy_risk("strat-split", "active") is True

    rd = wr.build_strategy_execution_readiness(_signal_record("strat-split"))
    assert rd["risk_state"] == "active"

    fake = _fake_executor()
    record = {"received_at": "2026-07-18T00:00:01Z", "execution_readiness": rd}
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(record)

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


# ===========================================================================
# Landmines 5/8 — on-disk migration: legacy pause -> risk reduce, never armed.
# ===========================================================================

def test_migration_legacy_pause_entry_becomes_risk_reduce(wr):
    # Landmine 5: legacy pause on a live-file strategy stored the forced-demo lie.
    # Migration keeps demo (the pre-pause account is unrecoverable -- fail-safe)
    # and converts pause to risk reduce; submit_orders is dropped so a close can
    # never be blocked at the arming gate.
    state = wr.default_control_state()
    state["strategy_overrides"] = {
        "strat-legacy": {"mode": "pause", "execution_mode": "demo",
                          "submit_orders": False, "set_at": "x"},
    }
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    entry = wr.load_control_state()["strategy_overrides"]["strat-legacy"]
    assert entry["risk_state"] == "reduce"       # landmine 8: pause -> reduce, not active
    assert entry["execution_mode"] == "demo"     # fail-safe: stored account kept
    assert entry["mode"] == "pause"
    assert "submit_orders" not in entry


def test_migration_legacy_demo_live_entries_become_active(wr):
    state = wr.default_control_state()
    state["strategy_overrides"] = {
        "d": {"mode": "demo", "execution_mode": "demo", "submit_orders": True, "set_at": "x"},
        "l": {"mode": "live", "execution_mode": "live", "submit_orders": True, "set_at": "x"},
    }
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    overrides = wr.load_control_state()["strategy_overrides"]
    assert overrides["d"]["risk_state"] == "active"
    assert overrides["d"]["execution_mode"] == "demo"
    assert overrides["l"]["risk_state"] == "active"
    assert overrides["l"]["execution_mode"] == "live"


def test_migration_pause_without_account_keeps_account_unset(wr):
    # A pause entry with NO stored execution_mode (the new-style risk-only shape)
    # must stay account-less: forcing demo here would recreate the pause-lies-demo
    # landmine for a live-file strategy. The file's execution_mode governs.
    state = wr.default_control_state()
    state["strategy_overrides"] = {"p": {"mode": "pause", "set_at": "x"}}
    wr.CONTROL_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    entry = wr.load_control_state()["strategy_overrides"]["p"]
    assert entry["risk_state"] == "reduce"
    assert "execution_mode" not in entry


# ===========================================================================
# Readiness resolution — pause-only override keeps the FILE account.
# ===========================================================================

def test_pause_only_override_on_live_file_keeps_live_account(wr):
    # New-style pause (risk-only entry, no execution_mode) on a live-file strategy:
    # the signal path resolves the LIVE account with risk reduce riding alongside.
    assert wr.set_strategy_override("strat-split", "pause") is True

    rd = wr.build_strategy_execution_readiness(
        _signal_record("strat-split", execution_mode="live"))
    assert rd["execution_mode"] == "live"
    assert rd["simulated_trading"] is False
    assert rd["risk_state"] == "reduce"


def test_setter_input_validation(wr):
    assert wr.set_strategy_account("strat-split", "bogus") is False
    assert wr.set_strategy_risk("strat-split", "bogus") is False
    assert wr.set_strategy_account("", "demo") is False
    assert wr.set_strategy_risk("", "reduce") is False
    assert not wr.CONTROL_STATE_FILE.exists()  # rejected writes never create the file
