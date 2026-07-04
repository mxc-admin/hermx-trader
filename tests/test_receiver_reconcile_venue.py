"""Issue #20a — the receiver's order-state reconciler must query each order's OWN
(venue, mode), not the global OKX-demo default.

Before the fix, ``_effective_execution_config()`` seeded ``ccxt_exchange="okx"`` and
never set ``simulated_trading``, so a Bybit-live order was reconciled against OKX-demo
-> not-found -> stuck ORDER_STATE_UNKNOWN forever. The fix threads the resolved
``(venue, simulated_trading)`` persisted on the order-journal intent through
``_effective_execution_config(order_intent)`` /  ``_reconciliation_executor(order_intent)``
and builds a per-order executor in the reconcile loops.

Offline: exercises the config/executor resolution and the intent-persistence path via
the ``wr`` fixture; never touches a real venue.
"""
from __future__ import annotations

from unittest import mock

from webhook.config import EXECUTION_DEFAULTS


# ---------------------------------------------------------------------------
# _effective_execution_config — the core resolution (B.4)
# ---------------------------------------------------------------------------

def test_reconcile_uses_order_venue(wr):
    # A KuCoin order reconciles against KuCoin, not OKX.
    cfg = wr._effective_execution_config({"venue": "kucoin"})["execution"]
    assert cfg["ccxt_exchange"] == "kucoin"


def test_reconcile_uses_order_mode(wr):
    # A live order (simulated_trading False) reconciles against the live account.
    cfg = wr._effective_execution_config(
        {"venue": "bybit", "simulated_trading": False}
    )["execution"]
    assert cfg["ccxt_exchange"] == "bybit"
    assert cfg["simulated_trading"] is False


def test_reconcile_okx_demo_default(wr):
    # No intent / no venue+mode on the order -> OKX default venue and NO forced
    # simulated flag (adapter then defaults to the demo sandbox — the safe fallback).
    cfg_none = wr._effective_execution_config()["execution"]
    assert cfg_none["ccxt_exchange"] == EXECUTION_DEFAULTS["ccxt_exchange"] == "okx"
    assert "simulated_trading" not in cfg_none

    cfg_empty = wr._effective_execution_config({})["execution"]
    assert cfg_empty["ccxt_exchange"] == "okx"
    assert "simulated_trading" not in cfg_empty


def test_reconcile_demo_order_sets_simulated_true(wr):
    cfg = wr._effective_execution_config(
        {"venue": "okx", "simulated_trading": True}
    )["execution"]
    assert cfg["ccxt_exchange"] == "okx"
    assert cfg["simulated_trading"] is True


def test_venue_case_normalized(wr):
    cfg = wr._effective_execution_config({"venue": "KuCoin"})["execution"]
    assert cfg["ccxt_exchange"] == "kucoin"


# ---------------------------------------------------------------------------
# Intent persistence — venue/mode land on the order-journal record (B.2)
# ---------------------------------------------------------------------------

def _readiness_record(wr, *, execution_mode, exchange):
    return wr.build_strategy_execution_readiness({
        "strategy_id": "s1",
        "strategy_config": {
            "strategy_id": "s1",
            "name": "T",
            "asset": "BTCUSDT",
            "instrument": {"exchange": exchange, "inst_id": "BTC-USDT-SWAP", "type": "swap"},
            "timeframe": "2h",
            "execution_mode": execution_mode,
            "submit_orders": True,
            "budget_usd": 1000,
            "leverage": 1,
            "margin_mode": "isolated",
        },
        "normalized": {
            "strategy_id": "s1", "symbol": "BTCUSDT", "side": "buy",
            "timeframe": "2h", "tv_time": f"2026-06-25T00:00:00Z|{exchange}|{execution_mode}",
            "tv_signal_price": 50000.0,
        },
    })


def test_order_intent_persists_venue_and_mode(wr):
    rd = _readiness_record(wr, execution_mode="live", exchange="bybit")
    intent = wr._order_intent_from_readiness(rd)
    assert intent["venue"] == "bybit"
    assert intent["mode"] == "live"
    assert intent["simulated_trading"] is False


def test_order_intent_demo_persists_simulated_true(wr):
    rd = _readiness_record(wr, execution_mode="demo", exchange="okx")
    intent = wr._order_intent_from_readiness(rd)
    assert intent["venue"] == "okx"
    assert intent["mode"] == "demo"
    assert intent["simulated_trading"] is True


# ---------------------------------------------------------------------------
# _executor_for_order — per-order resolution + env caching
# ---------------------------------------------------------------------------

def test_executor_for_order_legacy_intent_uses_default(wr):
    # A pre-#20a intent (no venue/mode) falls back to the caller's default executor.
    sentinel = object()
    cache: dict = {}
    got = wr._executor_for_order({"symbol": "BTCUSDT"}, cache, sentinel)
    assert got is sentinel
    assert cache == {}  # never built a per-order executor


def test_executor_for_order_builds_per_env_and_caches(wr, monkeypatch):
    built = []

    def fake_build(intent=None):
        built.append(intent)
        return f"exec:{(intent or {}).get('venue')}:{(intent or {}).get('simulated_trading')}"

    monkeypatch.setattr(wr, "_reconciliation_executor", fake_build)
    cache: dict = {}
    a = wr._executor_for_order({"venue": "bybit", "simulated_trading": False}, cache, None)
    b = wr._executor_for_order({"venue": "bybit", "simulated_trading": False}, cache, None)
    assert a == "exec:bybit:False"
    assert a is b  # cached: one build for the shared (venue, mode)
    assert len(built) == 1
    # A different environment builds its own executor.
    wr._executor_for_order({"venue": "okx", "simulated_trading": True}, cache, None)
    assert len(built) == 2


# ---------------------------------------------------------------------------
# reconcile_startup — production path builds a per-order executor (#20a)
# ---------------------------------------------------------------------------

def test_reconcile_startup_uses_persisted_venue(wr, monkeypatch):
    # Journal a SUBMITTED Bybit-live order, then run startup reconcile with executor
    # unset (production path). The executor must be built from the order's own intent.
    cl = "mxc-bybit-live-0001"
    intent = {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP",
              "venue": "bybit", "mode": "live", "simulated_trading": False}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    seen = []

    class _StubExec:
        def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
            return None  # not present -> stays non-terminal; we only assert the venue

        def get_open_orders(self, inst_id):
            return []

        def get_order_history_archive(self, inst_id, limit=100):
            return []

    def fake_build(order_intent=None):
        seen.append(order_intent)
        return _StubExec()

    monkeypatch.setattr(wr, "_reconciliation_executor", fake_build)
    summary = wr.reconcile_startup()  # executor=None -> production per-order path

    assert summary["executor_available"] is True
    # The per-order build saw the Bybit-live intent (not an OKX-demo default).
    venue_builds = [i for i in seen if isinstance(i, dict) and i.get("venue") == "bybit"]
    assert venue_builds, f"expected a bybit build, saw {seen}"
    assert venue_builds[0]["simulated_trading"] is False


def test_reconcile_startup_explicit_executor_unchanged(wr):
    # An explicitly-passed executor is used for every order (backward compatible).
    cl = "mxc-okx-demo-0002"
    intent = {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP",
              "venue": "bybit", "mode": "live", "simulated_trading": False}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    calls = []

    class _StubExec:
        def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
            calls.append(inst_id)
            return None

        def get_open_orders(self, inst_id):
            return []

        def get_order_history_archive(self, inst_id, limit=100):
            return []

    stub = _StubExec()
    with mock.patch.object(wr, "_reconciliation_executor",
                           side_effect=AssertionError("must not build when executor passed")):
        summary = wr.reconcile_startup(executor=stub)

    assert summary["executor_available"] is True
    assert calls == ["BTC-USDT-SWAP"]  # the passed stub handled the order
