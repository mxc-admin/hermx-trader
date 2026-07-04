# HermX ⇄ Nautilus Trader — Robustness Gap Analysis (2026-07-03)

> Compares HermX's execution flow (`docs/HERMX_TRADING_EXECUTION_FLOW.md`, cited `file:line`) against
> Nautilus Trader (`nautechsystems/nautilus_trader`) architecture. Goal: find the top robustness/reliability
> improvements worth adopting **at HermX's scale** — TradingView-alert-driven, ~1 strategy per symbol,
> market orders, a handful of CCXT venues, single worker, systemd `Restart=always`, local dashboard.
>
> Ruthlessness rule applied: an item is only a "gap" if Nautilus's approach is *genuinely safer/more correct
> for a production money system at HermX's scale* — not merely fancier. Rejected-as-not-a-gap items are
> listed in §B so the reasoning is auditable. Analysis only — no code was changed.

---

## A. What HermX already gets right (do NOT churn these)

Before the gaps: several things HermX does are *already* Nautilus-grade and must not be "upgraded" into regressions.

- **Strict order-state transition table.** `_ORDER_STATE_TRANSITIONS` (`webhook_receiver.py:1535-1542`) + `order_state_can_transition` (fail-closed on unknown edges) is exactly Nautilus's `_ORDER_STATE_TABLE` → `FiniteStateMachine` pattern, just with a smaller state set.
- **Event-sourced, checkpointed order journal.** `order-journal.jsonl` (append-only, write-ahead PLANNED/SUBMITTED fsync'd before the venue call, checkpoint-fold every 1000 records) is a JSONL analogue of Nautilus's event-stream + Cache-DB. Correct at HermX's volume; a Redis/Postgres Cache would be over-engineering.
- **Idempotency.** `duplicate_cl_ord_id` journal gate (`service.py:175-183`) + signal dedupe (`check_and_mark_signal`, `:767`) mirror Nautilus's `ClientOrderId` immutability + `trade_id` fill dedup.
- **Ambiguity → UNKNOWN, never auto-REJECTED** (`invariant`, `service.py:253-262`). Conservative and correct for a low-volume operator-in-loop system. Do not adopt Nautilus's "in-flight → REJECTED after N retries" (§B).
- **Reconcile is observe-only** — never auto-cancels/auto-corrects. Nautilus's `generate_missing_orders` auto-submits position-alignment orders; that is *too aggressive* for HermX (§B).

---

## B. Nautilus patterns deliberately REJECTED (not gaps at HermX's scale)

| Nautilus pattern | Why rejected for HermX |
|---|---|
| **MessageBus pub/sub (Redis streams)** | HermX is webhook → WAL → single worker → execute. Direct call path is correct and debuggable at 1 worker / ~1 strategy-per-symbol. A bus adds indirection with zero reliability gain here. |
| **Injectable `TestClock`/`LiveClock` for backtest parity** | HermX is **live-only**; there is no backtest engine to keep in parity. `now_iso()` ad-hoc time is fine. Freshness is already correctly bounded on `tv_time`, not server time. Minor test-determinism value only. |
| **In-flight order → auto-`REJECTED` after retries** | HermX deliberately pauses the symbol + alerts the operator instead (`:2453-2618`). For a money system at low order volume, "stop and page a human" is *safer* than auto-rejecting an order that may have filled under eventual consistency. |
| **Auto-correcting reconciliation orders (`generate_missing_orders`)** | HermX's reconcile is observe-only by design. Auto-submitting orders to force venue↔internal agreement is a foot-gun HermX correctly avoids. |
| **Redis/Postgres Cache + Parquet catalog** | JSONL WAL + checkpoint already gives crash-safe recovery at HermX's data volume. External DB is operational overhead without a reliability win here. |
| **Fill dedup on `trade_id`** | HermX reconstructs P&L from **per-order** history rows (aggregate `avg_px`/`accFillSz`), deduped on `ord_id` — correct at order-row granularity; partial-in-progress rows are already skipped (`_NON_TERMINAL_ORDER_STATES`, `pnl_ledger.py:62-64`). Trade-id granularity buys nothing here. |
| **9-variant `OrderType` / TIF matrix, emulated/triggered orders** | HermX submits market orders only (`order_type default "market"`). STOP/TRAILING/emulated states are unused surface. |

---

## C. Section-by-section comparison (HermX §2–§12 vs Nautilus)

### §2 Strategy Config → Nautilus Instrument/Strategy + Cache
- **HermX:** strategy JSON (`schemas/strategy.schema.json`), `instrument.exchange` selects venue; `capital.budget_usd`, `leverage`; per-strategy `execution_mode`. Instrument step/min read from the live CCXT market at submit (`_market_spec`, `ccxt_adapter.py:586`).
- **Nautilus:** first-class `Instrument` objects (`price_increment`, `size_increment`, `lot_size`, `min/max_quantity`, `min/max_notional`) held in the `Cache`; these feed `RiskEngine` pre-trade checks.
- **Gap:** HermX reads step/min but **never validates the order against `min/max_notional` or `min_quantity` before submit** — a sub-min or over-max order goes to the venue and comes back a reject → UNKNOWN churn. → **Opp 1**.

### §3 Inbound Alert → Nautilus data/command ingestion
- **HermX:** durable WAL (`raw-webhooks.jsonl`) fsync before queue, signal dedupe (`signals.jsonl`), startup replay. This is *stronger* intake durability than a stock Nautilus data feed (which relies on the venue/catalog). **No gap; HermX wins.**
- The only intake-adjacent gap is **rate**: HermX rate-limits by client IP (transport), not per-strategy order rate. Nautilus's `RiskEngine` `max_order_submit_rate` throttler is per-trading-path. → **Opp 8** (low priority; dedupe + no-pyramid already bound it).

### §4 Execution Readiness → Nautilus RiskEngine (pre-trade)
- **HermX:** readiness computes `live_execution_enabled`, mode, `cl_ord_id`, `planned_notional_usd`. The doc's own §4.2 note: **"no explicit budget-remaining check or position-state pre-check."** Gates enforced later are arming/mode/kill-switch/symbol-pause/idempotency — **none of them size- or balance-aware.**
- **Nautilus:** `RiskEngine._check_orders_risk()` denies on `NOTIONAL_EXCEEDS_MAX_PER_ORDER`, `NOTIONAL_LESS_THAN_MIN_FOR_INSTRUMENT`, `NOTIONAL_EXCEEDS_FREE_BALANCE`, `MARGIN_EXCEEDS_FREE_BALANCE`, quantity precision/bounds — *before* the order reaches the client, emitting a typed `OrderDenied`.
- **Gap (largest):** a fat-fingered `budget_usd`/`leverage` or a bad reinvest calc sends the full oversized notional to a **real venue** with zero pre-trade guard. → **Opp 1**.
- **Sub-gap:** no global `TradingState` (`HALTED`/`REDUCING`). HermX's kill switch is binary and *live-venue-only* (demo still submits); `manual_pause` is legacy/unconsulted. → **Opp 2**.

### §5 Order Execution → Nautilus ExecutionEngine + order lifecycle
- **HermX:** `PLANNED → SUBMITTED → {FILLED | REJECTED | UNKNOWN}`. No `ACCEPTED` (venue-ack-but-resting), no first-class `PARTIALLY_FILLED` (collapses into `FILLED`; partial-in-progress rows skipped in the ledger), no `CANCELED`/`EXPIRED`, no modify/cancel command path.
- **Nautilus:** 14-state `OrderStatus` with `PartiallyFilled` self-loop and `Accepted`; `Order` is a fold over `OrderInitialized/Submitted/Accepted/Filled/...` events; overfill guard (`check_for_potential_overfills`).
- **Gap:** market-order-only makes `ACCEPTED` low-value, but a formal `PARTIALLY_FILLED` order state would let HermX *track and update* a partial to terminal instead of skipping it. Medium value. → **Opp 7**. Overfill check → **Opp 10** (low).

### §6 Receiver Reconciliation → Nautilus fill reconciliation
- **HermX:** three paths (startup, post-submit, periodic resolver) all reconcile **only orders already in HermX's own journal** (`load_open_orders`). There is **no venue-authoritative sweep** — HermX never asks "what open orders/positions does the venue *actually* hold?" independent of its journal.
- **Nautilus:** `LiveExecutionEngine` on startup + continuously calls `generate_order_status_reports()`, `generate_fill_reports()`, **`generate_position_status_reports()`**, matches vs Cache, and synthesizes inferred events; unknown orders become first-class *external orders*; position discrepancies are flagged.
- **Gap:** HermX is **blind to position/exposure drift** — an external/manual fill, a fee mismodel, or an unattributed close silently diverges its belief from the venue with no detection. → **Opp 3** (position-drift alert), **Opp 5** (external fills into the ledger). Reconcile-interface typing → **Opp 9**.

### §7 P&L Ledger → Nautilus Portfolio/Position (event-sourced)
- **HermX:** `reconcile_from_order_history` **recomputes** a signed running position by replaying the last ~100 order-history rows *every dashboard build (~10s)* and flags closes by `reduceOnly` or position-delta sign flip (`QTY_EPS`). Documented fragilities: M5 partial-fill/sign-flip, 100-row `history_window_ageout`.
- **Nautilus:** `Position` is a persisted aggregate folded from `OrderFilled` events (`avg_px_open/close`, `signed_qty`, explicit flip-split into `PositionClosed`+`PositionOpened`, per-fill realized PnL). Never recomputed from a bounded window.
- **Gap:** recompute-from-window is fragile and window-bounded; a persisted, incrementally-folded position would be authoritative and window-independent. → **Opp 6**.

### §8 P&L Aggregation → Nautilus Portfolio reporting
- **HermX:** `equity_now_usd = budget_usd + closed_net + open_upl` — a **synthetic, budget-anchored** figure never reconciled to the venue's actual balance. Balance *is* fetched for health display but not cross-checked.
- **Nautilus:** `Account` tracks real `balances`/`margins` via `AccountState` events; `Portfolio.equity() = balances_total + Σ unrealized_pnl` from venue truth.
- **Gap:** HermX's computed equity can silently drift from real account equity (missed fill, fee error, unattributed close) with no alarm. → **Opp 4**.

### §10 Exception Flows → Nautilus error handling
- **HermX:** strong here — fail-closed state writes (`_fail_closed_state_write`), UNKNOWN-not-REJECTED, symbol-pause-on-stuck, red-acted secrets, best-effort P&L never blocks the trade. Matches Nautilus's component-FSM `DEGRADED`/`FAULTED` posture in spirit. **Mostly no gap.** The one addition worth it is a per-strategy submit-rate throttle (Opp 8) as an order-storm backstop.

### §12 Data Files → Nautilus persistence
- **HermX:** JSONL WAL + checkpoint + `control-state.json`. Crash-safe at volume. **No gap** (Redis/Postgres/Parquet rejected in §B). The persistence gap that *does* matter is semantic, not storage: there is **no persisted position/account state object** (only order journal + recomputed P&L) — covered by Opp 4 & Opp 6.

---

## D. Top 10 Opportunities (ranked by impact × confidence ÷ effort)

## Opportunity 1: Pre-trade risk gate (notional cap + min-notional + affordability)
**Nautilus pattern:** `RiskEngine` denies an order *before* it reaches the execution client on `NOTIONAL_EXCEEDS_MAX_PER_ORDER`, `NOTIONAL_LESS_THAN_MIN_FOR_INSTRUMENT`, `NOTIONAL_EXCEEDS_FREE_BALANCE`/`MARGIN_EXCEEDS_FREE_BALANCE`, emitting a typed `OrderDenied`.
**HermX gap:** readiness computes `planned_notional_usd` (`:1450-1451`) but **no gate checks it.** The service gate stack (`service.py:100-183`) is arming/mode/kill-switch/pause/idempotency only — never size- or balance-aware. A fat-fingered `budget_usd`, wrong `leverage`, or bad reinvest sends the full notional to a **real venue**; a sub-min order goes out and bounces to UNKNOWN.
**Impact:** an oversized live order from a one-character config typo is the highest-severity uncaught event in the whole flow — real money, no guard. Secondary: sub-min/over-max/unaffordable orders each become an UNKNOWN that pauses the symbol and pages the operator (§6.4) — so the missing check also drives operator toil.
**Effort:** S — a new pure gate between idempotency and the write-ahead in `ExecutionService.execute`, reading `readiness.planned_notional_usd` vs a per-strategy `max_notional_usd` (config) + venue `min/max_notional` from `_market_spec`, plus an optional free-balance check (balance already fetched for health). Fail closed → `not_submitted/notional_out_of_bounds`, no journal write.
**Recommended approach:** add `max_notional_usd` to `strategy.schema.json` (optional; default = `budget_usd × leverage × safety_factor`), and a `_check_notional(readiness)` gate that also enforces the instrument min. Keep it a *pure* function like the other gates; do not pull in an account/margin model yet (that's Opp 4).
**Confidence:** 0.9

## Opportunity 2: Global REDUCING / HALT trading state
**Nautilus pattern:** `set_trading_state(ACTIVE | HALTED | REDUCING)` on the `RiskEngine`. `HALTED` denies all new submissions (cancels/closes pass); `REDUCING` accepts only exposure-reducing orders across every instrument.
**HermX gap:** controls are (a) `HERMX_LIVE_TRADING` — binary, **live-venue-only** (demo keeps submitting), and (b) per-symbol pause. There is **no single "block all new opens, keep letting closes through, on every symbol including demo"** control. `manual_pause` exists in `control-state.json` but is explicitly *not consulted by the execution path*. An operator wanting global risk-off today must pause every symbol individually.
**Impact:** in a fast-moving adverse market or an incident, the operator cannot flip the whole system to reduce-only in one action; the kill switch either does too little (leaves demo on / doesn't block opens specifically) or the operator races per-symbol pauses. `close_only` bypass logic *already exists* in the gate — this is the missing global toggle that reuses it.
**Effort:** S — add `trading_state ∈ {active, halted, reducing}` to `control-state.json`, read it in the service gate; `halted` blocks any non-close; `reducing` blocks OPEN legs but allows CLOSE (the existing `close_only`/`reduceOnly` machinery already distinguishes them).
**Recommended approach:** one new gate above the symbol-pause gate, consulting `load_control_state()["trading_state"]`; wire a dashboard toggle + `/hx-emergency-stop` to set `reducing`/`halted`. Reuses the close-bypass semantics already in `service.py:130-158`.
**Confidence:** 0.85

## Opportunity 3: Venue position-drift detection & alert
**Nautilus pattern:** `LiveExecutionEngine` calls `generate_position_status_reports()` per venue on startup and continuously, compares net position per (account, instrument) against the venue at instrument precision, and flags/reconciles any discrepancy.
**HermX gap:** all three reconcile paths iterate **only HermX's own journal orders** (`load_open_orders`). Nothing ever compares HermX's believed open position against the venue's *reported* position. The P&L reconcile reads order history but recomputes P&L, not an authoritative "do we agree on the current position" check.
**Impact:** HermX can be silently wrong about its live exposure — a missed/mis-attributed fill, a manual venue action, or a partial-fill mismodel leaves belief and reality diverged with **no alarm**; risk decisions (and the no-pyramid guard) then operate on a false position.
**Effort:** M — add a periodic (reuse `unknown_resolver_loop` cadence) `fetch_positions()` read per active `(venue, mode)`, compare vs the position implied by the order journal / last snapshot, and emit a `POSITION_DRIFT` reconcile alert (observe-only; do **not** auto-correct — §B).
**Recommended approach:** a `reconcile_positions_once()` alongside `resolve_unknown_orders_once()`; the CCXT adapter already exposes positions via `health()`. Alert + `pause_symbol` on divergence beyond a size epsilon; never auto-trade.
**Confidence:** 0.85

## Opportunity 4: Account equity/balance reconciliation vs venue
**Nautilus pattern:** `Account` tracks real `balances`/`margins` from `AccountState` events; `Portfolio.equity() = balances_total + Σ unrealized_pnl` — anchored to venue truth.
**HermX gap:** `equity_now_usd = budget_usd + closed_net + open_upl` (`pnl_ledger.py`, §8.2) is **synthetic and budget-anchored**, never cross-checked against the actual venue balance (which is already fetched for the health panel).
**Impact:** the displayed equity/P&L can silently drift from the real account balance — a missed fill, wrong `ORDER_PNL_IS_NET`, fee mismodel, or unattributed close accumulates undetected. For a money dashboard, "the number is wrong and nobody knows" is a serious reliability defect.
**Effort:** M — periodically compute `expected_equity = budget + closed_net + open_upl` and compare to venue `balance` from the executor health read; emit an `EQUITY_DRIFT` alert past a tolerance. Read-only; does not change accounting.
**Recommended approach:** fold a drift check into the dashboard model build (balance is already in `strategy_live_snapshot`); surface `equity_drift_usd` in `reconcile_health` and alert when `|drift| > tolerance`. Ship as an observability signal first, not an auto-adjust.
**Confidence:** 0.8

## Opportunity 5: External / manual fills first-class in the P&L ledger
**Nautilus pattern:** an order/fill the system didn't originate becomes a first-class *external order* (routed to a claiming strategy or the `EXTERNAL` id) so it still updates positions and PnL.
**HermX gap:** `reconcile_from_order_history` only writes closes where `is_hermx_cl_ord_id` is true (`mxc…`/`operator_close_…`/resolvable HL cloid, `pnl_ledger.py:121-144`). A **purely manual venue close** (no HermX cl_ord_id) is *excluded* from the ledger → realized P&L is incomplete after any out-of-band operator action on the exchange.
**Impact:** operators do occasionally flatten directly on the exchange during incidents (the exact scenario the kill switch anticipates). Those closes never hit `closed-trades.jsonl`, so lifetime realized P&L understates reality and the ledger silently disagrees with the account.
**Effort:** M — when position-delta detects a close with no HermX-attributable cl_ord_id, write it with `strategy_id=None` + `source="external"` (the ledger already tolerates unattributed rows) instead of dropping it. Gate behind a flag to preserve current attribution semantics.
**Recommended approach:** relax the `is_hermx_cl_ord_id` drop for *position-reducing* rows only; tag `attribution="external"`; keep them out of per-strategy sums but include in a portfolio/account-level total so the ledger matches the venue.
**Confidence:** 0.75

## Opportunity 6: Persisted, event-sourced position aggregate
**Nautilus pattern:** `Position` is a persisted aggregate folded from `OrderFilled` events — `avg_px_open/close`, `signed_qty`, explicit flip-split (`PositionClosed`+`PositionOpened`), per-fill realized PnL — never recomputed from a bounded window.
**HermX gap:** `reconcile_from_order_history` **recomputes** signed position by replaying the last ~100 order-history rows every ~10s dashboard build, detecting closes via `reduceOnly` or a `QTY_EPS` sign flip. Documented fragilities: M5 partial-fill/sign-flip ambiguity and the 100-row `history_window_ageout`.
**Impact:** a busy instrument can push earlier fills out of the 100-row window (age-out already needs an alert to paper over it); a sign-flip on a partial is a warning-but-still-treated-as-close. Both can mis-detect closes → wrong realized P&L. A persisted position folded incrementally from journal fills removes both failure modes.
**Effort:** L — introduce a `positions.jsonl` (or a `position` section in the journal) folded from terminal FILLED order events, with flip-split; the dashboard reads the aggregate instead of re-replaying history. Larger blast radius (touches the P&L core) → sequence it *after* Opps 1–5.
**Recommended approach:** incrementally fold FILLED journal transitions into a per-`(venue,mode,inst_id)` position record (mirroring Nautilus flip-split); keep order-history reconcile as a periodic *audit* against it (feeds Opp 3), not the primary source. Respect Principle 10 — never prune.
**Confidence:** 0.7

## Opportunity 7: Formal PARTIALLY_FILLED (and ACCEPTED) order-journal states
**Nautilus pattern:** `PARTIALLY_FILLED` is a real state with a self-loop for successive fills; `ACCEPTED` distinguishes venue-acknowledged-and-resting from filled.
**HermX gap:** the journal collapses partials into `FILLED` and skips partial-in-progress ledger rows (`_NON_TERMINAL_ORDER_STATES`). There is no state that says "resting on the venue, partially done, still working."
**Impact:** medium and mostly latent because HermX submits market orders (fast, usually full fills). But when a market order *does* partial-and-rest (thin book), HermX can only skip-or-collapse; it can't represent "60% filled, tracking the rest." Adds correctness headroom for non-instant fills.
**Effort:** M — add `PARTIALLY_FILLED` to `_ORDER_STATE_TRANSITIONS` (self-loop + → FILLED/REJECTED/UNKNOWN) and map `partially_filled` there instead of straight to FILLED. `ACCEPTED` is optional/low-value for market-only.
**Recommended approach:** extend the transition table + `map_order_outcome` (`:1986-2026`); low risk since the table is already the single source of truth. Defer `ACCEPTED` until limit orders exist.
**Confidence:** 0.65

## Opportunity 8: Per-strategy order submit-rate throttle
**Nautilus pattern:** `RiskEngine` token-bucket `Throttler` denies orders over `max_order_submit_rate` ("N/interval") with `Exceeded MAX_ORDER_SUBMIT_RATE`.
**HermX gap:** rate limiting is HTTP-transport (120/60s per IP), not per-strategy order rate. Signal dedupe + no-pyramid bound normal flow, but a strategy legitimately flipping long/short each bar in a fast market, or an alert-config mistake, could still fire real orders faster than intended.
**Impact:** low-to-medium — HermX's dedupe (86400s window) and no-pyramid guard already make a true storm unlikely; this is cheap insurance, not a hole. Included for completeness, ranked accordingly.
**Effort:** S — a per-`strategy_id` min-interval / token bucket checked in the service gate; deny → `not_submitted/rate_limited`.
**Recommended approach:** small in-memory per-strategy last-submit timestamp + configurable `min_submit_interval_s`; alert on trip. Do not build a general throttler framework.
**Confidence:** 0.6

## Opportunity 9: Typed reconciliation report interface (adapter ↔ engine)
**Nautilus pattern:** adapters return typed `OrderStatusReport` / `FillReport` / `PositionStatusReport`; the engine owns all truth-from-venue logic, so adapters don't hand-roll per-venue race handling.
**HermX gap:** `map_order_outcome` (`:1986-2026`) and the P&L reconcile parse **raw CCXT dicts** inline, per venue, at the call site. Venue-specific field quirks (OKX string `reduceOnly`, per-venue `pnl` field names) leak into reconcile/ledger logic.
**Impact:** medium — each new venue means more inline dict-shape handling scattered across reconcile and ledger, a recurring source of the "Bybit realized P&L is None" / field-name-drift class of bugs (§13). A thin typed report struct at the adapter boundary localizes venue quirks.
**Effort:** M — introduce a small `OrderStatusReport`/`FillReport` dataclass the adapter fills (normalizing `reduceOnly`, `realized_pnl`, `status`), consumed by `map_order_outcome` + `_build_entry`. No engine rewrite — just a normalization seam.
**Recommended approach:** wrap the existing `_normalized_realized_pnl` / `_normalize_reduce_only` helpers into a single per-row `to_fill_report()` on the adapter; callers stop touching raw `info`. Incremental, venue-by-venue.
**Confidence:** 0.65

## Opportunity 10: Overfill / fill-vs-ordered quantity check
**Nautilus pattern:** `check_for_potential_overfills()` compares `filled_qty + last_qty` against original quantity; `allow_overfills` gates reject-vs-warn with tracked `overfill_qty`.
**HermX gap:** HermX trusts the venue's reported fill quantity; it never asserts `accFillSz ≤ ordered`. A venue bug or mis-parse that reports more filled than ordered would silently corrupt position/P&L math.
**Impact:** low — rare, and market-order-only limits exposure — but it's a cheap invariant assertion that catches a whole class of venue/parse corruption early.
**Effort:** S — in `map_order_outcome`/reconcile, assert reported filled ≤ ordered (within step epsilon); on violation, clamp + emit a `RECONCILE_MISMATCH` (`overfill`) alert rather than trusting the number.
**Recommended approach:** one guard in the fill-summary path; reuse the existing `RECONCILE_MISMATCH` alert kind. Warn-and-clamp (don't hard-fail) to match HermX's observe-only posture.
**Confidence:** 0.6

---

## E. Summary ranking

| # | Opportunity | Nautilus anchor | Effort | Conf | Priority |
|---|---|---|---|---|---|
| 1 | Pre-trade risk gate (notional cap + min-notional + affordability) | `RiskEngine._check_orders_risk` / `OrderDenied` | S | 0.90 | **Highest** |
| 2 | Global REDUCING / HALT trading state | `set_trading_state(HALTED\|REDUCING)` | S | 0.85 | High |
| 3 | Venue position-drift detection & alert | `generate_position_status_reports` | M | 0.85 | High |
| 4 | Account equity/balance reconciliation | `Account` / `Portfolio.equity()` | M | 0.80 | High |
| 5 | External/manual fills first-class in ledger | external-order claim system | M | 0.75 | Medium |
| 6 | Persisted event-sourced position aggregate | `Position` fold + flip-split | L | 0.70 | Medium |
| 7 | Formal PARTIALLY_FILLED / ACCEPTED states | 14-state `OrderStatus` | M | 0.65 | Medium |
| 8 | Per-strategy order submit-rate throttle | `Throttler` / `max_order_submit_rate` | S | 0.60 | Low |
| 9 | Typed reconciliation report interface | `OrderStatusReport`/`FillReport` | M | 0.65 | Low |
| 10 | Overfill / fill-vs-ordered quantity check | `check_for_potential_overfills` | S | 0.60 | Low |

**Sequencing:** ship **1 → 2** first (both small, both close real-money holes with pure gates in the existing stack). Then **3 → 4 → 5** (venue-truth reconciliation & accounting integrity, observe-only). Defer **6** (P&L-core blast radius) until 1–5 are in. **7–10** are correctness headroom, adopt opportunistically.

**Cross-cutting theme:** HermX's *order-lifecycle* machinery already matches Nautilus (strict FSM, event-sourced journal, idempotency, conservative UNKNOWN). Every high-value gap is on the **risk (pre-trade sizing/state) and reconciliation (venue-truth for position/balance)** axes — the two places Nautilus centralizes "truth from the venue" and HermX currently trusts its own computed state.

---
*End of analysis.*
