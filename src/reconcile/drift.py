"""Observe-only journal-vs-venue position drift detection (REFACTOR_PLAN.md Phase 5).

emit_reconcile_alert is monkeypatched directly on wr by tests (e.g.
test_drift_emits_reconcile_mismatch, test_multiple_drifts_emit_one_alert_each,
test_drift_alert_threads_actual_venue_and_mode) and expects reconcile_position_drift
to observe the patch, so it is dereferenced through `_wr.` at call time rather than
imported directly -- mirroring the _reconciliation_executor pattern in
reconcile.executor_select. RECONCILE_ALERT_MISMATCH is a plain constant (not
monkeypatched) and is imported directly.
"""
from __future__ import annotations

import logging

from reconcile.alerts import RECONCILE_ALERT_MISMATCH


def reconcile_position_drift(executor, journal_positions: dict, venue: str, mode: str) -> list:
    """OBSERVE-ONLY (B1): detect journal-vs-venue position drift and alert. NEVER
    auto-corrects, cancels, or submits.

    Delegates detection to the adapter's pure ``detect_position_drift`` (which reads
    ``executor.get_positions()`` and degrades to ``[]`` on any venue error), then logs
    each drift as a WARNING and emits a ``RECONCILE_MISMATCH`` (type=position_drift).
    Returns the drift list (also useful for tests / the dashboard snapshot)."""
    import webhook_receiver as _wr
    from executors.ccxt_adapter import detect_position_drift
    drifts = detect_position_drift(executor, journal_positions, venue, mode)
    for d in drifts:
        logging.warning(
            "position_drift inst_id=%s journal_qty=%s venue_qty=%s drift=%s venue=%s mode=%s",
            d.get("inst_id"), d.get("journal_qty"), d.get("venue_qty"), d.get("drift"), venue, mode,
        )
        _wr.emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {
            "stage": "position_drift",
            "type": "position_drift",
            "inst_id": d.get("inst_id"),
            "journal_qty": d.get("journal_qty"),
            "venue_qty": d.get("venue_qty"),
            "drift": d.get("drift"),
            "venue": venue,
            "mode": mode,
        })
    return drifts
