"""C1 -- HERMX_LIVE_TRADING kill switch wired into the live-mode submission gate.

These assert the wired behavior plus the pure ``live_trading_enabled`` helper and
the per-strategy submit-flag posture (the latter two merged from the retired
``test_kill_switch.py``):

  * An ``execution_mode == "live"`` strategy submits ONLY when the global
    ``HERMX_LIVE_TRADING`` switch is explicitly armed AND the readiness agrees the
    order is NOT simulated (``simulated_trading is False``). Either inconsistency
    fails closed with ``not_submitted`` and the executor is NEVER built.
  * A demo-mode strategy IGNORES ``HERMX_LIVE_TRADING`` entirely -- it submits to the
    sandbox regardless of the switch (the switch gates only live mode).

All offline: the executor is mocked via ``ExecutorFactory.create``, so the only way
the submit call is reached is by passing the gate; no exchange/network is touched.
The ``wr`` fixture gives each test an isolated temp order journal so submitting tests
do not collide on ``client_order_id`` dedupe.
"""
from __future__ import annotations

from unittest import mock

from conftest import fake_executor as _fake_executor


def _record(*, execution_mode: str, simulated_trading=None, cl: str = "cid-live") -> dict:
    readiness = {
        "live_execution_enabled": True,
        "execution_mode": execution_mode,
        "symbol": "XRPUSDT",
        "signal_side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
        "okx_fill": {"client_order_id": cl},
        "block_reason": None,
    }
    if simulated_trading is not None:
        readiness["simulated_trading"] = simulated_trading
    return {"received_at": "2026-06-25T00:00:00Z", "execution_readiness": readiness}


# ---------------------------------------------------------------------------
# Live mode REQUIRES the switch.
# ---------------------------------------------------------------------------

def test_live_mode_blocked_when_switch_unset(wr, monkeypatch):
    """execution_mode=live + HERMX_LIVE_TRADING unset => not_submitted, never built."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_live_mode_blocked_when_switch_false(wr, monkeypatch):
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_live_mode_blocked_when_simulated_trading_inconsistent(wr, monkeypatch):
    """Switch armed but readiness still says simulated => fail closed (no live/sim mix)."""
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    for sim in (True, None):  # True, and absent (defaults to simulated)
        fake = _fake_executor()
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
            out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=sim))
        create_mock.assert_not_called()
        fake.execute.assert_not_called()
        assert out["mode"] == "not_submitted", sim
        assert out["reason"] == "live_mode_simulated_inconsistent", sim


def test_live_mode_submits_when_switch_armed_and_not_simulated(wr, monkeypatch):
    """The only live-submit path: switch armed AND simulated_trading is False."""
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=False))

    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"


# ---------------------------------------------------------------------------
# Demo mode IGNORES the switch.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ANY non-sandbox submit (not just execution_mode==live) is gated on the switch and
# is sandbox-only unless it is genuinely live. Closes the hole where a non-live mode
# that resolves to a REAL venue could submit without the global kill switch.
# ---------------------------------------------------------------------------

def test_non_sandbox_non_live_blocked_even_with_switch_armed(wr, monkeypatch):
    # A demo strategy that (mis)resolves to simulated_trading=False would hit a REAL
    # venue. Even with the kill switch armed it must be refused -- demo stays sandbox-only.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_if_enabled(_record(execution_mode="demo", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "non_sandbox_requires_live_mode"


def test_non_sandbox_blocked_when_switch_unset(wr, monkeypatch):
    # Real-venue routing without the kill switch is blocked outright, regardless of mode.
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_unknown_execution_mode_rejected(wr, monkeypatch):
    # A typo'd / unknown execution_mode must fail closed, never reach the executor.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_if_enabled(_record(execution_mode="production", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "unknown_execution_mode"


def test_blocked_results_name_the_gate(wr, monkeypatch):
    # Every blocked result surfaces the FIRST blocking gate explicitly (operator clarity).
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    with mock.patch.object(wr.ExecutorFactory, "create", return_value=_fake_executor()):
        out = wr.execute_if_enabled(_record(execution_mode="live", simulated_trading=False))
    assert out["reason"] == "live_trading_disabled"
    assert out["gate"] == "live_trading_kill_switch"


def test_demo_mode_ignores_switch_unset(wr, monkeypatch):
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(_record(execution_mode="demo", simulated_trading=True))

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_demo_mode_ignores_switch_false(wr, monkeypatch):
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_if_enabled(_record(execution_mode="demo", simulated_trading=True))

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


# ---------------------------------------------------------------------------
# Per-strategy submit flag + the live_trading_enabled helper (merged from the
# retired test_kill_switch.py -- the two cases not already covered above).
# ---------------------------------------------------------------------------

def test_per_strategy_submit_flag_blocks(wr):
    """The per-strategy submit flag is the paper arm: live_execution_enabled=False
    (strategy.submit_orders=false) => no executor is ever built and nothing submits."""
    rec = _record(execution_mode="demo", simulated_trading=True)
    rec["execution_readiness"]["live_execution_enabled"] = False

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert result["mode"] == "not_submitted"


def test_live_trading_enabled_helper(wr, monkeypatch):
    """HERMX_LIVE_TRADING is a fail-closed positive enable flag (Phase B gate input)."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    assert wr.live_trading_enabled()[0] is False  # unset => live disabled (fail closed)

    for disabled in ("false", "0", "no", "", "  ", "False", "NO"):
        monkeypatch.setenv("HERMX_LIVE_TRADING", disabled)
        assert wr.live_trading_enabled()[0] is False, disabled

    for enabled in ("true", "1", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("HERMX_LIVE_TRADING", enabled)
        assert wr.live_trading_enabled()[0] is True, enabled
