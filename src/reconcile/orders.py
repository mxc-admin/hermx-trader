"""The OKX v5 order-status fallback chain, bounded backoff, and startup reconcile
bootstrap (REFACTOR_PLAN.md Phase 5).

RECONCILE_STARTUP_COMPLETE / RECONCILE_STARTUP_AT stay defined in webhook_receiver.py
(read by log_execution_arm_state there, and asserted directly as wr.RECONCILE_STARTUP_
COMPLETE / wr.RECONCILE_STARTUP_AT by tests) -- reconcile_startup mutates them via
module-attribute assignment on the imported webhook_receiver module object (the exact
external-observable effect a `global` statement would have had, but reachable from a
different module).
"""
from __future__ import annotations

import time

from orders.journal import (
    ORDER_STATE_UNKNOWN,
    ORDER_STATE_FILLED,
    ORDER_STATE_REJECTED,
    ORDER_STATE_SUBMITTED,
    ORDER_TERMINAL_STATES,
    order_state_can_transition,
    load_open_orders,
    record_order_state,
)
from webhook.timeutil import now_iso
from reconcile.executor_select import _executor_for_order
from reconcile.alerts import emit_reconcile_alert, RECONCILE_ALERT_MISMATCH

# Raw OKX order states that mean "the order genuinely exists on the venue". Anything
# else returned by the query layer (not_found / error / not_implemented / unknown /
# empty) is treated as "not present here" so the fallback chain keeps searching.
_PRESENT_ORDER_STATES = frozenset({"live", "partially_filled", "filled", "canceled"})

# Bounded exponential backoff (:213): max 5 attempts, 500ms base, ~8s cap, <=~20s wall.
RECONCILE_MAX_ATTEMPTS = 5
RECONCILE_BASE_DELAY_SECONDS = 0.5
RECONCILE_CAP_DELAY_SECONDS = 8.0
RECONCILE_WALL_CLOCK_BUDGET_SECONDS = 20.0
RECONCILE_HISTORY_LIMIT = 100


def _reconcile_float(value, default=0.0):
    """Tolerant float coercion for normalized query fields (PURE)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def map_order_outcome(order: "dict | None", ordered: "float | None" = None) -> tuple:
    """PURE: map a normalized order (or None) to a reconciliation outcome.

    Returns ``(state, partial, reason)`` per the :211-:212 mapping rules:
      * state=partially_filled OR (0 < accFillSz < ordered) -> FILLED, partial=True
      * state=filled (and not a known partial)              -> FILLED, partial=False
      * state=canceled with accFillSz=0                      -> REJECTED (canceled)
      * state=canceled with accFillSz>0                      -> FILLED, partial=True
      * not-found (absent from get_order/pending/archive)    -> UNKNOWN (not_found)
      * state=live (non-terminal)                            -> SUBMITTED (keep polling)
      * any other / inconclusive                             -> UNKNOWN

    MONEY-SAFETY: absence is NEVER an auto-rejection. A not_found order may have filled
    and aged out of the queryable windows, or the query layer may be transiently
    failing; concluding REJECTED there would drop a live position as flat. Absence maps
    to UNKNOWN so the order stays tracked (backoff re-polls it; the periodic resolver +
    lifecycle backstop chase it). The ONLY venue-confirmed rejection is canceled with
    zero fill.
    """
    if order is None:
        return ORDER_STATE_UNKNOWN, False, "not_found"
    state = str(order.get("state") or "").lower()
    acc = _reconcile_float(order.get("acc_fill_sz"), 0.0)
    is_partial_by_size = ordered is not None and ordered > 0 and 0.0 < acc < ordered
    if state == "not_found":
        return ORDER_STATE_UNKNOWN, False, "not_found"
    if state == "partially_filled":
        return ORDER_STATE_FILLED, True, "partially_filled"
    if state == "filled":
        if is_partial_by_size:
            return ORDER_STATE_FILLED, True, "partial_by_size"
        return ORDER_STATE_FILLED, False, "filled"
    if state == "canceled":
        if acc > 0.0:
            return ORDER_STATE_FILLED, True, "canceled_after_partial_fill"
        # canceled_zero_fill is the ONLY venue-confirmed rejection -- but only when the
        # zero is REAL. A missing/malformed acc_fill_sz must NOT be coerced to 0 and
        # rejected: a canceled-after-partial would then be dropped as flat. Keep it
        # UNKNOWN (report-driven reconcile) so the backoff/resolver chases the true size.
        if _reconcile_float(order.get("acc_fill_sz"), None) is None:
            return ORDER_STATE_UNKNOWN, False, "canceled_fill_size_unavailable"
        return ORDER_STATE_REJECTED, False, "canceled_zero_fill"
    if state == "live":
        # Non-terminal: still working. partial flag only informs the caller's logging.
        return ORDER_STATE_SUBMITTED, is_partial_by_size, "live"
    # error / not_implemented / unknown / empty -> inconclusive, keep it UNKNOWN.
    return ORDER_STATE_UNKNOWN, False, f"inconclusive:{state or 'empty'}"


def _order_is_present(order: "dict | None") -> bool:
    """A normalized order genuinely exists on the venue (vs not_found/error/...)."""
    return isinstance(order, dict) and str(order.get("state") or "").lower() in _PRESENT_ORDER_STATES


def _order_matches(order: dict, ord_id: "str | None", cl_ord_id: "str | None") -> bool:
    """Does a list-returned (pending/archive) order match the keys we are chasing?
    Match by ordId or clOrdId when provided; with neither, accept the first present
    order for the instrument."""
    if not _order_is_present(order):
        return False
    if ord_id and order.get("ord_id") == ord_id:
        return True
    if cl_ord_id and order.get("cl_ord_id") == cl_ord_id:
        return True
    return not ord_id and not cl_ord_id


def reconcile_order_once(executor, lookup: dict) -> dict:
    """One pass of the OKX v5 fallback chain (:209):
       1. GET /trade/order              (instId + ordId preferred, else clOrdId)
       2. GET /trade/orders-pending     (instId) if 1 returns not-found
       3. GET /trade/orders-history-archive (instId, limit) if still absent
    Returns the normalized outcome dict consumed by the backoff driver."""
    inst_id = lookup.get("inst_id")
    ord_id = lookup.get("ord_id")
    cl_ord_id = lookup.get("cl_ord_id")
    ordered = lookup.get("ordered")
    limit = int(lookup.get("history_limit") or RECONCILE_HISTORY_LIMIT)

    matched: "dict | None" = None
    source: "str | None" = None

    if inst_id:
        order = executor.get_order(inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id)
        if _order_is_present(order):
            matched, source = order, "get_order"
    if matched is None and inst_id:
        for cand in executor.get_open_orders(inst_id) or []:
            if _order_matches(cand, ord_id, cl_ord_id):
                matched, source = cand, "orders_pending"
                break
    if matched is None and inst_id:
        for cand in executor.get_order_history_archive(inst_id, limit=limit) or []:
            if _order_matches(cand, ord_id, cl_ord_id):
                matched, source = cand, "orders_history_archive"
                break

    state, partial, reason = map_order_outcome(matched, ordered=ordered)
    return {
        "state": state,
        "partial": partial,
        "reason": reason,
        "matched_order": matched,
        "source": source,
        "acc_fill_sz": _reconcile_float((matched or {}).get("acc_fill_sz"), 0.0),
        "avg_px": (matched or {}).get("avg_px") if matched else None,
        "ord_id": (matched or {}).get("ord_id") if matched else ord_id,
        "cl_ord_id": (matched or {}).get("cl_ord_id") if matched else cl_ord_id,
    }


def reconcile_order_with_backoff(
    executor,
    lookup: dict,
    *,
    max_attempts: int = RECONCILE_MAX_ATTEMPTS,
    base_delay: float = RECONCILE_BASE_DELAY_SECONDS,
    cap_delay: float = RECONCILE_CAP_DELAY_SECONDS,
    wall_clock_budget: float = RECONCILE_WALL_CLOCK_BUDGET_SECONDS,
    sleep=time.sleep,
    clock=time.time,
) -> dict:
    """Bounded exponential-backoff reconciliation (:213). Terminal outcomes
    (FILLED/REJECTED, incl. not_found) return immediately; a non-terminal (live)
    order is re-polled with delays 0.5s,1s,2s,4s (capped 8s). When the attempt or
    wall-clock bound is exhausted while still non-terminal, the outcome is UNKNOWN
    and a RECONCILE_MISMATCH is the caller's responsibility. ``sleep``/``clock`` are
    injectable so tests exercise the bound with no real waiting. The submission is
    NEVER retried -- only the read-only status query is."""
    start = clock()
    last: "dict | None" = None
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        last = reconcile_order_once(executor, lookup)
        if last["state"] in ORDER_TERMINAL_STATES:
            last["attempts"] = attempts
            last["elapsed_s"] = round(clock() - start, 3)
            return last
        if attempts >= max_attempts:
            break
        delay = min(cap_delay, base_delay * (2 ** attempt))
        if (clock() - start) + delay > wall_clock_budget:
            break
        sleep(delay)

    outcome = dict(last) if last else {
        "matched_order": None, "source": None, "acc_fill_sz": 0.0, "avg_px": None,
        "ord_id": lookup.get("ord_id"), "cl_ord_id": lookup.get("cl_ord_id"), "partial": False,
    }
    prior_reason = (last or {}).get("reason") or "no_result"
    outcome["state"] = ORDER_STATE_UNKNOWN
    outcome["reason"] = f"deadline_exhausted:{prior_reason}"
    outcome["attempts"] = attempts
    outcome["elapsed_s"] = round(clock() - start, 3)
    return outcome


def reconcile_startup(executor=None) -> dict:
    """One-time startup reconcile bootstrap (:215, acceptance :236). OBSERVE-ONLY:
    reconcile every still-open order (load_open_orders) against the exchange and,
    where the venue reports a terminal outcome and the journal state legally allows
    it, write the authoritative terminal transition.

    Sets RECONCILE_STARTUP_COMPLETE + RECONCILE_STARTUP_AT for FUTURE enforcement; it
    does NOT auto-trade and does NOT hard-block submission in this task. ``summary``
    keeps an (always-empty) ``position_mismatches`` list for backward-compatible shape.
    Returns a summary dict (also useful for tests)."""
    import logging
    import webhook_receiver as _wr
    # When a caller passes an executor (tests / injected), use it for every order
    # (unchanged behavior). In production (executor is None) resolve a per-order
    # executor from each order's persisted (venue, mode) so a Bybit-live order is
    # checked on Bybit-live, not OKX-demo (#20a). ``default_executor`` is the OKX-demo
    # fallback for legacy orders that predate venue/mode persistence.
    explicit_executor = executor is not None
    default_executor = executor if explicit_executor else _wr._reconciliation_executor()
    _exec_cache: dict = {}
    summary = {"open_orders": [], "position_mismatches": [], "executor_available": default_executor is not None, "errors": []}

    if default_executor is not None:
        try:
            open_orders = load_open_orders()
        except Exception as exc:  # pragma: no cover - tolerant
            open_orders = []
            summary["errors"].append(f"load_open_orders: {exc}")
        for rec in open_orders:
            cl = rec.get("cl_ord_id")
            cur_state = rec.get("state")
            intent = rec.get("intent") or {}
            lookup = {"inst_id": intent.get("inst_id"), "cl_ord_id": cl}
            order_executor = default_executor if explicit_executor else _executor_for_order(intent, _exec_cache, default_executor)
            if order_executor is None:
                summary["errors"].append(f"executor_unavailable[{cl}]")
                continue
            try:
                outcome = reconcile_order_once(order_executor, lookup)
            except Exception as exc:  # pragma: no cover - tolerant
                summary["errors"].append(f"reconcile_order_once[{cl}]: {exc}")
                continue
            recon_state = outcome["state"]
            wrote = False
            # Observe-only: only persist a LEGAL terminal transition (e.g. SUBMITTED/
            # UNKNOWN -> FILLED/REJECTED). PLANNED->FILLED etc. is illegal and skipped.
            if recon_state in ORDER_TERMINAL_STATES and order_state_can_transition(cur_state, recon_state):
                try:
                    record_order_state(
                        cl, recon_state, intent=intent,
                        detail={
                            "startup_reconcile": True, "reason": outcome["reason"], "source": outcome["source"],
                            "acc_fill_sz": outcome["acc_fill_sz"], "avg_px": outcome["avg_px"],
                        },
                        prev_state=cur_state,
                    )
                    wrote = True
                except (ValueError, OSError) as exc:  # pragma: no cover - tolerant
                    summary["errors"].append(f"record_order_state[{cl}]: {exc}")
            if recon_state != cur_state and not (recon_state in ORDER_TERMINAL_STATES and wrote):
                # Non-persisted divergence (e.g. still UNKNOWN) is still worth alerting.
                emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {
                    "stage": "startup_open_order", "cl_ord_id": cl,
                    "journal_state": cur_state, "reconciled_state": recon_state, "reason": outcome["reason"],
                })
            summary["open_orders"].append({
                "cl_ord_id": cl, "from": cur_state, "outcome": recon_state,
                "reason": outcome["reason"], "wrote_transition": wrote,
            })

    _wr.RECONCILE_STARTUP_COMPLETE = True
    _wr.RECONCILE_STARTUP_AT = now_iso()
    logging.info(
        "RECONCILE_STARTUP_COMPLETE at=%s executor_available=%s open_orders=%d position_mismatches=%d errors=%d",
        _wr.RECONCILE_STARTUP_AT, summary["executor_available"], len(summary["open_orders"]),
        len(summary["position_mismatches"]), len(summary["errors"]),
    )
    return summary
