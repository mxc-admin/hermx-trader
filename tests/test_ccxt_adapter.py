from __future__ import annotations

from pathlib import Path

from executors.ccxt_adapter import CcxtExecutor, _okx_inst_to_ccxt_symbol


class _FakeClient:
    def __init__(self):
        self.calls = []

    def load_markets(self):
        return None

    def market(self, _symbol):
        return {
            "contract": True,
            "contractSize": 0.01,
            "precision": {"amount": 0},
            "limits": {"amount": {"min": 1}},
        }

    def fetch_positions(self, symbols=None):
        if symbols:
            return [
                {
                    "symbol": symbols[0],
                    "side": "short",
                    "contracts": 3,
                    "entryPrice": 60500.0,
                    "unrealizedPnl": 12.0,
                    "info": {"instId": "BTC-USDT-SWAP", "posSide": "short"},
                }
            ]
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "short",
                "contracts": 3,
                "entryPrice": 60500.0,
                "unrealizedPnl": 12.0,
                "info": {"instId": "BTC-USDT-SWAP", "posSide": "short", "mgnMode": "cross", "lever": "2"},
            }
        ]

    def fetch_ticker(self, _symbol):
        return {"last": 60000.0}

    def create_order(self, symbol, order_type, side, amount, price, params):
        self.calls.append(
            {
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params or {}),
            }
        )
        return {
            "id": f"ord-{len(self.calls)}",
            "symbol": symbol,
            "clientOrderId": params.get("clientOrderId"),
            "status": "open",
            "average": None,
            "filled": 0,
            "amount": amount,
            "timestamp": 1719300000000,
            "info": {
                "clOrdId": params.get("clOrdId"),
                "posSide": params.get("posSide"),
                "reduceOnly": params.get("reduceOnly"),
            },
        }

    def fetch_closed_orders(self, symbol=None, limit=100):
        _ = limit
        return [
            {
                "id": "ord-h-1",
                "symbol": symbol or "BTC/USDT:USDT",
                "clientOrderId": "cid-1",
                "status": "closed",
                "side": "sell",
                "type": "market",
                "average": 61000.0,
                "filled": 2,
                "amount": 2,
                "timestamp": 1719300002000,
                "lastTradeTimestamp": 1719300003000,
                "fee": {"cost": 1.2, "currency": "USDT"},
                "info": {
                    "instId": "BTC-USDT-SWAP",
                    "clOrdId": "cid-1",
                    "posSide": "long",
                    "tdMode": "cross",
                    "fillSz": "2",
                    "pnl": "5.5",
                    "reduceOnly": True,
                    "lever": "2",
                },
            }
        ]

    def fetch_open_orders(self, symbol=None):
        return []

    def fetch_order(self, order_id, symbol=None):
        return {
            "id": order_id,
            "symbol": symbol,
            "clientOrderId": "cid-1",
            "status": "closed",
            "side": "sell",
            "type": "market",
            "average": 61000.0,
            "filled": 2,
            "amount": 2,
            "timestamp": 1719300003000,
            "info": {"instId": "BTC-USDT-SWAP", "clOrdId": "cid-1", "posSide": "long"},
        }

    def fetch_balance(self):
        return {
            "total": {"USDT": 1000.0},
            "free": {"USDT": 900.0},
            "info": {"posMode": "long_short_mode"},
        }


def _executor() -> CcxtExecutor:
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "simulated_trading": True,
            "ccxt_pos_mode": "long_short_mode",
            "td_mode": "cross",
        }
    }
    return CcxtExecutor(cfg, Path("."))


def test_symbol_mapping_linear_and_inverse_swap():
    assert _okx_inst_to_ccxt_symbol("BTC-USDT-SWAP") == "BTC/USDT:USDT"
    assert _okx_inst_to_ccxt_symbol("BTC-USD-SWAP") == "BTC/USD:BTC"


def test_execute_contract_sizing_and_close_flip_semantics(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda: fake)

    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-p5",
            "target_direction": "long",
            "planned_notional_usd": 1500.0,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"],
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"
    assert len(fake.calls) == 2

    close_call = fake.calls[0]
    open_call = fake.calls[1]

    assert close_call["side"] == "buy"
    assert close_call["amount"] == 3
    assert close_call["params"].get("reduceOnly") is True
    assert close_call["params"].get("posSide") == "short"

    assert open_call["side"] == "buy"
    assert open_call["amount"] == 2
    assert open_call["params"].get("reduceOnly") is None
    assert open_call["params"].get("posSide") == "long"


def test_get_order_history_raw_shape(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda: fake)

    rows = ex.get_order_history_raw(["BTC-USDT-SWAP"], limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["instId"] == "BTC-USDT-SWAP"
    assert row["ordId"] == "ord-h-1"
    assert row["clOrdId"] == "cid-1"
    assert row["reduceOnly"] is True


def test_health_snapshot_shape(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda: fake)

    snap = ex.health()

    assert snap["ok"] is True
    assert snap["exchange"] == "ccxt"
    assert "generated_at" in snap
    assert isinstance(snap.get("positions"), list)
    assert snap["positions"][0]["instId"] == "BTC-USDT-SWAP"
    assert snap["positions"][0]["mgnMode"] == "cross"
