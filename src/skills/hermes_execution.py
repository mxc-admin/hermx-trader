#!/usr/bin/env python3
"""Hermes execution skill runtime (REFACTOR_PLAN.md Phase 5 / P5-05).

This is the concrete runtime behind ``docs/hermes-execution.md`` -- the single
agent-facing execution surface. The agent never touches an exchange SDK; it calls
:meth:`HermesRelayAdapter.execute`, which:

  1. Normalizes ``signal`` + ``strategy`` + ``account_context`` into a controlled
     ``execution_intent`` (stable client-order-id, target direction, actions).
  2. Fails closed BEFORE any submit on an invalid signal side or an unresolved
     venue mapping / missing credentials mapping (contract: "Fail closed on
     missing credentials or unresolved venue mapping").
  3. In ``dry_run`` mode returns ``not_submitted`` WITHOUT calling the service --
     it must never submit.
  4. In ``live`` mode submits *only* through :class:`execution.ExecutionService`,
     so the kill switch, gate precedence, write-ahead journal, idempotency and
     UNKNOWN handling all run below this seam. The service's result vocabulary is
     mapped onto the skill's contract modes.

The skill owns NO money-safety policy of its own -- it delegates gates/risk to the
service path. A submit timeout/exception surfaces as ``unknown`` (never a blind
retry), matching the service + ``docs/hermes-execution.md`` failure contract.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


# Contract output modes (docs/hermes-execution.md "Output Contract").
MODE_NOT_SUBMITTED = "not_submitted"
MODE_SUBMITTED = "submitted"
MODE_FILLED = "filled"
MODE_REJECTED = "rejected"
MODE_UNKNOWN = "unknown"

VALID_MODES = ("dry_run", "live")  # accepted `mode` inputs

_SIDE_TO_DIRECTION = {"buy": "long", "sell": "short"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_symbol(value) -> str:
    text = str(value or "").strip().upper()
    return text.replace("OKX:", "").replace("/", "").replace("-", "")


def _stable_client_order_id(signal: dict, strategy: dict, role: str = "base") -> str:
    """Deterministic, idempotency-stable id (journal-first dedupe key).

    Mirrors the receiver's ``stable_client_order_id`` shape (sha256 -> ``mxc`` +
    32 chars) so the same signal yields the same id regardless of which surface
    built it -- the journal dedupe in the service path then holds across retries.
    """
    explicit = str((signal or {}).get("client_order_id") or "").strip()
    if explicit:
        return explicit
    identity = "|".join(
        str((signal or {}).get(k, ""))
        for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time", "signal_id")
    )
    digest = hashlib.sha256(f"{identity}|{role}".encode("utf-8")).hexdigest()
    return f"mxc{digest}"[:32]


def _resolve_inst_id(signal: dict, strategy: dict, account_context: dict) -> "str | None":
    """Resolve the venue instrument id from strategy/account context.

    Returns ``None`` when the venue mapping cannot be resolved so the caller can
    fail closed rather than submit against an unknown instrument.
    """
    for source in (strategy or {}, signal or {}):
        value = str((source or {}).get("inst_id") or "").strip()
        if value:
            return value
        inst = (source or {}).get("instrument")
        if isinstance(inst, dict):
            value = str(inst.get("inst_id") or "").strip()
            if value:
                return value
    symbol = _norm_symbol((signal or {}).get("symbol") or (strategy or {}).get("asset"))
    assets = (account_context or {}).get("assets") or {}
    asset_cfg = assets.get(symbol) or {}
    value = str(asset_cfg.get("inst_id") or "").strip()
    if value:
        return value
    return None


def _planned_notional(strategy: dict, account_context: dict, symbol: str) -> "float | None":
    for key in ("planned_notional_usd", "budget_usd", "notional_usd"):
        value = (strategy or {}).get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    asset_cfg = ((account_context or {}).get("assets") or {}).get(symbol) or {}
    value = asset_cfg.get("budget_usd")
    if value not in (None, ""):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def build_execution_intent(*, signal: dict, strategy: dict, account_context: dict) -> dict:
    """Build the normalized, exchange-agnostic execution intent.

    The actions are always close-verify-open ordered (``CLOSE_OPPOSITE_IF_ANY``
    then ``OPEN_<dir>``); the adapter enforces no-pyramid / reverse semantics so
    the skill never re-derives position math.
    """
    side = str((signal or {}).get("side") or (signal or {}).get("action") or "").strip().lower()
    direction = _SIDE_TO_DIRECTION.get(side, "")
    symbol = _norm_symbol((signal or {}).get("symbol") or (strategy or {}).get("asset"))
    inst_id = _resolve_inst_id(signal, strategy, account_context)
    # Distinct clOrdId per leg (close vs open) so a reversal's second leg is not rejected
    # as a duplicate by the venue. ``client_order_id`` stays the OPEN-leg id (the leg that
    # defines the final position and the journal dedupe key).
    client_order_id_close = _stable_client_order_id(signal, strategy, role="close")
    client_order_id_open = _stable_client_order_id(signal, strategy, role="open")
    client_order_id = client_order_id_open
    notional = _planned_notional(strategy, account_context, symbol)
    actions = ["CLOSE_OPPOSITE_IF_ANY", f"OPEN_{direction.upper()}"] if direction else []
    return {
        "symbol": symbol,
        "side": side,
        "target_direction": direction,
        "inst_id": inst_id,
        "policy": str((strategy or {}).get("strategy_id") or (strategy or {}).get("policy") or ""),
        "planned_notional_usd": notional,
        "leverage": (strategy or {}).get("leverage") or (account_context or {}).get("leverage"),
        "actions": actions,
        "client_order_id": client_order_id,
        "client_order_id_open": client_order_id_open,
        "client_order_id_close": client_order_id_close,
    }


def build_execution_record(
    *, signal: dict, strategy: dict, account_context: dict, intent: dict, mode: str
) -> dict:
    """Build the venue-neutral ``record`` the ExecutionService consumes.

    ``live_execution_enabled`` is only requested in ``live`` mode; even then the
    service still owns the real gate precedence (kill switch, config/risk gates,
    auth + watchdog health, symbol pause) -- this flag merely expresses intent.
    """
    execution_mode = str((strategy or {}).get("execution_mode") or "demo").lower()
    readiness = {
        "live_execution_enabled": (mode == "live"),
        "execution_mode": execution_mode,
        "simulated_trading": (execution_mode != "live"),
        "symbol": intent.get("symbol"),
        "signal_side": intent.get("side"),
        "inst_id": intent.get("inst_id"),
        "td_mode": (strategy or {}).get("td_mode") or (account_context or {}).get("td_mode"),
        "execution_intent": dict(intent),
        "okx_fill": {"client_order_id": intent.get("client_order_id")},
        "block_reason": None,
    }
    return {
        "received_at": str((signal or {}).get("received_at") or _utc_now_iso()),
        "auth_healthy": bool((account_context or {}).get("auth_healthy", True)),
        "execution_readiness": readiness,
    }


def _map_service_mode(result: dict) -> str:
    """Map the ExecutionService result vocabulary onto the skill contract modes.

    Service/adapter vocabulary -> contract:
      * ``not_submitted``                      -> ``not_submitted``
      * ``submit_timeout`` / ``submit_exception`` -> ``unknown`` (uncertain, never retried)
      * reconciled terminal state (when present) wins: FILLED/REJECTED/UNKNOWN
      * otherwise infer from ok + adapter fill status.
    """
    raw = str((result or {}).get("mode") or "")
    ok = bool((result or {}).get("ok"))

    if raw == MODE_NOT_SUBMITTED:
        return MODE_NOT_SUBMITTED
    if raw in {"submit_timeout", "submit_exception"}:
        return MODE_UNKNOWN

    recon_state = str(((result or {}).get("reconcile") or {}).get("state") or "").upper()
    recon_map = {
        "FILLED": MODE_FILLED,
        "REJECTED": MODE_REJECTED,
        "UNKNOWN": MODE_UNKNOWN,
        "SUBMITTED": MODE_SUBMITTED,
        "PLANNED": MODE_SUBMITTED,
    }
    if recon_state in recon_map:
        return recon_map[recon_state]

    fill_status = str(
        (((result or {}).get("payload") or {}).get("fill_summary") or {}).get("status") or ""
    ).lower()
    if ok:
        if fill_status in {"submitted", "partially_filled"}:
            return MODE_SUBMITTED
        return MODE_FILLED
    if fill_status in {"rejected", "blocked", "close_not_verified"}:
        return MODE_REJECTED
    # ok is False with no decisive status: stay uncertain, do not assume rejected.
    return MODE_UNKNOWN


class HermesRelayAdapter:
    """Runtime for the Hermes execution skill (the only agent-facing surface).

    The skill is constructed with a controlled ``service`` (an
    :class:`execution.ExecutionService` or any object exposing ``execute(record)``)
    and never reaches an exchange directly. ``intent_builder`` / ``record_builder``
    are injectable purely for testing; defaults are the module functions above.
    """

    def __init__(self, *, service, intent_builder=None, record_builder=None, hooks=None):
        if service is None:
            raise ValueError("HermesRelayAdapter requires a controlled execution service")
        self.service = service
        self._build_intent = intent_builder or build_execution_intent
        self._build_record = record_builder or build_execution_record
        self.hooks = hooks or {}

    def _result(
        self,
        *,
        ok: bool,
        mode: str,
        intent: dict,
        reason: "str | None" = None,
        exchange_result=None,
        extra: "dict | None" = None,
    ) -> dict:
        out = {
            "ok": bool(ok),
            "mode": mode,
            "reason": reason,
            "execution_intent": intent,
            "client_order_id": (intent or {}).get("client_order_id"),
            "exchange_result": exchange_result,
        }
        if extra:
            out.update(extra)
        return out

    def execute(
        self,
        *,
        signal: dict,
        strategy: dict,
        account_context: "dict | None" = None,
        mode: str = "dry_run",
        context: "dict | None" = None,
    ) -> dict:
        account_context = account_context or {}
        requested_mode = str(mode or "dry_run").strip().lower()

        intent = self._build_intent(
            signal=signal, strategy=strategy, account_context=account_context
        )

        # --- Fail closed BEFORE any submit ------------------------------------
        if intent.get("target_direction") not in {"long", "short"}:
            return self._result(
                ok=True,
                mode=MODE_NOT_SUBMITTED,
                intent=intent,
                reason="invalid_signal_side",
            )
        if not intent.get("inst_id"):
            # Unresolved venue mapping == fail closed (never submit blind).
            return self._result(
                ok=True,
                mode=MODE_NOT_SUBMITTED,
                intent=intent,
                reason="unresolved_venue_mapping",
            )

        # --- dry_run: build everything, submit nothing ------------------------
        if requested_mode != "live":
            return self._result(
                ok=True,
                mode=MODE_NOT_SUBMITTED,
                intent=intent,
                reason="dry_run",
            )

        # --- live: submit ONLY through the controlled service -----------------
        record = self._build_record(
            signal=signal,
            strategy=strategy,
            account_context=account_context,
            intent=intent,
            mode="live",
        )
        try:
            service_result = self.service.execute(record)
        except Exception as exc:  # timeout/transport/etc -> uncertain, never retried
            return self._result(
                ok=False,
                mode=MODE_UNKNOWN,
                intent=intent,
                reason="submit_exception",
                exchange_result={"error": type(exc).__name__},
            )

        contract_mode = _map_service_mode(service_result)
        reason = service_result.get("reason")
        if contract_mode == MODE_NOT_SUBMITTED and not reason:
            reason = "not_submitted"
        ok = bool(service_result.get("ok")) and contract_mode in {
            MODE_NOT_SUBMITTED,
            MODE_SUBMITTED,
            MODE_FILLED,
        }
        extra = {}
        if "reconcile" in (service_result or {}):
            extra["reconcile"] = service_result.get("reconcile")
        return self._result(
            ok=ok,
            mode=contract_mode,
            intent=intent,
            reason=reason,
            exchange_result=service_result,
            extra=extra or None,
        )
