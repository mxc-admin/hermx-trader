# God-File Refactoring Plan — `webhook_receiver.py` & `dashboard.py`

Status: **PLANNED — not started.** Analysis performed 2026-07-04 (CC2). No code changes yet.

## Why

`src/webhook_receiver.py` (3,791 LOC, ~140 top-level defs) and `src/dashboard.py` (3,293 LOC,
~12% type-annotated) are god files. Prior extraction (`src/webhook/{money,timeutil,ledger_io,config}.py`,
`src/security/webhook_auth.py`, `src/pnl_ledger.py`) is genuine and correctly wired via a re-export
shim pattern (`wr.<fn>` stays monkeypatchable), but reconciliation, advisory/risk-gating, and signal
dedup logic still live inline in the monolith.

## Current State Inventory (line ranges approximate, from 2026-07-04 analysis)

### `src/webhook_receiver.py`

| Cluster | Lines | ~LOC | Status |
|---|---|---|---|
| Module constants/globals | 89–460 | 370 | stays |
| Strategy-record reads | 330–491 | 160 | **extract → Phase 3** (duplicated in dashboard.py) |
| Security thin-wrappers | 492–500, 659–711 | 90 | already delegates to `security/webhook_auth.py` |
| Symbol-lock / fairness tickets | 504–573 | 70 | stays (tightly bound to `worker_loop`) |
| Watchdog/heartbeat | 574–658 | 85 | stays |
| **Signal dedup** | 711–848 | 137 | **extract → Phase 1** |
| Ledger rotation / pipeline events | 848–978 | 130 | stays (WAL semantics, don't touch) |
| **Normalize + schema validation** | 978–1168 | 190 | **extract → Phase 1** |
| **Control-state read/write** | 1168–1414 | 246 | **extract → Phase 2** (duplicated in dashboard.py D2) |
| Strategy execution readiness | 1414–1599 | 185 | **extract → Phase 3** |
| Order journal / state machine | 1599–2003 | 404 | **extract → Phase 4** |
| **Reconciliation + unknown-resolver + drift** | 2003–2703 | 700 | **extract → Phase 5 (HIGH risk)** |
| Operator alert emission | 2192–2258 | 65 | extract with Phase 5 |
| Execution-service glue | 2735–2955 | 220 | stays (or moves with Phase 6 boundary) |
| **Advisory / risk gating** | 2955–3084 | 130 | **extract → Phase 6 (0 tests today — write tests first)** |
| Record building | 3084–3359 | 275 | stays in receiver (pre-schema gate ordering is sensitive) |
| Worker/queue/replay | 3359–3517 | 158 | stays |
| HTTP Handler | 3517–3673 | 156 | stays |
| Startup/main | 3674–3791 | 117 | stays |

### `src/dashboard.py`

| Cluster | Lines | ~LOC | Status |
|---|---|---|---|
| Strategy reads (dup of C2) | 123–162 | 40 | **delete, import from Phase 3 module** |
| Control-state (dup of C9) | 186–357 | 170 | **delete, import from Phase 2 module** |
| Data projections | 357–505 | 150 | → Phase 7 `dashboard/model.py` |
| Formatting/HTML-escape | 506–676 | 170 | → Phase 7 `dashboard/render.py` |
| Executor construction + snapshots | 676–1182 | 500 | → Phase 7 `dashboard/snapshots.py` |
| OKX enrich/history | 1182–1490 | 310 | → Phase 7 `dashboard/snapshots.py` |
| Health/freshness summaries | 1490–1588 | 100 | → Phase 7 `dashboard/model.py` |
| Dashboard model + P&L contracts | 1588–1786 | 200 | → Phase 7 `dashboard/model.py` (TypedDicts here) |
| API payloads | 1786–1899 | 110 | → Phase 7 `dashboard/model.py` |
| HTML rendering | 1899–2608 | 700 | → Phase 7 `dashboard/render.py` |
| `render()` master template | 2608–2971 | 363 | → Phase 7 `dashboard/render.py` |
| HTTP Handler + control routes | 2971–3267 | 300 | → Phase 7 `dashboard/server.py` |
| Cache refresh loop | 3267–end | 25 | → Phase 7 `dashboard/server.py` |

## `src/strategy/` disposition

Empty except `__init__.py`. Confirmed via `git log` this is a **leftover of a deleted subsystem**
(`decision_math.py`, added `b3e711ec`, removed `d5782f08` — "remove position_journal, decision_math,
and shadow paper-state subsystem"), NOT a stalled in-progress extraction.

**Decision: reuse it** in Phase 3 as the home for strategy-record + readiness logic. Don't delete.

## Phased Roadmap (risk-adjusted order)

1. **`src/signals/{normalize,dedupe}.py`** (C6+C8, ~330 LOC) — LOW risk. `normalize` referenced by
   21 test files (best-tested code in the repo). Move `_SIGNAL_DEDUPE_INDEX` + lock with it.
   Verify: `test_phase5_normalization_cleanup.py`, `test_phase3_idempotency.py`, `test_action_close_intake.py`.

2. **`src/control_state.py`** (C9, ~246 LOC, kills dashboard.py D2 duplication) — LOW–MED risk.
   Preserve `_atomic_json_dump`/`_fail_closed_state_write` semantics verbatim.
   Verify: `test_phase3_runtime_controls.py`, `test_phase3_strategy_overrides.py`, `test_phase4_dashboard.py`,
   `test_dashboard_mode_aware.py`.

3. **`src/strategy/{records,readiness}.py`** (C2+C10, kills dashboard.py D1 dup, ~345 LOC) — LOW–MED risk.
   Reuses the empty `src/strategy/` dir.
   Verify: `test_characterization_strategy_matching.py`, `test_phase6_strategy_schema_v2.py`, `test_phase5_exec_routing.py`.

4. **`src/orders/journal.py`** (C11, ~404 LOC) — MED risk. Self-contained state machine; owns the
   `_order_index()` cache — move it with the functions. Do before Phase 5.
   Verify: `test_order_journal_checkpoint.py`, `test_order_state_machine.py`.

5. **`src/reconcile/`** package (C12+C13, ~765 LOC) — **HIGH risk, thinnest tested cluster.**
   - `reconcile/orders.py` — `reconcile_order_once`, `_with_backoff`, `map_order_outcome`, `reconcile_startup`
   - `reconcile/unknown_resolver.py` — `resolve_unknown_orders_once`, `unknown_resolver_loop`
   - `reconcile/drift.py` — `reconcile_position_drift`
   - `reconcile/executor_select.py` — `_effective_execution_config`, `_reconciliation_executor`,
     `_executor_for_order` (preserve per-order venue/mode read invariant)
   - `reconcile/alerts.py` — `emit_reconcile_alert`, `emit_operator_alert`
   - Keep `pnl_ledger.reconcile_from_order_history` where it is; re-export both halves under
     `reconcile/__init__.py`.
   - **Write characterization tests FIRST** where coverage is 1 (`reconcile_order_once`,
     `resolve_unknown_orders_once`, `reconcile_position_drift` each have exactly 1 test today).
   Prereq: Phases 3, 4.
   Verify: `test_reconciliation_observe_only.py`, `test_receiver_reconcile_venue.py`,
   `test_unknown_resolver_controls.py`, `test_phase_b_robustness.py`, `test_phase_a_robustness.py`.

6. **`src/advisor.py`** (C15, ~130 LOC) — MED risk, **`execute_with_advisor` has ZERO tests today.**
   Write characterization tests first (anchor on `test_phase8_advisor.py` which covers
   `run_execution_advisor`).
   Verify: `test_phase8_advisor.py`, `test_execution_gate_precedence.py`.

7. **`src/dashboard/` package split** — MED risk, do LAST. Sub-steps in order:
   1. `dashboard/snapshots.py` (D5+D6, ~810 LOC) — executor construction + live/history snapshots;
      fold shared executor-build logic with `reconcile/executor_select.py`.
   2. `dashboard/model.py` (D3+D7+D8+D9, ~560 LOC) — data aggregation + P&L contracts + payloads.
      Define `DashboardModel`/`StrategyPnlContract`/`PortfolioContract` as `TypedDict`s here —
      highest-leverage typing move (types the seam feeding `render.py`).
   3. `dashboard/render.py` (D4+D10+D11, ~1,230 LOC) — pure HTML/formatting functions of the model.
   4. `dashboard/server.py` (D12+D13) — `Handler`, auth, control routes; imports `src/control_state.py`.
   Verify after each sub-step: `test_phase4_dashboard.py`, `test_dashboard_mode_aware.py`,
   `test_pnl_api_contracts.py`.

## Dashboard.py Typing Path (12% → incremental, no big-bang pass)

1. Type new modules at creation time (Phase 7 sub-steps) — don't retrofit later.
2. Define `TypedDict`s for the model boundary (`DashboardModel` etc.) in `dashboard/model.py` first —
   types `render.py`'s inputs for free.
3. Backfill the shrunken `dashboard.py` residue last, after Phase 7.
4. Add `mypy`/`pyright` (non-strict) scoped only to `src/dashboard/**`, `src/signals/`, `src/reconcile/`,
   `src/orders/` — gate CI on new dirs only, don't wall-of-errors the legacy files.

## Non-Goals & Invariants to Protect (every phase)

- **Money-ledger invariants** — `closed-trades.jsonl` append-only, NEVER pruned/rotated. Phase 4 moves
  order-journal rotation helpers (`_rotate_ledger_if_large`) — must not get wired to the closed-trades
  ledger during the move.
- **Read-side dedup key** `(exchange, inst_id, ord_id, mode)`, last-wins, preserve malformed rows —
  stays owned by `pnl_ledger.py`; don't reimplement in `reconcile/`.
- **WAL-before-queue** — `raw-webhooks.jsonl` fsync'd before enqueue; don't reorder intake→WAL→queue
  during C7/C17 touches.
- **Reconcile reads `(venue, mode)` from the order's own intent record**, never global defaults; skip
  non-terminal venue rows; never force UNKNOWN→REJECTED (Phase 5 `executor_select.py`).
- **Pre-schema gates precede jsonschema** in `build_record`: `side not in ALLOWED_SIDES` → 400,
  `source != "tradingview"` → 202, both before `validate_alert_schema()`. Gate ordering must stay
  byte-identical when C8 (schema) is split from C16 (record-build).
- **HMAC/replay/rate-limit order** in `Handler.do_POST` must not be reordered when C3 is extracted
  (pure delegation move only).
- **Concurrency globals move WITH their functions**: `_SIGNAL_DEDUPE_INDEX`+lock (Phase 1),
  `_order_index()` cache (Phase 4), symbol fairness tickets (leave in receiver, bound to `worker_loop`).
- **Compatibility shim required every phase** — keep `wr.<fn>` re-export surface intact; the test
  suite monkeypatches through it.
- Explicit non-goals: don't collapse existing lazy-import cycle-avoidance points; don't touch
  `execution/service.py` or `executors/`.

## Execution Order (risk-adjusted value)

Phase 1 → 2 → 3 (low-risk, kill duplication, best-tested) → 4 → 6 (add tests first) →
5 (highest risk, needs 4+3 done) → 7 (dashboard split, last, type as you go).

Run full `tests/` suite (`./.venv/bin/pytest tests/ -q`) green after each phase; phase-specific
files above are the fast local gate.
