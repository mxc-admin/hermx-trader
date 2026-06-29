from __future__ import annotations

import importlib
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
    cfg_path = repo_root / "shadow-config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
    else:
        cfg = json.loads((repo_root / "config" / "runtime.demo.json").read_text())
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
    cfg.setdefault("risk", {})["allow_live_execution"] = True
    return cfg


def _require_okx_credentials():
    creds = resolve_exchange_credentials("okx", os.environ)
    missing = [name for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE") if not creds.get(name)]
    if missing:
        pytest.skip(f"missing OKX paper credentials: {','.join(missing)}")
    return creds


_INST_INFO = {
    "BTC-USDT-SWAP": ("btcusdt_duo_base_dev_2h", "BTCUSDT", "2h"),
    "XRP-USDT-SWAP": ("xrpusdt_duo_base_dev_4h", "XRPUSDT", "4h"),
}


def _build_okx_demo_shadow_root(
    repo_root: Path,
    tmp_path: Path,
    strategy_overrides: dict | None = None,
) -> Path:
    """Create a temporary SHADOW_ROOT wired for OKX demo CCXT execution.

    Copies the real strategy files from ``strategies/`` and writes a
    ``shadow-config.json`` that enables the CCXT/OKX demo path. Per-strategy
    overrides (e.g. small budget for a small-notional test) are applied to
    the strategy files before the receiver binds them.
    """
    root = tmp_path / "okx-demo-shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    for src in sorted((repo_root / "strategies").glob("*.json")):
        strategy = json.loads(src.read_text())
        overrides = (strategy_overrides or {}).get(src.stem)
        if overrides:
            strategy.update(overrides)
        (strategies_dir / src.name).write_text(
            json.dumps(strategy, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    cfg = json.loads((repo_root / "config" / "runtime.demo.json").read_text())
    execution = dict(cfg.get("execution") or {})
    execution.update(
        {
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "ccxt_default_type": "swap",
            "simulated_trading": True,
            "submit_orders": True,
            "td_mode": "cross",
            "order_type": "market",
        }
    )
    cfg["execution"] = execution
    cfg.setdefault("risk", {})["allow_live_execution"] = True
    (root / "shadow-config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _load_wr_for_root(shadow_root: Path):
    """Return webhook_receiver reloaded with SHADOW_ROOT bound to ``shadow_root``."""
    import webhook_receiver as wr  # noqa: WPS433

    os.environ["SHADOW_ROOT"] = str(shadow_root)
    importlib.reload(wr)
    return wr


@pytest.fixture
def okx_demo_wr(repo_root, tmp_path, shadow_root):
    """webhook_receiver bound to a temporary OKX demo SHADOW_ROOT."""
    wr = _load_wr_for_root(_build_okx_demo_shadow_root(repo_root, tmp_path))
    try:
        yield wr
    finally:
        os.environ["SHADOW_ROOT"] = str(shadow_root)
        importlib.reload(wr)


def _direct_okx_executor(repo_root: Path, td_mode: str) -> CcxtExecutor:
    return CcxtExecutor(_okx_write_config(repo_root, td_mode), repo_root)


def _close_position_direct(executor: CcxtExecutor, inst_id: str, td_mode: str) -> None:
    """Close any open ``inst_id`` position using a direct CcxtExecutor call."""
    positions = executor.get_positions(inst_id)
    if not positions:
        return
    pos = positions[0]
    current_pos = float(pos.get("pos") or 0.0)
    if current_pos == 0.0:
        return
    current_side = "long" if current_pos > 0 else "short"
    close_signal_side = "sell" if current_side == "long" else "buy"
    close_target = "short" if current_side == "long" else "long"
    client_order_id = "hermxclose" + uuid.uuid4().hex[:16]
    close_readiness = {
        "signal_side": close_signal_side,
        "inst_id": inst_id,
        "td_mode": td_mode,
        "execution_intent": {
            "client_order_id": client_order_id,
            "target_direction": close_target,
            "actions": [f"CLOSE_{current_side.upper()}"],
        },
    }
    result = executor.execute(close_readiness)
    print("DIRECT CLOSE result:", json.dumps(result, default=str, indent=2))
    assert result["ok"] is True, f"direct close failed: {result}"


def _tv_alert(strategy_id: str, symbol: str, side: str, timeframe: str, tv_time: str, price: float) -> dict:
    """Construct a TradingView strategy-file alert payload."""
    return {
        "source": "tradingview",
        "strategy_id": strategy_id,
        "strategy_name": f"{symbol} Duo Base Dev {timeframe.upper()}",
        "indicator": "mxc duo-base",
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "tv_time": tv_time,
        "tv_signal_price": price,
        "exchange": "okx",
    }


def _okx_execution_result(record: dict) -> dict:
    return record.get("okx_execution") or {}


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


def _okx_write_config(repo_root: Path, td_mode: str) -> dict:
    cfg = _okx_paper_config(repo_root)
    cfg["execution"]["td_mode"] = td_mode
    cfg["execution"]["order_type"] = "market"
    return cfg


@pytest.mark.skipif(
    not RUN_OKX_WRITE,
    reason="set HERMX_RUN_OKX_WRITE_TESTS=true to PLACE real OKX *demo* orders (submit->query->close)",
)
def test_okx_paper_receiver_btc_roundtrip(repo_root, okx_demo_wr):
    """PROVE the CCXT write path through the receiver API on OKX demo.

    Constructs a TradingView strategy-file alert, processes it through
    ``webhook_receiver.build_record``, confirms the order is queryable, then
    flattens the demo account. Demo only (simulated_trading=True) -- no real money.
    """
    _load_local_env(repo_root)
    _require_okx_credentials()

    wr = okx_demo_wr
    td_mode = "cross"
    inst_id = "BTC-USDT-SWAP"
    strategy_id, symbol, timeframe = _INST_INFO[inst_id]
    # Small budget so the planned notional is ~1 BTC swap contract.
    wr.STRATEGIES[strategy_id]["budget_usd"] = 300
    wr.STRATEGIES[strategy_id]["margin_mode"] = td_mode

    executor = _direct_okx_executor(repo_root, td_mode)
    _close_position_direct(executor, inst_id, td_mode)

    buy_alert = _tv_alert(strategy_id, symbol, "buy", timeframe, "2026-06-28T00:00:00Z", 65000.0)
    status, record = wr.build_record(buy_alert, "2026-06-28T00:00:00Z")
    print("BUY record:", json.dumps(record, default=str, indent=2))
    assert status == 200
    okx_exec = _okx_execution_result(record)
    assert okx_exec.get("mode") == "submit_enabled", f"buy failed: {okx_exec}"
    order_id = (okx_exec.get("payload", {}).get("fill_summary") or {}).get("order_id")
    assert order_id, f"no order id returned: {okx_exec}"

    queried = executor.get_order(inst_id, ord_id=order_id)
    print("QUERY result:", json.dumps(queried, default=str, indent=2))
    assert queried.get("state") not in {"not_implemented", "error", None}
    assert queried.get("exchange") == "ccxt"

    # A sell signal closes the existing long and opens a short; the direct cleanup
    # afterwards flattens the demo account.
    sell_alert = _tv_alert(strategy_id, symbol, "sell", timeframe, "2026-06-28T00:01:00Z", 65000.0)
    status, record = wr.build_record(sell_alert, "2026-06-28T00:01:00Z")
    print("SELL record:", json.dumps(record, default=str, indent=2))
    assert status == 200
    okx_exec = _okx_execution_result(record)
    assert okx_exec.get("mode") == "submit_enabled", f"sell failed: {okx_exec}"

    _close_position_direct(executor, inst_id, td_mode)


@pytest.mark.skipif(
    not RUN_OKX_WRITE,
    reason="set HERMX_RUN_OKX_WRITE_TESTS=true to PLACE real OKX *demo* orders (submit->query->close)",
)
def test_okx_paper_receiver_xrp_usdt_small_notional(repo_root, okx_demo_wr):
    """Small-notional XRP-USDT-SWAP demo roundtrip via the receiver API.

    Opens a ~$5 long by lowering the strategy budget, confirms the order is
    queryable, then flattens the demo account. Demo only (simulated_trading=True).
    """
    _load_local_env(repo_root)
    _require_okx_credentials()

    wr = okx_demo_wr
    td_mode = "cross"
    inst_id = "XRP-USDT-SWAP"
    strategy_id, symbol, timeframe = _INST_INFO[inst_id]
    wr.STRATEGIES[strategy_id]["budget_usd"] = 2.5
    wr.STRATEGIES[strategy_id]["margin_mode"] = td_mode

    executor = _direct_okx_executor(repo_root, td_mode)
    _close_position_direct(executor, inst_id, td_mode)

    buy_alert = _tv_alert(strategy_id, symbol, "buy", timeframe, "2026-06-28T00:00:00Z", 2.0)
    status, record = wr.build_record(buy_alert, "2026-06-28T00:00:00Z")
    print("BUY record:", json.dumps(record, default=str, indent=2))
    assert status == 200
    okx_exec = _okx_execution_result(record)
    assert okx_exec.get("mode") == "submit_enabled", f"buy failed: {okx_exec}"
    order_id = (okx_exec.get("payload", {}).get("fill_summary") or {}).get("order_id")
    assert order_id, f"no order id returned: {okx_exec}"

    queried = executor.get_order(inst_id, ord_id=order_id)
    print("QUERY result:", json.dumps(queried, default=str, indent=2))
    assert queried.get("state") not in {"not_implemented", "error", None}
    assert queried.get("exchange") == "ccxt"

    sell_alert = _tv_alert(strategy_id, symbol, "sell", timeframe, "2026-06-28T00:01:00Z", 2.0)
    status, record = wr.build_record(sell_alert, "2026-06-28T00:01:00Z")
    print("SELL record:", json.dumps(record, default=str, indent=2))
    assert status == 200
    okx_exec = _okx_execution_result(record)
    assert okx_exec.get("mode") == "submit_enabled", f"sell failed: {okx_exec}"

    _close_position_direct(executor, inst_id, td_mode)
