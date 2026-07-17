"""Backend venue-agnostic ledger sweep in unknown_resolver_loop.

Mirrors tests/test_drift_wiring.py patterns: drive the loop with a fast stop
event, monkeypatch cadence + enumerator + executor/reconcile seams on the
production call path (lazy imports through home modules).
"""
from __future__ import annotations

import pnl_ledger
import reconcile.unknown_resolver as ur


class _FastStop:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        return self._set


def _tick_summary():
    return {"checked": 0, "resolved": 0, "pending": 0, "expired": 0, "errors": []}


def _run_ledger_ticks(
    wr,
    monkeypatch,
    *,
    n_ticks,
    every_n,
    instruments,
    history_by_env,
    reconcile_calls,
    reconcile_raises=frozenset(),
    history_raises=frozenset(),
    ageout_calls=None,
):
    """Drive unknown_resolver_loop for n_ticks with the ledger sweep faked.

    instruments: dict[(venue, simulated)] -> set[inst_id]
    history_by_env: dict[(venue, mode_str)] -> list[rows]
    reconcile_calls: list appended with (venue, mode, n_rows)
    reconcile_raises / history_raises: venues that raise
    """
    ticks = []
    stop = _FastStop()

    def fake_resolve():
        ticks.append(1)
        if len(ticks) >= n_ticks:
            stop.set()
        return _tick_summary()

    class _StubExecutor:
        def __init__(self, venue, mode):
            self.venue = venue
            self.mode = mode

        def get_order_history_raw(self, inst_ids, limit=100):
            if self.venue in history_raises:
                raise RuntimeError(f"venue {self.venue} history exploded")
            return list(history_by_env.get((self.venue, self.mode), []))

    def fake_executor(intent=None):
        intent = intent or {}
        venue = str(intent.get("venue") or "okx").strip().lower()
        simulated = bool(intent.get("simulated_trading", True))
        mode = "demo" if simulated else "live"
        return _StubExecutor(venue, mode)

    def fake_reconcile(rows, exchange_id, mode):
        if exchange_id in reconcile_raises:
            raise RuntimeError(f"venue {exchange_id} reconcile exploded")
        reconcile_calls.append((exchange_id, mode, len(rows or [])))
        return len(rows or [])

    def fake_ageout(rows, venue, mode):
        if ageout_calls is not None:
            ageout_calls.append((venue, mode, len(rows or [])))

    monkeypatch.setattr(wr, "resolve_unknown_orders_once", fake_resolve)
    monkeypatch.setattr(wr, "_reconciliation_executor", fake_executor)
    monkeypatch.setattr(ur, "HERMX_LEDGER_SWEEP_EVERY_N_TICKS", every_n)
    # Keep B1 drift from interfering with tick counts / side effects.
    monkeypatch.setattr(ur, "HERMX_DRIFT_CHECK_EVERY_N_TICKS", 0)
    monkeypatch.setattr(ur, "active_venue_mode_instruments", lambda: dict(instruments))
    monkeypatch.setattr(pnl_ledger, "reconcile_from_order_history", fake_reconcile)
    monkeypatch.setattr(pnl_ledger, "detect_history_ageout", fake_ageout)
    wr.unknown_resolver_loop(stop_event=stop)
    return len(ticks)


def test_ledger_sweep_fires_every_n_ticks(wr, monkeypatch):
    calls = []
    ticks = _run_ledger_ticks(
        wr, monkeypatch, n_ticks=7, every_n=3,
        instruments={("okx", True): {"BTC-USDT-SWAP"}},
        history_by_env={("okx", "demo"): [{"ordId": "1"}]},
        reconcile_calls=calls,
    )
    assert ticks == 7
    # sweeps at ticks 3 and 6 only
    assert calls == [("okx", "demo", 1), ("okx", "demo", 1)]


def test_ledger_sweep_includes_demo_pairs(wr, monkeypatch):
    # Deliberate divergence from B1: demo P&L is a product surface.
    calls = []
    ticks = _run_ledger_ticks(
        wr, monkeypatch, n_ticks=2, every_n=1,
        instruments={("okx", True): {"BTC-USDT-SWAP"}, ("bybit", True): {"ETH-USDT-SWAP"}},
        history_by_env={
            ("okx", "demo"): [{"ordId": "a"}],
            ("bybit", "demo"): [{"ordId": "b"}],
        },
        reconcile_calls=calls,
    )
    assert ticks == 2
    assert sorted(calls) == [
        ("bybit", "demo", 1), ("bybit", "demo", 1),
        ("okx", "demo", 1), ("okx", "demo", 1),
    ]


def test_ledger_sweep_exception_blocks_neither_pairs_nor_resolver(wr, monkeypatch):
    calls = []
    ticks = _run_ledger_ticks(
        wr, monkeypatch, n_ticks=3, every_n=1,
        instruments={
            ("bybit", False): {"BTC-USDT-SWAP"},
            ("okx", False): {"BTC-USDT-SWAP"},
        },
        history_by_env={
            ("bybit", "live"): [{"ordId": "b"}],
            ("okx", "live"): [{"ordId": "o"}],
        },
        reconcile_calls=calls,
        history_raises={"bybit"},
    )
    assert ticks == 3
    assert calls == [("okx", "live", 1)] * 3


def test_zero_n_disables_ledger_sweep(wr, monkeypatch):
    calls = []
    ticks = _run_ledger_ticks(
        wr, monkeypatch, n_ticks=4, every_n=0,
        instruments={("okx", True): {"BTC-USDT-SWAP"}},
        history_by_env={("okx", "demo"): [{"ordId": "1"}]},
        reconcile_calls=calls,
    )
    assert ticks == 4
    assert calls == []


def test_sweep_threads_actual_venue_mode_to_reconcile(wr, monkeypatch):
    calls = []
    _run_ledger_ticks(
        wr, monkeypatch, n_ticks=1, every_n=1,
        instruments={
            ("okx", True): {"BTC-USDT-SWAP"},
            ("bybit", False): {"ETH-USDT-SWAP"},
        },
        history_by_env={
            ("okx", "demo"): [{"ordId": "1"}, {"ordId": "2"}],
            ("bybit", "live"): [{"ordId": "3"}],
        },
        reconcile_calls=calls,
    )
    assert sorted(calls) == [
        ("bybit", "live", 1),
        ("okx", "demo", 2),
    ]


def test_sweep_calls_ageout_detector(wr, monkeypatch):
    calls = []
    ageout = []
    _run_ledger_ticks(
        wr, monkeypatch, n_ticks=1, every_n=1,
        instruments={("okx", True): {"BTC-USDT-SWAP"}},
        history_by_env={("okx", "demo"): [{"ordId": "1"}]},
        reconcile_calls=calls,
        ageout_calls=ageout,
    )
    assert ageout == [("okx", "demo", 1)]
    assert calls == [("okx", "demo", 1)]


def test_sweep_roundtrip_attributes_pnl(wr, monkeypatch, ledger_dir):
    """submit-map → history rows → real reconcile_from_order_history → aggregate.

    Does not hand-inject strategy_id on ledger rows (C1 attribution rule).
    """
    import pnl_strategy_map

    sid = "sweep_alpha"
    cl_open = "mxcSweepOpen"
    cl_close = "mxcSweepClose"
    pnl_strategy_map.record_submit_strategy(cl_open, sid, venue="okx", mode="demo")
    pnl_strategy_map.record_submit_strategy(cl_close, sid, venue="okx", mode="demo")

    history = [
        {
            "instId": "BTC-USDT-SWAP", "ordId": "o1", "clOrdId": cl_open,
            "side": "buy", "accFillSz": 1.0, "reduceOnly": False,
            "state": "filled", "avgPx": "50000", "uTime": 100,
        },
        {
            "instId": "BTC-USDT-SWAP", "ordId": "c1", "clOrdId": cl_close,
            "side": "sell", "accFillSz": 1.0, "reduceOnly": True,
            "state": "filled", "avgPx": "51000", "pnl": "10.0",
            "fee": "-0.5", "feeCcy": "USDT", "uTime": 200,
        },
    ]

    ticks = []
    stop = _FastStop()

    def fake_resolve():
        ticks.append(1)
        stop.set()
        return _tick_summary()

    class _Exec:
        def get_order_history_raw(self, inst_ids, limit=100):
            return list(history)

    monkeypatch.setattr(wr, "resolve_unknown_orders_once", fake_resolve)
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda intent=None: _Exec())
    monkeypatch.setattr(ur, "HERMX_LEDGER_SWEEP_EVERY_N_TICKS", 1)
    monkeypatch.setattr(ur, "HERMX_DRIFT_CHECK_EVERY_N_TICKS", 0)
    monkeypatch.setattr(
        ur, "active_venue_mode_instruments",
        lambda: {("okx", True): {"BTC-USDT-SWAP"}},
    )
    # Use real reconcile_from_order_history / detect_history_ageout
    wr.unknown_resolver_loop(stop_event=stop)

    agg = pnl_ledger.aggregate_strategy_pnl(sid, mode="demo")
    assert agg["closed_order_count"] == 1
    assert (agg.get("closed_net_pnl_usd") or agg.get("closed_realized_pnl_usd") or 0) != 0


def test_detect_history_ageout_emits_on_gap(ledger_dir, monkeypatch):
    alerts = []

    def fake_emit(kind, detail):
        alerts.append((kind, detail))
        return {}

    monkeypatch.setattr("reconcile.alerts.emit_reconcile_alert", fake_emit)
    # high-water at 1000; saturated window oldest at 5000 -> gap
    pnl_ledger.append_closed_trades([
        {
            "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "old",
            "mode": "demo", "closed_at_ms": 1000, "side": "sell",
            "qty": 1.0, "leg_kind": "close",
        },
    ])
    rows = [
        {"uTime": 5000 + i, "instId": "BTC-USDT-SWAP", "ordId": str(i)}
        for i in range(100)
    ]
    pnl_ledger.detect_history_ageout(rows, "okx", "demo")
    assert len(alerts) == 1
    kind, detail = alerts[0]
    assert kind == "RECONCILE_MISMATCH"
    assert detail["stage"] == "history_window_ageout"
    assert detail["venue"] == "okx"
    assert detail["mode"] == "demo"
    assert detail["high_water_ms"] == 1000
    assert detail["oldest_ms"] == 5000


def test_detect_history_ageout_no_emit_when_not_saturated(ledger_dir, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        "reconcile.alerts.emit_reconcile_alert",
        lambda kind, detail: alerts.append((kind, detail)),
    )
    pnl_ledger.append_closed_trades([
        {
            "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "old",
            "mode": "demo", "closed_at_ms": 1000, "side": "sell",
            "qty": 1.0, "leg_kind": "close",
        },
    ])
    rows = [{"uTime": 9000, "instId": "BTC-USDT-SWAP", "ordId": "1"}]
    pnl_ledger.detect_history_ageout(rows, "okx", "demo")
    assert alerts == []


def test_active_venue_mode_instruments_groups_by_env(wr, monkeypatch):
    from reconcile.executor_select import active_venue_mode_instruments

    wr.STRATEGIES = {
        "a": {
            "strategy_id": "a",
            "execution_mode": "demo",
            "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP"},
        },
        "b": {
            "strategy_id": "b",
            "execution_mode": "demo",
            "instrument": {"exchange": "okx", "inst_id": "ETH-USDT-SWAP"},
        },
        "c": {
            "strategy_id": "c",
            "execution_mode": "live",
            "instrument": {"exchange": "bybit", "inst_id": "SOL-USDT-SWAP"},
        },
        "skip": {
            "strategy_id": "skip",
            "execution_mode": "demo",
            "instrument": {},
        },
    }
    out = active_venue_mode_instruments()
    assert out[("okx", True)] == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert out[("bybit", False)] == {"SOL-USDT-SWAP"}
    assert ("", True) not in out
