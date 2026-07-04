"""Phase 6 / M2: alert schema widening + explicit intake validation.

Proves:
  * the alert `exchange` field is removed from the schema; routing comes entirely
    from `strategy.instrument.exchange`,
  * the alert `symbol` regex is relaxed (no longer pinned to *USDT) to match the
    v2 strategy asset regex,
  * the runtime helper `validate_alert_schema` enforces the schema on a
    normalized alert,
  * intake enforcement is gated behind `strategy_engine.enforce_alert_schema`
    (default OFF / observe-only):
      - flag OFF  -> a schema-invalid alert is logged + counted but processed
        BYTE-IDENTICALLY to the pre-M2 pipeline (no quarantine),
      - flag ON   -> a schema-invalid alert is routed to the EXISTING strategy
        quarantine path (STRATEGY_QUARANTINE_LEDGER) and never processed.

Schema checks are pure jsonschema; the intake tests reuse the `wr` characterization
harness (webhook_receiver reloaded against an isolated temp HERMX_ROOT with
execution hard-disabled by the dry-run corpus config). No network, no OKX.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from conftest import load_alert

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_SCHEMA_PATH = REPO_ROOT / "schemas" / "tradingview-alert.schema.json"

RECEIVED_AT = "2026-06-22T00:00:00Z"

# A minimal alert in the normalized (canonical snake_case) shape the runtime
# validates and the schema requires.
BASE_ALERT = {
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "symbol": "BTCUSDT",
    "timeframe": "2h",
    "side": "buy",
    "action": "buy",
    "tv_signal_price": 65000.0,
    "tv_time": "2026-06-20T00:00:00Z",
    "source": "tradingview",
}


def _without(*keys) -> dict:
    return {k: v for k, v in BASE_ALERT.items() if k not in keys}


def _schema() -> dict:
    return json.loads(ALERT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _is_valid(payload: dict) -> bool:
    return not list(_validator().iter_errors(payload))


def _alert(**overrides) -> dict:
    out = dict(BASE_ALERT)
    out.update(overrides)
    return out


# --------------------------------------------------------------------------- #
# (a) widened exchange enum                                                     #
# --------------------------------------------------------------------------- #


def test_schema_is_well_formed():
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_alert_valid_without_exchange():
    """exchange was removed from the schema; alerts must be valid without it."""
    alert_without_exchange = {k: v for k, v in BASE_ALERT.items() if k != "exchange"}
    assert _is_valid(alert_without_exchange)


# --------------------------------------------------------------------------- #
# (a2) PR2: `action` field (anyOf side|action; action adds `close`)             #
# --------------------------------------------------------------------------- #


def test_action_buy_valid():
    """action=buy with no side present is schema-valid (anyOf side|action)."""
    alert = _without("side")
    alert["action"] = "buy"
    assert _is_valid(alert)


def test_action_sell_valid():
    alert = _without("side")
    alert["action"] = "sell"
    assert _is_valid(alert)


def test_action_close_valid():
    """action=close with side removed is valid."""
    alert = _without("side")
    alert["action"] = "close"
    assert _is_valid(alert)


def test_action_close_no_side_valid():
    """BASE_ALERT with the side key absent and action=close is valid."""
    alert = _without("side")
    alert["action"] = "close"
    assert _is_valid(alert)


def test_action_invalid_enum():
    """action outside {buy,sell,close} is rejected even if a valid side is present."""
    assert not _is_valid(_alert(action="long"))


def test_neither_action_nor_side():
    """anyOf requires at least one of side|action; an alert with neither is invalid."""
    assert not _is_valid(_without("side", "action"))


def test_action_side_matching():
    """Both present and in agreement (buy/buy) is valid."""
    assert _is_valid(_alert(action="buy", side="buy"))


# --------------------------------------------------------------------------- #
# (b) relaxed symbol regex (matches the v2 strategy asset regex)               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/USDT", "BTC-USDT-SWAP", "ETHBTC", "BTC/USDT:USDT".replace(":", "-")])
def test_relaxed_symbol_regex_accepts(symbol):
    assert _is_valid(_alert(symbol=symbol)), f"{symbol} must be accepted"


@pytest.mark.parametrize("symbol", ["btcusdt", "", "BTC USDT", "BTC_USDT"])
def test_relaxed_symbol_regex_still_rejects_malformed(symbol):
    assert not _is_valid(_alert(symbol=symbol))


# --------------------------------------------------------------------------- #
# runtime helper: validate_alert_schema                                        #
# --------------------------------------------------------------------------- #


def test_runtime_validator_accepts_alert_without_exchange(wr):
    """exchange removed from schema; validator must accept alerts that omit it."""
    alert_without_exchange = {k: v for k, v in BASE_ALERT.items() if k != "exchange"}
    ok, error = wr.validate_alert_schema(alert_without_exchange)
    assert ok is True, error
    assert error is None


# --------------------------------------------------------------------------- #
# (c) flag OFF (default): logged + counted but processed byte-identically       #
# --------------------------------------------------------------------------- #


def _reset_dedupe(wr) -> None:
    """Clear the in-memory dedupe index so the same alert can be replayed within
    one test without tripping duplicate detection (index stays `loaded`)."""
    wr._SIGNAL_DEDUPE_INDEX["signals"].clear()
    wr._SIGNAL_DEDUPE_INDEX["keys"].clear()


def test_flag_is_off_by_default(wr):
    """The intake-enforcement flag defaults to OFF (observe-only)."""
    assert bool(wr.STRATEGY_ENGINE.get("enforce_alert_schema", False)) is False


def test_flag_off_invalid_alert_logged_but_processed_byte_identical(wr, monkeypatch):
    """With the flag OFF a schema-invalid alert is counted but processed exactly
    as the pre-M2 pipeline would (byte-identical record)."""
    # A schema-invalid alert (tv_signal_price normalizes to null, failing the
    # required number|string oneOf) that still matches its strategy by
    # id/symbol/timeframe, so only the schema verdict differs.
    alert = load_alert("strategy/btcusdt_buy.json")
    alert["tv_signal_price"] = "not-a-price"

    monkeypatch.setitem(wr.STRATEGY_ENGINE, "enforce_alert_schema", False)
    # Force the execution surface unavailable so the corpus strategy resolves to a
    # deterministic not_submitted outcome rather than attempting a real sandbox submit
    # -- keeps both runs byte-identical. (2-mode model: the per-strategy submit_orders
    # flag is gone, so a demo strategy would otherwise arm and submit.)
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)

    # Baseline = the schema feature effectively absent (pre-M2): validator says
    # valid, so the M2 block is a no-op.
    monkeypatch.setattr(wr, "validate_alert_schema", lambda n: (True, None))
    base_status, base_record = wr.build_record(alert, RECEIVED_AT)

    # Replay the SAME alert with the REAL validator (which reports invalid) and
    # the flag OFF. Reset dedupe so the replay isn't flagged a duplicate.
    monkeypatch.undo()
    monkeypatch.setitem(wr.STRATEGY_ENGINE, "enforce_alert_schema", False)
    # Same deterministic posture for the replay (execution surface unavailable).
    monkeypatch.setattr(wr.ExecutorFactory, "available", lambda: False)
    _reset_dedupe(wr)
    before = dict(wr.ALERT_SCHEMA_METRICS)
    off_status, off_record = wr.build_record(alert, RECEIVED_AT)

    # Byte-identical outcome despite the schema being invalid.
    assert off_status == base_status
    assert off_record == base_record
    # ...and definitely not quarantined for schema reasons.
    assert off_record.get("quarantined") is not True
    assert off_record["mode"] == "strategy_file_trial"
    # The invalid alert was counted (observe-only) but never quarantined.
    assert wr.ALERT_SCHEMA_METRICS["invalid"] == before["invalid"] + 1
    assert wr.ALERT_SCHEMA_METRICS["quarantined"] == before["quarantined"]


# --------------------------------------------------------------------------- #
# (d) flag ON: quarantined via the existing path, never processed              #
# --------------------------------------------------------------------------- #


def test_flag_on_invalid_alert_quarantined_via_existing_path(wr, monkeypatch):
    # tv_signal_price normalizes to null (as_float of a non-numeric string),
    # failing the schema's required number|string oneOf while leaving strategy
    # matching intact.
    alert = load_alert("strategy/btcusdt_buy.json")
    alert["tv_signal_price"] = "not-a-price"

    monkeypatch.setitem(wr.STRATEGY_ENGINE, "enforce_alert_schema", True)
    before = dict(wr.ALERT_SCHEMA_METRICS)
    status, record = wr.build_record(alert, RECEIVED_AT)

    # Routed to the EXISTING strategy-alert quarantine path.
    assert status == 202
    assert record["mode"] == "strategy_alert_quarantine"
    assert record["quarantined"] is True
    assert record["reason"].startswith("alert_schema_invalid:")
    # Never processed: no decision / execution surface, no strategy match.
    assert record["strategy_config"] is None
    assert "execution_readiness" not in record
    assert "okx_execution" not in record

    # Durably appended to pipeline.jsonl under the SAME quarantine stage the strategy
    # path uses (strategy-alert-quarantine was consolidated into the pipeline ledger).
    quarantine_rows = [
        json.loads(line)
        for line in wr.PIPELINE_LEDGER.read_text(encoding="utf-8").strip().splitlines()
        if line.strip() and json.loads(line).get("stage") == "quarantine"
    ]
    assert quarantine_rows, "expected a quarantine pipeline record"
    last = quarantine_rows[-1]
    assert last["mode"] == "strategy_alert_quarantine"
    assert last["reason"].startswith("alert_schema_invalid:")

    assert wr.ALERT_SCHEMA_METRICS["invalid"] == before["invalid"] + 1
    assert wr.ALERT_SCHEMA_METRICS["quarantined"] == before["quarantined"] + 1


def test_flag_on_valid_alert_still_processed(wr, monkeypatch):
    """Enforcement must not quarantine schema-valid traffic."""
    monkeypatch.setitem(wr.STRATEGY_ENGINE, "enforce_alert_schema", True)
    status, record = wr.build_record(load_alert("strategy/btcusdt_buy.json"), RECEIVED_AT)
    assert status == 200
    assert record["mode"] == "strategy_file_trial"
    assert record.get("quarantined") is not True
    assert record["strategy_config"]["strategy_id"] == "btcusdt_duo_base_dev_2h"
