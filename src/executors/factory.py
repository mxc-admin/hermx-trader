#!/usr/bin/env python3
"""Executor factory: select the right exchange adapter from config.

The active venue is chosen by ``config["execution"]["exchange"]`` (e.g.
"okx_demo", "kucoin_paper", "bybit_testnet"). The factory keeps a registry of
known executor classes so adding a new exchange is a one-line registration — no
changes to ``webhook_receiver`` are required.
"""
from __future__ import annotations

from pathlib import Path

from .base import BaseExecutor
from .okx_demo import OkxDemoExecutor
from .kucoin_paper import KuCoinPaperExecutor


class ExecutorFactory:
    # Registry of exchange key -> executor class. Populated below + extensible
    # at runtime via ``register``.
    _registry: dict[str, type[BaseExecutor]] = {}

    # Backward-compatibility aliases. Older configs used "okx"; map it to the
    # demo adapter so existing deployments keep working unchanged.
    _aliases: dict[str, str] = {
        "okx": "okx_demo",
        "okx_api": "okx_demo",
        "okx_sandbox": "okx_demo",
        "kucoin": "kucoin_paper",
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
        key = str(exchange or "okx_demo").strip().lower()
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
ExecutorFactory.register(OkxDemoExecutor.key, OkxDemoExecutor)
ExecutorFactory.register(KuCoinPaperExecutor.key, KuCoinPaperExecutor)
