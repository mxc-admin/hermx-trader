"""`action` field intake wiring in webhook_receiver.build_record.

Proves, against the REAL production build_record (not an inline re-implementation):
  * a legacy side-only alert (no `action`) is now rejected 400 side_not_allowed —
    `side` is no longer read from the payload,
  * an action=buy alert routes through the strategy-file trial path,
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


# --------------------------------------------------------------------------- #
# side-only alert (legacy template) is now rejected                             #
# --------------------------------------------------------------------------- #


def test_side_only_alert_is_rejected_400(wr):
    """A legacy alert carrying only `side` (no `action`) is now rejected: `side` is
    no longer read, so normalize yields no valid action → 400 side_not_allowed."""
    alert = load_alert("strategy/btcusdt_buy.json")  # action=buy
    # Reconstruct an old-style payload: drop `action`, send only `side`.
    alert.pop("action", None)
    alert["side"] = "buy"
    status, record = wr.build_record(alert, RECEIVED_AT)
    assert status == 400
    assert record["ok"] is False
    assert record["error"] == "side_not_allowed"


# --------------------------------------------------------------------------- #
# action=buy routes through the strategy-file trial path                         #
# --------------------------------------------------------------------------- #


def test_action_buy_routes_through_strategy_trial(wr, monkeypatch):
    # Force the execution surface unavailable so the run resolves to a deterministic
    # not_submitted outcome instead of a real sandbox submit.
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)

    alert = load_alert("strategy/btcusdt_buy.json")  # action=buy
    status, record = wr.build_record(alert, RECEIVED_AT)

    assert status == 200
    assert record["mode"] == "strategy_file_trial"
    # `action` is the single canonical intent field; there is no `side` output.
    assert record["normalized"]["action"] == "buy"
    assert "side" not in record["normalized"]


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
