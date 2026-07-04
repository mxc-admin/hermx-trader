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

## 0. Post-Refactor Revalidation (2026-07-04)

> Addendum — **supersedes stale file:line specifics in §§1-3 below; the verdicts
> themselves all survive.** The in-flight refactor flagged in the header has landed
> and went further than anticipated: `src/webhook_receiver.py` (now ~2,100 lines,
> largely a re-export/composition shim) was split into
> `src/reconcile/{orders,unknown_resolver,executor_select,alerts,drift}.py`,
> `src/orders/journal.py`, `src/control_state.py`, `src/webhook/`, `src/signals/`,
> `src/strategy/`, `src/advisor.py`, `src/alerts.py`; dashboard snapshot/enrich
> helpers moved to `src/dashboard/snapshots.py`. Every symbol referenced by the
> accepted and rejected items was re-located **by name** in the current tree.
> Nothing was accidentally fixed by the refactor; one deliberate behavior change
> was found (`map_order_outcome` — §0.4.2).

### 0.1 Fresh test baseline

- **Full suite: 810 passed / 28 skipped / 0 failed** (2m45s, `.venv/bin/python -m pytest -q tests/`).
- The 7 suites cited in the header: **225 passed / 0 failed** (was 203 at
  `be3c0c66` — +22 tests, no regressions).
- New-module coverage is green inside the full run (`test_order_journal_checkpoint`,
  `test_receiver_reconcile_venue`, `test_reconciliation_observe_only`,
  `test_unknown_resolver_controls`, `test_replay_startup`, `test_operator_close`).
- Working-tree note: an uncommitted `pytest.ini` diff adds a `slow` marker
  (`tests/test_docker_state.py` / `test_phase3_worker_pool.py` also touched) —
  unrelated to this plan; baseline was taken with it in place.

### 0.2 Accepted items — per-item revalidation

**Item A (balance check + sub-min outcome) — STILL VALID. Targets unchanged; one new wiring constraint; one uncertainty closed.**
- Finding fully intact: **zero** balance references on the submit path
  (`src/execution/service.py`, adapter execute path, `webhook_receiver.py`,
  `src/webhook/*`); `check_balance_drift`/`get_balance_summary` remain observe-only
  and uncalled. Sub-min still collapses into `zero_size`
  (`_contracts_for_notional` `ccxt_adapter.py:618-628`, floor at `:626-627`;
  `_amount_from_readiness` `:630-644`). `limits.cost.min` (min-notional) still
  unconsulted — `_market_spec` (`:431-457`) reads only `limits.amount.min` (`:449`).
- Current anchors: open-leg `create_order` still `ccxt_adapter.py:863`; skipped-leg
  contract `{submitted: False, status: "skipped", reason: ...}` (zero-size open
  `:847-852`); all-legs-skip → `mode="submit_failed"` (`:943-945`) → journal
  REJECTED (`service.py:343-352`, `SUBMITTED→REJECTED` prev_state `:382`) — the
  outcome-mapping constraint holds. A1b block moved to **`service.py:402-418`**;
  its pins are still `tests/test_phase_a_robustness.py:238,254`.
- **New design constraint:** `service.py` now reaches `emit_reconcile_alert` via
  the hooks dict (`self._h("emit_reconcile_alert")`, `:415`; registered at
  `webhook_receiver.py:1119`); the implementation moved to
  `src/reconcile/alerts.py:30`. Item A's alert extension stays inside the existing
  hook call — do NOT import `reconcile.alerts` directly from `service.py`.
- **Uncertainty closed:** `_market_spec` retains the raw market dict
  (`"market": market`, `ccxt_adapter.py:452`), so
  `market_spec["market"].get("settle")` works exactly as designed.
- Files: unchanged — `src/executors/ccxt_adapter.py`, `src/execution/service.py`, 2 test files.

**Item B1 (balance-drift wiring) — STILL VALID. Targets moved; the refactor built half the seam.**
- `check_balance_drift` still `ccxt_adapter.py:256-310`, still a hard no-op unless
  `mode=="live"` (`:267`), still **zero production callers** (repo grep across
  `src/`, `scripts/`, `deploy/`, cron installers: definition +
  `test_phase_b_robustness.py` only). Inert-monitor finding intact.
- Resolver moved: `unknown_resolver_loop` → **`src/reconcile/unknown_resolver.py:317`**;
  cadence const `UNKNOWN_RESOLVER_INTERVAL_SECONDS` stays `webhook_receiver.py:433`
  (=30.0), read lazily as a `_wr.` attribute (so env/monkeypatch overrides land).
- **Seam half-built by the refactor:** `src/reconcile/executor_select.py:81-87`
  (`_executor_for_order`) already caches executors keyed by exactly the
  `(venue, simulated_trading)` tuple the plan wants. What is still missing is the
  **domain**: it enumerates from open-order journal intents (empty with zero open
  orders), not from active strategy configs. The raw materials exist un-assembled:
  `strategy_instrument()` (`src/strategy/records.py:32`, exchange default at
  `:47`) + per-strategy `execution_mode` with control-state override
  (`src/strategy/readiness.py:55,62-65`). The enumerator's natural home is now
  **`src/reconcile/executor_select.py`**, NOT `webhook_receiver.py` as §2 said.
- Equity-assembler gap unchanged: `aggregate_strategy_pnl` (`pnl_ledger.py:423`)
  is per-strategy with caller-supplied `budget_usd`/`open_upl_usd`; sole
  production caller `dashboard.py:932-934`. No per-(venue,mode) equity exists.
- Files (B1): `src/reconcile/executor_select.py` (enumerator),
  `src/reconcile/unknown_resolver.py` (throttled call), `src/pnl_ledger.py`
  (equity helper), 1 test file. That is 3 source files — at the dev-rules limit;
  the §2 cron-gate alternative call-site remains valid and would drop
  `unknown_resolver.py` from the set.

**Item B2 (position drift) — STILL VALID as posed; the design decision is still unmade.**
- `journal_positions` still has **no producer** anywhere (whole-repo grep:
  parameter name + docstrings only). `reconcile_position_drift` moved to
  `src/reconcile/drift.py:18` (delegates to `detect_position_drift`,
  `ccxt_adapter.py:210`), still zero callers. Option 1 vs Option 2 decision
  unchanged. Option 1 got marginally cheaper: `_strategy_venue`/`_venue_symbols`
  (`src/dashboard/snapshots.py:175,191`) are now reference implementations for
  "which inst_ids do active strategies claim".

**Item C1 (dashboard realized-pnl read) — STILL VALID. Target moved; fix unchanged; one supporting nuance.**
- `enrich_close_rows_with_okx_history` → **`src/dashboard/snapshots.py:641`**
  (was `dashboard.py:1299`); the offending read is now **`snapshots.py:688`**:
  `"realized_pnl": _dash.as_float(hist.get("pnl"))` — raw OKX field, normalized
  `realized_pnl` ignored. `_normalized_realized_pnl` unchanged
  (`ccxt_adapter.py:43-63`) and adapter history rows DO carry `realized_pnl`
  (`ccxt_adapter.py:1058`), so the one-line preference fix stands.
- Nuance strengthening the fix: the refactor added a skip-guard at
  `snapshots.py:650` (`row.get("realized_pnl") is not None → continue`) — the
  code already expects the normalized key on these rows.
- Files: **`src/dashboard/snapshots.py`** (was `dashboard.py`), 1 test file.

**Item C2 (partial-fill journal detail parity) — STILL VALID. Both targets moved into `src/reconcile/` and the fix got cheaper.**
- Parity reference intact: post-submit path persists `acc_fill_sz`/`avg_px` in
  `detail["reconcile"]` (`service.py:449-459`).
- Startup reconcile detail dict — **`src/reconcile/orders.py:260`** — still only
  `{startup_reconcile, reason, source}`. Resolver terminal detail dicts —
  **`src/reconcile/unknown_resolver.py:260-266` and `:288-294`** — still only
  `{unknown_resolver, reason, source, attempts, elapsed_s}`.
- Cheaper now: `reconcile_order_once` (`reconcile/orders.py:150-160`) already
  returns `acc_fill_sz`/`avg_px` in the `outcome` dict in scope at every one of
  those call sites — the fix is adding two keys from a local variable, zero plumbing.
- Files: `src/reconcile/orders.py`, `src/reconcile/unknown_resolver.py`
  (no longer touches `webhook_receiver.py`), 2 test files.

### 0.3 Rejected items — re-confirmations (one line each)

- **Gap 3 (derived realized-PnL): rejection HOLDS.** `pnl_source` zero hits; the
  running-position dict is still `{inst_id: signed qty}` with no entry price
  (`pnl_ledger.py:645`); None-pnl rows still aggregate as $0 while counted
  (`:447`, `:462` — aggregation now reads the durable ledger via
  `read_closed_trades`, same behavior).
- **Gap 4 (100-row ageout recovery): rejection HOLDS.** `get_order_history_archive`
  still the same `fetch_closed_orders` endpoint with a bigger limit, no
  `since`/pagination (`ccxt_adapter.py:1072-1076`); `fetch_my_trades` zero hits;
  detector now `snapshots.py:316` with `limit=100` at `:341,:535`;
  `reconcile_from_order_history` still called ONLY from dashboard snapshot code
  (`snapshots.py:372-374`, `:546-549`) — the dashboard-only-reconciler
  architectural question stands, unresolved.
- **Gap 5 (PARTIALLY_FILLED state): rejection HOLDS and is STRONGER.**
  `_ORDER_STATE_TRANSITIONS` (now `src/orders/journal.py:79-86`) still the 9-edge
  set with no new state; `map_order_outcome` (now `src/reconcile/orders.py:52`)
  still maps `partially_filled → (FILLED, partial=True)`; market-only confirmed
  (`ccxt_adapter.py:752`, zero `timeInForce` hits); see §0.4.2 for the mapping
  change that further reduces the value of a new state.
- **FillReport seam: rejection HOLDS.** Zero
  dataclass/TypedDict/NamedTuple in `pnl_ledger.py`/`executors/`; the reduceOnly
  string-quirk still exists 3× — `ccxt_adapter.py:73`, `pnl_ledger.py:666`, and
  `snapshots.py:663` (via `bool_text`, `:628`) — the third copy moved from
  `dashboard.py` to `snapshots.py`.

### 0.4 New findings from the refactor itself (money-path relevant)

1. **`RECONCILE_STARTUP_COMPLETE`/`RECONCILE_STARTUP_AT` cross-module mutation is
   SAFE as built.** Defined `webhook_receiver.py:447-448`; mutated by
   `reconcile_startup` via attribute assignment on the imported module object
   (`_wr.RECONCILE_STARTUP_COMPLETE = True`, `reconcile/orders.py:277-278`); read
   as bare module globals in `log_execution_arm_state` (`webhook_receiver.py:2006`)
   and as `wr.` attributes in tests. Crucially they are **never value-re-exported**
   (excluded from the shim block `webhook_receiver.py:246-266` and
   `reconcile/__init__.py`), so there is exactly one binding and no stale-copy
   risk. **Guard to preserve:** never add these two names to any
   `from ... import` re-export — that would silently freeze them.
2. **`map_order_outcome` changed semantics since this plan was written**
   (money-safety, Nautilus-aligned report-driven reconcile):
   `not_found → UNKNOWN` (was REJECTED), and `canceled` with a missing/malformed
   `acc_fill_sz → UNKNOWN` (was REJECTED via coerced 0). This supersedes §1
   Gap 5(a)'s description of the mapping. It does not affect any accepted item's
   design (Item A's skip path maps through `submit_failed`, not through
   reconcile), and it strengthens the Gap-5 rejection.
3. **No shim behavior regressions found.** `emit_reconcile_alert` has a single
   implementation (`reconcile/alerts.py:30`); all former callers route to it
   (several via the `_wr.` seam, preserving test monkeypatchability); the service
   reaches it via an injected hook.

### 0.5 Key changes to implement

- **A:** add a live-mode-only, open-leg-only free-balance check in the CCXT
  adapter that skips the open leg with reason `insufficient_balance` when the free
  settlement-currency balance cannot cover notional/leverage, failing open on
  fetch errors; extend the service's existing zero-size alert block to alert
  distinctly on it; in a separate commit, give "below instrument minimum" its own
  reason and start consulting the venue's min-notional (`limits.cost.min`) limit.
- **B1:** enumerate active (venue, mode) pairs from loaded strategy files,
  assemble a per-pair synthetic equity figure (budgets + closed net P&L, live UPL
  best-effort), and call the existing balance-drift check from the resolver loop
  every Nth tick, throttled and fail-open.
- **B2:** make the Option 1 vs Option 2 decision first; recommended Option 1 =
  alert on venue positions that no active strategy claims (no believed-position
  bookkeeping needed). If unfunded, annotate or delete the dead drift functions.
- **C1:** in the dashboard close-row enricher, prefer the adapter-normalized
  `realized_pnl` field over the raw OKX `pnl` field.
- **C2:** add `acc_fill_sz`/`avg_px` (already present in the in-scope reconcile
  outcome) to the journal detail dicts at the startup-reconcile and the two
  resolver terminal-write sites.

### 0.6 Ordered execution plan (implementation-ready, NOT implemented)

Priority order = validated value; C1/C2 are near-zero-risk quick wins that may be
shipped first if a release is imminent. Each item stays an independent ≤3-source-file
task, tests-first, extending (never rewriting) pinned tests.

1. **Item A — pre-trade balance check** (2 commits)
   1. Tests first: extend `tests/test_ccxt_adapter.py` +
      `tests/test_phase_a_robustness.py` with the six tests from §2 Item A step 1
      (open-leg skip on insufficient free balance in live mode; close leg /
      `close_only` submits with zero balance; fetch-`None` fail-open;
      `simulated_trading=True` skips the check; non-USDT settle currency fetched;
      service emits distinct-stage alert when all legs skip for that reason).
   2. Adapter: add `_sufficient_free_balance(client, market_spec, notional, leverage)`
      to `src/executors/ccxt_adapter.py`; call it in the OPEN branch of
      `CcxtExecutor.execute` immediately before the open-leg `create_order`
      (currently `:863`); settle currency via
      `market_spec["market"].get("settle") or "USDT"` (retained dict verified,
      `:452`); skip result uses the existing
      `{submitted: False, status: "skipped", reason: "insufficient_balance"}`
      leg contract so all-legs-skip flows through `mode="submit_failed"` →
      `SUBMITTED→REJECTED` (`service.py:343-352,:382`).
   3. Service: extend the A1b block (`service.py:402-418`) to alert on
      `reason in {"zero_size", "insufficient_balance"}` with distinct stages, via
      the existing `self._h("emit_reconcile_alert")` hook — no new hook, no new
      import. Keep the pinned `zero_size` behavior
      (`test_phase_a_robustness.py:238,254`) untouched.
   4. Separate commit — sub-min disambiguation: thread `below_instrument_min`
      through `_contracts_for_notional`/`_amount_from_readiness`
      (`ccxt_adapter.py:618-644`) for the `min_amount` floor case; consult
      `limits.cost.min` in `_market_spec` (`:449` area). Regression tests:
      `test_below_instrument_min_reason_distinct_from_zero_size`,
      `test_min_cost_limit_floors_to_zero_with_reason`.
2. **Item C1 — normalized realized-pnl in enriched close rows** (one-liner)
   1. Test first: HL-shaped history row (`closedPnl` → normalized `realized_pnl`)
      enriches with the value; OKX row without `realized_pnl` still falls back to
      `pnl` (extend the dashboard snapshot tests, e.g.
      `tests/test_phase4_dashboard.py`).
   2. Fix: `src/dashboard/snapshots.py:688` — prefer `hist.get("realized_pnl")`,
      fall back to `hist.get("pnl")`.
3. **Item C2 — partial-fill journal detail parity** (two files, additive keys)
   1. Tests first: startup reconcile of a venue-`partially_filled` order →
      journal detail carries `acc_fill_sz`/`avg_px` (extend
      `tests/test_reconciliation_observe_only.py`); resolver resolves a
      venue-`partially_filled` order → same (extend
      `tests/test_unknown_resolver_controls.py`).
   2. Fix: add `"acc_fill_sz": outcome["acc_fill_sz"], "avg_px": outcome["avg_px"]`
      to the detail dicts at `src/reconcile/orders.py:260` and
      `src/reconcile/unknown_resolver.py:260-266` + `:288-294`.
4. **Item B1 — balance-drift wiring** (after A; 3 source files, at the limit)
   1. Tests first (new `tests/test_drift_wiring.py`): throttle (called once per N
      ticks per (venue, mode)); demo no-op stays pinned by
      `test_phase_b_robustness.py:195` (do not touch); equity assembler returns
      budgets + closed-net per (venue, mode); resolver tick never blocked by a
      drift-check exception (fail-open).
   2. Enumerator: `active_venue_modes()` in `src/reconcile/executor_select.py`,
      derived from loaded strategy files via `strategy_instrument()`
      (`strategy/records.py:32`) + per-strategy `execution_mode` including the
      control-state override (`strategy/readiness.py:62-65`).
   3. Equity helper: `_account_equity_estimate(venue, mode)` in
      `src/pnl_ledger.py` — sum per-strategy `budget_usd` + closed-net for that
      (exchange, mode); live UPL best-effort, fail-open to closed-only equity
      with the omission logged.
   4. Wire: call `check_balance_drift` from the tick body in
      `src/reconcile/unknown_resolver.py` (loop at `:317`) every Nth tick
      (`HERMX_DRIFT_CHECK_EVERY_N_TICKS`, default 10 ≈ 5 min at the 30 s tick),
      fail-open. *Alternative preserving zero receiver changes: a Hermes cron
      gate script per the `hermx_gate_lib` pattern — drops
      `unknown_resolver.py` from the file set.*
5. **Item B2 — position drift** (blocked on the Option 1 vs 2 decision; do not
   start before it is made)
   1. If Option 1: implement unclaimed-venue-position detection in
      `src/reconcile/drift.py`, reusing B1's `active_venue_modes()` and the
      claimed-inst_id derivation modeled on `_venue_symbols`
      (`dashboard/snapshots.py:191`). Tests: fake venue reports a position on an
      unclaimed inst_id → `RECONCILE_MISMATCH` with `stage="position_drift"`;
      claimed position → no alert.
   2. If neither option is funded: annotate `reconcile_position_drift` +
      `detect_position_drift` as intentionally unscheduled, or delete them —
      leaving them silently inert stays forbidden.

### 0.7 Revalidation confidence

**0.9.** Every accepted and rejected item was re-verified by symbol against the
current tree by three independent code sweeps, against a fully green 810-test
baseline; the two riskiest unknowns from §4 were closed (all
`webhook_receiver.py` symbols re-located; `market["settle"]` availability
confirmed). Residual uncertainty: (i) B1's equity-precision decision (live UPL
in-scope or closed-only) and B2's Option 1/2 decision remain operator calls, as
in §4; (ii) exact test-file placement for C1's regression test (the enrich path
has no dedicated test module today).

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
