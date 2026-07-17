# KAIRO Documentation (v0.1)

Your AI trading copilot for automating TradingView signals into real exchange orders — safely, transparently, and on dedicated secure infrastructure.

> **A note on naming.** KAIRO was built under the internal codename **HERMX**. You may still see the legacy `HERMX_` prefix on environment variables and a few `/hx-` command aliases in current builds; treat those as equivalent to the `KAIRO_` / `/kr-` names used throughout this documentation. The signal and market-regime intelligence brand is **Kinetic Flow**.

---

## Table of Contents

1. [Getting Started](#page-1-getting-started) — `getting-started`
   - [How It Works: The Basics](#how-it-works-the-basics)
   - [Why Automation?](#why-automation)
   - [Who KAIRO Is For](#who-kairo-is-for)
   - [Key Limitations](#key-limitations)
2. [How KAIRO Works](#page-2-how-kairo-works) — `how-kairo-works`
3. [Strategies](#page-3-strategies) — `strategies`
4. [Connecting TradingView](#page-4-connecting-tradingview) — `connecting-tradingview`
5. [The Alert Contract](#page-5-the-alert-contract) — `alert-contract`
6. [Budget, Sizing & Compounding](#page-6-budget-sizing--compounding) — `budget-and-sizing`
7. [Safety Gates & Kill Switch](#page-7-safety-gates--kill-switch) — `safety-gates`
8. [Order & Signal States](#page-8-order--signal-states) — `order-and-signal-states`
9. [The Dashboard](#page-9-the-dashboard) — `dashboard`
10. [The KAIRO AI Copilot](#page-10-the-kairo-ai-copilot) — `ai-copilot`
11. [Monitoring & Alerts](#page-11-monitoring--alerts) — `monitoring-and-alerts`
12. [Operator Commands & Emergency Controls](#page-12-operator-commands--emergency-controls) — `operator-commands`
13. [Quick Command Reference](#page-13-quick-command-reference) — `command-reference`
14. [Glossary](#page-14-glossary) — `glossary`

*(For Featurebase admins: see the note at the very end for how to split these into separate articles.)*

---

# Page 1: Getting Started

**KAIRO turns your TradingView alerts into live exchange orders — with a disciplined AI copilot watching the whole time.**

KAIRO is a crypto trading execution layer that receives your TradingView signals over a webhook, validates and sizes them, runs them through a chain of money-safety checks, and dispatches orders to your exchange. Unlike a hosted SaaS, KAIRO is **self-hosted, dedicated infrastructure** — it runs on your own server, with your own exchange keys and ledgers, not on shared multi-tenant plumbing. Every strategy starts in **demo** mode, and an always-on AI copilot answers your questions over Telegram.

## How It Works: The Basics

KAIRO is built from four simple building blocks. Once you understand these, the rest of the documentation is detail.

1. **Strategies.** A strategy is a single JSON file in `strategies/` that defines *what* to trade and *how*: instrument, venue, budget, leverage, and mode. The strategy — never the alert — owns all routing and sizing. See [Strategies](#page-3-strategies).
2. **Exchange credentials.** You bring your own API keys on your own server (OKX, KuCoin, Bybit, Binance, Bitget, Gate, Hyperliquid, Coinbase) through a unified CCXT adapter. Keys default to the exchange's demo/sandbox account until you deliberately go live.
3. **TradingView webhook.** All alerts hit one shared endpoint, `POST /webhook`. Each alert carries a `strategy_id`, which is how KAIRO knows exactly which strategy file to run.
4. **Safety gates + AI copilot.** Before any order reaches a venue it passes a deterministic gate chain (kill switch, notional cap, trading state, idempotency). Separately, an AI copilot on Telegram handles status and ops questions — it sits *on top of* the money path and never inside it.

That's the foundation. From here, dive into strategies, the alert contract, and the dashboard.

## Why Automation?

- **Remove emotion, improve consistency.** The same signal always produces the same deterministic decision — no hesitation, no second-guessing, no "just this once."
- **24/7 execution when signals fire.** Your server runs continuously, so a 3 a.m. signal executes whether or not you're awake or online.
- **Fewer delayed entries and manual order errors.** No fat-fingered sizes, no missed fills while you were away from the screen.
- **Run a whole desk from one place.** Operate multiple Kinetic Flow strategies across symbols and timeframes side by side.
- **Keep full ownership.** Your keys, your server, your ledgers — no shared multi-tenant infrastructure between you and your money.

## Who KAIRO Is For

- **Systematic traders** running Kinetic Flow (or other rule-based) strategies on TradingView who want hands-off, 24/7 execution.
- **Operators who want full ownership** — their own server, their own exchange keys, and no shared infrastructure.
- **Traders who want an AI copilot** for status, P&L, and emergency controls over Telegram — without ever putting AI inside the money path.
- **Explicitly NOT for:** high-frequency / sub-minute scalping, multi-tenant signal-selling platforms, or users who want a fully hosted SaaS with no server of their own.

KAIRO is designed for independent operators. You configure strategies, credentials, risk limits, and automation preferences yourself. Even when using third-party indicators, you own the execution stack.

## Key Limitations

We'd rather you know these up front than discover them live.

1. **Not designed for high-frequency or sub-minute trading.** Alert timeframes are `30m`, `1h`, `2h`, `3h`, and `4h`. TradingView webhooks do not retry failed deliveries, and end-to-end signal speed depends on TradingView plus your host latency.
2. **Signal conflicts are your responsibility.** KAIRO is event-driven; overlapping signals across strategies on the same symbol can race. Dedup only catches *identical* signals within 24 hours — it does not resolve conflicting opposite signals.
3. **Strategy state lives in KAIRO and the venue, not back in TradingView.** TradingView only ever sees `200 queued`. Order IDs, positions, and P&L live in KAIRO's dashboard and ledgers and on the exchange. Design your strategies accordingly.
4. **Not a set-it-and-forget-it black box.** Monitors and the copilot help, but you remain responsible for watching signals, open positions, credentials, and host health. Things can and will go wrong — stuck `UNKNOWN` orders, expired TradingView alerts, venue outages. Check the dashboard regularly, especially after config changes.
5. **Self-hosted operational burden.** You run the server, secrets, restarts, and upgrades. There is no multi-tenant cloud ops team behind your instance.
6. **Crypto-focused exchange execution.** Supported venues are crypto exchanges via CCXT (OKX, KuCoin, Bybit, Binance, Bitget, Gate, Hyperliquid, Coinbase). KAIRO does not trade stocks, options, or prop-firm broker accounts.

`[Screenshot: KAIRO dashboard overview showing strategy cards, positions, and the arming banner]`

**Next:** [How KAIRO Works →](#page-2-how-kairo-works)

---

# Page 2: How KAIRO Works

**A single alert travels a fixed path: receive → validate → size → safety-check → dispatch → reconcile.** Here's the journey end to end. This page is the detailed pipeline; for the four building blocks, see [Getting Started](#page-1-getting-started).

## The signal-to-order flow

```
TradingView alert
  → POST /webhook            authenticate, log to durable WAL, acknowledge
    → validate               normalize, schema check, match a strategy, deduplicate
      → build order          venue + sizing from the strategy file
        → safety gates        kill switch, notional cap, trading state, idempotency
          → dispatch          send to the exchange via the CCXT adapter
            → reconcile       confirm the real outcome against the venue
```

## Step by step

1. **Receive.** TradingView fires a webhook to `POST /webhook`. KAIRO authenticates it, writes the raw payload to a durable write-ahead log, and immediately replies `200 {"ok": true, "status": "queued", "received_at": "…", "queue_depth": 3}`. That log — not memory — is the source of truth, so a restart never loses an accepted alert.

2. **Validate.** A background worker normalizes the payload, checks it against the alert schema, matches it to a strategy file, and deduplicates it. A repeated signal within 24 hours is rejected as a duplicate and never executed twice.

3. **Size.** The matched **strategy file** — not the alert — decides *where* the order routes (which exchange, demo or live) and *how big* it is (budget × leverage, optionally compounding).

4. **Safety-check.** The order passes through a precedence-ordered gate chain. If any gate blocks it, KAIRO records exactly which one fired and stops — no order reaches the venue.

5. **Dispatch.** The order is journaled (`PLANNED → SUBMITTED`) and sent to the exchange. A confirmed fill becomes `FILLED`; anything uncertain becomes `UNKNOWN` (never a fabricated rejection).

6. **Reconcile.** After submission, KAIRO checks the real venue state to resolve the final outcome and record P&L. Nothing is invented — the durable ledger always wins over a live snapshot.

## Why it's built this way

- **Restarts are routine.** The system is designed to restart frequently and recover cleanly by replaying its write-ahead log.
- **The alert carries only the signal.** Venue, mode, and sizing all come from your strategy files, so the same alert shape works for every exchange.
- **Every outcome is logged, never returned to TradingView.** TradingView only sees "queued." The real result (traded, skipped, blocked, duplicate) lives in your logs and dashboard.

`[Screenshot: dashboard execution ledger showing a signal flowing to a FILLED order]`

**Next:** [Strategies →](#page-3-strategies)

---

# Page 3: Strategies

**A strategy is one JSON file that tells KAIRO what to trade, where, how much, and in which mode.** Strategies are the center of the system — everything routes through them.

Each strategy lives as a single file in `strategies/`. It defines the instrument, timeframe, indicator, budget, leverage, and execution mode.

## The strategy file

```json
{
  "schema_version": 2,
  "strategy_id": "solusdt_kinetic_flow_3h",
  "name": "SOLUSDT Kinetic Flow 3H",
  "indicator": "kinetic-flow",
  "timeframe": "3h",
  "instrument": {
    "exchange": "okx",
    "inst_id": "SOL-USDT-SWAP",
    "type": "swap"
  },
  "capital": {
    "budget_usd": 1500,
    "reinvest": true
  },
  "execution_mode": "demo",
  "leverage": 2,
  "margin_mode": "isolated"
}
```

## Key fields

| Field | What it does |
|---|---|
| `strategy_id` | Unique ID. Every alert must reference it so KAIRO knows which strategy to run. |
| `instrument.exchange` | **The only source of venue routing.** The alert never picks an exchange. |
| `instrument.inst_id` | The exact exchange instrument. The traded asset is **derived** from it (`SOL-USDT-SWAP` → `SOLUSDT`); an optional explicit `instrument.asset` overrides the derivation. |
| `timeframe` | Must match the TradingView alert's timeframe. |
| `capital.budget_usd` | The starting margin budget (the "seed"). Not the leveraged notional. |
| `capital.reinvest` | Compounding switch (default **on**). See [Budget & Sizing](#page-6-budget-sizing--compounding). |
| `leverage` | Notional multiplier used for sizing. |
| `execution_mode` | `demo` (sandbox/paper) or `live` (real money). |

## Rules to remember

- The strategy's asset (`instrument.asset` if set, else derived from `instrument.inst_id`) **should match** the alert's `symbol`; a mismatch is a soft warning (`strategy_warning: strategy_symbol_mismatch`) — the alert still executes on the strategy's instrument, and a close is never blocked.
- The strategy's `timeframe` **must match** the alert's `timeframe`.
- `execution_mode` is a two-value enum. **Only `live` is real money** — and it additionally requires the global kill switch (`KAIRO_LIVE_TRADING=true`) to be released. `demo` always routes to the exchange sandbox.
- One asset can have **multiple strategies** (e.g. a 3H production strategy and a research strategy). This is exactly why `strategy_id` is required on every alert.

## Example: an active demo portfolio

| File | Symbol | Timeframe | Seed | Leverage |
|---|---|---|---:|---:|
| `btcusdt_kinetic_flow_2h.json` | BTCUSDT | 2H | $1,500 | 2× |
| `ethusdt_kinetic_flow_2h.json` | ETHUSDT | 2H | $1,500 | 2× |
| `solusdt_kinetic_flow_3h.json` | SOLUSDT | 3H | $1,500 | 2× |
| `xrpusdt_kinetic_flow_4h.json` | XRPUSDT | 4H | $1,500 | 2× |

`[Screenshot: strategy cards grid on the dashboard, one card per strategy file]`

**Next:** [Connecting TradingView →](#page-4-connecting-tradingview)

---

# Page 4: Connecting TradingView

**Point a TradingView alert at your KAIRO webhook and your signals start executing.** This page is the practical setup guide; the field-by-field contract is on the next page.

## Setup in 6 steps

1. **Open the right chart.** Use the symbol *and* timeframe that match your strategy file (e.g. SOLUSDT on the 3H chart for `solusdt_kinetic_flow_3h`). A mismatched chart is quarantined, not silently accepted.

2. **Create the alert.** Set the condition to your Kinetic Flow BUY/SELL signal. Set **Trigger = Once Per Bar Close** — intrabar triggers fire on unconfirmed signals.

3. **Enable the Webhook URL** notification and set it to your KAIRO endpoint: `https://<your-host>/webhook`.

4. **Authenticate with your shared secret.** For a **native TradingView alert** (the default), put the secret in the message body as `"secret_key": "<KAIRO_SECRET>"` — TradingView's native webhook cannot send custom headers. For a **relay/proxy** setup, inject the `X-Webhook-Secret` header instead. **Never** put the secret in the URL.

5. **Paste the message JSON.** Use one of the ready-made templates below. Keep `timeframe` and `source` hard-coded.

6. **Set Expiration** to open-ended or the longest available — TradingView silently stops delivering at expiration and never retries failed sends.

## Message template

Paste into the alert's **Message** box. TradingView substitutes `{{ticker}}`, `{{strategy.order.action}}`, `{{close}}`, and `{{time}}` at fire time. Replace `<KAIRO_SECRET>` with your real secret.

```json
{"strategy_id":"solusdt_kinetic_flow_3h","strategy_name":"SOLUSDT Kinetic Flow 3H","indicator":"kinetic-flow","symbol":"{{ticker}}","timeframe":"3h","action":"{{strategy.order.action}}","tv_signal_price":"{{close}}","tv_time":"{{time}}","source":"tradingview","secret_key":"<KAIRO_SECRET>"}
```

> Tip: KAIRO can generate copy-paste BUY and SELL templates for any strategy with `/kr-tv-alerts <symbol>`.

## Test before you go live

```bash
curl -sS -X POST "https://<your-host>/webhook" \
  -H "Content-Type: application/json" \
  -d '{"strategy_id":"solusdt_kinetic_flow_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview","secret_key":"'"$KAIRO_SECRET"'"}'
```

Expect `200 {"ok": true, "status": "queued", ...}`. The order outcome is asynchronous — check your dashboard or logs. Re-sending the same body within 24 hours is a duplicate; change `tv_time` to test again.

## Common pitfalls

- **`action` values other than `buy`, `sell`, `close`** (e.g. `long`/`short`) are rejected. (The schema reads only `action` — there is no `side` field.)
- **Alert on the wrong chart/timeframe** → quarantined as a symbol/timeframe mismatch.
- **Missing or wrong secret** → `401 forbidden`; TradingView shows the webhook as failing with no detail. Re-check your `secret_key`.
- **Using `{{interval}}` for `timeframe`** → hard-code it instead, so a misplaced alert is caught rather than silently accepted.
- TradingView webhooks require a **paid plan** and **do not retry** failed deliveries.

`[Screenshot: TradingView "Create Alert" dialog with Webhook URL and Message JSON filled in]`

**Next:** [The Alert Contract →](#page-5-the-alert-contract)

---

# Page 5: The Alert Contract

**Every alert is a small JSON message. This is the exact contract KAIRO accepts.** The alert carries only the *signal* — never venue, sizing, or mode.

## Required fields

```json
{
  "strategy_id": "solusdt_kinetic_flow_3h",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "action": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

| Field | Notes |
|---|---|
| `strategy_id` | Must match a file in `strategies/`. |
| `symbol` | e.g. `SOLUSDT`. KAIRO uppercases it and strips `OKX:` / `-` / `/`. |
| `timeframe` | One of `30m`, `1h`, `2h`, `3h`, `4h`; must match the strategy. |
| `action` | `buy` (enter long), `sell` (enter short), or `close` (flatten). **The sole direction field.** |
| `tv_signal_price` | Use `{{close}}`. |
| `tv_time` | Use `{{time}}`. |
| `source` | Must be `"tradingview"`. |

**Optional:** `strategy_name`, `indicator`, and an `extras` object for observe-only debug context (never affects routing or sizing).

## What the alert does *not* carry

There is **no `exchange`, size, notional, budget, or leverage field.** All of that comes from the strategy file matched on `strategy_id`. This keeps the contract exchange-agnostic — the same alert shape works everywhere.

## Close semantics

An alert with `"action": "close"` **flattens the strategy's open position, reduce-only.** It never opens a new position, needs no direction field, and — like the operator close button — **bypasses the kill switch and symbol pauses**. A close can only reduce exposure, so KAIRO always allows it.

## Authentication

The shared secret (`KAIRO_SECRET`) travels one of two ways:

- **Default — `secret_key` JSON body field.** The standard method for native TradingView alerts.
- **Alternative — `X-Webhook-Secret` header.** For relay/proxy setups. When present, the header takes precedence and must match.

The secret is stripped immediately after authentication, so it never lands in any log. Never place it in the URL.

## Validation errors reference

Validation happens in two stages. **Stage 1 (transport)** returns an HTTP status directly to TradingView. **Stage 2 (semantic)** usually acknowledges `200 queued` and records the real outcome in your logs — but two semantic errors are **hard rejections** returned directly to TradingView, not queued acknowledgements: a missing `strategy_id` is a `400`, and an unknown `strategy_id` is a `404`.

| Reason | Stage | Meaning |
|---|---|---|
| `invalid_json` (400) | 1 | Body isn't valid JSON (trailing comma, unquoted placeholder). |
| `payload_too_large` (413) / `rate_limited` (429) / `queue_full` (503) | 1 | Body too big, too many requests, or the queue is saturated. |
| `401 forbidden` / `missing_webhook_secret` | 1 | Secret missing/wrong, or the server secret is unset (fails closed). |
| `side_not_allowed` (400) | 2 | `action` missing or not `buy`/`sell`/`close`. |
| `missing_strategy_id` (400) | 2 | A recognized alert arrived with no `strategy_id`. Hard-rejected, not queued. |
| `unknown_strategy_id` (404) | 2 | No matching file in `strategies/`. Hard-rejected, not queued. |
| `strategy_symbol_mismatch` (warning) | 2 | `symbol` doesn't match the strategy's asset. SOFT: still executes on the strategy's instrument; recorded as `strategy_warning`. |
| `strategy_timeframe_mismatch` (202) | 2 | Alert on the wrong timeframe. |
| `symbol_not_allowed` (400) | 2 | A no-`strategy_id` alert for a symbol no strategy trades. |
| `non_tradingview_source` (202) | 2 | `source` isn't `tradingview`; acknowledged but ignored. |
| `dedup_reject` | 2 | A duplicate signal within 24 hours. |

**Next:** [Budget, Sizing & Compounding →](#page-6-budget-sizing--compounding)

---

# Page 6: Budget, Sizing & Compounding

**KAIRO decides how much each strategy trades — and grows or shrinks that as the strategy wins or loses.** This page explains the money model.

## Seed vs. equity

| Term | Meaning |
|---|---|
| **Seed** (`capital.budget_usd`) | The starting capital you set in the strategy file. Fixed until you edit the file. |
| **Equity** | Seed *plus* every realized net profit or loss from that strategy's closed trades. Moves automatically. |

With compounding on (the default), each new trade sizes off **equity**, not the fixed seed. Win, and the next trade is bigger; lose, and it's smaller.

```
equity      = seed + realized net P&L
sizing      = max(equity, 0)
notional    = sizing × leverage
```

## Compounding on/off

The switch is `capital.reinvest`:

| Value | Behavior |
|---|---|
| `true` (**default**) | Trades size off equity. Compounding. |
| `false` | Trades always size off the fixed seed. |

## When a strategy runs out of money

If equity hits **zero or below**, KAIRO stops the strategy from opening **new** trades (the "equity stop"). It will **never** refuse to close an existing position. Recovery is automatic: the moment equity is positive again — because a later close booked a profit, or you raised the seed — the next signal re-arms trading. No manual reset.

## Demo and live are always separate

Equity is computed only from trades closed in the **same account mode** the strategy is running in. Demo profit can never inflate live sizing, and vice versa. Flipping a strategy's mode also flips which ledger its equity sums from.

## Notional ceilings

Two independent, operator-set **absolute** ceilings can refuse an oversized order before it reaches the venue:

- `capital.max_notional_usd` (per strategy)
- the `KAIRO_MAX_NOTIONAL_USD` environment variable (global)

The effective cap is the **smaller** of the two. Both are deliberately independent of `budget × leverage`, so a fat-fingered budget can't quietly raise its own ceiling. Both unset = no cap.

## Editing the budget

Sizing is recomputed on every signal, but its inputs have different freshness:

| Input | Takes effect |
|---|---|
| `budget_usd`, `reinvest`, `leverage`, `max_notional_usd` (strategy file) | **After a receiver restart** — strategy files are cached at startup. |
| Realized P&L, accounting window, mode override | **Immediately, next signal** — re-read live. |

After an edit and restart: open positions are left alone (no resizing mid-trade), and the next new trade sizes off `new seed + realized P&L so far`.

**Want a clean slate without touching the seed?** Set an **accounting window** (`accounting_start_at`) via the API or dashboard. It excludes older closes from the equity sum without deleting them from the permanent ledger — and takes effect with no restart.

## Where P&L comes from

`closed-trades.jsonl` is the append-only, lifetime P&L ledger — never rotated or pruned. Each row records gross P&L, fees, net P&L, mode, and the strategy it belongs to.

> **Gross vs. net:** KAIRO displays **gross** realized P&L as the authoritative figure until each venue's fee-inclusion behavior is verified empirically. Net (gross + fees) is used for sizing.

**Next:** [Safety Gates & Kill Switch →](#page-7-safety-gates--kill-switch)

---

# Page 7: Safety Gates & Kill Switch

**Before any order reaches an exchange, it passes a chain of money-safety gates. If one blocks, KAIRO records which and stops.** Safety is plain code — never AI judgment.

## The gate chain (in order)

1. **Arming + health** — the strategy must be un-paused, webhook auth must be healthy, and the liveness watchdog must not be paused.
2. **Canonical mode** — `execution_mode` must be exactly `demo` or `live`. Anything else is a config typo and fails closed.
3. **Live kill switch** — any order resolving to a **real** account requires `KAIRO_LIVE_TRADING=true`. A `close` order **bypasses** this gate.
4. **Symbol pause** — a per-symbol hard block on new submissions (set automatically on a stuck order, or manually); closes always pass.
5. **Pre-trade notional cap** — planned notional must not exceed the smaller of `capital.max_notional_usd` and `KAIRO_MAX_NOTIONAL_USD`.
6. **Global trading state** — in `reducing`, every new open/reversal is blocked; closes always pass.
7. **Equity stop** — a compounding strategy with depleted equity can't open new risk; closes always pass.
8. **Idempotency** — a client order ID already in the journal is refused, so a duplicate can never double-execute.

## The two switches that arm live trading

Real money only moves when **two independent controls agree**:

1. A strategy's `execution_mode` (or dashboard override) is set to **Live**, **and**
2. The global kill switch `KAIRO_LIVE_TRADING` is set to `true`.

If either is off, live orders are blocked. This is why the dashboard's **Live** button stays locked (🔒) until the kill switch is released.

`[Screenshot: dashboard arming banner in amber "DEMO MODE — kill switch active"]`

## Global trading states

Stored in `control-state.json` and shown in the API:

| State | Behavior |
|---|---|
| **`active`** | Normal trading. |
| **`reducing`** | Risk-off wind-down: new opens/reversals blocked, **closes always pass.** Emergency flatten still works. |

Set it via the API or `/kr-emergency-stop`. There is no UI button for this by design.

## Symbol pauses

`control-state.json` can also carry per-symbol **pauses** — a hard block on new submissions for one symbol. Pauses are set automatically when an order gets stuck in an unresolvable state, or manually via `/kr-emergency-stop pause-symbol <sym>`. A paused symbol rejects everything **except closes**.

## The invariant that ties it together

> **KAIRO never blocks a close.** No gate, state, pause, or outage can stop you from flattening a position. Emergency exits work exactly when new entries are disabled — because that's precisely when you need them.

**Next:** [Order & Signal States →](#page-8-order--signal-states)

---

# Page 8: Order & Signal States

**Every signal gets a decision, and every order gets a lifecycle state. Knowing them lets you read the dashboard at a glance.** This is your decision-state reference.

## Signal decisions

When a TradingView signal is processed, it lands on one of these outcomes (shown in the Strategy Alert Log):

| Decision | Meaning |
|---|---|
| **TRADE** | Matched a strategy, passed validation, and was handed to execution. |
| **SKIP** | Received and logged, but not executed (e.g. no strategy matched, or observe-only). |
| **DUPLICATE** | A repeat of a signal already seen within 24 hours. Never executed twice. |
| **BLOCKED** | A safety gate refused it. The block reason names which gate fired. |

## Order lifecycle states

Once an order is created, it moves through the order journal:

```
PLANNED → SUBMITTED → FILLED
                    → REJECTED
                    → UNKNOWN → (reconciled) → FILLED | REJECTED
```

| State | Meaning |
|---|---|
| **PLANNED** | The order passed the gates and was journaled, about to be sent. |
| **SUBMITTED** | The exchange acknowledged receipt; the fill is not yet confirmed. |
| **FILLED** | A confirmed fill. Terminal. |
| **REJECTED** | The venue explicitly rejected it. Terminal. |
| **UNKNOWN** | The outcome is genuinely uncertain — a timeout, exception, or partial multi-leg submit. **KAIRO never fabricates a rejection.** Reconciliation later resolves it against the venue. |

## Why UNKNOWN matters

`UNKNOWN` is a feature, not a bug. If KAIRO can't confirm what happened, it says so rather than guessing "flat" and risking a double position. A stuck `UNKNOWN` order:

- stays visible in the Open Orders table until resolved,
- can trigger an automatic **symbol pause** to prevent a colliding order,
- and is reconciled against the real venue state — not the dashboard's guess.

> **UNKNOWN, never "flat."** Across the whole system, any failed or degraded read reports **UNKNOWN** rather than an empty (flat) position. Only a healthy, confirmed, empty book is genuinely flat.

## Strategy modes

Each strategy card shows a three-state mode pill:

| Mode | Behavior |
|---|---|
| **Pause** | No orders submitted. Signals are still validated and logged. |
| **Demo** | Orders route to the sandbox/paper account. |
| **Live** | Orders route to the real account. Locked until the kill switch is released. |

Clicking the pill writes an override in `control-state.json` — it never edits your strategy file. Clearing the override reverts to the file's mode.

`[Screenshot: strategy card with the Pause/Demo/Live pill and a LONG position badge]`

**Next:** [The Dashboard →](#page-9-the-dashboard)

---

# Page 9: The Dashboard

**A local web app that shows everything KAIRO is doing — strategies, positions, P&L, and controls — and lets you pause, resume, and switch modes.**

## Access

- **URL:** `http://127.0.0.1:8098` (port from `KAIRO_DASHBOARD_PORT`). Binds to loopback by default.
- **Auth:** every page and API route requires your `KAIRO_SECRET`. In the browser, the Basic auth prompt appears — leave the username blank and paste the secret as the password. Tools can send `Authorization: Bearer <secret>`.
- **Pages:** the main dashboard (`/`) and a **System Health** page.

Data refreshes on a background loop; numbers can lag reality by up to ~25 seconds. KAIRO computes freshness from true data age, so a stalled feed shows as **stale** rather than quietly looking current.

## Layout, top to bottom

| Section | Shows |
|---|---|
| **Arming banner** | Red **LIVE TRADING ARMED**, amber **DEMO MODE — kill switch active**, or **System disarmed**. |
| **Summary cards** | System status, strategy count (demo/live split), open positions (longs/shorts), execution engine health. |
| **Strategy cards** | One per strategy: symbol, timeframe, config badges, the Pause/Demo/Live pill, position side, budget, equity, UPnL, mark price, alert count. |
| **Positions** | First-class positions view: an **OPEN** table (live venue positions with entry, mark, and unrealized P&L) and a **CLOSED** table (completed round trips with entry/exit, net P&L, and fees). A strategy filter in the header scopes this section and the event tables below it. A warning appears if the ledger and the venue disagree about open exposure (observe-only — KAIRO never auto-corrects). |
| **Execution ledger** | Trade rows: time, asset, side, fill price, notional, state (FILLED / REJECTED / UNKNOWN), P&L. These are **order events** — for positions and realized P&L, read the Positions section. |
| **Strategy alerts** | Every matched signal with its decision (TRADE / SKIP / DUPLICATE / BLOCKED) and block reason. |
| **Open orders** | Non-terminal orders — in-flight and stuck **UNKNOWN** rows. |
| **Reconcile & operator alerts** | Reconcile mismatches, position drift, and operator actions. |

`[Screenshot: full dashboard with arming banner, summary cards, and strategy grid]`

## Which number to trust

Two kinds of data sit side by side:

- **Live panel** (positions, UPnL, mark price on cards) — a best-effort venue readback. Informational; may be stale. If it fails, the Engine card shows **ERROR/STALE** rather than pretending "flat."
- **Ledgers** — the durable record of what actually happened. **If the two disagree, the ledger wins.**

For accounting-grade P&L, read the `strategy_pnl` object from the `/api` endpoint rather than eyeballing a card:

```bash
curl -s -H "Authorization: Bearer $KAIRO_SECRET" http://127.0.0.1:8098/api \
  | jq '.strategies[] | select(.strategy_id=="solusdt_kinetic_flow_3h") | .strategy_pnl'
```

## Controls

The mode pill is the only button. Trading state and accounting windows are set via the API or slash commands:

```bash
# Pause / resume / switch a strategy
curl -X POST -H "Authorization: Bearer $KAIRO_SECRET" -H "Content-Type: application/json" \
  -d '{"mode": "pause"}' http://127.0.0.1:8098/api/control/strategy/<strategy_id>
# mode: "pause" | "demo" | "live" | "clear"

# Risk-off: block new opens, allow closes
curl -X POST -H "Authorization: Bearer $KAIRO_SECRET" -H "Content-Type: application/json" \
  -d '{"state": "reducing"}' http://127.0.0.1:8098/api/control/trading-state
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard looks plain/old | The SPA build is missing; rebuild the UI. |
| 401 on every route | `KAIRO_SECRET` unset/blank or wrong token. |
| `Engine - ERROR` | Venue readback failed — check credentials or venue status. |
| Mode pill reverts after clicking | The state write failed silently — the data directory needs to be writable. |
| Cards show FLAT but a position is open | Check Engine status first — never trust an errored read as "flat." |

**Next:** [The KAIRO AI Copilot →](#page-10-the-kairo-ai-copilot)

---

# Page 10: The KAIRO AI Copilot

**KAIRO's copilot is an always-on AI assistant that reads your system, answers questions in plain language, and — as it earns trust — helps guard the desk.** It sits *on top of* the deterministic core, never inside it.

## What it does today

The copilot behaves like a disciplined human trading assistant. Over **Telegram**, you can ask:

> *"What's open? What's our P&L? Is it armed? What happened with the last alert?"*

…and get a truthful, plain-language answer. It reads the same loopback API a human with `curl` would use — it holds **no exchange credentials**, computes **no order sizes**, and its only order-creating action is a `POST /webhook` that the same deterministic gate chain evaluates exactly as if TradingView had sent it.

`[Screenshot: Telegram chat asking "is KAIRO armed?" with the copilot's status reply]`

## The core invariant

> **Money-safety lives in code, never in AI prose.** Every "the copilot decides" statement is bounded by the deterministic gates. The copilot grows more capable *in front of* the safety floor — never by lowering it.

Because of this, the intelligence layer **fails open**: a slow, broken, or absent LLM changes nothing about execution. The desk keeps running deterministically.

## Progressive autonomy

The copilot's authority increases in discrete, individually reversible phases — each with an explicit entry gate and an instant rollback (an env flag):

| Phase | Role |
|---|---|
| **Advisory** *(today)* | Reads state, answers questions, carries out explicit operator commands. |
| **Gated (veto)** *(built, off by default)* | Every signal is shown to the copilot *before* the gate chain; it may answer `proceed` or `skip`. Fails open. |
| **Conviction-scored** *(planned)* | The copilot scores signal conviction from multiple inputs. |
| **Discretionary** *(planned)* | Calibrated judgment learned from its own trade history. |
| **Proactive** *(planned)* | Monitors risk beyond the desk and raises issues unprompted. |

Every advisor verdict and execution outcome is logged, so simply running the system builds a real `(signal, context, outcome)` dataset the copilot can learn from over time.

## Kinetic Flow risk context

As it graduates, the copilot is planned to consult external risk context — most importantly the **Kinetic Flow risk dashboard**, which provides market **regime** and risk-state signals. **On the roadmap** (not a live, per-strategy toggle today), a strategy would optionally be able to have KAIRO call the Kinetic Flow risk endpoint before executing, so a signal fired into a hostile regime could be vetoed. It is designed to **fail open**: if the risk API is slow or down, execution proceeds deterministically rather than wedging.

## Running the desk from your phone

The copilot carries your commands as both slash commands and natural language — status, positions, P&L, pause/resume, close, and emergency stop — all over Telegram. The Telegram gateway is **comms-only**: it grants no trading authority, and its downtime never blocks execution.

**Next:** [Monitoring & Alerts →](#page-11-monitoring--alerts)

---

# Page 11: Monitoring & Alerts

**KAIRO watches itself for the failures that quietly cost money — a stuck order, a stalled feed, a dead process — and pings your Telegram only when something is genuinely wrong.**

## Why a separate watchdog

The expensive failures in an unattended trading system are **silent**: an order stuck in `UNKNOWN`, a reconcile mismatch no one reads, TradingView quietly stopping while `/health` stays green. In-process checks can't be trusted to report their own death, so monitoring runs in a **separate process** — a cron scheduler that ticks every 60 seconds and delivers to Telegram.

## Design posture

- **Fail-open.** A monitor that can't read state emits nothing — never a false all-clear, never a false alarm. A monitor that crashes surfaces the crash.
- **Read-only.** Monitors only read system state; they never touch the money path.
- **Only speak on change.** Fingerprint dedup and suppression windows keep Telegram quiet unless a problem is genuinely new, escalated, or overdue.

## The monitor jobs

| Job | Cadence | Watches |
|---|---|---|
| **health-check** | every 5 min | Receiver/dashboard liveness, kill-switch state. A broken watchdog auto-delivers an error — it can't fail silently. |
| **reconcile** | every 5 min | Reconcile/watchdog alerts, stuck orders, ledger mismatches, rejected orders. |
| **ledger-reconcile** | every 10 min | Drives the P&L ledger reconcile so closes keep getting recorded. |
| **signal-late** | every 30 min | Zero TradingView intake (absence detection) — catches TradingView silently stopping. |
| **daily digest** | 08:00 UTC | A daily operator summary. |
| **weekly digest** | Mon 09:00 UTC | A weekly summary. |

## How alerting stays quiet

Each problem has a stable **fingerprint** (e.g. `reconcile:stuck_order:<id>`, `health:receiver_down`) with no timestamps or counters — so the "same" problem doesn't re-alert on every tick. A per-concern suppression window (e.g. 30 minutes for reconcile issues) throttles repeats, and severity is ranked `info < warning < error < critical`.

## Gated vs. plain jobs

- **Plain jobs** run a script only. Silence means all-clear; any output is delivered verbatim; a crash or timeout auto-delivers an error.
- **Gated jobs** run a cheap pre-check first. Only if something fresh is found does it wake the AI copilot to write a human summary — which is either delivered, or suppressed as `[SILENT]` if the copilot inspects it and finds it benign.

`[Screenshot: Telegram alert from the reconcile monitor flagging a stuck order]`

## What it doesn't cover (yet)

A dedicated market-risk watchdog was specified but cut before shipping, and a redundant reconcile-lag job was retired in favor of the sharper conditions inside the reconcile monitor. Absence detection (zero-intake) is covered; deeper risk-regime monitoring is on the roadmap alongside the copilot's Kinetic Flow integration.

**Next:** [Operator Commands →](#page-12-operator-commands--emergency-controls)

---

# Page 12: Operator Commands & Emergency Controls

**KAIRO gives you twelve headline operator commands — five read-only diagnostics, seven guarded mutations — to run and protect the desk.** None of them places or sizes an order directly.

Commands are invoked as `/kr-<name>`. Every **mutating** command dry-runs first and requires an explicit `yes` before it writes. Promoting a strategy to **live** always requires an explicit confirmation.

## Read-only diagnostics

| Command | What it tells you |
|---|---|
| **`/kr-status`** | Armed? mode, dashboard/receiver up, last alert, strategy count. |
| **`/kr-positions`** | Open positions: side, size, entry, mark, UPL, leverage. |
| **`/kr-strategy-list`** | Every strategy with its file mode vs. effective mode, and whether it's paused. |
| **`/kr-trace <symbol>`** | Follows one signal from intake → dedupe → pipeline → execution, showing where it stopped. |
| **`/kr-tv-alerts <symbol>`** | Prints copy-paste BUY and SELL TradingView templates for a strategy. |

## Guarded mutations

| Command | What it does |
|---|---|
| **`/kr-strategy-mode <id> <pause\|resume\|demo\|live>`** | Sets a per-strategy override. Never edits the strategy file. Live requires `yes`. |
| **`/kr-close <symbol>`** | Flattens ONE position, reduce-only and sizeless. Refuses if the read is UNKNOWN. |
| **`/kr-emergency-stop <action>`** | Layered stop: `kill` / `flatten` / `demo <id>` / `pause-symbol <sym>`. |
| **`/kr-restart`** | Restarts a down dashboard/receiver. |
| **`/kr-upgrade`** | Pull + deps + UI build + tests + restart, with auto-rollback on failure. |
| **`/kr-exchange <...>`** | Add/update/remove/validate exchange API keys (the command never handles the key itself). |
| **`/kr-telegram <...>`** | Set up/rotate/allowlist the Telegram operator gateway (never handles the bot token). |

## The emergency stop, layered

`/kr-emergency-stop` gives you graduated responses:

- **`kill`** — engage the global live kill switch (`KAIRO_LIVE_TRADING=false` + restart), confirmed via `/health`.
- **`flatten`** — close every open position, reduce-only, then re-read to verify flat.
- **`demo <id>`** — force one strategy back to the sandbox.
- **`pause-symbol <sym>`** — hard-block new submissions for one symbol (closes still pass).

## Shared safety rules

- **UNKNOWN, never "flat."** Any failed or degraded read reports UNKNOWN — a command never assumes a position is closed.
- **Mutations require preview + `yes`.** No silent writes.
- **`/kr-close` is reduce-only and sizeless.** It never routes through `/webhook`.
- **No implicit demo→live.** Every live transition needs an explicit confirmation.
- **Sizing is owned by the execution layer.** A command never sets or suggests a size.

`[Screenshot: Telegram running /kr-emergency-stop flatten with the dry-run preview and confirmation]`

**Next:** [Quick Command Reference →](#page-13-quick-command-reference)

---

# Page 13: Quick Command Reference

**Every command and control in one place.** Read-only commands are safe to run any time; mutating commands dry-run and require confirmation.

## Operator commands

| Command | Type | Example |
|---|---|---|
| `/kr-status` | read | `/kr-status` |
| `/kr-positions` | read | `/kr-positions` |
| `/kr-strategy-list` | read | `/kr-strategy-list` |
| `/kr-trace <symbol\|time>` | read | `/kr-trace BTCUSDT` |
| `/kr-tv-alerts <symbol>` | read | `/kr-tv-alerts SOLUSDT` |
| `/kr-strategy-mode <id> <pause\|resume\|demo\|live>` | mutate | `/kr-strategy-mode btcusdt_kinetic_flow_2h demo` |
| `/kr-close <symbol>` | mutate | `/kr-close BTCUSDT` |
| `/kr-emergency-stop <kill\|flatten\|demo <id>\|pause-symbol <sym>>` | mutate | `/kr-emergency-stop flatten` |
| `/kr-restart [force]` | mutate | `/kr-restart` |
| `/kr-upgrade [--no-pull\|--no-tests\|--no-ui]` | mutate | `/kr-upgrade` |
| `/kr-exchange <list\|status\|add\|update\|remove> ...` | mutate | `/kr-exchange add okx --demo` |
| `/kr-telegram <status\|setup\|rotate\|allow\|revoke\|...>` | mutate | `/kr-telegram allow 987654321` |

## API endpoints

| Route | Purpose |
|---|---|
| `POST /webhook` | Receive a TradingView alert. |
| `GET /api` | Full dashboard model (strategies, P&L, positions, health). |
| `GET /health` | Service + arming status (kill switch, live-trading, armed). |
| `GET /api/signals?n=50&symbol=BTCUSDT` | Last *n* execution events. |
| `POST /api/control/strategy/<id>` | Set mode (`pause`/`demo`/`live`/`clear`) or `accounting_start_at`. |
| `POST /api/control/trading-state` | Set `active` / `reducing`. |
| `POST /api/close` | Operator close (reduce-only, sizeless). |

## Key environment variables

| Variable | Purpose |
|---|---|
| `KAIRO_SECRET` | Shared secret for the webhook and dashboard. |
| `KAIRO_LIVE_TRADING` | Global kill switch — must be `true` for any real-money order. |
| `KAIRO_MAX_NOTIONAL_USD` | Global absolute notional ceiling. |
| `KAIRO_DASHBOARD_PORT` | Dashboard port (default `8098`). |
| `KAIRO_DATA_DIR` | Where durable state and ledgers live. |
| `KAIRO_REQUIRE_HMAC` | Require HMAC-signed webhooks (needs a signing proxy). |

*(Legacy builds may present these with the `HERMX_` prefix — they are equivalent.)*

## Quick test

```bash
curl -sS -X POST "https://<your-host>/webhook" \
  -H "Content-Type: application/json" \
  -d '{"strategy_id":"solusdt_kinetic_flow_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview","secret_key":"'"$KAIRO_SECRET"'"}'
```

**Next:** [Glossary →](#page-14-glossary)

---

# Page 14: Glossary

**Key terms used across KAIRO documentation.**

| Term | Definition |
|---|---|
| **KAIRO** | The AI trading copilot and execution layer described here (internal codename: HERMX). |
| **Kinetic Flow** | The signal/indicator and market-regime intelligence brand KAIRO executes. Also the risk dashboard the copilot can consult for regime context. |
| **Strategy** | One JSON file in `strategies/` defining instrument, timeframe, budget, leverage, and execution mode. |
| **`strategy_id`** | The unique ID an alert uses to select which strategy runs. |
| **Alert** | The JSON message TradingView sends to `/webhook`. Carries only the signal — never venue, sizing, or mode. |
| **Action** | The direction field: `buy`, `sell`, or `close`. |
| **Execution mode** | `demo` (sandbox/paper) or `live` (real money). Only `live` is real money, and it also needs the kill switch released. |
| **Seed** (`budget_usd`) | The starting capital assigned to a strategy. Fixed until the file is edited. |
| **Equity** | Seed + realized net P&L. What sizing uses when compounding is on. |
| **Reinvest / compounding** | `capital.reinvest`; when on (default), each trade sizes off equity, not the fixed seed. |
| **Notional** | The leveraged order size (`sizing × leverage`). |
| **Notional ceiling** | An absolute, operator-set cap that refuses an oversized order pre-trade. |
| **Kill switch** | `KAIRO_LIVE_TRADING` — the global gate that must be `true` for any real-money order. |
| **Trading state** | Global `active` or `reducing`. In `reducing`, new opens are blocked but closes always pass. |
| **Symbol pause** | A per-symbol hard block on new submissions (closes still pass). |
| **Equity stop** | Blocks new opens for a compounding strategy whose equity is ≤ 0; closes always pass. |
| **Signal decision** | The per-signal outcome: TRADE, SKIP, DUPLICATE, or BLOCKED. |
| **Order state** | The order lifecycle: PLANNED → SUBMITTED → FILLED / REJECTED / UNKNOWN. |
| **UNKNOWN** | An unconfirmed order outcome. KAIRO never fabricates a rejection; reconciliation resolves it. |
| **Reconciliation** | Confirming the real venue state after submission to resolve UNKNOWN and record P&L. |
| **WAL (write-ahead log)** | The durable record of every accepted alert; the recovery source after a restart. |
| **Dedupe** | Rejecting a repeated signal within a 24-hour window so it never executes twice. |
| **Accounting window** | An optional per-strategy reset point that scopes displayed P&L without deleting history. |
| **The copilot** | KAIRO's always-on AI assistant — reads state, answers questions, and (progressively) guards the desk. |
| **Progressive autonomy** | The copilot's phased authority: advisory → gated (veto) → conviction-scored → discretionary → proactive. |
| **Fail open / fail closed** | Safety gates fail closed (block the order); intelligence layers fail open (proceed) so a broken AI can't wedge the desk. |
| **Regime** | Market-state context (from the Kinetic Flow risk dashboard) the copilot can use to veto a signal. |

---

## For Featurebase Admins

This single file is designed to be split into **14 separate Featurebase articles** — one per top-level `# Page N:` heading. When importing:

1. **Each `# Page N:` heading becomes its own article.** Use the slug listed in the Table of Contents (e.g. `getting-started`, `connecting-tradingview`, `command-reference`, `glossary`) as the article URL slug.
2. **Drop the `# Page N:` prefix** from each article title — keep just the descriptive part (e.g. "Getting Started", "The Dashboard").
3. **Suggested collection order** matches the numbering above: Getting Started → How KAIRO Works → Strategies → Connecting TradingView → Alert Contract → Budget & Sizing → Safety Gates → States → Dashboard → AI Copilot → Monitoring → Operator Commands → Command Reference → Glossary.
4. **The four H2s on Page 1** (*How It Works: The Basics*, *Why Automation?*, *Who KAIRO Is For*, *Key Limitations*) can be kept as in-page anchors within the single "Getting Started" article, or split into separate short articles under a *Getting Started* collection if you prefer one-topic-per-page granularity.
5. **Convert the "Next: →" links** at the bottom of each page into Featurebase's related-article links, or delete them if your theme auto-generates next/previous navigation.
6. **Replace every `[Screenshot: ...]` placeholder** with an actual image before publishing. Recommended captures: dashboard overview, strategy cards, TradingView alert dialog, arming banner, a Telegram copilot exchange, and an emergency-stop confirmation.
7. **Recommended grouping into collections:** *Getting Started* (primarily page 1, optionally pages 2–5), *Trading Model* (pages 6–8), *Operating KAIRO* (pages 9–13), *Reference* (page 14). The Table of Contents and this admin note can be dropped from the published set or kept as a hidden internal index.
