#!/usr/bin/env python3
"""HermX P&L-ledger reconcile safety net (Hermes cron, non-LLM).

Phase 3 "Also": a fixed-cadence trigger that folds every active strategy's recent
exchange order history into the durable closed-trade ledger (``closed-trades.jsonl``)
so a HermX close is captured even when no operator is looking at the dashboard —
closing the "close ages out of the 100-row window before anyone reads it" race
(Risk Register: History-window race).

It does NOT re-implement the reconcile. The dashboard's ``dashboard_model()`` already
iterates the DISTINCT ``(venue, mode)`` pairs across the strategy set (Phase 0.5) and
calls ``pnl_ledger.reconcile_from_order_history(rows, venue, mode)`` for each — venue
and mode correct, never the OKX-demo literal (#20 / #20a). So this job simply GETs the
dashboard ``/api`` route: the GET serves the cached model and stamps viewer presence,
and the dashboard's background loop then rebuilds the model (driving that reconcile)
within one active tick (~15s) while the stamp is fresh — else within the idle rebuild
interval. Reusing the shipped, tested feed avoids duplicating executor/credential
handling in a cron and keeps the (venue, mode) resolution in exactly one place.

This is the LEDGER reconcile — distinct from, and NOT gated by, the receiver's
``HERMX_RECONCILE_ENABLED`` post-submit reconcile (Flag Dependencies). It is a non-LLM
job: it never wakes an agent, so no ``--provider``/``--model`` pin is required (per the
monitor-pivot design). READ-ONLY w.r.t. HermX state; the only write is the append-only
ledger, performed inside the dashboard model build.

Exit 0 on success (or a benign unreachable dashboard — the health watchdog owns
reachability); non-zero only on an unexpected internal error.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402


def main() -> int:
    ops = g.import_hermx_ops()
    secret = ops._load_secret()
    # GET /api stamps presence; the dashboard's refresh loop rebuilds within one
    # active tick (~15s) while the stamp is fresh, reconciling every distinct
    # (venue, mode) account's order history into the ledger. The GET itself only
    # serves the cached model — harmless for a periodic cron.
    api, err = ops._get_json(ops.DASHBOARD_BASE, "/api", secret=secret)
    if not isinstance(api, dict):
        # Fail-open: an unreachable/again-later dashboard is the health watchdog's
        # concern, never a false trading-state signal here.
        print(json.dumps({"ok": False, "reconciled": False, "reason": err or "no_api"}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
