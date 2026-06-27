# Hermes Agent Brain (Nous) — Design

> **STATUS: NOT BUILT. This is a design document for a planned Phase 8 layer.**
> There is no LLM, no decision code, no learning/memory, and no scheduler in the
> repository today. In `REFACTOR_PLAN.md`'s target diagram this brain appears as the
> "Hermes analysis skill" — that is a **name only**. Nothing in this document
> describes existing behavior. Do not cite it as implemented.

This document specifies the **Layer 1** reasoning cap above the deterministic
execution stack. Everything below Layer 1 (the HermesExecutionSkill, the
ExecutionService, the CCXT adapters, the journals) is built and tested today; the
brain is the only piece that is still aspirational.

```text
Layer 1  [PLANNED]  Hermes Agent brain (Nous Research)   ← THIS DOCUMENT
   │  emits {venue, intent}; advisory only
   ▼
Layer 2  [BUILT]    HermesExecutionSkill   (the only agent-facing execution surface)
   ▼
Layer 3  [BUILT]    ExecutionService + journals   (all money-safety)
   ▼
Layer 4  [BUILT]    CCXT adapters   (OKX demo live-verified; others planned)
```

---

## 1. Purpose

The brain turns a raw signal plus *live* market context into a routed decision. It
is meant to do four things, none of which exist yet:

1. **Venue + action selection.** From a TradingView signal and current market
   context — liquidity/depth, fees, funding rate, leverage limits, venue health —
   decide the **best venue** and the **action** to take (open/close/reverse, size).
2. **Scheduled scanning (cron).** Run periodic scans independent of inbound alerts
   (e.g. funding-rate sweeps, venue-health checks, position reviews) and propose
   actions from them.
3. **Learning / persistent memory.** Keep memory keyed on **outcomes from the
   execution ledger** (fills, slippage, fees, PnL, reconciliation results) to learn
   which venues and strategies perform best, and bias future recommendations
   accordingly.
4. **(Optional) skill proposals.** Suggest new skills/strategies for human review —
   proposed, never auto-deployed into the money path.

## 2. Hard Architectural Boundary (non-negotiable)

The brain is **advisory / routing ONLY**.

- It emits a `{venue, intent}` **recommendation**. It does not execute.
- It **MUST** act exclusively through `HermesExecutionSkill → ExecutionService`. It
  **never** touches CCXT, never constructs an exchange client, never reads
  credentials, and never bypasses any gate, kill switch, or journal.
- The **deterministic layer validates and can VETO** any brain recommendation. If a
  recommendation violates gate precedence, risk constraints, symbol pause, or
  idempotency, it is rejected — the brain cannot override it.
- **Money-safety never moves into the LLM.** Idempotency, write-ahead journal
  transitions, reconciliation semantics, gate precedence, and kill switches stay in
  first-party deterministic code at Layers 2–3, exactly as they are today. The brain
  adds intelligence above the safety substrate; it never becomes part of it.

A useful mental model: the brain is a *strategist that files requests*. The
deterministic stack is the *clerk that can refuse them*. The clerk's rules are not
up for negotiation.

## 3. Inputs / Outputs Contract (proposed)

**Inputs**
- `signal`: normalized TradingView / scan-derived signal.
- `market_context`: per-candidate-venue liquidity/depth, fees, funding rate,
  leverage limits, venue health/reachability.
- `portfolio_context`: current positions, balances, exposure, per-symbol pause state
  (read-only view derived from the execution ledger / journals).
- `memory`: learned priors keyed on prior outcomes (see §4).

**Output (a recommendation, not an order)**
```json
{
  "venue": "okx",
  "intent": {
    "strategy_id": "solusdt_duo_base_dev_3h",
    "asset": "SOLUSDT",
    "target_side": "long",
    "target_notional_usd": 3000,
    "margin_mode": "isolated",
    "leverage": 2
  },
  "rationale": "human-readable why-this-venue/why-this-action",
  "confidence": 0.0
}
```

The recommendation is handed to `HermesExecutionSkill`, which builds the normalized
`execution_intent` and calls `ExecutionService`. The skill/service remain the
authority on whether anything is actually submitted (dry-run vs live, gates, risk).
The brain's `venue`/`intent` is an input to that deterministic flow — never a
substitute for it.

## 4. Memory / Learning Loop

- **Source of truth is the execution ledger**, not the brain. After each terminal
  outcome (FILLED/REJECTED/UNKNOWN, with actual fill size/price, fees, slippage,
  reconciliation result), the outcome is fed back into the brain's persistent memory.
- Memory is keyed on outcome identity (venue × strategy × symbol × conditions) so the
  brain can learn, e.g., "venue A fills SOL perps with less slippage at this funding
  regime." Learned priors **bias** future recommendations; they never change
  money-safety behavior.
- Memory is advisory state. Corrupt/empty/unavailable memory must degrade to a
  safe default (see §6), never block execution and never alter the deterministic
  gates.

## 5. Cron / Scan Model

- The brain may schedule periodic scans (funding sweeps, venue-health checks,
  position reviews) that produce candidate recommendations **on the same advisory
  contract** as alert-driven ones.
- Scan-produced recommendations flow through the identical
  `HermesExecutionSkill → ExecutionService` path — there is **no privileged cron
  execution path** that bypasses gates. A cron-originated action is gated exactly
  like a webhook-originated one.

## 6. Failure Posture (fail-safe, never block safety)

- **Brain down / unreachable / times out** → fall back to the **strategy file's
  default venue** and the deterministic path; the system keeps operating without the
  brain. The brain is an enhancement, not a dependency of execution.
- **Brain uncertain / low confidence** → defer to the strategy default venue rather
  than guess; do not invent a venue the strategy didn't sanction.
- **The brain can never block money-safety.** It cannot force a submit, cannot
  silence an alert, cannot clear a pause, cannot disable the kill switch. Its only
  powers are *recommend* and *defer*.
- A brain failure is a **degraded** condition (operator-visible), not an outage of
  the execution stack.

## 7. Observability / Audit

- **Every brain decision is logged with its rationale** — inputs considered,
  candidate venues, chosen `{venue, intent}`, confidence, and why. Recommendations
  are auditable independently of whether they were accepted, vetoed, or deferred.
- The deterministic layer's veto/accept decision is logged alongside the
  recommendation, so a reviewer can reconstruct: what the brain proposed, what the
  gates did with it, and what actually executed.
- Brain logs are separate from (and never a substitute for) the execution journals,
  which remain the authoritative money record.

## 8. Reference Points

- **Nous Research — Hermes Agent.** The intended basis for the reasoning/agent layer.
- **`hermes-quant` patterns** (daemon loop, strategy modules, portfolio management) as
  a *starter base* for the cron/scan daemon and portfolio-context shaping — adapted to
  HermX's hard boundary (advisory only; execution stays in the deterministic stack).

These are directional references, not committed dependencies; concrete library/model
choices are deferred to the Phase 8 implementation.

## 9. Phasing

This is **Phase 8** in `REFACTOR_PLAN.md`, gated behind:

1. **P6 multi-exchange enablement** — venue selection is the brain's core value and is
   meaningless with a single configured venue. Until ≥2 venues route end-to-end, there
   is nothing for the brain to choose between.
2. **A clean OKX live track record** — the deterministic stack must be proven in real
   use before a reasoning layer is allowed to influence it.

Rollout order:

1. **Advisory / shadow.** The brain runs and **logs recommendations without acting**.
   Compare its `{venue, intent}` against what the deterministic path actually did.
2. **Influence routing.** Only after the shadow period demonstrates sound, safe
   recommendations is the brain allowed to influence venue routing — still through the
   skill/service, still vetoable, still with money-safety entirely in the
   deterministic layer.
