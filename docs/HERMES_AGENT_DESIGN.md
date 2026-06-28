# HermX × Hermes — Advanced Agent Design

> **STATUS (2026-06-27).** HermX is a Hermes-Agent-enabled trading system. The
> only HermX-side artifact built for the agent so far is
> `skills/hermx-control/SKILL.md` — a skill the **external** Hermes Agent runtime
> (Nous) loads. There is **no** HermX-side agent process, no LLM, no scheduler,
> and (deliberately) **no MCP server** in this repo. The deterministic execution
> stack the agent is constrained by — `ExecutionService.execute`, the CCXT
> adapters, the write-ahead journals, the gate chain — is **built and tested
> today**. Everything else in this doc is **operating philosophy**, not code to be
> written.

This is the design for a **local, single-user** system. It was deliberately
simplified: earlier drafts proposed an MCP server, a propose→token→confirm
handshake, and a two-mode `orchestration_mode` gate. All three were judged
over-engineered for a loopback single-operator deployment and are demoted to
§7 *Deferred*. The transport is dead simple instead (see §3).

---

## 1. What it is, in one breath

Signals reach HermX from **TradingView webhooks** *or* from **a human chatting
with the agent** — those are the only two sources, and the agent can act on
**nothing else** (§2). Strategy files in `strategies/*.json` are defined **in
advance** and hold every constraint (instrument, `capital.budget_usd`, `leverage`,
`margin_mode`, timeframe; the asset is derived from `instrument.inst_id`). The agent **never** sets size: existing code computes
notional from the strategy file. All exchange steps run through the deterministic
Python stack via CCXT. The agent is advisory/relay only; the gate chain is
authoritative and can veto anything.

## 2. The hard boundary (non-negotiable)

- **Advisory / relay only.** The agent reads state and relays sanctioned signals;
  it does **not** execute. The deterministic layer validates and can **VETO** any
  request — gate precedence, kill switch, symbol pause, idempotency are final.
- **Never self-initiates.** Absent (a) an inbound webhook signal or (b) an
  explicit human instruction, **nothing** the agent "decides" can reach
  submission. A timer firing, a cron finding something, or a model hunch are all
  insufficient. This is restated in `SKILL.md` under *CANNOT*.
- **Constrained by what the API exposes.** The agent literally cannot do anything
  the local HTTP API doesn't offer. There is no raw CCXT, filesystem, shell, or
  credential access for order purposes. **That is the safety model.**
- **Sizing is never the LLM's.** `build_strategy_execution_readiness`
  (`src/webhook_receiver.py`) computes
  `base_notional = capital.budget_usd * leverage` and emits it as `target_notional_usd` /
  `planned_notional_usd`. The `/webhook` body has **no** size/notional/leverage
  field; any number the model produces is ignored.
- **Money-safety lives in Python, not in `SKILL.md`.** The skill text is treated
  as **untrusted guidance**. The real gates are in `ExecutionService.execute`
  (`src/execution/service.py`), where the LLM cannot edit them. Safety never
  moves up into the model.

## 3. Transport — the local API is the whole surface

The external Hermes Agent loads `skills/hermx-control/SKILL.md` and calls the
**existing local HTTP API over loopback** (`127.0.0.1`, no auth key needed
on-host). No MCP server, no new endpoints, no token handshake — just the two
servers that already run.

**Read path — dashboard (`src/dashboard.py`, `127.0.0.1:8098`):**
`GET /api` → positions / PnL / balances / ledger / executor health;
`GET /health` → live-trading / arm state (`live_trading_enabled`, per-strategy
`execution_mode` + `submit_orders`, `armed_summary`).

**Act path — receiver (`src/webhook_receiver.py`, `127.0.0.1:8891`):**
`POST /webhook` with a TradingView alert JSON (schema
`schemas/tradingview-alert.schema.json`); `GET /health` and `GET /latest` for
liveness and the last processed alert. The receiver normalizes the alert,
`build_strategy_execution_readiness` computes the plan, and
`_execute_okx_via_service` (`src/webhook_receiver.py`) hands it to
`ExecutionService.execute`.

## 4. The deterministic gate chain (authoritative, built + tested)

`ExecutionService.execute` (`src/execution/service.py`) is the **single
chokepoint**. In order:

1. **Kill switch (live only)** — `HERMX_LIVE_TRADING`; an `execution_mode=live`
   order requires `=true`, and `false`/unset ⇒ `not_submitted` before anything
   else. `execution_mode=demo` is unaffected.
2. **Submit + mode** — `should_execute` requires `strategy.submit_orders` true
   **and** a resolved `execution_mode` (`demo` → OKX sandbox/paper, always
   allowed; `live` → real account, subject to step 1) **and** `auth_healthy`
   **and** `watchdog_ok`.
3. **Symbol pause** — `symbol_pause_info(symbol)` ⇒ `not_submitted` when paused.
4. **Idempotency** — a duplicate `cl_ord_id` (`latest_order_record`) ⇒
   `not_submitted` (`duplicate_cl_ord_id`).
5. **Write-ahead order journal** — `record_order_state` transitions
   `PLANNED → SUBMITTED → FILLED | REJECTED | UNKNOWN`, fail-closed on a journal
   write error (`fail_closed_state_write`); UNKNOWN is a first-class state.
6. **Submit** via the CCXT adapter, then **post-submit reconciliation**
   (`reconcile_order_with_backoff`, mismatch ⇒ `emit_reconcile_alert`).
   Secrets are scrubbed with `redact_secrets`.

New risk checks (drawdown, exposure, venue caps) are added **here** as more
guards — the floor only ever rises, and never grants the agent new freedom.

## 5. Crons are read-only / advisory

Scheduled jobs may fetch data, watch positions, and feed the dashboard. They
produce **reports only** and have **no path to submission** — a cron **never**
auto-executes. (Headless runs have no human present, so the gate chain would be
the only safety anyway; the cleaner rule is simply that cron cannot reach the
act path.)

## 6. Failure posture (fail-safe)

- **Read failure / stale data ⇒ UNKNOWN, never "flat".** A `/api` error or a
  degraded executor must be reported as "can't confirm", never as "no positions"
  — that would be a money-relevant lie (`SKILL.md` enforces this for the agent).
- **Agent down / unreachable ⇒ deterministic path unaffected.** The receiver's
  webhook→execute path runs without the agent; the agent is an enhancement, not a
  dependency of execution.
- **Agent can never block or force money-safety.** It cannot force a submit,
  clear a pause, silence an alert, or disable the kill switch. Its only powers are
  *read*, *relay a sanctioned signal*, *answer*, *defer*.

## 7. Deferred / explicitly out of scope

Revisit only if a real need appears.

- **MCP server / proxy** — unnecessary; the agent calls the existing loopback
  HTTP API directly.
- **propose→token→confirm handshake** — over-engineered for a single local
  operator; the human *is* the confirmation by issuing the instruction.
- **`orchestration_mode` two-mode config gate** — no second mode is needed; there
  is one path (advisory/relay) and the gates decide the rest.
- **Manual close / flatten** — unsupported today: the readiness builder emits only
  open intents (`execution_intent.actions = ["CLOSE_OPPOSITE_IF_ANY",
  "OPEN_<dir>"]`); there is no close-only intent for a human "close SOLUSDT".
- **Memory / learning loop** — no persistent agent memory; recommendations are
  stateless. Add only if outcomes-keyed priors prove worth the complexity.

## 8. Pre-execution advisor — Design 1 (BUILT + tested, default OFF)

The receiver may consult an LLM **risk overseer** between
`build_strategy_execution_readiness` and submission. This is the deterministic
front door asking the brain for a second opinion — **the LLM is never the front
door**, so it can never be "down" in a way that stops trades.

- **Authority:** binary only — `proceed` or `skip` (+ free-text `risk_note`, optional
  0–100 `score`). It **cannot** change symbol, side, size, leverage, or strategy;
  those are already fixed by the alert's `strategy_id` and the strategy file.
- **Single switch.** `HERMX_ADVISOR_ENABLED` (default OFF). When ON, a `skip`
  response **blocks** the trade (`okx_execution.reason = "vetoed_by_advisor"`).
  No annotate-only middle mode — if you turn it on, the veto is live.
- **Fails OPEN.** Timeout / transport error / malformed reply ⇒ proceed
  deterministically (logged). A down or slow LLM never blocks a sanctioned trade.
- **Seams:** `run_execution_advisor` / `execute_okx_with_advisor`
  (`src/webhook_receiver.py`), defaults built-in with an optional
  `shadow-config.json` `advisor` block (env-overridable), decisions logged to
  `logs/advisor-decisions.jsonl`.
  Tests: `tests/test_phase8_advisor.py`.
- **Transport:** the **Hermes Agent** run as a one-shot **with our skills loaded**
  (`hermes -z "<prompt>" --skills hermx-control`) — the full agent loop through
  Hermes (its configured provider + credentials), so it can use `hermx-control`
  (and skills we add later) to read the live `/api` before deciding. Not a bare LLM
  call. The skill set will expand over time.

This narrows the earlier "deferred agent mode": the agent gains a veto **within
the fixed envelope** (it can only decline a trade, never originate or resize one),
gated behind a single explicit switch and a fail-open default.

## 9. Compact layer sketch

```text
TradingView webhook ─┐                         ┌─ Human chat (Hermes Agent / Nous,
                     │                         │   loads skills/hermx-control/SKILL.md,
                     │                         │   Telegram/WhatsApp gateway optional)
                     ▼                         ▼
            ┌───────────────────── LOCAL HTTP API (127.0.0.1) ─────────────────────┐
            │  read:  dashboard.py  GET /api · /health        (:8098)               │
            │  act:   webhook_receiver.py  POST /webhook      (:8891)               │
            └───────────────────────────────┬──────────────────────────────────────┘
                                             ▼
        build_strategy_execution_readiness  (notional = capital.budget_usd * leverage)
                                             ▼
        [optional] pre-exec advisor  — proceed | skip(+veto)  · FAILS OPEN · default OFF
                                             ▼
        ExecutionService.execute  — gate chain (AUTHORITATIVE, built + tested)
        kill switch → gate precedence → symbol pause → idempotency →
        write-ahead order journal → CCXT submit → reconciliation → redaction
                                             ▼
                                      CCXT adapter(s)

Crons → read/watch → dashboard only (NEVER reach POST /webhook).
```

The agent sits **above** the API line; everything **below** it is deterministic
Python the agent cannot edit or bypass.
