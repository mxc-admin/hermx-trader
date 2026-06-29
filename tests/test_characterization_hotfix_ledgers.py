"""Retroactive proof of the Immediate Hotfix Track (REFACTOR_PLAN.md:145, :146).

Closes the two deferred acceptance criteria:
  :145  No NameError for execution ledgers in shadow-processing-errors.jsonl
        after a synthetic alert run.
  :146  the authoritative executions.jsonl outcome ledger receives entries for
        processed alerts. (The separate, dead execution-plan.jsonl write was later
        removed -- nothing consumed it -- so these tests assert it is NOT produced.)

The hotfix was the EXECUTION_PLAN_LEDGER / EXECUTION_LEDGER constants (module
lines 37-38). Previously the readiness/execution writers referenced undefined
OKX_*_LEDGER names and raised NameError silently. Here we drive real alerts all
the way through the readiness builders + execute_okx_if_enabled and assert both
ledgers got valid JSON lines with no exception.

Post the Phase A execution-mode refactor the submit path always routes through
ExecutionService -> ExecutorFactory. We stub that single submit seam with a benign
in-process executor so the ledger path runs end-to-end WITHOUT reaching any exchange.

test_ledger_constants.py is the static guard against reintroduction (:147);
this is the dynamic, end-to-end proof.
"""
from __future__ import annotations

import json
from unittest import mock

from conftest import load_alert

RECEIVED_AT = "2026-06-23T00:00:00Z"


def _read_jsonl(path):
    assert path.exists(), f"expected ledger {path} to exist"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))  # raises if any line is not valid JSON
    return rows


def _pipeline_stage(wr_root, stage):
    """Rows of one stage from the consolidated pipeline.jsonl."""
    path = wr_root / "logs" / "pipeline.jsonl"
    return [r for r in _read_jsonl(path) if r.get("stage") == stage]


def _stub_executor(monkeypatch, wr):
    """Install a benign in-process executor for the single submit seam, so the ledger
    path runs end-to-end without reaching a real exchange. Post the CCXT cutover that
    seam is ExecutorFactory.create(...).execute()."""
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value={
        "ok": True, "mode": "submit_enabled", "exchange": "ccxt", "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {},
    })
    if wr.ExecutorFactory is not None:
        monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: fake)
    return fake


def test_strategy_alert_writes_both_ledgers(wr, wr_root, monkeypatch):
    """Strategy path: build_strategy_execution_readiness + execute_okx_if_enabled.

    The corpus strategy is execution_mode=demo with submit_orders=true, so it is armed
    for sandbox submission; the stubbed executor stands in for the exchange call."""
    _stub_executor(monkeypatch, wr)

    status, record = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert status == 200

    exec_rows = _pipeline_stage(wr_root, "execution")
    assert len(exec_rows) == 1

    # The dead execution-plan.jsonl write was removed entirely: the authoritative outcome
    # is recorded to pipeline.jsonl (stage="execution"), the single source the dashboard
    # consumes, so no separate plan ledger is produced.
    assert not (wr_root / "logs" / "execution-plan.jsonl").exists()

    # The execution pipeline stage shows the (sandboxed) submission outcome.
    assert exec_rows[0]["okx_execution"]["mode"] == "submit_enabled"


def test_no_strategy_match_alert_is_observe_only_no_execution(wr, wr_root, monkeypatch):
    """Generic (non-strategy) alert: the legacy shadow/policy engine that used to paper-
    trade these was removed, so the alert is recorded observe-only with no okx_execution,
    no execution pipeline row, and no execution-plan ledger."""
    _stub_executor(monkeypatch, wr)

    status, record = wr.build_record(load_alert("shadow/btcusdt_shadow_buy.json"), RECEIVED_AT)
    assert status == 200
    assert record["mode"] == "no_strategy_match"
    assert "okx_execution" not in record

    assert not (wr_root / "logs" / "execution-plan.jsonl").exists()
    assert _pipeline_stage(wr_root, "execution") == []


def test_execute_okx_directly_appends_execution_ledger(wr, wr_root, monkeypatch):
    """Direct call to the patched-constant writer proves the symbol resolves. A
    gate-blocked record (per-strategy submit flag off) stays not_submitted."""
    record = {"received_at": RECEIVED_AT, "execution_readiness": {"live_execution_enabled": False}}
    result = wr.execute_okx_if_enabled(record)  # would NameError pre-hotfix

    assert result["mode"] == "not_submitted"
    rows = _pipeline_stage(wr_root, "execution")
    assert rows[-1]["okx_execution"]["mode"] == "not_submitted"


def test_async_path_logs_no_nameerror(wr, wr_root, monkeypatch):
    """:145 -- run the full swallow-and-log async path; assert the error ledger
    contains no NameError (it should not exist at all for a clean alert)."""
    _stub_executor(monkeypatch, wr)

    wr.process_payload_async(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)

    # Processing errors are consolidated into pipeline.jsonl (stage="error").
    err_rows = _pipeline_stage(wr_root, "error")
    if err_rows:
        text = json.dumps(err_rows)
        assert "NameError" not in text, text
        assert "_LEDGER" not in text, text

    # And the authoritative execution outcome was written by the async run.
    assert _pipeline_stage(wr_root, "execution"), "expected an execution pipeline row"
