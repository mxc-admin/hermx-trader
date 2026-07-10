"""B1 step 4.4 wiring tests (NAUTILUS_GAP_REMEDIATION_PLAN.md §0.6 item 4.4):
check_balance_drift wired into unknown_resolver_loop every Nth tick.

Covers:
  - throttle: the drift sweep fires exactly once per N ticks per (venue, mode),
  - demo pairs are skipped by the wiring (check_balance_drift is a pinned
    live-only no-op -- test_phase_b_robustness.py -- so building an
    authenticated executor / equity estimate for demo would be pure waste),
  - fail-open: one pair's drift-check exception neither blocks other pairs in
    the same sweep nor the resolver's real order-reconciliation ticks,
  - _account_equity_estimate -> None (no strategies for the pair) is skipped,
  - N <= 0 disables the sweep entirely.

The loop under test is reconcile.unknown_resolver.unknown_resolver_loop; the
sweep globals (HERMX_DRIFT_CHECK_EVERY_N_TICKS, active_venue_mode_currencies)
live on that module and are monkeypatched there. check_balance_drift and
_account_equity_estimate are dereferenced through their home modules at call
time (the module's documented lazy-import pattern), so they are patched on
executors.ccxt_adapter / pnl_ledger respectively -- the production call path,
never a re-implemented copy.
"""
from __future__ import annotations

import pnl_ledger
import reconcile.unknown_resolver as ur
from executors import ccxt_adapter


class _FastStop:
    """Event stand-in whose wait() never sleeps, so multi-tick loop tests run
    at full speed instead of one UNKNOWN_RESOLVER_INTERVAL_SECONDS per tick."""

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


def _run_ticks(wr, monkeypatch, *, n_ticks, every_n, pairs, equity, drift_calls,
               drift_raises=frozenset(), equity_fn=None, currency_calls=None):
    """Drive unknown_resolver_loop for exactly n_ticks with the drift seam faked.

    pairs: (venue, simulated) 2-tuples (implicit "USDT" settle currency) OR
    (venue, simulated, currency) 3-tuples -- fed to the currency-aware enumerator
    active_venue_mode_currencies (#9 Half 1).
    equity: dict[(venue, mode)] -> float | None (missing key -> None);
    equity_fn overrides it when a test needs to observe the estimate calls.
    drift_calls: list appended with (venue, mode, equity) per production
    check_balance_drift invocation. currency_calls: when provided, list appended
    with (venue, mode, currency) so a test can assert per-currency iteration.
    drift_raises: venues whose check raises.
    Returns the tick count observed by the (faked) resolver pass.
    """
    ticks = []
    stop = _FastStop()

    def _as_triple(p):
        return p if len(p) == 3 else (p[0], p[1], "USDT")

    def fake_resolve():
        ticks.append(1)
        if len(ticks) >= n_ticks:
            stop.set()
        return _tick_summary()

    class _StubExecutor:
        pass

    def fake_check_balance_drift(executor, hermx_equity_usd, venue, mode, currency="USDT"):
        if venue in drift_raises:
            raise RuntimeError(f"venue {venue} balance read exploded")
        assert isinstance(executor, _StubExecutor)
        drift_calls.append((venue, mode, hermx_equity_usd))
        if currency_calls is not None:
            currency_calls.append((venue, mode, currency))
        return None

    monkeypatch.setattr(wr, "resolve_unknown_orders_once", fake_resolve)
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda intent=None: _StubExecutor())
    monkeypatch.setattr(ur, "HERMX_DRIFT_CHECK_EVERY_N_TICKS", every_n)
    monkeypatch.setattr(ur, "active_venue_mode_currencies", lambda: {_as_triple(p) for p in pairs})
    monkeypatch.setattr(pnl_ledger, "_account_equity_estimate",
                        equity_fn or (lambda venue, mode: equity.get((venue, mode))))
    monkeypatch.setattr(ccxt_adapter, "check_balance_drift", fake_check_balance_drift)
    wr.unknown_resolver_loop(stop_event=stop)
    return len(ticks)


def test_drift_check_fires_once_per_n_ticks_per_pair(wr, monkeypatch):
    # every_n=3 over 7 ticks -> sweeps at ticks 3 and 6 only: exactly 2 calls
    # per live (venue, mode) pair, never one per tick.
    calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=7, every_n=3,
        pairs={("okx", False), ("bybit", False)},
        equity={("okx", "live"): 1000.0, ("bybit", "live"): 500.0},
        drift_calls=calls,
    )
    assert ticks == 7
    assert sorted(calls) == [
        ("bybit", "live", 500.0), ("bybit", "live", 500.0),
        ("okx", "live", 1000.0), ("okx", "live", 1000.0),
    ]


def test_drift_check_not_called_every_tick(wr, monkeypatch):
    # every_n=10 (the default) over 9 ticks -> the sweep never fires.
    calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=9, every_n=10,
        pairs={("okx", False)}, equity={("okx", "live"): 1000.0},
        drift_calls=calls,
    )
    assert ticks == 9
    assert calls == []


def test_demo_pairs_skipped_by_wiring(wr, monkeypatch):
    # check_balance_drift is a pinned live-only no-op (test_phase_b_robustness),
    # so the wiring skips simulated pairs BEFORE computing equity or building an
    # authenticated executor -- same semantics, zero wasted venue clients.
    calls = []
    equity_asked = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=2, every_n=1,
        pairs={("okx", True), ("bybit", True)},  # demo-only domain
        equity={}, drift_calls=calls,
        equity_fn=lambda venue, mode: equity_asked.append((venue, mode)) or 1000.0,
    )
    assert ticks == 2
    assert calls == []
    assert equity_asked == []


def test_mixed_domain_checks_only_live_pair(wr, monkeypatch):
    calls = []
    _run_ticks(
        wr, monkeypatch, n_ticks=1, every_n=1,
        pairs={("okx", True), ("okx", False)},
        equity={("okx", "live"): 750.0, ("okx", "demo"): 999.0},
        drift_calls=calls,
    )
    assert calls == [("okx", "live", 750.0)]


def test_drift_exception_blocks_neither_other_pairs_nor_resolver(wr, monkeypatch):
    # bybit sorts first in the sweep and raises; okx must still be checked in
    # the SAME sweep, and the resolver's real tick work must keep running on
    # subsequent ticks (fail-open, never blocks order reconciliation).
    calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=3, every_n=1,
        pairs={("okx", False), ("bybit", False)},
        equity={("okx", "live"): 1000.0, ("bybit", "live"): 500.0},
        drift_calls=calls, drift_raises={"bybit"},
    )
    assert ticks == 3  # resolver ticks unaffected by 3 consecutive sweep failures
    assert calls == [("okx", "live", 1000.0)] * 3


def test_none_equity_estimate_skipped_without_error(wr, monkeypatch):
    # No loaded strategy matches (okx, live) -> estimate is None -> pair skipped;
    # the pair WITH an estimate is still checked and the loop stays healthy.
    calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=2, every_n=1,
        pairs={("okx", False), ("bybit", False)},
        equity={("bybit", "live"): 500.0},  # okx-live absent -> None
        drift_calls=calls,
    )
    assert ticks == 2
    assert calls == [("bybit", "live", 500.0)] * 2


def test_zero_or_negative_n_disables_sweep(wr, monkeypatch):
    calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=4, every_n=0,
        pairs={("okx", False)}, equity={("okx", "live"): 1000.0},
        drift_calls=calls,
    )
    assert ticks == 4
    assert calls == []


def test_drift_check_iterates_per_currency_same_venue(wr, monkeypatch):
    # #9 Half 1: a USDT strategy and a USDC strategy on the SAME live venue are
    # two distinct (venue, mode, currency) tuples -> two drift checks, each
    # reading its own currency, NOT one hardcoded-USDT check that misses USDC.
    calls = []
    ccy_calls = []
    ticks = _run_ticks(
        wr, monkeypatch, n_ticks=1, every_n=1,
        pairs={("okx", False, "USDT"), ("okx", False, "USDC")},
        equity={("okx", "live"): 1000.0},
        drift_calls=calls, currency_calls=ccy_calls,
    )
    assert ticks == 1
    assert sorted(ccy_calls) == [
        ("okx", "live", "USDC"),
        ("okx", "live", "USDT"),
    ]


def test_default_cadence_is_ten_ticks():
    # 10 ticks at the 30 s resolver interval ~= 5 min between sweeps (plan §0.6).
    assert ur.HERMX_DRIFT_CHECK_EVERY_N_TICKS == 10


def test_env_int_parse_matches_config_pattern(monkeypatch):
    # Same fail-open parse posture as webhook.config._env_float: blank/garbage
    # falls back to the default instead of raising at import time.
    monkeypatch.setenv("HERMX_DRIFT_CHECK_EVERY_N_TICKS", "7")
    assert ur._env_int("HERMX_DRIFT_CHECK_EVERY_N_TICKS", 10) == 7
    monkeypatch.setenv("HERMX_DRIFT_CHECK_EVERY_N_TICKS", "")
    assert ur._env_int("HERMX_DRIFT_CHECK_EVERY_N_TICKS", 10) == 10
    monkeypatch.setenv("HERMX_DRIFT_CHECK_EVERY_N_TICKS", "not-a-number")
    assert ur._env_int("HERMX_DRIFT_CHECK_EVERY_N_TICKS", 10) == 10
    monkeypatch.delenv("HERMX_DRIFT_CHECK_EVERY_N_TICKS", raising=False)
    assert ur._env_int("HERMX_DRIFT_CHECK_EVERY_N_TICKS", 10) == 10
