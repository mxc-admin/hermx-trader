"""Characterization: buy->sell paper round-trip (REFACTOR_PLAN.md:167, :179).

Drives a shadow-path (no strategy_id) Duo-raw alert pair through build_record,
which calls apply_paper_trading internally, and snapshots the resulting:
  * paper-trade events for both alerts,
  * paper-trades.jsonl rows (the closed trade), and
  * the final paper-state.json.

This is THE regression oracle for the paper-trading math that P1/P3 must not
silently change. Under the no-MXC test condition only the `duo_raw` policy
trades (it uses the TradingView alert itself); the MXC-gated policies SKIP.

Determinism: alerts carry a fixed tv_time and build_record gets a fixed
received_at_override, so signal_id, client_order_id, latency, and every *_at
field are stable. See conftest.normalize_snapshot for the (paths-only)
normalization rule.
"""
from __future__ import annotations

import json

from conftest import load_alert

BUY_AT = "2026-06-21T00:00:01Z"
SELL_AT = "2026-06-21T04:00:01Z"


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_buy_sell_paper_round_trip(wr, wr_root, seed_paper_state, assert_snapshot):
    # Start from the empty version-3 seed for a deterministic baseline.
    assert json.loads(seed_paper_state.read_text())["policies"] == {}

    buy_status, buy_rec = wr.build_record(load_alert("shadow/btcusdt_shadow_buy.json"), BUY_AT)
    sell_status, sell_rec = wr.build_record(load_alert("shadow/btcusdt_shadow_sell.json"), SELL_AT)

    assert buy_status == 200 and sell_status == 200
    assert buy_rec["mode"] == "vps_parallel_shadow"

    buy_events = buy_rec["paper_events"]
    sell_events = sell_rec["paper_events"]

    # --- behavioral invariants (meaningful even before goldens exist) --------
    def event(events, account, policy):
        return next(e for e in events if e["paper_account"] == account and e["policy"] == policy)

    # duo_raw opens a long on the buy (no-MXC: signal-only path stays armed).
    buy_duo = event(buy_events, "realistic_policies", "duo_raw")
    assert any(a.startswith("OPEN_LONG") for a in buy_duo["actions"])
    # MXC-gated policy skips with no live CDP context.
    buy_regime = event(buy_events, "realistic_policies", "duo_regime_rsi_30m")
    assert "SKIP_NO_NEW_ENTRY" in buy_regime["actions"]

    # sell closes the long for a profit (66300 vs 65000 entry), then REVERSES
    # into a short: decide_duo_raw always returns decision=TRADE/weight=1.0, so the
    # opposite signal both closes the open long AND opens a fresh short. This is the
    # real, deterministic engine behavior the round-trip must lock (close + reverse),
    # not a flat exit.
    sell_duo = event(sell_events, "realistic_policies", "duo_raw")
    assert any(a.startswith("CLOSE_LONG") for a in sell_duo["actions"])
    assert any(a.startswith("OPEN_SHORT") for a in sell_duo["actions"])
    assert sell_duo["realized_pnl_usd"] > 0

    # paper-trades.jsonl recorded exactly the closed long (one per paper account).
    trades = _read_jsonl(wr_root / "logs" / "paper-trades.jsonl")
    duo_trades = [t for t in trades if t["policy"] == "duo_raw"]
    assert len(duo_trades) == 3  # policies / realistic_policies / compound_policies
    for t in duo_trades:
        assert t["symbol"] == "BTCUSDT"
        assert t["side"] == "long"
        assert t["entry_price"] == 65000.0
        assert t["exit_price"] == 66300.0
        assert t["pnl_pct"] > 0

    # final paper-state: the long is closed (1 win) and a reversal short is now open
    # at the sell price.
    final_state = json.loads((wr_root / "paper-state.json").read_text())
    duo_state = final_state["realistic_policies"]["duo_raw"]
    assert duo_state["stats"]["closed_trades"] == 1
    assert duo_state["stats"]["wins"] == 1
    assert duo_state["stats"]["entries"] == 2  # original long + reversal short
    btc_pos = duo_state["symbols"]["BTCUSDT"]
    assert btc_pos["side"] == "short"
    assert btc_pos["entry_price"] == 66300.0
    assert btc_pos["entry_at"] == SELL_AT

    # --- golden snapshot (full structure, paths normalized) ------------------
    assert_snapshot(
        "paper_roundtrip.json",
        {
            "buy_events": buy_events,
            "sell_events": sell_events,
            "paper_trades": trades,
            "final_paper_state": final_state,
        },
    )
