# HermX Relay Adapter

> **This is the HermX-internal relay adapter (`HermesRelayAdapter`), not a Hermes Agent
> SKILL.md.** It runs inside HermX behind the webhook receiver's execution seam. The Hermes
> Agent accesses HermX only via the loopback HTTP API; it never imports or calls this
> component directly.

This component relays a validated signal + strategy context into the controlled HermX
execution chokepoint (`ExecutionService`). It is internal to HermX, not an agent-facing
surface.

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

Submission requires:

1. `strategy.submit_orders = true`
2. `strategy.execution_mode` resolved — `demo` routes to the exchange sandbox/paper
   account (always allowed); `live` routes to the real account and additionally
   requires `HERMX_LIVE_TRADING=true` (the global kill switch)
3. Auth health affirmative
4. Watchdog health affirmative
5. Symbol not paused

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

- Implemented by `src/skills/hermes_execution.py` (`HermesRelayAdapter`).
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
