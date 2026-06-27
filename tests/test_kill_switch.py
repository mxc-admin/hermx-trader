"""Kill-switch tests (REFACTOR_PLAN.md:168, :180 -- Phase 0 task 6).

Asserts ``HERMX_SUBMIT_ENABLED`` hard-blocks OKX order submission at the top of
``execute_okx_if_enabled`` regardless of config, and that its unset default is
inert (existing config-driven behavior preserved).
"""
from __future__ import annotations

from unittest import mock

import webhook_receiver as wr


def _armed_config() -> dict:
    """A config where every gate is affirmative, so submission WOULD proceed."""
    return {
        "execution": {
            "enabled": True,
            "submit_orders": True,
            "simulated_trading": True,
            "force_ipv4": True,
        },
        "risk": {"allow_live_execution": True},
    }


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


def test_kill_switch_false_blocks_submit(monkeypatch):
    """HERMX_SUBMIT_ENABLED=false => no executor submit at all."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "false")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_okx_if_enabled(_armed_record())

    create_mock.assert_not_called()
    assert result["mode"] == "not_submitted"
    assert "kill switch" in result["reason"].lower()


def test_kill_switch_falsey_variants_block(monkeypatch):
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    for value in ("false", "0", "no", ""):
        monkeypatch.setenv("HERMX_SUBMIT_ENABLED", value)
        with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
            result = wr.execute_okx_if_enabled(_armed_record())
        create_mock.assert_not_called()
        assert result["mode"] == "not_submitted", f"value={value!r} should block"


def test_kill_switch_unset_is_inert_armed_config_submits(monkeypatch):
    """Positive control: with the switch unset and config armed, the executor submit
    IS invoked -- proving it is the kill switch (not the config) that blocks above."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.delenv("HERMX_SUBMIT_ENABLED", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        wr.execute_okx_if_enabled(_armed_record())

    fake.execute.assert_called_once()


def test_submit_kill_switch_armed_helper(monkeypatch):
    monkeypatch.delenv("HERMX_SUBMIT_ENABLED", raising=False)
    assert wr.submit_kill_switch_armed()[0] is True  # unset => inert/armed

    for blocked in ("false", "0", "no", "", "  ", "False", "NO"):
        monkeypatch.setenv("HERMX_SUBMIT_ENABLED", blocked)
        assert wr.submit_kill_switch_armed()[0] is False, blocked

    for armed in ("true", "1", "yes"):
        monkeypatch.setenv("HERMX_SUBMIT_ENABLED", armed)
        assert wr.submit_kill_switch_armed()[0] is True, armed
