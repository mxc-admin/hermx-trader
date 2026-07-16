"""Positions-first read model: episode fold, filters, identity vs the ledger
aggregate, and the observe-only position diff.

Same isolation pattern as test_pnl_ledger.py: the ``ledger_dir`` fixture binds
HERMX_DATA_DIR to a tmp dir, so seeding legs (via reconcile or raw writes)
exercises the real read path.
"""
from __future__ import annotations

import json

import pytest

import pnl_ledger
import pnl_positions


def _leg(*, kind, ord_id, side, qty, px, ts, sid="alpha", inst="BTC-USDT-SWAP",
         exchange="okx", mode="demo", gross=None, fee=None, net=None):
    return {
        "schema_version": 4, "leg_kind": kind, "exchange": exchange,
        "inst_id": inst, "ord_id": ord_id, "mode": mode, "strategy_id": sid,
        "side": side, "filled_qty": qty, "avg_px": px, "pnl_gross": gross,
        "fee_cost": fee, "fee_currency": "USDT", "net_realized_pnl": net,
        "closed_at_ms": ts,
    }


def _seed(ledger_dir, legs):
    ledger_dir.write_text(
        "".join(json.dumps(r) + "\n" for r in legs), encoding="utf-8"
    )


def _round_trip(sid="alpha", base_ts=100):
    return [
        _leg(kind="open", ord_id=f"{sid}-o1", side="buy", qty=1.0, px=50000.0,
             ts=base_ts, sid=sid),
        _leg(kind="close", ord_id=f"{sid}-c1", side="sell", qty=1.0, px=51000.0,
             ts=base_ts + 100, sid=sid, gross=10.0, fee=-0.5, net=9.5),
    ]


# --- episode fold -------------------------------------------------------------

def test_missing_ledger_returns_empty(ledger_dir):
    assert pnl_positions.list_positions() == []


def test_single_round_trip_folds_to_closed_position(ledger_dir):
    _seed(ledger_dir, _round_trip())
    rows = pnl_positions.list_positions()
    assert len(rows) == 1
    pos = rows[0]
    assert pos["status"] == "closed"
    assert pos["strategy_id"] == "alpha"
    assert pos["venue"] == "okx"
    assert pos["mode"] == "demo"
    assert pos["side"] == "long"
    assert pos["qty"] == pytest.approx(1.0)  # sum of close-leg fills
    assert pos["entry_px"] == pytest.approx(50000.0)
    assert pos["exit_px"] == pytest.approx(51000.0)
    assert pos["opened_at_ms"] == 100
    assert pos["closed_at_ms"] == 200
    assert pos["realized_pnl_net"] == pytest.approx(9.5)
    assert pos["fees"] == pytest.approx(-0.5)


def test_open_position_from_unclosed_legs(ledger_dir):
    _seed(ledger_dir, [
        _leg(kind="open", ord_id="o1", side="buy", qty=0.4, px=50000.0, ts=100),
        _leg(kind="open", ord_id="o2", side="buy", qty=0.6, px=52000.0, ts=150),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 1
    pos = rows[0]
    assert pos["status"] == "open"
    assert pos["qty"] == pytest.approx(1.0)
    assert pos["entry_px"] == pytest.approx(0.4 * 50000 + 0.6 * 52000)
    assert pos["opened_at_ms"] == 100
    assert pos["closed_at_ms"] is None
    assert pos["upl"] is None  # live UPnL joined at the API layer


def test_partial_close_keeps_episode_open_with_realized(ledger_dir):
    _seed(ledger_dir, [
        _leg(kind="open", ord_id="o1", side="buy", qty=2.0, px=50000.0, ts=100),
        _leg(kind="close", ord_id="c1", side="sell", qty=0.5, px=51000.0,
             ts=200, gross=5.0, fee=-0.1, net=4.9),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 1
    pos = rows[0]
    assert pos["status"] == "open"
    assert pos["qty"] == pytest.approx(1.5)
    assert pos["realized_pnl_net"] == pytest.approx(4.9)


def test_short_round_trip(ledger_dir):
    _seed(ledger_dir, [
        _leg(kind="open", ord_id="s-o", side="sell", qty=1.0, px=51000.0, ts=100),
        _leg(kind="close", ord_id="s-c", side="buy", qty=1.0, px=50000.0,
             ts=200, gross=10.0, fee=-0.2, net=9.8),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 1
    assert rows[0]["status"] == "closed"
    assert rows[0]["side"] == "short"
    assert rows[0]["realized_pnl_net"] == pytest.approx(9.8)


def test_two_sequential_episodes_stay_separate(ledger_dir):
    legs = _round_trip(base_ts=100) + [
        _leg(kind="open", ord_id="o2", side="buy", qty=2.0, px=52000.0, ts=300),
        _leg(kind="close", ord_id="c2", side="sell", qty=2.0, px=53000.0,
             ts=400, gross=20.0, fee=-1.0, net=19.0),
    ]
    _seed(ledger_dir, legs)
    rows = pnl_positions.list_positions()
    assert len(rows) == 2
    assert all(r["status"] == "closed" for r in rows)
    # Newest-first within the closed group.
    assert [r["closed_at_ms"] for r in rows] == [400, 200]


def test_orphan_close_becomes_closed_position(ledger_dir):
    # Legacy/pre-wipe history: a close leg with no recorded open. Its realized
    # net must still land in exactly one closed row (identity).
    _seed(ledger_dir, [
        _leg(kind="close", ord_id="c-orphan", side="sell", qty=1.0, px=51000.0,
             ts=200, gross=7.0, fee=0.0, net=7.0),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 1
    pos = rows[0]
    assert pos["status"] == "closed"
    assert pos["qty"] == pytest.approx(1.0)
    assert pos["entry_px"] is None
    assert pos["opened_at_ms"] is None
    assert pos["realized_pnl_net"] == pytest.approx(7.0)


def test_reversal_close_splits_episodes(ledger_dir):
    # Long 1.0, then a single sell of 1.5 flattens AND opens a 0.5 short.
    _seed(ledger_dir, [
        _leg(kind="open", ord_id="o1", side="buy", qty=1.0, px=50000.0, ts=100),
        _leg(kind="close", ord_id="c1", side="sell", qty=1.5, px=51000.0,
             ts=200, gross=10.0, fee=0.0, net=10.0),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 2
    open_pos = [r for r in rows if r["status"] == "open"][0]
    closed_pos = [r for r in rows if r["status"] == "closed"][0]
    assert open_pos["side"] == "short"
    assert open_pos["qty"] == pytest.approx(0.5)
    assert closed_pos["realized_pnl_net"] == pytest.approx(10.0)


def test_strategies_fold_independently(ledger_dir):
    _seed(ledger_dir, _round_trip("alpha") + [
        _leg(kind="open", ord_id="b-o", side="buy", qty=1.0, px=50000.0,
             ts=300, sid="beta"),
    ])
    rows = pnl_positions.list_positions()
    assert len(rows) == 2
    assert {r["strategy_id"]: r["status"] for r in rows} == {
        "alpha": "closed", "beta": "open",
    }


# --- filters + sort -----------------------------------------------------------

def test_filters(ledger_dir):
    legs = (
        _round_trip("alpha")
        + [_leg(kind="open", ord_id="b-o", side="buy", qty=1.0, px=1.0, ts=300,
                sid="beta", mode="live", exchange="bybit", inst="ETH-USDT")]
    )
    _seed(ledger_dir, legs)
    assert len(pnl_positions.list_positions(strategy_id="alpha")) == 1
    assert len(pnl_positions.list_positions(status="open")) == 1
    assert len(pnl_positions.list_positions(mode="live")) == 1
    assert len(pnl_positions.list_positions(venue="bybit")) == 1
    assert pnl_positions.list_positions(strategy_id="alpha", status="open") == []


def test_accounting_start_drops_old_closed_keeps_open(ledger_dir):
    legs = _round_trip("alpha", base_ts=100) + [
        _leg(kind="open", ord_id="o-live", side="buy", qty=1.0, px=1.0, ts=150),
    ]
    _seed(ledger_dir, legs)
    rows = pnl_positions.list_positions(accounting_start_at=500)
    assert [r["status"] for r in rows] == ["open"]


def test_open_sorts_before_closed(ledger_dir):
    legs = _round_trip("alpha", base_ts=100) + [
        _leg(kind="open", ord_id="o-live", side="buy", qty=1.0, px=1.0,
             ts=50, sid="beta"),
    ]
    _seed(ledger_dir, legs)
    rows = pnl_positions.list_positions()
    assert [r["status"] for r in rows] == ["open", "closed"]


# --- identity vs aggregate_strategy_pnl ----------------------------------------

def test_closed_positions_net_equals_aggregate(ledger_dir):
    legs = _round_trip("alpha", base_ts=100) + _round_trip("alpha", base_ts=1000)
    # Distinct ord_ids for the second round trip.
    legs[2]["ord_id"] = "o-2"
    legs[3]["ord_id"] = "c-2"
    _seed(ledger_dir, legs)
    closed = pnl_positions.list_positions(strategy_id="alpha", status="closed")
    agg = pnl_ledger.aggregate_strategy_pnl("alpha", mode="demo")
    assert sum(p["realized_pnl_net"] for p in closed) == pytest.approx(
        agg["closed_net_pnl_usd"]
    )
    assert agg["closed_net_pnl_usd"] == pytest.approx(19.0)


def test_identity_through_real_reconcile(ledger_dir):
    # End-to-end: order history -> reconcile (open+close legs) -> positions fold
    # vs ledger aggregate, through the submit-time attribution map.
    import pnl_strategy_map
    pnl_strategy_map.record_submit_strategy("mxcO", "alpha")
    pnl_strategy_map.record_submit_strategy("mxcC", "alpha")
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "o1", "clOrdId": "mxcO",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False,
         "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "c1", "clOrdId": "mxcC",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True,
         "avgPx": "51000", "pnl": "10.0", "fee": "-0.5", "feeCcy": "USDT",
         "uTime": 200},
    ]
    assert pnl_ledger.reconcile_from_order_history(rows, "okx", "demo") == 2
    closed = pnl_positions.list_positions(strategy_id="alpha", status="closed")
    assert len(closed) == 1
    agg = pnl_ledger.aggregate_strategy_pnl("alpha", mode="demo")
    assert closed[0]["realized_pnl_net"] == pytest.approx(agg["closed_net_pnl_usd"])
    assert closed[0]["realized_pnl_net"] == pytest.approx(9.5)


def test_open_plus_closed_realized_equals_aggregate_under_partial_close(ledger_dir):
    # While an episode is partially closed, its realized-so-far lives on the OPEN
    # row — the two views together always reconcile to the close-leg aggregate.
    _seed(ledger_dir, _round_trip("alpha") + [
        _leg(kind="open", ord_id="o2", side="buy", qty=2.0, px=52000.0, ts=300),
        _leg(kind="close", ord_id="c2", side="sell", qty=1.0, px=53000.0,
             ts=400, gross=10.0, fee=0.0, net=10.0),
    ])
    rows = pnl_positions.list_positions(strategy_id="alpha")
    agg = pnl_ledger.aggregate_strategy_pnl("alpha", mode="demo")
    assert sum(p["realized_pnl_net"] for p in rows) == pytest.approx(
        agg["closed_net_pnl_usd"]
    )


# --- diff_open_positions (observe-only reconcile-by-position) -------------------

def test_diff_no_drift_when_matching():
    ledger = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
               "side": "long", "qty": 1.0}]
    venue = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
              "side": "long", "qty": 1.0}]
    assert pnl_positions.diff_open_positions(ledger, venue) == []


def test_diff_ledger_open_venue_flat():
    ledger = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
               "side": "long", "qty": 1.0}]
    drift = pnl_positions.diff_open_positions(ledger, [])
    assert len(drift) == 1
    assert drift[0]["kind"] == "ledger_open_venue_flat"


def test_diff_venue_open_ledger_unknown():
    venue = [{"venue": "okx", "mode": "demo", "inst_id": "ETH-USDT-SWAP",
              "side": "short", "qty": 2.0}]
    drift = pnl_positions.diff_open_positions([], venue)
    assert len(drift) == 1
    assert drift[0]["kind"] == "venue_open_ledger_unknown"
    assert drift[0]["venue_qty"] == pytest.approx(-2.0)


def test_diff_qty_mismatch_beyond_tolerance():
    ledger = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
               "side": "long", "qty": 1.0}]
    venue = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
              "side": "long", "qty": 1.5}]
    drift = pnl_positions.diff_open_positions(ledger, venue)
    assert [d["kind"] for d in drift] == ["qty_mismatch"]
    # Within tolerance -> no drift.
    venue_ok = [{"venue": "okx", "mode": "demo", "inst_id": "BTC-USDT-SWAP",
                 "side": "long", "qty": 1.005}]
    assert pnl_positions.diff_open_positions(ledger, venue_ok) == []
