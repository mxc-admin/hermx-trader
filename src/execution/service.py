#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from pathlib import Path

# Canonical strategy execution modes (mirrors schemas/strategy.schema.json). Anything
# else is a config typo and must fail closed, never route a submit. 'demo'/'paper'/
# 'shadow' are sandbox-only; 'live' is the ONLY mode permitted to reach a real venue,
# and only with the global HERMX_LIVE_TRADING kill switch armed.
CANONICAL_EXECUTION_MODES = frozenset({"demo", "paper", "shadow", "live"})


def resolve_execution_config(config: dict, readiness: dict | None = None) -> dict:
    """PURE: the execution config the write/reconcile path actually resolves.

    Two independent selectors are applied:

    * ``execution.exchange`` selects the *adapter class* (post CCXT cutover this is
      ``ccxt``); an explicit ``HERMX_EXEC_BACKEND`` overrides it, honored identically
      by submit and reconcile so the two can never diverge.
    * ``execution.ccxt_exchange`` selects the *active CCXT venue* and is resolved
      from the strategy instrument block (``readiness['instrument']['exchange']``,
      the Phase 6 / M1 selection) when present, so a v2 strategy picks its own venue.

    BACKWARD COMPATIBLE: a v1/OKX strategy resolves ``instrument.exchange == 'okx'``
    via ``strategy_instrument()``, so ``ccxt_exchange`` is set to ``okx`` -- exactly
    the existing value -- and the config is byte-identical. When the instrument block
    is absent/empty the config's existing ``ccxt_exchange`` (default okx) is preserved.
    A venue NEVER changes the adapter selector, only which CCXT venue it targets.
    """
    cfg = dict(config or {})
    execution_cfg = dict(cfg.get("execution") or {})
    backend = (os.environ.get("HERMX_EXEC_BACKEND") or "").strip()
    if backend:
        execution_cfg["exchange"] = backend
    rd = readiness or {}
    instrument = rd.get("instrument") or {}
    venue = str(instrument.get("exchange") or "").strip().lower()
    if venue:
        execution_cfg["ccxt_exchange"] = venue
    # Phase A: per-strategy execution_mode controls sandbox routing. The readiness block
    # carries the resolved ``simulated_trading`` (always True in Phase A) and the
    # ``execution_mode`` so the adapter sandboxes accordingly.
    if "simulated_trading" in rd:
        execution_cfg["simulated_trading"] = bool(rd["simulated_trading"])
    if "execution_mode" in rd:
        execution_cfg["execution_mode"] = rd["execution_mode"]
    cfg["execution"] = execution_cfg
    return cfg


class ExecutionService:
    """Controlled execution API surface for submission + post-submit reconciliation.

    The service keeps money-safety controls (gate precedence, write-ahead journal,
    idempotency, UNKNOWN handling) above the adapter boundary.
    """

    def __init__(self, *, config: dict, root: Path, executor_factory, hooks: dict, submit_timeout_seconds: float = 45.0):
        self.config = config or {}
        self.root = Path(root)
        self.executor_factory = executor_factory
        self.hooks = hooks or {}
        self.submit_timeout_seconds = max(1.0, float(submit_timeout_seconds or 45.0))

    def _h(self, name: str):
        return self.hooks[name]

    def _execution_config(self, readiness: dict | None = None) -> dict:
        return resolve_execution_config(self.config, readiness)

    def execute(self, record: dict) -> dict:
        append_jsonl = self._h("append_jsonl")
        execution_ledger = self._h("execution_ledger")

        readiness = record.get("execution_readiness") or {}

        webhook_auth_config_healthy = self._h("webhook_auth_config_healthy")
        watchdog_submission_state = self._h("watchdog_submission_state")

        auth_healthy = bool(record.get("auth_healthy", True)) and webhook_auth_config_healthy()
        watchdog_ok, watchdog_reason = watchdog_submission_state()

        def _blocked(reason: str, gate: str, **extra) -> dict:
            # Single exit for every blocked gate: always records WHICH gate fired (the
            # FIRST in precedence order) so the operator never has to guess. ``ok`` stays
            # True because a refusal-to-submit is a successful, expected control outcome.
            result = {"ok": True, "mode": "not_submitted", "reason": reason, "gate": gate, **extra}
            append_jsonl(execution_ledger, {"received_at": record.get("received_at"), "okx_execution": result})
            return result

        # Gate 1 -- arming + health. Arms on the single per-strategy submit flag
        # (readiness.live_execution_enabled, derived from strategy.submit_orders) plus
        # auth + watchdog health. The dead config-flag arming chain (execution.enabled/
        # submit_orders, risk.allow_live_execution) is gone.
        should_execute = (
            bool(readiness.get("live_execution_enabled"))
            and bool(auth_healthy)
            and bool(watchdog_ok)
        )
        if not should_execute:
            block_reason = readiness.get("block_reason")
            if not block_reason and not auth_healthy:
                block_reason = "Auth health gate is not affirmative"
            if not block_reason and not watchdog_ok:
                block_reason = watchdog_reason or "watchdog_submission_paused"
            if not bool(readiness.get("live_execution_enabled")):
                gate = "strategy_submit_flag"
            elif not auth_healthy:
                gate = "auth_health"
            else:
                gate = "watchdog"
            return _blocked(block_reason or "execution disabled", gate)

        # Gate 2 -- execution_mode must be canonical. Normalize once (lower/strip) and
        # reject any unknown mode early; a typo must never silently route a submit.
        execution_mode = str(readiness.get("execution_mode") or "").strip().lower()
        if execution_mode and execution_mode not in CANONICAL_EXECUTION_MODES:
            return _blocked("unknown_execution_mode", "execution_mode", execution_mode=execution_mode)

        # Gate 3 -- real-venue kill switch. ANY submit the adapter will route to a REAL
        # venue requires the global HERMX_LIVE_TRADING switch -- not just
        # execution_mode==live. "Real venue" is decided exactly as the adapter decides it:
        # the RESOLVED execution config's ``simulated_trading`` is falsey (the adapter
        # skips set_sandbox_mode). Demo/paper/shadow must stay sandbox-only.
        resolved_exec = self._execution_config(readiness).get("execution") or {}
        non_sandbox = not bool(resolved_exec.get("simulated_trading", True))
        is_live_mode = execution_mode == "live"
        if is_live_mode or non_sandbox:
            live_trading_enabled = self._h("live_trading_enabled")
            if not live_trading_enabled()[0]:
                return _blocked("live_trading_disabled", "live_trading_kill_switch")
            if non_sandbox and not is_live_mode:
                # A non-live mode resolved to a real-venue submit: refuse outright.
                return _blocked("non_sandbox_requires_live_mode", "sandbox_only")
            if is_live_mode and not non_sandbox:
                # Live mode but the resolved config still sandboxes: no live/sim mixing.
                return _blocked("live_mode_simulated_inconsistent", "live_sandbox_consistency")

        symbol_pause_info = self._h("symbol_pause_info")
        symbol_pause = symbol_pause_info(readiness.get("symbol"))
        if symbol_pause:
            return _blocked("symbol_paused", "symbol_pause", symbol_pause=symbol_pause)

        order_intent_from_readiness = self._h("order_intent_from_readiness")
        cl_ord_id_from_readiness = self._h("cl_ord_id_from_readiness")
        latest_order_record = self._h("latest_order_record")
        record_order_state = self._h("record_order_state")
        fail_closed_state_write = self._h("fail_closed_state_write")

        order_state_planned = self._h("order_state_planned")
        order_state_submitted = self._h("order_state_submitted")
        order_state_filled = self._h("order_state_filled")
        order_state_rejected = self._h("order_state_rejected")
        order_state_unknown = self._h("order_state_unknown")

        order_intent = order_intent_from_readiness(readiness)
        cl_ord_id = cl_ord_id_from_readiness(readiness)

        existing_order = latest_order_record(cl_ord_id)
        if existing_order is not None:
            existing_state = str(existing_order.get("state") or "")
            return _blocked(
                "duplicate_cl_ord_id",
                "idempotency",
                cl_ord_id=cl_ord_id,
                existing_state=existing_state,
            )

        try:
            record_order_state(cl_ord_id, order_state_planned, intent=order_intent, prev_state=None)
        except OSError as exc:
            fail_closed_state_write("order-journal-planned", exc, context={"cl_ord_id": cl_ord_id})
            raise

        try:
            record_order_state(cl_ord_id, order_state_submitted, intent=order_intent, prev_state=order_state_planned)
        except OSError as exc:
            fail_closed_state_write("order-journal-submitted", exc, context={"cl_ord_id": cl_ord_id})
            raise

        started = time.time()
        redact_secrets = self._h("redact_secrets")

        try:
            if self.executor_factory is None:
                raise RuntimeError("executor_factory_unavailable")
            executor = self.executor_factory.create(self._execution_config(readiness), self.root)
            adapter_result = executor.execute(readiness)
            elapsed_ms = int((adapter_result or {}).get("elapsed_ms") or round((time.time() - started) * 1000))

            if isinstance(readiness.get("okx_fill"), dict) and isinstance((adapter_result or {}).get("fill_summary"), dict):
                readiness["okx_fill"].update((adapter_result or {}).get("fill_summary") or {})

            ok = bool((adapter_result or {}).get("ok"))
            mode = (adapter_result or {}).get("mode")
            result = {
                "ok": ok,
                "mode": mode,
                "elapsed_ms": elapsed_ms,
                "payload": adapter_result,
            }

            fill_status = str(((adapter_result or {}).get("fill_summary") or {}).get("status") or "").lower()
            if ok:
                # An adapter ACK (mode "submit_enabled") is only SUBMITTED -- CCXT has
                # merely acknowledged create_order, the order is not necessarily filled.
                # Only a confirmed fill (mode "filled" or fill status "filled") is FILLED;
                # reconciliation later transitions SUBMITTED -> FILLED.
                if mode == "filled" or fill_status == "filled":
                    outcome_state = order_state_filled
                else:
                    outcome_state = order_state_submitted
            else:
                # submit_partial: a leg reached the venue (e.g. the close) but a later leg
                # failed -- venue state is uncertain, so UNKNOWN (needs reconciliation),
                # never a flat REJECTED that would corrupt position math.
                if mode in {"submit_timeout", "submit_exception", "submit_partial"}:
                    outcome_state = order_state_unknown
                elif mode in {"not_submitted"}:
                    outcome_state = order_state_unknown
                else:
                    outcome_state = order_state_rejected

            outcome_detail = {
                "exchange": (adapter_result or {}).get("exchange"),
                "payload_mode": mode,
                "ok": ok,
            }
        except Exception as exc:
            mode = "submit_exception"
            result = {
                "ok": False,
                "mode": mode,
                "elapsed_ms": round((time.time() - started) * 1000),
                "error": redact_secrets(str(exc)),
            }
            outcome_state = order_state_unknown
            outcome_detail = {"error": redact_secrets(str(exc)), "exception_type": type(exc).__name__}

        def _record_tentative_outcome() -> None:
            # An ACK leaves the order at SUBMITTED, already durably written by the
            # write-ahead -- there is no new transition to record (and SUBMITTED ->
            # SUBMITTED is illegal). Reconciliation later moves it to a terminal state.
            if outcome_state == order_state_submitted:
                return
            try:
                record_order_state(
                    cl_ord_id,
                    outcome_state,
                    intent=order_intent,
                    detail=outcome_detail,
                    prev_state=order_state_submitted,
                )
            except OSError as exc:
                fail_closed_state_write(
                    "order-journal-outcome",
                    exc,
                    context={"cl_ord_id": cl_ord_id, "outcome_state": outcome_state},
                )

        reconcile_post_submit_enabled = self._h("reconcile_post_submit_enabled")
        reconciliation_executor = self._h("reconciliation_executor")
        reconcile_order_with_backoff = self._h("reconcile_order_with_backoff")
        order_state_can_transition = self._h("order_state_can_transition")
        emit_reconcile_alert = self._h("emit_reconcile_alert")
        reconcile_alert_mismatch = self._h("reconcile_alert_mismatch")

        if reconcile_post_submit_enabled() and mode == "submit_partial":
            # A partial multi-leg submit is an operator-actionable money-safety event:
            # one leg reached the venue while another failed. Surface it explicitly so
            # the open position (e.g. an executed close with a failed re-open) is noticed.
            emit_reconcile_alert(
                reconcile_alert_mismatch,
                {
                    "stage": "post_submit_partial",
                    "cl_ord_id": cl_ord_id,
                    "payload_mode": mode,
                    "reason": "submit_partial",
                },
            )

        if reconcile_post_submit_enabled():
            executor = reconciliation_executor()
            reconcile_outcome = None
            if executor is not None:
                try:
                    reconcile_outcome = reconcile_order_with_backoff(
                        executor,
                        {"inst_id": order_intent.get("inst_id"), "cl_ord_id": cl_ord_id},
                    )
                except Exception:
                    reconcile_outcome = None
            if reconcile_outcome is None:
                _record_tentative_outcome()
            else:
                recon_state = reconcile_outcome["state"]
                result["reconcile"] = {
                    k: reconcile_outcome.get(k)
                    for k in ("state", "partial", "reason", "attempts", "elapsed_s", "source", "acc_fill_sz", "avg_px")
                }
                if order_state_can_transition(order_state_submitted, recon_state):
                    try:
                        record_order_state(
                            cl_ord_id,
                            recon_state,
                            intent=order_intent,
                            detail={"reconcile": result["reconcile"], "stdout_outcome": outcome_state},
                            prev_state=order_state_submitted,
                        )
                    except OSError as exc:
                        fail_closed_state_write(
                            "order-journal-reconcile",
                            exc,
                            context={"cl_ord_id": cl_ord_id, "outcome_state": recon_state},
                        )
                else:
                    _record_tentative_outcome()
                # A SUBMITTED -> FILLED resolution is the expected forward progression of
                # an acknowledged order, not a disagreement -- only alert on a genuine
                # divergence (e.g. SUBMITTED/FILLED locally vs REJECTED on the venue).
                expected_progression = (
                    outcome_state == order_state_submitted and recon_state == order_state_filled
                )
                if recon_state != outcome_state and not expected_progression:
                    emit_reconcile_alert(
                        reconcile_alert_mismatch,
                        {
                            "stage": "post_submit",
                            "cl_ord_id": cl_ord_id,
                            "stdout_outcome": outcome_state,
                            "reconciled_outcome": recon_state,
                            "reason": reconcile_outcome.get("reason"),
                        },
                    )
        else:
            _record_tentative_outcome()

        append_jsonl(execution_ledger, {"received_at": record.get("received_at"), "okx_execution": result})
        return result
