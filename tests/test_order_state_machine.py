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

from unittest import mock

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


def test_record_order_state_rejects_illegal(wr):
    with pytest.raises(ValueError):
        wr.record_order_state("cl-x", wr.ORDER_STATE_FILLED, prev_state=wr.ORDER_STATE_PLANNED)
    # Illegal transition must persist nothing.
    assert not wr.ORDER_JOURNAL_LEDGER.exists()


# ---------------------------------------------------------------------------
# Shared config / readiness builders for the integration tests.
# ---------------------------------------------------------------------------

def _armed_config() -> dict:
    return {
        "execution": {"enabled": True, "submit_orders": True, "simulated_trading": True, "force_ipv4": True},
        "risk": {"allow_live_execution": True},
    }


def _disabled_config() -> dict:
    return {
        "execution": {"enabled": False, "submit_orders": False, "simulated_trading": True, "force_ipv4": True},
        "risk": {"allow_live_execution": False},
    }


def _armed_record(cl: str = "mxc-xrpusdt-buy-abc0123456789de") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }


def _adapter_result(*, ok=True, mode="submit_enabled"):
    """A normalized adapter result (BaseExecutor.normalized_result shape)."""
    return {
        "ok": ok,
        "mode": mode,
        "exchange": "ccxt",
        "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {"symbol": "XRP/USDT:USDT"},
    }


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
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "1")

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

    result = wr.execute_okx_if_enabled(_armed_record())
    return events, result


def test_write_ahead_planned_before_submit_success_fills(wr, monkeypatch):
    events, result = _run_forced_submit(wr, monkeypatch, lambda: _adapter_result(ok=True, mode="submit_enabled"))

    # Write-ahead PROOF: PLANNED and SUBMITTED are both written BEFORE executor.execute().
    assert events == [
        ("write", wr.ORDER_STATE_PLANNED),
        ("write", wr.ORDER_STATE_SUBMITTED),
        ("executor_execute", None),
        ("write", wr.ORDER_STATE_FILLED),
    ]
    planned_idx = events.index(("write", wr.ORDER_STATE_PLANNED))
    run_idx = events.index(("executor_execute", None))
    assert planned_idx < run_idx, "PLANNED must be durably written before submit"

    # Tentative FILLED is terminal => no open order remains.
    assert wr.load_open_orders() == []
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
    }


# ---------------------------------------------------------------------------
# (c) Disabled production config is observe-only inert.
# ---------------------------------------------------------------------------

def test_disabled_gates_write_no_order_journal(wr, monkeypatch):
    """Default/disabled config: gates fail => not_submitted, ZERO order-journal writes."""
    monkeypatch.setattr(wr, "CONFIG", _disabled_config())
    monkeypatch.delenv("HERMX_SUBMIT_ENABLED", raising=False)  # unset => kill switch inert/armed

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_okx_if_enabled(_armed_record())

    create_mock.assert_not_called()  # no executor built => no submit
    assert result["mode"] == "not_submitted"
    assert not wr.ORDER_JOURNAL_LEDGER.exists()
    assert wr.load_open_orders() == []


def test_kill_switch_branch_writes_no_order_journal(wr, monkeypatch):
    """Even with config armed, the kill switch not_submitted branch writes no record."""
    monkeypatch.setattr(wr, "CONFIG", _armed_config())
    monkeypatch.setenv("HERMX_SUBMIT_ENABLED", "false")

    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_okx_if_enabled(_armed_record())

    create_mock.assert_not_called()  # kill switch blocks before any executor build
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
