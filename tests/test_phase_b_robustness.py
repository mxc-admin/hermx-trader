"""Phase B robustness — venue-truth observability (HERMX_ROBUSTNESS_EXECUTION_PLAN.md §Phase B).

All three items are OBSERVE-ONLY: they read the venue and alert on divergence but
NEVER auto-correct, cancel, or submit. Every check is flag-gated / mode-gated so the
default runtime is byte-identical to today.

  B1  Venue position-drift detection (+ Opp-10 overfill invariant). Compare HermX's
      journal view of open positions against the venue's reported positions; emit
      RECONCILE_MISMATCH on drift. A close fill exceeding the ordered size logs a
      WARNING. Neither ever blocks.
  B2  Account-balance reconciliation (live-mode only). Fetch the real venue balance,
      compare to HermX's computed equity, alert past a % threshold. Demo is skipped
      (sandbox balance is fake).
  B3  External / manual fills first-class in the ledger (flag-gated OFF). An external
      close (no HermX cl_ord_id) is ledgered with strategy_id=None, source="external"
      only when HERMX_LEDGER_EXTERNAL_FILLS is armed. The ``source`` field is
      backfilled as "hermx" for legacy rows on read.

Tests exercise the PRODUCTION functions (detect_position_drift / get_balance_summary /
check_balance_drift / reconcile_from_order_history), never a re-implemented copy.
Offline: fake executors/clients so nothing reaches a venue.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import pnl_ledger
import webhook_receiver as wr_mod
from executors.ccxt_adapter import (
    CcxtExecutor,
    check_balance_drift,
    detect_position_drift,
)


# ===========================================================================
# Fakes
# ===========================================================================

class _FakePosExecutor:
    """Read-only stand-in exposing get_positions(). Records whether any write verb
    was called so observe-only can be asserted."""

    def __init__(self, positions, *, raise_exc=False):
        self._positions = positions
        self._raise = raise_exc
        self.execute_called = False

    def get_positions(self, inst_id=None):
        if self._raise:
            raise RuntimeError("venue unreachable")
        return list(self._positions)

    def execute(self, readiness):  # must never be called by an observe-only path
        self.execute_called = True
        return {}


class _FakeBalanceExecutor:
    """Exposes get_balance_summary() returning a canned total, for B2 drift tests."""

    def __init__(self, total):
        self._total = total

    def get_balance_summary(self, currency="USDT"):
        return {"free": self._total, "used": 0.0, "total": self._total, "currency": currency}


class _FakeBalanceClient:
    """In-memory ccxt client serving a canned unified balance (no network)."""

    def __init__(self, balance=None, raise_exc=False):
        self._balance = balance or {}
        self._raise = raise_exc

    def fetch_balance(self):
        if self._raise:
            raise RuntimeError("balance endpoint down")
        return self._balance


def _executor() -> CcxtExecutor:
    return CcxtExecutor(
        {"execution": {"exchange": "ccxt", "ccxt_exchange": "okx", "simulated_trading": True}},
        Path("."),
    )


# ===========================================================================
# B1 — venue position-drift detection
# ===========================================================================

def test_detect_position_drift_finds_discrepancy():
    # journal says 1.0, venue says 0.8 -> drift -0.2 reported.
    ex = _FakePosExecutor([{"inst_id": "BTC-USDT-SWAP", "pos": 0.8}])
    drifts = detect_position_drift(ex, {"BTC-USDT-SWAP": 1.0}, "okx", "demo")
    assert len(drifts) == 1
    d = drifts[0]
    assert d["inst_id"] == "BTC-USDT-SWAP"
    assert d["journal_qty"] == 1.0
    assert d["venue_qty"] == 0.8
    assert d["drift"] == pytest.approx(-0.2)
    assert d["venue"] == "okx"
    assert d["mode"] == "demo"
    # Observe-only: detection never touches the submit path.
    assert ex.execute_called is False


def test_detect_position_drift_no_drift():
    ex = _FakePosExecutor([{"inst_id": "BTC-USDT-SWAP", "pos": 1.0}])
    assert detect_position_drift(ex, {"BTC-USDT-SWAP": 1.0}, "okx", "demo") == []


def test_detect_position_drift_venue_has_extra_position():
    # Venue reports a position HermX's journal has no record of -> reported.
    ex = _FakePosExecutor([{"inst_id": "ETH-USDT-SWAP", "pos": 2.0}])
    drifts = detect_position_drift(ex, {}, "okx", "demo")
    assert len(drifts) == 1
    d = drifts[0]
    assert d["inst_id"] == "ETH-USDT-SWAP"
    assert d["journal_qty"] == 0.0
    assert d["venue_qty"] == 2.0
    assert d["drift"] == pytest.approx(2.0)


def test_detect_position_drift_exception_returns_empty():
    # A venue read that throws must degrade to [] and never crash startup.
    ex = _FakePosExecutor([], raise_exc=True)
    assert detect_position_drift(ex, {"BTC-USDT-SWAP": 1.0}, "okx", "demo") == []


def test_drift_emits_reconcile_mismatch(monkeypatch):
    # The webhook wiring logs each drift AND emits RECONCILE_MISMATCH.
    emitted = []
    monkeypatch.setattr(
        wr_mod, "emit_reconcile_alert",
        lambda kind, detail: emitted.append((kind, detail)),
    )
    ex = _FakePosExecutor([{"inst_id": "BTC-USDT-SWAP", "pos": 0.5}])
    drifts = wr_mod.reconcile_position_drift(ex, {"BTC-USDT-SWAP": 1.0}, "okx", "demo")

    assert len(drifts) == 1
    assert len(emitted) == 1
    kind, detail = emitted[0]
    assert kind == wr_mod.RECONCILE_ALERT_MISMATCH  # "RECONCILE_MISMATCH"
    assert detail["type"] == "position_drift"
    assert detail["inst_id"] == "BTC-USDT-SWAP"
    assert detail["journal_qty"] == 1.0
    assert detail["venue_qty"] == 0.5
    assert detail["drift"] == pytest.approx(-0.5)
    assert ex.execute_called is False  # never auto-corrects


# ---- Opp 10 — overfill invariant (folded into B1) ------------------------

def test_overfill_check_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        hit = pnl_ledger.check_overfill("BTC-USDT-SWAP", filled_qty=1.5, ordered_qty=1.0)
    assert hit is True
    assert any("overfill detected" in r.getMessage() for r in caplog.records)


def test_overfill_check_normal_fill_silent(caplog):
    with caplog.at_level(logging.WARNING):
        # Exactly the ordered size (and a within-tolerance nudge) is NOT an overfill.
        assert pnl_ledger.check_overfill("BTC-USDT-SWAP", 1.0, 1.0) is False
        assert pnl_ledger.check_overfill("BTC-USDT-SWAP", 1.005, 1.0) is False
    assert not any("overfill detected" in r.getMessage() for r in caplog.records)


# ===========================================================================
# B2 — account balance reconciliation (live-only, observe-only)
# ===========================================================================

def test_get_balance_returns_normalized_dict(monkeypatch):
    ex = _executor()
    client = _FakeBalanceClient(balance={
        "total": {"USDT": 100.0}, "free": {"USDT": 80.0}, "used": {"USDT": 20.0},
    })
    monkeypatch.setattr(ex, "_client", lambda *a, **k: client)
    out = ex.get_balance_summary("USDT")
    assert out == {"free": 80.0, "used": 20.0, "total": 100.0, "currency": "USDT"}


def test_get_balance_exception_returns_none(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda *a, **k: _FakeBalanceClient(raise_exc=True))
    assert ex.get_balance_summary("USDT") is None


def test_balance_drift_skipped_for_demo_mode(monkeypatch):
    emitted = []
    monkeypatch.setattr(wr_mod, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    # Even a wild balance vs equity gap is ignored in demo (sandbox balance is fake).
    ex = _FakeBalanceExecutor(total=999999.0)
    assert check_balance_drift(ex, 1000.0, "okx", "demo") is None
    assert emitted == []


def test_balance_drift_below_threshold_no_alert(monkeypatch):
    monkeypatch.delenv("HERMX_BALANCE_DRIFT_THRESHOLD_PCT", raising=False)
    emitted = []
    monkeypatch.setattr(wr_mod, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    # equity 1000, venue 1020 -> 2% drift < 5% default -> no alert.
    ex = _FakeBalanceExecutor(total=1020.0)
    out = check_balance_drift(ex, 1000.0, "okx", "live")
    assert out is not None
    assert out["alerted"] is False
    assert out["drift_pct"] == pytest.approx(2.0)
    assert emitted == []


def test_balance_drift_above_threshold_alerts(monkeypatch):
    monkeypatch.delenv("HERMX_BALANCE_DRIFT_THRESHOLD_PCT", raising=False)
    emitted = []
    monkeypatch.setattr(wr_mod, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    # equity 1000, venue 1100 -> 10% drift > 5% default -> alert.
    ex = _FakeBalanceExecutor(total=1100.0)
    out = check_balance_drift(ex, 1000.0, "okx", "live")
    assert out["alerted"] is True
    assert out["drift_pct"] == pytest.approx(10.0)
    assert len(emitted) == 1
    kind, detail = emitted[0]
    assert kind == wr_mod.RECONCILE_ALERT_MISMATCH
    assert detail["type"] == "balance_drift"
    assert detail["venue_balance"] == 1100.0
    assert detail["hermx_equity"] == 1000.0


def test_balance_drift_threshold_env_override(monkeypatch):
    monkeypatch.setenv("HERMX_BALANCE_DRIFT_THRESHOLD_PCT", "2.0")
    emitted = []
    monkeypatch.setattr(wr_mod, "emit_reconcile_alert", lambda k, d: emitted.append((k, d)))
    # equity 1000, venue 1030 -> 3% drift; default 5% would NOT alert, but 2% override does.
    ex = _FakeBalanceExecutor(total=1030.0)
    out = check_balance_drift(ex, 1000.0, "okx", "live")
    assert out["alerted"] is True
    assert out["drift_pct"] == pytest.approx(3.0)
    assert len(emitted) == 1


# ===========================================================================
# B3 — external fills first-class in ledger (flag-gated OFF)
# ===========================================================================

def _external_close_row(cl="venue-native-999", *, ord_id="ext1", inst_id="BTC-USDT-SWAP",
                        side="sell", pnl="50.0", uTime=200):
    """A reduceOnly close whose cl_ord_id is NOT HermX-attributed (external/manual)."""
    return {
        "instId": inst_id,
        "ordId": ord_id,
        "clOrdId": cl,
        "side": side,
        "accFillSz": 1.0,
        "sz": 1.0,
        "reduceOnly": True,
        "avgPx": "51000",
        "pnl": pnl,
        "fee": "-0.5",
        "feeCcy": "USDT",
        "state": "filled",
        "uTime": uTime,
    }


def _hermx_close_row(cl="mxcClose1", **kw):
    row = _external_close_row(cl=cl, **kw)
    row["ordId"] = kw.get("ord_id", "hermx1")
    return row


def test_external_fills_ledgered_when_flag_on(ledger_dir, monkeypatch):
    monkeypatch.setenv("HERMX_LEDGER_EXTERNAL_FILLS", "true")
    written = pnl_ledger.reconcile_from_order_history([_external_close_row()], "okx", "demo")
    assert written == 1
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 1
    assert rows[0]["source"] == "external"
    assert rows[0]["strategy_id"] is None


def test_external_fills_dropped_when_flag_off(ledger_dir, monkeypatch):
    monkeypatch.delenv("HERMX_LEDGER_EXTERNAL_FILLS", raising=False)
    written = pnl_ledger.reconcile_from_order_history([_external_close_row()], "okx", "demo")
    assert written == 0
    assert pnl_ledger.read_closed_trades() == []


def test_source_field_backfilled_for_legacy_rows(ledger_dir):
    # A legacy row (schema v2, no ``source``) reads back as source="hermx".
    ledger_dir.write_text(
        json.dumps({
            "schema_version": 2,
            "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "legacy1",
            "mode": "demo", "strategy_id": "alpha", "side": "sell",
            "filled_qty": 1.0, "pnl_gross": 10.0, "fee_cost": -0.2,
            "closed_at_ms": 111,
        }) + "\n",
        encoding="utf-8",
    )
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 1
    assert rows[0]["source"] == "hermx"


def test_external_fills_deduped_idempotently(ledger_dir, monkeypatch):
    monkeypatch.setenv("HERMX_LEDGER_EXTERNAL_FILLS", "true")
    row = _external_close_row()
    assert pnl_ledger.reconcile_from_order_history([row], "okx", "demo") == 1
    # Re-running the same external close writes nothing new (composite-key dedup).
    assert pnl_ledger.reconcile_from_order_history([row], "okx", "demo") == 0
    assert len(pnl_ledger.read_closed_trades()) == 1


def test_external_fills_count_helper(ledger_dir, monkeypatch):
    # external_fills_count tallies only source="external" rows (account-level view).
    monkeypatch.setenv("HERMX_LEDGER_EXTERNAL_FILLS", "true")
    pnl_ledger.reconcile_from_order_history([_external_close_row()], "okx", "demo")
    pnl_ledger.reconcile_from_order_history([_hermx_close_row()], "okx", "demo")
    assert pnl_ledger.external_fills_count() == 1
    assert pnl_ledger.external_fills_count(mode="live") == 0


def test_hermx_attributed_row_source_is_hermx(ledger_dir):
    # A HermX-attributed close (mxc-prefixed cl_ord_id) is written with source="hermx".
    written = pnl_ledger.reconcile_from_order_history([_hermx_close_row()], "okx", "demo")
    assert written == 1
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 1
    assert rows[0]["source"] == "hermx"
