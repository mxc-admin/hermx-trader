"""Reconcile-kind alert emission (REFACTOR_PLAN.md Phase 5).

Houses emit_reconcile_alert plus the reconcile alert-kind string constants.
ALERTS_LEDGER is root-bound and reloaded per-test, so it stays defined in
webhook_receiver.py and is read lazily via `import webhook_receiver as _wr` --
matching the src/alerts.py (Phase 0) pattern. append_jsonl and emit_operator_alert
are ALSO monkeypatched directly on wr by tests (e.g.
test_emit_reconcile_alert_ledger_oserror_still_notifies_operator patches both
wr.append_jsonl and wr.emit_operator_alert and expects emit_reconcile_alert to
observe them), so both are dereferenced through `_wr.` at call time rather than
imported directly.
"""
from __future__ import annotations

from webhook.timeutil import now_iso

# Money-safety context (unchanged from webhook_receiver.py): a paused symbol hard-
# blocks submission (symbol_pause_info gate in ExecutionService.execute); these are
# the reconcile-kind alert identifiers emitted alongside that lifecycle.
RECONCILE_ALERT_MISMATCH = "RECONCILE_MISMATCH"
RECONCILE_ALERT_RESOLVER_TIMEOUT = "UNKNOWN_RESOLVER_TIMEOUT"
# A PLANNED order that crashed before submission (never advanced to SUBMITTED) was, by
# write-ahead ordering, NEVER sent to the venue -- the resolver rejects it never_submitted.
RECONCILE_ALERT_PLANNED_ABANDONED = "PLANNED_ORDER_ABANDONED"
# Anomaly: a PLANNED orphan that the venue unexpectedly DOES know about (should not
# happen given write-ahead) -- promoted to SUBMITTED for normal reconciliation, alerted.
RECONCILE_ALERT_PLANNED_ON_VENUE = "PLANNED_ORDER_ON_VENUE"


def emit_reconcile_alert(kind: str, detail: dict) -> dict:
    """Reconcile alert row in the unified ledger (kind="reconcile") + Task 6 operator
    transport (emit_operator_alert writes a paired kind="operator" row)."""
    import logging
    import webhook_receiver as _wr
    record = {"ts": now_iso(), "kind": "reconcile", "alert": kind, "detail": detail or {}}
    try:
        _wr.append_jsonl(_wr.ALERTS_LEDGER, record)
    except OSError as exc:
        logging.error("failed to write reconcile alert %s: %s", kind, exc)
    _wr.emit_operator_alert(kind, detail or {}, severity="warning")
    return record
