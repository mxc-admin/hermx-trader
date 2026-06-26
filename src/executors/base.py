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


# Canonical, venue-neutral normalized shapes for the OBSERVE-ONLY query contract
# (REFACTOR_PLAN.md:207). Reconciliation (Task 4) consumes these and must never
# see venue-specific payloads. Each adapter's query implementation returns these
# shapes; the ``raw`` field carries the untouched venue response for forensics.
#
#   order    -> {exchange, inst_id, ord_id, cl_ord_id, state, acc_fill_sz(float),
#                avg_px(float|None), ord_type, side, pos_side, ts, raw}
#   position -> {exchange, inst_id, pos(float, signed), pos_side, avg_px, upl, raw}
#   balance  -> {exchange, ccy, eq(float), avail(float), raw}
#
# A not-found order is a normalized ``{state: "not_found", raw: <error>}`` rather
# than an exception, so reconciliation can map it deterministically.
def empty_normalized_order(exchange: str | None = None, state: str = "unknown", raw=None) -> dict:
    return {
        "exchange": exchange,
        "inst_id": None,
        "ord_id": None,
        "cl_ord_id": None,
        "state": state,
        "acc_fill_sz": 0.0,
        "avg_px": None,
        "ord_type": None,
        "side": None,
        "pos_side": None,
        "ts": None,
        "raw": raw,
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

    # -- observe-only queries (read-only; used by reconciliation) -----------
    # Safe NotImplemented defaults so a venue that hasn't built a query path yet
    # degrades gracefully (empty / not_found) instead of crashing reconciliation.
    # An adapter implements these against its own venue and returns the canonical
    # normalized shapes documented above. Reading must NEVER require or arm
    # submission.
    def get_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> dict:
        """Single order status, normalized. Defaults to ``state="not_implemented"``."""
        return empty_normalized_order(
            self.key, state="not_implemented", raw={"error": "get_order_not_implemented"}
        )

    def get_open_orders(self, inst_id: str | None = None) -> list:
        """Live (pending) orders, normalized. Defaults to empty."""
        return []

    def get_order_history_archive(self, inst_id: str | None = None, limit: int = 100) -> list:
        """Aged-out order history, normalized. Defaults to empty."""
        return []

    def get_positions(self, inst_id: str | None = None) -> list:
        """Open positions, normalized. Defaults to empty."""
        return []

    def get_balance(self, ccy: str | None = None) -> list:
        """Account balance per currency, normalized. Defaults to empty."""
        return []

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
