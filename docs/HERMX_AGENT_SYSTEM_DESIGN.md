# HermX + Hermes Agent — Trading Assistant System Design

**Status:** Living engineering specification
**Last updated:** 2026-06-28
**Owner:** HermX
**Scope:** End-to-end design for the HermX execution core and the Hermes Agent orchestration layer, including planned intelligence (Kronos, MXC risk, TradingView CDP) and the user communication layer (Telegram/WhatsApp).

> **Reading note.** Sections 1–6 and 10–11 describe the system as designed; where a
> component is **already built and tested**, it is marked **[BUILT]**. Where it is
> **planned**, it is marked **[PLANNED]**. The intelligence roadmap (§7) and
> implementation plan (§12) make the build/plan boundary explicit. The single most
> important invariant in this document: **money-safety lives in Python gate code, never
> in agent/skill prose.** Every "the agent decides" statement is bounded by that.

---

## Table of Contents

1. [Vision & Goals](#1-vision--goals)
2. [System Architecture](#2-system-architecture)
3. [Signal Processing Pipeline](#3-signal-processing-pipeline)
4. [Hermes Agent Orchestration Design](#4-hermes-agent-orchestration-design)
5. [Skills Specification](#5-skills-specification)
6. [User Communication Layer](#6-user-communication-layer)
7. [Intelligence Evolution Roadmap](#7-intelligence-evolution-roadmap)
8. [Deployment Architecture](#8-deployment-architecture)
9. [Reliability Design](#9-reliability-design)
10. [Security Model](#10-security-model)
11. [Data & State Model](#11-data--state-model)
12. [Implementation Plan](#12-implementation-plan)

---

## 1. Vision & Goals

### 1.1 The thesis

HermX is a money-safety-critical execution core that already turns a TradingView
webhook into an exchange order **deterministically**, through a Python gate chain
(kill switch → gate precedence → symbol pause → idempotency → write-ahead journal →
CCXT submit → reconciliation). It is correct and boring on purpose.

The **Hermes Agent** is the layer on top: an always-on LLM orchestrator (Nous Research
Hermes) that behaves like a disciplined human trading assistant. It reads system
state, answers the operator's questions in natural language, relays sanctioned
signals, and — as it earns trust — applies judgment *before* an order is allowed to
submit. It never replaces the deterministic gates; it sits in front of them as an
additional, removable, fail-open check.

The product goal is a system where the operator can ask, over Telegram, *"what's
open, what's our PnL, is it armed, what happened with the last alert?"* and get a
truthful answer — and where, over time, the agent can say *"this signal looks weak,
Kronos disagrees and the chart is choppy, I'm vetoing it"* with the operator's
blessing.

### 1.2 Design goals

| Goal | Meaning |
|------|---------|
| **Discretionary, like a human** | The agent reasons over multiple signals (price action, Kronos prediction, MXC risk, chart state) and makes a call — within hard, code-enforced bounds. |
| **Conversational operations** | The operator runs the desk from a phone via Telegram/WhatsApp: status, PnL, kill-switch state, last alert, cancel/confirm. |
| **Progressive autonomy** | The agent's authority increases in discrete, individually-reversible phases: **advisory → gated (veto) → conviction-scored → discretionary → proactive.** Each step has an explicit entry gate and an instant rollback (an env flag). |
| **Copy-and-deploy** | Anyone can clone the repo, fill a `.env`, run one install script, and have their own VPS instance with a unique stable ingress URL (their own Tailscale tailnet). No shared infrastructure, no central service. |
| **Fail safe, fail open** | Safety gates fail *closed* (block the order). Intelligence layers fail *open* (proceed deterministically) so a slow/broken LLM or a down Kronos API can never wedge the desk. |

### 1.3 Non-goals

- The agent is **not** a high-frequency system. Signals arrive **1–2 per day** on
  **2h–4h** strategy timeframes (canonical set: `30m, 1h, 2h, 3h, 4h`). There is no
  latency budget that requires the agent to be fast — a 30s LLM round-trip is fine.
- The agent does **not** compute position size. Ever. Sizing is `capital.budget_usd * leverage`
  from the strategy file, computed in the receiver.
- The agent does **not** hold exchange credentials, shell, or filesystem access for
  order purposes. Its only act path is `POST /webhook` on loopback.

---

## 2. System Architecture

### 2.1 Three layers

| Layer | Responsibility | Components |
|-------|----------------|------------|
| **Ingress** | Receive signals and operator messages from the outside world; authenticate them. | Tailscale Funnel, webhook receiver (`:8891`), messenger gateway (Telegram/WhatsApp) |
| **Orchestration** | Reason, sequence validations, answer the operator, relay sanctioned signals. | Hermes Agent + skill catalog (loopback HTTP only) |
| **Execution** | Deterministic, money-safe order path. | `ExecutionService` gate chain, CCXT executor, ledgers, dashboard (`:8098`) |

The critical architectural rule: **the orchestration layer talks to the execution
layer only through the same loopback HTTP API a human would use** (`GET :8098/api`,
`GET :8098/health`, `GET :8891/health`, `GET :8891/latest`, `POST :8891/webhook`).
There is no privileged backchannel, no MCP server, no direct function calls into the
gate chain. This is what makes the agent *removable* and the safety *independent of
the agent*.

### 2.2 Component & data-flow diagram

```
                                   ┌──────────────────────────────────────────────┐
   EXTERNAL                        │                  THE VPS (one box)             │
                                   │                                                │
 ┌───────────────┐                 │   INGRESS LAYER                                 │
 │  TradingView   │   HTTPS POST    │  ┌──────────────────────────────────────────┐ │
 │  Pine alert    │────────────────▶│  │ Tailscale Funnel  (public, --bg)         │ │
 │  (1-2/day)     │  X-Webhook-     │  │ https://hermx.<tailnet>.ts.net/webhook   │ │
 └───────────────┘   Secret        │  └─────────────────────┬────────────────────┘ │
                                   │                         │ loopback             │
 ┌───────────────┐  Bot API /      │  ┌──────────────────────▼────────────────────┐ │
 │ Operator       │  Business API   │  │ Webhook Receiver  127.0.0.1:8891          │ │
 │ phone          │◀───────────────▶│  │  /webhook  /health  /latest               │ │
 │ Telegram /     │  (messenger     │  │  auth → rate-limit → normalize → schema → │ │
 │ WhatsApp       │   gateway)      │  │  dedupe → queue → worker                  │ │
 └───────────────┘                 │  └───────────┬──────────────────────┬────────┘ │
                                   │              │ readiness            │ enqueue   │
                                   │  ORCHESTRATION LAYER                 │           │
                                   │  ┌───────────▼──────────────────┐   │           │
                                   │  │ Hermes Agent (always-on)     │   │           │
                                   │  │  loads skills, reasons,      │   │           │
                                   │  │  answers operator, relays    │   │           │
                                   │  │  sanctioned signals          │   │           │
                                   │  │                              │   │           │
                                   │  │  skills (loopback HTTP only):│   │           │
                                   │  │   hermx-control      [BUILT] │   │           │
                                   │  │   signal-memory    [PLANNED] │   │           │
                                   │  │   kronos-validate   [PLAN]   │───┼──┐        │
                                   │  │   dashboard-risk    [PLAN]   │───┼──┼──┐     │
                                   │  │   tradingview-chart [PLAN]   │───┼──┼──┼─┐   │
                                   │  │   telegram: hermes gateway   │   │  │  │ │   │
                                   │  └──────────────┬───────────────┘   │  │  │ │   │
                                   │   advisor seam  │ (pre-exec, opt-in) │  │  │ │   │
                                   │                 ▼                    ▼  │  │ │   │
                                   │  EXECUTION LAYER                        │  │ │   │
                                   │  ┌──────────────────────────────────┐  │  │ │   │
                                   │  │ ExecutionService.execute()        │  │  │ │   │
                                   │  │  GATE CHAIN (fail-closed):        │  │  │ │   │
                                   │  │   1 kill switch                   │  │  │ │   │
                                   │  │   2 gate precedence (AND)         │  │  │ │   │
                                   │  │   3 symbol pause                  │  │  │ │   │
                                   │  │   4 idempotency (cl_ord_id)       │  │  │ │   │
                                   │  │   5 write-ahead order journal     │  │  │ │   │
                                   │  │   6 CCXT submit + reconcile       │  │  │ │   │
                                   │  └───────────────┬──────────────────┘  │  │ │   │
                                   │                  │                      │  │ │   │
                                   │  ┌───────────────▼─────────┐  ┌─────────▼──▼─▼─┐ │
                                   │  │ CCXT Executor           │  │ Ledgers (JSONL) │ │
                                   │  │ okx/kucoin/bybit/hyperl │  │ + state JSON    │ │
                                   │  └───────────┬─────────────┘  └────────┬───────┘ │
                                   │              │                         │ reads    │
                                   │  ┌───────────▼─────────┐   ┌───────────▼───────┐ │
                                   │  │ Exchange (OKX perp) │   │ Dashboard :8098   │ │
                                   │  └─────────────────────┘   │  /api /health  /  │ │
                                   │            ▲               └───────────────────┘ │
                                   └────────────┼──────────────────────────────────┬─┘
                                                │ REST (CCXT)                       │
                ┌───────────────────────────────┘            EXTERNAL APIs (egress) │
                │                                                                    │
          ┌─────▼──────┐   ┌──────────────────────────┐   ┌────────────────────────▼┐
          │ OKX / etc. │   │ Kronos API [PLANNED]      │   │ MXC Dashboard [PLANNED]  │
          │ exchange   │   │ candle-prediction model   │   │ mxc-kinetic-crypto       │
          └────────────┘   └──────────────────────────┘   │  .replit.app (risk)      │
                           ┌──────────────────────────┐   └──────────────────────────┘
                           │ TradingView (CDP desktop) │
                           │ chart screenshot/indics   │ [PLANNED]
                           └──────────────────────────┘
```

### 2.3 External integrations

| Integration | Direction | Transport | Status |
|-------------|-----------|-----------|--------|
| **TradingView webhook** | inbound | HTTPS → Tailscale Funnel → `:8891/webhook` | [BUILT] |
| **TradingView CDP desktop** | outbound (read) | Chrome DevTools Protocol on operator desktop, surfaced as MCP tools | [PLANNED] |
| **Exchanges (OKX/KuCoin/Bybit/Hyperliquid)** | outbound | CCXT REST | [BUILT] |
| **Kronos API** | outbound | HTTPS POST (OHLCV + signal → conviction) | [PLANNED] |
| **MXC Dashboard** | outbound (read) | HTTPS GET `mxc-kinetic-crypto.replit.app` | [PLANNED] |
| **Telegram** | bidirectional | Telegram Bot API (long-poll/webhook) | [PLANNED] |
| **WhatsApp** | bidirectional | WhatsApp Business Cloud API | [PLANNED] |

---

## 3. Signal Processing Pipeline

### 3.1 Full flow: Pine Script alert → exchange order

```
[1] TradingView Pine strategy fires an alert (2h/4h close)
        │  webhook URL: https://hermx.<tailnet>.ts.net/webhook
        │  header:      X-Webhook-Secret: <HERMX_SECRET>
        ▼
[2] Tailscale Funnel forwards to 127.0.0.1:8891 (no inbound firewall rule)
        ▼
[3] Webhook receiver (src/webhook_receiver.py, http.server, threaded):
      a. authenticate_webhook_request()  → X-Webhook-Secret (+ optional HMAC)
      b. rate_limit_allow()              → sliding window per source IP
      c. body size guard                 → HERMX_MAX_BODY_BYTES (262144)
      d. normalize()                     → uppercase symbol, canonical timeframe
      e. JSON schema validate            → schemas/tradingview-alert.schema.json
      f. dedupe                          → seen-signals (HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS)
      g. enqueue                         → PROCESS_QUEUE (maxsize HERMX_QUEUE_MAXSIZE)
        ▼
[4] Worker thread dequeues:
      a. match strategy_id → strategies/*.json
      b. build_strategy_execution_readiness()
            - resolves instrument (e.g. SOL-USDT-SWAP)
            - computes notional = capital.budget_usd * leverage
            - resolves td_mode / margin_mode / pos_mode
            - attaches health-gate context (MXC pp_acc / pp_vel if required)
        ▼
[5] ── PRE-EXECUTION ADVISOR SEAM (Hermes intercept point) ── [BUILT, default OFF]
      run_execution_advisor(readiness):
        - if HERMX_ADVISOR_ENABLED is false → skip entirely (byte-identical to no-advisor)
        - else: one-shot `hermes -z "<prompt>" --skills <HERMX_ADVISOR_SKILLS>`
        - agent returns {action: proceed|skip, risk_note?, score?}
        - FAILS OPEN: timeout / malformed / missing binary → proceed
        - when enabled, a `skip` blocks the trade (reason: vetoed_by_advisor)
        ▼
[6] ExecutionService.execute(readiness)  ← deterministic gate chain (see §3.4)
        ▼
[7] CcxtExecutor.execute(readiness)  → OKX perpetual swap order (clOrdId set)
        ▼
[8] Order journal records terminal state; dashboard :8098 reflects it; ledgers append.
```

### 3.2 Where Hermes intercepts

Hermes participates at **two** points, both already wired or trivially extendable:

1. **The advisor seam (step 5)** — `run_execution_advisor()` /
   `execute_okx_with_advisor()` in `src/webhook_receiver.py`. This is the *inline*
   intercept: before submission, the LLM gets the readiness context and returns a
   binary `proceed | skip`. This is where Kronos/chart/risk validation will be folded
   in (the advisor prompt expands to "consult your skills, then decide"). It is
   **fail-open** and gated by two independent env flags.
2. **The conversational path** — the operator talks to the agent over Telegram; the
   agent reads `/api` + `/health` and answers, or relays a human-instructed signal to
   `/webhook`. This path does not touch step 5; it produces a *new* inbound signal
   that re-enters at step 3.

### 3.3 How each intelligence source contributes

| Source | What it answers | How it's consulted | Failure posture |
|--------|-----------------|--------------------|-----------------|
| **Kronos** [PLANNED] | "Does a learned candle-prediction model agree with the signal's direction, and how strongly?" | `kronos-validate` skill POSTs OHLCV + signal; returns `direction_prob` + `conviction`. | Timeout/down → treated as `UNKNOWN`, contributes nothing, never blocks. |
| **MXC Dashboard** [PLANNED] | "What's the current regime/risk context (pp_acc, pp_vel, risk-on/off)?" | `dashboard-risk` skill GETs the MXC page, parses metrics into a structured risk assessment. | Unreachable → `risk: UNKNOWN`; the deterministic health gate already routes signal-only strategies (`duo_raw`) without MXC. |
| **TradingView CDP** [PLANNED] | "Does the actual chart confirm the setup (indicator values, clean structure, not mid-candle)?" | `tradingview-chart` skill drives the operator's desktop Chrome via CDP MCP tools: screenshot + read indicators. | Desktop offline → `chart: UNKNOWN`, contributes nothing. |
| **signal-memory** [PLANNED] | "What did we just do — recent decisions, open positions, daily PnL — so we don't double up or fight ourselves?" | `signal-memory` skill returns a rolling log of last N signals + outcomes. | Empty/stale → agent proceeds with reduced context, flags it. |

### 3.4 Deterministic gate chain (the floor under everything)

`ExecutionService.execute()` (`src/execution/service.py`) runs **regardless of what the
agent decided**. Order is fixed; all must pass to submit.

| # | Gate | Check | Block outcome (`mode` / `reason`) |
|---|------|-------|-----------------------------------|
| 1 | **Kill switch (live only)** | `HERMX_LIVE_TRADING`: `execution_mode=live` orders require `=true`; `false`/unset blocks every live submit. `execution_mode=demo` is unaffected. | `not_submitted` / `"HERMX_LIVE_TRADING kill switch engaged"` |
| 2 | **Strategy active + mode** | Strategy has valid `execution_mode` (`demo` → OKX sandbox/paper, always allowed; `live` → real account, subject to gate 1), AND health: `auth_healthy`, `watchdog_ok` | `not_submitted` / first failing control |
| 3 | **Symbol pause** | `symbol_pause_info(symbol)` empty | `not_submitted` / `"symbol_paused"` |
| 4 | **Idempotency** | no existing order for derived `client_order_id` | `not_submitted` / `"duplicate_cl_ord_id"` |
| 5 | **Write-ahead journal** | record `PLANNED` then `SUBMITTED`; fail-closed on `OSError` | `not_submitted` / state-write error |
| 6 | **CCXT submit + reconcile** | `executor.execute()`; classify result | `filled` / `rejected` / `unknown` |

`watchdog_ok` = worker heartbeat younger than `HERMX_WATCHDOG_STALE_SECONDS` (120) AND
queue lag below `HERMX_QUEUE_LAG_SLO_SECONDS` (30). `auth_healthy` = webhook auth
configured and secret present.

### 3.5 Decision matrix — combined-input → outcome

This is the **target** decision logic for **Phase 3** (conviction scoring). In Phase 1
the agent has no vote; in Phase 2 it has binary veto. The matrix below shows how the
agent will combine inputs into an advisor verdict (`proceed | skip` + score) — which is
*then* still subject to the deterministic gate chain (§3.4). The agent can only make a
"go" into a "no-go"; it can never turn a gate-blocked order into a submitted one.

Legend: ✓ = confirms signal, ✗ = contradicts, **?** = UNKNOWN/unavailable.

| TV signal | Kronos | Chart (CDP) | MXC risk | Agent verdict | Rationale |
|-----------|--------|-------------|----------|---------------|-----------|
| fire | ✓ high conv | ✓ clean | risk-on | **APPROVE** (score ≥ 80) | full confluence |
| fire | ✓ high conv | ✓ clean | **?** | **APPROVE** (score ~70) | MXC optional for signal-only strategies |
| fire | ✓ med conv | **?** | risk-on | **APPROVE** (score ~65) | majority confirm, no contradiction |
| fire | ✗ disagrees | ✓ clean | risk-on | **MODIFY→skip or flag** | Kronos contradiction → in P3, below threshold → veto; in P2 operator-confirm |
| fire | ✗ disagrees | ✗ choppy | risk-off | **VETO** (score < 30) | broad contradiction |
| fire | **?** | **?** | **?** | **APPROVE (deterministic)** | all intelligence down → fail open, gate chain still governs |
| fire | ✓ | ✓ | risk-off | **VETO/flag** | regime risk-off overrides a clean setup at higher autonomy |

> **MODIFY** never means "change the size/side." HermX has no modify-order path and the
> agent cannot construct one. "MODIFY" here means the agent **downgrades to skip** or
> **routes to operator confirmation** (Phase 2+). The only three real outcomes the
> agent can produce are **proceed**, **skip (veto)**, and **escalate-to-human**.

---

## 4. Hermes Agent Orchestration Design

### 4.1 Agent lifecycle

- **Always-on.** The Hermes Agent runs as a long-lived process on the VPS (`hermes`
  binary), supervised by systemd alongside the receiver and dashboard, `Restart=always`.
- **Stateless by design.** The agent holds no authoritative state in memory. On
  restart it reloads its skills and reconstructs context by *reading* the system
  (`/api`, `/health`, `/latest`) and the `signal-memory` ledger. A crash loses nothing
  that matters — the money record lives in HermX ledgers, not the agent.
- **Skill-loaded.** At start it loads the skill catalog (§4.2). Skills are markdown
  contracts + the loopback HTTP endpoints they may call. Reloading a skill is just
  re-reading a file.
- **Two invocation modes:**
  - *One-shot advisor* — the receiver spawns `hermes -z "<prompt>" --skills hermx-control`
    at the advisor seam, gets one verdict, exits. This is the [BUILT] inline path.
  - *Conversational daemon* — the messenger gateway feeds operator messages to a
    persistent agent session that answers and (on instruction) relays signals. [PLANNED]

### 4.2 Skill catalog

| Skill | Status | Purpose | Endpoints / tools |
|-------|--------|---------|-------------------|
| `hermx-control` | **[BUILT]** | Read state + relay signals over loopback. | `GET :8098/api`, `GET :8098/health`, `GET :8891/health`, `GET :8891/latest`, `POST :8891/webhook` |
| `signal-memory` | [PLANNED] | Rolling log of last N signals + outcomes for context. | reads `logs/executions.jsonl`, `logs/advisor-decisions.jsonl` (via a small read endpoint, not raw FS) |
| `kronos-validate` | [PLANNED] | Validate candle-prediction direction/conviction. | `POST <KRONOS_API_URL>/predict` |
| `dashboard-risk` | [PLANNED] | Structured risk read from MXC dashboard. | `GET mxc-kinetic-crypto.replit.app` |
| `tradingview-chart` | [PLANNED] | Screenshot + indicator read + chart validation. | TradingView CDP MCP tools |

> **Operator comms are not a skill.** Telegram operator interaction is handled via Hermes'
> native gateway (`hermes gateway` config with `TELEGRAM_BOT_TOKEN` and
> `TELEGRAM_ALLOWED_USERS`). No separate skill is required.

### 4.3 Skill interface contract

Every skill MUST satisfy this contract so the agent can sequence them safely:

```
Skill contract
──────────────
name:        kebab-case, unique
purpose:     one sentence; when to use / when NOT to use
inputs:      explicit, typed fields the agent must supply
outputs:     structured object with a mandatory `status` ∈ {ok, unknown, error}
endpoints:   exact URLs/tools the skill may touch — and ONLY those
fail mode:   what `status` and what the agent should conclude on failure
boundaries:  what the skill must NEVER do (touch an exchange, invent a size, etc.)
```

Two contract rules are load-bearing:

1. **A skill never returns a falsely-confident value.** A read failure or stale data
   returns `status: unknown` — never a fabricated "flat / no risk / signal confirmed."
   (This is enforced in prose for `hermx-control` today: *"Never report a failed/empty
   read as flat."*)
2. **A skill cannot widen its own authority.** The endpoints list is exhaustive. No
   skill may call an exchange, a shell, or the filesystem for order purposes. The only
   write the agent can cause is `POST /webhook`.

### 4.4 Multi-skill validation sequence

For a signal the agent is asked to evaluate (Phase 2+), the sequence is:

```
1. signal-memory.recent()        → are we already in this symbol? recent contradictory action?
2. kronos-validate.predict(ohlcv, signal)
                                  → direction_prob, conviction
3. dashboard-risk.assess(symbol) → regime, risk_on/off
4. tradingview-chart.validate(symbol, timeframe)
                                  → clean? indicators confirm? mid-candle?
5. aggregate → decision matrix (§3.5) → {proceed | skip | escalate} (+ score in P3)
```

The agent runs 1–4 **in parallel where possible** (they are independent reads) and
aggregates. It must **degrade gracefully**: any `status: unknown` contributes nothing
and is named explicitly in the verdict's `risk_note`.

### 4.5 Timeout handling

| Situation | Behavior |
|-----------|----------|
| A single skill is slow | Per-skill deadline (default 10s). On expiry → that input is `UNKNOWN`; aggregation continues. |
| The whole advisor is slow | `HERMX_ADVISOR_TIMEOUT_SECONDS` (default 30) bounds the inline advisor. On expiry → **proceed deterministically** (fail open). |
| Kronos / MXC / CDP down | Skill returns `status: unknown`; never blocks; logged to `advisor-decisions.jsonl`. |
| Messenger gateway down | Operator comms degrade; **execution path is unaffected** (it does not depend on messaging). |

The governing principle: **no intelligence dependency can wedge the desk.** Every
optional layer fails open; only the deterministic gates fail closed.

### 4.6 Agent memory

The agent's working memory across signals is reconstructed, not retained:

| What it "remembers" | Source of truth | Scope |
|---------------------|-----------------|-------|
| Recent decisions | `logs/advisor-decisions.jsonl` via `signal-memory` | last N (default 50) |
| Open positions | `GET :8098/api` → `okx_live.positions` | live snapshot |
| Daily PnL | `/api` → `upl` + `realized_pnl` aggregates | rolling day |
| Kill-switch / arm state | `GET :8098/health` → `arm` block | live |
| Last alert | `GET :8891/latest` | most recent |

Because every "memory" is a read, the agent is correct immediately after a restart and
can never drift from the money record.

---

## 5. Skills Specification

Each skill below follows the §4.3 contract. `[BUILT]` is shipped; `[PLANNED]` is spec.

---

### 5.a `hermx-control` **[BUILT]**

**Purpose.** Read HermX state and relay sanctioned signals, talking only to the local
HermX HTTP API on loopback. Never touches an exchange; never invents a size.

**Inputs.**
- For reads: none (or a sub-query: positions / pnl / armed / last-alert).
- For relay: a complete TradingView alert object (see below).

**Outputs.** Natural-language answer backed by structured reads, or the receiver's
`POST /webhook` response, relayed truthfully.

**Endpoints.**
- `GET http://127.0.0.1:8098/api` — positions, PnL, executor health, ledger health, freshness
- `GET http://127.0.0.1:8098/health` — `arm` block (`live_trading_enabled`, per-strategy `execution_mode`, `armed_summary` — the summary reflects `execution_mode`)
- `GET http://127.0.0.1:8891/health`, `GET http://127.0.0.1:8891/latest`
- `POST http://127.0.0.1:8891/webhook`

**Relay payload (required fields).**

```json
{
  "strategy_id": "duo_regime_rsi_30m",
  "symbol": "SOLUSDT",
  "timeframe": "2h",
  "side": "buy",
  "tv_signal_price": 168.42,
  "tv_time": "2026-06-28T14:00:00Z",
  "exchange": "okx",
  "source": "tradingview"
}
```
> **No `size`/`notional`/`leverage` field exists.** The receiver computes notional from
> the strategy file (`capital.budget_usd * leverage`). Adding one is a boundary violation.

**Fail behavior.** A 4xx/5xx or stale/degraded `executor` ⇒ report **UNKNOWN**, never
"flat / no positions." Close/flatten is **not supported** via this API; the agent must
say so rather than improvise a close via `/webhook`.

**Example.**

> **Prompt:** "Are we armed and what's open?"
> **Agent:** *(GET /health → `armed_summary:false, live_trading_enabled:false`; GET /api → SOL-USDT-SWAP LONG)*
> "Not armed for live — `HERMX_LIVE_TRADING` is off, so no `execution_mode=live` order will
> submit. One position is open: **SOL-USDT-SWAP LONG**, ~$420 notional, uPnL **+$11.30**,
> 3× isolated. I can't change the kill switch from here."

---

### 5.b `kronos-validate` **[PLANNED]**

**Purpose.** Ask a learned candle-prediction model (Kronos LLM) whether it agrees with
the signal's direction and how strongly.

**Inputs.** Recent OHLCV window for the symbol/timeframe + the signal (`side`,
`tv_signal_price`, `timeframe`).

**Outputs.**

```json
{
  "status": "ok",
  "direction": "up",
  "direction_prob": 0.71,
  "conviction": 72,
  "horizon_candles": 3,
  "model": "kronos-v1",
  "agrees_with_signal": true
}
```

**API.** `POST <KRONOS_API_URL>/predict`

```json
// request
{
  "symbol": "SOL-USDT-SWAP",
  "timeframe": "2h",
  "ohlcv": [[ts,o,h,l,c,v], ...],   // last K candles
  "signal": {"side": "buy", "price": 168.42}
}
```

**Fail behavior.** Timeout/HTTP error/malformed ⇒ `status: unknown`; contributes
nothing to the decision matrix; logged. Never blocks.

**Example.**

> **Agent (internal):** validate the 2h SOL buy.
> **Kronos:** `{direction:"up", direction_prob:0.71, conviction:72, agrees_with_signal:true}`
> **Agent conclusion:** "Kronos confirms direction (72/100)." → contributes a ✓ at med-high conviction.

---

### 5.c `dashboard-risk` **[PLANNED]**

**Purpose.** Read the MXC Kinetic Crypto dashboard and return a structured risk
assessment (regime, acceleration/velocity, risk-on/off) for context.

**Inputs.** `symbol` (and optionally timeframe).

**Outputs.**

```json
{
  "status": "ok",
  "symbol": "SOLUSDT",
  "regime": "BULL",
  "pp_acc": 0.42,
  "pp_vel": 0.18,
  "risk_state": "risk_on",
  "as_of": "2026-06-28T14:01:10Z"
}
```

**API.** `GET https://mxc-kinetic-crypto.replit.app/` (HTML/JSON scrape → parse into the
structure above). The same `pp_acc`/`pp_vel` semantics already used by
`strategy/decision_math.py` (`regime_from_acc`, `phase_from_acc_vel`).

**Fail behavior.** Unreachable/parse-fail ⇒ `status: unknown`, `risk_state: unknown`.
For signal-only strategies (`duo_raw`), the deterministic health gate already proceeds
without MXC, so an MXC outage degrades context but not execution.

**Example.**

> **dashboard-risk:** `{regime:"BULL", risk_state:"risk_on", pp_acc:0.42}`
> **Agent:** "Regime BULL, risk-on — consistent with a long." → ✓ risk-on in the matrix.

---

### 5.d `tradingview-chart` **[PLANNED]**

**Purpose.** Validate the actual chart setup on the operator's desktop TradingView via
Chrome DevTools Protocol: screenshot, read indicator values, confirm the structure and
that we're not mid-candle.

**Inputs.** `symbol`, `timeframe`, expected `side`.

**Outputs.**

```json
{
  "status": "ok",
  "screenshot_ref": "logs/charts/SOLUSDT-2h-20260628T1400.png",
  "indicators": {"rsi": 58.1, "jrsx": 61.0, "regime": "BULL"},
  "structure": "clean_uptrend",
  "mid_candle": false,
  "confirms_setup": true
}
```

**Tools.** TradingView CDP **MCP tools** driving the operator's desktop Chrome
(navigate, screenshot, evaluate DOM/indicator panes). Surfaced to the agent as MCP
tool calls, not raw CDP.

**Fail behavior.** Desktop offline / CDP unreachable ⇒ `status: unknown`,
`confirms_setup: null`. Contributes nothing; never blocks.

**Example.**

> **tradingview-chart:** `{structure:"clean_uptrend", mid_candle:false, confirms_setup:true}`
> **Agent:** "Chart confirms: clean uptrend, candle closed, RSI 58 (not overheated)." → ✓ clean.

---

### 5.e Operator comms — Hermes native gateway (not a skill)

Telegram operator interaction is handled via Hermes' native gateway (`hermes gateway`
config with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS`). No separate skill is
required. The gateway is read-first and loads the `hermx-control` skill so the operator
can query positions, PnL, arm status, and the last signal in chat; it inherits the same
hard rule that it cannot execute without an explicit inbound signal or human instruction,
and never bypasses the gate chain. A **sender allowlist** (`TELEGRAM_ALLOWED_USERS`) drops
messages from unknown senders before they reach the agent; if the gateway is down,
operator comms degrade but **execution is unaffected**.

---

### 5.f `signal-memory` **[PLANNED]**

**Purpose.** Maintain and serve a rolling log of the last N signals and their outcomes
so the agent has continuity across signals (avoid doubling up, notice contradictions).

**Inputs.** `n` (default 50), optional `symbol` filter.

**Outputs.**

```json
{
  "status": "ok",
  "signals": [
    {"ts":"2026-06-28T14:00:00Z","strategy_id":"duo_regime_rsi_30m","symbol":"SOLUSDT",
     "side":"buy","advisor":"proceed","score":72,"outcome":"filled"},
    {"ts":"2026-06-28T08:00:00Z","symbol":"XRPUSDT","side":"sell",
     "advisor":"skip","outcome":"vetoed_by_advisor"}
  ]
}
```

**Source.** Reads `logs/executions.jsonl` + `logs/advisor-decisions.jsonl` through a
small read-only endpoint (not raw filesystem access — keeps the agent's surface to HTTP).

**Fail behavior.** Empty/stale ⇒ `status: unknown` with whatever partial history exists;
the agent flags reduced context.

**Example.**

> **Agent (before evaluating a new SOL buy):** signal-memory.recent(symbol=SOLUSDT)
> → "Already long SOL from 14:00, advisor proceeded at 72. New buy would double up —
> flag to operator rather than auto-stack." 

---

## 6. User Communication Layer

### 6.1 Telegram bot design

**Commands (structured):**

| Command | Action | Endpoint(s) used |
|---------|--------|------------------|
| `/status` | armed state + open positions summary | `GET /health`, `GET /api` |
| `/pnl` | daily uPnL + realized | `GET /api` |
| `/positions` | per-symbol detail | `GET /api` |
| `/last` | last processed alert | `GET /latest` |
| `/killswitch` | report kill-switch state (read-only) | `GET /health` |
| `/cancel <id>` | request cancel of a pending order (confirmation flow) | see §6.4 |
| `/help` | command list | — |

**Natural-language queries.** The agent also answers free text ("are we good?", "did
the SOL alert go through?", "how much are we down today?") by mapping intent to the
same reads. Ambiguous money-relevant questions get precise, non-reassuring answers
(stale data ⇒ "I can't confirm right now," never "nothing open").

### 6.2 WhatsApp integration

Same intent set over the WhatsApp Business Cloud API. WhatsApp's template-message rules
mean **proactive** notifications (e.g., "signal vetoed") use pre-approved templates;
**reactive** replies (operator asked first) are free-form within the session window.
Telegram has no such restriction and is the primary channel; WhatsApp is the fallback.

### 6.3 What the agent CAN answer vs. CANNOT do

| The agent CAN (read-only) | The agent CANNOT without confirmation |
|---------------------------|---------------------------------------|
| last signal / alert | place an order |
| open positions, sizes, uPnL | relay a signal the operator didn't ask for |
| daily PnL | cancel an order |
| kill-switch / arm state | anything that changes money state |
| explain a strategy file's constraints | override a gate / pause / kill switch (impossible by design) |

### 6.4 Confirmation flow for agent-initiated actions

Any action that *changes money state* (relay a human-instructed signal; cancel) uses an
explicit confirm step:

```
Operator: "send the SOL 2h buy from the alert"
  Agent:  "Confirm: relay SOLUSDT 2h BUY @168.42 (strategy duo_regime_rsi_30m, OKX)?
           Size is computed by the system, not me. Reply YES to proceed."  [60s TTL]
Operator: "YES"
  Agent:  POST /webhook → reports the gate-chain outcome truthfully
           ("Submitted, FILLED" / "Blocked: kill switch engaged" / "UNKNOWN, reconciling").
```

Rules:
- Confirmation is **single-use** and **time-boxed** (default 60s); a stale `YES` is
  rejected and re-prompted.
- The agent **never** auto-confirms its own proposals.
- The confirmation message restates the *exact* payload so the operator approves what
  will actually be sent.

### 6.5 Rate limiting / abuse prevention

- **Sender allowlist** (`TELEGRAM_ALLOWED_USERS` / `WHATSAPP_ALLOWED_USERS`): non-listed
  senders are dropped at the gateway, before the agent.
- **Per-sender rate limit** on inbound messages (e.g., 30/min) to bound LLM cost and
  prevent prompt-flooding.
- **Action throttle**: money-changing confirmations are limited (e.g., ≤ 5 pending
  confirmations/hour) and de-duplicated.
- The webhook receiver's own IP rate limit (`HERMX_RATE_LIMIT_*`, default 120/60s) and
  body-size cap (`HERMX_MAX_BODY_BYTES`) still apply to anything that reaches `/webhook`.

---

## 7. Intelligence Evolution Roadmap

Each phase is **individually reversible** via env flags. "Rollback" below is the exact
lever that returns to the prior phase's behavior with no code change.

### Phase 1 — Deterministic execution; agent = advisory annotator **[CURRENT]**

- **Built:** Full deterministic pipeline (§3.4). Advisor seam exists, `HERMX_ADVISOR_ENABLED`
  default OFF. `hermx-control` skill ships. Agent can read and relay, cannot decide.
- **Entry gate:** none — this is the baseline.
- **Rollback:** N/A (this is the floor).

### Phase 2 — Hermes validates signals; veto power **[NEAR]**

- **Built:** Turn on the advisor as a binary gate: `HERMX_ADVISOR_ENABLED=true`
  (enabling makes the veto live — there is no separate veto flag). The agent (via
  `hermx-control`, then `kronos-validate`/`dashboard-risk`) returns `proceed | skip`.
  A `skip` blocks execution with `reason: vetoed_by_advisor`. Fails open.
- **Entry gate:** advisor verdicts have been logged and reviewed over a burn-in period
  and the operator agrees with them; `test_phase8_advisor.py` green.
- **Rollback:** set `HERMX_ADVISOR_ENABLED=false` (off) — instant return to Phase 1.

### Phase 3 — Conviction score gates execution **[MID]**

- **Built:** `kronos-validate`, `dashboard-risk`, `tradingview-chart`, `signal-memory`
  shipped. The agent aggregates inputs into a **0–100 conviction score** (decision
  matrix §3.5). A configurable threshold (`HERMX_ADVISOR_MIN_SCORE`) gates execution:
  below threshold ⇒ skip/escalate.
- **Entry gate:** the four intelligence skills each have measured availability and a
  logged track record; the threshold is tuned against historical `advisor-decisions.jsonl`.
- **Rollback:** lower the threshold to 0 (score never blocks) → behaves like Phase 2;
  or disable the advisor entirely → Phase 1.

### Phase 4 — ML layer on collected outcomes; agent makes the discretionary call **[FUTURE]**

- **Built:** A model trained on the accumulated `(signal, intelligence inputs, outcome)`
  dataset (from the ledgers) feeds the agent a calibrated prior. The agent's verdict
  becomes the primary go/no-go *within the gate chain*.
- **Entry gate:** a statistically meaningful outcome dataset; offline backtest showing
  the agent's calls beat naive "always proceed"; operator sign-off.
- **Rollback:** drop the ML prior (agent reverts to Phase 3 rule-based scoring); flags
  cascade down.

### Phase 5 — Agent initiates trade ideas from market scans **[FUTURE]**

- **Built:** The agent runs scheduled market scans (via the same intelligence skills)
  and *proposes* trade ideas not triggered by a TradingView alert. Every proposal still
  enters via `POST /webhook` and **requires operator confirmation** (§6.4) — the agent
  never self-initiates an order.
- **Entry gate:** Phases 2–4 stable; a long record of the agent's *proposals* (logged,
  not executed) that the operator would have approved; explicit opt-in flag
  (`HERMX_PROACTIVE_ENABLED`).
- **Rollback:** `HERMX_PROACTIVE_ENABLED=false` — the agent goes back to purely reactive.

> Across all phases, the §3.4 gate chain and the kill switch are untouched. Autonomy is
> added *in front of* the floor, never by lowering it.

---

## 8. Deployment Architecture

### 8.1 VPS baseline

- **OS:** Ubuntu 22.04 LTS.
- **Layout:** code at `/opt/hermx`, venv at `/opt/hermx/.venv`, `.env` at
  `/opt/hermx/.env` (mode 600, owner `hermx`).
- **Services (systemd, `Restart=always`, `User=hermx`):**
  - `hermx-receiver.service` → `python src/webhook_receiver.py` (`:8891`)
  - `hermx-dashboard.service` → `python src/dashboard.py` (`:8098`)
  - `hermx-agent.service` → `hermes` daemon (Phase 2+) [PLANNED]
  - Both shipped units key off `tailscaled.service` and `network-online.target`, with
    `StartLimitIntervalSec=60` / `StartLimitBurst=5` to prevent restart storms.
- **Ingress:** Tailscale Funnel exposes only `:8891` publicly: `tailscale funnel --bg 8891`
  → `https://hermx.<tailnet>.ts.net/webhook`. No inbound firewall rule; everything else
  stays loopback.

### 8.2 Docker Compose spec [PLANNED — parallel to the systemd path]

A single `docker-compose.yml` mirroring the systemd services for users who prefer
containers:

```yaml
services:
  receiver:
    build: .
    command: python src/webhook_receiver.py
    env_file: .env
    network_mode: host            # keep services on loopback; Funnel handles ingress
    restart: always
  dashboard:
    build: .
    command: python src/dashboard.py
    env_file: .env
    network_mode: host
    restart: always
  agent:                          # Phase 2+
    build: ./agent
    command: hermes --daemon --skills hermx-control
    env_file: .env
    network_mode: host
    restart: always
    depends_on: [receiver, dashboard]
  tailscale:
    image: tailscale/tailscale:latest
    cap_add: [NET_ADMIN]
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_EXTRA_ARGS=--funnel=8891
    restart: always
```

> `network_mode: host` is deliberate: it keeps `:8891`/`:8098` on `127.0.0.1` so only
> Tailscale Funnel reaches the outside, preserving the loopback-only posture (§10).

### 8.3 Environment configuration

`.env` (secrets, mode 600) holds everything sensitive; config files (`engine-config.json`,
`strategies/*.json`) hold non-secret runtime behavior. Real env keys (from `setup/env.example`):

| Group | Keys |
|-------|------|
| Webhook/auth | `HERMX_SECRET`, `HERMX_REQUIRE_HMAC`, `HERMX_WEBHOOK_HMAC_KEY`, `HERMX_REPLAY_WINDOW_SECONDS`, `HERMX_MAX_BODY_BYTES`, `HERMX_RATE_LIMIT_WINDOW_SECONDS`, `HERMX_RATE_LIMIT_MAX_REQUESTS` |
| Receiver/exec | `SHADOW_PORT` (legacy naming; code still reads it for backward compatibility), `HERMX_QUEUE_MAXSIZE`, `HERMX_SUBMIT_TIMEOUT_SECONDS`, `HERMX_WORKER_POOL_SIZE`, `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS`, `HERMX_WATCHDOG_ENABLED`, `HERMX_WATCHDOG_STALE_SECONDS`, `HERMX_QUEUE_LAG_SLO_SECONDS` |
| Kill switch | `HERMX_LIVE_TRADING` *(`false`/unset = live trading disabled; `execution_mode=demo` is unaffected)* |
| Dashboard | `CLEAN_DASHBOARD_PORT`, `HERMX_DASH_AUTH`, `HERMX_SECRET` |
| Exchange (OKX) | `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`, `OKX_DEMO_API_KEY`, `OKX_DEMO_SECRET_KEY`, `OKX_DEMO_PASSPHRASE` |
| Exchange (others) | `BINANCE_TESTNET_API_KEY/SECRET_KEY`, `BYBIT_TESTNET_API_KEY/SECRET_KEY`, `KUCOIN_PAPER_API_KEY/SECRET/PASSPHRASE`, `BITGET_DEMO_API_KEY/SECRET_KEY/PASSPHRASE`, `GATE_TESTNET_API_KEY/SECRET_KEY`, `COINBASE_SANDBOX_API_KEY/SECRET_KEY`, `HYPERLIQUID_WALLET_ADDRESS/PRIVATE_KEY` |
| **Removed** | ~~`HERMX_SUBMIT_ENABLED`~~, ~~`OKX_SUBMIT_ORDERS`~~, ~~`OKX_SIMULATED_TRADING`~~, ~~`HERMX_EXEC_API`~~, ~~`HERMX_EXEC_WRITE_BACKEND`~~, ~~`HERMX_EXEC_SHADOW`~~ — superseded by `execution_mode` + `HERMX_LIVE_TRADING`. (`HERMX_EXEC_BACKEND` is **retained** as an optional CCXT override.) |
| Ingress | Tailscale Funnel via `TS_AUTHKEY` (the sole supported ingress; `cloudflared` is not used) |
| Advisor [BUILT, OFF] | `HERMX_ADVISOR_ENABLED`, `HERMX_ADVISOR_COMMAND`, `HERMX_ADVISOR_SKILLS`, `HERMX_ADVISOR_MODEL`, `HERMX_ADVISOR_TIMEOUT_SECONDS` |
| Intelligence [PLANNED] | `KRONOS_API_URL`, `MXC_DASHBOARD_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `WHATSAPP_*`, `HERMX_ADVISOR_MIN_SCORE`, `HERMX_PROACTIVE_ENABLED` |

### 8.4 Copy-and-deploy (step by step)

This is the deploy flow (see INSTALL.md §8.4, `deploy/install-services.sh`, and
`docker-compose.yml`), condensed:

1. Provision an Ubuntu 22.04 VPS.
2. Copy the package to `/opt/hermx`.
3. Create the venv and `pip install -r requirements.txt`.
4. Copy `setup/env.example` → `/opt/hermx/.env`, fill secrets, `chmod 600`.
5. Set each strategy's `execution_mode` (`demo` vs. `live`); keep
   `HERMX_LIVE_TRADING` off (live trading disabled) until verified.
6. Install Tailscale; `tailscale up` to authenticate to **your own** tailnet.
7. `tailscale funnel --bg 8891` → note your URL `https://hermx.<tailnet>.ts.net/webhook`.
8. `sudo deploy/install-services.sh` (creates `hermx` user, installs + enables both units).
9. `systemctl status hermx-receiver hermx-dashboard` — confirm running.
10. `curl 127.0.0.1:8891/health` and `127.0.0.1:8098/health` — confirm healthy.
11. Send a synthetic **valid** alert → confirm it processes; send an **invalid** one →
    confirm it's rejected.
12. Confirm the dashboard reflects the test.
13. Confirm OKX demo behavior (`execution_mode: demo` in the strategy) before going live.
14. Point your TradingView alert webhook at your Tailscale URL with your `X-Webhook-Secret`.

Because each user runs their **own tailnet**, every instance gets a unique, stable
ingress URL with no shared infrastructure — the system is genuinely fork-and-own.

### 8.5 Backup / restore

| Stateful (back up) | Stateless (reproducible) |
|--------------------|--------------------------|
| `/opt/hermx/.env` | code (`git`) |
| `strategies/*.json` | venv |
| `logs/*.jsonl` (ledgers — the money record) | systemd units (in `deploy/`) |
| `*-state.json`, `*.checkpoint.json` | Tailscale (re-auth) |
| `engine-config.json` | — |

Restore = re-deploy code + venv, restore `.env` + `strategies/` + `logs/` + state JSON,
restart units. The position-journal checkpoint reconciles on load, so restoring the
ledgers reconstructs authoritative position state.

---

## 9. Reliability Design

| Concern | Mechanism |
|---------|-----------|
| **Process supervision** | systemd `Restart=always`, `RestartSec=5`, restart-storm guard (`StartLimitIntervalSec=60`/`Burst=5`) on receiver, dashboard, and (P2+) agent. |
| **Ingress persistence** | `tailscale funnel --bg 8891` runs detached and survives reboots (tailscaled is itself a service; receiver unit `After=tailscaled.service`). |
| **Signal-miss detection** | UptimeRobot (or equivalent) polling `GET /health` over the Funnel URL; alert on non-200 or stale receiver heartbeat. |
| **Agent crash recovery** | Agent is stateless; on restart it reloads skills and reconstructs context from reads (§4.6). Nothing is lost because the agent owns no money state. |
| **Exchange connectivity** | CCXT calls bounded by `HERMX_SUBMIT_TIMEOUT_SECONDS` (45). A timeout/exception ⇒ order state `UNKNOWN` (not assumed filled, not assumed rejected). The unknown-resolver loop (`resolve_unknown_orders_once`, every `HERMX_UNKNOWN_RESOLVER_INTERVAL_SECONDS`) reconciles `UNKNOWN` → `FILLED`/`REJECTED` against the exchange. |
| **Crash-torn ledgers** | Appends are fsync'd; `read_jsonl_tolerant()` quarantines a crash-truncated trailing line and fails loud on mid-file corruption — no silent data loss. |
| **Kill switch** | `HERMX_LIVE_TRADING=false` blocks **all** `execution_mode=live` submission before any gate, in under a second, with no restart needed (read per-execution). `false`/unset is the live-safe default; demo trading is unaffected. |

The reliability philosophy mirrors the safety one: **unknown is a first-class state.**
The system never *guesses* a fill; it records `UNKNOWN` and reconciles.

---

## 10. Security Model

| Surface | Control |
|---------|---------|
| **Network** | All services bind `127.0.0.1` only. The *only* public surface is `:8891/webhook` via Tailscale Funnel. No other inbound ports. |
| **Webhook auth** | `X-Webhook-Secret` (shared secret) mandatory; optional HMAC (`HERMX_REQUIRE_HMAC`) adds `X-Webhook-Timestamp` + `X-Webhook-Signature` (SHA256) with a replay window (`HERMX_REPLAY_WINDOW_SECONDS`, 300s). Body capped at `HERMX_MAX_BODY_BYTES`. Per-IP rate limit. |
| **Dashboard auth** | Optional token (`HERMX_DASH_AUTH` + `HERMX_SECRET`) via `X-Dashboard-Token`; typically off on a same-host loopback deploy since it isn't publicly reachable. |
| **Messaging auth** | Telegram bot token / WhatsApp business credentials, plus a **sender allowlist** so only the operator's accounts are accepted (§6.5). |
| **Agent confinement** | The agent reaches the system **only** via loopback HTTP. It cannot read `.env`, hold exchange credentials, or call CCXT/shell/filesystem for order purposes. Its sole write is `POST /webhook`. |
| **Safety location** | Money-safety gates are **Python code** in `ExecutionService.execute()`, *not* skill/agent text. The skill prose is guidance only and is explicitly stated to be non-authoritative. An adversarial or buggy LLM cannot widen its authority because the authority isn't in the prose. |
| **Secrets at rest** | `.env` is mode 600, owner-only; never committed; never exposed to the agent. |

Threat-model summary: even a fully-compromised or hallucinating agent can, at worst,
relay a *valid-schema* signal that the deterministic gate chain still independently
evaluates — and it cannot relay anything while the kill switch is engaged.

---

## 11. Data & State Model

### 11.1 Where data lives

| Data | File(s) (`logs/` unless noted) | Format | Role |
|------|-------------------------------|--------|------|
| Raw inbound webhooks | `shadow-webhooks.jsonl` | JSONL | audit |
| Normalized intake | `shadow-intake.jsonl` | JSONL | audit |
| Decisions per strategy | `shadow-decisions.jsonl` | JSONL | audit |
| Duplicates / dedupe index | `shadow-duplicates.jsonl`, `seen-signals.jsonl`/`.json` | JSONL/JSON | idempotency |
| Execution plan | `execution-plan.jsonl` | JSONL | readiness record |
| Execution outcomes | `executions.jsonl` (legacy `okx-executions.jsonl` mirror removed; read-only dashboard fallback) | JSONL | **money record** |
| Order state machine | `order-journal.jsonl` | JSONL | write-ahead (PLANNED→SUBMITTED→terminal) |
| Position state | `position-journal.jsonl` + `position-journal.checkpoint.json` | JSONL/JSON | authoritative in `journal` backend |
| Reconciliation mismatches | `reconcile-alerts.jsonl` | JSONL | safety alerts |
| Operator/state alerts | `operator-alerts.jsonl`, `state-alerts.jsonl` | JSONL | ops |
| Advisor verdicts | `advisor-decisions.jsonl` | JSONL | agent decisions + ML dataset seed |
| MXC availability | `tab-health.jsonl` | JSONL | health-gate context |
| Last alert | `latest.json` | JSON | `/latest` |
| Runtime config | `engine-config.json` | JSON | non-secret config |

### 11.2 Durability & integrity

- All ledger appends are **fsync'd**; Decimal fields canonicalized before write.
- `read_jsonl_tolerant()` tolerates a crash-truncated tail (quarantined to
  `<file>.corrupt`) and fails loud on mid-file corruption.
- Journals carry `schema_version`; the position-journal checkpoint stores a verified
  snapshot + high-water mark and reconciles on load.

### 11.3 Agent memory scope

The agent retains nothing authoritative. Its context is reconstructed from reads
(§4.6) and the `signal-memory` view over `executions.jsonl` + `advisor-decisions.jsonl`
(last N). This guarantees the agent is correct immediately after a restart.

### 11.4 Signal history for ML (future)

`advisor-decisions.jsonl` + `executions.jsonl` together form the training corpus for
Phase 4: each record links `(signal, intelligence inputs, advisor verdict, outcome)`.
No separate pipeline is needed to *collect* the data — running Phases 2–3 produces it as
a byproduct. When the corpus is large enough, the ML layer trains offline on these files.

---

## 12. Implementation Plan

Ordered by **what unlocks the most value next**, with the build/plan boundary explicit.

### Already built [BUILT]

- Deterministic pipeline: receiver, schema, dedupe, queue, worker, readiness builder.
- `ExecutionService` gate chain (kill switch → precedence → pause → idempotency →
  write-ahead journal → CCXT submit → reconcile).
- CCXT executor (OKX/KuCoin/Bybit/Hyperliquid; OKX perpetual swap specifics).
- Unknown-order resolver loop; reconciliation with backoff.
- Dashboard (`/api`, `/health`, `/`).
- `hermx-control` skill; deploy flow in INSTALL.md §8.4 (`deploy/install-services.sh`,
  `docker-compose.yml`); systemd units in `deploy/`.
- Pre-execution **advisor seam** (`run_execution_advisor`, `execute_okx_with_advisor`),
  default OFF, fail-open, with `tests/test_phase8_advisor.py` green.

### Build order

| Step | Deliverable | Depends on | Unlocks | Status |
|------|-------------|------------|---------|--------|
| **1** | `signal-memory` read endpoint + skill | executions/advisor ledgers (exist) | agent continuity; ML corpus surfaced | [PLANNED] |
| **2** | `hermes gateway` (Telegram first; native, not a skill) | `hermx-control` (exists) | conversational ops — the headline UX win | [PLANNED] |
| **3** | Confirmation flow (§6.4) | step 2 | safe human-instructed relay over chat | [PLANNED] |
| **4** | Advisor burn-in: log & review verdicts | advisor seam (exists) | verdicts validated before relying on the veto | config-only |
| **5** | `kronos-validate` skill + `KRONOS_API_URL` | Kronos API stood up | direction/conviction confirmation | [PLANNED] |
| **6** | `dashboard-risk` skill (MXC parse) | MXC reachable | regime/risk context in verdicts | [PLANNED] |
| **7** | Enable advisor (`HERMX_ADVISOR_ENABLED=true`) → **Phase 2** | steps 4–6 | agent can block weak signals | config-only |
| **8** | `tradingview-chart` skill (CDP MCP) | desktop CDP available | chart confirmation | [PLANNED] |
| **9** | Conviction scoring + threshold (`HERMX_ADVISOR_MIN_SCORE`) → **Phase 3** | steps 5–8 | graded gating | [PLANNED] |
| **10** | WhatsApp channel | step 2 | fallback comms channel | [PLANNED] |
| **11** | Docker Compose path | services (exist) | container deploy option | [PLANNED] |
| **12** | ML layer on collected outcomes → **Phase 4** | corpus from steps 4–9 | discretionary calls | [FUTURE] |
| **13** | Proactive market scans → **Phase 5** | Phases 2–4 stable | agent-initiated ideas | [FUTURE] |

### Rationale for the ordering

- **Comms before intelligence.** Steps 1–4 deliver the conversational-operations win
  (status/PnL/last-alert/cancel over Telegram, plus advisor burn-in) using components
  that already exist — the highest value for the least new code and zero new risk.
- **Veto only after burn-in.** Step 7 (the first time the agent can *block* an order) is
  gated on steps 4–6 producing a track record the operator trusts. The rollback is a
  single env flag.
- **Scoring and ML last.** Steps 9–13 require accumulated data and standing infra
  (Kronos, CDP); they ride on the corpus the earlier phases generate for free.

> Throughout, the deterministic gate chain (§3.4) and kill switch are never modified.
> Every new capability is added in front of the floor and is reversible with an env flag.

---

*End of specification.*
