#!/usr/bin/env python3
"""Executor factory: select the right exchange adapter from config.

The active venue is chosen by ``config["execution"]["exchange"]``. Post P5-06/P5-07
CCXT cutover, ``ccxt`` is the ONLY execution backend; the legacy okx_demo CLI
adapter was removed. The factory keeps a registry of known executor classes so
adding a new exchange is a one-line registration — no changes to
``webhook_receiver`` are required.
"""
from __future__ import annotations

from pathlib import Path

from .base import BaseExecutor

try:
    from .ccxt_adapter import CcxtExecutor
except Exception:  # optional dependency or import-time guard
    CcxtExecutor = None


class ExecutorFactory:
    # Registry of exchange key -> executor class. Populated below + extensible
    # at runtime via ``register``.
    _registry: dict[str, type[BaseExecutor]] = {}

    # Backward-compatibility aliases. After the CCXT cutover every legacy OKX
    # exchange key (including "okx_demo" itself) routes to the CCXT adapter so any
    # existing config or alias keeps working unchanged on the single backend.
    _aliases: dict[str, str] = {
        "okx": "ccxt",
        "okx_api": "ccxt",
        "okx_sandbox": "ccxt",
        "okx_demo": "ccxt",
        "okx_ccxt": "ccxt",
    }

    @classmethod
    def register(cls, key: str, executor_cls: type[BaseExecutor]) -> None:
        """Register (or override) an executor class for an exchange key."""
        cls._registry[key] = executor_cls

    @classmethod
    def alias(cls, alias_key: str, canonical_key: str) -> None:
        """Register a backward-compat alias for an existing exchange key."""
        cls._aliases[alias_key] = canonical_key

    @classmethod
    def resolve_key(cls, exchange: str | None) -> str:
        key = str(exchange or "ccxt").strip().lower()
        return cls._aliases.get(key, key)

    @classmethod
    def available(cls) -> list[str]:
        return sorted(cls._registry.keys())

    @classmethod
    def create(cls, config: dict, root: Path) -> BaseExecutor:
        """Instantiate the executor selected by ``config['execution']['exchange']``."""
        exchange = ((config or {}).get("execution") or {}).get("exchange")
        key = cls.resolve_key(exchange)
        executor_cls = cls._registry.get(key)
        if executor_cls is None:
            raise ValueError(
                f"Unknown execution exchange '{exchange}' (resolved '{key}'). "
                f"Available: {cls.available()}"
            )
        return executor_cls(config, root)


# Register the built-in adapters. New venues are added here (one line each).
# CcxtExecutor is the sole backend; if its optional ``ccxt`` import failed the
# registry is empty and ExecutorFactory.available() == [] so the receiver fails
# closed (never submits) rather than guessing a venue.
if CcxtExecutor is not None:
    ExecutorFactory.register(CcxtExecutor.key, CcxtExecutor)
