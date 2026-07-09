"""Tests for src/orders/journal.py::order_history_for and src/orders/troubleshoot.py.

Covers:
  (a) order_history_for -- full seq-ordered history + the self-contained
      history_complete flag (True iff the earliest visible record has prev_state=None).
  (b) classify_terminal_overwritten -- the one provable, write-capable classifier: a
      terminal record exists but is not the last record in history.
  (c) classify_ambiguous_unknown -- genuinely-ambiguous UNKNOWN (no terminal ever seen),
      gated on history_complete; incomplete history reports evidence_incomplete instead
      and offers no action either way.
  (d) troubleshoot_all_open_orders -- integration: only a corrupted UNKNOWN order is
      surfaced; a normal PLANNED/SUBMITTED order is not.
"""
from __future__ import annotations

from orders.troubleshoot import (
    SafeAction,
    classify_ambiguous_unknown,
    classify_terminal_overwritten,
    run_classifiers,
    troubleshoot_all_open_orders,
)

import webhook_receiver as wr  # noqa: F401  (imported for type/reference parity with sibling test files)


_INTENT = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}


def _seed_corrupted_unknown(wr, cl: str) -> None:
    """Simulate PRE-FIX corruption: REJECTED written legitimately, then an illegal
    UNKNOWN appended DIRECTLY (bypassing record_order_state's stale-write guard) --
    reproducing the exact historical artifact the guard now prevents going forward.
    Resets the in-memory order index so load_open_orders() sees the corrupted state."""
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=_INTENT, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_REJECTED, intent=_INTENT, prev_state=wr.ORDER_STATE_SUBMITTED)
    seq = wr._order_journal_next_seq()
    wr.append_jsonl_durable(wr.ORDER_JOURNAL_LEDGER, {
        "schema_version": wr.ORDER_JOURNAL_SCHEMA_VERSION,
        "seq": seq,
        "ts": "2026-01-01T00:00:01Z",
        "cl_ord_id": cl,
        "state": wr.ORDER_STATE_UNKNOWN,
        "prev_state": wr.ORDER_STATE_SUBMITTED,
        "intent": _INTENT,
        "detail": {"unknown_resolver": True, "simulated_corruption": True},
    })
    wr._order_journal_index = None  # force a fresh rebuild that sees the corrupted record


# ---------------------------------------------------------------------------
# (a) order_history_for
# ---------------------------------------------------------------------------

def test_order_history_for_complete_from_true_origin(wr):
    cl = "mxc-xrpusdt-buy-history-complete"
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=_INTENT, prev_state=wr.ORDER_STATE_PLANNED)

    history = wr.order_history_for(cl)

    assert history["history_complete"] is True
    assert [r["state"] for r in history["records"]] == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED]


def test_order_history_for_incomplete_when_origin_missing(wr):
    cl = "mxc-xrpusdt-buy-history-pruned"
    wr.append_jsonl_durable(wr.ORDER_JOURNAL_LEDGER, {
        "schema_version": wr.ORDER_JOURNAL_SCHEMA_VERSION,
        "seq": wr._order_journal_next_seq(),
        "ts": "2026-01-01T00:00:00Z",
        "cl_ord_id": cl,
        "state": wr.ORDER_STATE_UNKNOWN,
        "prev_state": wr.ORDER_STATE_SUBMITTED,  # its earlier record is missing (simulated retention pruning)
        "intent": {},
        "detail": {},
    })

    history = wr.order_history_for(cl)

    assert history["history_complete"] is False


def test_order_history_for_sorts_by_seq_and_handles_unknown_cl(wr):
    assert wr.order_history_for("never-seen") == {"records": [], "history_complete": False}
    assert wr.order_history_for(None) == {"records": [], "history_complete": False}


def test_order_history_for_sees_corrupted_sequence(wr):
    cl = "mxc-xrpusdt-buy-history-corrupted"
    _seed_corrupted_unknown(wr, cl)

    history = wr.order_history_for(cl)

    assert [r["state"] for r in history["records"]] == [
        wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_REJECTED, wr.ORDER_STATE_UNKNOWN,
    ]
    assert history["history_complete"] is True


# ---------------------------------------------------------------------------
# (b) classify_terminal_overwritten -- pure, no wr fixture needed.
# ---------------------------------------------------------------------------

def _rec(seq, state, prev_state):
    return {"cl_ord_id": "cl-x", "seq": seq, "state": state, "prev_state": prev_state, "ts": f"t{seq}"}


def test_classify_terminal_overwritten_detects_corruption():
    records = [
        _rec(1, "PLANNED", None),
        _rec(2, "SUBMITTED", "PLANNED"),
        _rec(3, "REJECTED", "SUBMITTED"),
        _rec(4, "UNKNOWN", "SUBMITTED"),  # illegal -- overwrote the terminal record
    ]
    result = classify_terminal_overwritten(records, history_complete=True)

    assert result is not None
    assert result.issue_type == "terminal_overwritten"
    assert result.evidence["terminal_state"] == "REJECTED"
    assert result.evidence["overwritten_by_state"] == "UNKNOWN"
    assert result.actions == [SafeAction(id="restore_terminal", label="Restore to REJECTED")]


def test_classify_terminal_overwritten_none_when_terminal_is_last():
    records = [_rec(1, "PLANNED", None), _rec(2, "SUBMITTED", "PLANNED"), _rec(3, "REJECTED", "SUBMITTED")]
    assert classify_terminal_overwritten(records, history_complete=True) is None


def test_classify_terminal_overwritten_none_when_no_terminal_at_all():
    records = [_rec(1, "PLANNED", None), _rec(2, "SUBMITTED", "PLANNED"), _rec(3, "UNKNOWN", "SUBMITTED")]
    assert classify_terminal_overwritten(records, history_complete=True) is None


def test_classify_terminal_overwritten_none_on_empty_history():
    assert classify_terminal_overwritten([], history_complete=False) is None


# ---------------------------------------------------------------------------
# (c) classify_ambiguous_unknown -- pure, no wr fixture needed.
# ---------------------------------------------------------------------------

def test_classify_ambiguous_unknown_positive_when_complete():
    records = [_rec(1, "PLANNED", None), _rec(2, "SUBMITTED", "PLANNED"), _rec(3, "UNKNOWN", "SUBMITTED")]
    result = classify_ambiguous_unknown(records, history_complete=True)

    assert result is not None
    assert result.issue_type == "ambiguous_unknown"
    assert result.actions == []  # never auto-actionable


def test_classify_ambiguous_unknown_reports_incomplete_evidence():
    records = [_rec(2, "SUBMITTED", "PLANNED"), _rec(3, "UNKNOWN", "SUBMITTED")]  # origin missing
    result = classify_ambiguous_unknown(records, history_complete=False)

    assert result is not None
    assert result.issue_type == "evidence_incomplete"
    assert result.actions == []


def test_classify_ambiguous_unknown_defers_when_terminal_present():
    records = [_rec(1, "PLANNED", None), _rec(2, "REJECTED", "PLANNED"), _rec(3, "UNKNOWN", "SUBMITTED")]
    assert classify_ambiguous_unknown(records, history_complete=True) is None


def test_classify_ambiguous_unknown_none_when_latest_not_unknown():
    records = [_rec(1, "PLANNED", None), _rec(2, "SUBMITTED", "PLANNED")]
    assert classify_ambiguous_unknown(records, history_complete=True) is None


# ---------------------------------------------------------------------------
# (d) run_classifiers / troubleshoot_all_open_orders -- integration, needs wr.
# ---------------------------------------------------------------------------

def test_run_classifiers_on_corrupted_order(wr):
    cl = "mxc-xrpusdt-buy-runclassifiers"
    _seed_corrupted_unknown(wr, cl)

    result = run_classifiers(cl)

    assert result is not None
    assert result.issue_type == "terminal_overwritten"
    assert result.evidence["terminal_state"] == wr.ORDER_STATE_REJECTED


def test_troubleshoot_all_open_orders_surfaces_only_the_corrupted_order(wr):
    corrupted_cl = "mxc-xrpusdt-buy-troubleshoot-corrupt"
    _seed_corrupted_unknown(wr, corrupted_cl)

    # A normal, still-in-flight PLANNED order -- must NOT be surfaced (self-healing owns it).
    planned_cl = "mxc-xrpusdt-buy-troubleshoot-planned"
    wr.record_order_state(planned_cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)

    # A normal, still-pending SUBMITTED order -- must NOT be surfaced (resolver owns it).
    submitted_cl = "mxc-xrpusdt-buy-troubleshoot-submitted"
    wr.record_order_state(submitted_cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)
    wr.record_order_state(submitted_cl, wr.ORDER_STATE_SUBMITTED, intent=_INTENT, prev_state=wr.ORDER_STATE_PLANNED)

    results = troubleshoot_all_open_orders()

    by_cl = {r.cl_ord_id: r for r in results}
    assert set(by_cl.keys()) == {corrupted_cl}
    assert by_cl[corrupted_cl].issue_type == "terminal_overwritten"
