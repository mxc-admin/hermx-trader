# Skill: Hermes Execution

Use this skill to execute trades through the controlled HermX execution API.

This skill is the only agent-facing execution surface.

## Purpose

- Translate analyzed webhook signal + strategy context into execution intent.
- Call controlled execution API (`ExecutionService`) for submit/reconcile.
- Keep risk controls in code constraints, not in free-form agent logic.
- Use CCXT as exchange transport via adapters, not as policy owner.

## Inputs

- `signal`: normalized TradingView payload.
- `strategy`: matched strategy JSON payload.
- `account_context`: current positions, balances, risk state.
- `mode`: `dry_run` or `live`.

## Output Contract

- `ok`: boolean
- `mode`: `not_submitted | submitted | filled | rejected | unknown`
- `reason`: optional blocking reason
- `execution_intent`: normalized intent payload
- `client_order_id`: stable id used for idempotency
- `exchange_result`: normalized adapter result payload

## Required Flow

1. Validate signal and strategy match.
2. Build execution intent from strategy policy.
3. Run gate precedence checks.
4. Run risk constraints (size/leverage/symbol pause/no-pyramid rules).
5. Persist `PLANNED` and `SUBMITTED` states before submit.
6. Submit via `ExecutionService` only.
7. Reconcile via adapter polling and resolve terminal/unknown states.
8. Persist outcome and emit operator-visible events.

## Gate Precedence

Live submit requires all true:

1. `HERMX_SUBMIT_ENABLED=true`
2. Execution config submit gate true
3. Risk config allow-live gate true
4. Auth health affirmative
5. Watchdog health affirmative
6. Symbol not paused

If any gate is false/unknown, return `not_submitted` with reason.

## Risk Constraints

- Stable client-order-id dedupe (journal-first check).
- No same-direction pyramiding unless policy allows.
- Reverse behavior must close-verify-open.
- Position sizing and leverage bounds enforced in code.
- Fail closed on missing credentials or unresolved venue mapping.

## Failure Handling

- Submit timeout/exception => `unknown`, never blind retry.
- Reconcile mismatch => raise operator alert and pause affected symbol.
- Adapter read failure => preserve uncertain state and alert.
- Missing credentials => disarm selected exchange, no fallback borrowing.

## Runtime

- Implemented by `src/skills/hermes_execution.py` (`HermesExecutionSkill`).
- `dry_run` builds the normalized intent and returns `not_submitted` (reason
  `dry_run`) without calling the service — it submits nothing.
- `live` submits exclusively through `ExecutionService.execute(record)`; the
  service result vocabulary is mapped onto the contract `mode` values, with a
  reconciled terminal state taking precedence over the stdout outcome.
- Fails closed before any submit on `invalid_signal_side` or
  `unresolved_venue_mapping`; a submit timeout/exception maps to `unknown` and is
  never blindly retried.
- Covered by `tests/test_phase5_hermes_skill.py`.

## Notes

- CCXT is implementation transport for exchange operations.
- Money-safety semantics remain owned by HermX execution API and journals.
