# Execution Gates — canonical reference

This is the single source of truth for the order-submission gate chain, the
environment defaults that drive it, and the system's safety posture. Money-safety
gates live in **Python** (`ExecutionService.execute`), never in skill/agent prose — an
adversarial or buggy LLM cannot widen its authority because the authority isn't in the
text.

## Gate precedence (first failing gate wins)

`ExecutionService.execute()` evaluates these in order. The first one that blocks returns
`{"ok": true, "mode": "not_submitted", "reason": <reason>, "gate": <gate>}` — `ok` is
true because a refusal is a successful, expected control outcome. The `gate` field names
the **first** blocking gate so the operator never has to guess.

| # | Gate (`gate` field) | Blocks when | `reason` |
|---|---------------------|-------------|----------|
| 1 | `strategy_active` | strategy has no valid `execution_mode` (readiness `live_execution_enabled` off) | `execution disabled` / strategy `block_reason` |
| 1 | `auth_health` | webhook auth config unhealthy (missing secret, or HMAC required w/o key) | `Auth health gate is not affirmative` |
| 1 | `watchdog` | liveness watchdog has paused submission | watchdog reason |
| 2 | `execution_mode` | `execution_mode` is non-empty but not canonical | `unknown_execution_mode` |
| 3 | `live_trading_kill_switch` | submit would reach a **real venue** but `HERMX_LIVE_TRADING` is not armed | `live_trading_disabled` |
| 3 | `sandbox_only` | a **non-live** mode resolved to a real-venue (non-sandbox) submit | `non_sandbox_requires_live_mode` |
| 3 | `live_sandbox_consistency` | `execution_mode=live` but the resolved config still sandboxes | `live_mode_simulated_inconsistent` |
| 4 | `symbol_pause` | the symbol is paused in `control-state.json > symbol_pauses` | `symbol_paused` |
| 5 | `idempotency` | a journal record already exists for this `cl_ord_id` | `duplicate_cl_ord_id` |

Only when **every** gate passes is the executor built and `executor.execute()` called
exactly once. Then PLANNED and SUBMITTED are durably journaled **before** the submit
(write-ahead), so restart reconciliation has authoritative `cl_ord_id` keys after a crash.

### "Real venue" is decided exactly as the adapter decides it

A submit is **sandbox** unless the *resolved* execution config's `simulated_trading` is
falsey (the CCXT adapter then skips `set_sandbox_mode`, hitting the real venue). The
adapter defaults `simulated_trading` to **true**, so missing/ambiguous config stays
sandbox. The kill switch (Gate 3) therefore guards **any** real-venue submit — not just
`execution_mode == "live"`. `demo` is sandbox-only; a non-live mode that resolves to a real venue is refused
outright (`sandbox_only`).

Canonical `execution_mode` values: `demo`, `live` (anything else →
`unknown_execution_mode`).

## Environment defaults & safety posture

| Env var | Default | Posture |
|---------|---------|---------|
| `HERMX_SECRET` | _(unset → `""`)_ | **Fail closed.** The sole secret for webhook + dashboard auth; blank ⇒ every webhook gets `401`, protected dashboard routes `401`. No legacy fallbacks. |
| `HERMX_LIVE_TRADING` | _(unset → disabled)_ | **Global kill switch.** Required for ANY real-venue submit. Unset/false ⇒ no real-money order can be sent; demo sandbox is unaffected. |
| `HERMX_REQUIRE_HMAC` | `false` | When false **and** the receiver binds a non-loopback interface, boot logs a SECURITY warning (off-host reachable on the shared secret alone). Recommended **true** for any non-loopback exposure. |
| `HERMX_WEBHOOK_HMAC_KEY` | _(unset)_ | Required when HMAC is on; missing ⇒ fail closed (`401`). |
| `HERMX_REPLAY_WINDOW_SECONDS` | `300` | **Security freshness** for the HMAC timestamp. Independent of the dedupe window — neither widens the other. |
| `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS` | `86400` | **Business idempotency** retention. Independent of the replay window. |
| `HERMX_RECONCILE_ENABLED` | _(unset → OFF)_ | Post-submit inline reconciliation. OFF ⇒ stdout drives the tentative outcome (observe-only soak). |
| `HERMX_UNKNOWN_RESOLVER_ENABLED` | _(unset → ON)_ | Periodic background resolver daemon (observe-only). |
| `HERMX_UNKNOWN_RESOLVER_INTERVAL_SECONDS` | `30` | Resolver tick cadence. |
| `HERMX_UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS` | `900` | **UNKNOWN lifecycle backstop**: an order older than this (measured from origin) is alerted + symbol-paused, **never auto-closed**. |
| `HERMX_DASH_AUTH` | `true` | Dashboard auth on (token via `X-Dashboard-Token` / Bearer / Basic). |

## Reconciliation is observe-only (never trades)

There are exactly three reconciliation paths — STARTUP (always), POST-SUBMIT
(`HERMX_RECONCILE_ENABLED`, default OFF), PERIODIC (`HERMX_UNKNOWN_RESOLVER_ENABLED`,
default ON). All three may only update the local order journal and emit alerts; none
submits, cancels, or auto-trades.

Money-safety mapping: a venue-confirmed `canceled` + zero-fill ⇒ `REJECTED`. **Absence**
(`not_found` across get_order / pending / archive) ⇒ `UNKNOWN`, never `REJECTED` — a
missing order may have filled and aged out, so it stays tracked rather than being dropped
as flat. A stuck UNKNOWN order trips the lifecycle backstop (alert + symbol pause), and
the pause hard-blocks submission until an operator clears it (see the runbook in
`webhook_receiver.py` near `RECONCILE_ALERT_LEDGER`).

## Startup self-check

`log_execution_arm_state()` logs the effective posture at boot: demo/live strategy
counts, `HERMX_LIVE_TRADING`, reconcile/resolver flags, auth health, HMAC requirement,
queue/worker sizing, and the non-loopback HMAC-off SECURITY warning when applicable.
