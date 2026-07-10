"""Phase B: internal ``side`` -> ``action`` cleanup (normalize output + dedupe).

Locks the two halves of the validated ATOMIC pair that must ship together:
  * ``normalize()`` no longer emits a ``side`` key (``action`` is canonical), and
  * ``dedupe_key`` / ``_signal_identity`` hash ``action`` instead of ``side``.

Splitting them would collapse a same-bar buy/sell to one dedupe key and falsely
reject the reversal's second leg -- the exact regression guarded here. Runs
against the REAL production ``normalize`` / ``dedupe_key`` / ``check_and_mark_signal``
via the ``wr`` characterization harness (no re-implemented handler logic).
"""
from __future__ import annotations

import pytest

BASE = {
    "source": "tradingview",
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "symbol": "BTCUSDT",
    "timeframe": "2h",
    "tv_time": "2026-07-09T00:00:00Z",
    "tv_signal_price": 65000.0,
    "exchange": "okx",
}


@pytest.mark.parametrize(
    "raw,expected_action",
    [({"side": "buy"}, "buy"), ({"side": "sell"}, "sell"), ({"action": "close"}, "close")],
)
def test_normalize_output_has_no_side_key(wr, raw, expected_action):
    norm = wr.normalize({**BASE, **raw})
    assert "side" not in norm
    assert norm["action"] == expected_action


def test_dedupe_key_uses_action_and_is_stable_for_opens(wr):
    """For an open, action == the old side value, so the key is byte-identical to the
    pre-change side-based key (the ``action`` value fills the slot ``side`` used to)."""
    norm = wr.normalize({**BASE, "side": "buy"})
    expected = "|".join(["btcusdt_duo_base_dev_2h", "BTCUSDT", "buy", "2h", "2026-07-09T00:00:00Z"])
    assert wr.dedupe_key(norm) == expected
    # Deterministic across calls.
    assert wr.dedupe_key(norm) == wr.dedupe_key(wr.normalize({**BASE, "side": "buy"}))


def test_same_bar_buy_and_sell_are_not_duplicates(wr):
    """THE atomicity regression: on the same bar a buy then a sell (a reversal) must
    stay distinct. If ``side`` were dropped without swapping dedupe to ``action`` both
    keys would collapse to an empty-side key and the sell would be a false duplicate."""
    buy = wr.normalize({**BASE, "side": "buy"})
    sell = wr.normalize({**BASE, "side": "sell"})
    assert wr.dedupe_key(buy) != wr.dedupe_key(sell)

    dup_buy, _ = wr.check_and_mark_signal(buy, "2026-07-09T00:00:01Z")
    dup_sell, _ = wr.check_and_mark_signal(sell, "2026-07-09T00:00:02Z")
    assert dup_buy is False
    assert dup_sell is False  # the sell is NOT a duplicate of the buy


def test_dedupe_round_trip_still_rejects_a_true_repeat(wr):
    """A genuine repeat of the same signal is still deduplicated."""
    buy = wr.normalize({**BASE, "side": "buy"})
    first, _ = wr.check_and_mark_signal(buy, "2026-07-09T00:00:01Z")
    second, meta = wr.check_and_mark_signal(buy, "2026-07-09T00:00:02Z")
    assert first is False
    assert second is True
    assert meta["first_seen_at"] == "2026-07-09T00:00:01Z"
