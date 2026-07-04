"""Read-only order-journal troubleshooting: classify open UNKNOWN orders against known
corruption/ambiguity patterns and offer only pre-vetted, server-self-validated actions.

Never accepts a caller-supplied target state -- every action is re-derived and
re-validated from the journal's own history at apply time (see webhook_receiver.py's
/api/admin/order-journal/apply-action). Classification never issues a live venue call;
it reads only what the journal/resolver has already recorded, so it cannot race or
duplicate the periodic resolve_unknown_orders_once() reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from orders.journal import ORDER_STATE_UNKNOWN, ORDER_TERMINAL_STATES, order_history_for


@dataclass(frozen=True)
class SafeAction:
    id: str
    label: str


@dataclass(frozen=True)
class TroubleshootResult:
    cl_ord_id: str
    issue_type: str
    evidence: dict
    actions: list = field(default_factory=list)


def classify_terminal_overwritten(records: list, history_complete: bool) -> "TroubleshootResult | None":
    """A terminal record (FILLED/REJECTED) exists somewhere in history but is NOT the
    last record -- per _ORDER_STATE_TRANSITIONS a terminal state can never legally be
    followed by anything, so this is unconditional proof of corruption regardless of
    history_complete (an earlier terminal record is still proof even if history before
    it is incomplete)."""
    if not records:
        return None
    terminal_idx = next((i for i, r in enumerate(records) if r.get("state") in ORDER_TERMINAL_STATES), None)
    if terminal_idx is None or terminal_idx == len(records) - 1:
        return None
    terminal_rec = records[terminal_idx]
    return TroubleshootResult(
        cl_ord_id=terminal_rec.get("cl_ord_id"),
        issue_type="terminal_overwritten",
        evidence={
            "terminal_state": terminal_rec.get("state"),
            "terminal_seq": terminal_rec.get("seq"),
            "terminal_ts": terminal_rec.get("ts"),
            "overwritten_by_state": records[-1].get("state"),
            "overwritten_by_seq": records[-1].get("seq"),
        },
        actions=[SafeAction(id="restore_terminal", label=f"Restore to {terminal_rec.get('state')}")],
    )


def classify_ambiguous_unknown(records: list, history_complete: bool) -> "TroubleshootResult | None":
    """Latest state is UNKNOWN with no terminal record anywhere in what we can see.
    Only reachable when classify_terminal_overwritten did not already match. Requires
    history_complete=True to conclude genuine ambiguity -- if earlier records were
    pruned by segment retention, a hidden terminal record cannot be ruled out, so this
    reports evidence_incomplete instead and offers NO action either way (Case 2 is never
    auto-actionable -- see the resolver's not-found-is-not-rejected invariant)."""
    if not records or records[-1].get("state") != ORDER_STATE_UNKNOWN:
        return None
    if any(r.get("state") in ORDER_TERMINAL_STATES for r in records):
        return None
    if not history_complete:
        return TroubleshootResult(
            cl_ord_id=records[-1].get("cl_ord_id"),
            issue_type="evidence_incomplete",
            evidence={"reason": "older journal records pruned by segment retention -- cannot rule out a hidden terminal state"},
            actions=[],
        )
    return TroubleshootResult(
        cl_ord_id=records[-1].get("cl_ord_id"),
        issue_type="ambiguous_unknown",
        evidence={"reason": "never confirmed terminal by the venue -- requires manual investigation (check OKX order history + current position/balance) before any write"},
        actions=[],
    )


CLASSIFIERS = [classify_terminal_overwritten, classify_ambiguous_unknown]


def run_classifiers(cl_ord_id: str) -> "TroubleshootResult | None":
    history = order_history_for(cl_ord_id)
    records, history_complete = history["records"], history["history_complete"]
    for classifier in CLASSIFIERS:
        result = classifier(records, history_complete)
        if result is not None:
            return result
    return None


def troubleshoot_all_open_orders() -> list:
    """Scan every open order for known confusing/corrupted patterns. Scoped to
    ORDER_STATE_UNKNOWN only: PLANNED orphans are already self-healed by
    _resolve_planned_orphan, and a routine SUBMITTED order not yet timed out is being
    actively retried by the resolver every tick -- neither is "confusing" and surfacing
    them would just be noise."""
    import webhook_receiver as _wr
    results = []
    for rec in _wr.load_open_orders():
        if rec.get("state") != ORDER_STATE_UNKNOWN:
            continue
        result = run_classifiers(rec.get("cl_ord_id"))
        if result is not None:
            results.append(result)
    return results
