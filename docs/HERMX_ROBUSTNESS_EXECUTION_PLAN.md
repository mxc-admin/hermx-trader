# HermX Robustness Execution Plan (Nautilus-informed)
> Generated: 2026-07-03 | Based on `NAUTILUS_GAP_ANALYSIS.md`, code-evolve pass (every opportunity read against actual source).
> Analysis + plan only — no code changed. Confidence scores are the reviewer's, after reading the code, and diverge from the analysis where the code contradicted it.

---

## 0. What the code read changed vs. the original analysis

Five findings materially shifted after reading the source. These drive the plan.

1. **The notional cap must be an INDEPENDENT absolute ceiling — the analysis's proposed default is tautological.**
   `planned_notional = strategy_budget_usd × leverage` (`webhook_receiver.py:1450-1451`). The analysis recommends defaulting `max_notional_usd = budget_usd × leverage × safety_factor`. With `safety_factor = 1.0` that *equals* `planned_notional` exactly and never trips; and the fat-finger scenario it targets is a wrong `budget_usd`/`leverage` **in the same file the planned notional is derived from** — so any ceiling derived from those fields cannot catch it. The gate is only meaningful against a ceiling that does **not** depend on `budget_usd`/`leverage` (a global `HERMX_MAX_NOTIONAL_USD` and/or an explicit per-strategy absolute `capital.max_notional_usd`). This is the single most important correction in the review.

2. **A sub-min order does NOT go to the venue and does NOT churn to UNKNOWN** (analysis Opp 1 claim is wrong).
   `_contracts_for_notional` floors a sub-min qty to `0.0` (`ccxt_adapter.py:473-474`); the OPEN action is then skipped locally as `zero_size` (`:682-689`); status aggregation yields `dry_run` → `ok=False, mode="submit_failed"` (`:755-781`) → the service maps that to a clean terminal **REJECTED** (`service.py:261-262`). No venue call, no UNKNOWN, no symbol pause, no operator toil. The real (lower-severity) defect is **silent under-execution**: the strategy wanted a position, got a `zero_size` REJECT buried in the payload, with **no operator alert**. This downgrades the min-notional/affordability half of Opp 1 and re-frames it as an observability alert, not a pre-trade churn fix.

3. **`health()` discards the balance amounts** (analysis Opp 4 "balance already fetched" is only half true).
   `CcxtExecutor.health()` calls `fetch_balance()` but keeps only `account.currencies = sorted(total.keys())` — the equity numbers are thrown away (`ccxt_adapter.py:998-1010`). The per-currency equity lives in the separate `get_balance()` (`:943-964`), which the dashboard does **not** call. So equity reconciliation is a bigger delta than stated (must add a `get_balance()` read into the snapshot), and it is only meaningful for **live** (a demo sandbox balance is arbitrary). Live/observe-only.

4. **HALTED and REDUCING collapse into ONE state for HermX.**
   HermX's execution model already partitions records into `close_only` (bypasses kill switch + symbol pause, `service.py:130-158`) vs. everything else. Nautilus's `HALTED` (block *all* incl. closes) directly **contradicts** HermX's deliberate design that a close must *never* be blocked (blocking it traps an operator mid-incident). So a coherent HermX control has only one new state — `reducing` / "risk_off": block every non-`close_only` record, always allow `close_only`. Two states would be one dead state. Downgrade Opp 2 to a single toggle.

5. **`reduce_only` in the reversal case is already handled for position math; only the ledger *row* is dropped** (Opp 5 is smaller than it reads).
   `reconcile_from_order_history` already threads external fills through its running `positions` accumulator for delta accuracy (`pnl_ledger.py:568-569`); the only thing it skips is *writing the row* when `is_hermx_cl_ord_id` is false (`:627-628`). The change is genuinely minimal.

Plus one **control-state gotcha** that gates Opp 2: `load_control_state` filters to `{k for k in default}` (`webhook_receiver.py:1207`) — any new top-level key (`trading_state`) is **silently dropped unless added to `default_control_state()`** (`:1164-1178`). Already burned once (accounting_windows, `:1210-1213`).

---

## 1. Ruthless filter results

| # | Opportunity | Orig. priority | Revised priority | Verdict | Reason |
|---|---|---|---|---|---|
| 1 | Pre-trade **notional CAP** (max) | Highest | **Highest** | **SHIP (A1)** | Only uncaught real-money hole. Pure gate on `planned_notional` vs an *independent absolute* ceiling. Corrected from analysis: ceiling must not derive from budget×lev. Conf 0.92. |
| 1b | Sub-min / zero-size **alert** | (bundled in 1) | Low | **SHIP-tiny (A1b)** | Not churn/toil (code proves it's a clean REJECT). Real gap = *silent under-execution*. One alert emit. Adapter/service, not a pre-trade gate. Conf 0.8. |
| 1c | Affordability (free-balance) check | (bundled in 1) | Low | **DEFER→B2** | Needs real balance (see #3); meaningless for demo (only current mode). Folds into Opp 4. Conf 0.6. |
| 2 | Global **HALTED/REDUCING** | High | High | **SHIP (A2)** — collapsed | Collapse to a single `trading_state ∈ {active, reducing}`. Reuses `close_only` bypass. Pure gate. Conf 0.85. |
| 3 | Venue **position-drift** detect & alert | High | High | **SHIP (B1)** | Genuine blind spot; `reconcile_startup` already has an (empty) `position_mismatches` hook and the adapter exposes `get_positions()`. Observe-only. Conf 0.85. |
| 4 | **Equity/balance** reconciliation | High | Medium | **SHIP (B2)** — live-only | Bigger delta than stated (`health()` drops balance; need `get_balance()`). Live-only, observe-only. Conf 0.75. |
| 5 | **External fills** first-class | Medium | Medium | **SHIP (B3)** | Minimal (one drop-guard relaxation + tag), flag-gated. Position math already accounts for them. Conf 0.78. |
| 6 | Persisted **position aggregate** | Medium | Low | **DEFER** | L blast radius over the P&L core. At 2h timeframe (~handful of fills/week) the 100-row window is months deep; the age-out already has an alert. Theoretical at this scale. Revisit only if a sub-hour/high-churn strategy ships. Conf 0.7 the *gap* is real, but ROI is negative now. |
| 7 | Formal **PARTIALLY_FILLED** state | Medium | Low | **DEFER** | Market-only → latent. Adopt when limit orders exist. Conf 0.65. |
| 8 | Per-strategy **submit-rate throttle** | Low | — | **REJECT** | The 86 400 s signal-dedupe window (`:122`) + no-pyramid guard already bound order rate to ~1/bar; a token bucket adds a control surface with ~zero marginal safety here. Conf 0.55. |
| 9 | **Typed reconcile report** interface | Low | Low | **DEFER** | Code-hygiene refactor, not a robustness hole. Do it the next time a venue is added (localizes the Bybit-None-pnl class). Conf 0.65. |
| 10 | **Overfill** (filled ≤ ordered) check | Low | Low | **DEFER→(fold into B1)** | Cheap invariant; natural to add inside the B1 position/fill audit rather than as its own item. Conf 0.6. |

**Net:** SHIP now = 1, 1b, 2 (Phase A) + 3, 4, 5 (Phase B). DEFER = 6, 7, 9, 10. REJECT = 8. Affordability (1c) merges into 4.

---

## Phase A — Pre-trade safety (ship first: pure gates, one file each, no new data files)

Both A1 and A2 are new **pure gates** inside `ExecutionService.execute`, placed as **Gate 4** and **Gate 5** — after the symbol-pause block (`service.py:158`) and **before** `order_intent`/idempotency/write-ahead (`:160`). A blocked gate returns the existing `_blocked(...)` shape (`{ok:True, mode:"not_submitted", reason, gate}`) and writes **no** journal row. There is **no HTTP status** to return — execution is asynchronous; the webhook already answered `200 queued` at intake (`:3556-3580`). Rejection is a `not_submitted` execution-ledger row + an operator alert.

### A1: Pre-trade notional cap gate
- **Goal:** refuse to submit any order whose `planned_notional_usd` exceeds an **independent** absolute ceiling — catching a fat-fingered `budget_usd`/`leverage`/reinvest before it reaches a real venue.
- **Files touched (3):**
  1. `src/execution/service.py` — new gate + one hook read.
  2. `src/webhook_receiver.py` — new hook wiring in `_run_execution_service` (`:2671-2695`) + a `pretrade_notional_ceiling()` resolver + `HERMX_MAX_NOTIONAL_USD` constant.
  3. `schemas/strategy.schema.json` — optional `capital.max_notional_usd` (absolute; `exclusiveMinimum: 0`).
- **Implementation:**
  - Function (service.py, module-level, pure):
    `def _check_pretrade_risk(readiness: dict, ceiling_usd: float | None) -> tuple[bool, str]:`
    - `planned = float((readiness.get("execution_intent") or {}).get("planned_notional_usd") or readiness.get("target_notional_usd") or 0.0)`
    - `if ceiling_usd is None or ceiling_usd <= 0: return (True, "")`  ← unset ⇒ no cap (backward-compatible)
    - `if planned > ceiling_usd: return (False, f"notional_exceeds_max:{planned:.2f}>{ceiling_usd:.2f}")`
    - `return (True, "")`
  - **Called from** `ExecutionService.execute`, new block immediately after the symbol-pause gate (`service.py:158`), before line 160:
    ```
    pretrade_ceiling = self._h("pretrade_notional_ceiling")   # hook, may be None
    ok_risk, risk_reason = _check_pretrade_risk(readiness, pretrade_ceiling(readiness))
    if not ok_risk:
        emit = self._h("emit_reconcile_alert"); emit(self._h("reconcile_alert_mismatch"),
              {"stage": "pretrade_risk", "reason": risk_reason, "symbol": readiness.get("symbol")})
        return _blocked(risk_reason, "pretrade_notional")
    ```
    (`close_only` records are **not** exempt — a close's `planned_notional` is 0/irrelevant so it never trips; no special-case needed.)
  - **Ceiling resolver** (webhook_receiver.py): `pretrade_notional_ceiling(readiness)` = `min(non-None of: strategy capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD)`; return `None` if neither set. Read strategy off `readiness["strategy_id"]` via `STRATEGIES`.
  - **Gate formula:** `planned_notional_usd > min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD)` where the RHS is an **absolute USD figure independent of budget×leverage**.
  - **Rejection:** `_blocked("notional_exceeds_max:…", "pretrade_notional")` → execution-ledger `not_submitted` row + `RECONCILE_MISMATCH` (`stage=pretrade_risk`) operator alert. No journal write, no venue call.
  - **Flags:** `HERMX_MAX_NOTIONAL_USD` (module constant, env-read, **default unset → no cap**). Rationale: matches HermX's ship-safe convention (`ORDER_PNL_IS_NET`, `HERMX_RECONCILE_ENABLED` — inert until an operator arms it), so the gate cannot regress existing demo flow. Operators SHOULD set it; document a recommended value (e.g. 5× the largest live strategy's `budget_usd × leverage`).
- **Tests to write first** (`tests/test_pretrade_notional_gate.py`):
  - `test_pretrade_gate_blocks_oversized_notional` — ceiling 5 000, planned 10 000 → `not_submitted/pretrade_notional`, **no** PLANNED row in journal.
  - `test_pretrade_gate_passes_normal_order` — ceiling 5 000, planned 3 000 → proceeds to write-ahead (assert PLANNED recorded).
  - `test_pretrade_gate_disabled_when_ceiling_unset` — no `HERMX_MAX_NOTIONAL_USD`, no `capital.max_notional_usd` → proceeds regardless of size.
  - `test_pretrade_gate_per_strategy_tightens_global` — global 50 000, strategy `max_notional_usd` 4 000, planned 6 000 → blocked (min wins).
  - `test_pretrade_gate_precedence_after_symbol_pause` — a paused symbol still blocks on `symbol_pause`, not `pretrade_notional` (assert gate ordering unchanged).
  - `test_pretrade_gate_close_only_not_tripped` — `close_only=True`, planned 0 → proceeds.
- **Edge cases:** planned `None`/missing → treated as 0 (never blocks); ceiling `0`/negative env typo → treated as unset (no cap — do NOT block everything on a bad env); reinvest inflating `budget_usd` between restarts (caught, that's the point); reversal record (open+close) uses the OPEN leg's `planned_notional` — correct.
- **Rollback:** unset `HERMX_MAX_NOTIONAL_USD` (and remove any `capital.max_notional_usd`) → gate returns `(True, "")` for every order; byte-identical to today. Or revert the 3 edits.

### A1b: Silent under-execution alert (sub-min / zero-size)
- **Goal:** when an OPEN action is skipped as `zero_size` (sub-min notional) so the strategy silently under-executes, emit an operator alert instead of burying `zero_size` in the payload.
- **Files touched (1):** `src/execution/service.py` (preferred — has the aggregated result + alert hooks) **or** `src/executors/ccxt_adapter.py`. Recommend service.py to keep the adapter observation-only.
- **Implementation:** after the adapter result is in hand (`service.py:234-243`), if `mode == "submit_failed"` and every `executed_orders[*].reason == "zero_size"`, emit `emit_reconcile_alert(RECONCILE_MISMATCH, {"stage":"under_execution","reason":"zero_size_below_min","cl_ord_id":…})`. Still records REJECTED (unchanged). No new flag.
- **Tests:** `test_under_execution_alert_on_zero_size` (adapter returns all-skipped zero_size → alert emitted, state REJECTED); `test_no_alert_on_normal_reject` (a real reject reason → no under-execution alert).
- **Rollback:** delete the alert block; behavior identical minus the alert.

### A2: Global `trading_state` (reducing / risk-off) gate
- **Goal:** one operator action puts the **whole system** (every symbol, demo *and* live) into close-only, without racing per-symbol pauses.
- **Files touched (3):**
  1. `src/webhook_receiver.py` — add `"trading_state": "active"` to `default_control_state()` (`:1165-1178`, **mandatory** or the merge drops it, `:1207`); `load_trading_state()` reader; `set_trading_state(state)` writer; hook wiring in `_run_execution_service`.
  2. `src/execution/service.py` — new gate.
  3. `src/dashboard.py` + `docs/hermx-slash-commands.md` — surface a toggle / wire `/hx-emergency-stop` (can be a fast-follow; the receiver-side gate is the safety-bearing part).
- **Implementation:**
  - `def load_trading_state() -> str:` → `str(load_control_state().get("trading_state") or "active").lower()`; valid `{active, reducing}` (anything else → `active`, fail-open-to-normal is fine because `reducing` is the *safe* extra state, and the kill switch still independently guards live).
  - **Gate (service.py), as Gate 5, right after A1:**
    ```
    trading_state = self._h("trading_state")()          # "active" | "reducing"
    if trading_state == "reducing" and not readiness.get("close_only"):
        return _blocked("trading_state_reducing", "trading_state")
    ```
  - `close_only` records bypass exactly as they bypass the kill switch/pause (same `readiness.get("close_only")` predicate). A reversal record (has an `OPEN_*` action, `close_only=False`) is blocked whole — correct: a reversal *opens* new opposite risk and is not risk-reducing.
  - **Collapsed design:** only `reducing` exists; no `halted` (a HALT that also blocks closes contradicts HermX's never-block-a-close invariant, `service.py:150-158`).
- **Tests to write first** (`tests/test_trading_state_gate.py`):
  - `test_reducing_blocks_open` — `trading_state=reducing`, normal buy → `not_submitted/trading_state`, no journal write.
  - `test_reducing_allows_close_only` — `reducing` + `close_only=True` → proceeds.
  - `test_reducing_blocks_reversal_open_leg` — `reducing` + reversal record (`close_only=False`) → blocked whole.
  - `test_active_is_noop` — `trading_state=active` (default) → proceeds; also assert a control-state with no `trading_state` key loads as `active` (merge-drop regression guard).
  - `test_trading_state_persists_round_trip` — `set_trading_state("reducing")` → `load_control_state()["trading_state"] == "reducing"` after reload (guards the `default_control_state` key-drop gotcha).
- **Edge cases:** unknown/legacy `trading_state` value → `active`; control-state written by an old build (no key) → `active`; interaction with kill switch — independent and both apply (a live open under `reducing` is blocked by `trading_state` first).
- **Rollback:** `set_trading_state("active")` (or delete the key — loads as `active`). Gate becomes a no-op.

---

## Phase B — Venue-truth observability (observe-only; never auto-corrects — §B of the analysis)

All three are **read-only alerts**, folded into the dashboard model build (`dashboard_model`, `dashboard.py:1537-1601`) and/or the periodic resolver. None submits, cancels, or auto-trades. Each is flag-gated OFF by default.

### B1: Venue position-drift detection & alert
- **Goal:** detect when HermX's believed net position diverges from the venue's reported position and alert (never auto-correct).
- **Files touched (2):** `src/webhook_receiver.py` (new `reconcile_positions_once()` beside `resolve_unknown_orders_once`, `:2453`) + reuse `emit_reconcile_alert`. Optionally schedule it in the `unknown_resolver_loop` cadence (`:2621-2650`).
- **Implementation:**
  - `def reconcile_positions_once(executor=None) -> dict:` — for each active `(venue, mode)` (derive from loaded strategies / open-order intents, same per-order executor resolution as `_executor_for_order`, `:2246`), call `executor.get_positions(inst_id)` (`ccxt_adapter.py:916-941`, returns signed `pos`), compare against the position implied by the order journal / last snapshot per `inst_id`.
  - On `abs(venue_pos - believed_pos) > POSITION_DRIFT_EPS` (a step-scaled epsilon): `emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {"stage":"position_drift", "inst_id":…, "venue_pos":…, "believed_pos":…})`. Optionally `pause_symbol` past a larger threshold (matches the resolver's stuck-order posture, `:2517`). **Never** submits.
  - Populate `reconcile_startup`'s existing `position_mismatches` list (`:2277`, currently always empty) as the startup one-shot equivalent.
  - **Fold Opp 10 here:** while reading fills, assert `accFillSz ≤ ordered + step_eps`; on violation, same `RECONCILE_MISMATCH` with `stage=overfill` (warn, don't hard-fail).
- **Flag:** `HERMX_POSITION_DRIFT_ENABLED` (default OFF).
- **Tests** (`tests/test_position_drift_reconcile.py`): `test_drift_alert_when_venue_disagrees`; `test_no_alert_within_epsilon`; `test_observe_only_never_submits` (assert executor `.execute`/`create_order` never called — mirror `test_reconciliation_observe_only.py`); `test_disabled_by_flag`; `test_overfill_alert_when_filled_gt_ordered`.

### B2: Account equity/balance reconciliation (live-only) — absorbs affordability (Opp 1c)
- **Goal:** alert when computed `equity_now_usd` drifts from the venue's real balance (live only); expose the balance so a future affordability check has real data.
- **Files touched (2):** `src/dashboard.py` (call `get_balance()` in `strategy_live_snapshot`, `:819-869`, surface `venue_equity_usd`; add drift check in the model build / `reconcile_health`) + `src/pnl_ledger.py` (a pure `equity_drift(expected, venue_balance)` helper if wanted).
- **Implementation:**
  - In the live snapshot, additionally call `executor.get_balance()` (`ccxt_adapter.py:943-964`) — **note `health()` does not carry equity** (only currency names, `:998-1010`), so this is a *new* read. Sum the quote-currency `eq` → `venue_equity_usd`. Demo skipped (sandbox balance is arbitrary).
  - Compare `expected = budget_usd + closed_net + open_upl` (`aggregate_strategy_pnl`, `pnl_ledger.py:346-388`) vs `venue_equity_usd`; on `|drift| > EQUITY_DRIFT_TOLERANCE_USD` emit `equity_drift_usd` in `reconcile_health` + a `RECONCILE_MISMATCH` (`stage=equity_drift`). Read-only; **does not** change accounting.
  - Affordability (Opp 1c) becomes available later as `planned_notional ≤ venue_equity × leverage_headroom`, but only wire it after balance is verified per-venue — do **not** gate submissions on it in this phase.
- **Flag:** `HERMX_EQUITY_DRIFT_ENABLED` (default OFF); live-only regardless.
- **Tests** (`tests/test_balance_reconcile.py`): `test_equity_drift_alert_live`; `test_demo_skipped` (no balance read/alert in demo); `test_within_tolerance_no_alert`; `test_disabled_by_flag`; `test_accounting_unchanged` (ledger writes identical with the check on).

### B3: External / manual fills first-class in the ledger
- **Goal:** a purely manual venue close (no HermX `cl_ord_id`) is recorded (unattributed) instead of dropped, so lifetime realized P&L matches the account after out-of-band operator action.
- **Files touched (1):** `src/pnl_ledger.py` — relax the drop at `:627-628`; tag rows.
- **Implementation:**
  - At `reconcile_from_order_history` (`:627-628`), when `is_hermx_cl_ord_id` is false **and** the row is a detected close (`is_close`, `:603`): instead of `continue`, build the entry with `strategy_id=None` and a new `source="external"` (or `attribution="external"`) field, gated behind `HERMX_LEDGER_EXTERNAL_FILLS` (default OFF to preserve current attribution semantics).
  - Keep external rows **out of per-strategy sums** (`aggregate_strategy_pnl` already filters by `strategy_id`; `None` naturally excludes them) but include them in a portfolio/account-level total so the ledger reconciles to the venue. Dedup key `(exchange, inst_id, ord_id, mode)` already handles idempotency (`:127-128`). Principle 10: never prune.
  - Position math is unaffected — externals already flow through the `positions` accumulator (`:568-569`).
- **Flag:** `HERMX_LEDGER_EXTERNAL_FILLS` (default OFF).
- **Tests** (`tests/test_external_fills_ledger.py`): `test_external_close_written_when_enabled` (row persists, `strategy_id=None`, `source=external`); `test_external_close_dropped_when_disabled` (unchanged default); `test_external_excluded_from_strategy_sums`; `test_external_deduped` (same ord_id twice → one row).

---

## Phase C — Defer / reject (with reasons)

| Item | Verdict | Reason | Revisit trigger |
|---|---|---|---|
| **Opp 6** — persisted position aggregate | DEFER | L blast radius over the P&L core (`reconcile_from_order_history`). At 2h timeframe the 100-row window (`get_order_history_raw limit=100`) holds months of fills; the saturated-window age-out already alerts (`dashboard.py:935-957`). Negative ROI now. | A sub-hour or high-churn strategy ships, OR the age-out alert actually fires in production. |
| **Opp 7** — PARTIALLY_FILLED / ACCEPTED states | DEFER | Market-order-only makes it latent; partial-in-progress rows are already skipped, not corrupted (`_NON_TERMINAL_ORDER_STATES`). | Limit / resting order types are introduced. |
| **Opp 8** — submit-rate throttle | REJECT | The 86 400 s dedupe window + no-pyramid guard already bound rate to ≈1 order/bar. A token bucket is a control surface with ~zero marginal safety at this scale. | Dedupe window is shortened, or a strategy legitimately submits many distinct orders/minute. |
| **Opp 9** — typed reconcile report interface | DEFER | Code-hygiene refactor, not a robustness hole; venue quirks are localized enough today. | Next new venue (do it as part of that venue's adapter work — localizes the Bybit-None-pnl class). |
| **Opp 10** — overfill check | FOLD → B1 | Cheap invariant; naturally lives inside the B1 fill audit rather than as a standalone item. | — |
| **Opp 1c** — affordability check | FOLD → B2 | Needs real balance; meaningless for demo. | After B2 lands balance + a per-venue balance verification. |

---

## Sequencing diagram

```
                 PHASE A (pure gates, service.py + control-state)          PHASE B (observe-only, dashboard + resolver)
                 ── independent, ship together ──                          ── depend on venue reads ──

  A1  notional-cap gate ─────────┐
  A1b under-exec alert  ─────────┤ (A1b trivially after A1; same file)
  A2  trading_state gate ────────┘
        │                                                       B1 position-drift  ──┐ (get_positions; +Opp10 overfill)
        │  no data-model coupling                               B2 equity-drift    ──┤ (get_balance; live-only; unlocks 1c)
        ▼                                                       B3 external fills   ─┘ (pnl_ledger drop-relax)
  ship A → verify in demo → arm HERMX_MAX_NOTIONAL_USD          ship B behind flags → verify alerts fire → keep observe-only
                                                                            │
                                                                            ▼
                                                                DEFER: Opp 6 (position aggregate) — only after B1/B3
                                                                        show the recompute path is actually a problem
```

**Dependency notes:**
- A1, A1b, A2 have **no** ordering dependency among themselves (three edits in `service.py` + control-state); land as one small PR or three tiny ones. Respect the dev-rule 3-file cap — A1 and A2 each touch 3 files, so ship them as **separate** changes, not one.
- B1 and B2 both add a venue read to the dashboard snapshot; do B1 first (positions; the primary blind spot) then B2 (balance).
- B3 is fully independent of B1/B2 (ledger-only) and can land any time.
- **Opp 6 depends on B1/B3 evidence** — build the persisted aggregate only if the drift/external-fill alerts prove the recompute-from-window path actually diverges in production.

---

## Test contracts (test-first, before ANY implementation)

**Phase A**
```
tests/test_pretrade_notional_gate.py
  test_pretrade_gate_blocks_oversized_notional
  test_pretrade_gate_passes_normal_order
  test_pretrade_gate_disabled_when_ceiling_unset
  test_pretrade_gate_per_strategy_tightens_global
  test_pretrade_gate_precedence_after_symbol_pause
  test_pretrade_gate_close_only_not_tripped
  test_under_execution_alert_on_zero_size          # A1b
  test_no_alert_on_normal_reject                    # A1b

tests/test_trading_state_gate.py
  test_reducing_blocks_open
  test_reducing_allows_close_only
  test_reducing_blocks_reversal_open_leg
  test_active_is_noop                               # incl. missing-key → active
  test_trading_state_persists_round_trip            # default_control_state key-drop guard
```
Extend `tests/test_execution_gate_precedence.py` with the two new gates in their exact positions (Gate 4 pretrade_notional, Gate 5 trading_state — both after symbol_pause, before idempotency). All new gate tests must exercise the **production** `ExecutionService.execute` via the real hook wiring (`_run_execution_service`), never a re-implemented gate body (anti-pattern in `code-quality.md`).

**Phase B**
```
tests/test_position_drift_reconcile.py
  test_drift_alert_when_venue_disagrees
  test_no_alert_within_epsilon
  test_observe_only_never_submits                   # assert create_order/execute never called
  test_disabled_by_flag
  test_overfill_alert_when_filled_gt_ordered        # folded Opp 10

tests/test_balance_reconcile.py
  test_equity_drift_alert_live
  test_demo_skipped
  test_within_tolerance_no_alert
  test_disabled_by_flag
  test_accounting_unchanged

tests/test_external_fills_ledger.py
  test_external_close_written_when_enabled
  test_external_close_dropped_when_disabled
  test_external_excluded_from_strategy_sums
  test_external_deduped
```

---

## Risk assessment

| Phase | What could go wrong | Mitigation | Rollback |
|---|---|---|---|
| **A1** | Ceiling derived from budget×lev would be tautological and catch nothing (the analysis's trap). | Ceiling is an **independent absolute** (`HERMX_MAX_NOTIONAL_USD` / `capital.max_notional_usd`), never budget-derived; unit test `test_pretrade_gate_per_strategy_tightens_global` pins it. | Unset env → no cap, byte-identical to today. |
| **A1** | A bad env (`HERMX_MAX_NOTIONAL_USD=0`) blocks *all* orders. | `ceiling <= 0` is treated as **unset** (no cap), not "block everything". Explicit test. | Unset env. |
| **A2** | New `trading_state` key silently dropped by `load_control_state` merge → gate never engages. | Add key to `default_control_state()`; `test_trading_state_persists_round_trip` guards it. | Key absent → `active` → no-op. |
| **A2** | A `halted` misconception blocks closes → traps operator mid-incident. | Collapsed design: only `reducing`, closes always bypass (same `close_only` predicate as the kill switch). | `set_trading_state("active")`. |
| **A (all)** | A gate placed on the wrong side of the write-ahead leaves orphan PLANNED/SUBMITTED rows. | Both gates return **before** line 160 (pre-idempotency, pre-write-ahead); `test_*_no_journal_write` asserts no PLANNED row on block. | Revert. |
| **B1/B2** | An "observability" read accidentally submits/cancels (Nautilus's auto-correct foot-gun, §B). | Observe-only by construction; `test_observe_only_never_submits` asserts no write call; reuse read-only executor path (`_reconciliation_executor`). | Flags OFF. |
| **B2** | Reading `get_balance()` on every 10 s model build adds latency / rate-limit pressure. | Reuse the existing snapshot cache TTLs (`OKX_LIVE_CACHE_TTL_SECONDS=5`); live-only; wrapped in the same `try/except: pass` as the current snapshot so a balance-read failure degrades, never fails, the dashboard. | Flag OFF. |
| **B3** | Relaxing the `is_hermx_cl_ord_id` drop pollutes per-strategy P&L with unattributed rows. | External rows carry `strategy_id=None` (already excluded from per-strategy sums) + `source=external`; flag OFF by default; dedup key unchanged. | Flag OFF → rows dropped as today. |
| **All** | Touching shared money-path code (`service.py`, `pnl_ledger.py`) — dev-rule 4 (shared-lib changes need confirmation) and the 3-file cap (rule 3). | Ship A1 and A2 as **separate** ≤3-file changes; get explicit confirmation before editing `service.py`/`pnl_ledger.py`; every change flag-gated OFF and test-first. | Per-item flag/revert above. |

---
*End of plan. Phase A is the real-money-safety core (notional cap + risk-off); Phase B is venue-truth observability, all observe-only; everything else is deferred with an explicit revisit trigger.*
