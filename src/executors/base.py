#!/usr/bin/env python3
"""Base executor interface for the exchange-agnostic execution layer.

Every exchange adapter (OKX demo, KuCoin paper, Bybit testnet, ...) implements
this interface so that ``webhook_receiver`` never has to know which venue it is
talking to. The receiver builds a venue-neutral ``execution_readiness`` block and
hands it to whatever executor the active config selected; the executor turns that
intent into real (or simulated) orders and returns a *normalized* result.

Design notes
------------
* Composition over inheritance: adapters wrap whatever client/SDK/subprocess the
  venue needs. ``BaseExecutor`` only fixes the *contract*, not the mechanism.
* The normalized result (see ``ExecutionResult``/``empty_fill_summary``) keeps the
  receiver and dashboards decoupled from venue-specific payload shapes.
"""
from __future__ import annotations

import abc
from pathlib import Path


# Canonical, venue-neutral fill summary. Every adapter must populate this shape
# (missing values stay ``None``) so dashboards can render any exchange the same
# way. Adapters may additionally pass their raw response through ``payload``.
def empty_fill_summary(client_order_id: str | None = None) -> dict:
    return {
        "status": None,            # e.g. "filled", "submitted", "rejected", "dry_run"
        "order_id": None,
        "client_order_id": client_order_id,
        "avg_fill_price": None,
        "filled_size": None,
        "fee_usd": None,
        "slippage_pct": None,
        "position_after_order": None,
    }


class BaseExecutor(abc.ABC):
    """Contract that all exchange executors follow.

    Subclasses are constructed with the loaded shadow ``config`` and the project
    ``root`` so they can locate venue scripts/credentials. They must implement
    :meth:`execute`; :meth:`health` and :meth:`plan` have safe defaults.
    """

    #: Stable key used by the factory/config, e.g. "okx_demo", "kucoin_paper".
    key: str = "base"

    def __init__(self, config: dict, root: Path) -> None:
        self.config = config or {}
        self.root = Path(root)
        self.execution_cfg = (self.config.get("execution") or {})

    # -- required ----------------------------------------------------------
    @abc.abstractmethod
    def execute(self, readiness: dict) -> dict:
        """Submit (or dry-run) orders for one ``execution_readiness`` block.

        Must return a normalized result dict:

            {
                "ok": bool,
                "mode": str,              # submit_enabled / dry_run_no_order / ...
                "exchange": self.key,
                "elapsed_ms": int | None,
                "fill_summary": <empty_fill_summary shape>,
                "payload": dict | None,   # raw venue response, for debugging/UI
            }
        """
        raise NotImplementedError

    # -- optional ----------------------------------------------------------
    def health(self) -> dict:
        """Return an account/connectivity snapshot. Adapters override as needed."""
        return {"ok": False, "exchange": self.key, "error": "health_not_implemented"}

    def plan(self, readiness: dict) -> dict:
        """Return order intents without submitting. Defaults to a no-op preview."""
        return {"exchange": self.key, "mode": "plan_not_implemented", "orders": []}

    # -- helpers -----------------------------------------------------------
    def normalized_result(
        self,
        *,
        ok: bool,
        mode: str,
        fill_summary: dict | None = None,
        payload: dict | None = None,
        elapsed_ms: int | None = None,
        **extra,
    ) -> dict:
        """Build the normalized result envelope shared by every adapter."""
        result = {
            "ok": ok,
            "mode": mode,
            "exchange": self.key,
            "elapsed_ms": elapsed_ms,
            "fill_summary": fill_summary or empty_fill_summary(),
            "payload": payload,
        }
        result.update(extra)
        return result
