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


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    """Isolated data dir for the ledger; returns the closed-trades.jsonl path."""
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    return tmp_path / "closed-trades.jsonl"


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
