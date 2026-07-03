"""close_only flatten reaches the venue even with the kill switch off (M4 regression).

execution/service.py lets a close_only (operator flatten) bypass the live kill switch
because a close only REDUCES exposure. But CcxtExecutor._client() re-checked
live_trading_enabled() with no close_only exception, so the emergency close never got a
client and never reached the venue. These tests drive the real _client(): with the kill
switch OFF it raises for a normal open but returns a client for a close_only call.
"""
from __future__ import annotations

import pytest

from executors import ccxt_adapter
from executors.ccxt_adapter import CcxtExecutor


def _live_executor(tmp_path):
    # simulated_trading=False forces the real-venue branch where the live gate lives.
    return CcxtExecutor({"execution": {"exchange": "ccxt", "simulated_trading": False}}, tmp_path)


def test_client_blocked_when_live_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(ccxt_adapter, "live_trading_enabled", lambda: (False, "disabled"))
    ex = _live_executor(tmp_path)
    with pytest.raises(RuntimeError):
        ex._client()


def test_close_only_bypasses_live_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(ccxt_adapter, "live_trading_enabled", lambda: (False, "disabled"))
    ex = _live_executor(tmp_path)
    # Must NOT raise: an emergency flatten has to reach the venue while the switch is off.
    client = ex._client(close_only=True)
    assert client is not None


def test_close_only_not_needed_when_live_enabled(tmp_path, monkeypatch):
    # Sanity: with the switch armed a normal open also gets a client.
    monkeypatch.setattr(ccxt_adapter, "live_trading_enabled", lambda: (True, "enabled"))
    ex = _live_executor(tmp_path)
    assert ex._client() is not None
