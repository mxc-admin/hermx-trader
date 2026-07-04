"""Periodic SUBMITTED/UNKNOWN/PLANNED order resolver (REFACTOR_PLAN.md Phase 5,
Task 6).

UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS, UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK,
UNKNOWN_RESOLVER_INTERVAL_SECONDS, and PLANNED_ORDER_TIMEOUT_SECONDS stay defined
in webhook_receiver.py (tests monkeypatch them directly on wr, and
unknown_resolver_enabled()/_set_resolver_heartbeat() -- which also read/write
wr-resident state -- stay there too), so they are read lazily via `import
webhook_receiver as _wr`.

reconcile_order_with_backoff and resolve_unknown_orders_once are ALSO monkeypatched
directly on wr by tests, which expect callers -- including unknown_resolver_loop,
in THIS SAME module, calling resolve_unknown_orders_once -- to observe the patch.
Both are therefore dereferenced through `_wr.` at call time rather than called
directly, mirroring the _reconciliation_executor/_executor_for_order pattern in
reconcile.executor_select.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from alerts import emit_operator_alert
from webhook.timeutil import parse_tv_time
from control_state import pause_symbol
from orders.journal import (
    ORDER_STATE_PLANNED,
    ORDER_STATE_SUBMITTED,
    ORDER_STATE_UNKNOWN,
    ORDER_STATE_REJECTED,
    ORDER_TERMINAL_STATES,
    order_state_can_transition,
    load_open_orders,
    record_order_state,
)
from reconcile.executor_select import _executor_for_order
from reconcile.orders import reconcile_order_once, _order_is_present
from reconcile.alerts import (
    emit_reconcile_alert,
    RECONCILE_ALERT_MISMATCH,
    RECONCILE_ALERT_RESOLVER_TIMEOUT,
    RECONCILE_ALERT_PLANNED_ABANDONED,
    RECONCILE_ALERT_PLANNED_ON_VENUE,
)


def _order_age_seconds(order_record: dict, now_ts: "str | None" = None) -> "float | None":
    order_ts = parse_tv_time(order_record.get("ts"))
    if order_ts is None:
        return None
    now_dt = parse_tv_time(now_ts) if now_ts else datetime.now(timezone.utc)
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    return max(0.0, (now_dt - order_ts).total_seconds())


def _resolve_planned_orphan(executor, rec: dict, lookup: dict, age_seconds: "float | None", summary: dict) -> None:
    """Lifecycle backstop for a crash-orphaned PLANNED order (closes the gap where a
    PLANNED order could never be resolved: the SUBMITTED/UNKNOWN resolver excluded it and
    PLANNED->UNKNOWN is illegal).

    Write-ahead ordering guarantees SUBMITTED is journalled BEFORE executor.execute() is
    called, so a record stuck at PLANNED crashed BEFORE the submit -- it was never sent.
    Once it is older than PLANNED_ORDER_TIMEOUT_SECONDS we confirm the venue has no record
    (single read-only pass, no backoff sleeps) and then take the LEGAL PLANNED->REJECTED
    transition with reason ``never_submitted`` + an operator alert. Idempotency is
    preserved: the rejected record is terminal, so the deterministic cl_ord_id stays
    deduped. A still-fresh PLANNED order may be an in-process submit between the two
    write-ahead writes, so it is left untouched. OBSERVE-ONLY: never submits/cancels."""
    import webhook_receiver as _wr
    cl_ord_id = rec.get("cl_ord_id")
    intent = rec.get("intent") or {}
    symbol = intent.get("symbol")

    if age_seconds is None or age_seconds <= _wr.PLANNED_ORDER_TIMEOUT_SECONDS:
        summary["pending"] += 1  # still within the in-flight submit window
        return

    try:
        outcome = reconcile_order_once(executor, lookup)
    except Exception as exc:  # pragma: no cover - defensive
        summary["errors"].append(f"reconcile_planned[{cl_ord_id}]: {exc}")
        emit_operator_alert(
            "PLANNED_RESOLVER_ERROR",
            {"cl_ord_id": cl_ord_id, "symbol": symbol, "error": str(exc)},
            severity="error",
        )
        return

    if _order_is_present(outcome.get("matched_order")):
        # ANOMALY: the venue knows an order we believe was never sent. Do NOT reject --
        # promote PLANNED->SUBMITTED (legal) so the standard reconciliation resolves it,
        # and alert loudly.
        if order_state_can_transition(ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED):
            try:
                record_order_state(
                    cl_ord_id,
                    ORDER_STATE_SUBMITTED,
                    intent=intent,
                    detail={"planned_backstop": True, "reason": "planned_found_on_venue", "source": outcome.get("source")},
                    prev_state=ORDER_STATE_PLANNED,
                )
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_planned_submitted[{cl_ord_id}]: {exc}")
        emit_operator_alert(
            RECONCILE_ALERT_PLANNED_ON_VENUE,
            {"cl_ord_id": cl_ord_id, "symbol": symbol, "age_s": round(age_seconds, 3), "source": outcome.get("source")},
            severity="error",
        )
        summary["pending"] += 1
        return

    # Venue has no record -> never submitted. Legal PLANNED -> REJECTED, idempotency-safe.
    if order_state_can_transition(ORDER_STATE_PLANNED, ORDER_STATE_REJECTED):
        try:
            record_order_state(
                cl_ord_id,
                ORDER_STATE_REJECTED,
                intent=intent,
                detail={
                    "planned_backstop": True,
                    "reason": "never_submitted",
                    "age_s": round(age_seconds, 3),
                    "timeout_s": _wr.PLANNED_ORDER_TIMEOUT_SECONDS,
                },
                prev_state=ORDER_STATE_PLANNED,
            )
            summary["resolved"] += 1
            summary["never_submitted"] += 1
            emit_operator_alert(
                RECONCILE_ALERT_PLANNED_ABANDONED,
                {
                    "cl_ord_id": cl_ord_id,
                    "symbol": symbol,
                    "age_s": round(age_seconds, 3),
                    "timeout_s": _wr.PLANNED_ORDER_TIMEOUT_SECONDS,
                    "reason": "never_submitted",
                },
                severity="warning",
            )
        except (ValueError, OSError) as exc:
            summary["errors"].append(f"record_planned_rejected[{cl_ord_id}]: {exc}")
    else:  # pragma: no cover - defensive, PLANNED->REJECTED is always legal
        summary["pending"] += 1


def resolve_unknown_orders_once(executor=None, *, now_ts: "str | None" = None, max_orders: "int | None" = None) -> dict:
    """Task 6 periodic resolver pass for open SUBMITTED/UNKNOWN orders.

    Re-runs reconciliation until terminal or per-order timeout budget expiry. On
    budget expiry emits alerts and persists a per-symbol pause artifact.
    """
    import webhook_receiver as _wr
    # Per-order (venue, mode) executor resolution mirrors reconcile_startup (#20a): an
    # explicitly-passed executor is used for every order; otherwise each order is checked
    # on the account it was submitted to, with default_executor as the OKX-demo fallback.
    explicit_executor = executor is not None
    default_executor = executor if explicit_executor else _wr._reconciliation_executor()
    _exec_cache: dict = {}
    summary = {
        "checked": 0,
        "resolved": 0,
        "pending": 0,
        "expired": 0,
        "never_submitted": 0,
        "paused_symbols": [],
        "errors": [],
        "executor_available": default_executor is not None,
    }
    if default_executor is None:
        return summary

    limit = max_orders if max_orders is not None else _wr.UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK
    candidates = [
        rec
        for rec in load_open_orders()
        if rec.get("state") in {ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED, ORDER_STATE_UNKNOWN}
    ]
    candidates.sort(key=lambda r: r.get("seq", 0))
    for rec in candidates[: max(0, int(limit))]:
        summary["checked"] += 1
        cl_ord_id = rec.get("cl_ord_id")
        cur_state = rec.get("state")
        intent = rec.get("intent") or {}
        symbol = intent.get("symbol")
        # Age from ORIGIN (first journal record), not the latest -- re-recording must not
        # reset the lifecycle clock. Falls back to the latest ts if origin is missing.
        age_seconds = _order_age_seconds({"ts": rec.get("origin_ts") or rec.get("ts")}, now_ts=now_ts)
        lookup = {"inst_id": intent.get("inst_id"), "cl_ord_id": cl_ord_id}
        order_executor = default_executor if explicit_executor else _executor_for_order(intent, _exec_cache, default_executor)
        if order_executor is None:
            summary["errors"].append(f"executor_unavailable[{cl_ord_id}]")
            continue

        if cur_state == ORDER_STATE_PLANNED:
            _resolve_planned_orphan(order_executor, rec, lookup, age_seconds, summary)
            continue

        if age_seconds is not None and age_seconds > _wr.UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS:
            # Lifecycle backstop: alert + pause the symbol, but NEVER auto-close the order
            # (no terminal write -- absence/ambiguity is not proof of any outcome). Dedupe
            # so one stuck order does not re-pause/re-alert every tick: the pause reason is
            # STABLE per (symbol, cl_ord_id, state), so pause_symbol() collapses repeats and
            # only a genuinely NEW pause emits the operator alerts. A symbol-less order
            # cannot be deduped via the pause store, so it always alerts (never swallowed).
            summary["expired"] += 1
            sym_norm = str(symbol or "").strip()
            pause_reason = (
                f"unknown resolver timeout: order {cl_ord_id} stuck {cur_state} "
                f"> {_wr.UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS}s"
            )
            newly_paused = pause_symbol(symbol, pause_reason) if sym_norm else False
            if newly_paused:
                summary["paused_symbols"].append(sym_norm)
            if newly_paused or not sym_norm:
                emit_reconcile_alert(
                    RECONCILE_ALERT_MISMATCH,
                    {
                        "stage": "unknown_resolver_timeout",
                        "cl_ord_id": cl_ord_id,
                        "symbol": symbol,
                        "state": cur_state,
                        "age_s": round(age_seconds, 3),
                        "timeout_s": _wr.UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS,
                        "reason": pause_reason,
                    },
                )
                emit_operator_alert(
                    RECONCILE_ALERT_RESOLVER_TIMEOUT,
                    {
                        "cl_ord_id": cl_ord_id,
                        "symbol": symbol,
                        "state": cur_state,
                        "age_s": round(age_seconds, 3),
                        "timeout_s": _wr.UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS,
                    },
                    severity="error",
                )
            continue

        try:
            outcome = _wr.reconcile_order_with_backoff(order_executor, lookup)
        except Exception as exc:  # pragma: no cover - defensive
            summary["errors"].append(f"reconcile[{cl_ord_id}]: {exc}")
            emit_operator_alert(
                "UNKNOWN_RESOLVER_ERROR",
                {"cl_ord_id": cl_ord_id, "symbol": symbol, "error": str(exc)},
                severity="error",
            )
            continue

        next_state = outcome.get("state")
        if next_state in ORDER_TERMINAL_STATES and order_state_can_transition(cur_state, next_state):
            try:
                record_order_state(
                    cl_ord_id,
                    next_state,
                    intent=intent,
                    detail={
                        "unknown_resolver": True,
                        "reason": outcome.get("reason"),
                        "source": outcome.get("source"),
                        "attempts": outcome.get("attempts"),
                        "elapsed_s": outcome.get("elapsed_s"),
                    },
                    prev_state=cur_state,
                )
                summary["resolved"] += 1
                continue
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_order_state[{cl_ord_id}]: {exc}")

        # Record the SUBMITTED->UNKNOWN transition ONCE. An already-UNKNOWN order that
        # re-resolves to UNKNOWN is NOT re-recorded: a no-op state change would only bloat
        # the journal, and the backstop measures age from origin_ts (not the latest record)
        # so re-recording would buy nothing.
        if (
            next_state == ORDER_STATE_UNKNOWN
            and cur_state != ORDER_STATE_UNKNOWN
            and order_state_can_transition(cur_state, ORDER_STATE_UNKNOWN)
        ):
            try:
                record_order_state(
                    cl_ord_id,
                    ORDER_STATE_UNKNOWN,
                    intent=intent,
                    detail={
                        "unknown_resolver": True,
                        "reason": outcome.get("reason"),
                        "source": outcome.get("source"),
                        "attempts": outcome.get("attempts"),
                        "elapsed_s": outcome.get("elapsed_s"),
                    },
                    prev_state=cur_state,
                )
                cur_state = ORDER_STATE_UNKNOWN
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_unknown[{cl_ord_id}]: {exc}")

        emit_reconcile_alert(
            RECONCILE_ALERT_MISMATCH,
            {
                "stage": "unknown_resolver_pending",
                "cl_ord_id": cl_ord_id,
                "symbol": symbol,
                "journal_state": cur_state,
                "reconciled_state": next_state,
                "reason": outcome.get("reason"),
                "attempts": outcome.get("attempts"),
            },
        )
        summary["pending"] += 1
    return summary


def unknown_resolver_loop(stop_event: "threading.Event | None" = None, sleep=time.sleep) -> None:
    import webhook_receiver as _wr
    # INTERVAL_SECONDS <= 0 disables the resolver. Short-circuit BEFORE the max(1.0, ...)
    # floor below so 0 means "off", not "poll every 1s".
    if _wr.UNKNOWN_RESOLVER_INTERVAL_SECONDS <= 0:
        return
    interval = max(1.0, _wr.UNKNOWN_RESOLVER_INTERVAL_SECONDS)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        _wr._set_resolver_heartbeat()
        try:
            summary = _wr.resolve_unknown_orders_once()
            if summary["checked"] or summary["expired"] or summary["errors"]:
                logging.info(
                    "UNKNOWN resolver tick checked=%d resolved=%d pending=%d expired=%d never_submitted=%d errors=%d",
                    summary["checked"],
                    summary["resolved"],
                    summary["pending"],
                    summary["expired"],
                    summary.get("never_submitted", 0),
                    len(summary["errors"]),
                )
        except Exception as exc:  # pragma: no cover - defensive
            emit_operator_alert("UNKNOWN_RESOLVER_ERROR", {"error": str(exc)}, severity="error")

        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            sleep(interval)
