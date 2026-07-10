"""Phase A: strategy ``side_policy`` (long_only / short_only / long_short).

Exercises the REAL production seam end-to-end (per the repo anti-pattern rule
against re-implementing handlers in tests): the readiness plan is built by
``strategy.readiness.build_strategy_execution_readiness`` and then resolved into
concrete legs by the CCXT adapter's real ``CcxtExecutor._expanded_actions`` for a
given current position side.

The truth table is the validated 14-case matrix: for each (policy, signal,
position) the executor must emit exactly the expected close/open legs. The
opposite-close leg ALWAYS runs (a policy change can never strand an open
position); only the same-direction OPEN leg is suppressed under a restrictive
policy.

Hermetic: ``strategy_override`` (control-state read) is stubbed to {} and the
strategy uses ``reinvest: False`` so no ledger/control-state files are touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import strategy.readiness as readiness_mod
from executors.ccxt_adapter import CcxtExecutor
from strategy.readiness import build_strategy_execution_readiness

SID = "btcusdt_duo_base_dev_2h"


@pytest.fixture(autouse=True)
def _isolate_control_state(monkeypatch):
    # No control-state override; keep readiness a pure function of the record.
    monkeypatch.setattr(readiness_mod, "strategy_override", lambda *a, **k: {})


def _strategy(side_policy=None) -> dict:
    s = {
        "schema_version": 2,
        "strategy_id": SID,
        "name": "Side Policy Test",
        "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        "capital": {"budget_usd": 1000, "reinvest": False},
        "timeframe": "2h",
        "indicator": "x",
        "leverage": 2,
        "margin_mode": "isolated",
        "execution_mode": "demo",
        "asset": "BTCUSDT",
    }
    if side_policy is not None:
        s["side_policy"] = side_policy
    return s


def _normalized(action: str) -> dict:
    return {
        "strategy_id": SID,
        "symbol": "BTCUSDT",
        "action": action,
        "timeframe": "2h",
        "tv_time": "2026-07-09T00:00:00Z",
        "tv_signal_price": 65000.0,
    }


def _readiness(side_policy, action) -> dict:
    record = {
        "normalized": _normalized(action),
        "strategy_config": _strategy(side_policy),
        "strategy_id": SID,
    }
    return build_strategy_execution_readiness(record)


def _executor() -> CcxtExecutor:
    cfg = {"execution": {"exchange": "ccxt", "ccxt_exchange": "okx", "simulated_trading": True}}
    return CcxtExecutor(cfg, Path("."))


def _legs(side_policy, action, current_side) -> list[str]:
    return _executor()._expanded_actions(_readiness(side_policy, action), current_side)


# policy, signal(action), position(current_side), expected executor legs.
TRUTH_TABLE = [
    ("long_short", "buy", "flat", ["OPEN_LONG"]),
    ("long_short", "sell", "flat", ["OPEN_SHORT"]),
    ("long_short", "buy", "short", ["CLOSE_SHORT", "OPEN_LONG"]),
    ("long_short", "sell", "long", ["CLOSE_LONG", "OPEN_SHORT"]),
    ("long_only", "buy", "flat", ["OPEN_LONG"]),
    ("long_only", "buy", "short", ["CLOSE_SHORT", "OPEN_LONG"]),
    ("long_only", "sell", "long", ["CLOSE_LONG"]),          # open-short suppressed
    ("long_only", "sell", "flat", []),                       # nothing
    ("long_only", "sell", "short", []),                      # sell can't touch a short
    ("short_only", "sell", "flat", ["OPEN_SHORT"]),
    ("short_only", "sell", "long", ["CLOSE_LONG", "OPEN_SHORT"]),
    ("short_only", "buy", "short", ["CLOSE_SHORT"]),         # open-long suppressed
    ("short_only", "buy", "flat", []),                       # nothing
    ("short_only", "buy", "long", []),                       # buy can't touch a long
]


@pytest.mark.parametrize(
    "policy,action,current_side,expected",
    TRUTH_TABLE,
    ids=[f"{p}-{a}-{c}" for p, a, c, _ in TRUTH_TABLE],
)
def test_side_policy_truth_table(policy, action, current_side, expected):
    assert _legs(policy, action, current_side) == expected


def test_open_suppressed_flag_set_only_when_restricted():
    # long_only + sell -> open suppressed, tag present, opposite-close leg retained.
    suppressed = _readiness("long_only", "sell")
    assert suppressed["execution_intent"]["open_suppressed"] is True
    assert suppressed["execution_intent"]["actions"] == ["CLOSE_OPPOSITE_IF_ANY"]
    assert suppressed["execution_intent"]["target_direction"] == "short"
    assert suppressed["side_policy_restriction"] == {
        "policy": "long_only",
        "suppressed_direction": "short",
    }
    # long_only + buy -> allowed, no suppression tags at all.
    allowed = _readiness("long_only", "buy")
    assert "open_suppressed" not in allowed["execution_intent"]
    assert "side_policy_restriction" not in allowed


def test_absent_side_policy_is_byte_identical_to_long_short():
    """Characterization: an existing strategy file with NO side_policy field yields
    the exact same execution_intent.actions (and no new tags) as today."""
    for action, direction in (("buy", "LONG"), ("sell", "SHORT")):
        absent = _readiness(None, action)
        explicit = _readiness("long_short", action)
        assert absent["execution_intent"]["actions"] == ["CLOSE_OPPOSITE_IF_ANY", f"OPEN_{direction}"]
        assert absent["execution_intent"]["actions"] == explicit["execution_intent"]["actions"]
        assert "open_suppressed" not in absent["execution_intent"]
        assert "side_policy_restriction" not in absent
