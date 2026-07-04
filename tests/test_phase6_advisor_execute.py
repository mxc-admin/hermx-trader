"""Phase 6 prep -- characterization tests for ``execute_with_advisor``.

``execute_with_advisor`` (webhook_receiver) is the single wrapper used by the
execution paths: it consults ``run_execution_advisor``, honors a veto when one
is granted, and otherwise delegates to ``execute_if_enabled``. Before Phase 6
extracts the advisor cluster out of the monolith, these tests lock in what the
function ACTUALLY does today -- they characterize current behavior, they do not
prescribe ideal behavior.

Behavior locked in here (direct calls, not via build_record -- that wiring is
covered by test_phase8_advisor.py):
  * advisor disabled  -> returns execute_if_enabled(record) verbatim; the record
    is NOT annotated with an "advisor" key; the SAME record object is passed on.
  * advisor proceed   -> record["advisor"] annotated, then delegates.
  * advisor skip/veto -> short-circuits with the exact vetoed_by_advisor result
    (execute_if_enabled is never called) and writes a stage="execution" pipeline
    event.
  * advisor transport error / malformed reply / invalid action -> FAILS OPEN:
    annotated with ok=False + error, veto_applied=False, then delegates.
  * pipeline-ledger write failures on the veto path (execute_with_advisor) and
    the execution_unavailable path (_execute_authoritative) are caught and
    logged (log-and-continue, same guard as run_execution_advisor's ledger
    append) -- the normal result is still returned, never an exception.

Like test_phase8_advisor.py, the transport seam (``_advisor_agent_query``) is
monkeypatched so no real agent runs, and the controlled execution surface is
made unavailable so proceed paths deterministically resolve to a
not_submitted/execution_unavailable outcome instead of attempting an order.
"""

import json

import pytest

RECEIVED_AT = "2026-07-04T00:00:00Z"

PROCEED_REPLY = '{"action": "proceed", "risk_note": "looks fine", "score": 10}'
SKIP_REPLY = '{"action": "skip", "risk_note": "elevated risk", "score": 88}'


@pytest.fixture(autouse=True)
def _execution_surface_unavailable(wr, monkeypatch):
    """Make the controlled execution surface unavailable so every proceed path
    resolves to {"ok": True, "mode": "not_submitted", "reason":
    "execution_unavailable"} instead of attempting a real sandbox order. The
    advisor sits ABOVE the submit gate, so this does not change what these
    tests assert (same posture as test_phase8_advisor.py)."""
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)


def _enable(wr, monkeypatch, reply=PROCEED_REPLY):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", True)
    monkeypatch.setattr(wr, "_advisor_agent_query", lambda prompt: reply)


def _record(**overrides) -> dict:
    """Minimal processing record shaped like what build_record hands to
    execute_with_advisor: enough for _signal_id_of, _advisor_state_snapshot and
    the execution-unavailable path."""
    rec = {
        "received_at": RECEIVED_AT,
        "normalized": {
            "signal_id": "sig-p6-characterization",
            "symbol": "BTC/USDT",
            "side": "buy",
            "timeframe": "1h",
            "strategy_id": "corpus_btc",
            "tv_signal_price": 50000,
        },
        "strategy_config": {"leverage": 3, "budget_usd": 100},
        "execution_readiness": {"live_execution_enabled": False},
    }
    rec.update(overrides)
    return rec


def _pipeline_rows(wr_root, stage=None):
    ledger = wr_root / "logs" / "pipeline.jsonl"
    if not ledger.exists():
        return []
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if stage is None or r.get("stage") == stage]


# --- advisor disabled (default): pure pass-through --------------------------

def test_disabled_delegates_verbatim_and_does_not_annotate(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", False)
    sentinel = {"ok": True, "mode": "sentinel"}
    seen = []

    def fake_execute_if_enabled(record):
        seen.append(record)
        return sentinel

    monkeypatch.setattr(wr, "execute_if_enabled", fake_execute_if_enabled)
    record = _record()
    result = wr.execute_with_advisor(record)
    # Verbatim delegation: the sentinel comes back untouched (same object).
    assert result is sentinel
    # The SAME record object is handed to execute_if_enabled ...
    assert len(seen) == 1 and seen[0] is record
    # ... and the record is never annotated when the advisor is disabled.
    assert "advisor" not in record


def test_disabled_with_unavailable_surface_returns_execution_unavailable(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", False)
    result = wr.execute_with_advisor(_record())
    assert result == {
        "ok": True,
        "mode": "not_submitted",
        "reason": "execution_unavailable",
    }


# --- proceed path: annotate then delegate ------------------------------------

def test_proceed_annotates_record_and_delegates(wr, monkeypatch):
    _enable(wr, monkeypatch, PROCEED_REPLY)
    sentinel = {"ok": True, "mode": "sentinel"}
    monkeypatch.setattr(wr, "execute_if_enabled", lambda record: sentinel)
    record = _record()
    result = wr.execute_with_advisor(record)
    assert result is sentinel
    decision = record["advisor"]
    assert decision["enabled"] is True
    assert decision["ok"] is True
    assert decision["action"] == "proceed"
    assert decision["veto_applied"] is False
    assert decision["risk_note"] == "looks fine"
    assert decision["score"] == 10
    assert "latency_ms" in decision
    # Model/skills provenance is stamped on every decision.
    assert "model" in decision and "skills" in decision


def test_proceed_writes_advisor_pipeline_event(wr, wr_root, monkeypatch):
    _enable(wr, monkeypatch, PROCEED_REPLY)
    wr.execute_with_advisor(_record())
    rows = _pipeline_rows(wr_root, stage="advisor")
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_id"] == "sig-p6-characterization"
    assert row["advisor"]["action"] == "proceed"
    # The advisor row carries the read-only state snapshot the LLM reasoned over.
    assert row["snapshot"]["symbol"] == "BTC/USDT"
    assert row["snapshot"]["budget_usd"] == 100.0
    # Proceed means NO stage="execution" row from execute_with_advisor itself
    # (execution_unavailable rows come from _execute_authoritative instead).
    exec_rows = _pipeline_rows(wr_root, stage="execution")
    assert len(exec_rows) == 1
    assert exec_rows[0]["okx_execution"]["reason"] == "execution_unavailable"


# --- veto path: short-circuit, exact shape, no submission --------------------

def test_veto_short_circuits_and_never_calls_execute_if_enabled(wr, monkeypatch):
    _enable(wr, monkeypatch, SKIP_REPLY)

    def _must_not_be_called(record):
        raise AssertionError("execute_if_enabled must not run on a veto")

    monkeypatch.setattr(wr, "execute_if_enabled", _must_not_be_called)
    record = _record()
    result = wr.execute_with_advisor(record)
    # Exact result shape: ok=True (the veto is a SUCCESSFUL outcome, not an
    # error) and the advisor sub-dict carries ONLY risk_note + score.
    assert result == {
        "ok": True,
        "mode": "not_submitted",
        "reason": "vetoed_by_advisor",
        "advisor": {"risk_note": "elevated risk", "score": 88},
    }
    # The full decision (with veto_applied) lands on the record itself.
    assert record["advisor"]["veto_applied"] is True
    assert record["advisor"]["action"] == "skip"


def test_veto_writes_execution_pipeline_event(wr, wr_root, monkeypatch):
    _enable(wr, monkeypatch, SKIP_REPLY)
    wr.execute_with_advisor(_record())
    exec_rows = _pipeline_rows(wr_root, stage="execution")
    assert len(exec_rows) == 1
    row = exec_rows[0]
    assert row["signal_id"] == "sig-p6-characterization"
    assert row["received_at"] == RECEIVED_AT
    assert row["okx_execution"]["reason"] == "vetoed_by_advisor"
    assert row["okx_execution"]["mode"] == "not_submitted"
    # The advisor decision row is ALSO written (by run_execution_advisor).
    assert len(_pipeline_rows(wr_root, stage="advisor")) == 1


# --- fail OPEN: advisor failure can never block a sanctioned trade -----------

def test_transport_error_fails_open_and_delegates(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", True)

    def _boom(prompt):
        raise RuntimeError("hermes one-shot exit 1: transport down")

    monkeypatch.setattr(wr, "_advisor_agent_query", _boom)
    sentinel = {"ok": True, "mode": "sentinel"}
    monkeypatch.setattr(wr, "execute_if_enabled", lambda record: sentinel)
    record = _record()
    result = wr.execute_with_advisor(record)
    assert result is sentinel
    decision = record["advisor"]
    assert decision["ok"] is False
    assert decision["action"] == "proceed"
    assert decision["veto_applied"] is False
    assert "transport down" in decision["error"]


def test_malformed_reply_fails_open_and_delegates(wr, monkeypatch):
    _enable(wr, monkeypatch, "not json at all")
    result = wr.execute_with_advisor(_record())
    assert result["reason"] == "execution_unavailable"  # delegation happened


def test_invalid_action_fails_open_and_delegates(wr, monkeypatch):
    # action="buy" is outside {proceed, skip}: the parser raises, the advisor
    # fails open, and execution proceeds deterministically.
    _enable(wr, monkeypatch, '{"action": "buy", "risk_note": "x", "score": 1}')
    record = _record()
    result = wr.execute_with_advisor(record)
    assert result["reason"] == "execution_unavailable"
    assert record["advisor"]["ok"] is False
    assert record["advisor"]["veto_applied"] is False


# --- pipeline-ledger write failures are observability: log and continue ------

def _failing_pipeline_write(stage, signal_id, payload=None, *, durable=False):
    raise OSError("disk full")


def test_veto_pipeline_write_failure_logs_and_continues(wr, monkeypatch, caplog):
    """The veto branch's record_pipeline_event call is guarded with the same
    try/except log-and-continue as the ledger append inside run_execution_advisor:
    a pipeline-ledger write failure is logged and the normal veto result is
    still returned -- observability failures never block the money path."""
    _enable(wr, monkeypatch, SKIP_REPLY)
    monkeypatch.setattr(wr, "record_pipeline_event", _failing_pipeline_write)
    with caplog.at_level("WARNING"):
        result = wr.execute_with_advisor(_record())
    assert result == {
        "ok": True,
        "mode": "not_submitted",
        "reason": "vetoed_by_advisor",
        "advisor": {"risk_note": "elevated risk", "score": 88},
    }
    assert any("execution ledger append failed" in r.message for r in caplog.records)


def test_execution_unavailable_pipeline_write_failure_logs_and_continues(wr, monkeypatch, caplog):
    """_execute_authoritative's execution_unavailable branch has the same guard:
    a broken pipeline-ledger writer is logged, and the fail-closed outcome is
    still returned instead of raising."""
    monkeypatch.setattr(wr, "record_pipeline_event", _failing_pipeline_write)
    with caplog.at_level("WARNING"):
        result = wr._execute_authoritative(_record())
    assert result == {
        "ok": True,
        "mode": "not_submitted",
        "reason": "execution_unavailable",
    }
    assert any("execution ledger append failed" in r.message for r in caplog.records)
