#!/usr/bin/env python3
"""HermX reconcile-watch pre-check gate (Hermes cron, every 5m).

Reads ``logs/alerts.jsonl`` (tolerant, skips a torn tail) and the dashboard ``/api``
open-orders view, computes reconcile fingerprints per ``docs/MONITOR_DAEMON_SPEC.md``
§4.2, and compares them against the sidecar ``~/.hermes/scripts/.hermx-reconcile.state``.
Emits ``{"wakeAgent": false}`` when nothing is fresh (suppression window 1800s,
escalation bypass), else ``{"wakeAgent": true, "context": {"alerts": [...]}}``.

READ-ONLY: never mutates HermX state. The only write is the reconcile sidecar.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402

# Rows this old (or older) are ignored on read, so a first run after a long history
# does not surface stale historical alerts. Bounded by the reconcile window by default.
LOOKBACK_SECONDS = float(
    os.environ.get("HERMX_RECONCILE_LOOKBACK_SECONDS", str(g.WINDOW["reconcile"]))
)
# alerts.jsonl rows we consider (reconcile/state ledger kinds + operator watchdog rows).
_RECONCILE_KINDS = {"reconcile", "state", "operator"}


def alert_conditions(ops, repo, now_epoch):
    """Reconcile/state/operator rows in ``logs/alerts.jsonl`` with severity >= warning
    and a fresh-enough ``ts`` → reconcile conditions."""
    path = os.path.join(repo, "logs", "alerts.jsonl")
    conds = []
    for row in ops._iter_jsonl(path, limit=200):
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "") not in _RECONCILE_KINDS:
            continue
        severity = str(row.get("severity") or "info")
        if g.severity_rank(severity) < g.severity_rank("warning"):
            continue
        # Recency bound: drop rows older than the lookback (torn/undated rows kept).
        epoch = g.row_epoch(row.get("ts"))
        if epoch is not None and (now_epoch - epoch) > LOOKBACK_SECONDS:
            continue
        alert = str(row.get("alert") or "UNKNOWN")
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        parts = ["reconcile", alert]
        symbol = detail.get("symbol")
        cl = detail.get("cl_ord_id")
        if symbol:
            parts.append(str(symbol))
        if cl:
            parts.append(str(cl))
        conds.append({
            "fingerprint": ":".join(parts),
            "severity": severity,
            "category": "reconcile",
            "title": alert,
            "detail": detail,
        })
    return conds


def stuck_order_conditions(ops):
    """Open orders in an UNKNOWN state on ``/api`` → stuck-order conditions.
    An unreachable dashboard yields nothing (fail-open — the health watchdog owns
    reachability); never a false all-clear on trading state."""
    secret = os.environ.get("HERMX_SECRET")
    api, _err = ops._get_json(ops.DASHBOARD_BASE, "/api", secret=secret)
    conds = []
    if not isinstance(api, dict):
        return conds
    rows = ((api.get("open_orders") or {}).get("rows")) or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("state") or "").upper() != "UNKNOWN":
            continue
        cl = row.get("cl_ord_id")
        conds.append({
            "fingerprint": f"reconcile:stuck_order:{cl}",
            "severity": "warning",
            "category": "reconcile",
            "title": f"stuck order {cl} -- run /hx-troubleshoot",
            "detail": {"cl_ord_id": cl, "symbol": row.get("symbol"), "state": "UNKNOWN",
                       "hint": "investigate via /hx-troubleshoot"},
        })
    return conds


def collect(ops, repo, now_epoch):
    return alert_conditions(ops, repo, now_epoch) + stuck_order_conditions(ops)


def main():
    ops = g.import_hermx_ops()
    now = time.time()
    conds = collect(ops, g.repo_root(), now)
    g.run_gate("reconcile", conds, now)


if __name__ == "__main__":
    main()
