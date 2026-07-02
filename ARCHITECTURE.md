# HermX — Architecture

HermX is a money-safety-critical crypto execution system. A TradingView Pine strategy fires an alert that travels over a private Tailscale Funnel HTTPS URL to a loopback-only webhook receiver, which authenticates, rate-limits, normalizes, and schema-validates it, matches it to a sanctioned strategy file by `strategy_id`, and hands a venue-neutral execution intent to a single deterministic execution chokepoint (`ExecutionService`). That chokepoint enforces every money-safety invariant — kill switch, gate precedence, idempotency, write-ahead journaling, UNKNOWN-on-timeout, post-submit reconciliation — *above* the CCXT adapter that talks to the exchange. An optional Hermes Agent reasoning layer can observe and advise, but **the LLM never touches money-safety**: it can only return `proceed`/`skip` and can never set a symbol, side, size, leverage, or bypass a gate.

---

## 1. System Overview

### 1.1 Design philosophy

- **Safety lives in code, not config or prose.** Every gate is a Python predicate in `ExecutionService.execute()` (`src/execution/service.py`). Config and strategy files can *disarm* the system but can never *widen* what the code allows; skill markdown and LLM output are advisory only.
- **Fail-closed gates, fail-open intelligence.** Any ambiguity on the money path refuses to submit (missing secret → 401, missing executor → `not_submitted`, submit timeout → `UNKNOWN`, partial credentials → disarmed). Any failure in the optional advisor/agent layer falls *open* to deterministic execution so a slow or broken LLM can never block a sanctioned trade.
- **Single execution chokepoint.** There is exactly one path to an order: `ExecutionService.execute()` → `CcxtExecutor.execute()`. The agent surface, the receiver, and any future caller all funnel through it; nothing reaches an exchange SDK directly.
- **Progressive autonomy.** Authority grows in discrete, reversible phases — deterministic execution today; an optional read-only advisor (default OFF); a planned agent that selects strategies but still submits *through the same gate chain*. Each step is gated behind its own env flag and adds no new money-safety surface.

### 1.2 Component map

```text
   ┌──────────────────────────┐       ┌──────────────────────────┐
   │  TradingView (external)   │       │  Operator (external)      │  [BUILT/EXTERNAL]
   │  Pine strategy alert      │       │  Telegram / WhatsApp      │
   └─────────────┬─────────────┘       └─────────────┬────────────┘
                 │ HTTPS POST (alert JSON)             │ message / command
                 ▼                                     ▼
   ┌──────────────────────────┐       ┌──────────────────────────┐
   │  Tailscale Funnel         │       │  Hermes Agent gateway    │  [PLANNED]
   │  https://hermx.<tailnet>  │       │  hermx-control skill     │
   │       .ts.net/webhook     │       │  reads /api /health      │
   └─────────────┬─────────────┘       │  relays POST /webhook    │
                 │ forwards to loopback └─────────────┬────────────┘
                 │                                    │ POST /webhook (loopback)
                 └──────────────────┬─────────────────┘
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Webhook Receiver        127.0.0.1:8891   (src/webhook_receiver.py)   │ [BUILT]
   │  auth → rate-limit → body cap → normalize → schema → dedupe → queue   │
   │  → worker → strategy match → readiness → execute                      │
   └───────────────┬─────────────────────────────────────┬───────────────┘
                   │ (optional, default OFF)              │
                   ▼                                      ▼
   ┌────────────────────────────┐      ┌──────────────────────────────────┐
   │ Pre-exec Advisor           │      │ ExecutionService gate chain       │ [BUILT]
   │ hermes -z subprocess       │      │ (src/execution/service.py)        │
   │ proceed|skip, fail-open    │      │ 7 money-safety invariants         │
   │ [BUILT, default OFF]       │      └──────────────┬───────────────────┘
   └────────────────────────────┘                     │
                                                       ▼
                                        ┌──────────────────────────────────┐
                                        │ CcxtExecutor                      │ [BUILT]
                                        │ (src/executors/ccxt_adapter.py)   │
                                        │ via ExecutorFactory               │
                                        └──────────────┬───────────────────┘
                                                       │ create_order / fetch_*
                                                       ▼
                                        ┌──────────────────────────────────┐
                                        │ Exchange (external)               │ [BUILT/EXTERNAL]
                                        │ OKX demo (live-verified) / ...    │
                                        └──────────────────────────────────┘

   Append-only ledgers  logs/*.jsonl  + latest.json (repo root)            [BUILT]
        ▲ writers: receiver, ExecutionService, advisor, reconciler
        │ readers: ↓
   ┌──────────────────────────────┐   ┌──────────────────────────────────┐
   │ Dashboard  127.0.0.1:8098    │   │ Hermes Agent / hermx-control skill│
   │ (src/dashboard.py)           │   │ loopback HTTP only                │
   │ reads ledgers + exchange     │   │ reads /api,/health,/latest        │
   │ readback → HTML + /api       │   │ relays POST /webhook              │
   │ [BUILT]                      │   │ [advisor BUILT; autonomy PLANNED] │
   └──────────────────────────────┘   └──────────────────────────────────┘
   ┌──────────────────────────────────────────────────────────────────────┐
   │ Telegram / WhatsApp operator gateway (hermes gateway)   [PLANNED]      │
   └──────────────────────────────────────────────────────────────────────┘
```

Every server binds `127.0.0.1` only. The sole public surface is the Tailscale Funnel URL forwarding to `:8891/webhook`.

---

## 2. Signal Flow

### 2.1 End-to-end: TradingView alert → exchange order

1. **Pine strategy fires.** A TradingView alert sends an HTTPS POST with the alert JSON body to the install's Tailscale Funnel URL.
2. **Tailscale Funnel** terminates public HTTPS and forwards the request to `127.0.0.1:8891/webhook`.
3. **Body-size guard** — `Handler.do_POST` rejects `Content-Length > HERMX_MAX_BODY_BYTES` (default 262144) with `413`, before reading the body.
4. **Rate limit** — `rate_limit_allow()` (`src/security/webhook_auth.py:rate_limit_allow`) applies a sliding window per source key (`X-Webhook-Key-Id` else client IP); over-limit → `429`.
5. **Authentication** — `authenticate_webhook_request()` requires a constant-time match on `X-Webhook-Secret` (`HERMX_SECRET`) and, when `HERMX_REQUIRE_HMAC` is set, an `X-Webhook-Signature` HMAC-SHA256 over `timestamp‖body` within `HERMX_REPLAY_WINDOW_SECONDS`. Any failure → `401` and an `AUTH_FAILURE` operator alert.
6. **Parse + raw-intake ledger.** The JSON body is parsed (`400 invalid_json` on failure) and appended verbatim to `logs/shadow-intake.jsonl` with its `received_at`.
7. **Enqueue** — `_queue_work_item()` reserves a per-symbol ordering ticket and pushes onto the bounded `PROCESS_QUEUE` (`HERMX_QUEUE_MAXSIZE`, default 200). A full queue → `503 queue_full` plus a `QUEUE_SATURATION` alert. The receiver answers `200 queued` immediately; all heavy work is async.
8. **Worker dequeue** — `worker_loop()` pulls an item, honors the per-symbol ticket turn (in-order per symbol), takes the symbol lock, and calls `process_payload_async()` → `build_record()`.
9. **Normalize** — `normalize()` uppercases the symbol (stripping `OKX:`/`/`/`-`), canonicalizes the timeframe via `canonical_timeframe()` (shared `hermx_shared`), lowercases side/exchange/source, and synthesizes a deterministic `signal_id` when absent.
10. **Alert schema validate** — `validate_alert_schema()` checks the normalized alert against `schemas/tradingview-alert.schema.json` (Draft 2020-12). Observe-only by default; quarantines only when `strategy_engine.enforce_alert_schema` is true. Fails open if `jsonschema`/schema is unavailable.
11. **Dedupe** — `check_and_mark_signal()` consults a `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS` (default 86400) seen-signals window; duplicates are ledgered and short-circuited.
12. **Strategy validation** — `validate_strategy_alert()` resolves `STRATEGIES.get(strategy_id)` and applies post-selection guards: asset match, canonical-timeframe match, and status ∈ {`trial_candidate`, `active_demo`}. A failure routes to the strategy-alert quarantine ledger (`202`).
13. **(Optional) Pre-execution advisor** — `execute_okx_with_advisor()` consults `run_execution_advisor()` (a `hermes -z` subprocess), default OFF, fail-open; `HERMX_ADVISOR_ENABLED` is a single live-veto switch, so when it is on a `skip` verdict vetoes the trade (no separate veto flag, no annotate-only mode).
14. **Build readiness** — `build_strategy_execution_readiness()` computes notional (`budget_usd × leverage`), the instrument block, a stable `client_order_id`, the close-verify-open action list, and `live_execution_enabled`.
15. **Execution chokepoint** — `execute_okx_if_enabled()` → `_execute_okx_via_service()` constructs an `ExecutionService` (wired with receiver hooks) and calls `.execute()`, which runs the 7 money-safety invariants (§3).
16. **Adapter submit** — on a green gate chain, `CcxtExecutor.execute()` resolves the venue, sizes contracts, and submits `create_order` calls; results are normalized.
17. **Reconciliation** — when `HERMX_RECONCILE_ENABLED` is set, a bounded backoff query loop verifies the real fill state and drives the authoritative `SUBMITTED → terminal` transition in `logs/order-journal.jsonl`, emitting `RECONCILE_MISMATCH` on divergence.
18. **Persist + serve** — the full record is appended to the decision/strategy/execution ledgers and written to `latest.json`; the **Dashboard** (`:8098`) reads the ledgers plus live exchange readback to render HTML and `/api`.

### 2.2 Strategy selection

`strategy_id` is the join key end to end. The TradingView alert *carries* it; the receiver loads every `strategies/*.json` at import into the `STRATEGIES` dict keyed by `strategy_id` (`load_strategy_files()`). `validate_strategy_alert()` looks the id up and then applies three post-selection guards — **asset**, **timeframe**, **status** — each of which can reject the alert. The result is deterministic: **one alert maps to zero or one strategy**, never more. An alert with no `strategy_id` is rejected when `strategy_engine.require_strategy_id` is true (the demo profile sets it true).

### 2.3 Pre-execution advisor (optional)

The advisor is a *safety overseer*, never a trader. It runs as a `hermes -z "<prompt>" --skills hermx-control` subprocess (`_advisor_agent_query`) — the full Hermes Agent loop with the read-only HermX skill loaded, not a bare LLM. It is **default OFF** (`HERMX_ADVISOR_ENABLED`), sees only a minimal read-only snapshot (symbol/side/timeframe/strategy/planned notional — already fixed by code), and may return **only** `proceed` or `skip` plus a free-text `risk_note` and optional 0–100 score. It **cannot** change symbol, side, size, leverage, or strategy. `HERMX_ADVISOR_ENABLED` is a single live-veto switch: whenever the advisor is enabled a `skip` verdict **is** a live veto that blocks the trade — there is no annotate-only mode and no separate veto flag. Any timeout/transport/parse error **fails open to PROCEED** (`HERMX_ADVISOR_TIMEOUT_SECONDS`, default 30). Every decision is logged to `logs/advisor-decisions.jsonl`.

---

## 3. Money-Safety Gate Chain

This is the most important part of the system. All of it lives in `ExecutionService.execute()` (`src/execution/service.py`), above the adapter boundary. Each gate that refuses to submit appends a `not_submitted` record to `logs/executions.jsonl` — a block is always ledgered, never silent.

### 3.1 The 7 invariants

| # | Invariant | Enforcement (in `ExecutionService.execute`) | On failure |
|---|---|---|---|
| 1 | **Live kill switch** | for an `execution_mode: "live"` strategy, `live_trading_enabled()` reads `HERMX_LIVE_TRADING`; unless truthy (`true`/`1`/`yes`) the live order is hard-blocked. Demo strategies never consult it | `not_submitted` (`live_trading_disabled`) |
| 2 | **Strategy active gate** | `readiness.live_execution_enabled` (always True for valid strategies) ∧ `auth_healthy` ∧ `watchdog_ok` must all be true | `not_submitted` (block reason names the failing gate) |
| 3 | **Symbol pause** | `symbol_pause_info(symbol)` consults the per-symbol pause registry | `not_submitted` (`symbol_paused`) |
| 4 | **Idempotency** | `latest_order_record(cl_ord_id)` — a duplicate stable `cl_ord_id` is refused | `not_submitted` (`duplicate_cl_ord_id`) |
| 5 | **Write-ahead journal** | `record_order_state(PLANNED)` then `record_order_state(SUBMITTED)` are fsync-durable *before* the adapter is called; an `OSError` here calls `fail_closed_state_write` and re-raises | submit never happens without a prior durable record |
| 6 | **Submit + outcome** | adapter result maps to `FILLED` / `REJECTED` / `UNKNOWN`; `submit_timeout`/`submit_exception` → `UNKNOWN`, any uncaught exception → `UNKNOWN` | never a silent reject; uncertainty is `UNKNOWN`, not failure |
| 7 | **Post-submit reconcile** | when `HERMX_RECONCILE_ENABLED`, `reconcile_order_with_backoff` queries the venue and drives the authoritative terminal state; divergence emits `RECONCILE_MISMATCH` | operator alert on stdout-vs-exchange mismatch |

The order journal enforces a strict state machine (`_ORDER_STATE_TRANSITIONS`): `None→PLANNED→{SUBMITTED,REJECTED}`, `SUBMITTED→{FILLED,REJECTED,UNKNOWN}`, `UNKNOWN→{FILLED,REJECTED,UNKNOWN}`; `FILLED` and `REJECTED` are terminal. `UNKNOWN` is a first-class state that *triggers* reconciliation — it is never treated as success or as a blind retry.

### 3.2 Two-control model (operator-facing)

Whether an order is placed — and where — is decided by exactly two controls. The dead config-flag arming chain (`execution.enabled`, `execution.submit_orders`, `risk.allow_live_execution`, `strategy_engine.submit_orders`) is gone.

| Control | Location | Key | Fresh-install posture |
|---|---|---|---|
| Per-strategy routing | `strategies/<id>.json` | `execution_mode` (`demo`\|`live`) | `demo` — routes to the exchange sandbox, no global switch needed |
| Global live switch | `.env` (environment) | `HERMX_LIVE_TRADING` | unset/`false` = live disabled (fail-closed); must be truthy for any `execution_mode:"live"` order |

> A `demo` strategy always routes to the sandbox and never consults `HERMX_LIVE_TRADING`. A `live` strategy submits to the real account ONLY when `HERMX_LIVE_TRADING` is truthy; otherwise `ExecutionService.execute()` returns `not_submitted` (`live_trading_disabled`). The legacy `OKX_SUBMIT_ORDERS` / `OKX_SIMULATED_TRADING` env vars are **removed** and not consumed by runtime code.

### 3.3 Guards vs Interceptors

- **Guards veto.** They can turn a submit into `not_submitted`/`UNKNOWN`: kill switch, gate-precedence set, symbol pause, idempotency lookup (and future drawdown/exposure caps). Adding a guard to the chain *raises the floor* — it only adds a new way to refuse.
- **Interceptors observe/wrap, never alter the verdict.** The write-ahead journal writer, the post-submit reconciler, and the secret redactor record and verify but cannot make a blocked order submit. The chain is **append-only**: new safety code can make the system more conservative, never less.

---

## 4. Execution Layer

### 4.1 CcxtExecutor

`src/executors/ccxt_adapter.py`, registered as the sole backend under key `ccxt`.

- **Selection.** `ExecutorFactory.create(config, root)` reads `config.execution.exchange`, runs it through `resolve_key` (aliases `okx`/`okx_demo`/`okx_sandbox`/… → `ccxt`), and instantiates the registered class. `ExecutorFactory.available()` returns `['ccxt']` when the optional `ccxt` import succeeded, `[]` otherwise.
- **Venue routing.** `resolve_execution_config()` (`src/execution/service.py`) sets `execution.ccxt_exchange` from the strategy instrument (`readiness['instrument']['exchange']`, a v2 selection); it falls back to the config's existing `ccxt_exchange` (default `okx`). The venue never changes the *adapter* selector, only which CCXT exchange it targets — submit and reconcile resolve identically so they can't diverge.
- **Timeout → UNKNOWN.** `_submit_timeout_ms()` derives the ccxt client `timeout` from `HERMX_SUBMIT_TIMEOUT_SECONDS` (default 45) so a hung `create_order` fails fast. `_is_timeout_error()` maps `ccxt.RequestTimeout`/`NetworkError` (or a `"timeout"` message) to mode `submit_timeout`; any other exception → `submit_exception`. Both become `UNKNOWN` upstream — **never** a silent reject.
- **Order semantics.** `execute()` resolves the symbol, snapshots the current position, and expands actions to close-verify-open (`CLOSE_OPPOSITE_IF_ANY` → `OPEN_<dir>`). It enforces no-pyramid (skip if already in target direction) and refuses to open while an opposite position is still open. Sizes are floored to market step/precision with `Decimal` math (`_decimal_floor`, `_contracts_for_notional`).
- **Sandbox.** When `execution.simulated_trading` is true (demo default) and the client supports it, `set_sandbox_mode(True)` is enabled.

### 4.2 Supported venues

| Venue | Status | Credential vars (resolved by `credentials.py`) | Notes |
|---|---|---|---|
| OKX | **BUILT, live-verified** | `OKX_DEMO_API_KEY` / `OKX_DEMO_SECRET_KEY` / `OKX_DEMO_PASSPHRASE` (fallback `OKX_API_KEY`/…) | swap, isolated/cross margin; the only configured + verified venue |
| KuCoin | **BUILT, untested live** | `KUCOIN_PAPER_API_KEY` / `KUCOIN_PAPER_SECRET` / `KUCOIN_PAPER_PASSPHRASE` | demo profile ships disarmed |
| Bybit | **BUILT, untested live** | `BYBIT_TESTNET_API_KEY` / `BYBIT_TESTNET_SECRET_KEY` | swap default type |
| Hyperliquid | **BUILT, untested live** | `HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_PRIVATE_KEY` | wallet-based auth (no passphrase); resolver returns the pair **only if both present** — fail-closed |

### 4.3 Fail-closed on missing executor

If the controlled surface is unavailable — `ExecutionService`/`ExecutorFactory` failed to import, or `ExecutorFactory.available() == []` because the optional `ccxt` dependency is missing — `_execute_okx_authoritative()` returns `not_submitted` / `execution_unavailable` and appends it to `logs/executions.jsonl`. It never panics, never writes a `PLANNED`/`SUBMITTED` journal record, and never guesses a venue. No executor → no order.

---

## 5. Strategy System

### 5.1 Strategy file schema (v2)

Strategies are validated against `schemas/strategy.schema.json` (a `oneOf` over v1 OKX-coupled and v2 exchange-agnostic shapes). Credentials are **explicitly forbidden** in any strategy file (`no_inline_credentials`). Annotated v2 example:

```jsonc
{
  "schema_version": 2,
  "strategy_id": "btcusdt_duo_base_dev_2h",  // join key; lowercase snake_case
  "name": "BTCUSDT Duo Base Dev 2H",
  "asset": "BTCUSDT",                          // must match alert symbol (uppercased)
  "instrument": {                              // v2 venue selection (no secrets)
    "exchange": "okx",                         // selects the CCXT venue
    "inst_id": "BTC-USDT-SWAP",                // OKX-native or CCXT-unified form
    "type": "swap"
  },
  "timeframe": "2h",                            // must match alert timeframe (canonical)
  "chart_type": "heikin_ashi",
  "indicator": "mxc duo-base",
  "indicator_version": "duo-base-2.5",
  "upper_band_mult": 1.40,
  "lower_band_mult": 0.95,
  "auto_alpha": false,
  "capital": { "budget_usd": 1500, "reinvest": true },  // notional = capital.budget_usd × leverage
  "leverage": 2,
  "margin_mode": "isolated",                    // → adapter tdMode
  "execution_mode": "demo",
  "submit_orders": true,                        // per-strategy submission gate
  "status": "active_demo"                       // gates whether alerts are accepted
}
```

A v2 file is bridged to the legacy execution keys at load (`normalize_strategy_record`): `instrument.inst_id → okx_inst_id`, `submit_orders → okx_submit_orders`. The `strategy_instrument()` helper returns one canonical `{exchange, inst_id, type}` block for either schema version.

### 5.2 Strategy statuses

| Status | Accepts signals? | Meaning |
|---|---|---|
| `active_demo` | yes | Live trial; submits to the configured demo/sandbox venue when all gates are armed |
| `trial_candidate` | yes | Monitored candidate; same intake path, can be promoted |
| `paper_only` | no | Rejected at intake as `strategy_not_active` (paper/observe context only) |
| `disabled` | no | Rejected at intake as `strategy_not_active` |

Only `active_demo` and `trial_candidate` pass `validate_strategy_alert()`; the other two are quarantined.

### 5.3 Shipped strategies

| ID | Asset | TF | Budget | Leverage | Venue | Status |
|---|---|---|---:|---:|---|---|
| `btcusdt_duo_base_dev_2h` | BTCUSDT | 2h | $1,500 | 2× | OKX swap | active_demo |
| `ethusdt_duo_base_dev_2h` | ETHUSDT | 2h | $2,000 | 2× | OKX swap | active_demo |
| `solusdt_duo_base_dev_3h` | SOLUSDT | 3h | $1,500 | 2× | OKX swap | active_demo |
| `xrpusdt_duo_base_dev_4h` | XRPUSDT | 4h | $1,500 | 2× | OKX swap | active_demo |

Total assigned demo budget: **$6,500** (notional after leverage: $13,000).

---

## 6. Data & State Model

### 6.1 Ledger files (append-only JSONL)

All ledgers live under `logs/` except `latest.json` (repo root). Writes use `append_jsonl` / `append_jsonl_durable` (fsync on the money path).

| File | Contents | Writer | Reader |
|---|---|---|---|
| `logs/shadow-intake.jsonl` | Every raw incoming alert, pre-validation | receiver | dashboard |
| `logs/shadow-webhooks.jsonl` | Per-alert intake summary | receiver | dashboard |
| `logs/shadow-decisions.jsonl` | Full processed decision record | receiver | dashboard |
| `logs/executions.jsonl` | Execution outcomes (incl. every `not_submitted` block) | `ExecutionService` / receiver | dashboard |
| `logs/execution-plan.jsonl` | Computed execution readiness per alert | receiver | dashboard |
| `logs/order-journal.jsonl` | Order state machine `PLANNED→SUBMITTED→FILLED/REJECTED/UNKNOWN` | `ExecutionService` | reconciler, dashboard |
| `logs/position-journal.jsonl` | Durable position-state transitions (write-ahead, replayable) | receiver (paper/state engine) | startup replay, dashboard |
| `logs/advisor-decisions.jsonl` | Pre-exec advisor verdicts | advisor subprocess path | dashboard |
| `logs/operator-alerts.jsonl` | Operator alerts (auth failure, queue saturation, …) | receiver | dashboard |
| `logs/reconcile-alerts.jsonl` | `RECONCILE_MISMATCH` / resolver-timeout alerts | reconciler | dashboard |
| `logs/state-alerts.jsonl` | Fail-closed state-write errors (e.g. ENOSPC) | receiver | operator |
| `logs/seen-signals.jsonl` / `shadow-duplicates.jsonl` | Dedupe window + duplicate hits | receiver | dashboard |
| `latest.json` (repo root) | Last processed signal snapshot | receiver | hermx-control skill, `/latest` |

The legacy OKX-named mirrors (`logs/okx-executions.jsonl` / `okx-execution-plan.jsonl`) were removed — nothing wrote them after the CCXT cutover. The dashboard keeps a read-only historical fallback so a pre-cutover box still renders old executions.

### 6.2 Corruption handling

`read_jsonl_tolerant()` performs bounded reverse-tail reads (no OOM on large logs). A corrupt or partially-written tail line is **quarantined, not fatal** — `startup_quarantine_partial_ledgers()` runs at boot, and `LEDGER_READ_STATS` records per-file skipped-line counts that the dashboard surfaces in its health view. The position journal additionally supports verified checkpoints (`_read_checkpoint` validates a sha256 of canonical state before trusting it) and segment rotation/retention, so journal-mode startup replay stays bounded and a missing/corrupt snapshot rebuilds by replay rather than silently resetting to empty.

---

## 7. Hermes Agent Integration

### 7.1 Architecture principle

The agent **only calls down through the same HTTP API a human would use** — loopback `127.0.0.1`. There is no privileged backchannel, no direct `ExecutionService` call, no credential access, no exchange SDK. Every money-safety property is enforced *below* the API seam, so the agent inherits the full gate chain whether it reads or relays.

### 7.2 hermx-control skill

`skills/hermx-control/SKILL.md` defines the only agent surface:

- **Reads:** `GET 127.0.0.1:8098/api` (positions, PnL, executor/ledger/freshness health), `GET :8098/health` (the `arm` block: `kill_switch_engaged`, `live_trading_enabled`, `demo_strategies`, `live_strategies`, `armed`), `GET :8891/health` and `:8891/latest`.
- **Relay:** `POST 127.0.0.1:8891/webhook` with an unaltered TradingView alert body.
- **Hard constraints (in the skill prose, enforced in code below it):** cannot set size/notional/leverage (there is no such field — the receiver computes notional from the strategy file), cannot override a strategy or a gate, cannot call an exchange/shell/filesystem, must report a read failure as **UNKNOWN** (never "flat"), and must never relay a signal a human didn't ask for.

The runtime behind the relay seam is `HermesRelayAdapter` (`src/skills/hermes_execution.py`): it normalizes signal+strategy into a controlled `execution_intent`, **fails closed** before any submit on an invalid side or unresolved venue mapping, returns `not_submitted` without calling the service in `dry_run`, and in `live` mode submits **only** through `ExecutionService` — owning no money-safety policy of its own.

### 7.3 Orchestration roles (conceptual)

These are **conceptual roles**, not a config-selected mode: there is **no `orchestration_mode`
flag**. A second config-gated mode was explicitly judged unnecessary — there is one path
(advisory/relay) and the gates decide the rest (see `docs/HERMES_AGENT_DESIGN.md`). The "agent"
row below is the planned end-state for who *originates* a relay; either way the same single
gate chain runs.

| Role | Who selects/drives execution | Agent role | Status |
|---|---|---|---|
| **deterministic** (today) | `webhook_receiver` matches `strategy_id` and drives `ExecutionService` | read-only observer + optional advisor | BUILT |
| **agent** | Hermes Agent selects `strategy_id` and relays `POST /webhook` | auto-executes — but the **full gate chain still applies** identically | PLANNED (conceptual) |

### 7.4 Advisor transport

`hermes -z "<prompt>" --skills hermx-control` subprocess (optionally `-m <model>`). Config: `HERMX_ADVISOR_ENABLED` (default false — a single live-veto switch; when on, a `skip` verdict blocks the trade, with no annotate-only middle mode), `HERMX_ADVISOR_TIMEOUT_SECONDS` (default 30), `HERMX_ADVISOR_COMMAND` (default `hermes`), `HERMX_ADVISOR_SKILLS` (default `hermx-control`), `HERMX_ADVISOR_MODEL` (optional `-m` override). **Fails open always** — a missing binary, non-zero exit, timeout, or malformed reply records the error and proceeds deterministically.

### 7.5 Telegram operator interface (planned)

A `hermes gateway` (native Telegram support) loads the `hermx-control` skill so an operator can query positions, PnL, arm status, and the last signal in chat. It is read-first; it inherits the same hard rule that it cannot execute without an explicit inbound signal or human instruction, and never bypasses the gate chain.

### 7.6 Slash-command skill set

Beyond the single `hermx-control` skill, HermX ships a set of focused operator slash commands
(`/hx-status`, `/hx-positions`, `/hx-strategy-list`, `/hx-trace`, `/hx-strategy-mode`, `/hx-close`,
`/hx-emergency-stop`, `/hx-restart`, `/hx-upgrade`, `/hx-help`). They follow a **4-layer architecture**:

```text
Hermes UI (slash command)  →  skills/hermx-*/SKILL.md  →  skills/hermx-ops/lib/hermx_ops.py  →  HermX runtime (dashboard :8098 / receiver :8891)
```

- **Hermes UI** — each `skills/hermx-*/SKILL.md` registers as a dynamic slash command.
- **SKILL.md** — command prose, safety guards, examples; carries `metadata.hermes` config with
  loopback defaults.
- **`hermx_ops.py`** — the shared helper library (`read_state()` encoding UNKNOWN-never-flat,
  `format_positions()`, `resolve_strategy()`, `post_close()`, `safe_update_control_state()`, …);
  every skill imports it via `sys.path.insert(0, "skills/hermx-ops/lib")`.
- **HermX runtime** — the same loopback HTTP API a human would use; all money-safety stays below
  the API seam.

The canonical API contract these skills speak is `skills/hermx-ops/references/api-contract.md`.
Full command reference: `docs/hermx-slash-commands.md`.

---

## 8. Deployment

### 8.0 Host topology

On a production VPS, **two separate units run on the same host** and communicate only via loopback:

```text
VPS Host  (Ubuntu 22.04)
│
├── HermX  [Docker / systemd]                        ← the money-safety plane
│   ├── receiver   127.0.0.1:8891  (webhook_receiver.py)
│   └── dashboard  127.0.0.1:8098  (dashboard.py)
│
└── Hermes Agent  [native install, ~/.hermes/]        ← the conversational plane
    ├── hermx-control skill  →  GET/POST 127.0.0.1:8891/:8098  (loopback only)
    ├── hermes gateway  →  Telegram bot  (persistent service, systemd on Linux)
    └── ~/.hermes/.env  (LLM provider key, TELEGRAM_BOT_TOKEN — never in HermX .env)

External
├── TradingView  →  Tailscale Funnel  →  :8891/webhook   (alerts, one-way)
└── Operator     →  Telegram          →  Hermes gateway  →  :8891/:8098 (loopback)
```

**Why Hermes is native, not in the Docker image:**
- Hermes is ~2.6 GB (own venv, Node runtime, model caches) — bloats the trading image unnecessarily.
- It carries durable per-user state (`state.db`, `sessions/`, `memories/`, `auth.json`) that cannot be baked into an immutable image layer.
- The two components have independent release cycles — `hermes update` must not force a HermX redeploy.
- Both units on the same host → loopback binding works with zero additional networking.

**Dev/Mac:** identical topology — Hermes installed in `~/.hermes/`, HermX in `.venv` or Docker, both on localhost. The `tradingview-bridge` MCP server (Mac-only) additionally lets Hermes read live chart state from TradingView Desktop, but this is advisory-only and absent on a headless VPS.

### 8.1 Process supervision

Two supervision modes run the *same* source. Both bind `127.0.0.1`.

**Systemd (VPS)** — `deploy/install-services.sh` installs both units:
- `deploy/hermx-receiver.service` — `ExecStart=.venv/bin/python src/webhook_receiver.py`, `Restart=always`, `RestartSec=5`, `EnvironmentFile=/opt/hermx/.env`, after `tailscaled.service`, with `StartLimitBurst=5` to prevent restart storms.
- `deploy/hermx-dashboard.service` — same pattern for `src/dashboard.py`.

**Docker** — `docker-compose.yml` builds one image (`Dockerfile`, `python:3.11-slim`) with two services:
- `receiver` + `dashboard`, both `network_mode: host` (required — both bind loopback; also lets Funnel reach `:8891`).
- Secrets come from `env_file: .env` (not a bind mount); `engine-config.json` and `strategies/` are bind-mounted **read-only** (`:ro`) — not editable in place.
- `HERMX_DATA_DIR=/app/data` on both services. Two named volumes persist across restarts: `hermx-data` → `/app/logs` (append-only ledgers, rw for the receiver, `:ro` for the dashboard) and `hermx-state` → `/app/data` (mutable snapshots, **rw**). The dashboard runs `read_only: true` on its root fs but still writes `control-state.json` to `hermx-state`, so that volume must stay writable even with the read-only root.
- `HEALTHCHECK` curls `/health` on each port. Secrets are never baked into the image.

### 8.2 Ingress

Tailscale Funnel (`--bg`, survives reboots) provides public HTTPS at a unique stable URL per install: `https://hermx.<user-tailnet>.ts.net/webhook`. No domain to buy, no inbound firewall rules, no public ports on the host.

### 8.3 Per-user isolation

Every install is fully isolated: its own Tailscale tailnet/URL, its own `.env`, its own `strategies/`, its own ledgers. There is no shared infrastructure or multi-tenant surface between installs.

---

## 9. Extension Guide

### 9.1 Adding a new exchange

1. Add the venue's credential resolution branch to `src/security/credentials.py` (`resolve_exchange_credentials`), namespaced and fail-closed.
2. Add a client-construction branch to `CcxtExecutor._client()` for the venue's auth shape (apiKey/secret/passphrase, or wallet/key).
3. Add any alias to `ExecutorFactory._aliases` if config may name it differently (the backend stays `ccxt`).
4. Create a `config/runtime.<exchange>.demo.json` profile — strategies stay in `execution_mode: "demo"` (sandbox) until the venue's gated write test passes; live execution additionally requires `HERMX_LIVE_TRADING=true`.
5. Add a gated integration test mirroring `tests/test_okx_paper_integration.py` (run behind an env flag like `HERMX_RUN_<EXCHANGE>_WRITE_TESTS`).

### 9.2 Adding a new strategy

1. Create `strategies/<id>.json` (copy `btcusdt_duo_base_dev_2h.json` as the v2 template).
2. Set `strategy_id`, `asset`, `instrument.{exchange,inst_id,type}`, `budget_usd`, `leverage`, `margin_mode`.
3. Set `status: "trial_candidate"` initially (promote to `active_demo` after validation).
4. Create the matching TradingView alert whose JSON body carries that `strategy_id` (and matching `symbol`/`timeframe`).

### 9.3 Adding a Hermes skill

1. Create `skills/<skill-name>/SKILL.md` following the `hermx-control` pattern (capabilities, hard rules, verification checklist).
2. Add any `scripts/` the skill needs.
3. Register it: `ln -sfn <path> ~/.hermes/skills/<name>`.
4. No HermX code changes are required — skills are pure HTTP calls to the existing loopback endpoints.

> **Not to be confused with the relay adapter.** The `HermesRelayAdapter`
> (`src/skills/hermes_execution.py`) is an internal Python component and tested reference
> seam — it is **not** a Hermes Agent skill. The agent's execution surface is the loopback
> HTTP API only.

### 9.4 Adding a gate (safety guard)

1. Add the predicate to the guard chain in `ExecutionService.execute()` (`src/execution/service.py`), returning a ledgered `not_submitted` when it vetoes.
2. Write a test asserting the gate blocks when its condition is met (mirror `tests/test_execution_gate_precedence.py` / `test_kill_switch.py`).
3. The floor rises: previously-passing trades that don't trip the new condition are unaffected; the new condition is now also required.

### 9.5 Adding intelligence (Kronos, MXC Dashboard, etc.)

Intelligence is **purely additive** and lives *outside* the money path. Pattern: a new Hermes skill calls the external API and returns a structured assessment; Hermes sequences skills before relaying to `POST /webhook`. HermX core is unchanged — new intelligence can inform *whether/which* signal is relayed, but the gate chain still owns *whether it submits*.

---

## 10. Security Model

- **Loopback-only servers.** Receiver (`:8891`) and dashboard (`:8098`) bind `127.0.0.1`; the only public surface is the Tailscale Funnel URL → `:8891/webhook`.
- **Webhook auth.** Constant-time `X-Webhook-Secret` compare (`HERMX_SECRET`); optional HMAC-SHA256 over `timestamp‖body` (`HERMX_REQUIRE_HMAC` + `HERMX_WEBHOOK_HMAC_KEY`) with a replay window (`HERMX_REPLAY_WINDOW_SECONDS`). Missing secret (or missing HMAC key when required) **fails closed** — every webhook gets `401`.
- **Rate limiting.** Per-source sliding window (`HERMX_RATE_LIMIT_WINDOW_SECONDS`, `HERMX_RATE_LIMIT_MAX_REQUESTS`) → `429`.
- **Body cap.** `HERMX_MAX_BODY_BYTES` (default 256 KiB) → `413` before the body is read.
- **Dashboard auth.** Optional, fail-closed: `HERMX_DASH_AUTH` on with a blank `HERMX_SECRET` returns `401` for protected routes. Accepts `X-Dashboard-Token`, `Authorization: Bearer`, or Basic, all constant-time compared.
- **Credentials.** Exchange keys are resolved per-exchange and namespaced (`src/security/credentials.py`); a missing/partial set disarms that venue and never borrows another's keys. Secrets are **never logged** — `redact_secrets()` scrubs known credential values and key/secret/passphrase/token patterns from every error string and adapter payload.
- **`.env` hygiene.** `chmod 600`, never committed (gitignored); `env_file_permissions_healthy()` warns at boot if it is group/other-readable.
- **Agent containment.** The agent has no access to `.env`, raw CCXT, or the filesystem for order purposes; its only order path is `POST /webhook`, behind the full gate chain.

---

## File Reference

| Path | Role |
|---|---|
| `src/webhook_receiver.py` | TradingView intake → auth/rate-limit/normalize/schema/dedupe → queue/workers → strategy match → readiness → execution + reconciliation; HTTP `:8891` |
| `src/webhook/money.py` | Pure money/decimal leaf: `Decimal` coercion (`D`), fixed-precision quantizers/formatters (`dec_usd`/`dec_notional`/`dec_pct`/`dec_units`), and recursive `canonicalize_decimal_fields`; re-exported by `webhook_receiver` for backward compatibility |
| `src/dashboard.py` | Read-only operator dashboard + `/api` + `/health`; HTTP `:8098` |
| `src/dashboard_core.py` | Dashboard data plumbing: tolerant ledger reads, OKX ticker cache, per-ledger read stats |
| `src/hermx_shared.py` | Single source of truth for `canonical_timeframe()` (shared by receiver + dashboard) |
| `src/execution/service.py` | `ExecutionService` — the single money-safety chokepoint (7 invariants); `resolve_execution_config` |
| `src/executors/base.py` | `BaseExecutor` contract + normalized fill/order/position/balance shapes |
| `src/executors/ccxt_adapter.py` | `CcxtExecutor` — sole execution backend: write path + normalized read/query contract |
| `src/executors/factory.py` | `ExecutorFactory` — venue→adapter registry, aliases, fail-closed `available()` |
| `src/executors/__init__.py` | Public executor API (`ExecutorFactory`, `BaseExecutor`) |
| `src/skills/hermes_execution.py` | `HermesRelayAdapter` — internal relay adapter (reference seam), not a Hermes Agent skill; delegates all safety to the service |
| `src/security/credentials.py` | Per-exchange namespaced credential resolution + `redact_secrets` |
| `src/security/webhook_auth.py` | Pure auth/rate-limit/HMAC/replay helpers |
| `deploy/deploy.sh` | Config-safe deploy script: snapshot, pull, pip install, UI build, test gate, restart, auto-rollback |
| `skills/hermx-control/SKILL.md` | Agent skill: loopback reads + signal relay, with hard money-safety rules (older single-skill model) |
| `skills/hermx-{status,positions,strategy-list,trace,strategy-mode,close,restart,upgrade,help}/SKILL.md` | Slash-command skills — one dynamic Hermes command each (see `docs/hermx-slash-commands.md`) |
| `skills/hermx-ops/` | Shared helper (`lib/hermx_ops.py`) + canonical API contract (`references/api-contract.md`) for the slash commands |
| `skills/*.md` | Flat operator runbooks (`emergency-stop`, `optimization-workflow`, `tradingview-alert-setup`, `tradingview-recovery`) |
| `schemas/strategy.schema.json` | Strategy file contract (v1 + v2, no inline credentials) |
| `schemas/tradingview-alert.schema.json` | Inbound alert contract (Draft 2020-12) |
| `engine-config.json` (repo root) | **Runtime/engine config source** — loaded via `load_engine_config()` (`src/dashboard_core.py`), consumed by the receiver. (`shadow-config.json` is dead code, not a config source.) |
| `config/runtime.demo.json` | Per-venue execution profile — OKX sandbox/demo (armed), not the engine runtime config |
| `config/runtime.{kucoin,hyperliquid}.demo.json` | Other-venue demo profiles (ship disarmed) |
| `config/runtime.live.example.json` | Live profile template (operator-created from explicit approval) |
| `strategies/*.json` | Sanctioned strategy files keyed by `strategy_id` |
| `deploy/hermx-receiver.service`, `hermx-dashboard.service` | systemd units (`Restart=always`) |
| `deploy/install-services.sh` | One-command systemd install |
| `docker-compose.yml`, `Dockerfile` | Single image, two services, host network, persistent ledger volume |
| `setup/env.example` | Annotated `.env` template (copy → `.env`) |
| `tests/` | Characterization + gated integration tests (kill switch, gate precedence, idempotency, order state machine, reconciliation, per-venue paper integration, schema/migration, advisor, dashboard) |
