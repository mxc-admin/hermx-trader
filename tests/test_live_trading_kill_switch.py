"""C1 -- HERMX_LIVE_TRADING kill switch wired into the live-mode submission gate.

These assert the wired behavior (vs ``test_kill_switch.py`` which covers the pure
``live_trading_enabled`` helper and the Phase A demo-mode posture):

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


def _armed_config() -> dict:
    return {"execution": {"exchange": "ccxt"}}


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


def _adapter_ok() -> dict:
    return {
        "ok": True,
        "mode": "submit_enabled",
        "exchange": "ccxt",
        "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {},
    }


def _fake_executor():
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_adapter_ok())
    return fake


# ---------------------------------------------------------------------------
# Live mode REQUIRES the switch.
# ---------------------------------------------------------------------------

def test_live_mode_blocked_when_switch_unset(wr, monkeypatch):
    """execution_mode=live + HERMX_LIVE_TRADING unset => not_submitted, never built."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_okx_if_enabled(_record(execution_mode="live", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_live_mode_blocked_when_switch_false(wr, monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_okx_if_enabled(_record(execution_mode="live", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_live_mode_blocked_when_simulated_trading_inconsistent(wr, monkeypatch):
    """Switch armed but readiness still says simulated => fail closed (no live/sim mix)."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    for sim in (True, None):  # True, and absent (defaults to simulated)
        fake = _fake_executor()
        with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
            out = wr.execute_okx_if_enabled(_record(execution_mode="live", simulated_trading=sim))
        create_mock.assert_not_called()
        fake.execute.assert_not_called()
        assert out["mode"] == "not_submitted", sim
        assert out["reason"] == "live_mode_simulated_inconsistent", sim


def test_live_mode_submits_when_switch_armed_and_not_simulated(wr, monkeypatch):
    """The only live-submit path: switch armed AND simulated_trading is False."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_okx_if_enabled(_record(execution_mode="live", simulated_trading=False))

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
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_okx_if_enabled(_record(execution_mode="demo", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "non_sandbox_requires_live_mode"


def test_non_sandbox_blocked_when_switch_unset(wr, monkeypatch):
    # Real-venue routing without the kill switch is blocked outright, regardless of mode.
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_okx_if_enabled(_record(execution_mode="paper", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "live_trading_disabled"


def test_unknown_execution_mode_rejected(wr, monkeypatch):
    # A typo'd / unknown execution_mode must fail closed, never reach the executor.
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_okx_if_enabled(_record(execution_mode="production", simulated_trading=False))

    create_mock.assert_not_called()
    fake.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "unknown_execution_mode"


def test_blocked_results_name_the_gate(wr, monkeypatch):
    # Every blocked result surfaces the FIRST blocking gate explicitly (operator clarity).
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    with mock.patch.object(wr.ExecutorFactory, "create", return_value=_fake_executor()):
        out = wr.execute_okx_if_enabled(_record(execution_mode="live", simulated_trading=False))
    assert out["reason"] == "live_trading_disabled"
    assert out["gate"] == "live_trading_kill_switch"


def test_demo_mode_ignores_switch_unset(wr, monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_okx_if_enabled(_record(execution_mode="demo", simulated_trading=True))

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


def test_demo_mode_ignores_switch_false(wr, monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        out = wr.execute_okx_if_enabled(_record(execution_mode="demo", simulated_trading=True))

    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"
