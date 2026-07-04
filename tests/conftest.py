"""Shared pytest fixtures for the HermX test harness (Phase 0 setup).

Safety-critical invariant: tests must NEVER read or write the real runtime
logs/state. ``webhook_receiver`` resolves ``HERMX_ROOT`` at *import* time, so we
redirect it to an isolated temp directory BEFORE the module is ever imported.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Repo layout: tests/ is a direct child of the repo root; code lives in src/.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Redirect HERMX_ROOT to an isolated temp dir up front, so merely importing
# webhook_receiver (which calls mkdir / load_engine_config at module scope)
# cannot touch the real repo logs/ or paper-state.json.
_HERMX_ROOT = Path(tempfile.mkdtemp(prefix="hermx-test-shadow-root-"))
(_HERMX_ROOT / "logs").mkdir(parents=True, exist_ok=True)
# Production resolves the root solely from HERMX_ROOT (the legacy SHADOW_ROOT
# fallback was removed), so binding it here fully isolates the test process.
os.environ["HERMX_ROOT"] = str(_HERMX_ROOT)
# HERMX_SECRET is the sole source authenticating both the webhook and the dashboard.
os.environ.setdefault("HERMX_SECRET", "test-secret")

# Ensure the live-trading kill switch starts disabled (fail-closed) for every test
# process, unless an individual test opts in via monkeypatch.
os.environ.pop("HERMX_LIVE_TRADING", None)

# Make `import webhook_receiver` resolve against src/.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture
def shadow_root() -> Path:
    """The isolated temp HERMX_ROOT the receiver module was bound to."""
    return _HERMX_ROOT


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# Characterization harness (REFACTOR_PLAN.md:160 task 3, :167, :179).
#
# webhook_receiver binds STRATEGIES / ALLOWED_SYMBOLS / STRATEGY_ENGINE and every
# LOG_DIR/state path from HERMX_ROOT *at import time*. The default conftest
# HERMX_ROOT above is an empty dir (STRATEGIES={}), so strategy alerts would be
# rejected unknown_strategy_id and no corpus config would apply.
#
# Chosen approach: build a fresh, *populated* temp HERMX_ROOT per test (copy the
# 4 corpus strategy files + write a synthetic engine-config.json), point
# HERMX_ROOT at it, and importlib.reload(webhook_receiver) so its globals rebind
# to that populated root. This is the faithful approach -- it exercises the real
# module-load binding (load_engine_config / load_strategy_files) rather than
# monkeypatching globals after the fact. Per-test reload also gives full isolation
# (fresh paper-state.json, seen-signals.json, logs/) so tests never touch the real
# runtime state and never interfere with each other.
#
# On teardown we restore HERMX_ROOT to the session temp root and reload again, so
# tests that import webhook_receiver WITHOUT this fixture (e.g.
# test_phase5_exec_routing) keep a module bound to a live (non-deleted) directory.
# ---------------------------------------------------------------------------

CORPUS_STRATEGIES_DIR = FIXTURES_DIR / "strategies"

# engine-config.json is the sole config the receiver binds (STRATEGY_ENGINE +
# advisor). shadow-config.json was removed entirely. require_strategy_id=false
# (unlike the production VPS profile) lets the corpus exercise non-strategy alerts
# without quarantine.
CORPUS_ENGINE_CONFIG = {
    "strategy_engine": {
        "enabled": True,
        "strategies_dir": "strategies",
        "default_status": "trial_candidate",
        "allow_strategy_alerts": True,
        "require_strategy_id": False,
        "quarantine_invalid_strategy_alerts": True,
    },
    "advisor": {
        "enabled": False, "command": "hermes", "skills": "hermx-control",
        "model": "", "timeout_seconds": 30.0,
    },
}


def write_engine_config(root: Path) -> None:
    """Write the corpus engine-config.json into a test HERMX_ROOT."""
    (root / "engine-config.json").write_text(
        json.dumps(CORPUS_ENGINE_CONFIG, indent=2), encoding="utf-8"
    )


def _build_populated_root(root: Path) -> None:
    """Lay out a HERMX_ROOT the receiver can bind to: logs/, strategies/, config."""
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(CORPUS_STRATEGIES_DIR.glob("*.json")):
        shutil.copy(src, strategies_dir / src.name)
    write_engine_config(root)


@pytest.fixture
def wr_root(tmp_path) -> Path:
    """A fresh populated HERMX_ROOT for one test."""
    root = tmp_path / "shadow-root"
    _build_populated_root(root)
    return root


@pytest.fixture
def wr(wr_root, monkeypatch):
    """webhook_receiver reloaded with globals bound to a populated HERMX_ROOT.

    Kill switch starts unset (inert/armed). Execution is hard-disabled via the
    corpus config, so no OKX subprocess is ever armed regardless.
    """
    import webhook_receiver as module  # noqa: WPS433 (import inside fixture is intentional)

    orig_shadow_root = os.environ.get("HERMX_ROOT")
    os.environ["HERMX_ROOT"] = str(wr_root)
    os.environ.pop("HERMX_LIVE_TRADING", None)
    importlib.reload(module)
    try:
        yield module
    finally:
        # Rebind to a live directory so unrelated tests don't import a module
        # whose LOG_DIR points at this now-vanishing tmp_path.
        if orig_shadow_root is not None:
            os.environ["HERMX_ROOT"] = orig_shadow_root
        else:
            os.environ["HERMX_ROOT"] = str(_HERMX_ROOT)
        importlib.reload(module)


@pytest.fixture
def reload_wr():
    """Loader for webhook_receiver bound to an arbitrary populated HERMX_ROOT.
    Unlike `wr`, the returned callable can be invoked multiple times in one test to
    bind different roots (e.g. a "crash" reload of the same root).

    webhook_receiver resolves HERMX_ROOT at import time, so each call sets the env
    then importlib.reload()s the module. Teardown restores HERMX_ROOT to the live
    session root so unrelated tests keep a module bound to a non-deleted directory.
    """
    import webhook_receiver as module  # noqa: WPS433

    orig_root = os.environ.get("HERMX_ROOT")

    def _load(root):
        root = Path(root)
        _build_populated_root(root)
        os.environ["HERMX_ROOT"] = str(root)
        os.environ.pop("HERMX_LIVE_TRADING", None)
        importlib.reload(module)
        return module

    try:
        yield _load
    finally:
        os.environ["HERMX_ROOT"] = orig_root if orig_root is not None else str(_HERMX_ROOT)
        importlib.reload(module)


# ---------------------------------------------------------------------------
# Snapshot (golden) helper.
#
# Normalization rule (documented, deterministic): the corpus drives every alert
# with a FIXED tv_time and passes received_at_override, so signal_id (sha256 of
# strategy_id|symbol|side|timeframe|tv_time), client_order_id, latency, and all
# *_at fields are already stable. The only non-portable values are absolute
# filesystem paths (e.g. strategy_config["_path"] = "<tmp>/strategies/x.json").
# normalize_snapshot() therefore:
#   1. drops any dict key named "_path",
#   2. replaces any string containing the active HERMX_ROOT path with "<ROOT>".
# Set SNAPSHOT_UPDATE=1 to (re)generate goldens.
# ---------------------------------------------------------------------------

SNAPSHOTS_DIR = FIXTURES_DIR / "snapshots"


def normalize_snapshot(obj, root: Path):
    root_str = str(root)
    if isinstance(obj, dict):
        return {k: normalize_snapshot(v, root) for k, v in obj.items() if k != "_path"}
    if isinstance(obj, list):
        return [normalize_snapshot(v, root) for v in obj]
    if isinstance(obj, str) and root_str in obj:
        return obj.replace(root_str, "<ROOT>")
    return obj


@pytest.fixture
def assert_snapshot(wr_root):
    def _assert(name: str, obj) -> None:
        normalized = normalize_snapshot(obj, wr_root)
        path = SNAPSHOTS_DIR / name
        if os.environ.get("SNAPSHOT_UPDATE") == "1" or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        expected = json.loads(path.read_text(encoding="utf-8"))
        assert normalized == expected, f"snapshot drift vs {path.name}"

    return _assert


def load_alert(rel_path: str) -> dict:
    """Load an alert payload fixture by path relative to tests/fixtures/alerts/."""
    return json.loads((FIXTURES_DIR / "alerts" / rel_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Shared HTTP-server harnesses (loopback, ephemeral port).
#
# Two shapes, both previously copy-pasted across the integration suites:
#   * _serve/_stop -- explicit (server, thread) pair for try/finally call sites
#     (webhook + dashboard auth/security tests).
#   * serve_dashboard -- contextmanager yielding the bound port (strategy-override
#     dashboard tests).
# ---------------------------------------------------------------------------

import threading  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from http.server import HTTPServer, ThreadingHTTPServer  # noqa: E402


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@contextmanager
def serve_dashboard(dash_mod):
    """Run the dashboard Handler on an ephemeral loopback port for one test."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), dash_mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Executor test doubles (mock CCXT submit seam + normalized adapter results).
# ---------------------------------------------------------------------------

def adapter_result(*, ok=True, mode="submit_enabled", client_order_id=None,
                   order_id="ord-1", payload=None) -> dict:
    """A normalized adapter result (BaseExecutor.normalized_result shape)."""
    return {
        "ok": ok,
        "mode": mode,
        "exchange": "ccxt",
        "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": order_id, "client_order_id": client_order_id},
        "payload": {} if payload is None else payload,
    }


def fake_executor(result=None):
    """Stand-in CCXT submit executor: its .execute is the single submit call."""
    from unittest import mock  # local import keeps conftest import-time light
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=adapter_result() if result is None else result)
    return fake


# ---------------------------------------------------------------------------
# Strategy JSON fixture writer (schema v2 template).
# ---------------------------------------------------------------------------

STRATEGY_TEMPLATE = {
    "schema_version": 2,
    "name": "Test Strategy",
    "asset": "BTCUSDT",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "timeframe": "2h",
    "chart_type": "heikin_ashi",
    "budget_usd": 1500,
    "leverage": 2,
    "margin_mode": "isolated",
    "execution_mode": "demo",
    "submit_orders": True,
    "status": "active_demo",
}


def _write_strategy(strategies_dir: Path, strategy_id: str, **overrides) -> Path:
    row = dict(STRATEGY_TEMPLATE)
    row["strategy_id"] = strategy_id
    row.update(overrides)
    path = strategies_dir / f"{strategy_id}.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# PnL ledger isolation fixture.
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    """Isolated data dir for the ledger; returns the closed-trades.jsonl path."""
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    return tmp_path / "closed-trades.jsonl"


# ---------------------------------------------------------------------------
# Gated paper/sandbox integration helpers (shared by test_paper_integration.py).
# ---------------------------------------------------------------------------

def load_local_env(repo_root: Path) -> None:
    """Load repo-root .env into os.environ (setdefault) for gated integration runs."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def paper_execution_hooks(execution_ledger: Path, order_journal: Path) -> dict:
    """The full ExecutionService hook surface used by the kill-switch paper proofs."""
    return {
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
