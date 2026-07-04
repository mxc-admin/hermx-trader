# HermX Execution & Reconciliation Design Document
## Influenced by Nautilus Trader Architecture Analysis

> Second-pass validation of `docs/NAUTILUS_EVOLUTION_PLAN.md`, grounded in a full
> re-read of the Nautilus Rust reconciliation crates and the HermX code the plan
> targets. Brutally honest: where the plan is right it is adopted verbatim; where it
> is wrong (Item 4) the evidence overturns it. Generated 2026-07-03.

---

### 1. Executive Summary

This document specifies what HermX should actually build after studying Nautilus
Trader's execution/reconciliation subsystem. Nautilus was studied because it is a
mature, venue-neutral reconciliation engine solving the same core problem HermX has
— keeping a durable, correct record of orders and fills against an unreliable venue —
at far greater scale. The study's central conclusion holds: **the two patterns the
first analysis sold as crown jewels (deterministic synthetic fill-IDs and the
three-case quantity-mismatch policy) are the *least* applicable to HermX**, because
both exist to reconcile a local in-memory fill cache against the venue, and HermX has
no such cache by deliberate design. What HermX should keep from Nautilus is small and
unglamorous: an observability timestamp, a position-accumulator invariant, an age-out
detector, and a *confirmation* — not a change — that HermX's handling of unresolvable
orders already matches Nautilus's.

**We kept:** `signed_qty` invariants + float snap-to-zero (Nautilus `position.rs`),
dual `ts_event`/`ts_init` timestamps (`events/order/filled.rs`), the age-out cursor
idea reduced to a detector (`engine/mod.rs` mass-status), and the fail-closed
transition philosophy (`orders/mod.rs:276`).
**We discarded:** synthetic `trade_id`s, the 3-case mismatch with back-solved fill
prices, netting position IDs, the Event/Report bus split, and a local position/fill
cache. Each presupposes machinery HermX structurally lacks, and importing it would
import the exact bug class HermX's "re-read every poll" design avoids.
**We overturned:** the plan's Item 4 ("stuck UNKNOWN is a terminal black hole; force
it to REJECTED"). The Nautilus Rust engine does the *opposite* of what the plan
attributes to it, and HermX already does the correct thing.

**Score of the raw evolution plan: 6.5 / 10.** Its rejections are all correct and
line-cited (the plan's real strength), but Item 4 rests on three factual errors, Item
5 is ~80% already shipped, and it misses one latent hazard and the entire
observability question. This design corrects the priorities, removes the dangerous
recommendation, and adds the monitoring the plan omitted. **Target score of *this*
design: 9.5 / 10**, contingent on the empirical validations named in §6.

---

### 2. Architecture Overview

```
 TradingView alert
      │  HTTPS POST /webhook
      ▼
 ┌──────────────────────────── webhook_receiver.py ─────────────────────────────┐
 │  validate → normalize → fsync raw-webhooks.jsonl (WAL)  → PROCESS_QUEUE       │
 │                                    │ dequeue                                  │
 │                                    ▼                                          │
 │            append signals.jsonl (dedupe ledger, written AFTER dequeue)        │
 │                                    │                                          │
 │                                    ▼                                          │
 │   ExecutionService.execute()  (src/execution/service.py)                     │
 │     gates: arming→mode→kill-switch→symbol-pause→idempotency                   │
 │     ORDER JOURNAL: record_order_state PLANNED→SUBMITTED (write-ahead)         │
 │                                    │                                          │
 │                                    ▼                                          │
 │                     CcxtExecutor.execute()  (executors/ccxt_adapter.py)       │
 │                                    │  create_order                            │
 └────────────────────────────────────┼─────────────────────────────────────────┘
                                       ▼
                                 ┌───────────┐
                                 │   VENUE   │  (OKX / Bybit / Binance / HL / Coinbase)
                                 └───────────┘
                                       ▲  read-only pulls
        ┌──────────────────────────────┼───────────────────────────────────────┐
        │ RECONCILE PATHS (all pull-based, no websocket, fail-open on read)     │
        │  a) post-submit   service.py:298  (HERMX_RECONCILE_ENABLED, default OFF)│
        │  b) startup       reconcile_startup / resolve_unknown_orders_once      │
        │  c) periodic      resolve_unknown_orders_once  → ORDER JOURNAL         │
        │  d) ledger feed   dashboard_model() → strategy_order_history_snapshot  │
        │                     → reconcile_from_order_history → closed-trades.jsonl│
        │  e) cron safety   deploy/hermes-scripts/hermx-ledger-reconcile.py      │
        │                     → GET /api → drives (d)                            │
        └───────────────────────────────────────────────────────────────────────┘
                                       │
                       read_closed_trades / aggregate_strategy_pnl
                                       ▼
                          /api  →  Next.js dashboard (React)

 DURABLE (survives Restart=always):
   raw-webhooks.jsonl  WAL, size-rotated (recovery source)
   signals.jsonl       dedupe ledger (correctness backstop on replay)
   ORDER_JOURNAL       per-order state transitions (append; latest-per-clOrdId wins)
   closed-trades.jsonl LIFETIME P&L ledger — append-only, NEVER pruned, dedupe on
                       (exchange, inst_id, ord_id, mode)
   control-state.json  strategy_overrides + accounting_windows
 EPHEMERAL (rebuilt after restart):
   PROCESS_QUEUE, _MODEL_CACHE, _OKX_LIVE_CACHE, _OKX_ORDER_HISTORY_CACHE,
   the per-poll `positions` signed-qty dict in reconcile_from_order_history
```

State lives in five append-oriented files; everything else is a cache. There is no
event bus, no position cache, and no portfolio manager — and this design adds none.

---

### 3. Prioritized Feature List

Six items. The plan proposed five; this rebalances them against the evidence, drops
Item 4's dangerous form, folds Item 5 (mostly already shipped) into a small P2, and
adds two gaps the plan missed (float-residue hardening; observability).

**P0 — Must Ship (silent-loss / silent-mis-accounting prevention)**

- **P0-1 — 100-row window age-out detector.** *What:* detect when an
  order-history snapshot may have dropped un-recorded closes. *Why:* the only open
  path to permanent silent P&L loss (`closed-trades.jsonl` depends on a 100-row read;
  a busy symbol can age a close out before any reconcile sees it). *Where:*
  `src/dashboard.py` (`strategy_order_history_snapshot`) + helper in
  `src/pnl_ledger.py`. *Effort:* S. *Risk if not done:* a real close is never
  ledgered; P&L is silently wrong with no signal.
- **P0-2 — `signed_qty` accumulator hardening.** *What:* snap-to-zero on the
  per-poll `positions` float, a single-unit tolerance on the flat test, and a
  log-only sign guard on detected closes. *Why:* `reconcile_from_order_history`'s
  position-delta close detector is load-bearing for spot / no-`reduceOnly` venues and
  is currently unguarded against float residue and sign flips. *Where:*
  `src/pnl_ledger.py:346-361`. *Effort:* S. *Risk if not done:* a phantom 1e-12
  position mis-detects (or misses) the next close → wrong ledger rows.

**P1 — Should Ship (robustness / operator confidence)**

- **P1-1 — Dual timestamps (`recorded_at_ms` = `ts_init`).** *What:* add a local
  observation timestamp beside venue `closed_at_ms` (`ts_event`); schema v2→v3,
  back-fill absent as `None` on read. *Why:* reconcile lag is currently unmeasurable
  and the P0-1 race is undiagnosable after the fact. Prerequisite for P1-2. *Where:*
  `src/pnl_ledger.py` (`_build_entry`, `SCHEMA_VERSION`, `read_closed_trades`).
  *Effort:* S.
- **P1-2 — Reconcile-lag & stuck-UNKNOWN observability.** *What:* a non-LLM monitor
  surfacing (a) max reconcile lag = `now − recorded_at_ms` vs `closed_at_ms`, (b)
  count of orders stuck `UNKNOWN` beyond `T`, (c) age-out detector fires from P0-1.
  *Why:* the plan makes reconcile correct but says nothing about how an operator
  *knows* when it silently fails. *Where:* `deploy/hermes-scripts/` (new cron gate)
  + a read-only `/api` field. *Effort:* S/M.

**P2 — Nice to Have (completeness / hygiene)**

- **P2-1 — Codify the fail-closed transition rule + a *non-position-affecting*
  archival state for genuinely-gone UNKNOWN.** The rescoped, de-fanged Item 4+5 (see
  §4.5). *Not* a transition to REJECTED. *Effort:* M. Touches `webhook_receiver.py`
  (shared — explicit confirmation required).
- **P2-2 — Delete the redundant literal reconcile feed** at `dashboard.py:1078`
  (`reconcile_from_order_history(rows, "okx", "demo")`). Safe only because it reads the
  demo account via an empty global config; a latent mis-stamp trap if that config ever
  carries a venue. *Effort:* S.

---

### 4. Detailed Technical Design

#### 4.1 P0-1 — 100-row window age-out detector

- **Problem statement.** `executor.get_order_history_raw(inst_ids, limit=100)`
  (`dashboard.py:930`, `:1071`) returns at most 100 rows. On a busy `(venue, mode)` a
  HermX close can be pushed out of the readable window before any reconcile pass folds
  it into `closed-trades.jsonl`. HermX mitigates with "reconcile often" (per-render +
  10-minute cron) but has **zero signal** when the mitigation loses the race. Nautilus
  solves the general form with a high-water cursor over paginated pulls
  (`engine/mod.rs` mass-status); HermX needs only the *detector*, not the pager.
- **Design.**
  - New helper in `pnl_ledger.py`:
    `max_recorded_closed_at(exchange: str, mode: str) -> int | None` — scans
    `read_closed_trades()` (already corrupt-tolerant) and returns the max
    `closed_at_ms` for that `(exchange, mode)`, or `None` if none.
  - In `strategy_order_history_snapshot` (`dashboard.py:906`), after the raw read and
    *before* the reconcile call, compute: `saturated = len(rows) >= 100`;
    `oldest = min(_row_ts(r) for r in rows)` (reuse `pnl_ledger._row_ts`);
    `high_water = max_recorded_closed_at(venue, mode_key)`.
  - If `saturated and high_water is not None and oldest > high_water`, the window's
    oldest row is *newer* than everything already ledgered for this environment →
    there is an unobserved gap between `high_water` and `oldest`. Emit
    `reconcile_alert_mismatch` (the existing `RECONCILE_ALERT_MISMATCH` channel) with
    `{stage: "history_window_ageout", venue, mode, oldest_ms, high_water_ms, gap_ms}`.
- **Algorithm (prose).** A saturated window whose oldest row post-dates our newest
  recorded close means at least one close occurred in `(high_water, oldest)` that we
  have no row for. That is exactly the silent-loss condition. One `min()`, one
  cached max, one comparison — no pagination, no new poll engine.
- **Error handling.** Detection is wrapped in the same `try/except` that already
  guards the reconcile (`dashboard.py:933-938`) so it can never fail the read-only
  snapshot. A missing `high_water` (fresh ledger) suppresses the alert (nothing to be
  behind). Fail-open: a detector exception logs and returns, never raises into
  `dashboard_model`.
- **Rollback plan.** Pure addition inside one `try`. Delete the helper + the block;
  no schema, no stored state.
- **Test plan.** `test_ageout_detector_fires_on_saturated_window_past_high_water`
  (100 rows all newer than ledger max → one alert); `test_no_ageout_when_window_unsaturated`
  (<100 rows → no alert); `test_no_ageout_when_oldest_row_overlaps_ledger`
  (oldest ≤ high_water → no alert); `test_ageout_detector_never_raises`
  (helper throws → snapshot still returns). All exercise production
  `strategy_order_history_snapshot`, not a re-implementation (per code-quality
  anti-pattern rule).
- **Nautilus reference.** Borrowed: the high-water/cursor *intuition* from
  `reconcile_execution_mass_status` (`engine/mod.rs:1728-1833`). Discarded: the
  paginating pull, the mass-status report objects, and the synthetic-fill
  reconstruction that follows a detected gap (`positions.rs:183-398`) — HermX detects
  and alerts; it never reconstructs.

#### 4.2 P0-2 — `signed_qty` accumulator hardening

- **Problem statement.** `reconcile_from_order_history` accumulates a per-instrument
  running position in a plain-float dict (`positions[inst_id] = prev + signed`,
  `pnl_ledger.py:353-355`) and classifies a close by `prev != 0.0 and opposite_side`
  (`:360-361`). Two unguarded failure modes: (1) float residue — successive
  add/subtract leaves e.g. `1e-13` instead of `0.0`, so `prev != 0.0` is spuriously
  true and the *next* same-direction fill is mis-classified as a close; (2) a single
  fill that flips the position through zero (short→long) is neither split nor flagged.
  Nautilus guards exactly these: it force-normalizes `signed_qty` to `0.0` when the
  fixed-point quantity is zero (`position.rs:370`, "Normalize"; rationale at
  `:545-546`), and re-baselines the average price on a sign flip (`:454-455`).
- **Design.** All changes local to the loop in `pnl_ledger.py:346-361`:
  - **Snap-to-zero + tolerance.** After `positions[inst_id] = prev + signed`, if
    `abs(positions[inst_id]) < QTY_EPS` (a module constant, e.g. `1e-9`, or derived
    from the row's quantity precision when available), set it to exactly `0.0`. Use the
    snapped value for the `opposite_side` / `prev != 0.0` test. This mirrors
    Nautilus's "zero test on the rounded quantity, then snap the float"
    (`position.rs:545-546`).
  - **Close-side sign guard (log-only).** When `is_close` is decided, assert-or-log
    that `sign(prev)` is opposite to the fill side; if not (e.g. a duplicated or
    mis-sorted row produced an impossible close), log a structured warning and
    continue. Never raise — the money path stays fail-open on observability
    (consistent with anti-pattern #3 in the plan and with Nautilus's own choice to
    make this a `debug_assert!`, i.e. dev-only: `position.rs:388-399`).
  - **Optional flip flag.** When a single fill crosses zero (`prev` and
    `prev + signed` have opposite signs and both non-zero), log a
    `position_sign_flip` note. HermX need not split the fill (it records one aggregate
    close row from cumulative `accFillSz`); it only needs the flip visible.
- **Error handling.** All additions are log-and-continue. A malformed row that would
  poison the accumulator is skipped by the existing `_as_float`/`_row_ts` tolerance;
  the guard only *reports* an anomaly.
- **Rollback plan.** Remove the constant + three inline checks; behavior reverts
  exactly. No schema, no stored state.
- **Test plan.** `test_float_residue_does_not_create_phantom_close` (open 3 lots in
  fractional legs that would leave float residue, then a same-side fill → must NOT be
  classified a close); `test_snap_to_zero_flattens_exactly` (equal-and-opposite fills
  → `positions[inst_id] is 0.0`); `test_sign_flip_is_logged_not_raised`
  (short→long single fill → warning captured, no exception, one close row);
  `test_mis_sorted_close_logs_guard`. Exercise production
  `reconcile_from_order_history`.
- **Nautilus reference.** Borrowed: snap-to-zero on the rounded quantity
  (`position.rs:370, 545-546`) and the side/sign consistency invariant
  (`:388-399`, adapted from `debug_assert!` to Python test-assert + prod log).
  Discarded: `peak_qty` high-water (`position.rs:74, 363-366`) and `duration_ns`
  (`:368-377`) — risk/return metrics for a Position object HermX does not have; the
  fixed-point `Money` PnL accumulation (`:743-749`) — HermX carries floats at its
  scale (anti-pattern #4).

#### 4.3 P1-1 — Dual timestamps (`recorded_at_ms`)

- **Problem statement.** Ledger rows record only venue event time (`closed_at_ms`
  from `uTime`/`cTime` via `_row_ts`, `pnl_ledger.py:317`), which is Nautilus's
  `ts_event`. There is no local observation time (`ts_init`), so reconcile lag is
  unmeasurable and the P0-1 race cannot be reconstructed after it fires. Note
  `_row_ts` returns `0` when the venue supplies no timestamp — so `closed_at_ms`
  alone is an unreliable ordering/freshness key; a local `recorded_at_ms` is always
  present.
- **Design.** In `_build_entry`, add `"recorded_at_ms": <local ms>` and bump
  `SCHEMA_VERSION` 2→3. The value is the writer's wall clock at reconcile time (this
  is genuinely `ts_init`, not a freshness measure — freshness stays bounded on bar
  time per CLAUDE.md). `read_closed_trades` back-fills absent `recorded_at_ms` as
  `None` on read — identical pattern to the existing v1 `net_realized_pnl` back-fill
  (`pnl_ledger.py:159-164`), so a v1/v2 ledger reads cleanly and the change is
  rollback-safe and append-only. **Never persisted on read** (no file mutation).
- **Error handling.** A `None` back-fill is a first-class value everywhere it is
  consumed (lag = `None` when unknown, never an error). The append path already
  fsyncs under lock (`append_closed_trades:266-274`); adding one field changes
  nothing there.
- **Rollback plan.** Drop the field and the reader tolerates it (absent → `None`).
  Because the ledger is append-only and never pruned, old rows without the field
  coexist forever with new rows that have it — the reader must not assume presence.
- **Test plan.** `test_recorded_at_ms_written_on_new_rows`;
  `test_v2_rows_backfill_recorded_at_none_on_read`;
  `test_schema_v3_reader_reads_v1_v2_v3_mixed_ledger` (the rollback-safety guard
  named in the plan's Verification step 5).
- **Nautilus reference.** Borrowed: the `ts_event` vs `ts_init` split
  (`events/order/filled.rs:75-78`) — event-occurred vs object-initialized. Edge case
  we adopt from Nautilus: `ts_event` can be missing/backfilled from other events
  (`orders/mod.rs:986-989` uses fill `ts_event` to stand in for a never-seen accept
  time), which is precisely why HermX needs an *independent* local `ts_init` rather
  than trusting the venue timestamp for ordering. Discarded: nanosecond precision and
  the `UnixNanos` type — HermX uses ms.

#### 4.4 P1-2 — Reconcile-lag & stuck-UNKNOWN observability

- **Problem statement.** The plan makes reconciliation correct but is silent on
  detection: an operator has no surface that says "reconcile is falling behind" or
  "N orders have been UNKNOWN for an hour." The existing `resolve_unknown_orders_once`
  already alerts on a *single* order crossing `UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS`
  (`webhook_receiver.py:2504-2544`), but there is no aggregate health view and no lag
  metric.
- **Design.** A non-LLM Hermes cron gate (mirroring
  `hermx-ledger-reconcile.py` and the absence-detection pattern already established in
  code-quality memory) that reads `/api` and the order journal and emits, via the
  existing gate library, three derived signals:
  - `reconcile_lag_ms` = `max(now − recorded_at_ms)` over the most recent ledger rows
    (needs P1-1); alert if it exceeds a window (e.g. > 2× the cron cadence).
  - `stuck_unknown_count` = orders in the journal still `UNKNOWN`/`SUBMITTED` with
    origin age > `T` (reuse `load_open_orders` + `_order_age_seconds`).
  - `ageout_fires` = count of P0-1 alerts in the window.
  Surface the same three as read-only fields on `/api` for the dashboard. No new env
  flag (Flag Dependencies constraint); it is a non-LLM job, so **no `--provider`
  /`--model` pin** is required (per the installer/design rule).
- **Error handling.** Fail-open like the existing ledger cron — an unreachable
  dashboard is the health watchdog's concern (`hermx-ledger-reconcile.py:44-48`),
  never a false trading signal. Uses the atomic sidecar + suppression-window
  primitives already in `hermx_gate_lib.py`; no new primitive (absence-detection
  memory: the lib touch is at most a suppression key).
- **Rollback plan.** Remove the cron registration in `install-cron-monitors.sh` and
  the `/api` fields; nothing stored beyond the gate's own sidecar.
- **Test plan.** `test_reconcile_lag_computed_from_recorded_at`;
  `test_stuck_unknown_count_matches_journal`;
  `test_monitor_fail_open_on_unreachable_dashboard`;
  `test_monitor_calls_real_gate_lib` (must call production `run_gate`/`evaluate`, not
  re-implement — code-quality anti-pattern).
- **Nautilus reference.** None direct — Nautilus emits events on a bus that a risk
  engine subscribes to; HermX has no bus, so observability is a periodic pull gate.
  This is a deliberate HermX-native divergence (see §6).

#### 4.5 P2-1 — Codified transition rule + non-position archival state (the corrected Item 4+5)

- **Problem statement — and correction of the plan.** The plan's Item 4 asserts
  "UNKNOWN is a terminal black hole… no exit from UNKNOWN," and prescribes forcing a
  stuck order to **REJECTED**. **All three factual premises are wrong, and the
  prescription is dangerous:**
  1. UNKNOWN is *not* a black hole. The transition table already permits
     `UNKNOWN → {FILLED, REJECTED, UNKNOWN}` (`webhook_receiver.py:1539`), it is
     tested (`test_order_state_machine.py:37-38`), and `resolve_unknown_orders_once`
     actively drives `UNKNOWN → terminal` whenever the venue confirms an outcome
     (`webhook_receiver.py:2558`).
  2. Nautilus does **not** resolve venue-absent orders to a terminal state "after
     bounded attempts." Its Rust mass-status reconciler is strictly report-driven and
     **leaves a locally-open, venue-absent order untouched** (`engine/mod.rs:1728-1833`);
     `create_reconciliation_rejected` fires *only* from a venue-reported
     `OrderStatus::Rejected` (`orders.rs:167-168`), never from silence. The Python
     engine's old "infer canceled on absence" behavior was **dropped** in the Rust
     rewrite. HermX's "absence → stay UNKNOWN + alert + pause, never auto-terminal"
     (`map_order_outcome` docstring `:1998-2003`; `resolve_unknown_orders_once:2504`)
     is therefore *aligned* with current Nautilus, not behind it. Forcing REJECTED
     would **diverge** from Nautilus and re-introduce the "drop a live position as
     flat" bug the code deliberately prevents.
  3. A stuck UNKNOWN does **not** trap the operator. Symbol pause is bypassed for
     close-only records (`service.py:154`), so the operator can always flatten; only
     *new opens* on that symbol are blocked, which is the intended safety behavior.
- **Design (the genuine, narrow gap).** The only real deficiency is that a
  genuinely-gone UNKNOWN never *clears* — it alerts and pauses new opens indefinitely.
  The safe resolution is an archival, **non-position-affecting** terminal state, e.g.
  `ABANDONED`, that:
  - is reached only after `N` resolver attempts *and* `T` elapsed with the venue
    consistently reporting `not_found` (never on a single miss, never on a query
    error);
  - records the journal as closed-for-tracking and un-pauses new opens;
  - **explicitly asserts no fill and no rejection** — it does not feed position or
    P&L math, and it is not `REJECTED`. It is "we give up tracking this," not "this
    did not fill."
  Add `ABANDONED` to `_ORDER_STATE_TRANSITIONS` as a *deliberate* new edge
  (`UNKNOWN → ABANDONED`), keep terminal-frozen semantics, and extend the existing
  exhaustive matrix test.
- **Note on Item 5.** The plan's Item 5 ("audit + codify the whitelist, add a
  table-driven test") is ~80% already done: `test_transition_matrix_is_exhaustive`
  (`test_order_state_machine.py:92-103`) already asserts the full legal/illegal
  matrix, the table is fail-closed (`_ORDER_STATE_TRANSITIONS.get(old, frozenset())`
  → unknown state ⇒ all-False, tested with a `"bogus"` state), and enforcement is
  tested (`test_record_order_state_rejects_illegal:106`). The only new work is the
  `ABANDONED` edge above and a one-line rule in code-quality.md.
- **Error handling / Rollback.** New state is additive and terminal; absent edge ⇒
  behavior is exactly today's (indefinite UNKNOWN). Journal is append-only; old
  UNKNOWN rows are unaffected.
- **Test plan.** `test_unknown_to_abandoned_only_after_N_and_T`;
  `test_abandoned_requires_consistent_not_found_not_query_error`;
  `test_abandoned_does_not_write_pnl_row`;
  `test_abandoned_unpauses_new_opens`;
  extend `test_transition_matrix_is_exhaustive` with the `ABANDONED` edges.
- **Shared-file flag.** This touches `src/webhook_receiver.py` — **per dev-rules,
  explicit operator confirmation is required before editing.** Additive keys/edges
  only; no change to existing transition semantics.
- **Nautilus reference.** Confirmed (not borrowed): report-driven-only handling
  (`engine/mod.rs:1728-1833`), venue-reported-only rejection (`orders.rs:167-168`),
  and the one real terminal exit `Canceled → Filled` (`orders/mod.rs:244`) which
  parallels HermX's `UNKNOWN → FILLED`. Discarded: any absence-triggered terminal.

---

### 5. Execution Plan

| Item | Files | Effort | Depends on | Target |
|------|-------|--------|-----------|--------|
| P0-1 age-out detector | `dashboard.py`, `pnl_ledger.py` | S | — | Sprint 1 |
| P0-2 signed_qty hardening | `pnl_ledger.py` | S | — | Sprint 1 |
| P1-1 dual timestamps (v3) | `pnl_ledger.py` | S | — | Sprint 1 |
| P1-2 observability monitor | `deploy/hermes-scripts/*`, `dashboard.py` (`/api`) | S/M | P1-1 | Sprint 2 |
| P2-1 ABANDONED + codify | `webhook_receiver.py`*, `tests/` | M | matrix test (exists) | Sprint 2/3 |
| P2-2 delete literal feed | `dashboard.py` | S | — | Sprint 2 |

`*` shared file — explicit confirmation gate (dev-rules; PNL master Open Decision 1).

**Critical path.** P1-1 (timestamps) blocks P1-2 (the monitor consumes
`recorded_at_ms`). P0-1 and P0-2 are independent and can ship first, in either order,
in one commit each. P2-1 depends on nothing technically but is gated on the empirical
UNKNOWN census (§6) *and* operator sign-off for the shared-file edit — do not build it
before both. P2-2 is independent and can ride with any dashboard commit.

**Ordering rule.** Nothing in P0/P1 touches `webhook_receiver.py` — deliberately, so
the money-safety-critical, silent-loss items ship without the shared-file confirmation
gate. Only P2-1 crosses that boundary.

---

### 6. Risk Register

| Risk | Prob. | Impact | Mitigation | Owner |
|------|-------|--------|-----------|-------|
| **`ord_id` not unique-per-close on some venue** (venue reuses/omits order ids on partial fills). If false, the ledger dedupe key `(exchange, inst_id, ord_id, mode)` has a hole and P0-1/P0-2 rest on sand. | Low–Med (venue-specific) | High | **Empirically confirm `ord_id` uniqueness per enabled venue (OKX first) before trusting the ledger.** Nautilus makes per-fill IDs unique by hashing `ts_event+qty+px` (`ids.rs:144-161`) precisely because one order has many discrete fills — HermX collapses to one aggregate row from cumulative `accFillSz`, so the concern is *cross-order* id reuse, not partial-fill reuse. | Operator |
| **Age-out race never actually approaches the 100-row window** → P0-1 detector is solving a non-problem. | Med | Low (cheap detector) | Detector is ~5 lines; measure real lag on a busy demo symbol over a day. Build pagination only if the detector fires (Open Decision 6). | Dev |
| **P0-2 `QTY_EPS` mis-tuned** — too large hides a real small position; too small doesn't absorb residue. | Low | Med | Derive tolerance from the row's quantity precision where available (mirror Nautilus `is_within_single_unit_tolerance`, `orders.rs:899`), not a blind constant; test both directions. | Dev |
| **P2-1 `ABANDONED` mis-fires on a transient not_found** and clears an order that actually filled. | Low | High | Require `N` attempts *and* `T` elapsed *and* consistent `not_found` (never a query error); `ABANDONED` writes no P&L/position row, so even a wrong fire cannot corrupt money math — it only stops tracking. | Dev + Operator |
| **Shared-file regression** — P2-1 edit to `webhook_receiver.py` breaks intake. | Low | High | Additive edges/keys only; explicit confirmation gate; the exhaustive matrix test already exists as a regression guard. | Operator |
| **Redundant literal feed (P2-2) becomes a live mis-stamp** if `_dashboard_executor(config)` ever resolves a non-OKX/live venue from a populated config. | Low today | Med | Delete it; it is safe now only because `shadow_config()` returns `{}`. Until deleted, it is a latent trap, not an active bug. | Dev |
| **f64 money math** accumulates error if a future edit sums running P&L in a hot loop and compares for equality. | Low | Med | Keep money to add-and-store, compare with tolerance (anti-pattern #4); Nautilus stores fixed-point `Money` (`position.rs:743-749`) — do not widen HermX float math. | Dev |
| **Observability monitor (P1-2) false-alarms** on normal cron cadence, training operators to ignore it. | Med | Med | Threshold lag at ≥ 2× cadence; reuse gate-lib suppression window; alert on sustained, not single-tick, breach. | Dev |

**What requires empirical validation before shipping (not assumption):**
1. `ord_id` uniqueness per enabled venue (gates the entire ledger correctness claim).
2. Real reconcile lag vs the 100-row window on a busy symbol (gates P0-1 pagination,
   not the detector).
3. A one-week census of how orders reach and stay UNKNOWN in production (sizes P2-1's
   `N`/`T` and confirms the `not_found`-consistency predicate).
4. Whether the `dashboard.py:1287` legacy path ever executes with non-empty config
   (confirms P2-2 is a pure cleanup vs a latent live bug).

**Where HermX deliberately diverges from Nautilus (Nautilus is a reference, not an
oracle):** no local cache (re-read every poll — sidesteps `Rc<RefCell<Cache>>` and the
entire three-case mismatch machinery); no event bus (one consumer — the dashboard —
reads one file); no synthetic fills or back-solved prices (log-and-skip a close that
isn't in the venue read, never fabricate — `positions.rs:803-858` is the exact code we
refuse to port); float money at HermX's scale; and observability by periodic pull gate
rather than bus subscription.

**HermX-specific concerns not in Nautilus at all** (covered elsewhere, noted here for
completeness): TradingView webhook latency and the `raw-webhooks.jsonl` WAL,
alert dedupe via `signals.jsonl`, signal normalization / `tv_time` freshness bounding,
and `mxc`/`operator_close_` client-id attribution. Nautilus has no analog; these are
governed by the existing PNL Master Plan and CLAUDE.md proven patterns, and this
design changes none of them.

---

### 7. Appendix: Nautilus Reference Mapping

| Nautilus concept | File:line | HermX equivalent | Verdict | Reason |
|------------------|-----------|------------------|---------|--------|
| Inferred-fill deterministic `trade_id` | `ids.rs:71` (`create_inferred_reconciliation_trade_id`), hash of 11 fields incl. `ts_last` | `ord_id` dedupe, `(exchange,inst_id,ord_id,mode)` (`pnl_ledger.py:96`) | **REJECT** | Exists only to id a *manufactured* fill (docstring `ids.rs:63-68`). HermX reads real rows with real `ordId`; never infers fills. |
| Per-fill synthetic IDs (`ts_event+qty+px` hash) | `ids.rs:144-161` | — | **REJECT** | Needed because one order has many discrete fills; HermX collapses to one aggregate row from `accFillSz`. |
| Report-driven mass-status reconcile; absent order untouched | `engine/mod.rs:1728-1833` | `resolve_unknown_orders_once` leaves absent orders UNKNOWN (`webhook_receiver.py:2504`) | **CONFIRM** (already aligned) | Rust engine never infers state from venue silence; HermX already matches. Overturns plan Item 4. |
| `create_reconciliation_rejected` | `orders.rs:466`, called only from venue-`Rejected` arm `:167-168` | `map_order_outcome`: only `canceled+zero-fill` ⇒ REJECTED (`webhook_receiver.py:2018-2021`) | **CONFIRM** | Neither system rejects on absence. HermX's "absence ⇒ UNKNOWN" is correct. |
| 3-case fill-qty mismatch | `orders.rs:886` (`<`:895 → None; `>`:918 → inferred fill; `=`:963 → updated) | none (no local fill cache) | **REJECT** | All three compare local `filled_qty()` to venue; HermX keeps no per-order filled qty. |
| Back-solved incremental fill price | `orders.rs:995-1053`; position variant `positions.rs:803-858`; dust cap `:464-473` | none — log-and-skip | **REJECT (anti-pattern)** | Money invented from a weighted-average inversion. HermX must never fabricate a priced fill. |
| `should_reconciliation_update` / `is_order_updated` drift detector | `orders.rs:420`; `reports/order.rs:327-344` (price, trigger, qty; None=not-reported) | post-submit state compare (`service.py:337-350`) | **DEFER** | Market-order-dominant HermX has little price/trigger drift; qty already collapses. |
| `report_is_confirmed_state` whitelist | `orders.rs:862` | `_ORDER_STATE_TRANSITIONS` fail-closed table (`webhook_receiver.py:1535-1548`) | **ADOPT (done)** | Same fail-closed philosophy; HermX table already exhaustively tested. |
| Fail-closed transition wildcard | `orders/mod.rs:276` (`_ => InvalidStateTransition`) | `.get(old, frozenset())` ⇒ unknown ⇒ reject | **ADOPT (done)** | Identical "unknown pair ⇒ reject, never guess." |
| `OrderStatus` enum (14, no UNKNOWN) | `enums.rs:1416-1445`; terminal `is_closed` `:1463` | 5 states incl. HermX-specific UNKNOWN (`webhook_receiver.py:200-208`) | **ADAPT (harden, don't grow)** | Nautilus has no "unresolvable" status; HermX's UNKNOWN is proportionate. Add only `ABANDONED` (P2-1). |
| One real terminal exit `Canceled → Filled` | `orders/mod.rs:244` | `UNKNOWN → FILLED` (`webhook_receiver.py:1539`) | **CONFIRM** | Both allow a "closed-looking" order to still receive a fill. |
| `signed_qty` + side/sign invariant | `position.rs:72, 388-399` (`debug_assert!`) | per-poll `positions` float dict (`pnl_ledger.py:346-361`) | **ADOPT** (P0-2) | Cheap guard on the load-bearing close detector; adapt dev-only assert → prod log. |
| Snap-to-zero on flat | `position.rs:370, 545-546` | none | **ADOPT** (P0-2) | Prevents phantom float-residue positions. |
| `peak_qty`, `duration_ns` | `position.rs:74, 363-377` | none | **REJECT** | Risk/return metrics for a Position object HermX lacks. |
| Fixed-point `Money` PnL | `position.rs:743-749` | float `_as_float` | **REJECT (tolerate)** | Acceptable at HermX scale; don't widen (anti-pattern #4). |
| `ts_event` vs `ts_init` | `events/order/filled.rs:75-78`; backfill `orders/mod.rs:986-989` | only `closed_at_ms` (`ts_event`), `_row_ts` → 0 if absent | **ADOPT** (P1-1) | Add local `recorded_at_ms`; venue ts alone is an unreliable ordering key. |
| Netting position id `{instrument}-{strategy}` | `engine/mod.rs:2913-2915` | attribute via `cl_ord_id` prefix (`is_hermx_cl_ord_id`) | **REJECT** | Keys a Position/OMS cache HermX does not have. |
| Position-report divergence = log-only warn | `positions.rs:410-445` | — | **ADOPT-lite** (folded into P0-2 log guard) | Nautilus itself only *warns* on divergence at runtime; HermX's log-and-continue is the same posture. |
| Event/Report split on a bus | `reports/*`, engine bus | `closed-trades.jsonl` is the event log; venue read is the report | **DEFER** | Pays off with ≥2 bus subscribers; HermX has one (the dashboard). |

*End of design document. Supersedes `docs/NAUTILUS_EVOLUTION_PLAN.md` where they
disagree — specifically Item 4, which this document overturns with cited evidence.*
