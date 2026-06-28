# Hermes Agent Brain (Nous) — Design

> **STATUS: NOT BUILT. This is a design document for a planned Phase 8 layer.**
> There is no LLM, no decision code, no MCP server, no agent process, and no
> scheduler in the repository today. In `REFACTOR_PLAN.md`'s target diagram this
> brain appears as the "Hermes analysis skill" — that is a **name only**. Nothing in
> this document describes existing behavior. Do not cite it as implemented.
>
> **Design decisions locked 2026-06-27 (modes, triggers, confirm handshake, MCP
> transport, sizing authority).** The sections below record *decided* architecture,
> not shipped code. Every reference to existing machinery (gate chain, journals,
> dry-run path, readiness builder) names code that exists today and is cited so the
> design stays anchored to the real seam; every reference to the agent, modes, MCP
> tools, and the `orchestration_mode` gate is PROPOSED and not yet implemented.

This document specifies the **Layer 1** reasoning/orchestration cap above the
deterministic execution stack. Everything below Layer 1 (the HermesExecutionSkill,
the ExecutionService, the CCXT adapters, the journals) is built and tested today;
the brain — and the MCP tool surface it would speak through — is the only piece that
is still aspirational.

```text
Layer 1  [PLANNED]  Hermes Agent brain (Nous) + MCP tool surface     ← THIS DOCUMENT
   │   always-on process. Routes/advises only. NEVER self-initiates an order.
   │   speaks ONLY through the tiny typed MCP tools (status/preflight/propose/execute)
   ▼
Layer 2  [BUILT]    HermesExecutionSkill   (the only agent-facing execution surface)
   │   HermesExecutionSkill.execute — dry_run builds intent + submits NOTHING;
   │   live submits ONLY through the controlled ExecutionService.
   ▼
Layer 3  [BUILT]    ExecutionService + journals   (ALL money-safety lives here)
   │   ExecutionService.execute gate chain: kill switch → gate set → symbol pause →
   │   idempotency → write-ahead order journal → submit → post-submit reconciliation.
   ▼
Layer 4  [BUILT]    CCXT adapters   (OKX demo live-verified; others enabled in P6)
```

The brain never reaches below Layer 1's tool seam. The deterministic stack at
Layers 2–3 can **VETO** anything the brain asks for, and money-safety never moves
upward into the LLM.

---

## 1. Purpose

The brain turns signals, human commands, and live market context into **routed
decisions and answers** — never into orders it issues itself. It is meant to do four
things, none of which exist yet:

1. **Venue + action selection / routing.** In `agent` mode (see §2), from a
   TradingView signal and current market context decide which sanctioned strategy to
   run and route it to execution. Size and leverage are **never** chosen by the LLM
   (see §5).
2. **Operator interface (read-only Q&A and human commands).** Answer "what's open?",
   "current PnL?", "are we armed?" at any time, and carry out explicit human commands
   ("close SOLUSDT") under a mandatory confirm handshake (see §3 / §4).
3. **Scheduled scanning (cron) — advisory only.** Run periodic scans (funding-rate
   sweeps, venue-health checks, position reviews) that produce **reports and
   recommendations only**. Cron output **never** auto-executes (see §6).
4. **Learning / persistent memory.** Keep memory keyed on **outcomes from the
   execution ledger** to bias future *recommendations* — never to alter money-safety.

## 2. Two Operating Modes

A new config gate selects how much execution authority the agent carries.

> **PROPOSED — not yet implemented.** Config key:
> `strategy_engine.orchestration_mode`, default `"deterministic"`. (`strategy_engine`
> already exists as a config block — `STRATEGY_ENGINE = CONFIG.get("strategy_engine", {})`
> in `src/webhook_receiver.py` — so this is a new key in an existing block.)

**The agent process is always-on in BOTH modes.** The mode decides only whether
*webhook-driven execution authority* is live. It does not turn the brain on or off.

### mode = `deterministic` (default, current behavior)

- `src/webhook_receiver.py` drives the deterministic match → execute path exactly as
  it does today: a webhook signal is normalized, `build_strategy_execution_readiness`
  computes the plan, and `_execute_okx_via_service` → `ExecutionService.execute`
  submits (subject to all gates).
- The agent runs **in parallel as a read-only observer**. It sees the same signals
  and can log recommendations, but its **EXECUTE authority for webhook signals is
  DISARMED** — it cannot route a webhook signal to submission.
- Read-only Q&A (§4) and human commands under the confirm handshake (§3) still work.

### mode = `agent`

- A webhook signal flows: **signal → agent selects `strategy_id` → agent calls the
  `hermx_execute` tool → `ExecutionService`**. This is auto-executed (no human in the
  loop) through the *same* gate chain as today.
- Critically, size/leverage are still computed by **our** code from the strategy file
  (`build_strategy_execution_readiness` computes `planned_notional = budget_usd * leverage`),
  **never emitted by the LLM** (see §5).
- The agent's only added power here is *which sanctioned strategy to route* — the gates
  still decide whether anything submits.

The migration from `deterministic` → `agent` is the central step of the autonomy
roadmap (§9) and is gated behind a clean live track record.

## 3. Execution Origination — exactly two sources

There are **exactly two** ways an order can originate, and a **hard invariant** that
the agent can **NEVER self-initiate** one:

| Source | Allowed in | Human in loop? | Path |
|---|---|---|---|
| **(a) Webhook signal** | `agent` mode only | No (auto, gated) | signal → agent routes → `hermx_execute` → gates |
| **(b) Human command** (e.g. "close SOLUSDT") | **both** modes | **Yes, always** | propose → confirm token → `hermx_execute` → gates |

**Cron / scheduled scans are READ-ONLY / advisory ONLY.** They produce reports and
recommendations; they **never** auto-execute and have **no** path to submission. (This
revises older doc text that implied cron recommendations could flow to execution —
they cannot.)

> **HARD INVARIANT:** the brain has no spontaneous order authority. Absent (a) an
> inbound webhook signal in `agent` mode, or (b) an explicit human command that the
> human then confirms, **nothing the LLM "decides" can reach submission.** A timer
> firing, a scan finding something interesting, or the model "wanting" to act are all
> insufficient.

## 4. Human-Command Confirm Handshake (propose → token → confirm)

Human commands always require a two-step handshake. This is enforced **inside our own
tool**, because the Hermes Agent runtime **cannot gate an MCP tool by name** — its
approval mechanism only gates dangerous shell patterns and skill/memory writes (see
§10). So we do not rely on the agent framework to require confirmation; the
chokepoint enforces it itself.

1. **Propose / preview.** The agent calls `hermx_propose`, which returns a
   human-readable preview of *exactly* what will happen — action, symbol, side,
   computed notional (open) or close-size (close), venue, mode — plus a short-lived
   `confirmation_token`. It **submits NOTHING.** This is the existing
   `HermesExecutionSkill.execute` **dry_run** path: it builds the `execution_intent`
   and returns `not_submitted` without ever calling the service.
2. **Human approves.** The human reads the preview and approves.
3. **Confirm / execute.** The agent calls `hermx_execute` **with that
   `confirmation_token`**. This is the live path through `ExecutionService.execute`
   and its full gate chain.

A **missing, expired, or mismatched** token ⇒ **deny-closed**: no submission. The
token is the only bridge from preview to live submission for human commands; there is
no other door.

## 5. Read-Only Q&A — both modes, always, no confirmation

"what's open?", "current PnL?", "are we armed?", "any reconcile mismatches?" are
answered at any time, in either mode, with no confirmation, via the read-only
`hermx_status` tool. Read-only tools never touch the order journal as writers and
never reach the submit path.

## 6. Sizing Authority (money-critical)

**The LLM NEVER emits dollar amounts.** This is non-negotiable.

- **Open** (webhook signal *or* human "open"): notional is computed by **code** from
  the strategy file — `build_strategy_execution_readiness` already does
  `base_notional = budget_usd * leverage` and exposes it as `planned_notional` /
  `target_notional_usd`. The agent may select *which* sanctioned strategy; it does not
  type a size.
- **Close / flatten** (human "close SOLUSDT"): size = the **actual current position**
  read from our position journal / state, computed by **code** — not a number the LLM
  produces. The model names the symbol and the intent ("close"); the quantity comes
  from our own state.

If the model ever produces a number where a size is expected, that number is ignored;
the authoritative value is recomputed from the strategy file or the position journal.

## 7. Transport + Tool Surface (MCP, tiny + strictly typed)

The chosen transport is a small **MCP server** exposing a **tiny, strictly typed**
tool set. The agent gets **no** raw access to CCXT, the filesystem, or a terminal for
anything order-related — only these wrappers.

| Tool | Kind | Modes | What it does |
|---|---|---|---|
| `hermx_status` | read-only | both | positions, PnL, arm / kill-switch state, reconcile mismatches |
| `hermx_preflight_risk` | read-only | both | returns the risk / gate verdict for an intent **without submitting** |
| `hermx_propose` | read-only | both | builds intent + human-readable preview + `confirmation_token`; **submits nothing** (the `HermesExecutionSkill` dry_run path) |
| `hermx_execute` | **write** | both¹ | the **single chokepoint** to submission |

¹ `hermx_execute` accepts **either** a webhook-signal-originated intent (only honored
in `agent` mode) **or** a `confirmation_token` (human command, both modes). Either
way it runs the **full `ExecutionService.execute` gate chain**.

### Design invariants for the tool surface

- **One chokepoint / no side doors.** All submission goes through `hermx_execute` →
  `ExecutionService.execute`. There is no second path to an exchange.
- **Deny-closed.** Anything ambiguous, unauthorized, missing a required token, or
  hitting an unavailable controlled surface returns `not_submitted` (mirrors the
  existing `execution_unavailable` / blocked-gate posture in
  `_execute_okx_authoritative`).
- **Idempotent.** Stable `cl_ord_id`; the service rejects a duplicate `cl_ord_id`
  before any submit, so a re-issued tool call cannot double-fire.
- **Gates live in CODE, not in SKILL.md.** Skill text and tool descriptions are
  treated as **untrusted guidance**, never as a place where safety is enforced. The
  real gates are in `ExecutionService.execute` (Python), where the LLM cannot edit
  them.
- **No raw access.** The agent never sees CCXT, credentials, the filesystem, or a
  shell for order purposes — only the four tools above.

## 8. Mandatory Gates Registry (extensible safety floor)

Inside the chokepoint, money-safety is an **append-only mandatory-gate chain**. New
risk checks (drawdown limits, exposure / correlation caps, per-venue notional caps)
are added to this chain **without changing the tool surface and without granting the
agent any new freedom**. Adding a gate can only ever *reduce* what gets submitted.

Two distinct roles in the chain:

- **GUARDS** — may **veto** a submission. Today's guards, all in
  `ExecutionService.execute`:
  - **Kill switch:** `submit_kill_switch_armed()` / the `HERMX_SUBMIT_ENABLED`
    environment kill switch — engaged ⇒ `not_submitted`, before anything else.
  - **Gate set (precedence):** `should_execute` requires **all** of
    `readiness.live_execution_enabled` **and** `execution.enabled` **and**
    `execution.submit_orders` **and** `risk.allow_live_execution` **and**
    `auth_healthy` **and** `watchdog_ok`.
  - **Symbol pause:** `symbol_pause_info(symbol)` ⇒ `not_submitted` when paused.
  - **Idempotency:** a duplicate `cl_ord_id` (`latest_order_record`) ⇒
    `not_submitted` (`duplicate_cl_ord_id`).
  - **Future risk gates** (drawdown / exposure / venue caps) slot in here as new
    guards.
- **INTERCEPTORS** — **wrap / observe**, never alter the verdict. They cannot turn a
  veto into a pass or vice versa. Today's interceptors:
  - **Write-ahead order journal:** `record_order_state` transitions
    `PLANNED → SUBMITTED → FILLED / REJECTED / UNKNOWN` (fail-closed on journal write
    error via `fail_closed_state_write`).
  - **Post-submit reconciliation:** `reconcile_order_with_backoff`, with mismatch
    alerts (`emit_reconcile_alert`).
  - **Redaction / metrics:** `redact_secrets` and ledger appends (`append_jsonl`).

Because every future check is a **guard** added to a chain the agent cannot see or
edit, the safety **floor rises** over time **without** the agent's power rising. The
agent's authority is bounded by an envelope (§9); the gate chain is the floor.

## 9. Hard Architectural Boundary (non-negotiable)

The brain is **advisory / routing ONLY**.

- It emits a **recommendation** or a **routing decision** (which strategy, which
  venue) and answers questions. It does **not** execute.
- It **MUST** act exclusively through the MCP tools → `HermesExecutionSkill` →
  `ExecutionService`. It **never** touches CCXT, never constructs an exchange client,
  never reads credentials, and never bypasses any gate, kill switch, or journal.
- The **deterministic layer validates and can VETO** any agent action. If a routed
  intent violates gate precedence, risk constraints, symbol pause, or idempotency, it
  is rejected — the brain cannot override it.
- **Money-safety never moves into the LLM.** Idempotency, write-ahead journal
  transitions, reconciliation semantics, gate precedence, and the kill switch stay in
  first-party deterministic code at Layers 2–3 (`ExecutionService.execute`), exactly
  as they are today. The brain adds intelligence above the safety substrate; it never
  becomes part of it.

A useful mental model: the brain is a *strategist that files requests*. The
deterministic stack is the *clerk that can refuse them*. The clerk's rules are not up
for negotiation, and the clerk's veto is final.

## 10. Failure Posture (fail-safe, never block safety)

- **Brain down / unreachable / times out** → in `deterministic` mode nothing changes
  (the deterministic path already drives execution). In `agent` mode, fall back to the
  deterministic path / the strategy file's default venue; the system keeps operating
  without the brain. The brain is an enhancement, not a dependency of execution.
- **Brain uncertain / low confidence** → defer to the strategy default rather than
  guess; do not invent a venue or strategy the configuration didn't sanction.
- **The brain can never block money-safety.** It cannot force a submit, cannot silence
  an alert, cannot clear a pause, cannot disable the kill switch, cannot fabricate a
  `confirmation_token`. Its only powers are *recommend*, *route within the envelope*,
  *answer*, and *defer*.
- A brain failure is a **degraded** condition (operator-visible), not an outage of the
  execution stack.

## 11. Observability / Audit

- **Every brain decision is logged with its rationale** — inputs considered, candidate
  strategies / venues, the chosen route, confidence, and why. Recommendations are
  auditable independently of whether they were accepted, vetoed, or deferred.
- **Every confirm handshake is logged** — the preview shown, the `confirmation_token`
  issued, and whether/when it was redeemed at `hermx_execute`.
- The deterministic layer's veto / accept decision is logged alongside, so a reviewer
  can reconstruct: what the brain proposed, what the gates did with it, and what
  actually executed.
- Brain logs are separate from (and never a substitute for) the execution journals and
  ledger, which remain the authoritative money record.

## 12. Progressive Autonomy Roadmap

Autonomy advances by **widening the envelope, never loosening the gates**:

1. **Advisory / shadow** (`deterministic` mode). The agent observes webhook signals
   read-only, answers Q&A, and carries out **human-confirmed** manual commands. It logs
   recommendations but holds **no** webhook execute authority. Compare its routing
   against what the deterministic path actually did.
2. **Agent mode.** After the shadow period demonstrates sound, safe routing, flip
   `strategy_engine.orchestration_mode` to `agent`: webhook signals auto-execute
   through the gates, human-confirmed manual commands still work — all within a **hard
   envelope**.
3. **Widen the envelope only.** Grow the agent's freedom by adjusting **caps**
   (per-trade size, allowed symbols, daily notional), **never** by removing or
   softening gates. The gate chain (§8) is monotone: it only ever gets stricter.

Still gated behind:

1. **P6 multi-exchange enablement — DONE.** Venue selection is meaningless with a
   single venue; ≥2 venues now route end-to-end (Phase 6 complete).
2. **A clean OKX live track record.** The deterministic stack must be proven in real
   use before the agent is allowed to influence it (i.e. before flipping to `agent`
   mode).

## 13. Hermes-Specific Gotchas (record these)

- **(a) Cannot gate an MCP tool by name.** The Hermes Agent runtime's approval
  mechanism gates dangerous shell patterns and skill/memory writes — **not** MCP tools
  by name. Therefore the confirm handshake (§4) is enforced **inside our own tool**
  (token issued by `hermx_propose`, required by `hermx_execute`), not by the agent
  framework.
- **(b) Cron / blueprints run headless.** Scheduled runs have **no human present** and
  default-deny on approvals. The **gate chain is the only safety** for any scheduled
  run — which is precisely why cron here is **read-only / advisory** (§3, §6) and has
  no execution path at all.

## 14. Inputs / Outputs Contract (proposed)

**Inputs**
- `signal`: normalized TradingView / scan-derived signal.
- `market_context`: per-candidate-venue liquidity/depth, fees, funding rate, leverage
  limits, venue health / reachability.
- `portfolio_context`: current positions, balances, exposure, per-symbol pause state
  (read-only view derived from the execution ledger / journals, surfaced via
  `hermx_status`).
- `memory`: learned priors keyed on prior outcomes (see §15).

**Output (a recommendation / routing decision, not an order)**
```json
{
  "venue": "okx",
  "intent": {
    "strategy_id": "solusdt_duo_base_dev_3h",
    "asset": "SOLUSDT",
    "target_side": "long",
    "margin_mode": "isolated"
  },
  "rationale": "human-readable why-this-strategy / why-this-venue",
  "confidence": 0.0
}
```

> Note the absence of any size/leverage field the LLM controls: notional is computed
> by code (`build_strategy_execution_readiness`, §6). The recommendation is handed to
> the tool surface; `HermesExecutionSkill` builds the normalized `execution_intent`
> and `ExecutionService` remains the authority on whether anything is submitted. The
> brain's `venue`/`intent` is an *input* to that deterministic flow — never a
> substitute for it.

## 15. Memory / Learning Loop

- **Source of truth is the execution ledger**, not the brain. After each terminal
  outcome (FILLED / REJECTED / UNKNOWN, with actual fill size/price, fees, slippage,
  reconciliation result), the outcome is fed back into the brain's persistent memory.
- Memory is keyed on outcome identity (venue × strategy × symbol × conditions) so the
  brain can learn, e.g., "venue A fills SOL perps with less slippage at this funding
  regime." Learned priors **bias** future recommendations; they never change
  money-safety behavior or size.
- Memory is advisory state. Corrupt / empty / unavailable memory must degrade to a
  safe default (§10), never block execution and never alter the deterministic gates.

## 16. Reference Points

- **Nous — Hermes Agent.** The intended basis for the reasoning / agent layer and the
  MCP tool-calling surface.
- **`hermes-quant` patterns** (daemon loop, strategy modules, portfolio management) as
  a *starter base* for the always-on process and portfolio-context shaping — adapted
  to HermX's hard boundary (advisory / routing only; execution stays deterministic).

These are directional references, not committed dependencies; concrete library/model
choices are deferred to the Phase 8 implementation.

## 17. Phasing

This is **Phase 8** in `REFACTOR_PLAN.md`. See §12 for the gating conditions (P6 done;
clean OKX live track record required) and the advisory → agent → widen-envelope
rollout order.
