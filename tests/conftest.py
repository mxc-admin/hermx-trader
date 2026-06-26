"""Shared pytest fixtures for the HermX test harness (Phase 0 setup).

Safety-critical invariant: tests must NEVER read or write the real runtime
logs/state. ``webhook_receiver`` resolves ``SHADOW_ROOT`` at *import* time, so we
redirect it to an isolated temp directory BEFORE the module is ever imported.
"""
from __future__ import annotations

import os
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

# Ensure the kill switch starts from its inert default for every test process,
# unless an individual test opts in via monkeypatch.
os.environ.pop("HERMX_SUBMIT_ENABLED", None)

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
