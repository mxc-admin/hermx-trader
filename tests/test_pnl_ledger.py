"""Tests for the durable closed-trade ledger (P&L Master Plan, Phase 1).

The ledger resolves its path from HERMX_DATA_DIR (-> HERMX_ROOT -> repo root) at
*call time*, so each test just points HERMX_DATA_DIR at an isolated tmp_path via
monkeypatch — no module reload needed. conftest already binds HERMX_ROOT to a
session temp dir, so nothing here can touch the real runtime state.
"""
from __future__ import annotations

import json
import threading

import pytest

import pnl_ledger


# --- path resolution --------------------------------------------------------

def test_ledger_path_resolves_from_hermx_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    assert pnl_ledger._ledger_path() == tmp_path / "closed-trades.jsonl"


def test_ledger_path_falls_back_to_hermx_root(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMX_DATA_DIR", raising=False)
    monkeypatch.setenv("HERMX_ROOT", str(tmp_path))
    assert pnl_ledger._ledger_path() == tmp_path / "closed-trades.jsonl"


# --- attribution ------------------------------------------------------------

def test_is_hermx_cl_ord_id_mxc_prefix():
    assert pnl_ledger.is_hermx_cl_ord_id("mxc1700000000abc") is True


def test_is_hermx_cl_ord_id_operator_close_prefix():
    assert pnl_ledger.is_hermx_cl_ord_id("operator_close_BTC-USDT-SWAP") is True


def test_is_hermx_cl_ord_id_rejects_external():
    assert pnl_ledger.is_hermx_cl_ord_id("someExternalBot123") is False
    assert pnl_ledger.is_hermx_cl_ord_id("") is False
    assert pnl_ledger.is_hermx_cl_ord_id(None) is False


def test_is_hermx_cl_ord_id_hyperliquid_numeric_resolves(ledger_dir, monkeypatch):
    # A numeric cloid is HermX-issued only if a submit-time mapping exists.
    import pnl_cloid_map
    pnl_cloid_map.record_cloid_mapping("mxc-hl-1", "987654321", "hyperliquid")
    assert pnl_ledger.is_hermx_cl_ord_id("987654321", "hyperliquid") is True
    # Unmapped numeric cloid -> external.
    assert pnl_ledger.is_hermx_cl_ord_id("111", "hyperliquid") is False
    # Numeric cloid without the hyperliquid hint stays external.
    assert pnl_ledger.is_hermx_cl_ord_id("987654321") is False


# --- read -------------------------------------------------------------------

def test_read_closed_trades_empty(ledger_dir):
    assert pnl_ledger.read_closed_trades() == []


def test_read_closed_trades_corrupt_tolerant(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP",
                    "ord_id": "1", "mode": "demo", "closed_at_ms": 100}) + "\n"
        + "{ this is not json }\n"
        + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "ETH-USDT-SWAP",
                      "ord_id": "2", "mode": "demo", "closed_at_ms": 200}) + "\n",
        encoding="utf-8",
    )
    rows = pnl_ledger.read_closed_trades()
    assert [r["ord_id"] for r in rows] == ["1", "2"]


def test_read_closed_trades_since_filter(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "1",
                    "mode": "demo", "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "2",
                      "mode": "demo", "closed_at_ms": 300}) + "\n",
        encoding="utf-8",
    )
    rows = pnl_ledger.read_closed_trades(since_ms=200)
    assert [r["ord_id"] for r in rows] == ["2"]


def test_read_closed_trades_strategy_filter(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "1",
                    "mode": "demo", "strategy_id": "alpha", "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "2",
                      "mode": "demo", "strategy_id": "beta", "closed_at_ms": 200}) + "\n",
        encoding="utf-8",
    )
    rows = pnl_ledger.read_closed_trades(strategy_id="beta")
    assert [r["ord_id"] for r in rows] == ["2"]


# --- read-side dedupe -------------------------------------------------------

def test_read_closed_trades_deduplicates_on_read(ledger_dir):
    # Two rows with the same (exchange, inst_id, ord_id, mode) key but different
    # payloads -> collapsed to one on read, last occurrence wins.
    a = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "d1",
         "mode": "demo", "net_realized_pnl": 5.0, "closed_at_ms": 100}
    b = dict(a, net_realized_pnl=9.0)  # same key, later payload
    ledger_dir.write_text(
        json.dumps(a) + "\n" + json.dumps(b) + "\n", encoding="utf-8"
    )
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 1
    assert rows[0]["net_realized_pnl"] == 9.0  # last-wins


def test_read_closed_trades_keeps_different_keys(ledger_dir):
    a = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "k1",
         "mode": "demo", "closed_at_ms": 100}
    b = {"exchange": "okx", "inst_id": "ETH-USDT-SWAP", "ord_id": "k2",
         "mode": "demo", "closed_at_ms": 200}
    ledger_dir.write_text(
        json.dumps(a) + "\n" + json.dumps(b) + "\n", encoding="utf-8"
    )
    rows = pnl_ledger.read_closed_trades()
    assert sorted(r["ord_id"] for r in rows) == ["k1", "k2"]


def test_read_dedup_does_not_mutate_file(ledger_dir):
    # Deduping is read-only: the underlying file still holds both rows afterward.
    dup = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "m1",
           "mode": "demo", "net_realized_pnl": 3.0, "closed_at_ms": 100}
    raw = json.dumps(dup) + "\n" + json.dumps(dup) + "\n"
    ledger_dir.write_text(raw, encoding="utf-8")
    assert len(pnl_ledger.read_closed_trades()) == 1
    # File is untouched by the read.
    assert ledger_dir.read_text(encoding="utf-8") == raw
    assert ledger_dir.read_text(encoding="utf-8").count("\n") == 2


# --- accounting window (Phase 3) --------------------------------------------

def _write_window_rows(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "old",
                    "mode": "demo", "strategy_id": "alpha", "net_realized_pnl": 5.0,
                    "pnl_gross": 5.0, "fee_cost": 0.0, "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "new",
                      "mode": "demo", "strategy_id": "alpha", "net_realized_pnl": 7.0,
                      "pnl_gross": 7.0, "fee_cost": 0.0, "closed_at_ms": 300}) + "\n",
        encoding="utf-8",
    )


def test_read_accounting_start_drops_older_rows(ledger_dir):
    _write_window_rows(ledger_dir)
    rows = pnl_ledger.read_closed_trades(accounting_start_at=200)
    assert [r["ord_id"] for r in rows] == ["new"]


def test_read_accounting_start_none_returns_all(ledger_dir):
    _write_window_rows(ledger_dir)
    rows = pnl_ledger.read_closed_trades(accounting_start_at=None)
    assert [r["ord_id"] for r in rows] == ["old", "new"]


def test_read_accounting_start_combines_with_since_ms(ledger_dir):
    # The stricter (later) of the two floors wins: since_ms=50 + window=250 -> 250.
    _write_window_rows(ledger_dir)
    rows = pnl_ledger.read_closed_trades(since_ms=50, accounting_start_at=250)
    assert [r["ord_id"] for r in rows] == ["new"]
    # And the reverse ordering: since_ms=250 dominates a looser window.
    rows2 = pnl_ledger.read_closed_trades(since_ms=250, accounting_start_at=50)
    assert [r["ord_id"] for r in rows2] == ["new"]


def test_net_realized_respects_accounting_start(ledger_dir):
    _write_window_rows(ledger_dir)
    assert pnl_ledger.net_realized_for_strategy("alpha") == pytest.approx(12.0)
    assert pnl_ledger.net_realized_for_strategy(
        "alpha", accounting_start_at=200
    ) == pytest.approx(7.0)


# --- aggregate_strategy_pnl (Phase 3 contract) ------------------------------

def test_aggregate_respects_accounting_start(ledger_dir):
    _write_window_rows(ledger_dir)
    agg = pnl_ledger.aggregate_strategy_pnl(
        "alpha", budget_usd=1000.0, mode="demo", accounting_start_at=200, open_upl_usd=3.0
    )
    assert agg["closed_net_pnl_usd"] == pytest.approx(7.0)
    assert agg["closed_order_count"] == 1
    assert agg["open_upl_usd"] == pytest.approx(3.0)
    assert agg["equity_now_usd"] == pytest.approx(1000.0 + 7.0 + 3.0)
    assert agg["accounting_start_at"] == 200


def test_aggregate_closed_order_count_matches_rows(ledger_dir):
    _write_window_rows(ledger_dir)
    agg = pnl_ledger.aggregate_strategy_pnl("alpha", mode="demo")
    assert agg["closed_order_count"] == 2
    assert agg["closed_net_pnl_usd"] == pytest.approx(12.0)


def test_aggregate_absent_ledger_returns_zeros(ledger_dir):
    # No ledger file at all -> zeros + budget, never an error.
    agg = pnl_ledger.aggregate_strategy_pnl("ghost", budget_usd=500.0)
    assert agg["closed_net_pnl_usd"] == 0.0
    assert agg["closed_order_count"] == 0
    assert agg["equity_now_usd"] == pytest.approx(500.0)


# --- append / dedupe --------------------------------------------------------

def test_append_dedupes_by_composite_key(ledger_dir):
    entry = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "42",
             "mode": "demo", "closed_at_ms": 100}
    assert pnl_ledger.append_closed_trades([entry]) == 1
    # Same composite key, different payload -> not written again.
    dup = dict(entry, pnl_gross=999.0)
    assert pnl_ledger.append_closed_trades([dup]) == 0
    # Same ord_id but a different mode is a distinct key -> written.
    other_mode = dict(entry, mode="live")
    assert pnl_ledger.append_closed_trades([other_mode]) == 1
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 2


def test_append_empty_is_noop(ledger_dir):
    assert pnl_ledger.append_closed_trades([]) == 0
    assert not ledger_dir.exists()


def test_append_concurrent_safe(ledger_dir):
    # 10 threads each append 20 unique entries; all 200 must land exactly once.
    def worker(base: int) -> None:
        entries = [
            {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": f"{base}-{i}",
             "mode": "demo", "closed_at_ms": base * 1000 + i}
            for i in range(20)
        ]
        pnl_ledger.append_closed_trades(entries)

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = pnl_ledger.read_closed_trades()
    ord_ids = [r["ord_id"] for r in rows]
    assert len(ord_ids) == 200
    assert len(set(ord_ids)) == 200


# --- reconcile --------------------------------------------------------------

def test_reconcile_extracts_reduce_only_closes(ledger_dir):
    # OKX-style: adapter normalized reduceOnly to a bool True.
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "open1", "clOrdId": "mxcOpen",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False,
         "avgPx": "50000", "pnl": None, "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "close1", "clOrdId": "mxcClose",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True,
         "avgPx": "51000", "pnl": "123.45", "fee": "-0.5", "feeCcy": "USDT",
         "uTime": 200},
    ]
    written = pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert written == 1
    ledger = pnl_ledger.read_closed_trades()
    assert len(ledger) == 1
    row = ledger[0]
    assert row["ord_id"] == "close1"
    assert row["side"] == "sell"
    assert row["mode"] == "demo"
    assert row["exchange"] == "okx"
    assert row["pnl_gross"] == 123.45
    assert row["fee_cost"] == -0.5
    assert row["fee_currency"] == "USDT"
    assert row["closed_at_ms"] == 200


def test_reconcile_extracts_position_delta_close(ledger_dir):
    # Spot-style: no reduceOnly at all. A buy opens, the later sell reduces the
    # running position to zero -> detected as a close via position-delta.
    rows = [
        {"instId": "BTC-USDT", "ordId": "spotOpen", "clOrdId": "mxcBuy",
         "side": "buy", "accFillSz": 2.0, "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT", "ordId": "spotClose", "clOrdId": "mxcSell",
         "side": "sell", "accFillSz": 2.0, "avgPx": "52000", "uTime": 200},
    ]
    written = pnl_ledger.reconcile_from_order_history(rows, "binance", "live")
    assert written == 1
    ledger = pnl_ledger.read_closed_trades()
    assert [r["ord_id"] for r in ledger] == ["spotClose"]
    assert ledger[0]["mode"] == "live"


def test_reconcile_skips_external_orders(ledger_dir):
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "open1", "clOrdId": "externalOpen",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False, "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "close1", "clOrdId": "externalClose",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True, "uTime": 200},
    ]
    assert pnl_ledger.reconcile_from_order_history(rows, "okx", "demo") == 0
    assert pnl_ledger.read_closed_trades() == []


def test_reconcile_norm_realized_pnl_okx(ledger_dir):
    # The adapter maps info.pnl -> realized_pnl for OKX; pnl_gross prefers it.
    rows = [
        {"instId": "ETH-USDT-SWAP", "ordId": "c", "clOrdId": "mxcC", "side": "sell",
         "accFillSz": 1.0, "reduceOnly": True, "realized_pnl": 77.0, "pnl": "77.0",
         "uTime": 300},
    ]
    pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert pnl_ledger.read_closed_trades()[0]["pnl_gross"] == 77.0


def test_reconcile_norm_realized_pnl_hyperliquid(ledger_dir):
    # Hyperliquid rows arrive with closedPnl already normalized to realized_pnl.
    rows = [
        {"instId": "BTC", "ordId": "hlClose", "clOrdId": "mxcHL", "side": "sell",
         "accFillSz": 1.0, "reduceOnly": True, "realized_pnl": -12.5, "uTime": 400},
    ]
    pnl_ledger.reconcile_from_order_history(rows, "hyperliquid", "live")
    row = pnl_ledger.read_closed_trades()[0]
    assert row["pnl_gross"] == -12.5
    assert row["exchange"] == "hyperliquid"


def test_reconcile_is_idempotent(ledger_dir):
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "open1", "clOrdId": "mxcOpen",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False, "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "close1", "clOrdId": "mxcClose",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True, "uTime": 200},
    ]
    assert pnl_ledger.reconcile_from_order_history(rows, "okx", "demo") == 1
    # Re-running the same snapshot must not double-write.
    assert pnl_ledger.reconcile_from_order_history(rows, "okx", "demo") == 0
    assert len(pnl_ledger.read_closed_trades()) == 1


def test_reconcile_attributes_hyperliquid_numeric_cloid(ledger_dir):
    # Hyperliquid read-back carries a numeric cloid, not the submitted mxc id.
    # A mapped close is attributed to HermX (and stored with the original mxc id);
    # an unmapped numeric close is treated as external and skipped.
    import pnl_cloid_map
    pnl_cloid_map.record_cloid_mapping("mxc-hl-open", "111000111", "hyperliquid")
    pnl_cloid_map.record_cloid_mapping("mxc-hl-close", "222000222", "hyperliquid")
    rows = [
        {"instId": "BTC", "ordId": "hlOpen", "clOrdId": "111000111",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False, "uTime": 100},
        {"instId": "BTC", "ordId": "hlClose", "clOrdId": "222000222",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True,
         "realized_pnl": 42.0, "uTime": 200},
        # External close (no submit-time mapping) must be skipped.
        {"instId": "ETH", "ordId": "extOpen", "clOrdId": "999",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False, "uTime": 150},
        {"instId": "ETH", "ordId": "extClose", "clOrdId": "888",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True, "uTime": 250},
    ]
    written = pnl_ledger.reconcile_from_order_history(rows, "hyperliquid", "live")
    assert written == 1
    ledger = pnl_ledger.read_closed_trades()
    assert [r["ord_id"] for r in ledger] == ["hlClose"]
    # The original mxc id is preserved via the submit-time map.
    assert ledger[0]["cl_ord_id"] == "mxc-hl-close"


# --- fee-currency + None-pnl logging ----------------------------------------

def test_fee_currency_mismatch_logs_warning(ledger_dir, caplog):
    # Fee paid in BNB on a USDT-quoted instrument: warn and still persist the row.
    row = {"instId": "BTC-USDT-SWAP", "ordId": "feeMismatch", "clOrdId": "mxcFee",
           "side": "sell", "accFillSz": 1.0, "reduceOnly": True, "avgPx": "51000",
           "pnl": "10.0", "fee": "-0.02", "feeCcy": "BNB", "uTime": 200}
    with caplog.at_level("WARNING", logger="pnl_ledger"):
        written = pnl_ledger.reconcile_from_order_history([row], "okx", "demo")
    assert written == 1
    assert any("fee_currency_mismatch" in r.getMessage() for r in caplog.records)


def test_none_pnl_logs_warning(ledger_dir, caplog):
    # A HermX close with no realized-pnl field: warn (pnl_gross_is_none) and persist.
    row = {"instId": "BTC-USDT-SWAP", "ordId": "nonePnl", "clOrdId": "mxcNone",
           "side": "sell", "accFillSz": 1.0, "reduceOnly": True, "avgPx": "51000",
           "uTime": 200}
    with caplog.at_level("WARNING", logger="pnl_ledger"):
        written = pnl_ledger.reconcile_from_order_history([row], "okx", "demo")
    assert written == 1
    assert any("pnl_gross_is_none" in r.getMessage() for r in caplog.records)
    assert pnl_ledger.read_closed_trades()[0]["pnl_gross"] is None


# --- P0-2 signed_qty accumulator hardening ----------------------------------

def test_float_residue_does_not_create_phantom_close(ledger_dir):
    # 0.1 + 0.2 - 0.3 leaves a 5.55e-17 float residue instead of exactly 0.0.
    # Without snap-to-zero that residue makes the following short-open (opposite
    # side of the +residue) mis-detect as a close; snap-to-zero suppresses it so
    # only the genuine close (c1) is ledgered.
    rows = [
        {"instId": "BTC-USDT", "ordId": "b1", "clOrdId": "mxc1", "side": "buy",
         "accFillSz": 0.1, "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT", "ordId": "b2", "clOrdId": "mxc2", "side": "buy",
         "accFillSz": 0.2, "avgPx": "50000", "uTime": 200},
        {"instId": "BTC-USDT", "ordId": "c1", "clOrdId": "mxc3", "side": "sell",
         "accFillSz": 0.3, "avgPx": "51000", "pnl": "10.0", "uTime": 300},
        {"instId": "BTC-USDT", "ordId": "s1", "clOrdId": "mxc4", "side": "sell",
         "accFillSz": 0.5, "avgPx": "52000", "uTime": 400},
    ]
    written = pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert written == 1
    assert [r["ord_id"] for r in pnl_ledger.read_closed_trades()] == ["c1"]


def test_snap_to_zero_after_equal_and_opposite_fills(ledger_dir):
    # 0.3 + 0.3 - 0.6 = 5.55e-17 residue; equal-and-opposite fills must leave the
    # running position snapped to 0.0 so the next same-direction open is not a
    # phantom close. Observed through reconcile output (positions is function-local).
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "o1", "clOrdId": "mxc1", "side": "buy",
         "accFillSz": 0.3, "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "o2", "clOrdId": "mxc2", "side": "buy",
         "accFillSz": 0.3, "avgPx": "50000", "uTime": 200},
        {"instId": "BTC-USDT-SWAP", "ordId": "c1", "clOrdId": "mxc3", "side": "sell",
         "accFillSz": 0.6, "avgPx": "51000", "pnl": "5.0", "uTime": 300},
        {"instId": "BTC-USDT-SWAP", "ordId": "reopen", "clOrdId": "mxc4", "side": "buy",
         "accFillSz": 1.0, "avgPx": "52000", "uTime": 400},
    ]
    written = pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert written == 1
    assert [r["ord_id"] for r in pnl_ledger.read_closed_trades()] == ["c1"]


def test_sign_flip_warning_is_logged(ledger_dir, caplog):
    # prev = +0.5, a single sell of 1.0 overshoots zero to -0.5. The flip is logged
    # (informational) and the close IS detected (prev > 0, sell = opposite side).
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "open1", "clOrdId": "mxcOpen",
         "side": "buy", "accFillSz": 0.5, "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "flip1", "clOrdId": "mxcFlip",
         "side": "sell", "accFillSz": 1.0, "avgPx": "51000", "pnl": "7.0",
         "uTime": 200},
    ]
    with caplog.at_level("WARNING", logger="pnl_ledger"):
        written = pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert written == 1
    assert any("reconcile_position_sign_flip" in r.message for r in caplog.records)


def test_sign_guard_mismatch_logs_and_continues(ledger_dir, caplog):
    # reduceOnly=True on a same-direction (buy-on-long) fill forces is_close while
    # the side is wrong for a close -> the guard logs and the row is still processed.
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "open1", "clOrdId": "mxcOpen",
         "side": "buy", "accFillSz": 1.0, "reduceOnly": False,
         "avgPx": "50000", "uTime": 100},
        {"instId": "BTC-USDT-SWAP", "ordId": "bad1", "clOrdId": "mxcBad",
         "side": "buy", "accFillSz": 0.5, "reduceOnly": True,
         "avgPx": "51000", "pnl": "1.0", "uTime": 200},
    ]
    with caplog.at_level("WARNING", logger="pnl_ledger"):
        written = pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    assert any("reconcile_sign_guard_mismatch" in r.message for r in caplog.records)
    assert written == 1
    assert [r["ord_id"] for r in pnl_ledger.read_closed_trades()] == ["bad1"]


# --- terminal-only ledgering (skip in-flight partials) ----------------------

def _term_rows(*, state="__absent__", reduce_only=False):
    # A position-delta close (buy opens, sell reduces to flat). ``state`` and
    # ``reduce_only`` on the close row vary per test; state="__absent__" omits it.
    close = {"instId": "BTC-USDT-SWAP", "ordId": "c", "clOrdId": "mxcClose",
             "side": "sell", "accFillSz": 1.0, "avgPx": "51000", "pnl": "10.0",
             "reduceOnly": reduce_only, "uTime": 200}
    if state != "__absent__":
        close["state"] = state
    return [
        {"instId": "BTC-USDT-SWAP", "ordId": "o", "clOrdId": "mxcOpen",
         "side": "buy", "accFillSz": 1.0, "avgPx": "50000", "uTime": 100},
        close,
    ]


def test_nonterminal_row_is_skipped(ledger_dir):
    written = pnl_ledger.reconcile_from_order_history(
        _term_rows(state="live", reduce_only=False), "okx", "demo"
    )
    assert written == 0
    assert pnl_ledger.read_closed_trades() == []


def test_reduce_only_nonterminal_is_processed(ledger_dir):
    # reduceOnly says it's an explicit close -> processed regardless of state.
    written = pnl_ledger.reconcile_from_order_history(
        _term_rows(state="live", reduce_only=True), "okx", "demo"
    )
    assert written == 1


def test_absent_state_is_processed(ledger_dir):
    # No state field -> fail open (assume terminal) -> processed.
    written = pnl_ledger.reconcile_from_order_history(
        _term_rows(reduce_only=False), "okx", "demo"
    )
    assert written == 1


def test_terminal_state_is_processed(ledger_dir):
    written = pnl_ledger.reconcile_from_order_history(
        _term_rows(state="filled", reduce_only=False), "okx", "demo"
    )
    assert written == 1


# --- P0-1 max_recorded_closed_at (age-out high-water helper) -----------------

def test_max_recorded_closed_at_returns_max(ledger_dir):
    pnl_ledger.append_closed_trades([
        {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "a",
         "mode": "demo", "closed_at_ms": 1000},
        {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "b",
         "mode": "demo", "closed_at_ms": 3000},
        {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "c",
         "mode": "demo", "closed_at_ms": 2000},
    ])
    assert pnl_ledger.max_recorded_closed_at("okx", "demo") == 3000


def test_max_recorded_closed_at_filters_by_env(ledger_dir):
    pnl_ledger.append_closed_trades([
        {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "a",
         "mode": "demo", "closed_at_ms": 1000},
        {"exchange": "bybit", "inst_id": "BTC-USDT-SWAP", "ord_id": "b",
         "mode": "live", "closed_at_ms": 9000},
    ])
    assert pnl_ledger.max_recorded_closed_at("okx", "demo") == 1000
    assert pnl_ledger.max_recorded_closed_at("bybit", "live") == 9000


def test_max_recorded_closed_at_none_when_empty(ledger_dir):
    assert pnl_ledger.max_recorded_closed_at("okx", "demo") is None


# --- P1-1 dual timestamps (schema v3, recorded_at_ms) -----------------------

def test_recorded_at_ms_written_on_new_rows(ledger_dir):
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "c", "clOrdId": "mxcC", "side": "sell",
         "accFillSz": 1.0, "reduceOnly": True, "pnl": "1.0", "uTime": 300},
    ]
    assert pnl_ledger.reconcile_from_order_history(rows, "okx", "demo") == 1
    row = pnl_ledger.read_closed_trades()[0]
    assert isinstance(row["recorded_at_ms"], int)
    assert row["recorded_at_ms"] > 0
    assert row["schema_version"] == 3


def test_v2_rows_backfill_recorded_at_none_on_read(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v2",
                    "mode": "demo", "closed_at_ms": 100, "pnl_gross": 5.0,
                    "fee_cost": -0.1, "net_realized_pnl": 4.9,
                    "schema_version": 2}) + "\n",
        encoding="utf-8",
    )
    row = pnl_ledger.read_closed_trades()[0]
    assert "recorded_at_ms" in row
    assert row["recorded_at_ms"] is None


def test_schema_v3_reader_reads_v1_v2_v3_mixed_ledger(ledger_dir):
    ledger_dir.write_text(
        "\n".join([
            # v1: no net_realized_pnl, no recorded_at_ms
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v1",
                        "mode": "demo", "closed_at_ms": 100, "pnl_gross": 5.0,
                        "fee_cost": -0.1}),
            # v2: net present, no recorded_at_ms
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v2",
                        "mode": "demo", "closed_at_ms": 200, "pnl_gross": 3.0,
                        "fee_cost": -0.2, "net_realized_pnl": 2.8, "schema_version": 2}),
            # v3: both present
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v3",
                        "mode": "demo", "closed_at_ms": 300, "pnl_gross": 1.0,
                        "fee_cost": -0.05, "net_realized_pnl": 0.95,
                        "recorded_at_ms": 123456789, "schema_version": 3}),
        ]) + "\n",
        encoding="utf-8",
    )
    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 3
    for row in rows:
        assert "net_realized_pnl" in row
        assert "recorded_at_ms" in row
    by_id = {r["ord_id"]: r for r in rows}
    assert by_id["v1"]["recorded_at_ms"] is None
    assert by_id["v2"]["recorded_at_ms"] is None
    assert by_id["v3"]["recorded_at_ms"] == 123456789
    assert by_id["v1"]["net_realized_pnl"] == 4.9


# --- P1-2 reconcile_health_stats --------------------------------------------

def test_reconcile_health_stats_with_recorded_at(ledger_dir):
    ledger_dir.write_text(
        "\n".join([
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "a",
                        "mode": "demo", "closed_at_ms": 100, "recorded_at_ms": 1000,
                        "schema_version": 3}),
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "b",
                        "mode": "demo", "closed_at_ms": 200, "recorded_at_ms": 3000,
                        "schema_version": 3}),
        ]) + "\n",
        encoding="utf-8",
    )
    stats = pnl_ledger.reconcile_health_stats()
    assert stats["max_recorded_at_ms"] == 3000
    assert isinstance(stats["reconcile_lag_ms"], int)
    assert stats["reconcile_lag_ms"] > 0
    assert stats["recorded_at_rows_pct"] == 1.0


def test_reconcile_health_stats_empty_ledger(ledger_dir):
    stats = pnl_ledger.reconcile_health_stats()
    assert stats["max_recorded_at_ms"] is None
    assert stats["reconcile_lag_ms"] is None
    assert stats["recorded_at_rows_pct"] is None


def test_reconcile_health_stats_mixed_v2_v3(ledger_dir):
    ledger_dir.write_text(
        "\n".join([
            # v2 row: no recorded_at_ms
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v2",
                        "mode": "demo", "closed_at_ms": 100, "net_realized_pnl": 1.0,
                        "schema_version": 2}),
            # v3 row: recorded_at_ms present
            json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v3",
                        "mode": "demo", "closed_at_ms": 200, "recorded_at_ms": 5000,
                        "schema_version": 3}),
        ]) + "\n",
        encoding="utf-8",
    )
    stats = pnl_ledger.reconcile_health_stats()
    assert stats["max_recorded_at_ms"] == 5000
    assert stats["recorded_at_rows_pct"] == 0.5
    assert stats["recorded_at_rows_pct"] < 1.0
