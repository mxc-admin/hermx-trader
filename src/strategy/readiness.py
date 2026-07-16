"""Strategy execution readiness (Phase 3 extraction, REFACTOR_PLAN.md).

Houses the readiness cluster that used to live in webhook_receiver.py:
``_strategy_config_for_readiness`` (the A1 pre-trade ceiling's strategy-config
lookup, passed as the ``strategy_config_lookup`` callback into the
ExecutionService gate) and ``build_strategy_execution_readiness`` (the
per-signal execution plan).

Root-bound / reload-reset module state STAYS defined in webhook_receiver.py
(``STRATEGIES`` -- bound at import time from load_strategy_files(), rebound by
the per-test ``importlib.reload(webhook_receiver)`` in conftest's ``wr``
fixture, and mutated in place by test_phase_a_robustness via
``monkeypatch.setitem(wr.STRATEGIES, ...)``). ``_strategy_config_for_readiness``
therefore reads it lazily via ``import webhook_receiver as _wr`` -- the same
pattern as src/alerts.py (Phase 0), src/signals/ (Phase 1) and
src/control_state.py (Phase 2). webhook_receiver re-exports both names so
``wr.<fn>`` call sites and monkeypatch seams keep working.

The leaf-pure / extracted collaborators are imported directly from their
homes: money decimals (webhook.money), execution defaults (webhook.config),
control-state reads (control_state -- itself lazily root-bound), signal
identity + clOrdId derivation (signals.dedupe) and the strategy-record reads
(strategy.records, moved in this same phase).
"""
from __future__ import annotations

import logging

from control_state import accounting_start_for, load_control_state
from pnl_ledger import net_realized_for_strategy
from signals.dedupe import _signal_identity, stable_client_order_id
from strategy.records import strategy_budget_usd, strategy_instrument, strategy_reinvest_enabled
from webhook.config import EXEC_BACKEND, EXECUTION_DEFAULTS, resolve_default_type
from webhook.money import dec_notional


def _strategy_config_for_readiness(readiness: "dict | None") -> dict:
    """Resolve the strategy config (with its capital block) for a readiness record so
    the A1 pre-trade notional ceiling can read ``capital.max_notional_usd``. Reads the
    module-bound STRATEGIES by the readiness ``strategy_id``; empty dict when unknown."""
    import webhook_receiver as _wr
    sid = str((readiness or {}).get("strategy_id") or "").strip()
    if not sid:
        return {}
    return _wr.STRATEGIES.get(sid) or {}


def strategy_override(strategy_id: "str | None") -> dict:
    """The live control-state.json override record for one strategy (written by
    set_strategy_override / the dashboard mode pill); {} when none. Read per call
    so an operator mode change applies without a restart."""
    ov = (load_control_state().get("strategy_overrides") or {}).get(str(strategy_id or ""))
    return ov if isinstance(ov, dict) else {}


def effective_execution_mode(strategy: "dict | None", strategy_id: "str | None" = None,
                             override: "dict | None" = None) -> str:
    """The strategy's operative execution_mode ("demo"/"live"): the file value
    (absent -> demo) unless the control-state override sets one. THE single
    resolution point -- build_strategy_execution_readiness and the reconcile
    enumerator active_venue_modes() (B1) both resolve through here so the signal
    path and the drift-check domain cannot drift apart. ``override`` lets a
    caller that already read the override record pass it in and skip a second
    control-state read."""
    mode = str((strategy or {}).get("execution_mode") or "demo").lower()
    ov = override if override is not None else strategy_override(
        strategy_id or (strategy or {}).get("strategy_id"))
    if isinstance(ov, dict) and ov.get("execution_mode"):
        mode = str(ov["execution_mode"]).lower()
    return mode


def build_strategy_execution_readiness(record: dict) -> dict:
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    # Runtime override from control-state.json (set_strategy_override / dashboard UI).
    # Checked live per-signal so no restart is needed when the operator changes mode.
    # An override carries BOTH execution_mode and submit_orders (see _STRATEGY_MODE_FLAGS).
    _cs_ov = strategy_override(record.get("strategy_id") or (strategy or {}).get("strategy_id"))
    # execution_mode is operative: ``sandbox`` is True for demo and False ONLY for live.
    # The resolved ``simulated_trading`` (= sandbox) and the ``execution_mode`` flow into
    # readiness so the ExecutionService gate can require HERMX_LIVE_TRADING for live
    # submissions and the adapter sandboxes accordingly.
    execution_mode = effective_execution_mode(strategy, override=_cs_ov)
    # submit_orders gates actual submission. Absent in the file -> default True (the
    # historical "submit" posture); Pause sets it False (validate+ledger, no order).
    submit_orders = bool((strategy or {}).get("submit_orders", True))
    if "submit_orders" in _cs_ov:
        submit_orders = bool(_cs_ov["submit_orders"])
    sandbox = (execution_mode != "live")  # demo -> True; live -> False
    # submit_orders is the submission gate: Pause -> False (no orders to either venue);
    # Demo/Live -> True. execution_mode then decides sandbox vs real account.
    live_execution_enabled = bool(submit_orders)
    live_allowed = live_execution_enabled
    direction = "long" if normalized.get("action") == "buy" else "short"
    # side_policy (schema default "long_short") gates which OPEN legs may be placed.
    # long_only/short_only suppress the opposite-direction OPEN while ALWAYS keeping the
    # CLOSE_OPPOSITE_IF_ANY leg, so a policy change can never strand an open position.
    side_policy = str(strategy.get("side_policy") or "long_short").lower()
    open_allowed = (
        side_policy == "long_short"
        or (direction == "long" and side_policy == "long_only")
        or (direction == "short" and side_policy == "short_only")
    )
    intent_actions = ["CLOSE_OPPOSITE_IF_ANY"]
    if open_allowed:
        intent_actions.append(f"OPEN_{direction.upper()}")
    signal_identity = _signal_identity(normalized)
    # Reversal signals submit two legs (close the opposite position, then open the new
    # one). Each leg needs its OWN clOrdId or the venue rejects the second as a duplicate.
    # ``client_order_id`` stays the OPEN-leg id (the leg that defines the final position
    # and the journal dedupe key); the close-leg id is carried alongside it.
    client_order_id_close = stable_client_order_id(signal_identity, role="close")
    client_order_id_open = stable_client_order_id(signal_identity, role="open")
    client_order_id = client_order_id_open
    # Sizing budget: ``budget_usd`` is the SEED; with capital.reinvest (schema default
    # True) the strategy sizes off equity = seed + durable realized net P&L from the
    # closed-trade ledger, scoped to this strategy's account mode and accounting
    # window — the exact "Effective budget" number the dashboard card shows (Phase 5
    # Decision ⑤A), so execution and display can never diverge. Stateless: recomputed
    # per signal, so a mid-flight budget_usd edit or accounting-window reset applies
    # on the very next signal. Fail-safe: a ledger read error degrades to seed-only
    # sizing with equity_usd=None (the equity-stop gate never fires on unknown equity).
    seed_budget_usd = strategy_budget_usd(strategy)
    sizing_budget_usd = seed_budget_usd
    equity_usd = None
    reinvest = strategy_reinvest_enabled(strategy)
    if reinvest:
        _pnl_sid = str(strategy.get("strategy_id") or normalized.get("strategy_id") or "").strip()
        _pnl_mode = "live" if execution_mode == "live" else "demo"  # ledger mode column is demo|live
        try:
            realized_net = (
                net_realized_for_strategy(
                    _pnl_sid, mode=_pnl_mode, accounting_start_at=accounting_start_for(_pnl_sid)
                )
                if _pnl_sid
                else 0.0
            )
            equity_usd = seed_budget_usd + realized_net
            # Depleted equity must never yield a negative notional; the ExecutionService
            # equity-stop gate blocks the open outright, this clamp keeps sizing sane.
            sizing_budget_usd = max(equity_usd, 0.0)
        except Exception as exc:
            logging.warning(
                "reinvest equity read failed strategy_id=%s mode=%s: %s -- sizing off seed budget",
                _pnl_sid, _pnl_mode, exc,
            )
            equity_usd = None
            sizing_budget_usd = seed_budget_usd
    base_notional = sizing_budget_usd * float(strategy.get("leverage") or 1.0)
    planned_notional = float(dec_notional(base_notional))
    # capital.max_notional_usd is a SOFT ceiling: size the order DOWN to it, never
    # reject the signal. Unset / non-positive => no cap. The env half of the ceiling
    # (HERMX_MAX_NOTIONAL_USD) is applied identically by the ExecutionService gate.
    try:
        max_notional = float((strategy.get("capital") or {}).get("max_notional_usd") or 0.0)
    except (TypeError, ValueError):
        max_notional = 0.0
    if max_notional > 0 and planned_notional > max_notional:
        logging.warning(
            "notional clamp strategy_id=%s planned=%.2f ceiling=%.2f",
            strategy.get("strategy_id") or normalized.get("strategy_id"),
            planned_notional, max_notional,
        )
        planned_notional = float(dec_notional(max_notional))
    # Exchange-agnostic instruction contract (Phase 6 / M3, ARCHITECTURE.md). ``td_mode``
    # below is the OKX translation of this same value, so derive both from one expression.
    margin_mode = strategy.get("margin_mode", "isolated")
    instrument = strategy_instrument(strategy)
    # Derive ccxt_default_type from strategy instrument.type (e.g., swap, spot, future)
    instrument_type = resolve_default_type(instrument)
    plan = {
        "mode": "strategy_file_live_order_enabled" if live_allowed else "strategy_file_trial_no_order",
        "live_execution_enabled": live_allowed,
        "execution_mode": execution_mode,
        "simulated_trading": sandbox,
        "execution_policy": f"strategy_file:{normalized.get('strategy_id')}",
        "execution_policy_label": strategy.get("name") or normalized.get("strategy_id"),
        "exchange": EXEC_BACKEND,
        "ccxt_default_type": instrument_type,
        "route": EXECUTION_DEFAULTS["route"],
        "account": EXECUTION_DEFAULTS["account"],
        "symbol": normalized.get("symbol"),
        "inst_id": instrument.get("inst_id"),
        "expected_leverage": strategy.get("leverage"),
        "td_mode": margin_mode,
        # --- Exchange-agnostic instruction contract (Phase 6 / M3) ---
        # THE wire contract going forward (ARCHITECTURE.md). The inst_id / td_mode
        # keys above stay present but are now adapter-derived translations of these:
        # the CCXT adapter maps inst_id<->symbol and tdMode<-margin_mode. Every value
        # here is byte-identical to its OKX-named twin / execution_intent field, so orders
        # and downstream readers are unchanged.
        "instrument": instrument,
        "strategy_id": strategy.get("strategy_id") or normalized.get("strategy_id"),
        "asset": strategy.get("asset") or normalized.get("symbol"),
        "target_side": direction,
        "target_notional_usd": planned_notional,
        # Equity-sizing observability + the equity-stop gate's input. ``equity_usd``
        # is present (a float, possibly <= 0) ONLY when reinvest sizing resolved;
        # None means fixed sizing or a failed ledger read (gate must not fire).
        "reinvest": reinvest,
        "budget_seed_usd": seed_budget_usd,
        "equity_usd": equity_usd,
        "margin_mode": margin_mode,
        "leverage": strategy.get("leverage"),
        "timeframe": normalized.get("timeframe"),
        "tv_time": normalized.get("tv_time"),
        "signal_side": normalized.get("action"),
        "signal_price": normalized.get("tv_signal_price"),
        "execution_intent": {
            "policy": f"strategy_file:{normalized.get('strategy_id')}",
            "decision": "TRADE",
            "risk_weight": 1.0,
            "target_direction": direction,
            "actions": intent_actions,
            "base_notional_usd": sizing_budget_usd,
            "planned_notional_usd": planned_notional,
            "client_order_id": client_order_id,
            "client_order_id_open": client_order_id_open,
            "client_order_id_close": client_order_id_close,
        },
        "okx_fill": {
            "status": "not_sent_strategy_trial" if not live_allowed else "ready_to_send_when_strategy_promoted",
            "order_id": None,
            "client_order_id": client_order_id,
            "avg_fill_price": None,
            "filled_size": None,
            "fee_usd": None,
            "slippage_pct": None,
            "position_after_order": None,
        },
        "block_reason": None if live_allowed else "Duo Base Dev strategy trial is not approved for OKX submission",
    }
    if not open_allowed:
        # Flag the suppression so the adapter drops the OPEN leg (open_suppressed) and the
        # pipeline event is observable. Decision-level tag only -- never the service `gate`.
        plan["execution_intent"]["open_suppressed"] = True
        plan["side_policy_restriction"] = {"policy": side_policy, "suppressed_direction": direction}
    # The separate execution-plan.jsonl ledger was removed entirely (constant + sweep
    # entry): nothing consumed it. The authoritative submission outcome is recorded to
    # pipeline.jsonl (stage="execution"), which the dashboard reads.
    return plan
