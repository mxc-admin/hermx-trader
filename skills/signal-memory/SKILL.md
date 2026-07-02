---
name: signal-memory
description: "Use when the agent needs recent HermX signal and decision history — last N processed alerts, advisor verdicts, and execution outcomes — for conversational continuity. Helps avoid doubling up on a recent action or contradicting a prior decision. Read-only, never relays a signal."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret"
    help: "Set in HermX .env on this host. Required by default (HERMX_DASH_AUTH is on unless explicitly set false)."
    required_for: "Authenticated read of the signal history endpoint"
metadata:
  hermes:
    tags: [trading, hermx, memory, read-only, signals]
    related_skills: [hermx-control, dashboard-risk]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
        prompt: "HermX dashboard base URL"
---

# signal-memory

Reads recent HermX signal history and advisor decisions for agent continuity. Use before relaying a signal to check if the same action was recently attempted or vetoed.

## When to Use

- Before relaying a signal: check if this symbol/side was recently submitted or vetoed
- When the operator asks "what happened lately?" or "did BTC trade recently?"
- When the advisor needs recent context to make a veto decision
- Do NOT use as a substitute for live position state — use `hermx-control GET /api` for that

## Quick Reference

| Action | Method | Endpoint | Auth |
|--------|--------|----------|------|
| Recent signals (last N) | GET | `{hermx.dashboard_base}/api/signals?n=50` | `X-Dashboard-Token: {HERMX_SECRET}` |
| Filter by symbol | GET | `{hermx.dashboard_base}/api/signals?n=20&symbol=BTCUSDT` | `X-Dashboard-Token: {HERMX_SECRET}` |

## Procedure

1. Call `GET {hermx.dashboard_base}/api/signals?n=50` with the `X-Dashboard-Token: {HERMX_SECRET}` header. Auth is on by default, so the header is required unless the operator explicitly set `HERMX_DASH_AUTH=false`.
2. Parse the response — a list of recent signal records, each containing:
   - `symbol`, `side`, `strategy_id`
   - `submitted_at` (ISO timestamp)
   - `mode` — `submitted`, `not_submitted`, `vetoed_by_advisor`, `unknown`
   - `reason` — why it was not submitted (if applicable)
   - `advisor_verdict` — `proceed`, `skip`, or `unknown` (if advisor was enabled)
3. Summarise for context: last action per symbol, any recent vetoes, any recent failures.
4. If the endpoint returns an error or is unreachable, treat as `status: unknown` — do not block or make claims about history.

## Pitfalls

- Never relay a signal based on this history — this skill is read-only context only
- An empty response means no recent history, not that trading is paused
- `mode: not_submitted` is normal when strategies are in demo/paper mode
- If `HERMX_DASH_AUTH=true` and you omit the token, you will receive a 401

## Verification

- [ ] Response contains recent signal records
- [ ] Each record has `symbol`, `side`, `mode`, `submitted_at`
- [ ] Unreachable endpoint → treat as unknown, do not block
