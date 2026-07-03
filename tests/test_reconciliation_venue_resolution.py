"""Reconciliation resolves a real venue, not the "ccxt" backend name (H2 regression).

_reconciliation_executor() builds a CcxtExecutor from _effective_execution_config(),
which carries the adapter selector exchange="ccxt" (the BACKEND name, not a venue) and
no strategy instrument block. If _exchange_id() treats "ccxt" as a venue, every
reconcile query resolves getattr(ccxt, "ccxt") -> None and silently fails. These tests
pin that "ccxt"/empty collapse to the okx default (or the HERMX_CCXT_EXCHANGE override)
and that the effective config seeds a concrete venue.
"""
from __future__ import annotations

from executors.ccxt_adapter import CcxtExecutor


def _executor(exchange, tmp_path):
    return CcxtExecutor({"execution": {"exchange": exchange}}, tmp_path)


def test_backend_name_ccxt_resolves_to_okx(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMX_CCXT_EXCHANGE", raising=False)
    assert _executor("ccxt", tmp_path)._exchange_id() == "okx"


def test_empty_exchange_resolves_to_okx(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMX_CCXT_EXCHANGE", raising=False)
    assert _executor("", tmp_path)._exchange_id() == "okx"


def test_env_override_wins_over_ccxt_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_CCXT_EXCHANGE", "kucoin")
    assert _executor("ccxt", tmp_path)._exchange_id() == "kucoin"


def test_explicit_venue_preserved(tmp_path, monkeypatch):
    # A real venue in ccxt_exchange must not be overridden.
    monkeypatch.setenv("HERMX_CCXT_EXCHANGE", "kucoin")
    ex = CcxtExecutor({"execution": {"ccxt_exchange": "bybit", "exchange": "ccxt"}}, tmp_path)
    assert ex._exchange_id() == "bybit"


def test_effective_execution_config_seeds_ccxt_exchange(wr):
    from webhook.config import EXECUTION_DEFAULTS

    cfg = wr._effective_execution_config()["execution"]
    assert cfg["ccxt_exchange"] == EXECUTION_DEFAULTS["ccxt_exchange"]
    # And that seeded config resolves to a real venue at the adapter.
    assert CcxtExecutor({"execution": cfg}, wr.ROOT)._exchange_id() == EXECUTION_DEFAULTS["ccxt_exchange"]
