"""PR2: `action` field intake wiring in webhook_receiver.build_record.

Proves, against the REAL production build_record (not an inline re-implementation):
  * an explicit action/side conflict (opposing open sides) is a hard 400,
  * an action=buy alert routes identically to the equivalent side=buy alert,
  * action=close reuses the operator-close path (close_only=True) — matched
    strategy → 200, unknown strategy → quarantine (never 400 side_not_allowed),
  * a repeated close signal is deduplicated.

Reuses the `wr` characterization harness (webhook_receiver reloaded against an
isolated temp HERMX_ROOT with the execution surface forced unavailable). No
network, no OKX.
"""
from __future__ import annotations

from conftest import load_alert

RECEIVED_AT = "2026-06-22T00:00:00Z"


def _reset_dedupe(wr) -> None:
    """Clear the in-memory dedupe index so a fresh (non-duplicate) alert can be
    replayed within one test."""
    wr._SIGNAL_DEDUPE_INDEX["signals"].clear()
    wr._SIGNAL_DEDUPE_INDEX["keys"].clear()


# --------------------------------------------------------------------------- #
# conflict gate                                                                 #
# --------------------------------------------------------------------------- #


def test_action_conflict_returns_400(wr):
    """action=buy with side=sell (opposing open sides) → 400 action_side_conflict."""
    alert = load_alert("strategy/btcusdt_buy.json")  # side=buy
    alert["action"] = "sell"
    status, record = wr.build_record(alert, RECEIVED_AT)
    assert status == 400
    assert record["mode"] == "action_side_conflict"
    assert record["error"] == "action_side_conflict"


# --------------------------------------------------------------------------- #
# action=buy is a drop-in for side=buy                                          #
# --------------------------------------------------------------------------- #


def test_action_buy_routes_identically_to_side_buy(wr, monkeypatch):
    # Force the execution surface unavailable so both runs resolve to a
    # deterministic not_submitted outcome instead of a real sandbox submit.
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)

    side_alert = load_alert("strategy/btcusdt_buy.json")  # side=buy, no action
    s1, r1 = wr.build_record(side_alert, RECEIVED_AT)

    # Same alert expressed via action only (side removed). Same strategy/symbol/
    # timeframe/tv_time → same signal_id, so reset dedupe before the replay.
    action_alert = {k: v for k, v in side_alert.items() if k != "side"}
    action_alert["action"] = "buy"
    _reset_dedupe(wr)
    s2, r2 = wr.build_record(action_alert, RECEIVED_AT)

    assert s1 == s2 == 200
    assert r1["mode"] == r2["mode"] == "strategy_file_trial"
    # Identical routing: same normalized alert and same execution outcome.
    assert r1["normalized"] == r2["normalized"]
    assert r1["okx_execution"] == r2["okx_execution"]
    # Derivation worked both ways: action present, side back-filled to buy.
    assert r2["normalized"]["action"] == "buy"
    assert r2["normalized"]["side"] == "buy"


# --------------------------------------------------------------------------- #
# action=close: operator-close path                                            #
# --------------------------------------------------------------------------- #


def test_action_close_unknown_strategy_quarantines(wr):
    """A close for an unknown strategy_id quarantines — NOT a 400 side_not_allowed
    (a close carries no side, so it must survive the ALLOWED_SIDES gate)."""
    alert = load_alert("strategy/btcusdt_close.json")
    alert["strategy_id"] = "doesnotexist_duo_base_dev_9h"
    status, record = wr.build_record(alert, RECEIVED_AT)
    assert status == 202
    assert record["quarantined"] is True
    assert record["mode"] == "strategy_alert_quarantine"
    assert record["reason"] == "unknown_strategy_id"
    assert record.get("error") != "side_not_allowed"


def test_action_close_matched_strategy_returns_200(wr, monkeypatch):
    """A close for a known strategy routes through the operator-close path
    (close_only=True). With the execution surface unavailable it degrades to a
    graceful not_submitted — no order is placed."""
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    alert = load_alert("strategy/btcusdt_close.json")
    status, record = wr.build_record(alert, RECEIVED_AT)
    assert status == 200
    assert record["mode"] == "webhook_close"
    assert record["close_only"] is True
    # No side survived normalization for a close bar.
    assert "side" not in record["normalized"]
    assert record["normalized"]["action"] == "close"
    # Execution surface unavailable → not_submitted, no order.
    assert record["okx_execution"]["mode"] == "not_submitted"
    assert record["okx_execution"]["reason"] == "execution_unavailable"


def test_action_close_dedupe(wr, monkeypatch):
    """The same close signal twice → the second is deduplicated."""
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    alert = load_alert("strategy/btcusdt_close.json")

    s1, r1 = wr.build_record(alert, RECEIVED_AT)
    s2, r2 = wr.build_record(alert, RECEIVED_AT)  # identical signal, no reset

    assert s1 == 200
    assert r1.get("duplicate") is not True
    assert s2 == 200
    assert r2["duplicate"] is True
    assert r2["mode"] == "webhook_close"
