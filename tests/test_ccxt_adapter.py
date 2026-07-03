from __future__ import annotations

import re
import types
from pathlib import Path

import executors.ccxt_adapter as ccxt_adapter
from executors.ccxt_adapter import (
    CcxtExecutor,
    _inst_id_to_ccxt_symbol,
    _to_hyperliquid_cloid,
)


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
            "td_mode": "cross",
        }
    }
    return CcxtExecutor(cfg, Path("."))


def test_symbol_mapping_linear_and_inverse_swap():
    assert _inst_id_to_ccxt_symbol("BTC-USDT-SWAP") == "BTC/USDT:USDT"
    assert _inst_id_to_ccxt_symbol("BTC-USD-SWAP") == "BTC/USD:BTC"


def test_execute_contract_sizing_and_close_flip_semantics(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

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
    # posSide is no longer emitted: the ccxt_pos_mode branch was dead (nothing in
    # production ever set it) and was removed. Hedge-mode posSide is out of scope.
    assert close_call["params"].get("posSide") is None

    assert open_call["side"] == "buy"
    assert open_call["amount"] == 2
    assert open_call["params"].get("reduceOnly") is None
    assert open_call["params"].get("posSide") is None


def test_get_order_history_raw_shape(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

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
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    snap = ex.health()

    assert snap["ok"] is True
    assert snap["exchange"] == "ccxt"
    assert "generated_at" in snap
    assert isinstance(snap.get("positions"), list)
    assert snap["positions"][0]["instId"] == "BTC-USDT-SWAP"
    assert snap["positions"][0]["mgnMode"] == "cross"


# ---------------------------------------------------------------------------
# Hyperliquid-specific fixes
# ---------------------------------------------------------------------------

_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")


def _hl_executor() -> CcxtExecutor:
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": "hyperliquid",
            "simulated_trading": True,
        }
    }
    return CcxtExecutor(cfg, Path("."))


class _FakeHLClient(_FakeClient):
    # Hyperliquid is not OKX-style contract sizing: allow fractional amounts so a
    # small planned notional still produces a non-zero order.
    def market(self, _symbol):
        return {
            "contract": True,
            "contractSize": 1.0,
            "precision": {"amount": 4},
            "limits": {"amount": {"min": 0.0001}},
        }

    def fetch_positions(self, symbols=None):
        return []

    def fetch_ticker(self, _symbol):
        return {"last": 60000.0}


class _FakeHLLongClient(_FakeHLClient):
    def fetch_positions(self, symbols=None):
        symbol = symbols[0] if symbols else "SOL/USDC:USDC"
        return [
            {
                "symbol": symbol,
                "side": "long",
                "contracts": 5,
                "entryPrice": 59000.0,
                "unrealizedPnl": 3.0,
                "info": {},
            }
        ]


def _hl_open_readiness() -> dict:
    return {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "SOL-USDC-SWAP",
        "execution_intent": {
            "client_order_id": "550e8400-e29b-41d4-a716-446655440000",
            "client_order_id_open": "550e8400-e29b-41d4-a716-446655440001",
            "target_direction": "long",
            "planned_notional_usd": 50.0,
            "actions": ["OPEN_LONG"],
        },
    }


def test_to_hyperliquid_cloid_format_and_determinism():
    cloid = _to_hyperliquid_cloid("550e8400-e29b-41d4-a716-446655440000")
    assert isinstance(cloid, str)
    assert _CLOID_RE.match(cloid) is not None
    # Deterministic: same input -> same output.
    assert cloid == _to_hyperliquid_cloid("550e8400-e29b-41d4-a716-446655440000")


def test_hyperliquid_params_sets_cloid_and_drops_okx_fields(monkeypatch):
    ex = _hl_executor()
    fake = _FakeHLClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_hl_open_readiness())

    assert out["ok"] is True
    assert len(fake.calls) == 1
    params = fake.calls[0]["params"]
    # The cloid must be carried under ``clientOrderId`` -- the key ccxt actually reads
    # for Hyperliquid. A ``cloid`` key would be silently dropped (order carries no id).
    assert _CLOID_RE.match(params.get("clientOrderId") or "") is not None
    assert "cloid" not in params
    assert "clOrdId" not in params
    assert "tdMode" not in params
    assert "reduceOnly" not in params


def test_hyperliquid_market_order_passes_reference_price(monkeypatch):
    ex = _hl_executor()
    fake = _FakeHLClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    ex.execute(_hl_open_readiness())

    assert len(fake.calls) == 1
    assert fake.calls[0]["price"] == 60000.0


def test_okx_params_unchanged_regression_guard(monkeypatch):
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    # _FakeClient reports a short position, so an OPEN_LONG must be preceded by a
    # CLOSE_OPPOSITE_IF_ANY (same shape as the existing flip-semantics test) for
    # orders to actually be submitted.
    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-okx-open",
            "target_direction": "long",
            "planned_notional_usd": 1500.0,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"],
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is True
    assert len(fake.calls) == 2
    params = fake.calls[0]["params"]
    assert params.get("clOrdId") == "cid-okx-open"
    assert params.get("tdMode") == "cross"
    assert fake.calls[0]["price"] is None
    # The open leg keeps the OKX clOrdId/tdMode shape too (no cloid).
    open_params = fake.calls[1]["params"]
    assert open_params.get("clOrdId") == "cid-okx-open"
    assert open_params.get("tdMode") == "cross"
    assert "cloid" not in open_params
    assert fake.calls[1]["price"] is None


# ---------------------------------------------------------------------------
# Operator close (close_only) regression: no target_direction must not fail.
# ---------------------------------------------------------------------------


class _FakeLongClient(_FakeClient):
    """OKX-style contract sizing but reports an OPEN LONG position, mirroring the
    reported bug (LONG BTC operator close)."""

    def fetch_positions(self, symbols=None):
        sym = symbols[0] if symbols else "BTC/USDT:USDT"
        return [
            {
                "symbol": sym,
                "side": "long",
                "contracts": 3,
                "entryPrice": 60500.0,
                "unrealizedPnl": 12.0,
                "info": {"instId": "BTC-USDT-SWAP", "posSide": "long", "mgnMode": "cross", "lever": "2"},
            }
        ]


def test_operator_close_long_without_target_direction(monkeypatch):
    """Regression: build_operator_close_readiness sets signal_side=None and no
    target_direction, relying on explicit CLOSE_LONG/CLOSE_SHORT actions. The adapter
    must NOT reject this as submit_failed/invalid_direction when close_only is set."""
    ex = _executor()
    fake = _FakeLongClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    # Shape matches build_operator_close_readiness output.
    readiness = {
        "close_only": True,
        "signal_side": None,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "policy": "operator_close:strat-1",
            "decision": "CLOSE",
            "actions": ["CLOSE_LONG", "CLOSE_SHORT"],
            "reduce_only": True,
            "client_order_id": "operator_close_BTC-USDT-SWAP_strat-1_20260702",
            "client_order_id_close": "operator_close_BTC-USDT-SWAP_strat-1_20260702",
        },
    }

    out = ex.execute(readiness)

    # The old bug returned this exact failure before any order was attempted.
    assert out["mode"] != "submit_failed"
    assert out["payload"].get("error") != "invalid_direction"
    assert out["ok"] is True
    # Only the CLOSE_LONG leg fires (reduceOnly sell); CLOSE_SHORT is skipped.
    assert len(fake.calls) == 1
    close_call = fake.calls[0]
    assert close_call["side"] == "sell"
    assert close_call["amount"] == 3
    assert close_call["params"].get("reduceOnly") is True
    # No synthesized direction leaked into the result payload for a close.
    assert out["payload"].get("target_direction") == ""


# ---------------------------------------------------------------------------
# Tier-2 adapter auth wiring (Binance, Bitget, Gate)
# ---------------------------------------------------------------------------


class _FakeAuthExchange:
    """Captures constructor kwargs so tests can assert credential injection."""

    def __init__(self, kwargs):
        self.captured_kwargs = dict(kwargs)

    def set_sandbox_mode(self, flag):
        self.captured_kwargs["_sandbox"] = flag


def _install_fake_ccxt(monkeypatch, **exchanges):
    fake = types.SimpleNamespace(**exchanges)
    monkeypatch.setattr(ccxt_adapter, "ccxt", fake)


def _auth_executor(exchange_id: str, simulated: bool = True) -> CcxtExecutor:
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": exchange_id,
            "simulated_trading": simulated,
        }
    }
    return CcxtExecutor(cfg, Path("."))


def test_ccxt_adapter_binance_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, binance=_FakeAuthExchange)
    # demo mode -> testnet creds preferred over plain.
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "binance-testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "binance-testnet-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "binance-live-key")
    monkeypatch.setenv("BINANCE_SECRET_KEY", "binance-live-secret")

    ex = _auth_executor("binance", simulated=True)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "binance-testnet-key"
    assert client.captured_kwargs["secret"] == "binance-testnet-secret"
    assert client.captured_kwargs["options"]["defaultType"] == "future"


def test_ccxt_adapter_bitget_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, bitget=_FakeAuthExchange)
    monkeypatch.setenv("BITGET_DEMO_API_KEY", "bitget-demo-key")
    monkeypatch.setenv("BITGET_DEMO_SECRET_KEY", "bitget-demo-secret")
    monkeypatch.setenv("BITGET_DEMO_PASSPHRASE", "bitget-demo-pass")

    ex = _auth_executor("bitget", simulated=True)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "bitget-demo-key"
    assert client.captured_kwargs["secret"] == "bitget-demo-secret"
    assert client.captured_kwargs["password"] == "bitget-demo-pass"


def test_ccxt_adapter_gate_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, gate=_FakeAuthExchange)
    # live mode -> plain creds preferred over testnet.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    monkeypatch.setenv("GATE_API_KEY", "gate-live-key")
    monkeypatch.setenv("GATE_SECRET_KEY", "gate-live-secret")
    monkeypatch.setenv("GATE_TESTNET_API_KEY", "gate-testnet-key")
    monkeypatch.setenv("GATE_TESTNET_SECRET_KEY", "gate-testnet-secret")

    ex = _auth_executor("gate", simulated=False)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "gate-live-key"
    assert client.captured_kwargs["secret"] == "gate-live-secret"
    # live mode never enables sandbox.
    assert "_sandbox" not in client.captured_kwargs


def test_symbol_mapping_usdc_suffix():
    # Hyperliquid quotes in USDC; the bare-suffix form must map like USDT does.
    assert _inst_id_to_ccxt_symbol("SOLUSDC") == "SOL/USDC"
    assert _inst_id_to_ccxt_symbol("BTCUSDT") == "BTC/USDT"


def test_hyperliquid_get_order_direct_cloid_fetch_before_scan(monkeypatch):
    # After a fill, fetch_closed_orders lags; get_order must hit fetch_order with the
    # hashed cloid FIRST and never fall back to scanning open/closed orders.
    ex = _hl_executor()
    hermx_id = "550e8400-e29b-41d4-a716-446655440000"
    expected_cloid = _to_hyperliquid_cloid(hermx_id)

    class _Client(_FakeHLClient):
        def __init__(self):
            super().__init__()
            self.fetch_order_calls = []
            self.scanned = False

        def fetch_order(self, order_id, symbol=None):
            self.fetch_order_calls.append((order_id, symbol))
            return {
                "id": "hl-ord-live-1",
                "symbol": symbol,
                "clientOrderId": expected_cloid,
                "status": "closed",
                "side": "buy",
                "type": "market",
                "average": 60000.0,
                "filled": 1,
                "amount": 1,
                "timestamp": 1719300003000,
                "info": {},
            }

        def fetch_open_orders(self, symbol=None):
            self.scanned = True
            return []

        def fetch_closed_orders(self, symbol=None, limit=100):
            self.scanned = True
            return []

    fake = _Client()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.get_order("SOL-USDC-SWAP", cl_ord_id=hermx_id)

    # fetch_order called once with the hashed cloid and the mapped symbol.
    assert fake.fetch_order_calls == [(expected_cloid, "SOL/USDC:USDC")]
    # Direct fetch succeeded -> no scan fallback.
    assert fake.scanned is False
    assert out["state"] == "filled"
    assert out["ord_id"] == "hl-ord-live-1"


def test_hyperliquid_get_order_scan_matches_hashed_cloid(monkeypatch):
    # If the live fetch_order misses, the scan fallback must compare against the
    # hashed cloid (what CCXT returns in clientOrderId), not the raw HermX id.
    ex = _hl_executor()
    hermx_id = "550e8400-e29b-41d4-a716-446655440000"
    expected_cloid = _to_hyperliquid_cloid(hermx_id)

    class _Client(_FakeHLClient):
        def fetch_order(self, order_id, symbol=None):
            raise Exception("order does not exist")

        def fetch_open_orders(self, symbol=None):
            return []

        def fetch_closed_orders(self, symbol=None, limit=100):
            return [
                {
                    "id": "hl-ord-closed-1",
                    "symbol": symbol,
                    # Hyperliquid reports the hashed cloid here, NOT the HermX UUID.
                    "clientOrderId": expected_cloid,
                    "status": "closed",
                    "side": "buy",
                    "type": "market",
                    "average": 60000.0,
                    "filled": 1,
                    "amount": 1,
                    "timestamp": 1719300003000,
                    "info": {},
                }
            ]

    fake = _Client()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.get_order("SOL-USDC-SWAP", cl_ord_id=hermx_id)

    # Raw-id comparison would have missed and returned not_found.
    assert out["state"] == "filled"
    assert out["ord_id"] == "hl-ord-closed-1"


def test_okx_get_order_scan_uses_raw_cl_ord_id(monkeypatch):
    # Non-hyperliquid path is unchanged: compare against the raw cl_ord_id, and never
    # hash it. _FakeClient.fetch_closed_orders returns clientOrderId="cid-1".
    ex = _executor()
    fake = _FakeClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.get_order("BTC-USDT-SWAP", cl_ord_id="cid-1")

    assert out["state"] == "filled"
    assert out["ord_id"] == "ord-h-1"


def test_hyperliquid_close_leg_gets_cloid(monkeypatch):
    ex = _hl_executor()
    fake = _FakeHLLongClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = {
        "signal_side": "sell",
        "signal_price": 60000.0,
        "inst_id": "SOL-USDC-SWAP",
        "execution_intent": {
            "client_order_id": "550e8400-e29b-41d4-a716-446655440000",
            "client_order_id_close": "550e8400-e29b-41d4-a716-446655440002",
            "target_direction": "short",
            "planned_notional_usd": 50.0,
            "actions": ["CLOSE_LONG"],
        },
    }

    ex.execute(readiness)

    assert len(fake.calls) == 1
    close_params = fake.calls[0]["params"]
    assert _CLOID_RE.match(close_params.get("clientOrderId") or "") is not None
    assert close_params.get("reduceOnly") is True
    assert "clOrdId" not in close_params
    assert "cloid" not in close_params
