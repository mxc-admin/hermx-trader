#!/usr/bin/env python3
"""KuCoin futures paper-trading executor adapter.

Wraps the in-process ``KuCoinPaperExecutor`` (kucoin_paper_executor.py) behind the
venue-neutral :class:`BaseExecutor` interface. Unlike the OKX adapter this venue
runs in-process via the kucoin SDK, so we import lazily to avoid forcing the
dependency on deployments that only use OKX.
"""
from __future__ import annotations

import time

from .base import BaseExecutor, empty_fill_summary


class KuCoinPaperExecutor(BaseExecutor):
    key = "kucoin_paper"

    def _build_instruction(self, readiness: dict) -> dict:
        """Map a venue-neutral readiness block to the KuCoin executor instruction."""
        intent = readiness.get("execution_intent") or {}
        target_direction = str(intent.get("target_direction") or readiness.get("signal_side") or "").lower()
        # The KuCoin executor wants a buy/sell side and a notional in USD.
        side = "buy" if target_direction in {"long", "buy"} else "sell"
        return {
            # Prefer the generic field; fall back to the legacy OKX-style id.
            "symbol": readiness.get("symbol"),
            "inst_id": readiness.get("inst_id") or readiness.get("okx_inst_id"),
            "okx_inst_id": readiness.get("okx_inst_id") or readiness.get("inst_id"),
            "target_side": side,
            "target_notional_usd": intent.get("planned_notional_usd") or intent.get("base_notional_usd") or 0.0,
            "client_order_id": intent.get("client_order_id"),
        }

    def execute(self, readiness: dict) -> dict:
        client_order_id = (readiness.get("execution_intent") or {}).get("client_order_id")
        # Lazy import keeps the kucoin SDK optional for OKX-only deployments.
        try:
            from kucoin_paper_executor import KuCoinPaperExecutor as _KuCoinClient
        except Exception as exc:
            return self.normalized_result(
                ok=False,
                mode="executor_unavailable",
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": f"kucoin executor import failed: {exc}"},
            )
        started = time.time()
        try:
            raw = _KuCoinClient().execute(self._build_instruction(readiness))
            elapsed_ms = round((time.time() - started) * 1000)
            ok = bool(raw.get("success"))
            fill = empty_fill_summary(client_order_id)
            fill["status"] = "filled" if ok else "rejected"
            fill["position_after_order"] = raw.get("open") or raw.get("close")
            return self.normalized_result(
                ok=ok,
                mode="submit_enabled" if ok else "submit_failed",
                elapsed_ms=elapsed_ms,
                fill_summary=fill,
                payload=raw,
            )
        except Exception as exc:
            return self.normalized_result(
                ok=False,
                mode="submit_exception",
                elapsed_ms=round((time.time() - started) * 1000),
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": str(exc)},
            )
