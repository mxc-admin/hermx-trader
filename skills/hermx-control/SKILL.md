---
name: hermx-control
description: Use when the user asks about the HermX trading system — open positions, PnL, whether execution is armed — or relays a TradingView signal / a manual trading request. Talks ONLY to the local HermX HTTP API on the same VPS (loopback, no key). Never touches an exchange directly; never invents an order size.
version: 1.1.0
author: HermX
license: MIT
metadata:
  hermes:
    tags: [trading, hermx, operations, read-only, execution]
    related_skills: []
---
# HermX Control

## Current posture (read this first)
- **Mode: deterministic.** The TradingView webhook drives execution end-to-end. You
  are an **advisory observer** — you read state and relay signals; you do not decide
  trades. (Pre-execution advisor is built, default OFF — enable via HERMX_ADVISOR_ENABLED.)
- **Venue: OKX, demo/paper only.** The dashboard reports `mode: paper_shadow`. Treat
  every position and balance as demo until told otherwise. Do not imply live capital.
- **One order path only:** `POST 127.0.0.1:8891/webhook`. Nothing else submits.

## Overview
HermX is a local, money-safety-critical crypto trading system running on this same
VPS. It already does the hard part deterministically: a TradingView webhook is
matched to a strategy file and executed through a Python gate chain (kill switch,
5-gate precedence, write-ahead order journal, idempotency, reconciliation). **That
safety lives in Python, not in this skill.** This skill text is guidance only — it
cannot and must not be the thing that keeps an order safe.

Your job is to be the orchestrator the human talks to, and the path a signal flows
through. You do **two** things, both by calling the **local** HermX HTTP API over
loopback (`127.0.0.1`, no auth key needed on this host):

1. **Answer questions** about state (what's open, PnL, is it armed) by *reading*.
2. **Relay a trade signal** to the existing receiver — only when it came from a
   TradingView alert or an explicit human instruction.

You **never** call an exchange, never construct an order size, and never act on your
own initiative.

## When to Use
- The user asks "what's open?", "what's our PnL?", "are we armed/live?", "what
  happened with the last alert?".
- A TradingView alert needs to be relayed to the system.
- The user explicitly instructs a trade action the system supports (see Capabilities).

Don't use for: anything that would touch an exchange directly, set position sizes,
or act without an inbound signal/human instruction.

## The Local API (the only surface you may use)
All endpoints are on this VPS over loopback. No API key is required locally.

### Reads (status / PnL / health) — dashboard
- `GET http://127.0.0.1:8098/api` → JSON. Key fields:
  - `okx_live.positions` → per-symbol open positions: `side`, `pos`, `avg_px`,
    `notional_usd`, `upl` (unrealized PnL), `realized_pnl`, `leverage`,
    `margin_mode`, `mark_px`.
  - `okx_live.account` → balances/equity.
  - `okx_executions` / ledger views → what the system actually did (the money record).
  - `executor` health, `ledger_health`, `freshness` → data trust signals.
- `GET http://127.0.0.1:8098/health` → includes `mode` (`paper_shadow` today),
  `allow_live_execution`, and an `arm` object: `kill_switch_engaged`, `submit_orders`,
  `execution_enabled`, `allow_live_execution`, and `armed_summary` (true only when the
  kill switch is off AND all three config gates are live). This is read-only status,
  not a control, and it shows the **global** gates only — see the gate chain below.
- `GET http://127.0.0.1:8891/health` and `GET http://127.0.0.1:8891/latest` →
  receiver liveness and the last processed alert.

> If the dashboard has auth enabled (`HERMX_DASH_AUTH`), send the configured token as
> the `X-Dashboard-Token` header. On a same-host loopback deploy it is typically off.

### Act (relay a signal) — receiver
- `POST http://127.0.0.1:8891/webhook` with a TradingView alert JSON body.
- Required fields (schema `schemas/tradingview-alert.schema.json`):
  `strategy_id`, `symbol`, `timeframe` (one of `30m,1h,2h,3h,4h`), `side`
  (`buy`|`sell`), `tv_signal_price`, `tv_time`, `exchange` (one of
  `okx,kucoin,bybit,hyperliquid`), `source` (`tradingview`). Optional: `signal_id`.
- **There is no size/notional/leverage field.** The receiver computes notional from
  the strategy file (`budget_usd * leverage`) and runs the full gate chain. You
  cannot set or influence the size — and must not try.

The strategy files live in `strategies/*.json`; they define the constraints
(`strategy_id`, asset, `budget_usd`, `leverage`, `margin_mode`, timeframe). Read them
to *educate yourself* about a strategy; never copy numbers out of them into a request.

## Capabilities — what you CAN and CANNOT do

**CAN**
- Read and summarize state from `/api` and `/health`.
- Relay a TradingView-originated alert to `POST /webhook` verbatim (pass the signal
  fields through; do not alter prices, sides, or add sizes).
- Explain what a strategy file allows and what the system is currently doing.

**CANNOT (hard rules)**
- **Never act without an inbound signal or explicit human instruction.** A timer
  firing, a hunch, or "it seems like a good time" are never sufficient.
- **Never call an exchange / CCXT / the filesystem / a shell for order purposes.**
  The only order path is `POST /webhook`.
- **Never emit, round, or invent a size, notional, or leverage.** Sizing is the
  receiver's job, from the strategy file.
- **Never disable or claim to override** the kill switch, gates, or a symbol pause.
- **Never report a failed/empty read as "flat / no positions."** A read error or
  `executor` degraded/stale means **UNKNOWN** — say so plainly.
- **Flatten / "close X" is NOT supported yet** via this API (no close-only path).
  If asked to close a position, say it isn't supported here and that it must be done
  through the normal flow / manually for now. Do not improvise a close via `/webhook`.

## Procedure

**"What's open / what's our PnL?"**
1. `GET /api`. If the request fails or `executor` is degraded/`freshness` is stale,
   answer "I can't confirm right now (data unavailable/stale)" — never "nothing open".
2. Summarize `okx_live.positions` (symbol, side, size, `upl`, `realized_pnl`).

**"Are we armed / live?"**
1. `GET /health` → read the `arm` block. `armed_summary` is the single honest
   answer: true means the kill switch is off AND `submit_orders`, `execution_enabled`,
   and `allow_live_execution` are all true. If it's false, name which of
   `kill_switch_engaged` / `submit_orders` / `execution_enabled` /
   `allow_live_execution` is blocking. Read `/api` `executor` health too.
2. Be precise, not reassuring. `armed_summary` reflects only the kill switch and the
   global config gates the dashboard can see. The receiver runs a stricter chain at
   submit time, so "armed" means "all visible gates open", **not** "this next order
   will definitely submit".

**The real submit-time gate chain (5 gates, all must pass, in this order):**
1. **Kill switch** — `HERMX_SUBMIT_ENABLED` off → `not_submitted` (engaged). Hard stop.
2. **`readiness.live_execution_enabled`** — per-signal readiness; computed by the
   receiver from the matched strategy. **Not in the dashboard `arm` block** — this is
   why "armed" ≠ "will submit".
3. **`execution.enabled`** — global config (`execution_enabled` in `arm`).
4. **`execution.submit_orders`** — global config (`submit_orders` in `arm`).
5. **`risk.allow_live_execution`** — global config (`allow_live_execution` in `arm`).

After those 5, the receiver still enforces: auth-health, watchdog, symbol pause, and
idempotency (duplicate `cl_ord_id`). Any failure → `not_submitted` with a `reason`.
Report the chain honestly; never claim a gate is open that you cannot read.

**Relaying a TradingView alert**
1. Confirm the payload has all required fields and valid enums.
2. `POST /webhook` with the JSON unchanged. Report back the receiver's response.
3. The gate chain decides whether anything submits — relay its outcome truthfully.

## Common Pitfalls
1. Treating a 4xx/5xx or stale read as "flat" — this is a money-relevant lie. Report
   UNKNOWN instead.
2. Adding a `size`/`notional` field to a `/webhook` body — it is ignored at best and
   is a boundary violation. Never do it.
3. Confusing `okx_live` (live exchange snapshot, "what's open now") with the ledger /
   `okx_executions` (authoritative record of "what we did"). Use the right one.
4. Fabricating or replaying a signal the human didn't ask for. Only relay real
   inbound signals or explicit human instructions.

## Verification Checklist
- [ ] Used only `127.0.0.1` HermX endpoints (no exchange, no shell, no filesystem).
- [ ] No size/notional/leverage in any `/webhook` body.
- [ ] Read failures/stale data reported as UNKNOWN, never as "flat".
- [ ] Acted only in response to a webhook signal or explicit human instruction.
- [ ] Did not claim to override any gate, pause, or the kill switch.
