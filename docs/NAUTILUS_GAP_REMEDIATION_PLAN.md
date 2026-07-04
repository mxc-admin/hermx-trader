# Nautilus Gap Remediation Plan — Validated (2026-07-04)

> Ruthless re-validation of the "Top gaps" in `docs/NAUTILUS_TRADER_COMPARISON.md`.
> Every claim was re-derived from the current tree (HEAD `be3c0c66`) by independent
> code reads — citations were NOT trusted. Baseline: the 7 relevant test suites
> (`test_phase_b_robustness`, `test_order_state_machine`, `test_pnl_ledger`,
> `test_unknown_resolver_controls`, `test_reconciliation_observe_only`,
> `test_ccxt_adapter`, `test_pnl_attribution`) — **203 pass / 0 fail** on this tree.
>
> ⚠ **Line-number volatility:** an uncommitted concurrent refactor is in flight
> (`src/control_state.py` extraction, −289 lines from `src/webhook_receiver.py`).
> This plan cites **symbols**, with HEAD line numbers as hints only. Per the
> project's own rule ("Re-read current code before executing a planned change"),
> every implementer MUST re-locate by symbol before editing.

**Scorecard: of the comparison doc's 6 items, 2 survive largely intact (with
corrections), 1 survives only as a rescoped smaller feature, and 3 are rejected
as written.** Several "minimal fixes" were premised on code that does not exist.

---

## 1. Validation log

Confidence scale: (a) citation accuracy, (b) gap still open, (c) fix genuinely
minimal as proposed, (d) claimed importance justified.

### Gap 1 — Pre-trade balance-sufficiency + instrument bounds (claimed Critical)

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Citation accurate | **Mostly.** `_check_pretrade_risk` (`service.py:23-50`), Gate 4 (`:198-215`), floor-to-zero (`ccxt_adapter.py:626-628` in `_contracts_for_notional`), `get_balance_summary` (`ccxt_adapter.py:1130-1152`) all exact. **But "silently floors to qty 0 with no operator-visible reason" is STALE**: the A1b mitigation (`service.py:374-390`) already emits a WARNING + `emit_reconcile_alert(stage="under_execution", reason="zero_size_below_min")` when all legs floor to zero, pinned by `tests/test_phase_a_robustness.py:238,254`. | 0.9 |
| (b) Still open | **Balance check: fully open** — zero balance-related code on the submit path (grep-clean in `service.py`/`webhook_receiver.py`); `check_balance_drift` is post-hoc observe-only. **Sub-min surfacing: partially closed** by A1b; open remainder = no distinct `below_instrument_min` reason (sub-min, no-price, and zero-notional all collapse to `zero_size`) and ccxt `limits.cost.min` (min-notional) is never consulted — only `limits.amount.min`. | 0.95 |
| (c) Fix minimal as written | **No — four landmines the one-liner ignores:** (1) a generic "before `create_order`" check would also gate the reduce-only close leg → violates never-block-a-close, and would false-block the open leg of a reversal (margin freed by the close leg); it must be scoped to the OPEN branch only. (2) demo sandbox balances are arbitrary (`get_balance_summary` docstring) → an unconditional check spuriously blocks demo; must be live-only. (3) settlement currency must come from `market["settle"]` (inverse swaps settle in base), not the hardcoded `"USDT"` default. (4) a new `mode="insufficient_balance"` that isn't explicitly mapped in the service's outcome handling could fall into the UNKNOWN path → symbol pause, **worse** than today's clean venue reject. Also: the write-ahead journal has already recorded SUBMITTED by adapter time, so the outcome must be a legal `SUBMITTED→REJECTED` transition. | 0.85 |
| (d) Importance justified | **Inflated: High, not Critical.** By the doc's own rubric, Critical = "can lose or misaccount real money, or double-submit". An underfunded submit is discovered as a venue reject → REJECTED (clean) or UNKNOWN churn + pause — noise and degraded operator control, not money loss or misaccounting. The nastiest real case (reversal: close leg fills, open leg venue-rejected → flat instead of reversed) is exactly "materially degrades correctness/operator control" = High. Additionally the deployment is currently demo-default, where the check is moot. | 0.8 |

**VERDICT: ACCEPT, downgraded to High, with a corrected (open-leg-only, live-only, settle-aware, explicitly-mapped) design.**

### Gap 2 — Wire the drift detectors (claimed Critical, "minus the last wire")

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Citation accurate | **Substance yes, lines no.** `check_balance_drift` at `ccxt_adapter.py:256-310` correct. `reconcile_position_drift` is at HEAD `webhook_receiver.py:~1965` (claimed 2226-2250 is `_resolve_planned_orphan`); `unknown_resolver_loop` at HEAD `:~2409` (claimed 2670-2681). `emit_reconcile_alert` is still in `webhook_receiver.py` (~1953), NOT moved to `src/alerts.py` by d31c402d. | 0.9 |
| (b) Still open | **Yes, textbook inert monitor.** Repo-wide grep (src/, scripts/, deploy/, cron installers): zero production callers of either function or their primitives (`detect_position_drift`, `get_balance_summary`). `deploy/install-cron-monitors.sh` has no drift job. `reconcile_startup` explicitly ships an always-empty `position_mismatches` list. Tests (`test_phase_b_robustness.py`, 11 relevant cases) call the real production functions. | 0.95 |
| (c) Fix minimal as written | **No — "minus the last wire" is false. The call is minimal; the INPUTS do not exist.** (1) `journal_positions` (`{inst_id: signed_qty}` of HermX's believed position) has **no producer anywhere** — HermX maintains no net-position view; grep hits only the signature + tests. (2) `hermx_equity_usd` has no per-(venue,mode) assembler — `aggregate_strategy_pnl` is per-strategy and needs caller-supplied budget + live UPnL. (3) No active-(venue,mode) registry — the resolver derives executors only from open-order intents, so with zero open orders it would check nothing. (4) `check_balance_drift` is a hard no-op unless `mode=="live"` — in today's demo-default deployment, wiring it changes nothing. (5) 30 s resolver cadence × 2 REST calls/venue is too hot; needs a throttle. The alert-routing half of the claim IS already done (both functions emit `RECONCILE_MISMATCH` internally). | 0.9 |
| (d) Importance justified | **Partially inflated: High, not Critical.** The inert-monitor anti-pattern is real and the project's own rule condemns it. But: close sizing reads a **live venue position snapshot** at execution time (`ccxt_adapter.py` `_position_snapshot`), so position drift cannot silently mis-size a close; and balance drift is inert-by-design until a live account exists. Undetected venue-side positions HermX doesn't know about are a real exposure gap — High. | 0.8 |

**VERDICT: ACCEPT, downgraded to High, rescoped as a small feature (input plumbing + throttled wiring), not a wire. Alternative per the project's own inert-monitor rule: if the plumbing is not funded, delete or explicitly annotate the dead functions.**

### Gap 3 — Realized-PnL fallback derived from own fills (claimed High)

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Citation accurate | **Partially.** Per-venue mapping correct (OKX `pnl` / HL `closedPnl` / Binance `realizedPnl` / others None), but `_normalized_realized_pnl` lives in `src/executors/ccxt_adapter.py:43-63`, **not** `pnl_ledger.py`. `reconcile_from_order_history:612-703` exact. Aggregation blindness confirmed: None rows count as $0 while incrementing `closed_order_count` (`pnl_ledger.py:412,438-440`). | 0.85 |
| (b) Still open | **Yes.** No `pnl_source`, no derived-PnL code anywhere; the adapter's "Phase 2 backfills via positions history" comment is an unimplemented plan. | 0.95 |
| (c) Fix minimal as written | **No — the premise is factually wrong.** The "running position state `reconcile_from_order_history` already maintains" is `positions: dict  # inst_id -> signed running qty` (`pnl_ledger.py:636`) — **no entry price, no cost basis, keyed by inst_id only**. `(exit_px − entry_px)` cannot be computed from state that has no entry price. A correct version requires: a weighted-average-cost accumulator, fill-splitting on position flips (today flips are one WARNING, not split), `contract_size` sourcing (NOT on the history row — needs market-metadata lookup, `ccxt_adapter.py:447`), inverse-contract handling (`(exit−entry)×qty` is simply wrong for inverse), and a window-truncation guard (entry fill outside the 100-row window → cost basis silently wrong). Plus it flips two tests that deliberately pin honest-None (`test_pnl_ledger.py:389-398`, `test_pnl_attribution.py:276-304`). | 0.9 |
| (d) Importance justified | **Inflated: Medium.** The current primary venue (OKX, demo-default) HAS the `pnl` field — today's deployment is unaffected. The hole bites on venue expansion (Bybit). And the codebase's own posture ("honest None over fabricated figure") cuts against shipping a derived estimate whose price math has five known failure modes: **a wrong number in a money ledger is worse than an honest None.** The better fix direction is the one the adapter comment already names: per-venue **authoritative backfill** (e.g. Bybit's closed-PnL / positions-history endpoint), not client-side derivation. | 0.85 |

**VERDICT: REJECT as written (false premise, money-path risk). Re-open as "per-venue authoritative closed-PnL backfill" when a None-pnl venue goes live.**

### Gap 4 — Recovery for closes aged out of the 100-row window (claimed High)

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Citation accurate | **Yes.** Detector at `dashboard.py:963-989` (`strategy_order_history_snapshot`), alert carries only window-boundary metadata (cannot know which orders aged out), `max_recorded_closed_at` high-water exists (`pnl_ledger.py:328-343`, tested). Two un-claimed extra holes found: the legacy `okx_order_history_snapshot` reconcile path has **no detector at all**, and `high_water is None` (fresh ledger) never alerts. | 0.95 |
| (b) Still open | **Yes.** No recovery code, no caller of `get_order_history_archive` on the ledger path. | 0.95 |
| (c) Fix minimal as written | **No — the claimed primitives are weaker than the doc implies.** `get_order_history_archive` is the **same** `fetch_closed_orders` endpoint with a bigger `limit` — no `since`, no pagination; on venues with 100/page caps a bigger limit may not reach deeper at all. It returns **normalized** rows while `reconcile_from_order_history` consumes **raw**-shape rows (`ordId` vs `ord_id`) — shape translation needed. `fetch_my_trades` has **zero hits** in src/ — that leg is new adapter code. The fold side IS cheap (read-side dedup by `(exchange,inst_id,ord_id,mode)` makes re-folding idempotent), but one wrinkle: a deep fetch starting mid-position can misclassify closes (running-position tracker starts at 0 per call) unless rows carry `reduceOnly`. | 0.9 |
| (d) Importance justified | **Substantially inflated: tail risk, not High.** The window is 100 terminal orders **per symbol** (fetch loops per inst_id); reconcile runs every ~15 s whenever the dashboard is up (`DASHBOARD_REFRESH_INTERVAL_SECONDS=15`); at operator-in-loop volume (single-digit closes/day/symbol) aging out requires **weeks of continuous dashboard downtime while the receiver keeps trading**. The genuinely important finding underneath: **P&L ledgering happens ONLY inside the optional dashboard process** — that architectural dependency, not the window size, is the real issue, and it deserves its own decision (move ledger reconcile into the receiver, or accept and document). | 0.85 |

**VERDICT: REJECT as a top item. Record two cheap opportunistic mitigations (configurable limit + one deep pass at dashboard startup) and the dashboard-only-reconciler architectural question as a separate decision.**

### Gap 5 — First-class PARTIALLY_FILLED order state (claimed High)

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Citation accurate | **No.** Both line citations wrong (`_ORDER_STATE_TRANSITIONS` at HEAD `:1337-1344`, not 1598-1611; `record_order_state` at `:1638-1671`, not ~1899-1932). Worse, the claim **omits that the proposed mapping already exists**: `map_order_outcome` (`webhook_receiver.py:1788-1834`) maps venue `partially_filled` → `(FILLED, partial=True, "partially_filled")`, `0<acc<ordered` → `partial_by_size`, `canceled`+`acc>0` → `canceled_after_partial_fill`; the post-submit reconcile path persists `partial`/`acc_fill_sz`/`avg_px` into the journal (`service.py:420-431`). | 0.9 |
| (b) Still open / scenario real | **The claimed failure scenario is essentially impossible for this flow.** HermX is market-order-only (`order_type` defaults `"market"`, no `timeInForce` anywhere); market orders don't rest. The IOC partial-then-cancel case is already handled end-to-end: the ledger's non-terminal skip does NOT skip `canceled` (not in `_NON_TERMINAL_ORDER_STATES`, `pnl_ledger.py:62-64`) and writes the **actual** `filled_qty` from `accFillSz` (`:641,594`); `_order_fully_filled` (`ccxt_adapter.py:183-198`) explicitly never terminalizes a partial IOC as complete; close sizing reads a live venue position snapshot, so position math cannot drift from a partial. Real residual defects, both small: (1) startup-reconcile and UNKNOWN-resolver journal records carry only the partial `reason` string, not `acc_fill_sz`/`avg_px` (the post-submit path carries them); (2) a transiently-open order with `filled>0` caught mid-matching maps to `partially_filled` → journalled FILLED and frozen before the remainder fills — cosmetic for market flow, real only if limit orders ever ship. | 0.85 |
| (c) Fix minimal | **No.** ~12-15 touch points across `webhook_receiver.py` (constants, two state sets, transition table, three reconcile/resolver gates, `load_open_orders`), `execution/service.py`, `dashboard.py` (terminal set + badges), 2 dashboard-ui TSX components, plus rewriting the exhaustive-matrix FSM test (`test_order_state_machine.py:94-105` pins the 9-edge set) and 4+ partial-mapping test files. **Rollback hazard**: old code reading a journal containing the new state fails closed (`ValueError`) and those orders vanish from open-order tracking. | 0.9 |
| (d) Importance justified | **No. High is unjustified** — P&L and position math are already correct for every partial scenario reachable by market-only flow. The state buys churn, not correctness, until limit orders exist. | 0.9 |

**VERDICT: REJECT the new state. Accept the proportionate micro-fix: carry `acc_fill_sz`/`avg_px` in the two journal-detail dicts that lack them. Revisit the full state only if/when limit orders ship.**

### Honorable mention — Typed FillReport normalization seam (claimed Medium)

| Sub-question | Verdict | Conf |
|---|---|---|
| (a) Claim accurate | **Substantially, with two corrections.** TRUE: the reduceOnly string quirk is implemented **3×** (`ccxt_adapter.py:66-76`, `pnl_ledger.py:655-657`, `dashboard.py:1241/1274`); raw OKX `pnl` is read downstream past the helper in 2 places (`pnl_ledger.py:536` fallback; **`dashboard.py:1299` reads `hist.get("pnl")` and ignores the normalized `realized_pnl` entirely** — HL/Binance closes show None P&L in enriched UI rows today); the bulk history path bypasses the existing normalized-order envelope **by documented design** (`base.py:131-139`). INACCURATE: downstream parses an adapter-built OKX-native-keyed dict, not literal raw CCXT dicts (CCXT parsing IS centralized); and webhook_receiver's reconcile/resolver consume only normalized shapes — not part of this gap. | 0.9 |
| (b) Still open | Yes — zero dataclass/TypedDict/NamedTuple in pnl_ledger.py or executors/. | 0.95 |
| (c) Size honest | **Mild undersell.** Emit side is thin (one build site, one adapter class), but proper consumption touches 4 source files / ~6 parse sites plus ledger+dashboard tests, and persisted-JSONL + UI-dict constraints mean the struct wraps rather than replaces. Lands at/over the project's ">3 files → break it up" rule unless staged 2-3 increments. | 0.85 |
| (d) Importance | Medium is fair — it's the structural fix for the bug class, but its value accrues mostly at venue expansion. | 0.85 |

**VERDICT: REJECT the full seam for now. Accept its highest-value fragment immediately: the `dashboard.py:1299` one-liner (prefer `realized_pnl`). Stage the full seam as a follow-up when a second None-pnl venue goes live (pairs with the Gap-3 replacement).**

---

## 2. ACCEPTED FOR PLAN (ranked by validated value)

Ordering note: per dev-rules, each item below is an independent task (≤3 source
files each), test-first (rule 6), never weakening existing tests — the pinned
tests listed are **extended**, not rewritten. All new gates follow the
fail-open-on-observability-failure and never-block-a-close invariants.

### Item A (was Gap 1, now High) — Live-mode pre-trade balance check + distinct sub-min outcome

**Why accepted:** the balance half is fully open, self-contained, on the money
path, and all inputs exist (`get_balance_summary`, leverage in readiness,
`market["settle"]`). It is the only top-5 item whose fix needs no new state or
plumbing — only correct scoping.

**Design constraints (from validation — these are the fix, not decoration):**
- OPEN leg only. The reduce-only close branch is NEVER gated (never-block-a-close).
  Placing the check inside the open-leg branch (after the close leg has executed)
  also makes reversal margin-freeing a non-issue.
- Live mode only (`simulated_trading is False`); demo sandbox balances are arbitrary.
- Fail OPEN: `get_balance_summary` returning `None` (fetch failure) → submit proceeds.
- Settlement currency from `market_spec["market"]["settle"]`, fallback `"USDT"`.
- The skip must reuse the existing leg-result contract (`submitted: False,
  status: "skipped", reason: "insufficient_balance"`) so the service's existing
  `submit_failed` mapping produces a clean `SUBMITTED→REJECTED` journal transition
  — it must NOT invent an unmapped top-level mode that could fall into the
  UNKNOWN path and pause the symbol.

**Steps:**
1. **Tests first** (extend `tests/test_ccxt_adapter.py` + `tests/test_phase_a_robustness.py`):
   - open leg skipped with `reason="insufficient_balance"` when
     `free < notional/leverage` (fake client balance fixture), live mode;
   - close leg / `close_only=True` submits even with zero free balance (invariant test);
   - balance fetch returns `None` → order submits (fail-open);
   - `simulated_trading=True` → check skipped entirely;
   - non-USDT settle currency is the one fetched;
   - service emits an `under_execution`-style alert with stage
     `insufficient_balance` when all legs skip for that reason.
2. Adapter: add `_sufficient_free_balance(client, market_spec, notional, leverage)`
   helper in `src/executors/ccxt_adapter.py`; call it in the OPEN branch of
   `CcxtExecutor.execute` (immediately before the open-leg `create_order`,
   HEAD `:863`). Optional headroom buffer via env
   `HERMX_BALANCE_CHECK_BUFFER_PCT` (default 0 — keep it dumb).
3. Service: extend the A1b block (`service.py:374-390`) to alert on
   `reason in {"zero_size", "insufficient_balance"}` with distinct stages.
   Keep the existing `zero_size` string match working (pinned by
   `test_phase_a_robustness.py:238,254`).
4. Sub-min disambiguation (small, separate commit): in `_contracts_for_notional`
   / `_amount_from_readiness`, thread a distinct `below_instrument_min` reason
   for the `min_amount` floor case (leave plain `zero_size` for no-price /
   zero-notional); consult `limits.cost.min` in `_market_spec` the same way.
   Extend (do not rewrite) the A1b tests: the service condition accepts both
   reason strings, old `zero_size` behavior still asserted.

**Regression tests to add:** the six in step 1, plus
`test_below_instrument_min_reason_distinct_from_zero_size` and
`test_min_cost_limit_floors_to_zero_with_reason`.
**Files:** `src/executors/ccxt_adapter.py`, `src/execution/service.py`, 2 test files.

### Item B (was Gap 2, now High) — Drift detection wiring, rescoped as two phases

**Why accepted:** genuinely inert, genuinely tested, and the project's own rule
forbids leaving it half-existing. But it is honestly a **small feature** (input
plumbing), not "the last wire" — and it ships in two phases so each stays ≤3 files.

**Phase B1 — balance drift (simpler; inert-by-design until live, wire it now so
coverage exists the day live arrives):**
1. **Tests first** (extend `tests/test_unknown_resolver_controls.py` or new
   `tests/test_drift_wiring.py`): throttled tick calls `check_balance_drift`
   once per N resolver ticks per (venue, mode); demo mode asserted no-op
   (already pinned by `test_phase_b_robustness.py:195` — do not touch);
   equity assembler returns budget + closed_net (+ live UPL when available)
   per (venue, mode).
2. Add an **active-(venue,mode) enumerator** in `webhook_receiver.py`: derive
   from loaded strategy configs (`execution_mode` + `instrument.exchange` per
   strategy) — ~15 lines, no new store.
3. Add `_account_equity_estimate(venue, mode)` helper: sum per-strategy
   `budget_usd` + `pnl_ledger` closed-net for that (exchange, mode); treat live
   UPL as best-effort (executor positions read, fail-open to closed-only equity
   with the omission logged — log-and-continue posture).
4. Call `check_balance_drift` from `unknown_resolver_loop` every Nth tick
   (`HERMX_DRIFT_CHECK_EVERY_N_TICKS`, default 10 → ~5 min at the 30 s tick),
   fail-open, never blocking the resolver's real work.
   *Alternative call-site if the operator prefers zero receiver changes: a
   Hermes cron gate script per the established `hermx_gate_lib` pattern
   (per code-quality rule, absence/frequency gates live in per-gate scripts).*

**Phase B2 — position drift (has an open design decision; do NOT start until
it's made):** `journal_positions` has no producer. Two honest options:
- **Option 1 (recommended, cheapest honest check):** asymmetric "unclaimed
  venue position" detection — venue reports a nonzero position on an inst_id
  no active strategy trades, or a position with no non-terminal/recently-filled
  order-journal record → alert. Needs no believed-position bookkeeping;
  catches the scariest case (venue-side exposure HermX knows nothing about).
- **Option 2 (fuller):** build `believed_positions(venue, mode)` by folding
  FILLED order-journal intents (side × qty per inst_id) — requires verifying
  the journal (checkpoint + segments) spans full position lifetimes; if it
  doesn't, this option is invalid. Verify before choosing.
- If neither is funded: per the inert-monitor rule, annotate
  `reconcile_position_drift` as intentionally unscheduled (docstring + comment
  in the comparison doc) or delete it. **Leaving it silently inert is the one
  outcome this plan forbids.**

**Regression tests:** wiring/throttle tests (B1 step 1); for B2 Option 1, a
fake-venue test: unclaimed position → `RECONCILE_MISMATCH` with
`stage="position_drift"`, plus claimed-position → no alert.
**Files (B1):** `src/webhook_receiver.py`, `src/pnl_ledger.py` (equity helper,
if placed there), 1 test file.

### Item C — Correctness micro-fixes surfaced by this validation (Medium, near-zero risk)

Small, independently committable, each with a regression test:

1. **`dashboard.py:1299`** — `enrich_close_rows_with_okx_history` reads raw
   `hist.get("pnl")` (OKX-only) and ignores the adapter-normalized
   `realized_pnl`; Hyperliquid/Binance closes show None P&L in enriched rows
   today. Fix: prefer `hist.get("realized_pnl")`, fall back to `pnl`.
   Test: HL-shaped history row (`closedPnl` → normalized `realized_pnl`)
   enriches with the value.
2. **Partial-fill journal detail parity** — startup reconcile and the UNKNOWN
   resolver persist only the `reason` string for partials; the post-submit path
   persists `acc_fill_sz`/`avg_px` (`service.py:420-431`). Add the same two
   fields to the detail dicts at the startup-reconcile and resolver
   `record_order_state` call sites (re-locate by symbol in
   `webhook_receiver.py`; lines volatile). Test: resolver resolves a
   venue-`partially_filled` order → journal row detail carries `acc_fill_sz`.
   This is the surviving 10% of Gap 5.

**Files:** `src/dashboard.py`, `src/webhook_receiver.py`, 2 test files.

---

## 3. REJECTED

- **Gap 3 (derived realized-PnL) — REJECTED as written.** Premise is false: the
  "running position state" is `{inst_id: signed_qty}` with no entry price; the
  real fix needs a WAC accumulator + flip-splitting + contract-size lookup +
  inverse handling + window-truncation guard, and flips two tests that pin the
  honest-None posture. A wrong number in a money ledger is worse than an honest
  None. **Replacement direction (future, when a None-pnl venue goes live):**
  per-venue authoritative closed-PnL backfill (e.g. Bybit positions-history
  endpoint) — the "Phase 2" the adapter comment already names.
- **Gap 4 (100-row ageout recovery) — REJECTED as a priority item.** Tail risk:
  window is per-symbol, reconcile runs every ~15 s while the dashboard is up;
  ageout needs weeks of dashboard downtime at real volume. The proposed
  primitives don't exist as described (`archive` = same endpoint, no
  `since`/pagination; `fetch_my_trades` absent; raw/normalized shape mismatch).
  *Opportunistic mitigations (Low, unranked):* make the `limit=100` at the two
  dashboard call sites configurable; run one deeper pass at dashboard startup.
  *Real underlying issue to decide separately:* P&L ledgering exists ONLY in
  the optional dashboard process.
- **Gap 5 (PARTIALLY_FILLED state) — REJECTED.** The scenario is unreachable in
  market-order-only flow, and the mapping it proposes already exists
  (`map_order_outcome` returns `partial=True` + reasons; ledger writes actual
  `filled_qty` for canceled-after-partial; close sizing reads live venue
  positions). Cost: ~12-15 touch points, exhaustive-FSM test rewrite, and a
  fail-closed rollback hazard on journals containing the new state. The
  surviving 10% ships as Item C.2. Revisit only if limit orders ship.
- **Honorable mention (typed FillReport seam) — REJECTED for now.** Right idea,
  undersold size (4 files / ~6 parse sites, staged); most of its immediate value
  is captured by Item C.1. Stage the full seam together with the Gap-3
  replacement when venue expansion makes it pay.

## 4. Reflection

**Overall confidence in the surviving list: 0.85.** Item A: 0.9 (all facts
verified twice — code reads + pinned tests). Item C: 0.9 (tiny, directly
observed defects). Item B: 0.75 — B1's equity assembler needs a precision
decision (whether live UPL is in-scope for synthetic equity, and where the
helper lives), and B2 hinges on an unmade design choice (Option 1 vs 2) plus
one unverified fact (whether the order journal spans full position lifetimes —
checkpoint/rotation may truncate). Those two questions, plus re-locating every
`webhook_receiver.py` symbol after the in-flight `control_state.py` refactor
lands, are the items needing investigation before implementation. The
rejections are high-confidence (0.85-0.9): each is grounded in code that
demonstrably exists (A1b alert, partial handling, live position snapshots) or
demonstrably doesn't (entry-price state, `fetch_my_trades`, `journal_positions`
producer).
