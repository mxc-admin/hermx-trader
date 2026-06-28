#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from pathlib import Path


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

        # Phase A gate: paper/sandbox only. Arms on the single per-strategy submit flag
        # (readiness.live_execution_enabled, derived from strategy.submit_orders) plus
        # auth + watchdog health. The dead config-flag arming chain (execution.enabled/
        # submit_orders, risk.allow_live_execution) is gone. The global HERMX_LIVE_TRADING
        # kill switch is intentionally NOT consulted here: in Phase A every order routes to
        # the demo sandbox, so the live-trading switch is inert.
        # Phase B: live mode gate goes here -- for execution_mode == "live", additionally
        # require live_trading_enabled().
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
            result = {
                "ok": True,
                "mode": "not_submitted",
                "reason": block_reason or "execution disabled",
            }
            append_jsonl(execution_ledger, {"received_at": record.get("received_at"), "okx_execution": result})
            return result

        symbol_pause_info = self._h("symbol_pause_info")
        symbol_pause = symbol_pause_info(readiness.get("symbol"))
        if symbol_pause:
            result = {
                "ok": True,
                "mode": "not_submitted",
                "reason": "symbol_paused",
                "symbol_pause": symbol_pause,
            }
            append_jsonl(execution_ledger, {"received_at": record.get("received_at"), "okx_execution": result})
            return result

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
            result = {
                "ok": True,
                "mode": "not_submitted",
                "reason": "duplicate_cl_ord_id",
                "cl_ord_id": cl_ord_id,
                "existing_state": existing_state,
            }
            append_jsonl(execution_ledger, {"received_at": record.get("received_at"), "okx_execution": result})
            return result

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

            if ok:
                outcome_state = order_state_filled
            else:
                if mode in {"submit_timeout", "submit_exception"}:
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
            result = {
                "ok": False,
                "mode": "submit_exception",
                "elapsed_ms": round((time.time() - started) * 1000),
                "error": redact_secrets(str(exc)),
            }
            outcome_state = order_state_unknown
            outcome_detail = {"error": redact_secrets(str(exc)), "exception_type": type(exc).__name__}

        def _record_tentative_outcome() -> None:
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
                if recon_state != outcome_state:
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
