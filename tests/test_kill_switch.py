"""Kill-switch / execution-mode tests for the Phase A refactor.

The old 6-flag arming chain is gone. Two operative controls remain:
  * per-strategy ``execution_mode`` ("demo" | "live"), surfaced via readiness, and
  * the single global ``HERMX_LIVE_TRADING`` kill switch (``live_trading_enabled``).

In Phase A every order routes to the demo sandbox, so a demo-mode strategy submits
WITHOUT ``HERMX_LIVE_TRADING`` armed -- the switch is scaffolded for Phase B, which
will gate ``execution_mode == "live"`` strategies on it. The per-strategy submit flag
(surfaced as ``readiness.live_execution_enabled``) is what arms paper submission.
"""
from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _armed_config() -> dict:
    """No arming flags: in Phase A the per-strategy submit flag arms paper submission."""
    return {"execution": {"exchange": "ccxt"}}


def _armed_record() -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "okx_fill": {},
            "block_reason": None,
        },
    }


def _fake_executor():
    """Stand-in CCXT submit executor: its .execute is the single submit call."""
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value={
        "ok": True, "mode": "submit_enabled", "exchange": "ccxt", "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {},
    })
    return fake


def test_demo_mode_submits_with_kill_switch_off(monkeypatch):
    """Demo mode does NOT need the kill switch armed: with HERMX_LIVE_TRADING=false
    a demo strategy still submits to the sandbox (the switch gates only live mode)."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        wr.execute_if_enabled(_armed_record())

    fake.execute.assert_called_once()


def test_demo_mode_submits_with_kill_switch_unset(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        wr.execute_if_enabled(_armed_record())

    fake.execute.assert_called_once()


def test_per_strategy_submit_flag_blocks(monkeypatch):
    """The per-strategy submit flag is the paper arm: live_execution_enabled=False
    (strategy.submit_orders=false) => no executor is ever built and nothing submits."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    rec = _armed_record()
    rec["execution_readiness"]["live_execution_enabled"] = False

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_if_enabled(rec)

    create_mock.assert_not_called()
    assert result["mode"] == "not_submitted"


def test_live_trading_enabled_helper(monkeypatch):
    """HERMX_LIVE_TRADING is a fail-closed positive enable flag (Phase B gate input)."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    assert wr.live_trading_enabled()[0] is False  # unset => live disabled (fail closed)

    for disabled in ("false", "0", "no", "", "  ", "False", "NO"):
        monkeypatch.setenv("HERMX_LIVE_TRADING", disabled)
        assert wr.live_trading_enabled()[0] is False, disabled

    for enabled in ("true", "1", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("HERMX_LIVE_TRADING", enabled)
        assert wr.live_trading_enabled()[0] is True, enabled
