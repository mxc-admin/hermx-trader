"""Gated paper/sandbox/testnet integration tests (parametrized over venue).

SKIPPED BY DEFAULT. Merged from the per-venue trio (OKX demo, KuCoin sandbox,
Hyperliquid testnet). Behind explicit per-venue env flags, each venue:
  * exercises the read-only CCXT path,
  * asserts the global kill switch blocks submission (no order placed), and
  * (write flag only) proves a real submit->query->close on the venue sandbox.

No network runs in the default suite -- every test is gated on its venue flag and
its credentials, and Hyperliquid additionally skips cleanly when the optional
``ccxt.hyperliquid`` build is absent.

    HERMX_RUN_OKX_PAPER_TESTS=true          HERMX_RUN_OKX_WRITE_TESTS=true
    HERMX_RUN_KUCOIN_PAPER_TESTS=true       HERMX_RUN_KUCOIN_WRITE_TESTS=true
    HERMX_RUN_HYPERLIQUID_PAPER_TESTS=true  HERMX_RUN_HYPERLIQUID_WRITE_TESTS=true
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest import mock

import pytest

from execution.service import ExecutionService
from executors.ccxt_adapter import CcxtExecutor
from executors.factory import ExecutorFactory
from security.credentials import resolve_exchange_credentials
from skills.hermes_execution import HermesRelayAdapter

from conftest import load_local_env, paper_execution_hooks


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}


def _no_guard() -> None:
    """Default per-venue availability guard (no extra dependency)."""


def _require_ccxt_hyperliquid() -> None:
    try:
        import ccxt  # noqa: WPS433 - optional dependency, imported lazily
    except Exception:  # pragma: no cover - dep guard
        pytest.skip("ccxt not installed")
    if getattr(ccxt, "hyperliquid", None) is None:
        pytest.skip("ccxt build has no hyperliquid support")


def _paper_config(repo_root: Path, config_file: str, ccxt_exchange: str) -> dict:
    cfg = json.loads((repo_root / "config" / config_file).read_text())
    execution = dict(cfg.get("execution") or {})
    execution.update(
        {
            "exchange": "ccxt",
            "ccxt_exchange": ccxt_exchange,
            "ccxt_default_type": "swap",
            "simulated_trading": True,
            "submit_orders": True,
        }
    )
    cfg["execution"] = execution
    return cfg


def _require_credentials(venue: dict):
    creds = resolve_exchange_credentials(venue["cred_venue"], os.environ)
    missing = [name for name in venue["cred_keys"] if not creds.get(name)]
    if missing:
        pytest.skip(f"missing {venue['cred_label']} credentials: {','.join(missing)}")
    return creds


# --------------------------------------------------------------------------- #
# Venue-specific read-only + health assertions (differ per exchange).           #
# --------------------------------------------------------------------------- #

def _read_only_okx(client) -> None:
    assert client.fetch_time()
    markets = client.load_markets()
    assert "BTC/USDT:USDT" in markets
    assert markets["BTC/USDT:USDT"].get("contract") is True
    assert markets["BTC/USDT:USDT"].get("contractSize")

    balance = client.fetch_balance({"type": "swap"})
    assert isinstance(balance, dict)

    positions = client.fetch_positions(["BTC/USDT:USDT"])
    assert isinstance(positions, list)


def _read_only_kucoin(client) -> None:
    assert client.fetch_time()
    markets = client.load_markets()
    assert markets


def _read_only_hyperliquid(client) -> None:
    markets = client.load_markets()
    assert markets


def _health_okx(executor) -> None:
    snap = executor.health()
    assert snap["ok"] is True
    assert snap["exchange"] == "ccxt"
    assert "generated_at" in snap
    assert isinstance(snap.get("positions"), list)
    assert isinstance(snap.get("account"), dict)
    assert isinstance((snap.get("account") or {}).get("currencies"), list)


def _health_kucoin(executor) -> None:
    assert executor._exchange_id() == "kucoin"
    snap = executor.health()
    assert snap["exchange"] == "ccxt"
    assert isinstance(snap.get("ok"), bool)


def _health_hyperliquid(executor) -> None:
    assert executor._exchange_id() == "hyperliquid"
    snap = executor.health()
    assert snap["exchange"] == "ccxt"
    assert isinstance(snap.get("ok"), bool)


# --------------------------------------------------------------------------- #
# Venue descriptors.                                                            #
# --------------------------------------------------------------------------- #

OKX = {
    "id": "okx",
    "paper_flag": "HERMX_RUN_OKX_PAPER_TESTS",
    "write_flag": "HERMX_RUN_OKX_WRITE_TESTS",
    "config_file": "runtime.demo.json",
    "ccxt_exchange": "okx",
    "cred_venue": "okx",
    "cred_keys": ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"),
    "cred_label": "OKX paper",
    "guard": _no_guard,
    "read_only": _read_only_okx,
    "health": _health_okx,
    "signal": {
        "strategy_id": "paper-integration",
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "2h",
        "tv_time": "2026-06-27T00:00:00Z",
        "signal_id": "okx-paper-kill-switch-proof",
    },
    "strategy": {
        "strategy_id": "paper-integration",
        "asset": "BTCUSDT",
        "inst_id": "BTC-USDT-SWAP",
        "budget_usd": 10,
        "leverage": 1,
        "td_mode": "isolated",
        "execution_mode": "live",
    },
    "symbol_key": "inst_id",
    "symbol_env": "HERMX_OKX_TEST_INST_ID",
    "symbol_default": "BTC-USDT-SWAP",
    "cl_prefix": "hermxokx",
    "amount": 1,
}

KUCOIN = {
    "id": "kucoin",
    "paper_flag": "HERMX_RUN_KUCOIN_PAPER_TESTS",
    "write_flag": "HERMX_RUN_KUCOIN_WRITE_TESTS",
    "config_file": "runtime.kucoin.demo.json",
    "ccxt_exchange": "kucoin",
    "cred_venue": "kucoin",
    "cred_keys": ("KUCOIN_API_KEY", "KUCOIN_SECRET", "KUCOIN_PASSPHRASE"),
    "cred_label": "KuCoin sandbox",
    "guard": _no_guard,
    "read_only": _read_only_kucoin,
    "health": _health_kucoin,
    "signal": {
        "strategy_id": "kucoin-paper-integration",
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "2h",
        "tv_time": "2026-06-27T00:00:00Z",
        "signal_id": "kucoin-paper-kill-switch-proof",
    },
    "strategy": {
        "strategy_id": "kucoin-paper-integration",
        "asset": "BTCUSDT",
        "inst_id": "BTC-USDT-SWAP",
        "instrument": {"exchange": "kucoin", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        "budget_usd": 10,
        "leverage": 1,
        "td_mode": "isolated",
        "execution_mode": "live",
    },
    "symbol_key": "inst_id",
    "symbol_env": "HERMX_KUCOIN_TEST_INST_ID",
    "symbol_default": "BTC-USDT-SWAP",
    "cl_prefix": "hermxkc",
    "amount": 1,
}

HYPERLIQUID = {
    "id": "hyperliquid",
    "paper_flag": "HERMX_RUN_HYPERLIQUID_PAPER_TESTS",
    "write_flag": "HERMX_RUN_HYPERLIQUID_WRITE_TESTS",
    "config_file": "runtime.hyperliquid.demo.json",
    "ccxt_exchange": "hyperliquid",
    "cred_venue": "hyperliquid",
    "cred_keys": ("HYPERLIQUID_WALLET_ADDRESS", "HYPERLIQUID_PRIVATE_KEY"),
    "cred_label": "Hyperliquid testnet",
    "guard": _require_ccxt_hyperliquid,
    "read_only": _read_only_hyperliquid,
    "health": _health_hyperliquid,
    "signal": {
        "strategy_id": "hyperliquid-paper-integration",
        "symbol": "BTCUSD",
        "side": "buy",
        "timeframe": "2h",
        "tv_time": "2026-06-27T00:00:00Z",
        "signal_id": "hyperliquid-paper-kill-switch-proof",
    },
    "strategy": {
        "strategy_id": "hyperliquid-paper-integration",
        "asset": "BTCUSD",
        "instrument": {"exchange": "hyperliquid", "inst_id": "BTC/USDC:USDC", "type": "swap"},
        "inst_id": "BTC/USDC:USDC",
        "budget_usd": 10,
        "leverage": 1,
        "td_mode": "cross",
        "execution_mode": "live",
    },
    "symbol_key": "ccxt_symbol",
    "symbol_env": "HERMX_HYPERLIQUID_TEST_SYMBOL",
    "symbol_default": "BTC/USDC:USDC",
    "cl_prefix": "hermxhl",
    "amount": 0.001,
}


def _venue_param(venue: dict):
    return pytest.param(
        venue,
        id=venue["id"],
        marks=[
            pytest.mark.integration,
            getattr(pytest.mark, f"{venue['id']}_paper"),
            pytest.mark.skipif(
                not _flag(venue["paper_flag"]),
                reason=f"set {venue['paper_flag']}=true to run {venue['cred_label']} integration tests",
            ),
        ],
    )


VENUES = [_venue_param(OKX), _venue_param(KUCOIN), _venue_param(HYPERLIQUID)]


@pytest.mark.parametrize("venue", VENUES)
def test_paper_ccxt_read_only(venue, repo_root):
    venue["guard"]()
    load_local_env(repo_root)
    _require_credentials(venue)

    executor = CcxtExecutor(_paper_config(repo_root, venue["config_file"], venue["ccxt_exchange"]), repo_root)
    venue["read_only"](executor._client())


@pytest.mark.parametrize("venue", VENUES)
def test_paper_executor_health_uses_ccxt_adapter(venue, repo_root):
    venue["guard"]()
    load_local_env(repo_root)
    _require_credentials(venue)

    executor = ExecutorFactory.create(_paper_config(repo_root, venue["config_file"], venue["ccxt_exchange"]), repo_root)
    assert isinstance(executor, CcxtExecutor)
    venue["health"](executor)


@pytest.mark.parametrize("venue", VENUES)
def test_hermes_skill_kill_switch_blocks_paper_submit(venue, repo_root, tmp_path, monkeypatch):
    venue["guard"]()
    load_local_env(repo_root)
    _require_credentials(venue)
    monkeypatch.setenv("HERMX_EXEC_BACKEND", "ccxt")
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    execution_ledger = tmp_path / "executions.jsonl"
    order_journal = tmp_path / "order-journal.jsonl"

    config = _paper_config(repo_root, venue["config_file"], venue["ccxt_exchange"])
    service = ExecutionService(
        config=config,
        root=repo_root,
        executor_factory=ExecutorFactory,
        hooks=paper_execution_hooks(execution_ledger, order_journal),
    )
    skill = HermesRelayAdapter(service=service)

    with mock.patch.object(CcxtExecutor, "execute", autospec=True) as adapter_execute:
        out = skill.execute(
            signal=venue["signal"],
            strategy=venue["strategy"],
            account_context={"auth_healthy": True},
            mode="live",
        )

    adapter_execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out.get("reason") == "live_trading_disabled"
    assert execution_ledger.exists()


@pytest.mark.parametrize("venue", VENUES)
def test_paper_ccxt_write_path_open_query_close(venue, repo_root):
    """PROVE the CCXT write path can submit->query->close on the venue sandbox.

    Sandbox/testnet only (simulated_trading=True) -- no real money. Leaves the
    account flat. Gated on the per-venue WRITE flag in addition to the paper flag.
    """
    if not _flag(venue["write_flag"]):
        pytest.skip(f"set {venue['write_flag']}=true to PLACE real {venue['cred_label']} orders (submit->query->close)")
    venue["guard"]()
    load_local_env(repo_root)
    _require_credentials(venue)

    symbol = os.environ.get(venue["symbol_env"], venue["symbol_default"])
    client_order_id = venue["cl_prefix"] + uuid.uuid4().hex[:16]
    executor = CcxtExecutor(_paper_config(repo_root, venue["config_file"], venue["ccxt_exchange"]), repo_root)

    open_readiness = {
        "signal_side": "buy",
        venue["symbol_key"]: symbol,
        "amount": venue["amount"],
        "execution_intent": {
            "client_order_id": client_order_id,
            "target_direction": "long",
            "actions": ["OPEN_LONG"],
        },
    }
    open_result = executor.execute(open_readiness)
    assert open_result["ok"] is True, f"open failed: {open_result}"
    assert open_result["mode"] == "submit_enabled"
    order_id = (open_result.get("fill_summary") or {}).get("order_id")
    assert order_id, f"no order id returned: {open_result}"

    queried = executor.get_order(symbol, ord_id=order_id, cl_ord_id=client_order_id)
    assert queried.get("state") not in {"not_implemented", "error", None}
    assert queried.get("exchange") == "ccxt"

    close_readiness = {
        "signal_side": "sell",
        venue["symbol_key"]: symbol,
        "execution_intent": {
            "client_order_id": client_order_id + "c",
            "target_direction": "short",
            "actions": ["CLOSE_LONG"],
        },
    }
    close_result = executor.execute(close_readiness)
    assert close_result["ok"] is True, f"close failed: {close_result}"
    position_after = (close_result.get("fill_summary") or {}).get("position_after_order") or {}
    assert position_after.get("side") == "flat", f"position not flat after close: {position_after}"
