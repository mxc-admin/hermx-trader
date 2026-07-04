"""Order reconciliation package (REFACTOR_PLAN.md Phase 5).

Behavior-preserving split of the reconcile cluster out of webhook_receiver.py:

  - reconcile.executor_select -- per-order (venue, mode) executor resolution (#20a)
  - reconcile.orders          -- the OKX v5 fallback chain + backoff + startup bootstrap
  - reconcile.unknown_resolver -- the periodic SUBMITTED/UNKNOWN/PLANNED resolver
  - reconcile.drift           -- observe-only journal-vs-venue position drift detection
  - reconcile.alerts          -- the reconcile-kind alert emitter + alert-kind constants

Everything is re-exported here so ``from reconcile import X`` works for any X, and
webhook_receiver.py re-exports the same names so ``wr.<fn>`` call sites and test
monkeypatch seams keep working unchanged.
"""
from __future__ import annotations

from reconcile.executor_select import (
    _effective_execution_config,
    _reconciliation_executor,
    _executor_for_order,
)
from reconcile.orders import (
    _PRESENT_ORDER_STATES,
    RECONCILE_MAX_ATTEMPTS,
    RECONCILE_BASE_DELAY_SECONDS,
    RECONCILE_CAP_DELAY_SECONDS,
    RECONCILE_WALL_CLOCK_BUDGET_SECONDS,
    RECONCILE_HISTORY_LIMIT,
    _reconcile_float,
    _order_is_present,
    _order_matches,
    map_order_outcome,
    reconcile_order_once,
    reconcile_order_with_backoff,
    reconcile_startup,
)
from reconcile.unknown_resolver import (
    _order_age_seconds,
    _resolve_planned_orphan,
    resolve_unknown_orders_once,
    unknown_resolver_loop,
)
from reconcile.drift import reconcile_position_drift
from reconcile.alerts import (
    emit_reconcile_alert,
    RECONCILE_ALERT_MISMATCH,
    RECONCILE_ALERT_RESOLVER_TIMEOUT,
    RECONCILE_ALERT_PLANNED_ABANDONED,
    RECONCILE_ALERT_PLANNED_ON_VENUE,
)

__all__ = [
    "_effective_execution_config",
    "_reconciliation_executor",
    "_executor_for_order",
    "_PRESENT_ORDER_STATES",
    "RECONCILE_MAX_ATTEMPTS",
    "RECONCILE_BASE_DELAY_SECONDS",
    "RECONCILE_CAP_DELAY_SECONDS",
    "RECONCILE_WALL_CLOCK_BUDGET_SECONDS",
    "RECONCILE_HISTORY_LIMIT",
    "_reconcile_float",
    "_order_is_present",
    "_order_matches",
    "map_order_outcome",
    "reconcile_order_once",
    "reconcile_order_with_backoff",
    "reconcile_startup",
    "_order_age_seconds",
    "_resolve_planned_orphan",
    "resolve_unknown_orders_once",
    "unknown_resolver_loop",
    "reconcile_position_drift",
    "emit_reconcile_alert",
    "RECONCILE_ALERT_MISMATCH",
    "RECONCILE_ALERT_RESOLVER_TIMEOUT",
    "RECONCILE_ALERT_PLANNED_ABANDONED",
    "RECONCILE_ALERT_PLANNED_ON_VENUE",
]
