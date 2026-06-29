"""Gated OKX demo integration tests.

SKIPPED BY DEFAULT. Behind explicit per-venue env flags: asserts the global kill
switch blocks submission, exercises the read-only CCXT path, and (write flag only)
proves a real demo submit->query->close on OKX. No network runs in the default suite.

    HERMX_RUN_OKX_PAPER_TESTS=true   # read-only + kill-switch proofs
    HERMX_RUN_OKX_WRITE_TESTS=true   # PLACE real OKX *demo* orders
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


RUN_OKX_PAPER = (os.environ.get("HERMX_RUN_OKX_PAPER_TESTS") or "").strip().lower() in {"1", "true", "yes"}
RUN_OKX_WRITE = (os.environ.get("HERMX_RUN_OKX_WRITE_TESTS") or "").strip().lower() in {"1", "true", "yes"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.okx_paper,
    pytest.mark.skipif(
        not RUN_OKX_PAPER,
        reason="set HERMX_RUN_OKX_PAPER_TESTS=true to run OKX paper/demo integration tests",
    ),
]


def _load_local_env(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _okx_paper_config(repo_root: Path) -> dict:
    cfg_path = repo_root / "config" / "runtime.demo.json"
    cfg = json.loads(cfg_path.read_text())
    execution = dict(cfg.get("execution") or {})
    execution.update(
        {
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "ccxt_default_type": "swap",
            "simulated_trading": True,
            "submit_orders": True,
        }
    )
    cfg["execution"] = execution
    return cfg


def _require_okx_credentials():
    creds = resolve_exchange_credentials("okx", os.environ)
    missing = [name for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE") if not creds.get(name)]
    if missing:
        pytest.skip(f"missing OKX paper credentials: {','.join(missing)}")
    return creds


def test_okx_paper_ccxt_public_and_private_read_only(repo_root):
    _load_local_env(repo_root)
    _require_okx_credentials()

    executor = CcxtExecutor(_okx_paper_config(repo_root), repo_root)
    client = executor._client()

    assert client.fetch_time()
    markets = client.load_markets()
    assert "BTC/USDT:USDT" in markets
    assert markets["BTC/USDT:USDT"].get("contract") is True
    assert markets["BTC/USDT:USDT"].get("contractSize")

    balance = client.fetch_balance({"type": "swap"})
    assert isinstance(balance, dict)

    positions = client.fetch_positions(["BTC/USDT:USDT"])
    assert isinstance(positions, list)


def test_okx_paper_executor_health_uses_ccxt_adapter(repo_root):
    _load_local_env(repo_root)
    _require_okx_credentials()

    executor = ExecutorFactory.create(_okx_paper_config(repo_root), repo_root)
    assert isinstance(executor, CcxtExecutor)

    snap = executor.health()

    assert snap["ok"] is True
    assert snap["exchange"] == "ccxt"
    assert "generated_at" in snap
    assert isinstance(snap.get("positions"), list)
    assert isinstance(snap.get("account"), dict)
    assert isinstance((snap.get("account") or {}).get("currencies"), list)


def test_hermes_skill_to_execution_service_kill_switch_blocks_okx_paper_submit(repo_root, tmp_path, monkeypatch):
    _load_local_env(repo_root)
    _require_okx_credentials()
    monkeypatch.setenv("HERMX_EXEC_BACKEND", "ccxt")
    monkeypatch.setenv("HERMX_LIVE_TRADING", "false")

    execution_ledger = tmp_path / "executions.jsonl"
    order_journal = tmp_path / "order-journal.jsonl"

    hooks = {
        "live_trading_enabled": lambda: (False, "false"),
        "append_jsonl": lambda path, row: Path(path).open("a").write(json.dumps(row) + "\n"),
        "execution_ledger": execution_ledger,
        "order_journal": order_journal,
        "webhook_auth_config_healthy": lambda: True,
        "watchdog_submission_state": lambda: (True, None),
        "symbol_pause_info": lambda symbol: None,
        "order_intent_from_readiness": lambda readiness: readiness.get("execution_intent") or {},
        "cl_ord_id_from_readiness": lambda readiness: (readiness.get("execution_intent") or {}).get("client_order_id"),
        "latest_order_record": lambda client_order_id: None,
        "record_order_state": lambda *args, **kwargs: None,
        "fail_closed_state_write": lambda *args, **kwargs: None,
        "post_submit_reconcile": lambda *args, **kwargs: None,
        "order_state_planned": lambda: "PLANNED",
        "order_state_submitted": lambda: "SUBMITTED",
        "order_state_filled": lambda: "FILLED",
        "order_state_rejected": lambda: "REJECTED",
        "order_state_unknown": lambda: "UNKNOWN",
        "reconcile_post_submit_enabled": lambda: False,
        "reconciliation_executor": lambda: None,
        "reconcile_order_with_backoff": lambda *args, **kwargs: None,
        "order_state_can_transition": lambda old, new: True,
        "emit_reconcile_alert": lambda *args, **kwargs: None,
        "reconcile_alert_mismatch": lambda *args, **kwargs: None,
        "redact_secrets": lambda text: text,
    }
    service = ExecutionService(config=_okx_paper_config(repo_root), root=repo_root, executor_factory=ExecutorFactory, hooks=hooks)
    skill = HermesRelayAdapter(service=service)

    with mock.patch.object(CcxtExecutor, "execute", autospec=True) as adapter_execute:
        out = skill.execute(
            signal={
                "strategy_id": "paper-integration",
                "symbol": "BTCUSDT",
                "side": "buy",
                "timeframe": "2h",
                "tv_time": "2026-06-27T00:00:00Z",
                "signal_id": "okx-paper-kill-switch-proof",
            },
            strategy={
                "strategy_id": "paper-integration",
                "asset": "BTCUSDT",
                "inst_id": "BTC-USDT-SWAP",
                "budget_usd": 10,
                "leverage": 1,
                "td_mode": "isolated",
                "execution_mode": "live",
            },
            account_context={"auth_healthy": True},
            mode="live",
        )

    adapter_execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out.get("reason") == "live_trading_disabled"
    assert execution_ledger.exists()


@pytest.mark.skipif(
    not RUN_OKX_WRITE,
    reason="set HERMX_RUN_OKX_WRITE_TESTS=true to PLACE real OKX *demo* orders (submit->query->close)",
)
def test_okx_paper_ccxt_write_path_open_query_close(repo_root):
    """PROVE the CCXT write path can submit->query->close on the OKX demo.

    Demo only (simulated_trading=True) -- no real money. Leaves the account flat.
    """
    _load_local_env(repo_root)
    _require_okx_credentials()

    inst_id = os.environ.get("HERMX_OKX_TEST_INST_ID", "BTC-USDT-SWAP")
    client_order_id = "hermxokx" + uuid.uuid4().hex[:16]
    executor = CcxtExecutor(_okx_paper_config(repo_root), repo_root)

    open_readiness = {
        "signal_side": "buy",
        "inst_id": inst_id,
        "amount": 1,
        "execution_intent": {
            "client_order_id": client_order_id,
            "target_direction": "long",
            "actions": ["OPEN_LONG"],
        },
    }
    open_result = executor.execute(open_readiness)
    print("OPEN result:", json.dumps(open_result, default=str, indent=2))
    assert open_result["ok"] is True, f"open failed: {open_result}"
    assert open_result["mode"] == "submit_enabled"
    order_id = (open_result.get("fill_summary") or {}).get("order_id")
    assert order_id, f"no order id returned: {open_result}"

    queried = executor.get_order(inst_id, ord_id=order_id, cl_ord_id=client_order_id)
    print("QUERY result:", json.dumps(queried, default=str, indent=2))
    assert queried.get("state") not in {"not_implemented", "error", None}
    assert queried.get("exchange") == "ccxt"

    close_readiness = {
        "signal_side": "sell",
        "inst_id": inst_id,
        "execution_intent": {
            "client_order_id": client_order_id + "c",
            "target_direction": "short",
            "actions": ["CLOSE_LONG"],
        },
    }
    close_result = executor.execute(close_readiness)
    print("CLOSE result:", json.dumps(close_result, default=str, indent=2))
    assert close_result["ok"] is True, f"close failed: {close_result}"
    position_after = (close_result.get("fill_summary") or {}).get("position_after_order") or {}
    assert position_after.get("side") == "flat", f"position not flat after close: {position_after}"
