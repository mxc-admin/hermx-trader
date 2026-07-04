"""Submission-outcome state machine + write-ahead order journal tests
(REFACTOR_PLAN.md:204 write-ahead ordering, :216 state machine -- Phase 1 task 5).

Covers:
  (a) the PURE transition table order_state_can_transition (legal + illegal, incl.
      terminal-frozen, None->PLANNED, SUBMITTED->UNKNOWN);
  (b) a FORCED-submit test proving the write-ahead ORDER: PLANNED and SUBMITTED are
      durably written BEFORE subprocess.run is invoked, and the outcome record is
      FILLED / REJECTED / UNKNOWN for success / non-zero / raised-Timeout respectively
      (UNKNOWN is first-class, not a failure);
  (c) the disabled production config is observe-only inert: not_submitted with ZERO
      order-journal records;
  (d) load_open_orders folds to the latest non-terminal record per clOrdId, ignores
      terminal orders, and tolerates a truncated trailing line.
"""
from __future__ import annotations

import os
from unittest import mock

from conftest import adapter_result

import pytest

import webhook_receiver as wr


# ---------------------------------------------------------------------------
# (a) Pure transition table.
# ---------------------------------------------------------------------------

LEGAL_TRANSITIONS = [
    (None, "PLANNED"),
    ("PLANNED", "SUBMITTED"),
    ("PLANNED", "REJECTED"),          # aborted before send
    ("SUBMITTED", "FILLED"),
    ("SUBMITTED", "REJECTED"),
    ("SUBMITTED", "UNKNOWN"),         # timeout/crash -- first-class
    ("UNKNOWN", "FILLED"),            # resolver re-reconciles
    ("UNKNOWN", "REJECTED"),
    ("UNKNOWN", "UNKNOWN"),           # may re-UNKNOWN until terminal/budget
]

ILLEGAL_TRANSITIONS = [
    (None, "SUBMITTED"),
    (None, "FILLED"),
    (None, "UNKNOWN"),
    ("PLANNED", "FILLED"),           # never fills without being submitted
    ("PLANNED", "UNKNOWN"),
    ("PLANNED", "PLANNED"),
    ("FILLED", "REJECTED"),          # terminal frozen
    ("FILLED", "UNKNOWN"),
    ("FILLED", "FILLED"),
    ("REJECTED", "FILLED"),          # terminal frozen
    ("REJECTED", "SUBMITTED"),
    ("SUBMITTED", "PLANNED"),        # no going back
    ("SUBMITTED", "SUBMITTED"),
    ("bogus", "PLANNED"),            # unknown old state
    ("SUBMITTED", "bogus"),          # unknown new state
]


@pytest.mark.parametrize("old,new", LEGAL_TRANSITIONS)
def test_transition_legal(old, new):
    assert wr.order_state_can_transition(old, new) is True


@pytest.mark.parametrize("old,new", ILLEGAL_TRANSITIONS)
def test_transition_illegal(old, new):
    assert wr.order_state_can_transition(old, new) is False


def test_terminal_states_are_frozen():
    for terminal in (wr.ORDER_STATE_FILLED, wr.ORDER_STATE_REJECTED):
        for target in ("PLANNED", "SUBMITTED", "FILLED", "REJECTED", "UNKNOWN"):
            assert wr.order_state_can_transition(terminal, target) is False


# The single source of truth for the EXHAUSTIVE matrix below: every legal edge, stated
# independently of the implementation table so the test fails if the table drifts.
_EXPECTED_LEGAL = {
    (None, "PLANNED"),
    ("PLANNED", "SUBMITTED"),
    ("PLANNED", "REJECTED"),
    ("SUBMITTED", "FILLED"),
    ("SUBMITTED", "REJECTED"),
    ("SUBMITTED", "UNKNOWN"),
    ("UNKNOWN", "FILLED"),
    ("UNKNOWN", "REJECTED"),
    ("UNKNOWN", "UNKNOWN"),
}


def test_transition_matrix_is_exhaustive():
    # Cross EVERY old state (incl. None + an unknown garbage state) against EVERY new
    # state. order_state_can_transition must return True for exactly the edges in
    # _EXPECTED_LEGAL and False for all others -- no silent extra or missing edge.
    old_states = [None, "PLANNED", "SUBMITTED", "FILLED", "REJECTED", "UNKNOWN", "bogus"]
    new_states = ["PLANNED", "SUBMITTED", "FILLED", "REJECTED", "UNKNOWN", "bogus"]
    actual_legal = set()
    for old in old_states:
        for new in new_states:
            if wr.order_state_can_transition(old, new):
                actual_legal.add((old, new))
    assert actual_legal == _EXPECTED_LEGAL


def test_record_order_state_rejects_illegal(wr):
    with pytest.raises(ValueError):
        wr.record_order_state("cl-x", wr.ORDER_STATE_FILLED, prev_state=wr.ORDER_STATE_PLANNED)
    # Illegal transition must persist nothing.
    assert not wr.ORDER_JOURNAL_LEDGER.exists()


def test_append_jsonl_writes_whole_line_under_short_writes(wr, monkeypatch):
    # Money-path durability: a PLANNED record must never leak as a HALF-written line.
    # Even if the OS short-writes (e.g. disk pressure), append_jsonl must loop until the
    # ENTIRE line is on disk before fsync, so the record is all-or-nothing and a later
    # append can never wedge behind a partial line (which would brick the ledger).
    real_write = os.write
    calls = []

    def short_write(fd, data):
        # Force a 1-byte-at-a-time drip the first few times to simulate short writes.
        chunk = data[:1] if len(data) > 1 else data
        n = real_write(fd, bytes(chunk))
        calls.append(n)
        return n

    path = wr.LOG_DIR / "atomic-ledger.jsonl"
    monkeypatch.setattr(wr.os, "write", short_write)
    wr.append_jsonl(path, {"cl_ord_id": "cl-atomic", "state": "PLANNED", "n": 12345})
    monkeypatch.undo()

    # The full record round-trips intact despite the short writes.
    records = wr.read_jsonl_tolerant(path)
    assert records == [{"cl_ord_id": "cl-atomic", "state": "PLANNED", "n": 12345}]
    assert len(calls) > 1  # proves the short-write loop actually iterated


def test_torn_trailing_planned_does_not_corrupt_journal(wr):
    # A crash mid-append leaves at most a torn TRAILING line. The prior clean records
    # (a recoverable open order) must still load, and reading must not raise.
    cl = "mxc-xrpusdt-buy-tornplanned"
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    # Simulate a half-written trailing record fragment (no newline, invalid JSON).
    with wr.ORDER_JOURNAL_LEDGER.open("a", encoding="utf-8") as f:
        f.write('{"schema_version":1,"cl_ord_id":"cl-torn","state":"PLA')

    open_orders = wr.load_open_orders()  # must not raise
    assert len(open_orders) == 1 and open_orders[0]["cl_ord_id"] == cl
    assert open_orders[0]["state"] == wr.ORDER_STATE_SUBMITTED


# ---------------------------------------------------------------------------
# Shared config / readiness builders for the integration tests.
# ---------------------------------------------------------------------------

def _blocked_record(cl: str = "mxc-xrpusdt-buy-blocked0000000de") -> dict:
    """An otherwise-armed record whose per-strategy submit flag is off (gate blocked)."""
    rec = _armed_record(cl)
    rec["execution_readiness"]["live_execution_enabled"] = False
    return rec


def _armed_record(cl: str = "mxc-xrpusdt-buy-abc0123456789de") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            # #20a: the resolved (venue, mode) persisted onto the order-journal intent
            # so the reconciler queries the order's own account, not OKX-demo.
            "instrument": {"exchange": "okx", "inst_id": "XRP-USDT-SWAP", "type": "swap"},
            "execution_mode": "demo",
            "simulated_trading": True,
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }


def _adapter_result(*, ok=True, mode="submit_enabled"):
    """A normalized adapter result (BaseExecutor.normalized_result shape)."""
    return adapter_result(ok=ok, mode=mode, payload={"symbol": "XRP/USDT:USDT"})


# ---------------------------------------------------------------------------
# (b) FORCED-submit: write-ahead ordering + outcome mapping, via the EXECUTOR seam.
# Post-cutover the single submit call is executor.execute() (ExecutorFactory.create),
# NOT a subprocess. The write-ahead guarantee is unchanged: PLANNED then SUBMITTED
# are durably journalled BEFORE that submit call.
# ---------------------------------------------------------------------------

def _run_forced_submit(wr, monkeypatch, adapter_fn):
    """Arm every gate, force submission, and capture the interleaving of order-journal
    writes vs the executor.execute() invocation. ``adapter_fn`` returns the normalized
    adapter result (or raises to simulate a submit exception). Returns (events, result)."""
    monkeypatch.setenv("HERMX_LIVE_TRADING", "1")

    events: list[tuple] = []
    orig_durable = wr.append_jsonl_durable

    def spy_durable(path, obj):
        if path == wr.ORDER_JOURNAL_LEDGER:
            events.append(("write", obj.get("state")))
        return orig_durable(path, obj)

    def fake_execute(readiness):
        events.append(("executor_execute", None))
        return adapter_fn()

    fake = mock.Mock()
    fake.execute = fake_execute

    monkeypatch.setattr(wr, "append_jsonl_durable", spy_durable)
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    result = wr.execute_if_enabled(_armed_record())
    return events, result


def test_write_ahead_planned_before_submit_success_is_submitted(wr, monkeypatch):
    events, result = _run_forced_submit(wr, monkeypatch, lambda: _adapter_result(ok=True, mode="submit_enabled"))

    # Write-ahead PROOF: PLANNED and SUBMITTED are both written BEFORE executor.execute().
    # A bare ACK (mode submit_enabled, fill status "submitted") records SUBMITTED -- not a
    # terminal FILLED -- so there is no extra journal write after the submit; reconciliation
    # later transitions SUBMITTED -> FILLED.
    assert events == [
        ("write", wr.ORDER_STATE_PLANNED),
        ("write", wr.ORDER_STATE_SUBMITTED),
        ("executor_execute", None),
    ]
    planned_idx = events.index(("write", wr.ORDER_STATE_PLANNED))
    run_idx = events.index(("executor_execute", None))
    assert planned_idx < run_idx, "PLANNED must be durably written before submit"

    # SUBMITTED is non-terminal: the order stays OPEN for reconciliation.
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["state"] == wr.ORDER_STATE_SUBMITTED
    assert result["mode"] == "submit_enabled"


def test_explicit_reject_maps_rejected(wr, monkeypatch):
    # Adapter mode submit_failed (explicit reject) => REJECTED.
    events, result = _run_forced_submit(wr, monkeypatch, lambda: _adapter_result(ok=False, mode="submit_failed"))

    assert [e for e in events if e[0] == "write"] == [
        ("write", wr.ORDER_STATE_PLANNED),
        ("write", wr.ORDER_STATE_SUBMITTED),
        ("write", wr.ORDER_STATE_REJECTED),
    ]
    assert result["mode"] == "submit_failed"
    assert wr.load_open_orders() == []  # REJECTED is terminal


def test_timeout_reaches_unknown_not_failure(wr, monkeypatch):
    # Adapter mode submit_timeout (ccxt client timeout) => UNKNOWN, not a failure.
    events, result = _run_forced_submit(wr, monkeypatch, lambda: _adapter_result(ok=False, mode="submit_timeout"))

    assert [e for e in events if e[0] == "write"] == [
        ("write", wr.ORDER_STATE_PLANNED),
        ("write", wr.ORDER_STATE_SUBMITTED),
        ("write", wr.ORDER_STATE_UNKNOWN),
    ]
    # UNKNOWN is first-class: the order stays OPEN for reconciliation, not discarded.
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN
    assert open_orders[0]["cl_ord_id"] == "mxc-xrpusdt-buy-abc0123456789de"
    assert result["mode"] == "submit_timeout"


def test_submit_exception_reaches_unknown(wr, monkeypatch):
    # A raised exception from executor.execute() => UNKNOWN (the order may have
    # reached the venue); the journal records UNKNOWN, not a terminal failure.
    def _boom():
        raise RuntimeError("connection reset")

    events, result = _run_forced_submit(wr, monkeypatch, _boom)

    assert [e for e in events if e[0] == "write"] == [
        ("write", wr.ORDER_STATE_PLANNED),
        ("write", wr.ORDER_STATE_SUBMITTED),
        ("write", wr.ORDER_STATE_UNKNOWN),
    ]
    assert result["mode"] == "submit_exception"
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1 and open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


def test_planned_record_carries_minimal_intent(wr, monkeypatch):
    _run_forced_submit(wr, monkeypatch, lambda: _adapter_result(ok=True, mode="submit_enabled"))
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    planned = next(r for r in records if r["state"] == wr.ORDER_STATE_PLANNED)
    assert planned["schema_version"] == wr.ORDER_JOURNAL_SCHEMA_VERSION
    assert planned["prev_state"] is None
    assert planned["cl_ord_id"] == "mxc-xrpusdt-buy-abc0123456789de"
    assert planned["intent"] == {
        "symbol": "XRPUSDT",
        "side": "buy",
        "inst_id": "XRP-USDT-SWAP",
        "planned_notional_usd": "1500.000000",
        "policy": "weighted_v1",
        # #20a: venue/mode persisted so reconcile targets the order's own account.
        "venue": "okx",
        "mode": "demo",
        "simulated_trading": True,
    }


# ---------------------------------------------------------------------------
# (c) Disabled production config is observe-only inert.
# ---------------------------------------------------------------------------

def test_blocked_gate_writes_no_order_journal(wr, monkeypatch):
    """Blocked gate (per-strategy submit flag off): not_submitted, ZERO order-journal writes."""

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_if_enabled(_blocked_record())

    create_mock.assert_not_called()  # no executor built => no submit
    assert result["mode"] == "not_submitted"
    assert not wr.ORDER_JOURNAL_LEDGER.exists()
    assert wr.load_open_orders() == []


def test_not_submitted_branch_writes_no_order_journal(wr, monkeypatch):
    """The not_submitted branch returns before any order-journal record is written."""

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_if_enabled(_blocked_record())

    create_mock.assert_not_called()
    assert result["mode"] == "not_submitted"
    assert not wr.ORDER_JOURNAL_LEDGER.exists()


# ---------------------------------------------------------------------------
# (d) load_open_orders folding.
# ---------------------------------------------------------------------------

def test_load_open_orders_folds_latest_non_terminal(wr):
    # A: open (SUBMITTED is latest)
    wr.record_order_state("A", wr.ORDER_STATE_PLANNED, prev_state=None)
    wr.record_order_state("A", wr.ORDER_STATE_SUBMITTED, prev_state=wr.ORDER_STATE_PLANNED)
    # B: terminal FILLED -> excluded
    wr.record_order_state("B", wr.ORDER_STATE_PLANNED, prev_state=None)
    wr.record_order_state("B", wr.ORDER_STATE_SUBMITTED, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state("B", wr.ORDER_STATE_FILLED, prev_state=wr.ORDER_STATE_SUBMITTED)
    # C: open (UNKNOWN is latest)
    wr.record_order_state("C", wr.ORDER_STATE_PLANNED, prev_state=None)
    wr.record_order_state("C", wr.ORDER_STATE_SUBMITTED, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state("C", wr.ORDER_STATE_UNKNOWN, prev_state=wr.ORDER_STATE_SUBMITTED)

    # Append a truncated trailing line that load_open_orders must tolerate.
    with wr.ORDER_JOURNAL_LEDGER.open("a", encoding="utf-8") as f:
        f.write('{"seq": 999, "cl_ord_id": "D", "stat')

    open_orders = wr.load_open_orders()
    by_cl = {r["cl_ord_id"]: r["state"] for r in open_orders}
    assert by_cl == {"A": wr.ORDER_STATE_SUBMITTED, "C": wr.ORDER_STATE_UNKNOWN}
    assert "B" not in by_cl  # terminal excluded
    assert "D" not in by_cl  # truncated trailing line dropped


# ---------------------------------------------------------------------------
# (e) Partial multi-leg submission must NOT be journalled as a flat REJECTED (H3).
# Merged from the retired test_multi_leg_partial.py. When the close leg of a
# reversal reaches the venue but the open leg fails, the adapter returns
# ok=False, mode="submit_partial": the venue already moved the position, so the
# outcome is UNCERTAIN -- the journal must record UNKNOWN (needs reconciliation),
# never a terminal REJECTED that would corrupt position math. Fully offline.
# ---------------------------------------------------------------------------

def _partial_adapter_result() -> dict:
    """Adapter result for a close-submitted / open-failed reversal."""
    return {
        "ok": False,
        "mode": "submit_partial",
        "exchange": "ccxt",
        "elapsed_ms": 7,
        "fill_summary": {
            "status": "submit_partial",
            "order_id": "close-ord-1",
            "client_order_id": None,
            "position_after_order": {"side": "flat", "contracts": 0.0},
        },
        "payload": {
            "executed_orders": [
                {"action": "CLOSE_SHORT", "submitted": True, "status": "submitted", "order": {"id": "close-ord-1"}},
                {"action": "OPEN_LONG", "submitted": True, "status": "rejected", "error": "insufficient_margin"},
            ],
        },
    }


def test_submit_partial_records_unknown_not_rejected(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-partial0000000de"

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_partial_adapter_result())
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    result = wr.execute_if_enabled(_armed_record(cl))

    fake.execute.assert_called_once()
    assert result["mode"] == "submit_partial"
    assert result["ok"] is False

    # The journal must record UNKNOWN (needs reconciliation), not a terminal REJECTED.
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]

    # UNKNOWN is non-terminal: the order stays OPEN for reconciliation.
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["cl_ord_id"] == cl
    assert open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


def test_submit_partial_emits_operator_alert_when_reconcile_enabled(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-partial-alert00de"
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")
    # No reconciliation executor available => tentative UNKNOWN is kept; the partial
    # alert is still emitted so an operator notices the half-executed reversal.
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: None)

    fake = mock.Mock()
    fake.execute = mock.Mock(return_value=_partial_adapter_result())
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    wr.execute_if_enabled(_armed_record(cl))

    alerts = wr.read_jsonl_tolerant(wr.ALERTS_LEDGER)
    partial = [a for a in alerts if a.get("kind") == "reconcile" and a["detail"].get("stage") == "post_submit_partial"]
    assert len(partial) == 1
    assert partial[0]["detail"]["cl_ord_id"] == cl
    assert partial[0]["detail"]["reason"] == "submit_partial"


def test_adapter_exception_with_reconcile_enabled_does_not_raise_unbound(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-exc000000000de"
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: None)

    fake = mock.Mock()
    fake.execute = mock.Mock(side_effect=RuntimeError("boom-create-order"))
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)

    result = wr.execute_if_enabled(_armed_record(cl))  # was: UnboundLocalError

    assert result["ok"] is False
    assert result["mode"] == "submit_exception"
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]


# ---------------------------------------------------------------------------
# Stale-write guard: record_order_state must validate against the journal's
# ACTUAL latest state (under the lock), not just the caller's prev_state claim.
# ---------------------------------------------------------------------------

def _seed_rejected(wr, cl: str) -> None:
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_REJECTED, intent=intent, prev_state=wr.ORDER_STATE_SUBMITTED)


def test_stale_prev_state_cannot_clobber_terminal(wr):
    # The resolver race: caller snapshotted SUBMITTED before a concurrent writer
    # landed terminal REJECTED. The stale UNKNOWN write must raise and append nothing.
    cl = "mxc-xrpusdt-buy-staleclobber"
    _seed_rejected(wr, cl)
    with pytest.raises(ValueError, match="stale"):
        wr.record_order_state(cl, wr.ORDER_STATE_UNKNOWN, prev_state=wr.ORDER_STATE_SUBMITTED)
    assert wr.latest_order_record(cl)["state"] == wr.ORDER_STATE_REJECTED
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_REJECTED]


def test_legitimate_transitions_still_pass_stale_guard(wr):
    # Happy path unchanged: correct prev_state at every step journals normally.
    cl = "mxc-xrpusdt-buy-staleok"
    _seed_rejected(wr, cl)
    assert wr.latest_order_record(cl)["state"] == wr.ORDER_STATE_REJECTED


def test_resolver_lost_race_leaves_terminal_state_intact(wr, monkeypatch):
    # End-to-end race replay: the resolver snapshots cur_state=SUBMITTED, then the
    # submit path journals REJECTED during the reconcile backoff window, then the
    # resolver's not-found outcome tries SUBMITTED -> UNKNOWN. The in-lock guard must
    # reject the stale write; the resolver swallows it into summary["errors"].
    cl = "mxc-xrpusdt-buy-resolverrace"
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    def racing_reconcile(executor, lookup, **kw):
        # Simulate the concurrent submit-path terminal write landing mid-backoff.
        wr.record_order_state(cl, wr.ORDER_STATE_REJECTED, intent=intent, prev_state=wr.ORDER_STATE_SUBMITTED)
        return {"state": wr.ORDER_STATE_UNKNOWN, "reason": "not_found", "source": "stub", "attempts": 1, "elapsed_s": 0.0}

    monkeypatch.setattr(wr, "reconcile_order_with_backoff", racing_reconcile)
    summary = wr.resolve_unknown_orders_once(executor=mock.Mock())
    assert wr.latest_order_record(cl)["state"] == wr.ORDER_STATE_REJECTED
    assert any(cl in err for err in summary["errors"])
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_REJECTED]
