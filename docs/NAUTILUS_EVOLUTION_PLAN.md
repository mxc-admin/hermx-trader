# Nautilus Evolution Plan for HermX
> Brutally honest filter of Nautilus patterns mapped to HermX gaps.
> Generated: 2026-07-03

Grounded in: `src/pnl_ledger.py` (372 lines), `src/execution/service.py` (356 lines),
`docs/PNL_MASTER_PLAN.md` (20 issues, Phases 0–3 shipped), and the actual Nautilus
Rust at `crates/execution/src/reconciliation/{orders,ids}.rs`,
`crates/model/src/position.rs`.

**One-line thesis:** The two patterns the first analysis sold as crown jewels —
deterministic synthetic IDs and the 3-case quantity-mismatch policy — are the
*least* applicable to HermX, because both exist to reconcile a **local in-memory
fill cache** against the venue. HermX has no such cache. Its reconcile is
"read venue order history → dedupe by `ord_id` → append." Half of Nautilus's
reconciliation machinery is solving a problem HermX designed away. The genuinely
useful borrowings are small and unglamorous: observability timestamps, invariant
assertions, age-out detection, and resolving stuck UNKNOWN orders.

---

## 1. Verdict on the Nautilus Analysis

| # | Pattern | Verdict |
|---|---------|---------|
| 1 | Event vs Report split | **DEFER** |
| 2 | Deterministic synthetic IDs | **REJECT** (revisit only if HermX ever infers fills) |
| 3 | Exhaustive order state machine | **ADAPT** (keep the fail-closed kernel, reject the scale) |
| 4 | `trade_id` universal idempotency key | **REJECT** |
| 5 | 3-case quantity-mismatch reconciliation | **REJECT** (the dangerous case) / **ADAPT** (the trivial case) |
| 6 | Startup mass-status + periodic pull poll | **ADOPT** (already shipped; codify + extend) |
| 7 | `signed_qty` as single source of truth + invariants | **ADOPT** |
| 8 | Deterministic netting position ID | **REJECT** |
| 9 | Dual timestamps (`ts_event` / `ts_init`) | **ADOPT** |
| 10 | Pure core + thin orchestrator | **ADOPT** (already the house style; codify) |

**1 — Event vs Report split → DEFER.** Nautilus models venue state as `Report`
objects and diffs them against a stream of local `Event`s on an event bus. HermX's
`closed-trades.jsonl` is *already* append-only and event-sourced-ish, and its
"report" is just the raw order-history read. The diff already happens implicitly in
`reconcile_from_order_history`. Formalizing a `Report` type and an event bus buys
type ceremony HermX has no consumer for. It is not wrong — it is architecture for a
system with a portfolio manager and a risk engine subscribing to the bus. HermX has
neither. Revisit only if HermX grows a second consumer of order events.

**2 — Deterministic synthetic IDs → REJECT (for now).** This is the most overhyped
recommendation. In Nautilus (`ids.rs:71`), `create_inferred_reconciliation_trade_id`
hashes eleven fields (account, instrument, client_order_id, venue_order_id, side,
type, filled_qty, last_qty, last_px, position_id, **venue-provided `ts_last`**) into a
`trade_id`. It exists for exactly one situation: **the venue reports a position or
fill that has no discrete trade record**, so Nautilus must *manufacture* a fill and
needs a replay-stable id for it. HermX never manufactures fills. It reads real
order-history rows that already carry a real `ordId`, and dedupes on
`(exchange, inst_id, ord_id, mode)` (`pnl_ledger.py:96`) — already collision-safe and
replay-stable. Adopting an eleven-field hash to replace a field that already exists is
pure ceremony. The pattern becomes relevant *only* if HermX starts inferring closes
from position deltas that lack an `ord_id` — which today it does not (the position-
delta path at `pnl_ledger.py:348-369` still keys off real rows with real `ordId`s).

**3 — Exhaustive order state machine → ADAPT.** The recommendation conflates two
things. Nautilus has ~14 order statuses because it supports 14 order types across
dozens of venues. HermX has five journal states (planned/submitted/filled/rejected/
unknown) and one transition guard (`order_state_can_transition`, used at
`service.py:317`). It does **not** need Nautilus's status enum — that would be textbook
over-engineering for a market-order-dominant TV bridge. The one kernel worth keeping:
*a single fail-closed whitelist; unknown (status, event) pairs reject rather than
guess.* HermX already has the whitelist — the gap is that it is not audited for
completeness and, critically, has **no exit from UNKNOWN** (see §2, §3-item-4). ADAPT =
harden the existing five-state machine, not import the fourteen-state one.

**4 — `trade_id` as universal idempotency key → REJECT.** Same root as #2. HermX's
posture is one order → one aggregate close row (it reads cumulative `accFillSz`, so
multiple partial fills of one order already collapse to one row). `ord_id` *is* the
universal key at every HermX layer already. Threading a `trade_id` through would add a
key with no distinct role. Revisit only alongside #2, if per-fill granularity ever
matters (limit-order ladders, TWAP) — which is not HermX's product.

**5 — 3-case quantity-mismatch reconciliation → mostly REJECT.** Read the actual code
(`orders.rs:886` `reconcile_fill_quantity_mismatch`): all three cases compare
`order.filled_qty()` (local cache) against `report.filled_qty` (venue). HermX keeps no
local per-order filled quantity to compare — it has no cache, by design. So:
 - *venue < cache → warn:* N/A. No cache.
 - *venue > cache → synthesize inferred fill with back-solved price:* this is the
   dangerous one, and it is **on the analysis's own "do NOT copy" list** (partial-window
   synthetic reconstruction). Nautilus itself clamps and back-solves the price
   (`orders.rs:995` `calculate_incremental_fill_price`, `clamp_inferred_fill_price`) —
   money invented from arithmetic. HermX must never do this. Hard REJECT.
 - *equal qty, status differs → emit update:* the only survivable case, and HermX's
   post-submit reconcile already does the equivalent — it emits a mismatch alert when
   `recon_state != outcome_state` and it is not the expected SUBMITTED→FILLED
   progression (`service.py:337-350`). Marginal ADAPT at best; no new machinery.

**6 — Startup mass-status + periodic pull poll → ADOPT (already done).** HermX already
*is* this: pull-based, no websocket, startup resolver (`reconcile_startup` /
`resolve_unknown_orders_once`) plus a 10-minute cron safety net
(`deploy/hermes-scripts/hermx-ledger-reconcile.py`, Phase 3). Nothing to import. The
worthwhile *extension* is age-out detection against the 100-row window (§3-item-3).

**7 — `signed_qty` single source of truth + invariants → ADOPT.** HermX already tracks
a running signed position per instrument inside `reconcile_from_order_history`
(`pnl_ledger.py:346-355`) to detect position-delta closes. Nautilus adds cheap
invariant assertions on top (`position.rs:388-399`: side sign must match `signed_qty`
sign). HermX has the state but not the guardrail. Adding assert-or-log is a few lines
and directly protects the close-detection logic. This is the cleanest real ADOPT.

**8 — Deterministic netting position ID `{instrument}-{strategy}` → REJECT.** HermX has
no position objects to key — it re-reads positions from the venue each poll and
attributes trades via `cl_ord_id` prefix (`is_hermx_cl_ord_id`). A synthetic position
id keys nothing that exists. No payload.

**9 — Dual timestamps `ts_event` / `ts_init` → ADOPT.** HermX ledger rows record only
venue time (`closed_at_ms` from `uTime`/`cTime`, `pnl_ledger.py:278-288`) — that is
`ts_event`. There is no `ts_init` (local observation time). This is a genuine hole:
without it you cannot measure reconcile lag, cannot tell a close recorded 8 minutes
late from one recorded instantly, and cannot detect the #8 age-out race after the fact.
It also aligns with the house rule already in CLAUDE.md ("freshness is bounded on bar
time, never server time") — dual timestamps is that rule made concrete on ledger rows.
Cheap, additive, real value.

**10 — Pure core + thin orchestrator → ADOPT (already the house style).** HermX already
does this: `resolve_execution_config` is explicitly documented `PURE` (`service.py:15`),
and `reconcile_from_order_history` takes rows and returns a count with I/O isolated in
`append_closed_trades`. Nothing to build — worth codifying as a rule so a future edit
doesn't smuggle a venue read into the pure path.

**Overhyped, called out:** #2 and #5 were presented as the highest-value ideas. They
are the *lowest*-value for HermX because both presuppose a local fill cache HermX
deliberately lacks. #3 (full state machine) is the classic "borrow the big system's
complexity" trap. Roughly half the recommendation list is Nautilus solving problems
HermX's simpler architecture never has.

---

## 2. HermX Gap Analysis

**Do we need a full order state machine?** No. We need to fix the *exit* on the one we
have. The five-state journal + `order_state_can_transition` whitelist is proportionate.
The real defect is that **UNKNOWN is a terminal black hole**: an order that reconciles
to nothing (wrong-env query, venue timeout, aged-out row) stays UNKNOWN forever. #20a
fixed the *wrong-env* cause of stuck UNKNOWNs, but not the case where the order is
genuinely gone from the venue's readable window. Nautilus resolves such orders to a
terminal state after bounded attempts (`generate_external_order_status_events`,
`create_reconciliation_rejected`). That exit is the gap — not the state count.

**Do we need a position cache?** No. Re-reading from the venue is correct for HermX's
scope and sidesteps an entire class of cache-coherence bugs (the exact bugs Nautilus's
`Rc<RefCell<Cache>>` and its three-case mismatch machinery exist to manage). The
failure mode of re-reading is *staleness within one poll*, which is acceptable for a
dashboard and a 10-minute reconcile cadence. Do not add a cache.

**Do we need continuous reconciliation?** No — post-submit + startup + 10-minute cron
already cover the money path. The *one* real gap in coverage is the **100-row history
window (#8)**: on a busy symbol a close can age out of the readable window before any
reconcile sees it → permanent silent loss. HermX currently mitigates by "reconcile
often" but has **no detection** that the mitigation failed. This is the single highest-
value borrowing target, and the borrowing is a *cursor/high-water check*, not
Nautilus's full poll engine.

**Do we need deterministic synthetic IDs?** No. `ord_id` dedupe is sound for a system
that reads real venue rows and never manufactures fills. The only "hole" is theoretical
and would open *only if* HermX started inferring closes without an `ord_id` — which the
plan explicitly avoids (spot closes are log-and-skip, Open Decision 3).

**Do we need the Event/Report split?** No incremental value today. The ledger is
already the event log; the venue read is already the report; the diff already happens.
The split pays off when you have multiple subscribers to an event bus — HermX has one
consumer (the dashboard) reading one file.

**What HermX actually lacks (ranked):**
1. **Age-out detection** for the 100-row window (#8) — the only silent-permanent-loss
   risk still open.
2. **A terminal exit from UNKNOWN** — stuck orders accumulate and never resolve.
3. **Local observation timestamps** — no way to measure reconcile lag or diagnose (1).
4. **Invariant guards** on the `signed_qty` running position — close detection is
   unprotected against a sign flip.
5. **A codified fail-closed transition whitelist** — exists, but unaudited and with the
   UNKNOWN hole above.

Everything else Nautilus offers is machinery for a cache/bus/portfolio HermX doesn't
have.

---

## 3. Evolution Plan — What to Actually Build

Five items. I deliberately stopped short of six — the sixth candidates (Event/Report
split, synthetic IDs) are DEFER/REJECT, and padding to six would be the exact over-
engineering this doc exists to prevent.

### Item 1 — `signed_qty` invariant assertions in close detection  *(ADOPT)*
- **What:** In `reconcile_from_order_history`, after updating the running position,
  assert-or-log that (a) the classified side matches the sign of the prior position on
  a detected close, and (b) a single fill never flips the position sign through zero
  silently (Nautilus splits such a fill; HermX should at minimum log it). Log-and-
  continue, never raise (money path stays fail-open on observability).
- **Why:** The position-delta close detector (`pnl_ledger.py:348-369`) is the load-
  bearing logic for spot/no-reduceOnly venues. A mis-sorted history or a duplicated row
  silently corrupts `signed_qty` and mis-detects closes. Today nothing guards it.
- **Where:** `src/pnl_ledger.py`.
- **Effort:** S
- **Phase:** post-plan hardening (or fold into Phase 1 code you already own).

### Item 2 — Dual timestamps: add `recorded_at_ms` (ts_init) to ledger rows  *(ADOPT)*
- **What:** Add a local observation timestamp alongside the venue `closed_at_ms`
  (`ts_event`). Schema bump v2→v3; `read_closed_trades` back-fills absent values as
  `None` (mirrors the existing v1 net back-fill at `pnl_ledger.py:159-164`, so it stays
  rollback-safe and append-only).
- **Why:** Without it, reconcile lag is unmeasurable and the #8 age-out race is
  undetectable after the fact. It is also the prerequisite signal for Item 3.
- **Where:** `src/pnl_ledger.py` (`_build_entry`, `SCHEMA_VERSION`, `read_closed_trades`).
- **Effort:** S
- **Phase:** post-plan (schema v3); trivially rollback-safe.

### Item 3 — 100-row window age-out detection  *(ADAPT — highest real value)*
- **What:** When an order-history snapshot returns exactly the row cap (100) and the
  **oldest** returned row is newer than the newest `closed_at_ms` already in the ledger
  for that `(venue, mode)`, the window may have dropped un-recorded closes. Emit a
  reconcile alert (reuse `reconcile_alert_mismatch`). This is Nautilus's high-water
  cursor idea reduced to a detector — not its full paginating poll engine.
- **Why:** #8 is the only remaining silent-permanent-loss path. HermX mitigates by
  polling often but has zero signal when the mitigation is losing the race. Detection
  first; pagination (Open Decision 6) only if detection fires in practice.
- **Where:** `src/dashboard.py` (order-history snapshot call site) + a small helper in
  `src/pnl_ledger.py` (max recorded `closed_at_ms` per `(venue, mode)`).
- **Effort:** S (detection) — do **not** build full pagination up front.
- **Phase:** closes Open Decision 6 / Risk Register "history-window race".

### Item 4 — Bounded terminal exit for stuck UNKNOWN orders  *(ADAPT)*
- **What:** In the startup/cron resolver, an order in UNKNOWN that is absent from the
  venue read after N attempts / T elapsed transitions to a terminal state
  (REJECTED, or a distinct `expired_unknown`) with an operator alert — instead of
  limbo forever. Mirrors Nautilus `generate_external_order_status_events` /
  `create_reconciliation_rejected` (`orders.rs:237,466`), minus the synthetic fills.
- **Why:** #20a fixed wrong-env queries, but genuinely-gone orders (aged out, venue
  timeout) still pile up UNKNOWN with no resolution, and each one keeps its symbol
  paused (`service.py:154-158`) — trapping the operator on that symbol.
- **Where:** `src/webhook_receiver.py` (resolver — **shared file, explicit confirmation
  required per dev-rules**) + the transition whitelist.
- **Effort:** M
- **Phase:** post-plan; pairs with Item 5.

### Item 5 — Audit + codify the fail-closed transition whitelist  *(ADOPT)*
- **What:** Confirm `order_state_can_transition` rejects every (from, to) pair not
  explicitly whitelisted (Nautilus's `should_reconciliation_update` /
  `report_is_confirmed_state` philosophy, `orders.rs:420,862`), add the new
  UNKNOWN→terminal edge from Item 4, and add a table-driven test asserting the full
  legal/illegal matrix. Codify "unknown transition ⇒ reject, never guess" as a rule.
- **Why:** The guard exists but is unaudited; Item 4 adds an edge that must be
  whitelisted deliberately, not by omission.
- **Where:** wherever `order_state_can_transition` is defined + `tests/`.
- **Effort:** S
- **Phase:** post-plan; ships with Item 4.

**Cut list (rated DEFER/REJECT, deliberately not built):** Event/Report split,
deterministic synthetic `trade_id`, full 14-status state machine, universal `trade_id`
key, 3-case mismatch with back-solved price, netting position IDs. Each presupposes a
cache/bus/position-object HermX does not have.

---

## 4. Anti-Patterns to Guard Against

1. **Back-solving a fill price to make the arithmetic close.** Nautilus's
   `create_incremental_inferred_fill` / `calculate_incremental_fill_price`
   (`orders.rs:692,995`) invent a fill and a price when the venue count exceeds the
   local count. That is money conjured from a subtraction. HermX must never synthesize a
   priced fill — if a close is not in the venue read, log-and-skip, never fabricate.

2. **A local cache that must be kept coherent with the venue.** `Rc<RefCell<Cache>>` and
   half of Nautilus's reconciliation exists to repair cache/venue divergence. HermX's
   "re-read every time" avoids the entire bug class. Do not add a position/fill cache to
   "optimize" the dashboard poll — you would import the exact problem you were free of.

3. **`panic!` / raise on a missing order.** Nautilus can afford to be loud; a money
   receiver under `Restart=always` cannot turn a missing-order read into a crash loop.
   Every reconcile path in HermX must degrade (log, alert, leave state recoverable),
   never raise into the intake path. (HermX already does this — keep it.)

4. **`f64` for money.** Nautilus uses fixed-point `Price`/`Quantity`/`Money` types and
   clamps to instrument precision. HermX carries floats through `_as_float`. This is
   tolerable at HermX's scale but it is a latent correctness risk — do not *widen* float
   math (e.g. don't start summing running P&L in a hot loop and comparing for equality);
   keep money arithmetic to add-and-store, compare with tolerance.

5. **Growing the state machine to match a bigger system's status enum.** Adding order
   statuses "for completeness" without an order *type* that produces them is dead
   complexity. Only add a state when a real HermX transition needs it (Item 4's
   terminal-unknown is the one justified addition).

---

## 5. Confidence & Caveats

**Overall confidence: 0.75.**

High confidence that the *rejections* are correct — the Nautilus code directly confirms
that synthetic IDs, the 3-case policy, and the netting position ID all operate on a
local cache HermX structurally lacks, so they cannot apply without first building that
cache (which is itself an anti-pattern for HermX). Lower confidence on the exact
effort/shape of Items 3 and 4, which depend on runtime facts not yet measured.

**What could make this plan wrong or wasteful:**
- If `ord_id` is *not* actually unique-per-close on some target venue (e.g. a venue that
  reuses or omits order ids on partial fills), then the dedupe key has a hole and #2/#4
  (synthetic IDs) stop being pure fluff. This must be checked per venue before trusting
  Item 1/Item 3.
- If real reconcile lag on a busy symbol never approaches the 100-row window, Item 3 is
  solving a non-problem — build the *detector* (cheap) before any pagination.
- If HermX's roadmap adds limit-order ladders, TWAP, or spot venues with position-delta
  closes lacking `ord_id`s, then #2/#5 (synthetic IDs, richer state) move from REJECT
  toward ADAPT. This plan is scoped to the *current* market-order-dominant posture.
- Items 4 and 5 touch `webhook_receiver.py` (shared file) and the order state machine —
  the dev-rules require explicit confirmation, and a mistake here regresses intake.

**Verification steps before any code:**
1. Confirm `ord_id` uniqueness-per-close empirically on each enabled venue (OKX first),
   not by assumption.
2. Measure actual reconcile lag: on a busy demo symbol, compare oldest row in the
   100-row window against ledger-max `closed_at_ms` over a day. Only build Item 3's
   detector if the margin is thin; only consider pagination if the detector fires.
3. Enumerate how orders currently reach and stay in UNKNOWN in production (count them
   over a week) to size Item 4 and confirm the terminal-exit predicate.
4. Audit the existing `order_state_can_transition` matrix and write the failing table-
   driven test *before* adding the UNKNOWN→terminal edge (dev-rules: repro first).
5. Confirm Items 2/3's schema-v3 back-fill is rollback-safe by reading a v1/v2 ledger
   with the new reader before shipping.
```
