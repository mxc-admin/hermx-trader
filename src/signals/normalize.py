"""Alert normalization + schema/strategy validation (Phase 1 extraction, REFACTOR_PLAN.md).

Houses the intake-normalization cluster that used to live in
webhook_receiver.py: the pure parsing helpers ``as_float`` / ``first``,
``normalize``, ``validate_strategy_alert``, and the alert-schema validation
trio ``_alert_schema_validator`` / ``_alert_schema_enforcement_status`` /
``validate_alert_schema``.

Root-bound / monkeypatchable module state STAYS defined in
webhook_receiver.py (``STRATEGIES``, ``STRATEGY_ENGINE``,
``ALERT_SCHEMA_PATH``, ``_ALERT_SCHEMA_UNENFORCEABLE_ALERTED``): tests
monkeypatch/rebind them on ``wr`` (test_intake_hardening rebinds
STRATEGY_ENGINE and the per-process alert flag; test_phase_a_robustness
injects into wr.STRATEGIES), and the per-test
``importlib.reload(webhook_receiver)`` in conftest must reset them. The
functions here read that state lazily via ``import webhook_receiver as _wr``
-- the same pattern as src/alerts.py (Phase 0). For the same reason the two
schema-status functions resolve ``_alert_schema_validator`` through
``_wr._alert_schema_validator()``: tests rebind that seam on ``wr``
(test_intake_hardening), and pre-extraction every internal call already
resolved through the receiver's module namespace.

webhook_receiver re-exports every public/underscore name here so ``wr.<fn>``
call sites and monkeypatch seams keep working.
"""
from __future__ import annotations

import hashlib
import json
import logging

from hermx_shared import canonical_timeframe
from webhook.timeutil import now_iso
from alerts import emit_operator_alert


def as_float(value):
    """Best-effort float coercion for generic intake parsing (None/'' -> None)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first(payload: dict, *names: str, default=""):
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return default


def normalize(payload: dict) -> dict:
    strategy_id = str(first(payload, "strategy_id", "strategyId", default="")).strip()
    strategy_name = str(first(payload, "strategy_name", "strategyName", default="")).strip()
    indicator = str(first(payload, "indicator", "indicator_name", "indicatorName", default="")).strip()
    symbol = str(first(payload, "symbol", "ticker", default="")).upper()
    symbol = symbol.replace("OKX:", "").replace("/", "").replace("-", "")
    # `action` is the single canonical intent field on the normalized output.
    # Legacy alerts send only the raw `side` input (buy|sell); `action` adds `close`.
    # The raw `side` input is still read here purely to derive `action`, but it is no
    # longer echoed onto the output dict. When both raw fields are present the conflict
    # gate in build_record catches opposing open sides.
    raw_action = str(first(payload, "action", default="") or "").lower().strip()
    raw_side = str(first(payload, "side", default="") or "").lower().strip()
    _valid_open = {"buy", "sell"}
    _valid_action = {"buy", "sell", "close"}
    if raw_action in _valid_action:
        action = raw_action
    elif raw_side in _valid_open:
        action = raw_side          # derive action from side for legacy alerts
    else:
        action = raw_action or raw_side   # preserve for error reporting
    timeframe = canonical_timeframe(first(payload, "timeframe", "interval", default="30m"))
    tv_time = str(first(payload, "tv_time", "time", "timestamp", "bar_time", "candle_time", default=now_iso()))
    signal_id = str(first(payload, "signal_id", default=""))
    if not signal_id:
        # Hash on `action` (not `side`): buy/sell are unchanged (action==side), while
        # a `close` bar gets a deterministic id instead of collapsing to an empty side.
        raw = f"{strategy_id}|{symbol}|{action}|{timeframe}|{tv_time}"
        signal_id = hashlib.sha256(raw.encode()).hexdigest()
    normalized = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "indicator": indicator,
        "symbol": symbol,
        "action": action,
        "timeframe": timeframe,
        "tv_signal_price": as_float(first(payload, "tv_signal_price", "tv_close", "signal_price", "price", "close", default=None)),
        "chart_type": (lambda c: str(c).lower() if c else None)(first(payload, "chart_type", default=None)),
        "okx_mark_price": as_float(first(payload, "okx_mark_price", "mark_price", default=None)),
        "okx_last_price": as_float(first(payload, "okx_last_price", "last_price", default=None)),
        "tv_time": tv_time,
        "exchange": str(first(payload, "exchange", default="okx")).lower(),
        "strategy": str(first(payload, "strategy", default="")),
        "source": str(first(payload, "source", default="tradingview")),
        "signal_id": signal_id,
    }
    # Optional observe-only debugging context. Only carried when it is a dict --
    # never inject ``extras: None``, which would fail the schema's ``object`` type
    # check (schema key ``extras`` is an object) and reject otherwise-valid alerts.
    extras = payload.get("extras")
    if isinstance(extras, dict):
        normalized["extras"] = extras
    return normalized


def validate_strategy_alert(normalized: dict) -> tuple[bool, dict | None, str | None]:
    import webhook_receiver as _wr
    strategy_id = str(normalized.get("strategy_id") or "").strip()
    if not strategy_id:
        if bool(_wr.STRATEGY_ENGINE.get("require_strategy_id", False)):
            return False, None, "missing_strategy_id_required"
        indicator = str(normalized.get("indicator") or "").lower()
        strategy_name = str(normalized.get("strategy_name") or normalized.get("strategy") or "").lower()
        if "duo-base" in indicator or "duo base" in indicator or "duo-base" in strategy_name or "duo base" in strategy_name:
            return False, None, "missing_strategy_id"
        return True, None, None
    if not bool(_wr.STRATEGY_ENGINE.get("allow_strategy_alerts", True)):
        return False, None, "strategy_alerts_disabled"
    strategy = _wr.STRATEGIES.get(strategy_id)
    if not strategy:
        return False, None, "unknown_strategy_id"
    if str(strategy.get("asset") or "").upper() != str(normalized.get("symbol") or "").upper():
        return False, strategy, "strategy_symbol_mismatch"
    if canonical_timeframe(strategy.get("timeframe")) != canonical_timeframe(normalized.get("timeframe")):
        return False, strategy, "strategy_timeframe_mismatch"
    # Every strategy file with a matching symbol+timeframe is active; the per-strategy
    # execution_mode (demo/live) decides sandbox vs real venue, not whether it submits.
    return True, strategy, None


# --------------------------------------------------------------------------- #
# Phase 6 / M2: explicit alert-schema validation at intake.                    #
#                                                                              #
# The schema is validated against the *normalized* alert (canonical snake_case #
# keys, lowercased exchange/source, uppercased symbol) so a raw payload's      #
# casing/aliasing (strategyId/ticker/action) never causes false rejections.    #
# jsonschema is loaded lazily and cached; if it (or the schema file) is        #
# unavailable we fail OPEN -- we never quarantine traffic we cannot evaluate.  #
# Enforcement is gated by strategy_engine.enforce_alert_schema (default OFF).  #
# --------------------------------------------------------------------------- #

_ALERT_SCHEMA_VALIDATOR = None
_ALERT_SCHEMA_LOAD_FAILED = False


def _alert_schema_validator():
    """Lazily build and cache the Draft 2020-12 validator for the alert schema.

    Returns None (and disables enforcement) if jsonschema or the schema file is
    unavailable -- import stays side-effect-free and enforcement fails open.
    """
    global _ALERT_SCHEMA_VALIDATOR, _ALERT_SCHEMA_LOAD_FAILED
    import webhook_receiver as _wr
    if _ALERT_SCHEMA_VALIDATOR is not None:
        return _ALERT_SCHEMA_VALIDATOR
    if _ALERT_SCHEMA_LOAD_FAILED:
        return None
    try:
        import jsonschema  # lazy: keep module import dependency-light

        schema = json.loads(_wr.ALERT_SCHEMA_PATH.read_text(encoding="utf-8"))
        _ALERT_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(schema)
        return _ALERT_SCHEMA_VALIDATOR
    except Exception as exc:  # fail open: cannot enforce what we cannot load
        logging.warning("alert schema unavailable; schema enforcement disabled: %s", exc)
        _ALERT_SCHEMA_LOAD_FAILED = True
        return None


def _alert_schema_enforcement_status() -> tuple[bool, bool]:
    """Return (armed, enforceable) for alert-schema enforcement.

    ``armed``       = strategy_engine.enforce_alert_schema is true.
    ``enforceable`` = an alert-schema validator is actually available.

    Armed-but-not-enforceable is a fail-open-WHILE-ARMED safety hole: the operator
    believes intake is guarded but validation silently passes everything. Emit a deduped
    error-severity operator alert the first time we observe it."""
    import webhook_receiver as _wr
    armed = bool(_wr.STRATEGY_ENGINE.get("enforce_alert_schema", False))
    enforceable = _wr._alert_schema_validator() is not None
    if armed and not enforceable and not _wr._ALERT_SCHEMA_UNENFORCEABLE_ALERTED:
        _wr._ALERT_SCHEMA_UNENFORCEABLE_ALERTED = True
        logging.error(
            "enforce_alert_schema is ARMED but the alert-schema validator is UNAVAILABLE; "
            "intake schema validation is failing OPEN."
        )
        emit_operator_alert(
            "ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE",
            {"detail": "enforce_alert_schema=true but the alert-schema validator is unavailable; "
                       "alert validation is failing OPEN (every alert passes unchecked)."},
            severity="error",
        )
    return armed, enforceable


def validate_alert_schema(normalized: dict) -> tuple[bool, str | None]:
    """Validate a normalized alert against the TradingView alert JSON schema.

    Returns (True, None) when valid or when the schema/jsonschema is unavailable
    (fail open). On failure returns (False, "<path>: <message>") for the first
    error in deterministic path order.
    """
    import webhook_receiver as _wr
    validator = _wr._alert_schema_validator()
    if validator is None:
        return True, None
    errors = sorted(validator.iter_errors(normalized), key=lambda e: list(e.path))
    if not errors:
        return True, None
    first = errors[0]
    loc = "/".join(str(p) for p in first.path) or "(root)"
    return False, f"{loc}: {first.message}"
