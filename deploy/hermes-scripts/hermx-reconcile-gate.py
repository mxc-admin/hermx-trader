#!/usr/bin/env python3
"""HermX reconcile-watch pre-check gate (Hermes cron, every 5m).

Derives four condition families and compares their fingerprints against the sidecar
``~/.hermes/scripts/.hermx-reconcile.state`` (per ``docs/MONITOR_DAEMON_SPEC.md`` §4.2):

- **alert rows** — ``logs/alerts.jsonl`` rows (tolerant, skips a torn tail) with
  ``kind in {reconcile, state, operator}`` and severity >= warning, within the lookback;
- **stuck orders** — dashboard ``/api`` open-orders rows with ``state == "UNKNOWN"``;
- **ledger mismatch** — a strategy's most recent close-implying intake signal FILLED at
  the venue but no matching ``closed-trades.jsonl`` row landed within the grace window
  (``HERMX_LEDGER_MISMATCH_GRACE_SECONDS``, default 1800s = the unknown-resolver order
  timeout + one ledger-reconcile cron tick + buffer);
- **rejected orders** — order-journal rows that reached terminal ``REJECTED`` within the
  lookback (a rejected close/flip leg leaves the position open; nothing else alerts on it).

All reads are HTTP (``/api``) or tolerant ``.jsonl`` file scans — no ``src/`` imports.
Every missing/corrupt input fails open to no condition. Emits ``{"wakeAgent": false}``
when nothing is fresh (suppression window 1800s, escalation bypass), else
``{"wakeAgent": true, "context": {"alerts": [...]}}``.

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

# Ledger-mismatch grace: how long after a close-implying signal a matching
# closed-trades.jsonl row may lawfully still be missing. Bounded by the system's own
# order-resolution ceiling — UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS (900s) plus one
# hermx-ledger-reconcile cron tick (600s) plus buffer — NOT by market activity.
LEDGER_MISMATCH_GRACE_SECONDS = float(
    os.environ.get("HERMX_LEDGER_MISMATCH_GRACE_SECONDS", "1800")
)


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


def _last_close_signals(ops, repo):
    """Per strategy: the most recent close-implying intake signal in
    ``logs/raw-webhooks.jsonl`` as ``{strategy_id: (epoch, received_at)}``.

    Close-implying = ``action == "close"``, or a buy/sell whose side flips the
    strategy's running side (per strategy/readiness.py every signal implies
    ``CLOSE_OPPOSITE_IF_ANY`` first, so a flip closes the prior position). Rows are
    processed in received_at order; a close resets the running side (the next signal
    is an open, not a flip). Fail-open: a missing WAL / no parseable rows -> {}."""
    path = os.path.join(repo, "logs", "raw-webhooks.jsonl")
    rows = []
    for row in ops._iter_jsonl(path):
        if not isinstance(row, dict) or row.get("phase") != "intake":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        sid = str(payload.get("strategy_id") or "").strip()
        if not sid:
            continue
        received_at = row.get("received_at")
        epoch = g.row_epoch(received_at)
        if epoch is None:
            continue
        action = str(payload.get("action") or "").lower().strip()
        side = str(payload.get("side") or "").lower().strip()
        rows.append((epoch, sid, action, side, received_at))
    rows.sort(key=lambda t: t[0])
    running = {}
    last_close = {}
    for epoch, sid, action, side, received_at in rows:
        if action == "close":
            last_close[sid] = (epoch, received_at)
            running[sid] = None  # flat after a close: the next signal opens
            continue
        eff = action if action in ("buy", "sell") else (side if side in ("buy", "sell") else "")
        if not eff:
            continue
        if running.get(sid) and eff != running[sid]:
            last_close[sid] = (epoch, received_at)  # flip: closes the opposite first
        running[sid] = eff
    return last_close


def _latest_journal_states(ops, repo):
    """Latest order-journal record per cl_ord_id from the LIVE segment
    (``logs/order-journal.jsonl``), highest seq wins. Records rotated into sealed
    segments are not seen — rotation triggers at 1000 records, far beyond what a
    30–60 min window produces at HermX signal cadence, and a missed record degrades
    to no alert (fail-open), never a false one. Missing/corrupt journal -> {}."""
    path = os.path.join(repo, "logs", "order-journal.jsonl")
    latest = {}
    for row in ops._iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        cl = row.get("cl_ord_id")
        if not cl:
            continue
        seq = row.get("seq") if isinstance(row.get("seq"), int) else -1
        cur = latest.get(cl)
        cur_seq = cur.get("seq") if cur is not None and isinstance(cur.get("seq"), int) else -1
        if cur is None or seq >= cur_seq:
            latest[cl] = row
    return latest


def ledger_mismatch_conditions(ops, repo, now_epoch):
    """A strategy's most recent close-implying signal FILLED at the venue but no
    matching ``closed-trades.jsonl`` row landed past the grace window -> one condition
    per strategy. Correlation is file-only: signal -> cl_ord_ids via the submit-time
    map ``cl-ord-strategy-map.jsonl`` (``ts_ms`` within [signal, signal+grace]),
    cl_ord_id -> terminal state via the live order journal, cl_ord_id/strategy ->
    ledger row via ``closed-trades.jsonl``. A REJECTED-only signal is skipped
    (rejected_order_conditions owns it); no terminal state yet is skipped (still
    resolving, or already surfaced as a stuck order). Fail-open on every missing or
    corrupt read -> []."""
    last_close = _last_close_signals(ops, repo)
    due = {sid: at for sid, at in last_close.items()
           if (now_epoch - at[0]) > LEDGER_MISMATCH_GRACE_SECONDS}
    if not due:
        return []
    map_rows = [r for r in ops._iter_jsonl(os.path.join(repo, "cl-ord-strategy-map.jsonl"))
                if isinstance(r, dict)]
    if not map_rows:
        return []
    states = _latest_journal_states(ops, repo)
    trades = [r for r in ops._iter_jsonl(os.path.join(repo, "closed-trades.jsonl"))
              if isinstance(r, dict)]
    conds = []
    for sid, (epoch, received_at) in sorted(due.items()):
        lo_ms = int(epoch * 1000)
        hi_ms = int((epoch + LEDGER_MISMATCH_GRACE_SECONDS) * 1000)
        candidates = set()
        for r in map_rows:
            if str(r.get("strategy_id") or "") != sid or not r.get("cl_ord_id"):
                continue
            try:
                ts_ms = int(r.get("ts_ms"))
            except (TypeError, ValueError):
                continue
            if lo_ms <= ts_ms <= hi_ms:
                candidates.add(str(r["cl_ord_id"]))
        if not candidates:
            continue  # nothing submitted for the signal -> not this gate's call
        filled = sorted(
            cl for cl in candidates
            if str((states.get(cl) or {}).get("state") or "").upper() == "FILLED"
        )
        if not filled:
            continue  # REJECTED (rejected_order_conditions) or still resolving
        ledgered = any(
            str(r.get("cl_ord_id") or "") in candidates
            or (str(r.get("strategy_id") or "") == sid
                and isinstance(r.get("recorded_at_ms"), (int, float))
                and r["recorded_at_ms"] >= lo_ms)
            for r in trades
        )
        if ledgered:
            continue
        conds.append({
            "fingerprint": f"reconcile:ledger_mismatch:{sid}",
            "severity": "warning",
            "category": "reconcile",
            "title": f"ledger mismatch: {sid} closed at venue, missing from ledger",
            "detail": {"strategy_id": sid, "signal_received_at": received_at,
                       "cl_ord_id": filled[0],
                       "grace_seconds": LEDGER_MISMATCH_GRACE_SECONDS},
        })
    return conds


def rejected_order_conditions(ops, repo, now_epoch):
    """Order-journal rows that reached terminal ``REJECTED`` within the lookback ->
    conditions. Every strategy signal implies CLOSE_OPPOSITE_IF_ANY, so a REJECTED
    order while a position is open means the position may still be open with nothing
    else alerting on it (the unknown-resolver journals a REJECTED resolution
    silently). Fail-open: a missing or corrupt journal -> []."""
    path = os.path.join(repo, "logs", "order-journal.jsonl")
    conds = []
    for row in ops._iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        if str(row.get("state") or "").upper() != "REJECTED":
            continue
        # Recency bound: drop rows older than the lookback (undated rows kept).
        epoch = g.row_epoch(row.get("ts"))
        if epoch is not None and (now_epoch - epoch) > LOOKBACK_SECONDS:
            continue
        cl = row.get("cl_ord_id")
        intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
        conds.append({
            "fingerprint": f"reconcile:rejected_order:{cl}",
            "severity": "warning",
            "category": "reconcile",
            "title": f"order rejected: {cl} -- position may still be open",
            "detail": {"cl_ord_id": cl, "symbol": intent.get("symbol"),
                       "side": intent.get("side"), "prev_state": row.get("prev_state")},
        })
    return conds


def collect(ops, repo, now_epoch):
    return (alert_conditions(ops, repo, now_epoch)
            + stuck_order_conditions(ops)
            + ledger_mismatch_conditions(ops, repo, now_epoch)
            + rejected_order_conditions(ops, repo, now_epoch))


def main():
    ops = g.import_hermx_ops()
    now = time.time()
    conds = collect(ops, g.repo_root(), now)
    g.run_gate("reconcile", conds, now)


if __name__ == "__main__":
    main()
