"""Phase 5 (P5-05): Hermes execution skill runtime.

The skill is the ONLY agent-facing execution surface. These tests prove it:
  * builds a normalized intent but submits NOTHING in dry_run,
  * in live mode routes exclusively through the controlled ExecutionService and
    maps the service result vocabulary onto the skill contract modes,
  * surfaces a kill-switch / gate ``not_submitted`` reason (delegated, not re-derived),
  * maps a submit timeout/exception path to ``unknown`` (uncertain, never retried).

Fully offline: the service is a mock/stub, so no exchange/network is touched.
"""
from __future__ import annotations

from unittest import mock

import pytest

from skills.hermes_execution import HermesExecutionSkill, build_execution_intent


def _signal(**over):
    base = {
        "strategy_id": "duo_raw",
        "symbol": "XRPUSDT",
        "side": "buy",
        "timeframe": "30m",
        "tv_time": "2026-06-25T00:00:00Z",
        "signal_id": "sig-1",
    }
    base.update(over)
    return base


def _strategy(**over):
    base = {
        "strategy_id": "duo_raw",
        "asset": "XRPUSDT",
        "timeframe": "30m",
        "inst_id": "XRP-USDT-SWAP",
        "budget_usd": 1500,
        "leverage": 2,
    }
    base.update(over)
    return base


def _account(**over):
    base = {"auth_healthy": True, "assets": {}}
    base.update(over)
    return base


# --- intent building --------------------------------------------------------

def test_intent_is_stable_and_close_verify_open_ordered():
    intent = build_execution_intent(signal=_signal(), strategy=_strategy(), account_context=_account())
    assert intent["target_direction"] == "long"
    assert intent["inst_id"] == "XRP-USDT-SWAP"
    assert intent["actions"] == ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"]
    # Stable / idempotent: same inputs -> identical client_order_id.
    again = build_execution_intent(signal=_signal(), strategy=_strategy(), account_context=_account())
    assert intent["client_order_id"] == again["client_order_id"]
    assert intent["client_order_id"].startswith("mxc")


# --- dry_run never submits --------------------------------------------------

def test_dry_run_returns_not_submitted_and_never_calls_service():
    service = mock.Mock()
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="dry_run")

    service.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "dry_run"
    assert out["ok"] is True
    assert out["execution_intent"]["client_order_id"] == out["client_order_id"]
    assert out["exchange_result"] is None


def test_dry_run_is_the_default_mode():
    service = mock.Mock()
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account())

    service.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "dry_run"


# --- live routes through the service ----------------------------------------

def test_live_routes_through_service_and_maps_filled():
    service = mock.Mock()
    service.execute.return_value = {
        "ok": True,
        "mode": "submit_enabled",
        "payload": {"fill_summary": {"status": "filled"}},
    }
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    service.execute.assert_called_once()
    record = service.execute.call_args.args[0]
    # The skill hands the service a venue-neutral record with live intent expressed.
    assert record["execution_readiness"]["live_execution_enabled"] is True
    assert record["execution_readiness"]["okx_inst_id"] == "XRP-USDT-SWAP"
    assert record["execution_readiness"]["execution_intent"]["client_order_id"] == out["client_order_id"]
    assert out["mode"] == "filled"
    assert out["ok"] is True


def test_live_maps_submitted_when_not_yet_filled():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": True,
        "mode": "submit_enabled",
        "payload": {"fill_summary": {"status": "submitted"}},
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert out["mode"] == "submitted"
    assert out["ok"] is True


def test_live_maps_reconciled_state_over_stdout():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": True,
        "mode": "submit_enabled",
        "payload": {"fill_summary": {"status": "submitted"}},
        "reconcile": {"state": "FILLED"},
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert out["mode"] == "filled"
    assert out["reconcile"] == {"state": "FILLED"}


# --- kill switch / gate false (delegated, not re-derived) -------------------

def test_kill_switch_not_submitted_reason_is_surfaced():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": True,
        "mode": "not_submitted",
        "reason": "HERMX_SUBMIT_ENABLED kill switch engaged",
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    service.execute.assert_called_once()
    assert out["mode"] == "not_submitted"
    assert "kill switch" in out["reason"].lower()


def test_gate_false_not_submitted_reason_is_surfaced():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": True,
        "mode": "not_submitted",
        "reason": "execution disabled",
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert out["mode"] == "not_submitted"
    assert out["reason"] == "execution disabled"


# --- exception / timeout maps to unknown ------------------------------------

def test_service_exception_maps_to_unknown_and_does_not_retry():
    service = mock.Mock()
    service.execute.side_effect = TimeoutError("boom")
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert service.execute.call_count == 1  # never blindly retried
    assert out["mode"] == "unknown"
    assert out["reason"] == "submit_exception"
    assert out["ok"] is False


def test_submit_exception_result_mode_maps_to_unknown():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": False,
        "mode": "submit_exception",
        "error": "redacted",
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert out["mode"] == "unknown"
    assert out["ok"] is False


def test_submit_failed_rejected_maps_to_rejected():
    service = mock.Mock(execute=mock.Mock(return_value={
        "ok": False,
        "mode": "submit_failed",
        "payload": {"fill_summary": {"status": "rejected"}},
    }))
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(), strategy=_strategy(), account_context=_account(), mode="live")

    assert out["mode"] == "rejected"


# --- fail closed before submit ----------------------------------------------

def test_unresolved_venue_mapping_fails_closed_without_submit():
    service = mock.Mock()
    skill = HermesExecutionSkill(service=service)
    # No inst_id anywhere (strategy lacks it, account has no asset map).
    strat = _strategy()
    strat.pop("inst_id")

    out = skill.execute(signal=_signal(), strategy=strat, account_context=_account(), mode="live")

    service.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "unresolved_venue_mapping"


def test_invalid_side_fails_closed_without_submit():
    service = mock.Mock()
    skill = HermesExecutionSkill(service=service)

    out = skill.execute(signal=_signal(side="hold"), strategy=_strategy(), account_context=_account(), mode="live")

    service.execute.assert_not_called()
    assert out["mode"] == "not_submitted"
    assert out["reason"] == "invalid_signal_side"


def test_inst_id_resolved_from_account_asset_map():
    intent = build_execution_intent(
        signal=_signal(),
        strategy={"strategy_id": "duo_raw", "asset": "XRPUSDT"},
        account_context=_account(assets={"XRPUSDT": {"inst_id": "XRP-USDT-SWAP"}}),
    )
    assert intent["inst_id"] == "XRP-USDT-SWAP"


def test_constructing_without_service_is_rejected():
    with pytest.raises(ValueError):
        HermesExecutionSkill(service=None)
