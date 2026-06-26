# Skill: Emergency Stop

Use this when execution must be paused immediately.

## Stop Levels

### Level 0: Global Kill Switch (fastest, no redeploy)

A single environment variable, `HERMX_SUBMIT_ENABLED`, hard-blocks **all** OKX
order submission regardless of runtime config. This is the fastest stop: no code
change, no config edit, no redeploy — just set the var and restart the receiver.

Verified behavior (`submit_kill_switch_armed()` in `src/webhook_receiver.py`):

- **Unset** — inert. Existing config-driven behavior is preserved (the switch's
  absence cannot, by itself, arm submission).
- **Falsey** (`false`, `0`, `no`, `""`, or blank/whitespace, case-insensitive) —
  **HARD-BLOCKS** submission before any subprocess is spawned.
- **Anything else** — armed; the config gates below still apply.

When engaged, `execute_okx_if_enabled()` returns mode `not_submitted` (with
`reason: "HERMX_SUBMIT_ENABLED kill switch engaged"`) and writes that record to
`logs/executions.jsonl` — no `okx_demo_executor` subprocess is ever launched.

**Engage it:**

1. Set the var in `.env` (or directly in the process environment):

   ```
   HERMX_SUBMIT_ENABLED=false
   ```

2. Restart the receiver (`python src/webhook_receiver.py`, or the start script).

**Confirm it:** on startup, `log_execution_arm_state()` (called from `main()`)
emits an `EXECUTION ARM STATE:` log line. Check that `kill_switch_armed=False`:

   ```
   EXECUTION ARM STATE: HERMX_SUBMIT_ENABLED=false (kill_switch_armed=False) ...
   ```

To re-arm, unset the var (or set a non-falsey value) and restart; config gates
then resume control.

### Level 1: Pause New Alerts

- disable execution in runtime config
- keep dashboard and webhook alive
- alerts are logged but no orders are sent

### Level 2: Stop Webhook Processing

- stop webhook service
- dashboard may remain online

### Level 3: Flatten Exchange

- close open OKX positions
- verify flat
- disable order submission

## Execution Gate Precedence

For a **live** OKX order to actually submit, every gate below must be affirmative.
They are evaluated in this real order inside `execute_okx_if_enabled()`
(`src/webhook_receiver.py`); the first non-affirmative gate short-circuits to a
`not_submitted` (dry-run) result:

1. **`HERMX_SUBMIT_ENABLED` not falsey** — the global kill switch is armed
   (see Level 0). Falsey here blocks before any other gate is checked.
2. **`readiness.live_execution_enabled == true`** — per-alert execution readiness
   (derived from `execution.enabled` + `risk.allow_live_execution`).
3. **`CONFIG.execution.enabled == true`** — execution subsystem enabled.
4. **`CONFIG.execution.submit_orders == true`** — order submission enabled.
5. **`CONFIG.risk.allow_live_execution == true`** — risk layer permits live trades.

Any gate that is unset, ambiguous, or false ⇒ **dry-run / `not_submitted`**
(fail-safe by design — the system never submits on uncertainty).

**Current repo posture (dry-run during refactor):** `shadow-config.json` ships
with `execution.submit_orders=false` and `risk.allow_live_execution=false`, so
gates 4 and 5 are not affirmative and no live order can submit even with the kill
switch armed.

## Required Log

Every emergency stop must log:

- time
- operator
- reason
- strategies affected
- exchange position before
- exchange position after

