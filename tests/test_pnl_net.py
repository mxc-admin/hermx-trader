"""Tests for fee-correct net realized P&L (P&L Master Plan, Phase 2).

Net is derived from the stored ``pnl_gross`` + signed ``fee_cost`` per venue
semantics (``ORDER_PNL_IS_NET``). Gross remains the displayed value (Decision ②:
gross first, verify net later); these tests only cover the ledger's net math and
its read-time back-fill of pre-Phase-2 (v1) rows.
"""
from __future__ import annotations

import json

import pytest

import pnl_ledger


# --- _compute_net_realized --------------------------------------------------

def test_compute_net_default_gross():
    # Default venue: pnl is gross, fee is signed negative -> net = gross + fee.
    assert pnl_ledger._compute_net_realized(100.0, -1.0, "okx") == 99.0


def test_compute_net_with_none_fee():
    # Missing fee is treated as zero (best available), not as unknown.
    assert pnl_ledger._compute_net_realized(100.0, None, "okx") == 100.0


def test_compute_net_with_none_gross():
    # Unknown gross -> unknown net, regardless of fee.
    assert pnl_ledger._compute_net_realized(None, -1.0, "okx") is None


def test_compute_net_venue_net_true(monkeypatch):
    # When the venue's pnl already includes fees, net == gross (not gross+fee).
    monkeypatch.setitem(pnl_ledger.ORDER_PNL_IS_NET, "okx", True)
    assert pnl_ledger._compute_net_realized(99.0, -1.0, "okx") == 99.0


def test_compute_net_zero_fee():
    assert pnl_ledger._compute_net_realized(100.0, 0.0, "okx") == 100.0


def test_compute_net_unknown_venue_defaults_gross():
    # Venue absent from ORDER_PNL_IS_NET defaults to gross (add fee).
    assert pnl_ledger._compute_net_realized(100.0, -2.5, "kraken") == 97.5


@pytest.mark.parametrize("exchange", ["okx", "hyperliquid", "binance", "bybit"])
def test_net_equals_gross_plus_signed_fee_per_venue(exchange):
    # All configured venues default to gross semantics -> net = gross + fee.
    assert pnl_ledger._compute_net_realized(50.0, -0.75, exchange) == 49.25


# --- ledger row carries net -------------------------------------------------

def test_ledger_row_contains_net(ledger_dir):
    rows = [
        {"instId": "BTC-USDT-SWAP", "ordId": "close1", "clOrdId": "mxcClose",
         "side": "sell", "accFillSz": 1.0, "reduceOnly": True,
         "avgPx": "51000", "pnl": "123.45", "fee": "-0.5", "feeCcy": "USDT",
         "uTime": 200},
    ]
    pnl_ledger.reconcile_from_order_history(rows, "okx", "demo")
    row = pnl_ledger.read_closed_trades()[0]
    assert row["pnl_gross"] == 123.45
    assert row["fee_cost"] == -0.5
    assert row["net_realized_pnl"] == pytest.approx(122.95)
    assert row["schema_version"] == pnl_ledger.SCHEMA_VERSION


# --- read-time back-fill of v1 rows -----------------------------------------

def test_v1_rows_backfill_net_on_read(ledger_dir):
    # A pre-Phase-2 row: no net_realized_pnl / schema_version. read_closed_trades
    # must derive net from stored gross + fee without mutating the file.
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "v1",
                    "mode": "demo", "pnl_gross": 100.0, "fee_cost": -1.0,
                    "closed_at_ms": 100}) + "\n",
        encoding="utf-8",
    )
    row = pnl_ledger.read_closed_trades()[0]
    assert row["net_realized_pnl"] == 99.0
    # File itself is untouched (append-only; derivation is read-only).
    on_disk = json.loads(ledger_dir.read_text(encoding="utf-8").strip())
    assert "net_realized_pnl" not in on_disk


def test_v1_row_missing_fee_backfills_to_gross(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "v1b",
                    "mode": "demo", "pnl_gross": 42.0, "closed_at_ms": 100}) + "\n",
        encoding="utf-8",
    )
    assert pnl_ledger.read_closed_trades()[0]["net_realized_pnl"] == 42.0


# --- net_realized_for_strategy ----------------------------------------------

def test_net_realized_for_strategy_sums(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "1", "mode": "demo",
                    "strategy_id": "alpha", "net_realized_pnl": 10.0,
                    "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "2", "mode": "demo",
                      "strategy_id": "alpha", "net_realized_pnl": -3.0,
                      "closed_at_ms": 200}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "C", "ord_id": "3", "mode": "demo",
                      "strategy_id": "beta", "net_realized_pnl": 999.0,
                      "closed_at_ms": 300}) + "\n",
        encoding="utf-8",
    )
    assert pnl_ledger.net_realized_for_strategy("alpha") == pytest.approx(7.0)


def test_net_realized_for_strategy_filters_mode(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "1", "mode": "demo",
                    "strategy_id": "alpha", "net_realized_pnl": 10.0,
                    "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "2", "mode": "live",
                      "strategy_id": "alpha", "net_realized_pnl": 5.0,
                      "closed_at_ms": 200}) + "\n",
        encoding="utf-8",
    )
    assert pnl_ledger.net_realized_for_strategy("alpha", mode="demo") == 10.0
    assert pnl_ledger.net_realized_for_strategy("alpha", mode="live") == 5.0
    assert pnl_ledger.net_realized_for_strategy("alpha") == pytest.approx(15.0)


def test_net_realized_for_strategy_none_counts_as_zero(ledger_dir):
    ledger_dir.write_text(
        json.dumps({"exchange": "okx", "inst_id": "A", "ord_id": "1", "mode": "demo",
                    "strategy_id": "alpha", "net_realized_pnl": None,
                    "closed_at_ms": 100}) + "\n"
        + json.dumps({"exchange": "okx", "inst_id": "B", "ord_id": "2", "mode": "demo",
                      "strategy_id": "alpha", "net_realized_pnl": 8.0,
                      "closed_at_ms": 200}) + "\n",
        encoding="utf-8",
    )
    assert pnl_ledger.net_realized_for_strategy("alpha") == 8.0
