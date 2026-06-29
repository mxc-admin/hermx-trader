"""Characterization: strategy-alert matching (REFACTOR_PLAN.md:167, :179).

Locks the current behavior of validate_strategy_alert / build_record for the
strategy-file matching matrix: valid match, symbol mismatch, timeframe mismatch,
unknown id, malformed (bad side), and duplicate. This is part of the regression
oracle for P1/P3 -- if matching/quarantine behavior changes, these fail.

Harness: the `wr` fixture reloads webhook_receiver bound to a populated temp
SHADOW_ROOT (4 corpus strategies + dry-run config). No MXC, no network, no OKX
subprocess (execution hard-disabled by the corpus config).
"""
from __future__ import annotations

from conftest import load_alert

RECEIVED_AT = "2026-06-22T00:00:00Z"


def _validate(wr, rel_path):
    payload = load_alert(rel_path)
    normalized = wr.normalize(payload)
    return wr.validate_strategy_alert(normalized)


def test_valid_strategy_match(wr):
    ok, strategy, error = _validate(wr, "strategy/btcusdt_buy.json")
    assert ok is True
    assert error is None
    assert strategy is not None
    assert strategy["strategy_id"] == "btcusdt_duo_base_dev_2h"
    assert strategy["asset"] == "BTCUSDT"
    assert strategy["timeframe"] == "2h"


def test_symbol_mismatch_quarantined(wr):
    ok, strategy, error = _validate(wr, "strategy/symbol_mismatch.json")
    assert ok is False
    assert error == "strategy_symbol_mismatch"
    assert strategy is not None  # matched the id, but asset disagrees


def test_timeframe_mismatch_quarantined(wr):
    ok, strategy, error = _validate(wr, "strategy/timeframe_mismatch.json")
    assert ok is False
    assert error == "strategy_timeframe_mismatch"
    assert strategy is not None


def test_unknown_strategy_id_quarantined(wr):
    ok, strategy, error = _validate(wr, "strategy/unknown_strategy_id.json")
    assert ok is False
    assert error == "unknown_strategy_id"
    assert strategy is None


def test_all_four_active_strategies_match(wr):
    """Every active corpus strategy resolves and matches its own asset/timeframe."""
    cases = [
        ("strategy/btcusdt_buy.json", "btcusdt_duo_base_dev_2h"),
        ("strategy/ethusdt_buy.json", "ethusdt_duo_base_dev_2h"),
        ("strategy/solusdt_sell.json", "solusdt_duo_base_dev_3h"),
        ("strategy/xrpusdt_buy.json", "xrpusdt_duo_base_dev_4h"),
    ]
    for rel_path, expected_id in cases:
        ok, strategy, error = _validate(wr, rel_path)
        assert ok is True, (rel_path, error)
        assert strategy["strategy_id"] == expected_id


# --- end-to-end via build_record (status codes + record shape) ---------------

def test_build_record_valid_match_demo_is_armed_sandboxed(wr, monkeypatch):
    # 2-mode model: a demo strategy is armed (both demo and live submit); the legacy
    # submit_orders flag is ignored and execution_mode alone decides sandbox vs live.
    # Force the execution surface unavailable so the offline outcome is deterministic
    # (a real submit would otherwise be attempted against the sandbox venue).
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    status, record = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert status == 200
    assert record["mode"] == "strategy_file_trial"
    assert record["ok"] is True
    assert record.get("quarantined") is not True
    assert record["strategy_config"]["strategy_id"] == "btcusdt_duo_base_dev_2h"
    # Demo arms submission but routes to the sandbox; the execution surface being
    # unavailable yields a deterministic not_submitted/execution_unavailable outcome.
    assert record["okx_execution"]["mode"] == "not_submitted"
    assert record["okx_execution"]["reason"] == "execution_unavailable"
    assert record["execution_readiness"]["live_execution_enabled"] is True
    assert record["execution_readiness"]["execution_mode"] == "demo"
    assert record["execution_readiness"]["simulated_trading"] is True


def test_build_record_malformed_bad_side_rejected(wr):
    status, record = wr.build_record(load_alert("strategy/malformed_bad_side.json"), RECEIVED_AT)
    assert status == 400
    assert record["ok"] is False
    assert record["error"] == "side_not_allowed"


def test_build_record_mismatches_quarantined_202(wr):
    for rel_path, reason in [
        ("strategy/symbol_mismatch.json", "strategy_symbol_mismatch"),
        ("strategy/timeframe_mismatch.json", "strategy_timeframe_mismatch"),
        ("strategy/unknown_strategy_id.json", "unknown_strategy_id"),
    ]:
        status, record = wr.build_record(load_alert(rel_path), RECEIVED_AT)
        assert status == 202, rel_path
        assert record["mode"] == "strategy_alert_quarantine", rel_path
        assert record["quarantined"] is True
        assert record["reason"] == reason


def test_build_record_duplicate_detected(wr):
    first_status, first = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert first_status == 200
    assert first.get("duplicate") is False

    dup_status, dup = wr.build_record(load_alert("strategy/btcusdt_buy_duplicate.json"), RECEIVED_AT)
    assert dup_status == 200
    assert dup["duplicate"] is True
    assert dup["dedupe"]["duplicate_by"] in {"signal_id", "symbol_side_timeframe_tv_time"}
    # Same dedupe identity as the original (strategy_id|symbol|side|timeframe|tv_time).
    assert dup["dedupe"]["dedupe_key"] == first["dedupe"]["dedupe_key"]
