"""Phase F — Docker deployment posture tests (bind host + state relocation).

These guard the two import-time knobs the Docker bridge compose relies on:

  * ``HERMX_BIND_HOST`` — defaults to loopback (unchanged for host/local deploys),
    overridable to ``0.0.0.0`` for bridge networking.
  * ``HERMX_DATA_DIR`` — relocates ONLY the mutable state snapshots
    (paper-state, control-state, latest) onto a dedicated volume, while
    logs / config / strategies stay under ROOT. (Dedup state moved to the
    consolidated signals.jsonl under LOG_DIR, so seen-signals.json is gone.)

Both ``webhook_receiver`` and ``dashboard`` resolve these at *import* time, so each
case is exercised in a fresh subprocess with a controlled environment. That keeps
the parent process's module globals (bound by conftest to the session HERMX_ROOT)
completely untouched — no reload side effects leak into other tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Slow by design: every case spawns a fresh Python subprocess so the module's
# import-time env resolution is exercised for real (no reload side effects).
pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

# The mutable state snapshots Phase B moves under HERMX_DATA_DIR. (seen-signals.json
# was retired in the JSONL ledger consolidation -- dedup state now lives in the
# LOG_DIR-anchored signals.jsonl, so SIGNAL_STATE_FILE no longer exists; paper-state.json
# was retired with the shadow/position-journal removal, so PAPER_STATE_FILE is gone too.)
MUTABLE_ATTRS = ["CONTROL_STATE_FILE", "LATEST_FILE"]
# Paths that must NEVER follow HERMX_DATA_DIR (they stay under ROOT).
ROOT_ANCHORED_ATTRS = ["LOG_DIR", "STRATEGIES_DIR"]

_RECEIVER_ATTRS = MUTABLE_ATTRS + ROOT_ANCHORED_ATTRS + ["ROOT", "DATA_DIR", "HERMX_BIND_HOST"]
_DASHBOARD_ATTRS = ["ROOT", "STRATEGIES_DIR", "HERMX_BIND_HOST"]


def _resolve(module: str, attrs: list[str], env_overrides: dict[str, str], root: Path) -> dict:
    """Import ``module`` in a clean subprocess and dump the requested attributes."""
    env = dict(os.environ)
    # Drop anything that would perturb a clean import, then apply the case's env.
    for key in ("HERMX_BIND_HOST", "HERMX_DATA_DIR"):
        env.pop(key, None)
    env["HERMX_ROOT"] = str(root)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_overrides)

    code = (
        f"import json, {module} as m\n"
        f"attrs = {attrs!r}\n"
        "print(json.dumps({a: str(getattr(m, a)) for a in attrs}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"import {module} failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def state_root(tmp_path) -> Path:
    root = tmp_path / "shadow-root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


# --- HERMX_BIND_HOST ---------------------------------------------------------

def test_bind_host_defaults_to_loopback(state_root):
    """No env var -> both servers keep the historical 127.0.0.1 bind."""
    rx = _resolve("webhook_receiver", _RECEIVER_ATTRS, {}, state_root)
    db = _resolve("dashboard", _DASHBOARD_ATTRS, {}, state_root)
    assert rx["HERMX_BIND_HOST"] == "127.0.0.1"
    assert db["HERMX_BIND_HOST"] == "127.0.0.1"


def test_bind_host_env_override(state_root):
    """HERMX_BIND_HOST=0.0.0.0 (Docker bridge) is honoured by both servers."""
    env = {"HERMX_BIND_HOST": "0.0.0.0"}
    rx = _resolve("webhook_receiver", _RECEIVER_ATTRS, env, state_root)
    db = _resolve("dashboard", _DASHBOARD_ATTRS, env, state_root)
    assert rx["HERMX_BIND_HOST"] == "0.0.0.0"
    assert db["HERMX_BIND_HOST"] == "0.0.0.0"


# --- HERMX_DATA_DIR ----------------------------------------------------------

def test_data_dir_default_keeps_state_under_root(state_root):
    """Default-green: no HERMX_DATA_DIR -> DATA_DIR == ROOT and the four mutable
    snapshots stay under ROOT (byte-identical layout for host/local deploys)."""
    rx = _resolve("webhook_receiver", _RECEIVER_ATTRS, {}, state_root)
    root = rx["ROOT"]
    assert rx["DATA_DIR"] == root
    for attr in MUTABLE_ATTRS:
        assert os.path.dirname(rx[attr]) == root, f"{attr} should sit under ROOT by default"


def test_data_dir_relocates_only_mutable_files(state_root, tmp_path):
    """HERMX_DATA_DIR relocates the four mutable snapshots but NOT logs/config/strategies."""
    data_dir = tmp_path / "container-data"
    rx = _resolve(
        "webhook_receiver",
        _RECEIVER_ATTRS,
        {"HERMX_DATA_DIR": str(data_dir)},
        state_root,
    )
    root = rx["ROOT"]
    assert rx["DATA_DIR"] == str(data_dir)
    # The four mutable snapshots now live under DATA_DIR...
    for attr in MUTABLE_ATTRS:
        assert os.path.dirname(rx[attr]) == str(data_dir), f"{attr} should follow HERMX_DATA_DIR"
    # ...while logs/config/strategies remain anchored to ROOT.
    assert rx["LOG_DIR"] == os.path.join(root, "logs")
    assert os.path.dirname(rx["STRATEGIES_DIR"]) == root
    # And the relocation must create the directory at import time.
    assert data_dir.is_dir()


def test_data_dir_does_not_affect_logs_or_config(state_root, tmp_path):
    """Belt-and-braces: relocating state must not drag the ledgers/config along."""
    data_dir = tmp_path / "elsewhere"
    rx = _resolve(
        "webhook_receiver",
        _RECEIVER_ATTRS,
        {"HERMX_DATA_DIR": str(data_dir)},
        state_root,
    )
    assert str(data_dir) not in rx["LOG_DIR"]
    assert str(data_dir) not in rx["STRATEGIES_DIR"]


# --- receiver / dashboard parity --------------------------------------------

def test_receiver_and_dashboard_config_paths_resolve_identically(state_root):
    """Both services must agree on the shared config surface (strategies dir +
    ROOT), regardless of where state is relocated — they read the same mounts."""
    env = {"HERMX_DATA_DIR": str(state_root / "data")}
    rx = _resolve("webhook_receiver", _RECEIVER_ATTRS, env, state_root)
    db = _resolve("dashboard", _DASHBOARD_ATTRS, env, state_root)
    assert rx["ROOT"] == db["ROOT"]
    assert rx["STRATEGIES_DIR"] == db["STRATEGIES_DIR"]
