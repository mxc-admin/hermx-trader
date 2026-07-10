"""Venue-neutral observe-only query-contract tests (REFACTOR_PLAN.md:207, :209-212,
:237) exercised through CcxtExecutor with a FAKE ccxt client (no network).

Post P5-07 the OKX-CLI executor and its pure normalizer were removed; CCXT is the
sole backend. The venue-neutral query CONTRACT is unchanged, so the coverage that
matters -- normalized order/position/balance shapes, the order state mapping
(live / partially_filled / filled / canceled), and not_found-is-not-an-exception --
is preserved here against CcxtExecutor's query methods. The OKX-v5-envelope pure
normalizer and CLI-dispatch cases were CLI-specific and have no target anymore.
"""
from __future__ import annotations

from pathlib import Path

from executors.base import BaseExecutor
from executors.ccxt_adapter import CcxtExecutor


class _FakeQueryClient:
    """In-memory ccxt client: serves canned unified orders/positions/balance and
    records calls. No network, no submission (read-only verbs only)."""

    def __init__(self, *, order=None, open_orders=None, closed_orders=None,
                 positions=None, balance=None, fetch_order_error=None):
        self._order = order
        self._open = open_orders or []
        self._closed = closed_orders or []
        self._positions = positions or []
        self._balance = balance or {"total": {"USDT": 1000.0}, "free": {"USDT": 900.0}, "info": {}}
        self._fetch_order_error = fetch_order_error
        self.calls = []

    def fetch_order(self, order_id, symbol=None):
        self.calls.append(("fetch_order", order_id, symbol))
        if self._fetch_order_error is not None:
            raise self._fetch_order_error
        return self._order

    def fetch_open_orders(self, symbol=None):
        self.calls.append(("fetch_open_orders", symbol))
        return list(self._open)

    def fetch_closed_orders(self, symbol=None, limit=100):
        self.calls.append(("fetch_closed_orders", symbol, limit))
        return list(self._closed)

    def fetch_positions(self, symbols=None):
        self.calls.append(("fetch_positions", symbols))
        return list(self._positions)

    def fetch_balance(self):
        self.calls.append(("fetch_balance",))
        return self._balance


def _executor() -> CcxtExecutor:
    return CcxtExecutor(
        {"execution": {"exchange": "ccxt", "ccxt_exchange": "okx", "simulated_trading": True}},
        Path("."),
    )


def _order(*, status, side="buy", filled=3, amount=3, average=63000.5, cl="cid-1", pos_side="long"):
    return {
        "id": "688577747503788032",
        "symbol": "BTC/USDT:USDT",
        "clientOrderId": cl,
        "status": status,
        "side": side,
        "type": "market",
        "average": average,
        "filled": filled,
        "amount": amount,
        "timestamp": 1719300001000,
        "info": {"instId": "BTC-USDT-SWAP", "clOrdId": cl, "posSide": pos_side},
    }


# ---------------------------------------------------------------------------
# (a) Normalized order shape + state mapping (the heart of acceptance :237).
# ---------------------------------------------------------------------------

def test_get_order_filled(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(order=_order(status="closed")))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="688577747503788032")
    assert out["exchange"] == "ccxt"
    assert out["state"] == "filled"
    assert out["inst_id"] == "BTC-USDT-SWAP"
    assert out["ord_id"] == "688577747503788032"
    assert out["cl_ord_id"] == "cid-1"
    assert out["acc_fill_sz"] == 3.0
    assert isinstance(out["acc_fill_sz"], float)
    assert out["avg_px"] == 63000.5
    assert out["side"] == "buy"
    assert out["pos_side"] == "long"
    assert out["ord_type"] == "market"


def test_get_order_partially_filled(monkeypatch):
    # status closed but 0 < filled < amount => partially_filled (-> FILLED+partial in Task 4).
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(order=_order(status="closed", filled=1, amount=3, average=3010.25)))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "partially_filled"
    assert out["acc_fill_sz"] == 1.0
    assert 0.0 < out["acc_fill_sz"] < float(out["raw"]["amount"])
    assert out["avg_px"] == 3010.25


def test_get_order_live_pending(monkeypatch):
    # status open + zero fill => live.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(order=_order(status="open", filled=0, average=None)))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "live"
    assert out["acc_fill_sz"] == 0.0


def test_get_order_canceled_zero_fill(monkeypatch):
    # canceled + accFillSz == 0 -> REJECTED later (:211). Empty avgPx -> None, not 0.0.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(order=_order(status="canceled", filled=0, average=None)))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "canceled"
    assert out["acc_fill_sz"] == 0.0
    assert out["avg_px"] is None


def test_get_order_not_found_is_not_an_exception(monkeypatch):
    # cl_ord_id lookup with nothing in open/closed -> normalized not_found, never raises.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(open_orders=[], closed_orders=[]))
    out = ex.get_order("BTC-USDT-SWAP", cl_ord_id="missing-cid")
    assert out["state"] == "not_found"
    assert out["exchange"] == "ccxt"
    assert out["acc_fill_sz"] == 0.0
    assert out["ord_id"] is None


def test_get_order_by_cl_ord_id_matches_closed(monkeypatch):
    ex = _executor()
    fake = _FakeQueryClient(open_orders=[], closed_orders=[_order(status="closed", cl="cid-match")])
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)
    out = ex.get_order("BTC-USDT-SWAP", cl_ord_id="cid-match")
    assert out["state"] == "filled"
    assert out["cl_ord_id"] == "cid-match"
    # Fallback chain: open orders consulted before closed orders.
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["fetch_open_orders", "fetch_closed_orders"]


def test_get_order_error_degrades_safely(monkeypatch):
    # A generic client error -> normalized error state, never raises.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(fetch_order_error=RuntimeError("boom")))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "error"
    assert out["exchange"] == "ccxt"


def test_get_order_not_found_error_text_maps_not_found(monkeypatch):
    # An explicit "order does not exist" venue error maps to not_found, not error.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(fetch_order_error=RuntimeError("Order does not exist")))
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "not_found"


# ---------------------------------------------------------------------------
# (b) Positions + balances normalized shapes.
# ---------------------------------------------------------------------------

def test_get_positions_open_is_signed(monkeypatch):
    ex = _executor()
    positions = [{
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 3,
        "entryPrice": 60000.0, "unrealizedPnl": 12.5,
        "info": {"instId": "BTC-USDT-SWAP", "posSide": "long"},
    }]
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(positions=positions))
    rows = ex.get_positions("BTC-USDT-SWAP")
    assert len(rows) == 1
    pos = rows[0]
    assert pos["exchange"] == "ccxt"
    assert pos["inst_id"] == "BTC-USDT-SWAP"
    assert pos["pos"] == 3.0  # long -> positive signed contracts
    assert pos["upl"] == 12.5


def test_get_balance(monkeypatch):
    ex = _executor()
    balance = {"total": {"USDT": 100012.5}, "free": {"USDT": 99000.0}, "info": {}}
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(balance=balance))
    rows = ex.get_balance("USDT")
    assert len(rows) == 1
    bal = rows[0]
    assert bal["exchange"] == "ccxt"
    assert bal["ccy"] == "USDT"
    assert bal["eq"] == 100012.5
    assert bal["avail"] == 99000.0


def test_get_balance_ccy_filter(monkeypatch):
    ex = _executor()
    balance = {"total": {"USDT": 100012.5, "BTC": 0.0}, "free": {"USDT": 99000.0, "BTC": 0.0}, "info": {}}
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeQueryClient(balance=balance))
    assert [r["ccy"] for r in ex.get_balance("USDT")] == ["USDT"]


# ---------------------------------------------------------------------------
# (c) Base-executor query defaults degrade, never crash (venue-neutral default).
# ---------------------------------------------------------------------------

def test_base_executor_query_defaults_are_safe():
    class Bare(BaseExecutor):
        key = "bare"

        def execute(self, readiness):  # only abstract method
            return self.normalized_result(ok=True, mode="noop")

    ex = Bare({}, Path(__file__).resolve().parents[1])
    assert ex.get_order("X")["state"] == "not_implemented"
    assert ex.get_open_orders() == []
    assert ex.get_order_history_archive() == []
    assert ex.get_positions() == []
    assert ex.get_balance() == []
