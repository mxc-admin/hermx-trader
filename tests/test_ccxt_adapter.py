from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

import executors.ccxt_adapter as ccxt_adapter
from executors.ccxt_adapter import (
    CcxtExecutor,
    _inst_id_to_ccxt_symbol,
    _order_fully_filled,
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
# Fix 2: market-spec lookup failure must fail closed (never size on defaults).
# ---------------------------------------------------------------------------


class _FakeMarketFailClient(_FakeClient):
    """load_markets / market() raise (transient venue/network failure)."""

    def load_markets(self):
        raise Exception("network timeout during load_markets")

    def market(self, _symbol):
        raise Exception("market lookup failed")


def test_market_spec_lookup_failure_blocks_order_not_default_size(monkeypatch):
    """Regression: a bare ``except: market = {}`` defaulted contract_size to 1.0 on a
    transient lookup failure, silently mis-sizing orders on contracts where
    contractSize != 1. The lookup failure must instead surface as UNKNOWN and never
    reach create_order with a fabricated size."""
    ex = _executor()
    fake = _FakeMarketFailClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-spec-fail",
            "target_direction": "long",
            "planned_notional_usd": 1500.0,
            "actions": ["OPEN_LONG"],
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is False
    assert out["mode"] in {"submit_exception", "submit_timeout"}
    # Never sized/submitted on a fabricated contract_size=1.0.
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Fix 4: a real short reported with a blank ccxt side must stay closable.
# ---------------------------------------------------------------------------


class _FakeBlankSideShortClient(_FakeClient):
    """A real SHORT position ccxt reports with a blank top-level side; only the
    venue-native ``info.posSide`` reveals direction (the reported bug)."""

    def fetch_positions(self, symbols=None):
        sym = symbols[0] if symbols else "BTC/USDT:USDT"
        return [
            {
                "symbol": sym,
                "side": "",  # blank -> old code defaulted to "long"
                "contracts": 3,
                "entryPrice": 60500.0,
                "info": {"instId": "BTC-USDT-SWAP", "posSide": "short"},
            }
        ]


class _FakeUnknownSideClient(_FakeClient):
    """A position with a blank side AND no native disambiguator -> genuinely unknown."""

    def fetch_positions(self, symbols=None):
        sym = symbols[0] if symbols else "BTC/USDT:USDT"
        return [{"symbol": sym, "side": "", "contracts": 3, "entryPrice": 60500.0, "info": {}}]


def test_close_short_with_blank_side_still_flattens(monkeypatch):
    ex = _executor()
    fake = _FakeBlankSideShortClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = {
        "close_only": True,
        "signal_side": None,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "actions": ["CLOSE_LONG", "CLOSE_SHORT"],
            "reduce_only": True,
            "client_order_id": "operator_close_BTC-USDT-SWAP_strat-1_20260704",
            "client_order_id_close": "operator_close_BTC-USDT-SWAP_strat-1_20260704",
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is True
    # CLOSE_LONG skipped (position is short); CLOSE_SHORT fires as a buy reduceOnly.
    assert len(fake.calls) == 1
    assert fake.calls[0]["side"] == "buy"
    assert fake.calls[0]["params"].get("reduceOnly") is True
    reasons = [r.get("reason") for r in out["payload"]["executed_orders"]]
    assert "no_short_position_to_close" not in reasons


def test_close_short_with_unknown_side_trusts_action(monkeypatch):
    ex = _executor()
    fake = _FakeUnknownSideClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = {
        "close_only": True,
        "signal_side": None,
        "inst_id": "BTC-USDT-SWAP",
        "execution_intent": {
            "actions": ["CLOSE_SHORT"],
            "reduce_only": True,
            "client_order_id": "operator_close_BTC-USDT-SWAP_strat-1_20260704",
            "client_order_id_close": "operator_close_BTC-USDT-SWAP_strat-1_20260704",
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["side"] == "buy"


# ---------------------------------------------------------------------------
# Fix 5: Hyperliquid reduce-only close must not submit price=None when feed down.
# ---------------------------------------------------------------------------


def test_hl_reduce_only_close_falls_back_to_position_price(monkeypatch):
    ex = _hl_executor()
    fake = _FakeHLLongClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)
    # Ticker feed down: reference price resolves to None.
    monkeypatch.setattr(ex, "_reference_price", lambda *a, **k: None)

    readiness = {
        "close_only": True,
        "signal_side": None,
        "inst_id": "SOL-USDC-SWAP",
        "execution_intent": {
            "actions": ["CLOSE_LONG", "CLOSE_SHORT"],
            "reduce_only": True,
            "client_order_id": "550e8400-e29b-41d4-a716-446655440010",
            "client_order_id_close": "550e8400-e29b-41d4-a716-446655440011",
        },
    }

    out = ex.execute(readiness)

    assert out["ok"] is True
    # CLOSE_LONG fires (position is long); price falls back to the position entryPrice.
    assert len(fake.calls) == 1
    assert fake.calls[0]["price"] == 59000.0


def test_hl_entry_with_none_price_still_blocks(monkeypatch):
    """The reduce-only fallback must NOT leak into the entry path: a new OPEN with no
    resolvable price still refuses to submit (unchanged)."""
    ex = _hl_executor()
    fake = _FakeHLClient()  # flat -> OPEN_LONG path
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)
    monkeypatch.setattr(ex, "_reference_price", lambda *a, **k: None)

    out = ex.execute(_hl_open_readiness())

    assert out["ok"] is False
    assert fake.calls == []


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


def test_ccxt_adapter_bybit_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, bybit=_FakeAuthExchange)
    # demo mode -> testnet creds preferred over plain.
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "bybit-testnet-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET_KEY", "bybit-testnet-secret")
    monkeypatch.setenv("BYBIT_API_KEY", "bybit-live-key")
    monkeypatch.setenv("BYBIT_SECRET_KEY", "bybit-live-secret")

    ex = _auth_executor("bybit", simulated=True)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "bybit-testnet-key"
    assert client.captured_kwargs["secret"] == "bybit-testnet-secret"
    # Bybit has no passphrase.
    assert "password" not in client.captured_kwargs


def test_ccxt_adapter_coinbase_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, coinbase=_FakeAuthExchange)
    # demo mode -> sandbox creds preferred over plain.
    monkeypatch.setenv("COINBASE_SANDBOX_API_KEY", "coinbase-sandbox-key")
    monkeypatch.setenv("COINBASE_SANDBOX_SECRET_KEY", "coinbase-sandbox-secret")
    monkeypatch.setenv("COINBASE_API_KEY", "coinbase-live-key")
    monkeypatch.setenv("COINBASE_SECRET_KEY", "coinbase-live-secret")

    ex = _auth_executor("coinbase", simulated=True)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "coinbase-sandbox-key"
    assert client.captured_kwargs["secret"] == "coinbase-sandbox-secret"


def test_ccxt_adapter_coinbase_demo_fails_closed_without_sandbox(monkeypatch):
    # ccxt's coinbase adapter has no sandbox URL; a demo request must fail closed.
    class _NoSandboxExchange:
        def __init__(self, kwargs):
            self.captured_kwargs = dict(kwargs)

    _install_fake_ccxt(monkeypatch, coinbase=_NoSandboxExchange)
    monkeypatch.setenv("COINBASE_SANDBOX_API_KEY", "coinbase-sandbox-key")
    monkeypatch.setenv("COINBASE_SANDBOX_SECRET_KEY", "coinbase-sandbox-secret")

    ex = _auth_executor("coinbase", simulated=True)
    with pytest.raises(RuntimeError, match="no sandbox support"):
        ex._client()


def test_ccxt_adapter_bitfinex_auth_kwargs(monkeypatch):
    _install_fake_ccxt(monkeypatch, bitfinex=_FakeAuthExchange)
    # live mode -> plain creds preferred over paper.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    monkeypatch.setenv("BITFINEX_API_KEY", "bitfinex-live-key")
    monkeypatch.setenv("BITFINEX_SECRET_KEY", "bitfinex-live-secret")
    monkeypatch.setenv("BITFINEX_PAPER_API_KEY", "bitfinex-paper-key")
    monkeypatch.setenv("BITFINEX_PAPER_SECRET_KEY", "bitfinex-paper-secret")

    ex = _auth_executor("bitfinex", simulated=False)
    client = ex._client()

    assert client.captured_kwargs["apiKey"] == "bitfinex-live-key"
    assert client.captured_kwargs["secret"] == "bitfinex-live-secret"
    assert client.captured_kwargs["options"]["defaultType"] == "swap"
    # Bitfinex has no passphrase; live mode never enables sandbox.
    assert "password" not in client.captured_kwargs
    assert "_sandbox" not in client.captured_kwargs


def test_ccxt_adapter_bitfinex_demo_fails_closed_without_sandbox(monkeypatch):
    # ccxt's bitfinex class inherits set_sandbox_mode but urls["test"] is None, so
    # enabling it raises; a demo request must fail closed.
    class _NoSandboxExchange:
        def __init__(self, kwargs):
            self.captured_kwargs = dict(kwargs)

        def set_sandbox_mode(self, flag):
            raise TypeError("'NoneType' object is not iterable")

    _install_fake_ccxt(monkeypatch, bitfinex=_NoSandboxExchange)
    monkeypatch.setenv("BITFINEX_PAPER_API_KEY", "bitfinex-paper-key")
    monkeypatch.setenv("BITFINEX_PAPER_SECRET_KEY", "bitfinex-paper-secret")

    ex = _auth_executor("bitfinex", simulated=True)
    with pytest.raises(RuntimeError, match="failed to enable sandbox"):
        ex._client()


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


# ---------------------------------------------------------------------------
# Hyperliquid fill-at-submit (reconciliation on Hyperliquid can't confirm fills
# in time, so a fully-filled create_order response is recorded FILLED directly)
# ---------------------------------------------------------------------------


def test_order_fully_filled_compares_against_requested_size():
    # Fill completeness is judged against the size WE submitted, so a partial fill is
    # never terminalized as complete even if the response self-reports no remainder.
    assert _order_fully_filled({"filled": 0.10}, 0.19) is False  # partial
    assert _order_fully_filled({"filled": 0.19}, 0.19) is True   # full
    assert _order_fully_filled({"filled": 0.0}, 0.19) is False   # nothing filled
    assert _order_fully_filled({"filled": 0.19}, None) is False  # no requested size -> ACK


def _record_fill(client, symbol, order_type, side, amount, price, params, *, filled, status=None, info=None):
    """Record the create_order call and return a fill response with the given filled
    size. Shared by the fill-scenario fakes so the recorded shape lives in one place."""
    client.calls.append({"symbol": symbol, "type": order_type, "side": side,
                         "amount": amount, "price": price, "params": dict(params or {})})
    resp = {
        "id": f"ord-{len(client.calls)}",
        "symbol": symbol,
        "clientOrderId": params.get("clientOrderId"),
        "status": status,
        "average": 60000.0,
        "filled": filled,
        "amount": amount,
    }
    if info is not None:
        resp["info"] = info
    return resp


class _FakeHLFillClient(_FakeHLClient):
    """create_order echoes a caller-chosen filled size so a test can drive full vs partial."""

    def __init__(self, filled: float):
        super().__init__()
        self._filled = filled

    def create_order(self, symbol, order_type, side, amount, price, params):
        return _record_fill(self, symbol, order_type, side, amount, price, params, filled=self._filled)


class _FakeOKXFullFillClient(_FakeClient):
    def create_order(self, symbol, order_type, side, amount, price, params):
        return _record_fill(self, symbol, order_type, side, amount, price, params,
                            filled=amount, status="closed", info={"clOrdId": params.get("clOrdId")})


def test_hyperliquid_full_fill_recorded_as_filled(monkeypatch):
    ex = _hl_executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeHLFillClient(filled=0.0008333))  # $50/60000
    out = ex.execute(_hl_open_readiness())
    assert out["ok"] is True
    assert out["fill_summary"]["status"] == "filled"


def test_hyperliquid_partial_fill_stays_submitted(monkeypatch):
    # Venue fills less than the requested ~0.0008; the order must stay an ACK for
    # reconciliation, not be terminalized as filled.
    ex = _hl_executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeHLFillClient(filled=0.0004))
    out = ex.execute(_hl_open_readiness())
    assert out["ok"] is True
    assert out["fill_summary"]["status"] == "submitted"


def test_okx_full_fill_stays_submitted_not_filled(monkeypatch):
    # The fill-at-submit shortcut is Hyperliquid-only: OKX must keep returning
    # "submitted" on a complete fill so its submit->reconcile path is byte-identical.
    ex = _executor()
    monkeypatch.setattr(ex, "_client", lambda **_kw: _FakeOKXFullFillClient())
    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-okx-fill",
            "target_direction": "long",
            "planned_notional_usd": 1500.0,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"],
        },
    }
    out = ex.execute(readiness)
    assert out["ok"] is True
    assert out["fill_summary"]["status"] == "submitted"


# ---------------------------------------------------------------------------
# Item A: live-mode pre-trade balance check (open leg only, fail-open)
# ---------------------------------------------------------------------------


def _live_executor() -> CcxtExecutor:
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "simulated_trading": False,
            "td_mode": "cross",
        }
    }
    return CcxtExecutor(cfg, Path("."))


class _FakeFlatClient(_FakeClient):
    """Flat position so OPEN_LONG is the only expanded action."""

    def __init__(self, free=None, balance_error=None):
        super().__init__()
        self._free = {} if free is None else dict(free)
        self._balance_error = balance_error
        self.balance_calls = 0

    def fetch_positions(self, symbols=None):
        return []

    def fetch_balance(self):
        self.balance_calls += 1
        if self._balance_error is not None:
            raise self._balance_error
        if self._free is None:
            return None
        return {"total": dict(self._free), "free": dict(self._free)}


class _FakeFlatNoneBalanceClient(_FakeFlatClient):
    def fetch_balance(self):
        self.balance_calls += 1
        return None


class _FakeBtcSettleFlatClient(_FakeFlatClient):
    """Inverse-swap-style market settling in BTC, not USDT."""

    def market(self, _symbol):
        spec = dict(super().market(_symbol))
        spec["settle"] = "BTC"
        return spec


def _open_long_readiness(planned_notional=1500.0, leverage=None) -> dict:
    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-bal",
            "target_direction": "long",
            "planned_notional_usd": planned_notional,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"],
        },
    }
    if leverage is not None:
        readiness["leverage"] = leverage
    return readiness


def test_live_open_skipped_on_insufficient_free_balance(monkeypatch):
    ex = _live_executor()
    # leverage 2 -> required margin 1500/2 = 750 USDT; only 100 free.
    fake = _FakeFlatClient(free={"USDT": 100.0})
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert fake.calls == []  # create_order never reached
    legs = out["payload"]["executed_orders"]
    assert legs == [
        {
            "action": "OPEN_LONG",
            "submitted": False,
            "status": "skipped",
            "reason": "insufficient_balance",
        }
    ]
    assert out["ok"] is False
    assert out["mode"] == "submit_failed"


def test_live_open_submits_when_leveraged_margin_covered(monkeypatch):
    ex = _live_executor()
    # 800 free >= 1500/2 = 750 required: leverage must divide the notional.
    fake = _FakeFlatClient(free={"USDT": 800.0})
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["side"] == "buy"


def test_live_close_never_gated_by_zero_balance(monkeypatch):
    # Never-block-a-close invariant: the reduce-only close leg must submit even
    # with zero free balance in live mode.
    ex = _live_executor()
    fake = _FakeClient()  # reports a short position
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)
    monkeypatch.setattr(fake, "fetch_balance", lambda: {"total": {"USDT": 0.0}, "free": {"USDT": 0.0}})

    readiness = {
        "close_only": True,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-close-bal",
            "actions": ["CLOSE_SHORT"],
        },
    }
    out = ex.execute(readiness)

    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["params"].get("reduceOnly") is True


def test_live_open_fail_open_on_balance_fetch_failure(monkeypatch):
    # fetch raising -> submit proceeds
    ex = _live_executor()
    fake = _FakeFlatClient(balance_error=RuntimeError("venue down"))
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)
    out = ex.execute(_open_long_readiness(leverage=2))
    assert out["ok"] is True
    assert len(fake.calls) == 1

    # fetch returning None (no balance data) -> submit proceeds
    ex2 = _live_executor()
    fake2 = _FakeFlatNoneBalanceClient()
    monkeypatch.setattr(ex2, "_client", lambda **_kw: fake2)
    out2 = ex2.execute(_open_long_readiness(leverage=2))
    assert out2["ok"] is True
    assert len(fake2.calls) == 1


def test_demo_mode_skips_balance_check_entirely(monkeypatch):
    # simulated_trading=True: zero free balance AND the check must not even
    # fetch the balance (demo sandbox balances are arbitrary).
    ex = _executor()  # simulated_trading=True
    fake = _FakeFlatClient(free={"USDT": 0.0})
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.balance_calls == 0


def test_balance_check_uses_market_settle_currency_not_hardcoded_usdt(monkeypatch):
    # Market settles in BTC: a huge USDT balance must NOT satisfy the check --
    # the settle currency's free balance (tiny) is the one consulted.
    ex = _live_executor()
    fake = _FakeBtcSettleFlatClient(free={"USDT": 1_000_000_000.0, "BTC": 0.0001})
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert fake.calls == []
    legs = out["payload"]["executed_orders"]
    assert legs[0]["status"] == "skipped"
    assert legs[0]["reason"] == "insufficient_balance"


# ---------------------------------------------------------------------------
# Leverage sync: set_leverage before OPEN (all derivatives venues via the
# _leverage_params() per-venue table, demo+live, fail-open)
# ---------------------------------------------------------------------------


class _FakeLeverageClient(_FakeFlatClient):
    """Flat position + setLeverage capability; records leverage calls and ordering."""

    has = {"setLeverage": True}

    def __init__(self, free=None, leverage_error=None):
        super().__init__(free=free)
        self.leverage_calls = []
        self.call_order = []
        self._leverage_error = leverage_error

    def set_leverage(self, leverage, symbol, params):
        self.call_order.append("set_leverage")
        self.leverage_calls.append((leverage, symbol, dict(params or {})))
        if self._leverage_error is not None:
            raise self._leverage_error

    def create_order(self, symbol, order_type, side, amount, price, params):
        self.call_order.append("create_order")
        return super().create_order(symbol, order_type, side, amount, price, params)


class _FakeLeverageShortClient(_FakeClient):
    """Short position + setLeverage capability, for the close-only path."""

    has = {"setLeverage": True}

    def __init__(self):
        super().__init__()
        self.leverage_calls = []

    def set_leverage(self, leverage, symbol, params):
        self.leverage_calls.append((leverage, symbol, dict(params or {})))


class _FakeNoLeverageCapabilityClient(_FakeFlatClient):
    """set_leverage exists but `has` lacks setLeverage -> must never be called."""

    def __init__(self, free=None):
        super().__init__(free=free)
        self.leverage_calls = []

    def set_leverage(self, leverage, symbol, params):
        self.leverage_calls.append((leverage, symbol, dict(params or {})))


def test_live_open_sets_leverage_before_create_order(monkeypatch):
    ex = _live_executor()
    fake = _FakeLeverageClient(free={"USDT": 800.0})
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert fake.leverage_calls == [(2, "BTC/USDT:USDT", {"mgnMode": "cross"})]
    assert isinstance(fake.leverage_calls[0][0], int)
    assert fake.call_order == ["set_leverage", "create_order"]


def test_demo_open_sets_leverage_too(monkeypatch):
    # Regression test for the reported bug: OKX demo leverage/margin state is
    # real, so the sync must NOT be gated behind simulated_trading the way the
    # balance check is.
    ex = _executor()  # simulated_trading=True
    fake = _FakeLeverageClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert fake.leverage_calls == [(2, "BTC/USDT:USDT", {"mgnMode": "cross"})]
    assert fake.call_order == ["set_leverage", "create_order"]


def test_set_leverage_failure_is_fail_open(monkeypatch):
    ex = _live_executor()
    fake = _FakeLeverageClient(free={"USDT": 800.0}, leverage_error=RuntimeError("lever busy"))
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert len(fake.calls) == 1  # order submitted anyway
    legs = out["payload"]["executed_orders"]
    assert [leg["status"] for leg in legs] == ["submitted"]


def test_close_only_never_sets_leverage(monkeypatch):
    ex = _live_executor()
    fake = _FakeLeverageShortClient()  # reports a short position
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = {
        "close_only": True,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "leverage": 2,
        "execution_intent": {
            "client_order_id": "cid-close-lev",
            "actions": ["CLOSE_SHORT"],
        },
    }
    out = ex.execute(readiness)

    assert out["ok"] is True
    assert fake.leverage_calls == []
    assert len(fake.calls) == 1
    assert fake.calls[0]["params"].get("reduceOnly") is True


def _venue_executor(exchange_id, td_mode="cross") -> CcxtExecutor:
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": exchange_id,
            "simulated_trading": True,
            "td_mode": td_mode,
        }
    }
    return CcxtExecutor(cfg, Path("."))


def test_bybit_open_sets_leverage_with_empty_params(monkeypatch):
    # Non-OKX venues now sync too. Bybit takes NO extra params: ccxt sets
    # buyLeverage=sellLeverage internally, and a stray mgnMode would leak
    # into the raw request.
    ex = _venue_executor("bybit")
    fake = _FakeLeverageClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert fake.leverage_calls == [(2, "BTC/USDT:USDT", {})]
    assert fake.call_order == ["set_leverage", "create_order"]


def test_hyperliquid_open_sets_leverage_with_explicit_margin_mode(monkeypatch):
    # marginMode MUST be explicit for hyperliquid: ccxt defaults its
    # set_leverage to cross, the opposite of HermX's isolated default.
    ex = _venue_executor("hyperliquid", td_mode="isolated")
    fake = _FakeLeverageClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = _open_long_readiness(leverage=3)
    readiness["td_mode"] = "isolated"
    out = ex.execute(readiness)

    assert out["ok"] is True
    assert fake.leverage_calls == [(3, "BTC/USDT:USDT", {"marginMode": "isolated"})]
    assert fake.call_order == ["set_leverage", "create_order"]


def test_bitget_isolated_open_sets_leverage_with_hold_side(monkeypatch):
    # Bitget isolated needs per-side leverage: holdSide derives from the
    # OPEN leg's target direction. Cross takes no extra params.
    ex = _venue_executor("bitget", td_mode="isolated")
    fake = _FakeLeverageClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    readiness = _open_long_readiness(leverage=2)
    readiness["td_mode"] = "isolated"
    out = ex.execute(readiness)

    assert out["ok"] is True
    assert fake.leverage_calls == [(2, "BTC/USDT:USDT", {"holdSide": "long"})]


def test_leverage_params_table():
    # Direct unit coverage of the per-venue params table.
    lp = CcxtExecutor._leverage_params
    assert lp("okx", "cross", "long") == {"mgnMode": "cross"}
    assert lp("okx", "isolated", "short") == {"mgnMode": "isolated"}
    assert lp("bybit", "isolated", "long") == {}
    assert lp("binance", "cross", "long") == {}
    assert lp("bitget", "cross", "long") == {}
    assert lp("bitget", "isolated", "short") == {"holdSide": "short"}
    assert lp("gate", "isolated", "long") == {"marginMode": "isolated"}
    assert lp("gateio", "cross", "long") == {"marginMode": "cross"}
    assert lp("kucoin", "isolated", "long") == {}
    assert lp("hyperliquid", "isolated", "long") == {"marginMode": "isolated"}
    assert lp("hyperliquid", "cross", "long") == {"marginMode": "cross"}
    # Unknown venue -> safe fallback: ccxt's own default takes over.
    assert lp("someothervenue", "isolated", "long") == {}


def test_set_leverage_skipped_without_capability(monkeypatch):
    # Client has no `has["setLeverage"]` (nor `.has` at all) -> the sync is a
    # silent no-op and the order still submits.
    ex = _executor()
    fake = _FakeNoLeverageCapabilityClient()
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(leverage=2))

    assert out["ok"] is True
    assert fake.leverage_calls == []
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Item A 1.4 -- sub-minimum order disambiguation (below_instrument_min)
# ---------------------------------------------------------------------------

class _FakeMinLimitClient(_FakeFlatClient):
    """Flat position; market with configurable amount/cost minimums."""

    def __init__(self, min_amount=1, min_cost=None, precision_amount=1):
        super().__init__(free={"USDT": 1_000_000.0})
        self._limits = {"amount": {"min": min_amount}}
        if min_cost is not None:
            self._limits["cost"] = {"min": min_cost}
        self._precision_amount = precision_amount

    def market(self, _symbol):
        spec = dict(super().market(_symbol))
        spec["precision"] = {"amount": self._precision_amount}
        spec["limits"] = self._limits
        return spec


def test_below_instrument_min_reason_distinct_from_zero_size(monkeypatch):
    # contractSize 0.01 @ price 60000 -> 1 contract = $600 notional. A $300
    # notional sizes to 0.5 contracts (step 0.1) -- nonzero, but below the
    # venue's limits.amount.min of 1 -> the skip must say below_instrument_min,
    # NOT the generic zero_size.
    ex = _executor()  # simulated -> balance check skipped
    fake = _FakeMinLimitClient(min_amount=1)
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(planned_notional=300.0))

    assert fake.calls == []
    legs = out["payload"]["executed_orders"]
    assert legs == [
        {
            "action": "OPEN_LONG",
            "submitted": False,
            "status": "skipped",
            "reason": "below_instrument_min",
        }
    ]
    assert out["ok"] is False
    assert out["mode"] == "submit_failed"


def test_min_cost_limit_floors_to_zero_with_reason(monkeypatch):
    # $800 notional sizes to 1.3 contracts -- above limits.amount.min (0.1) --
    # but the notional itself is below limits.cost.min ($1000), so the venue
    # would reject it. Skip with below_instrument_min, not zero_size.
    ex = _executor()
    fake = _FakeMinLimitClient(min_amount=0.1, min_cost=1000.0)
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(planned_notional=800.0))

    assert fake.calls == []
    legs = out["payload"]["executed_orders"]
    assert legs == [
        {
            "action": "OPEN_LONG",
            "submitted": False,
            "status": "skipped",
            "reason": "below_instrument_min",
        }
    ]
    assert out["mode"] == "submit_failed"


def test_zero_notional_and_no_price_keep_plain_zero_size(monkeypatch):
    # Regression pin: a genuinely zero notional still skips with the plain
    # zero_size reason -- 1.4 refines only the sub-minimum sub-case.
    ex = _executor()
    fake = _FakeMinLimitClient(min_amount=1)
    monkeypatch.setattr(ex, "_client", lambda **_kw: fake)

    out = ex.execute(_open_long_readiness(planned_notional=0.0))

    assert fake.calls == []
    legs = out["payload"]["executed_orders"]
    assert legs == [
        {
            "action": "OPEN_LONG",
            "submitted": False,
            "status": "skipped",
            "reason": "zero_size",
        }
    ]
    assert out["mode"] == "submit_failed"

    # No reference price -> also plain zero_size (helper-level pin).
    spec = ex._market_spec(fake, "BTC/USDT:USDT")
    amount, reason = ex._amount_from_readiness(
        {"execution_intent": {"planned_notional_usd": 100.0}}, spec, None
    )
    assert amount == 0.0
    assert reason == "zero_size"


# ---------------------------------------------------------------------------
# #1 -- read_only bypass of the live-trading kill-switch gate in _client().
#
# When HERMX_LIVE_TRADING is disarmed but a live-mode executor is built
# (simulated_trading=False), pure-read operations must still connect (they only
# READ; they never submit), while any submit path stays hard-blocked. The gate
# lives in _client(): read call sites pass read_only=True, the submit path
# (execute -> _client(close_only=...)) does NOT. These exercise the REAL gate
# (ccxt is stubbed, not _client), so a regression that drops read_only or leaks
# the bypass into the submit path fails here.
# ---------------------------------------------------------------------------


class _GateFakeClient:
    """Offline stand-in for a live ccxt client -- no network, just enough shape
    for health()/get_positions() to complete once the gate lets them through."""

    def __init__(self, kwargs=None):
        self.kwargs = kwargs

    def fetch_balance(self):
        return {"total": {"USDT": 100.0}, "free": {"USDT": 100.0}, "info": {"posMode": "net"}}

    def fetch_positions(self, symbols=None):
        return []


def _live_executor() -> CcxtExecutor:
    """A live-mode (simulated_trading=False) executor -- the only mode whose
    _client() reaches the HERMX_LIVE_TRADING gate."""
    cfg = {
        "execution": {
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "simulated_trading": False,
            "td_mode": "cross",
        }
    }
    return CcxtExecutor(cfg, Path("."))


def _stub_live_ccxt(monkeypatch):
    monkeypatch.setattr(
        ccxt_adapter, "ccxt", types.SimpleNamespace(okx=_GateFakeClient)
    )


def test_read_only_bypasses_gate_when_disarmed(monkeypatch):
    """Kill switch OFF + live mode: a read (health) still connects via read_only=True."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    _stub_live_ccxt(monkeypatch)
    ex = _live_executor()

    # Direct gate assertion: read_only=True does NOT raise.
    client = ex._client(read_only=True)
    assert isinstance(client, _GateFakeClient)

    # End-to-end read: health() succeeds (ok=True), not the gate-error shape.
    snap = _live_executor().health()
    assert snap["ok"] is True
    assert "live_trading_disabled" not in str(snap.get("error", ""))


def test_submit_path_still_blocked_when_disarmed(monkeypatch):
    """Kill switch OFF + live mode: the submit path (_client without read_only)
    stays hard-blocked, and execute() surfaces it as submit_exception."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    _stub_live_ccxt(monkeypatch)

    # Direct gate assertion: the submit-path call still raises.
    with pytest.raises(RuntimeError, match="live_trading_disabled"):
        _live_executor()._client(close_only=False)

    # End-to-end: execute() reaches _client(close_only=False), gate raises, and
    # the RuntimeError is mapped to submit_exception (UNKNOWN) -- never a silent pass.
    readiness = {
        "signal_side": "buy",
        "signal_price": 60000.0,
        "inst_id": "BTC-USDT-SWAP",
        "td_mode": "cross",
        "execution_intent": {
            "client_order_id": "cid-gate",
            "target_direction": "long",
            "planned_notional_usd": 1500.0,
            "actions": ["OPEN_LONG"],
        },
    }
    out = _live_executor().execute(readiness)
    assert out["ok"] is False
    assert out["mode"] == "submit_exception"
    assert "live_trading_disabled" in out["payload"]["error"]


def test_armed_allows_both_read_and_submit(monkeypatch):
    """Kill switch ON: normal path unchanged -- both read and submit connect."""
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    _stub_live_ccxt(monkeypatch)

    # Read connects.
    assert isinstance(_live_executor()._client(read_only=True), _GateFakeClient)
    # Submit-path connects too (no gate raise when armed).
    assert isinstance(_live_executor()._client(close_only=False), _GateFakeClient)
    # And a read completes end-to-end.
    assert _live_executor().health()["ok"] is True
