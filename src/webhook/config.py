"""Leaf config module: engine + advisor config and execution-backend defaults.
MUST NOT import webhook_receiver (no cycle). Pure functions + constants only."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path

# Adapter backend is hardcoded to the sole CCXT backend; env overrides for tests/ops.
EXEC_BACKEND = (os.environ.get("HERMX_EXEC_BACKEND") or "ccxt").strip().lower()

# Inline defaults for the residual execution keys (route/account are audit-only
# labels; td_mode already inline-defaults at each readiness builder).
EXECUTION_DEFAULTS = {
    "exchange": EXEC_BACKEND,        # adapter selector — always ccxt
    "ccxt_exchange": "okx",          # default venue; overridden by instrument.exchange
    "ccxt_default_type": "swap",     # overridden by instrument.type (see resolve_default_type)
    "route": "okx_api",              # audit label only
    "account": "sandbox",            # audit label only
}

ADVISOR_DEFAULTS = {
    "enabled": False,
    "command": "hermes",
    "skills": "hermx-control",
    "model": "",
    "timeout_seconds": 30.0,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def load_engine_config(path: Path) -> dict:
    """strategy_engine + advisor only. No risk/assets/policies/fees/execution block."""
    default = {
        "strategy_engine": {
            "enabled": True,
            "strategies_dir": "strategies",
            "default_status": "trial_candidate",
            "allow_strategy_alerts": True,
            "require_strategy_id": True,
            "quarantine_invalid_strategy_alerts": True,
            "enforce_alert_schema": False,
        },
        "advisor": dict(ADVISOR_DEFAULTS),
    }
    if not path.exists():
        return default
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return {
            "strategy_engine": default["strategy_engine"] | (loaded.get("strategy_engine") or {}),
            "advisor": dict(ADVISOR_DEFAULTS) | (loaded.get("advisor") or {}),
        }
    except Exception as exc:
        logging.warning("Failed to load engine config: %s", exc)
        return default


def resolve_default_type(instrument: dict | None, fallback: str = "swap") -> str:
    """instrument.type ('swap'|'spot'|'future') → CCXT defaultType; wire it in."""
    return str((instrument or {}).get("type") or fallback).lower()
