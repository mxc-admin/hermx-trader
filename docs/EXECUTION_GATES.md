# Execution Gate Precedence

Live submission is allowed when **both** conditions are met:

1. `strategy.submit_orders = true` — this strategy is permitted to submit orders
2. `strategy.execution_mode` — controls which account receives the order:
   - `demo` → OKX sandbox/paper account (always allowed, no kill switch needed)
   - `live` → real exchange account (requires `HERMX_LIVE_TRADING=true` in env)

## Kill switch

`HERMX_LIVE_TRADING=false` (default) blocks all `execution_mode=live` orders.
Demo/paper orders are unaffected by this flag.

## Runtime health gates (always checked)

- Webhook auth health (`webhook_auth_config_healthy()`)
- Watchdog / auth healthy

If either health gate fails, `execute_if_enabled()` returns `mode=not_submitted`.

## Notes

- Per-symbol pause (`control-state.json > symbol_pauses`) blocks submission after gate checks with `reason=symbol_paused`.
- Reconciliation and UNKNOWN resolver enrich state but never auto-trade.
- Startup logs include current gate states via `log_execution_arm_state()`.
