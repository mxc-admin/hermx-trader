# Execution Strategy Design — Signal-Timing Refinement, Side Restriction, Capital Circuit Breaker, External Risk Veto, Message-Driven SL/TP (2026-07-04)

> Design exploration for nine operator-proposed features:
> `side` restriction, `buy-execution` (retrace + delay), `sell-execution`
> (top-wick + delay), `max-loss` circuit breaker, `risk-engine-check` external
> veto, the strategy-as-execution-loop model, lower-timeframe execution
> optimization, and message-driven stop-loss / take-profit (`action: sl` /
> `action: tp` — the only two that touch the *alert* contract rather than the
> strategy file). Grounded in the current tree (post `225a9103`, REFACTOR_PLAN
> Phase 4) and a competitive scan of TradersPost + 3Commas.
>
> **Scope note:** this document remains a SEPARATE planning thread from
> `docs/NAUTILUS_TRADER_COMPARISON.md` / `docs/NAUTILUS_GAP_REMEDIATION_PLAN.md`
> (protective SL/TP via venue-native conditional orders). Two deliberate
> overlaps only: §5, where the execution-loop mechanism is genuinely shared,
> and #8/#9, which target the same *capability* (protective SL/TP) through a
> deliberately cheaper message-driven mechanism — §5.6 contrasts the two and
> reframes the venue-native approach as an optional later hardening layer, not
> a prerequisite. Do not merge the two threads.
>
> **Design doc only. No code or schema changes ship with this document.**
> Line numbers verified 2026-07-04; re-locate by symbol before editing.

---

## 1. Problem framing — jobs to be done

HermX today: one TradingView alert → gate chain → one immediate market order
(or close+open reversal pair) → journal → P&L ledger. Direction, timing, and
price all come from the alert instant. Ideas #1–7 all refine *when, whether,
and at what price* that order is placed — not *what* the strategy trades.
Ideas #8–9 instead extend *what an alert can say*: two new `action` values
(`sl`, `tp`) that let the Pine script itself report "my stop / my target just
tripped — flatten", riding the existing close path.

| # | Feature | JTBD |
|---|---------|------|
| 1 | `side: long-only \| long-short` | "When I've only validated a strategy's long side, let me guarantee it never opens a short — regardless of what the Pine script emits." |
| 2 | `buy-execution`: `buy-retrace: x%`, `buy-delay: x` | "Signals print at momentum extremes; let me enter x% below the signal price on the pullback, and/or wait x (minutes or bars) to confirm the move is real before committing." |
| 3 | `sell-execution`: `sell-top-wick: x%`, `sell-delay: x` | "Exits also print into momentum; let me capture the wick spike above the signal price instead of market-selling into the mid, and/or delay the exit to avoid reacting to a one-tick fakeout." |
| 4 | `max-loss` | "If a strategy loses x% of its capital, stop it automatically and make me — a human — review it before it can trade again. Never auto-resume." |
| 5 | `risk-engine-check: na \| api` | "Before committing capital, ask an external regime/risk service (e.g. the MXC Kinetic Flow dashboard) for a go/no-go. If the service is down or disabled, trade as normal (fail-open)." |
| 6 | Strategy = execution loop | "The strategy file should describe an *execution behavior*, not just routing metadata — an alert should arm a local stateful loop that manages entry/exit timing, the way a Nautilus `Strategy` actor lives below its data feed." |
| 7 | Lower-timeframe execution | "My strategy decides on 1h–4h bars, but execution quality is decided in the minutes after the signal. That optimization is architecturally impossible in TradingView and must live in HermX." |
| 8 | `stop_loss` (`action: sl`) | "When price crosses my stop level, get me flat — without HermX having to watch price or attach a venue-side conditional order. My Pine script already knows the level; let it just *tell* HermX." |
| 9 | `take_profit` (`action: tp`) | "Same as #8 for the profit target: when my target trades, flatten (v1) and record that the close was a *win-taking* exit, not a stop-out or a manual flatten." |

**Why #7 cannot live in Pine (the constraint driving #2/#3/#6):** a TradingView
alert fires exactly once, at evaluation of the strategy's own bar (bar close for
non-repainting configs). After `alert()`/an order-fill alert fires, the script
has no execution context until the *next* bar of its own timeframe — Pine cannot
poll intrabar price after emitting an alert, cannot sleep-then-recheck, and
cannot send a "now actually execute" follow-up mid-bar. A 1h strategy is
therefore blind for up to an hour exactly when retrace/wick conditions play out.
The only place a finer-grained post-signal watcher can exist is the receiving
side: HermX must track a finer price feed for a bounded window after signal
receipt and evaluate retrace/wick/delay before submitting.

**Why #8/#9 nonetheless CAN live in Pine (no contradiction):** the #7
constraint is about *refining the execution of an alert that already fired* —
Pine cannot follow up on its own signal. An SL/TP cross is not a follow-up; it
is a **new, independent condition** that Pine evaluates natively on every
realtime bar update (`alertcondition` / `strategy.exit`-style logic) and that
fires its own distinct alert when it trips. Pine is the price watcher; HermX
just receives one more message through the exact same intake path as today's
`action: close`. That is the operator's steer: send SL/TP *as messages*, not
build them as HermX-side price infrastructure.

---

## 2. Competitive scan

TradersPost is the closest product-shape comparator (webhook → broker relay
with per-strategy execution settings). All TradersPost field names below were
confirmed against its live per-field reference pages
(https://traderspost.io/reference, 2026-07-04). 3Commas' **Stop Loss Timeout**
triangulates the delay/wick concept
(https://help.3commas.io/en/articles/3108977-smarttrade-dca-bots-how-stop-loss-works).

| HermX proposed | TradersPost equivalent | 3Commas equivalent | What HermX should do differently |
|---|---|---|---|
| `side: long-only\|long-short` | `Allowed sides` (`Both Sides`/`Long`/`Short`) + `Sides isolated`. **Documented loophole: reverse actions bypass Allowed sides.** | Long/Short bot type is fixed per bot | HermX reversals are first-class (`CLOSE_OPPOSITE_IF_ANY` + `OPEN_*`, `readiness.py:124`), so the side gate must decide reversals explicitly — allow the close leg, block the open leg — not inherit TradersPost's bypass. Enforce as a ledgered skip (`gate` field, `service.py:131`), never a silent drop. |
| `buy_execution.buy_retrace_pct` | `Entry trail amount` / `Entry trail percent` — a *trailing* entry stop that follows market before triggering (webhook `trailAmount`/`trailPercent`) | — | TradersPost trails (dynamic); the proposed `buy_retrace_pct` is a *fixed* offset off `tv_signal_price` — simpler, restart-provable state (one target price vs a continuously-updated trail). Watcher must be venue-agnostic CCXT (multi-venue futures), leverage-aware, WAL-journaled so a restart re-arms it. |
| `buy_execution.buy_delay` | `Reject entry if signal is older than` (1–30 s, measured from webhook `time` else receive time, **plus any configured Delay**; per-signal `rejectAfter` override) + `Cancel open entry order after delay` (1–3600 s) | — | TradersPost splits *staleness rejection* from *pending-order cancellation*; HermX should keep the same split: `tv_time`-bounded freshness already exists at intake (never `received_at`), delay/cancel is new pending-intent state. Note TradersPost's staleness clock includes the delay — §8 Q2. |
| `sell_execution.sell_top_wick_pct` | `Exit trail amount` / `Exit trail percent` (trailing exit stop) | — | Same fixed-offset-vs-trail distinction as entry. Wick capture ≈ limit above signal price + bounded fill-wait + mandatory market fallback. Never applies to *operator* closes — emergency flatten stays immediate market (§5). |
| `sell_execution.sell_delay` | `Reject exit if signal is older than` + `Cancel open exit order after delay` + `Exit order fill wait time` (60–300 s, default 120 s; unfilled exit ⇒ the trade fails and the follow-on entry is not submitted) | **Stop Loss Timeout**: SL triggers a countdown (seconds); position closes only if price *stays* at/below SL for the full timeout; recovery resets the counter. Purpose: filter "temporary dips/wicks/fake-outs". | 3Commas' timeout is the strongest prior art that *delay-as-noise-filter* is a real shipped feature; HermX generalizes it from SL-only to any exit signal. 3Commas also documents the failure mode: too long a timeout + a *limit* stop lets price gap past the level and skip the fill — which is why §4 makes the market fallback after `fill_wait_seconds` mandatory. |
| `max_loss.pct` | `Stop loss amount` / `Stop loss percent` / `Stop loss PnL amount` — all **per-trade**, computed off the entry price/quantity, never off strategy capital | Deal-level SL, also per-trade | The per-strategy-*capital* breaker is HermX's differentiator: it needs the submit-time attribution map (`pnl_strategy_map`) + `aggregate_strategy_pnl`, which relay products don't have. Manual-resume-only is stricter than both vendors. |
| `action: sl` (#8) | Same per-trade `Stop loss amount` / `Stop loss percent` / `Stop loss PnL amount` fields — but TradersPost *implements* them as **broker-side bracket/exit orders** attached at entry | Deal-level SL, broker/exchange-side | Architecturally different mechanism for the same job: HermX's stop is a **Pine-originated webhook message**, not a resting broker-side order. Much cheaper to ship (an enum value + the existing close path, §3.7/§5.6) — but it inherits webhook-delivery risk (TradingView → network → receiver) that a broker-side bracket doesn't have. Same delivery dependency as every other HermX signal today; no better, no worse. |
| `action: tp` (#9) | `Take profit amount` / `Take profit percent` / `Take profit PnL amount` — likewise broker-side bracket orders | Deal-level TP, broker/exchange-side | Same message-vs-bracket trade-off as `action: sl`. One extra gap the bracket products don't have: partial take-profit sizing — HermX v1 ships `tp` as full-close-only (§8 Q8). |
| `risk_engine_check` | — (no external go/no-go hook) | — | Genuinely novel among relay products. HermX already has the exact seam: the Phase 8 advisor veto (§3.4), subprocess-based today, fail-open, journaled. |
| *(context)* sizing fields | `Risk percent` / `Risk dollar amount` (qty = risk ÷ \|entry − stop\|; both *require* a stop loss) | — | HermX equivalent is `capital.budget_usd` + A1 `max_notional_usd` ceiling — already shipped. Risk-based sizing stays out of scope: the `action: sl` message (#8) arrives when the stop *trips*, not at entry time, so it cannot inform entry sizing the way TradersPost's entry-attached stop does. |

TradersPost shipped exactly the staleness half of #2/#3 in its **May 2, 2025**
release: "prevent trades from executing if a signal is too old by configuring a
maximum age (in seconds) for both entries and exits … TradersPost will ignore
signals that exceed this threshold **after any configured delay**", with a
per-signal `rejectAfter` (1–30 s) webhook override
(https://docs.traderspost.io/releases; wording corroborated from indexed
snapshots — the dated page itself no longer resolves — and from the live
`no-entry-reject-after-exceeded` rejection-message reference). The takeaway for
HermX: *max-age and delay interact and must be specified together* (§8 Q2).

---

## 3. Current architecture gaps

### 3.1 Headline: the pause/close trap — **already fixed** (correction to the planning brief)

The brief for this document described pausing a strategy as also blocking the
operator's close. That was true historically, but **commit `a8e5c29f`
(2026-06-29, "simplify execution modes to Pause/Demo/Live three-pill model")
fixed it.** Verified mechanism, current tree:

- Signal path: pause sets `submit_orders=False` → `live_execution_enabled=False`
  (`src/strategy/readiness.py:53-68`). Gate 1 (`strategy_submit_flag`,
  `src/execution/service.py:138-155`) blocks on this *before* the `close_only`
  bypasses (kill switch `:163-186`, symbol pause `:188-196`) run.
- **But closes do not use the signal path.** Both `/api/close` and webhook
  `action=close` (`_build_close_record`, `src/webhook_receiver.py:1890`, call
  at `:2008`) route through `execute_operator_close` (`:1811`) →
  `build_operator_close_readiness` (`:1760`), which since `a8e5c29f`
  **hardcodes `submit_orders = True`** (`:1774`) and deliberately ignores the
  control-state override's `submit_orders` flag (it honors only
  `execution_mode`, `:1770-1772`). So `live_execution_enabled=True` (`:1783`)
  and Gate 1 passes for every close, paused or not.

Net effect: **a paused strategy can be flattened.** The precondition the brief
set for `max-loss` ("a circuit breaker must not trap the operator unable to
flatten the position that tripped it") is already satisfied: max-loss can
reuse the existing pause mechanism as-is, and closes will keep working.

Remaining design obligations this fact imposes on the new features:

- **Every new gate in this document must preserve the property.** `max_loss`,
  `risk_engine_check`, `side`, and the timing loops must never be evaluated on
  a `close_only` readiness record — either check `readiness.get("close_only")`
  (the `service.py:177/:192` pattern) or live only in the open-signal
  readiness builder (`build_strategy_execution_readiness`), which closes never
  traverse.
- **Belt-and-braces for max-loss:** even though pause no longer blocks closes,
  the breaker should still offer `on_trip: pause | close_then_pause` — firing a
  forced `execute_operator_close` *before* pausing removes the position risk
  entirely rather than leaving it to the operator (§8, Q5 for the default).
- **UI:** `dashboard-ui/components/StrategyCard.tsx:17,31` renders the plain
  three-pill `Pause/Demo/Live`. A max-loss trip must render as a *distinct*
  tripped state (not a normal Pause pill) — it wasn't operator-set, and the
  resume affordance must be an explicit "reviewed & resume" action, not a mode
  toggle.

### 3.2 No side gate today

`schemas/strategy.schema.json` `strategy_v2` (`:106-185`) has **no
side/direction field**: `schema_version, strategy_id, name,
instrument{exchange,inst_id,type}, timeframe, indicator,
capital{budget_usd,reinvest,max_notional_usd}, leverage, margin_mode,
execution_mode, submit_orders, notes` — nothing else
(`additionalProperties:false`). Direction is derived purely from the alert:
`direction = "long" if normalized.get("side") == "buy" else "short"`
(`src/strategy/readiness.py:70`). Idea #1 is a pure gap, not a rework.

Reversal note (agrees with §2's `side` row and §4's field description): every
open signal's intent is the two-leg `["CLOSE_OPPOSITE_IF_ANY", "OPEN_*"]` pair
(`readiness.py:124`, with separate per-leg clOrdIds at `:76-78`), so the side
gate acts **per-leg** — a disallowed-direction signal keeps its close leg and
drops only the `OPEN_*` action, letting a long-only strategy still exit via a
short reversal signal while long.

### 3.3 No capital-loss tracking in the gate chain

Per-strategy realized P&L *exists* — `aggregate_strategy_pnl`
(`src/pnl_ledger.py:415`) over `closed-trades.jsonl`, attributed via the
submit-time map (`pnl_strategy_map`) — but **nothing in the gate chain reads
it**. There is no drawdown state, no peak-equity tracking, no per-strategy loss
counter anywhere in `src/execution/` or `src/strategy/`.

### 3.4 No outbound HTTP anywhere in the execution gate chain

Verified: `grep -rE "requests|httpx|urllib|aiohttp" src/execution src/strategy
src/advisor.py` → zero hits in the gate chain; `src/webhook_receiver.py`'s
only matches are a `urlparse` import used for request-*path* parsing in the
HTTP handler (`:22-23` — no outbound calls) and the words "webhook requests"
inside log strings (`:2540,:2542`). The closest existing mechanism is the
**Phase 8 pre-execution advisor**, extracted from the receiver to
`src/advisor.py` in REFACTOR_PLAN Phase 6 (extraction marker at
`src/webhook_receiver.py:1881-1884`):
`run_execution_advisor`/`execute_with_advisor` (`src/advisor.py:141-195`) —
consult → optional veto (`vetoed_by_advisor`) → journal to `pipeline.jsonl`
stage `advisor` → **fail-open on any error** ("a down/slow/garbage LLM can
never block a sanctioned trade", `src/advisor.py:145`). It is
subprocess-based (`hermes -z`, `src/advisor.py:102`), disabled by default
(`engine-config.json` `advisor.enabled=false`, `timeout_seconds=30`).
`risk_engine_check` should generalize this seam with an HTTP transport, not
add a parallel gate (§5.4).

### 3.5 No price feed

The only market-data read in the tree is a one-shot `fetch_ticker` for a
reference price at submit time (`src/executors/ccxt_adapter.py:594`). Ideas
#2/#3/#7 require a bounded post-signal poller — net-new infrastructure (§5.3).

### 3.6 Reusable control-state patterns (don't invent parallel mechanisms)

- `symbol_pauses` dict: `{paused, paused_at, reason}` per key
  (`src/control_state.py:108-139`) — the shape for max-loss trip records.
- `trading_state: active|reducing` global gate (`:251-283`) — precedent for
  "blocks opens, allows closes".
- `strategy_overrides` three-pill flags (`:142-172`) — the pause mechanism
  max-loss reuses.
- **Gotcha (code-quality rule):** every new `control-state.json` key
  (`max_loss_trips`, `risk_engine_state`) MUST be added to
  `default_control_state()` (`:50-55` region) or the merge filter silently
  drops it on next load.

### 3.7 No `sl`/`tp` action — but the gap is an enum value, not a subsystem

The alert contract knows exactly three actions:
`schemas/tradingview-alert.schema.json:38-41` enums `action` as
`["buy", "sell", "close"]`. There is no way today for a Pine script to say
"my stop tripped" — the only expressible close is a generic `close`.

Unlike gaps 3.2–3.5, closing this one requires **no new machinery**. The
branch point already exists: `build_record` routes
`normalized.get("action") == "close"` to `_build_close_record`
(`src/webhook_receiver.py:2007`, current working tree) *before* the
`ALLOWED_SIDES` gate, because a close carries no `side` and only reduces risk
(comment block at `:2004-2006`; same rationale in `_build_close_record`'s
docstring, `:1891-1894`). `sl`/`tp` take the same or a sibling branch with the
identical bypass — a stop-out or take-profit, like a close, can only reduce
risk, never open a position — and route through the same
`execute_operator_close` path (`close_only=True`, bypasses kill switch +
symbol pause). Additive enum extension + branch condition; smaller than
anything else in §3. An `sl`/`tp` arriving for a strategy with NO open
position is a journaled no-op, never an error — the adapter's existing
reduce-only close behavior already skips the side that isn't open
(`build_operator_close_readiness` emits both close legs; the adapter closes
whichever is open, `webhook_receiver.py:1799-1801`).

---

## 4. Proposed schema v3 additions

Style-consistent with v2: `additionalProperties:false`, optional blocks whose
absence means "today's behavior", descriptions in the
`capital.max_notional_usd` register. All five strategy-file additions are
optional — `schema_version: 3` is v2 + these. (#8/#9 need a sixth addition to
a *different* contract — the alert schema, not the strategy schema — see the
end of this section.)

```jsonc
"side": {
  "type": "string",
  "enum": ["long-only", "long-short"],
  "default": "long-short",
  "description": "Directions this strategy may act on. 'long-only': a sell/short signal keeps its CLOSE_OPPOSITE_IF_ANY leg (flattens an existing long) but has its OPEN_SHORT action STRIPPED from execution_intent.actions before submission (ledgered, not rejected — mode='side_restricted'); the disallowed direction is never opened, and closes/exits always execute. Mirrored for 'short-only' if ever added. Default 'long-short' = today's behavior."
},

"buy_execution": {
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "buy_retrace_pct": {
      "type": "number", "exclusiveMinimum": 0, "maximum": 50,
      "description": "Arm the entry and wait for price to pull back this % below tv_signal_price before submitting. Requires the post-signal watcher (§5). Unset => immediate submission."
    },
    "buy_delay": {
      "type": "number", "exclusiveMinimum": 0,
      "description": "Wait this long after signal receipt before submitting the entry (confirmation delay). Units per buy_delay_basis."
    },
    "buy_delay_basis": {
      "type": "string", "enum": ["minutes", "bars"], "default": "minutes",
      "description": "'minutes' = wall-clock from received_at; 'bars' = N bars of the strategy timeframe (30m–4h per the timeframe enum), evaluated at bar boundaries. Open question §8 Q1."
    },
    "on_timeout": {
      "type": "string", "enum": ["cancel", "submit_market"], "default": "cancel",
      "description": "What happens when buy_retrace_pct never fills within the watch window: 'cancel' skips the trade (ledgered mode='retrace_timeout'); 'submit_market' chases. §8 Q2."
    },
    "timeout_minutes": {
      "type": "number", "exclusiveMinimum": 0,
      "description": "Watch-window bound for retrace/delay. Default: one bar of the strategy timeframe. The window is ALWAYS bounded — an armed intent must never outlive its signal's relevance (freshness is bounded on tv_time, never server time)."
    }
  }
},

"sell_execution": {
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "sell_top_wick_pct": {
      "type": "number", "exclusiveMinimum": 0, "maximum": 50,
      "description": "On an exit signal, place a limit this % ABOVE tv_signal_price (long exit; mirrored below for short) to capture the wick spike, with a bounded fill-wait, then fall back to market. Never applies to operator closes."
    },
    "sell_delay": { "type": "number", "exclusiveMinimum": 0, "description": "As buy_delay, for exits." },
    "sell_delay_basis": { "type": "string", "enum": ["minutes", "bars"], "default": "minutes" },
    "fill_wait_seconds": {
      "type": "number", "exclusiveMinimum": 0, "default": 30,
      "description": "How long the wick-capture limit may rest unfilled before the market fallback fires (TradersPost 'Exit order fill wait time' analogue). The fallback is mandatory: an exit signal must ALWAYS end in a flat position."
    }
  }
},

"max_loss": {
  "type": "object",
  "additionalProperties": false,
  "required": ["pct"],
  "properties": {
    "pct": {
      "type": "number", "exclusiveMinimum": 0, "maximum": 100,
      "description": "Trip the breaker when the strategy's cumulative realized loss reaches this % of the denominator. Computed from aggregate_strategy_pnl (closed-trades.jsonl via the submit-time attribution map) — realized only in v1 (§8 Q4)."
    },
    "denominator": {
      "type": "string", "enum": ["budget_usd", "current_equity", "peak_equity"],
      "default": "budget_usd",
      "description": "What 'capital' means. budget_usd = static file value (simple, recommended v1); peak_equity = true drawdown (needs equity tracking). §8 Q3."
    },
    "requires_manual_resume": {
      "type": "boolean", "const": true,
      "description": "Always true in v3 — a tripped breaker NEVER auto-resumes; the operator must explicitly review-and-resume via the dashboard. Declared as const so a future auto-resume is a deliberate schema change, not a config edit."
    },
    "on_trip": {
      "type": "string", "enum": ["pause", "close_then_pause"], "default": "pause",
      "description": "'pause' sets the strategy pause override (closes still work — see §3.1). 'close_then_pause' first fires execute_operator_close, then pauses. §8 Q5."
    }
  }
},

"risk_engine_check": {
  "type": "object",
  "additionalProperties": false,
  "required": ["mode"],
  "properties": {
    "mode": {
      "type": "string", "enum": ["na", "api"], "default": "na",
      "description": "'na' = disabled (today's behavior). 'api' = call url for a go/no-go before each OPEN submission (never closes). Follows the Phase 8 advisor's fail-open posture."
    },
    "url": {
      "type": "string", "format": "uri",
      "description": "External risk endpoint, e.g. an MXC Kinetic Flow dashboard route exposing regime/risk-state. Required when mode='api'. Vendor-agnostic: any endpoint honoring the response contract (§8 Q6)."
    },
    "timeout_s": {
      "type": "number", "exclusiveMinimum": 0, "maximum": 10, "default": 2.0,
      "description": "Hard HTTP timeout. Kept small — this sits on the money path; a slow risk engine must not become a de-facto delay gate."
    },
    "fail_mode": {
      "type": "string", "enum": ["open", "closed"], "default": "open",
      "description": "'open' (default): timeout/error/non-2xx/unparseable => proceed, anomaly journaled (log-and-continue, consistent with the advisor). 'closed': treat failure as no-go — only for operators who prefer missing trades to unassessed ones."
    }
  }
}
```

Validation notes: `mode:"api"` without `url` must fail schema validation
(`if/then` on `mode`). Loader-side: every consumer of these blocks must treat
*absent block* and *mode:na* identically (masked-default lesson from the
shadow-config regression).

**The sixth addition lives in the ALERT schema, not the strategy schema.**
Everything above is a strategy-*file* field
(`schemas/strategy.schema.json`); #8/#9 are the first — and only — change in
this document to the alert contract, `schemas/tradingview-alert.schema.json`.
It is a pure additive enum extension, same file, same style:

```jsonc
// schemas/tradingview-alert.schema.json:38-41, current:
"action": { "type": "string", "enum": ["buy", "sell", "close"] }
// becomes:
"action": { "type": "string", "enum": ["buy", "sell", "close", "sl", "tp"] }
```

Existing alerts remain valid byte-for-byte (additive enum). Worth stating
because the two contracts have different blast radii: a strategy-schema change
is gated by `schema_version` and touches only files the operator edits; the
alert schema is what every deployed Pine script emits against, and its
enforcement posture is currently observe-only by default
(`enforce_alert_schema`, §3.7's cited intake code) — so shipping the enum
*before* any Pine script emits `sl`/`tp` is free, and the rollout order is
schema first, Pine second. No `side` field accompanies `sl`/`tp` (same as
`close`); v1 carries no sizing field either — full close only (§8 Q8).

---

## 5. Execution loop architecture

### 5.1 The conceptual shift (idea #6)

Today the pipeline is stateless per signal: intake → WAL fsync
(`raw-webhooks.jsonl`) → `PROCESS_QUEUE` → dequeue → dedupe ledger →
`build_strategy_execution_readiness` → `ExecutionService.execute` → market
order. The Nautilus model this borrows from (briefly — see the comparison doc
for the full mapping) is the `Strategy`/`Actor`: a stateful object living
below the data feed with `on_bar`/`on_order_filled`-style hooks, so *deciding*
and *executing* are separate events. HermX's version:

> **An alert no longer submits — it ARMS.** Arming creates a durable
> *pending intent*; a watcher then manages entry/exit timing
> (retrace/wick/delay) against a finer feed and performs the actual submission
> when conditions are met (or the window expires).

When a strategy has no `buy_execution`/`sell_execution` block, arming and
submitting are the same instant — the current one-shot path is the degenerate
case, byte-identical behavior.

### 5.2 Where it lives

- **Not `webhook_receiver.py` intake** — the receiver must return 200 fast;
  its job ends at the WAL fsync + queue put.
- **Not inside `ExecutionService.execute`** — that is a synchronous gate chain;
  blocking it for minutes would serialize the queue consumer.
- **New module `src/execution/pending.py`** (Phase-N extraction style, like
  `src/orders/journal.py`): owns the pending-intent store and the watcher
  loop. The queue consumer, after readiness is built and the strategy has a
  timing block, writes the ARMED intent and returns; a single per-process
  watcher thread evaluates all armed intents.

### 5.3 Per-pending-intent state and the watcher

Each armed intent (one JSONL row, atomic-append, same fsync posture as the WAL):

| Field | Source / semantics |
|---|---|
| `signal_id`, `received_at` | join keys back to intake (existing conventions) |
| `cl_ord_id` (open + close legs) | **computed at ARM time** via `stable_client_order_id` (`src/signals/dedupe.py`) — the eventual submit reuses it, so a crash between arm and submit cannot double-submit after replay |
| `armed_at`, `deadline` | absolute timestamps; `deadline = armed_at + timeout`. Replay after an outage evaluates expiry against these, never against wall-clock-now (the `normalize()` replay lesson) |
| `signal_price` | `tv_signal_price` from the alert (`readiness.py:118`) |
| `target_price` | derived: entry `signal_price × (1 − buy_retrace_pct/100)`; exit wick `signal_price × (1 + sell_top_wick_pct/100)` (mirrored for shorts) |
| `kind` | `entry_retrace` \| `entry_delay` \| `exit_wick` \| `exit_delay` |
| `readiness` snapshot | the full readiness record, so submission needs no re-derivation |
| `state` | `ARMED → SUBMITTED \| EXPIRED_CANCELLED \| EXPIRED_MARKET \| CANCELLED_SUPERSEDED` — a pre-submit extension of the order-journal state machine (`src/orders/journal.py`), not a parallel one |

Watcher mechanics:

- Single thread, coarse tick (2–5 s), polling `fetch_ticker` **only for
  instruments with armed intents** (delay-only intents need no price at all —
  they are pure timers, which is why delay ships before retrace, §6).
- **Restart-safe:** systemd restarts are routine (`Restart=always`/5 s). On
  startup, the watcher replays the pending-intents file exactly like the WAL
  replay: re-arm live intents, expire dead ones by their recorded `deadline`,
  and let the dedupe ledger + deterministic `cl_ord_id` guarantee no
  double-submission.
- **Gates run at SUBMIT time, not arm time.** The armed intent goes through
  the full `ExecutionService.execute` chain when it fires — so a kill switch
  flipped, a symbol paused, or a max-loss tripped *during* the wait is
  honored. Arming performs only the cheap fatal checks (schema, dedupe, side).
- **Close-class events invalidate armed intents.** Any close-class event for a
  strategy+symbol — `action: close`/`sl`/`tp`, an operator close, or a
  `max_loss` trip — must synchronously cancel all ARMED intents for that
  strategy+symbol before (or as part of) executing the close, transitioning
  them to `CANCELLED_SUPERSEDED` and journaling the reason. Otherwise a
  delayed/retrace entry could fire *after* the flatten and reopen an
  unprotected position.
- **Operator closes never arm.** `build_operator_close_readiness` /
  `execute_operator_close` bypass the watcher entirely — emergency flatten is
  an immediate market order, always. Only *signal* exits get `sell_execution`
  timing.

### 5.4 `risk_engine_check` insertion point

Generalize the Phase 8 advisor seam rather than adding a new gate:
`execute_with_advisor` (`src/advisor.py:175-195`) already implements
consult → veto → journal → fail-open. Add an HTTP transport alongside the
subprocess transport: `run_execution_advisor` grows a sibling
`run_risk_engine_check(readiness, cfg)` in `src/advisor.py` sharing the
decision-dict shape
(`veto_applied`, `latency_ms`, `error`) and the `pipeline.jsonl` journaling
(new stage `risk_gate`). It runs at **submit time** (so armed intents get a
fresh answer, not a stale arm-time one) and is skipped for `close_only`
records. First `requests`/`httpx` dependency on the money path — keep it to
one call site, one timeout, no retries (a retry is a delay gate in disguise).

### 5.5 `max_loss` insertion point

A pure read-side gate + trip writer, no feed needed:

1. At open-signal submit time, compute cumulative realized P&L for the
   strategy via `aggregate_strategy_pnl` (`src/pnl_ledger.py:415`).
2. If loss ≥ `pct` of denominator: write
   `control-state.json.max_loss_trips[sid] = {tripped: true, tripped_at,
   reason, pnl_usd, threshold_usd}` (the `symbol_pauses` shape,
   `control_state.py:108-139`) **and** set the existing pause override
   (`set_strategy_override(sid, "pause")`, `:157-172`). Optionally fire
   `execute_operator_close` first (`on_trip`).
3. Closes keep working (§3.1). Resume = an explicit dashboard action that
   clears the trip record *and* the override in one atomic control-state
   write; the trip record's presence is what the UI renders as the distinct
   tripped state.
4. `max_loss_trips` must be added to `default_control_state()` or the merge
   filter drops it (§3.6 gotcha).

### 5.6 Pine-driven SL/TP (`action: sl`/`tp`) vs venue-native conditional orders

The one part of this section #8/#9 deliberately do NOT use. Two ways to get a
protective stop/target:

| | **Message-driven (this doc, #8/#9)** | **Venue-native (the Nautilus docs' thread)** |
|---|---|---|
| Price watcher | TradingView's own Pine runtime, evaluating the SL/TP cross on every realtime bar update | HermX price-feed poller, or an exchange-side algo/conditional order attached per venue |
| New HermX infrastructure | **None of §5.** No pending-intent module, no watcher thread, no price feed, no per-venue conditional-order capability matrix. Intake reuses the `action=close` branch and `execute_operator_close` nearly verbatim (§3.7). | All of the above, plus a per-venue capability/semantics matrix for conditional-order types — the large remediation plan in `docs/NAUTILUS_GAP_REMEDIATION_PLAN.md` |
| Survives a TradingView / webhook-delivery outage | **No.** If the alert never arrives, the position stays open. Exactly the delivery-dependency every HermX signal has today — entries, exits, and closes included. No better, no worse. | **Yes** — the protective order rests at the venue and fires even if HermX and TradingView are both dark. That outage-independence is the entire value proposition, and the entire cost. |
| Cost to ship | S (enum value + branch + tests) | L–XL (separate planning thread) |

**Recommendation: ship the message-driven version first, as the default
mechanism.** It delivers the core capability — positions get stopped out and
targets get taken — at near-zero cost, with restart-safety, dedupe, WAL
durability, and the never-block-a-close invariant all inherited from the
existing close path rather than rebuilt. Venue-native conditional orders are
then an optional **later hardening layer** for the operator who wants
protection that survives delivery outages — a reliability upgrade on top of a
shipped feature, not a prerequisite for having stops at all. (The
`sell_top_wick_pct` exit-timing machinery in §5.3 is orthogonal: it refines
*how* a signal exit executes; `sl`/`tp` add *new reasons* to exit. An
`action: sl` should NEVER get `sell_execution` timing treatment — like an
operator close, a stop-out is an emergency and goes straight to market.)

---

## 6. Prioritized roadmap

Value = money-risk reduced or execution quality gained; Cost = new
infrastructure required. Dependencies explicit. The pause/close fix the brief
listed as a gating dependency is **already shipped** (`a8e5c29f`, §3.1), so
`max-loss` is unblocked today.

| Rank | Item | Value | Cost | Depends on |
|---|---|---|---|---|
| 1 | `side` restriction (#1) | High (hard safety guarantee) | S — one schema field + one skip gate in the readiness builder | — |
| 2 | `action: sl` / `action: tp` (#8/#9) | High (first protective-exit vocabulary at all; Pine does the watching) | S — additive alert-schema enum + a sibling of the `action=close` branch; reuses `_build_close_record` / `execute_operator_close` almost verbatim (§3.7, §5.6) | — (**no** dependency on the pending-intent/watcher items 5–7; ships independently and early) |
| 3 | `max_loss` breaker (#4) | High (bounds worst case per strategy) | M — read-side gate + trip state + UI tripped-state/resume flow | P&L attribution (shipped: `pnl_strategy_map`); pause mechanism (shipped, close-safe) |
| 4 | `risk_engine_check` (#5) | Med-High (external regime veto) | M — HTTP transport on the advisor seam + audit journaling | Advisor seam (shipped, Phase 8); decision-audit ledger (suggestion B) |
| 5 | `buy_delay` / `sell_delay`, minutes basis (#2b/#3b) | Medium (noise filter; 3Commas-validated concept) | M — pending-intent journal + watcher, **no price feed** (pure timers) | §5 pending module |
| 6 | `buy_retrace_pct` (#2a) | Medium (entry-price improvement) | L — everything in 5 plus the ticker poller | 5 |
| 7 | `sell_top_wick_pct` (#3a) | Medium (exit-price improvement; hardest semantics) | L — poller + limit-then-market-fallback lifecycle | 6 (poller), fill-wait/fallback design |
| 8 | Full strategy-loop model (#6/#7) | — (architecture umbrella) | delivered incrementally as 5→6→7, never big-bang | — |
| 9 | Suggestion A: post-trip cooldown | Medium | S once 3 exists | 3 |
| 10 | Suggestion C: timing dry-run mode | Medium (de-risks 6/7 tuning) | M | 5 (watcher journals) |
| 11 | Suggestion B: risk-decision audit ledger | shipped *with* 4, not after | S | 4 |
| — | Venue-native SL/TP (Nautilus docs' thread) | Reliability hardening over 2 (survives delivery outages) | L–XL — tracked in `docs/NAUTILUS_GAP_REMEDIATION_PLAN.md`, not here | 2 shipped first (§5.6: upgrade, not prerequisite) |

Sequencing rationale: 1–4 are pure gate-chain / intake work with no new feed
and deliver the safety features — and 2 is the smallest item on the board (an
enum value and a branch) for the largest single risk reduction (positions get
stops at all); 5 builds the pending-intent machinery on timers alone
(restart-safety provable without price-feed nondeterminism); 6–7 add the
poller onto proven machinery.

---

## 7. Additional suggested features

- **A. Per-strategy cooldown after a losing close** (`cooldown: {minutes|bars,
  after: loss|max_loss_trip}`). JTBD: "after a stop-out in chop, don't let the
  strategy immediately re-enter the same saw." Complements `max_loss` (breaker
  = catastrophic stop; cooldown = tactical damper) and reuses the same trip
  plumbing with an auto-expiring window instead of manual resume.
- **B. Risk-decision audit ledger for `risk_engine_check`.** Every call
  journaled: url, latency, raw response, decision, and — critically — every
  fail-open event. JTBD: "prove the risk gate is actually consulted and see
  what it said when it mattered." Direct application of the 'an inert monitor
  is worse than no monitor' rule: a silently-always-failing-open gate is false
  reassurance, so fail-open events must be visible, counted, and alertable via
  the existing Hermes cron gate library.
- **C. Dry-run (shadow) mode for timing parameters** (`buy_execution.dry_run:
  true`). The watcher evaluates retrace/wick/delay and journals
  *would-have-submitted-at* price/time next to the actual immediate execution,
  without changing live behavior. JTBD: "tune `x%` against reality before
  arming it with money." Gives an empirical basis for §8 Q1/Q2 answers instead
  of guessing.

---

## 8. Open questions for the operator

1. **Delay basis — wall-clock minutes vs strategy bars?** `minutes` is simpler
   and restart-provable; `bars` (of the 30m–4h strategy timeframe) aligns with
   how the strategy "thinks" but needs bar-boundary alignment from the feed.
   Recommendation: ship `minutes` first, keep `*_delay_basis` in the schema so
   `bars` is additive. Which do you actually want to reason in?
2. **Retrace timeout behavior:** if price never retraces `buy_retrace_pct`
   within the window — cancel the trade (miss it) or submit at market (chase)?
   Proposed default `cancel`; note the interaction TradersPost documents
   (max-age must account for any configured delay).
3. **`max_loss` denominator:** `budget_usd` (static, simple, recommended v1) vs
   current equity (tracks `reinvest:true` compounding) vs peak equity (true
   drawdown, needs equity high-water tracking)?
4. **`max_loss` scope:** realized-only (from `closed-trades.jsonl`, cheap and
   exact) or realized+unrealized (needs a mark-price poll of open positions —
   couples the breaker to the price feed)? Proposed v1: realized-only.
5. **`on_trip` default:** plain `pause` (position left open for your decision —
   closes work, §3.1) or `close_then_pause` (auto-flatten, then pause)?
6. **`risk_engine_check` response contract:** exact JSON the URL must return —
   proposal: `{"decision": "go"|"no-go", "reason": str, "as_of": iso8601}`,
   with anything else = parse failure → `fail_mode`. Does the MXC Kinetic Flow
   dashboard (https://mxc-kinetic-crypto.replit.app/) already expose a route
   shaped like this, or do we adapt to its existing shape? Auth (header token)?
7. **Wick-capture mechanics for `sell_top_wick_pct`:** resting limit above
   signal price with `fill_wait_seconds` then market fallback (proposed — one
   venue round-trip, works on every CCXT venue), or watch-the-high-then-trigger
   locally (more control, more poller coupling)?
8. **Partial take-profit sizing (#9):** `action=close` today always fully
   flattens — `execute_operator_close` closes whichever side is open,
   full reduce-only, and the alert carries no quantity. A `tp` that should
   scale out (e.g. "take half at target 1") needs either (a) a
   `size_pct`/`qty` field on the alert — a schema addition that needs a home
   (extras bag vs first-class field) *and* a partial-reduce execution path
   that doesn't exist yet, or (b) v1 ships `tp` as full-close-only, identical
   to `sl`, with partial-TP staging as a later additive schema change.
   **Proposed: (b)** — it keeps #8/#9 at the "enum + branch" cost that makes
   them rank 2, and partial sizing stays additive. Confirm full-close-only is
   acceptable for v1.
9. **Should `sl`/`tp` closes be distinguishable in the ledger/journal?** All
   three (`close`, `sl`, `tp`) reduce to the same `execute_operator_close`
   call, so post-hoc analysis can't tell a stop-out from a target hit from a
   manual flatten unless the reason is recorded at intake. Proposal: thread a
   `kind` (e.g. `sl_close` / `tp_close` vs today's `operator_close`) into the
   journal/pipeline records — write-side tag only, no behavior difference —
   so "why did this position close?" is answerable from `closed-trades.jsonl`
   joins later. Cheap now, unrecoverable retroactively (same lesson as the
   submit-time attribution map: record it at the moment it's known).
