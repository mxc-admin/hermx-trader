# Execution Gate Precedence

Live submission is allowed only when **all** gates are affirmative.
Any unset, false, or ambiguous gate value forces dry-run/no-submit.

## Gate order

1. `HERMX_SUBMIT_ENABLED` kill switch (`submit_kill_switch_armed()`)
2. `execution_readiness.live_execution_enabled`
3. `config.execution.enabled`
4. `config.execution.submit_orders`
5. `config.risk.allow_live_execution`
6. Webhook auth health (`webhook_auth_config_healthy()`)

If any gate fails, `execute_okx_if_enabled()` returns `mode=not_submitted`.

## Notes

- Per-symbol pause (`control-state.json > symbol_pauses`) is an explicit operator override after gate checks and blocks submission with `reason=symbol_paused`.
- Reconciliation and UNKNOWN resolver enrich state but never auto-trade.
- Startup logs include current gate states via `log_execution_arm_state()`.
