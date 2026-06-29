"""Shared pytest fixtures for the HermX test harness (Phase 0 setup).

Safety-critical invariant: tests must NEVER read or write the real runtime
logs/state. ``webhook_receiver`` resolves ``SHADOW_ROOT`` at *import* time, so we
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

# Redirect SHADOW_ROOT to an isolated temp dir up front, so merely importing
# webhook_receiver (which calls mkdir / load_shadow_config at module scope)
# cannot touch the real repo logs/ or paper-state.json.
_SHADOW_ROOT = Path(tempfile.mkdtemp(prefix="hermx-test-shadow-root-"))
(_SHADOW_ROOT / "logs").mkdir(parents=True, exist_ok=True)
os.environ["SHADOW_ROOT"] = str(_SHADOW_ROOT)
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
    """The isolated temp SHADOW_ROOT the receiver module was bound to."""
    return _SHADOW_ROOT


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# Characterization harness (REFACTOR_PLAN.md:160 task 3, :167, :179).
#
# webhook_receiver binds CONFIG / STRATEGIES / ALLOWED_SYMBOLS / STRATEGY_ENGINE /
# POLICY_KEYS and every LOG_DIR/state path from SHADOW_ROOT *at import time*. The
# default conftest SHADOW_ROOT above is an empty dir (CONFIG=defaults,
# STRATEGIES={}), so strategy alerts would be rejected unknown_strategy_id and no
# corpus config would apply.
#
# Chosen approach: build a fresh, *populated* temp SHADOW_ROOT per test (copy the
# 4 corpus strategy files + the synthetic dry-run shadow-config.json), point
# SHADOW_ROOT at it, and importlib.reload(webhook_receiver) so its globals rebind
# to that populated root. This is the faithful approach -- it exercises the real
# module-load binding (load_shadow_config / load_strategy_files) rather than
# monkeypatching globals after the fact. Per-test reload also gives full isolation
# (fresh paper-state.json, seen-signals.json, logs/) so tests never touch the real
# runtime state and never interfere with each other.
#
# On teardown we restore SHADOW_ROOT to the session temp root and reload again, so
# tests that import webhook_receiver WITHOUT this fixture (e.g. test_kill_switch)
# keep a module bound to a live (non-deleted) directory.
# ---------------------------------------------------------------------------

CORPUS_STRATEGIES_DIR = FIXTURES_DIR / "strategies"
CORPUS_CONFIG = FIXTURES_DIR / "config" / "shadow-config.dryrun.json"


def _build_populated_root(root: Path) -> None:
    """Lay out a SHADOW_ROOT the receiver can bind to: logs/, strategies/, config."""
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(CORPUS_STRATEGIES_DIR.glob("*.json")):
        shutil.copy(src, strategies_dir / src.name)
    shutil.copy(CORPUS_CONFIG, root / "shadow-config.json")
    # engine-config.json is the split-out home of strategy_engine + advisor (the
    # receiver reads STRATEGY_ENGINE from it, not shadow-config.json). Derive it from
    # the corpus strategy_engine block so the bound STRATEGY_ENGINE — notably
    # require_strategy_id=false, which lets the corpus exercise non-strategy alerts
    # without quarantine — is byte-identical to the pre-split behavior.
    corpus = json.loads(CORPUS_CONFIG.read_text(encoding="utf-8"))
    engine_cfg = {
        "strategy_engine": corpus.get("strategy_engine", {}),
        "advisor": corpus.get("advisor", {
            "enabled": False, "command": "hermes", "skills": "hermx-control",
            "model": "", "timeout_seconds": 30.0,
        }),
    }
    (root / "engine-config.json").write_text(json.dumps(engine_cfg, indent=2), encoding="utf-8")


@pytest.fixture
def wr_root(tmp_path) -> Path:
    """A fresh populated SHADOW_ROOT for one test."""
    root = tmp_path / "shadow-root"
    _build_populated_root(root)
    return root


@pytest.fixture
def wr(wr_root, monkeypatch):
    """webhook_receiver reloaded with globals bound to a populated SHADOW_ROOT.

    Kill switch starts unset (inert/armed). Execution is hard-disabled via the
    corpus config, so no OKX subprocess is ever armed regardless.
    """
    import webhook_receiver as module  # noqa: WPS433 (import inside fixture is intentional)

    orig_shadow_root = os.environ.get("SHADOW_ROOT")
    os.environ["SHADOW_ROOT"] = str(wr_root)
    os.environ.pop("HERMX_LIVE_TRADING", None)
    importlib.reload(module)
    try:
        yield module
    finally:
        # Rebind to a live directory so unrelated tests don't import a module
        # whose LOG_DIR points at this now-vanishing tmp_path.
        if orig_shadow_root is not None:
            os.environ["SHADOW_ROOT"] = orig_shadow_root
        else:
            os.environ["SHADOW_ROOT"] = str(_SHADOW_ROOT)
        importlib.reload(module)


@pytest.fixture
def reload_wr():
    """Loader for webhook_receiver bound to an arbitrary populated SHADOW_ROOT and
    HERMX_STATE_BACKEND (Phase 1 task 1 tests). Unlike `wr`, the returned callable
    can be invoked multiple times in one test to bind different roots/backends
    (e.g. a legacy run and a journal run, or a "crash" reload of the same root).

    webhook_receiver resolves SHADOW_ROOT and reads HERMX_STATE_BACKEND at import
    time, so each call sets the env then importlib.reload()s the module. Teardown
    restores SHADOW_ROOT to the live session root and clears HERMX_STATE_BACKEND so
    unrelated tests keep a module bound to a non-deleted directory in legacy mode.
    """
    import webhook_receiver as module  # noqa: WPS433

    orig_root = os.environ.get("SHADOW_ROOT")
    orig_backend = os.environ.get("HERMX_STATE_BACKEND")

    def _load(root, backend="legacy"):
        root = Path(root)
        _build_populated_root(root)
        os.environ["SHADOW_ROOT"] = str(root)
        if backend:
            os.environ["HERMX_STATE_BACKEND"] = backend
        else:
            os.environ.pop("HERMX_STATE_BACKEND", None)
        os.environ.pop("HERMX_LIVE_TRADING", None)
        importlib.reload(module)
        return module

    try:
        yield _load
    finally:
        os.environ["SHADOW_ROOT"] = orig_root if orig_root is not None else str(_SHADOW_ROOT)
        if orig_backend is not None:
            os.environ["HERMX_STATE_BACKEND"] = orig_backend
        else:
            os.environ.pop("HERMX_STATE_BACKEND", None)
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
#   2. replaces any string containing the active SHADOW_ROOT path with "<ROOT>".
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
