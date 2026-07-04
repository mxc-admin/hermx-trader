# God-File Refactoring Plan — `webhook_receiver.py` & `dashboard.py`

Status: **PLANNED — not started.** Analysis performed 2026-07-04 (CC2), corrected 2026-07-04
after adversarial review. All line numbers below re-verified against the working tree at correction
time (`wc -l`, `grep -n`, `awk` def-map). No code changes yet.

## Why

`src/webhook_receiver.py` (3,791 LOC, ~140 top-level defs) and `src/dashboard.py` (3,291 LOC,
~12% type-annotated) are god files. Prior extraction (`src/webhook/{money,timeutil,ledger_io,config}.py`,
`src/security/webhook_auth.py`, `src/pnl_ledger.py`) is genuine and correctly wired via a re-export
shim pattern (`wr.<fn>` stays monkeypatchable), but reconciliation, advisory/risk-gating, and signal
dedup logic still live inline in the monolith.

## Import-Root Convention (read before any phase)

`src/` is on `sys.path`, and packages are imported **bare** — `from signals.dedupe import ...`,
**never** `from src.signals.dedupe import ...`. Two mechanisms put `src/` on the path, both verified:

1. The entrypoints run as `python src/webhook_receiver.py` / `python src/dashboard.py`
   (`Dockerfile:75` CMD, `deploy/hermx-receiver.service:12` and `deploy/hermx-dashboard.service:18`
   ExecStart). Python auto-inserts the **script's own directory** (`src/`) at `sys.path[0]`.
2. Reinforced explicitly in `webhook_receiver.py:473`:
   `sys.path.insert(0, str(Path(__file__).resolve().parent))`.

This is why the existing bare imports work — verified live:
`webhook_receiver.py:28 from security.webhook_auth import ...`,
`:41 from webhook.money import ...`, `:60 from webhook.timeutil import ...`,
`:66 from webhook.ledger_io import ...`, `:72 from webhook.config import ...`.
All carry `# noqa: E402` because they sit below the module docstring, not because of path tricks.

### Worked example — the Phase 1 shim (the pattern EVERY phase repeats)

Current monolith (`webhook_receiver.py:771`):

```python
def check_and_mark_signal(normalized: dict, received_at: str) -> tuple[bool, dict]:
    # ~60 lines of dedupe-index read-modify-write under _SIGNAL_DEDUPE_LOCK ...
```

New file `src/signals/dedupe.py` receives the function body **verbatim** plus its module globals
(`_SIGNAL_DEDUPE_INDEX`, `_SIGNAL_DEDUPE_LOCK`, `_load_signal_dedupe_index`, `_dedupe_window_seconds`):

```python
# src/signals/dedupe.py
_SIGNAL_DEDUPE_INDEX: dict = {}
_SIGNAL_DEDUPE_LOCK = threading.Lock()

def check_and_mark_signal(normalized: dict, received_at: str) -> tuple[bool, dict]:
    # ... identical body, moved not rewritten ...
```

The original 60-line body in `webhook_receiver.py` is replaced by **one re-export line** (so
`wr.check_and_mark_signal` stays a real attribute the test suite can monkeypatch):

```python
from signals.dedupe import (  # noqa: E402  re-export shim; wr.<fn> stays monkeypatchable
    dedupe_key, stable_client_order_id, check_and_mark_signal,
)
```

## Current State Inventory (contiguous, non-overlapping — rows tile 1..EOF and sum exactly)

Each table below is a strict tiling of the file: every line 1..EOF belongs to exactly one row, so the
`~LOC` column sums to the file's real line count. Top-of-file (imports/constants) is its **own row**,
not folded silently into a "constants" range.

### `src/webhook_receiver.py` — sums to **3,791** (verified)

| Cluster | Lines | ~LOC | Disposition |
|---|---|---|---|
| Top-of-file: docstring, imports, module constants/globals (`as_float`@80) | 1–329 | 329 | stays |
| Strategy-record reads (`strategy_instrument`@330 … `load_strategy_files`@414) | 330–491 | 162 | **Phase 3** (re-impl'd in dashboard D1) |
| Security config-health wrappers (`webhook_auth_config_healthy`@492, `_client_ip`@500) | 492–503 | 12 | delegates to `security/webhook_auth.py` |
| Symbol-lock / fairness tickets | 504–573 | 70 | stays (bound to `worker_loop`) |
| Watchdog/heartbeat (`_maybe_watchdog_alert`@595 → alerts) | 574–658 | 85 | stays |
| Security/HMAC/rate-limit wrappers (`authenticate_webhook_request`@700) | 659–710 | 52 | delegates to `security/webhook_auth.py` |
| **Signal dedup** (`dedupe_key`@711 … `check_and_mark_signal`@771) | 711–847 | 137 | **Phase 1** |
| Ledger rotation / pipeline events (`_rotate_ledger_if_large`@879) | 848–977 | 130 | stays (WAL semantics) |
| **Normalize + schema validation** (`normalize`@996; `_alert_schema_enforcement_status`@1123 → alerts) | 978–1167 | 190 | **Phase 1** |
| **Control-state CRUD** (`default_control_state`@1168 … `clear_trading_state`@1409) | 1168–1413 | 246 | **Phase 2** (re-impl'd in dashboard D2) |
| `_strategy_config_for_readiness`@1414 (readiness helper) | 1414–1423 | 10 | **Phase 3** |
| **Control-state atomic-write helpers** (`_canonical_state_json`@1424, `_fsync_dir`@1431, `_atomic_json_dump`@1443, `_fail_closed_state_write`@1456) | 1424–1469 | 46 | **Phase 2** (see boundary note) |
| Strategy execution readiness (`build_strategy_execution_readiness`@1470) | 1470–1598 | 129 | **Phase 3** |
| Order journal / state machine (`order_state_can_transition`@1599 … `latest_order_record`@1981; owns `_order_index`@1700) | 1599–2002 | 404 | **Phase 4** |
| **Reconcile part A** (`map_order_outcome`@2040 … `reconcile_order_with_backoff`@2145) | 2003–2191 | 189 | **Phase 5** |
| **Shared alert emission** (`emit_operator_alert`@2192, `emit_auth_failure_alert`@2228, `maybe_emit_queue_saturation_alert`@2236) | 2192–2246 | 55 | **Phase 0 (NEW)** |
| **Reconcile part B** (`emit_reconcile_alert`@2247, `reconcile_position_drift`@2259, executor-select@2287–2350, `reconcile_startup`@2351, unknown-resolver@2427–2734) | 2247–2734 | 488 | **Phase 5** |
| Execution-service glue (`_run_execution_service`@2743 … `_execute_authoritative`@2902) | 2735–2954 | 220 | stays |
| **Advisory / risk gating** (`run_execution_advisor`@3032, `execute_with_advisor`@3065) | 2955–3083 | 129 | **Phase 6 (0 tests on `execute_with_advisor` — tests first)** |
| Record building (`_build_close_record`@3084, `build_record`@3180; pre-schema gate ordering) | 3084–3358 | 275 | stays |
| Worker/queue/replay (`worker_loop`@3379, `replay_intake_webhooks`@3417) | 3359–3516 | 158 | stays |
| HTTP Handler (`class Handler`@3517; `emit_operator_alert`@3653, `maybe_emit_queue_saturation_alert`@3667) | 3517–3673 | 157 | stays |
| Startup/main (`log_execution_arm_state`@3674, `main`@3731) | 3674–3791 | 118 | stays |

**Boundary note (was the self-contradiction):** the four atomic-write helpers physically sit at
1424–1469 — *interleaved inside* the readiness region (readiness helper `_strategy_config_for_readiness`
at 1414–1423 precedes them; `build_strategy_execution_readiness` at 1470 follows). They are the
control-state persistence primitives (`save_control_state`@1191 → `_atomic_json_dump`; the state
hash@1769 → `_canonical_state_json`) and therefore belong to **Phase 2 (control-state)**, NOT Phase 3.
Caveat: `_atomic_json_dump`/`_fsync_dir`/`_fail_closed_state_write` are *also* called by the order
journal (`:1869`, `:1872`) and record-building (`:3176`, `:3330`, `:3355`). They are generic fs
primitives; their home module (`src/control_state.py`) must therefore be importable by Phase 4 and by
the receiver residue. Phase 2 preceding Phase 4 in the execution order satisfies this — the receiver
imports them back through the shim.

**Alert double-count removed:** the prior table counted an "Operator alert emission | 2192–2258 | 65"
row *on top of* a "Reconcile | 2003–2703 | 700" row that physically **contains** 2192–2258 — a
55–65-LOC double-count. Now the alert emitters (2192–2246, 55 LOC) are counted **once** under Phase 0,
and reconcile is split into part A (2003–2191) + part B (2247–2734) that *exclude* the alert range.

### `src/dashboard.py` — sums to **3,291** (verified)

Prior header said "3,293". Recount via both `wc -l` and `grep -c ''` returns **3,291**; the 3,293
figure was a stale 2-line over-estimate (named, not hidden). Table below tiles 1..3291.

| Cluster | Lines | ~LOC | Disposition |
|---|---|---|---|
| Top-of-file: imports, module constants (`CONTROL_STATE_FILE`@57) | 1–122 | 122 | stays |
| Strategy reads — D1 (`strategy_asset`@123, `load_strategy_files`@144, `is_strategy_active`@162) | 123–185 | 63 | **Phase 3** — reconcile call sites, then import |
| Control-state — D2 (`_load_control_state`@199 … `_set_trading_state`@343) | 186–356 | 171 | **Phase 2** — reconcile call sites, then import |
| Data projections (`active_strategies`@357 … `strategy_alert_rows`@476) | 357–505 | 149 | → Phase 7 `dashboard/model.py` |
| Formatting/HTML-escape (`money`@506 … `execution_badge`@671) | 506–675 | 170 | → Phase 7 `dashboard/render.py` |
| Executor construction + live/history snapshots (`_dashboard_executor`@772 … `okx_order_history_snapshot`@938) | 676–1181 | 506 | → Phase 7 `dashboard/snapshots.py` |
| OKX enrich/history (`symbol_from_inst_id`@1182 … `human_age`@1477) | 1182–1489 | 308 | → Phase 7 `dashboard/snapshots.py` |
| Health/freshness summaries (`executor_health_summary`@1490 … `freshness_summary`@1544) | 1490–1568 | 79 | → Phase 7 `dashboard/model.py` |
| Dashboard model + P&L contracts (`_build_dashboard_model`@1588, `_strategy_pnl_contract`@1673, `portfolio_contract`@1755) | 1569–1785 | 217 | → Phase 7 `dashboard/model.py` (TypedDicts here) |
| API payloads (`api_payload`@1786, `health_payload`@1865) | 1786–1898 | 113 | → Phase 7 `dashboard/model.py` |
| HTML rendering (`metric_cards`@1899 … `summary_cards`@2542) | 1899–2605 | 707 | → Phase 7 `dashboard/render.py` |
| `render()` master template (@2606) | 2606–2968 | 363 | → Phase 7 `dashboard/render.py` |
| HTTP Handler + control routes (`class Handler`@2969; `_apply_strategy_control`@3139) | 2969–3264 | 296 | → Phase 7 `dashboard/server.py` |
| Cache refresh loop (`_refresh_dashboard_cache_loop`@3265) | 3265–3291 | 27 | → Phase 7 `dashboard/server.py` |

## D1 / D2 are re-implementations with different signatures — NOT duplicates

The prior plan said "delete, import from Phase N module." That is unsafe: the dashboard copies are
**re-implemented with different names and signatures**, so Phase 2/3 must **reconcile call sites**, not
blind delete-and-import. Verified pairs:

**D1 — strategy reads.** `dashboard.py:strategy_asset(row) -> str`@123 vs
`webhook_receiver.py:strategy_asset(strategy: dict) -> str`@356 — same name, param renamed
(`row` vs `strategy`), receiver copy is type-annotated. Dashboard has no `strategy_instrument`
(receiver@330); receiver has no `is_strategy_active` (dashboard@162). Phase 3 must pick the canonical
signature and adapt the other side's callers.

**D2 — control-state.** Dashboard uses **`_`-prefixed private** names; receiver uses **public** names,
and one signature genuinely differs:

| Concern | dashboard.py | webhook_receiver.py | Reconcile action |
|---|---|---|---|
| load | `_load_control_state()`@199 | `load_control_state()`@1209 | rename callers |
| save | `_save_control_state(state)`@217 | `save_control_state(state)`@1191 | rename callers |
| override | `_set_strategy_override(sid, mode)`@237 | `set_strategy_override(sid, mode)`@1286 | rename callers |
| accounting start read | `_accounting_start_for(sid, ctrl_state=None)`@311 | `accounting_start_for(sid)`@1364 | **signature differs** |
| trading state read | `_get_trading_state(ctrl_state=None)`@335 | `get_trading_state()`@1401 | **signature differs** |

The dashboard variants accept an **optional `ctrl_state=` param** so a caller that already holds a state
dict avoids a re-read; the receiver variants always re-read. The unified `src/control_state.py` function
must keep the optional `ctrl_state=None` param (superset signature) so both call patterns survive; Phase 2
then updates the receiver's callers to the reconciled name/signature.

## `src/strategy/` disposition

Empty except `__init__.py`. Confirmed via `git log` this is a **leftover of a deleted subsystem**
(`decision_math.py`, added `b3e711ec`, removed `d5782f08` — "remove position_journal, decision_math,
and shadow paper-state subsystem"), NOT a stalled in-progress extraction.

**Decision: reuse it** in Phase 3 as the home for strategy-record + readiness logic. Don't delete.

## Phased Roadmap (risk-adjusted order; Phase 0 is new and must go first)

0. **`src/alerts.py`** (shared observability, ~55 LOC, monolith lines 2192–2246) — LOW risk, **must
   precede everything.** Houses the three cross-cutting emitters: `emit_operator_alert`,
   `emit_auth_failure_alert`, `maybe_emit_queue_saturation_alert`. These are called from **four
   distinct clusters that are NOT reconcile** — so leaving them inside Phase 5's `reconcile/` package
   (as the prior plan did) is a dependency inversion: the watchdog, HTTP Handler, and schema-enforcement
   path would all have to import *up* into `reconcile/`. Verified call sites:
   - `webhook_receiver.py:601` — `_maybe_watchdog_alert` (watchdog cluster) → `emit_operator_alert`.
   - `webhook_receiver.py:707` — `authenticate_webhook_request` passes `emit_auth_failure_alert` as a
     callback into `_authenticate_webhook_request_impl` (security wrapper cluster).
   - `webhook_receiver.py:1141` — `_alert_schema_enforcement_status` (normalize/schema cluster) emits
     `ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE` when schema validation is failing **open**. (This is the
     schema fail-*open* observability path, not the control-state fail-*close* path.)
   - `webhook_receiver.py:3653` and `:3667` — HTTP `Handler.do_POST` queue-full drop
     (`emit_operator_alert`) and the queue-saturation check (`maybe_emit_queue_saturation_alert`).
   - Plus the reconcile cluster (`:2274`…`:2726`) and `_fail_closed_state_write`'s alert ledger append.

   `emit_reconcile_alert` (monolith `:2247`, a thin wrapper that calls `emit_operator_alert`) **stays
   with reconcile** in Phase 5 (`reconcile/alerts.py`) and imports `emit_operator_alert` **from
   `src/alerts.py`**. Re-export all three shared emitters through `wr.` so watchdog/Handler/schema
   monkeypatch points survive.
   Verify: `test_intake_hardening.py`, `test_reconciliation_observe_only.py` (alert-row assertions),
   plus the full suite (these emitters are touched by ~everything).

1. **`src/signals/{normalize,dedupe}.py`** (~330 LOC, monolith 711–847 + 978–1167) — LOW risk.
   `normalize` referenced by 21 test files (best-tested code in the repo). Move `_SIGNAL_DEDUPE_INDEX`
   + `_SIGNAL_DEDUPE_LOCK` with `dedupe.py`. Note `_alert_schema_enforcement_status`@1141 calls
   `emit_operator_alert`, so `signals/` imports `src/alerts.py` → **Phase 0 must be done first.**
   Verify: `test_phase5_normalization_cleanup.py`, `test_phase3_idempotency.py`, `test_action_close_intake.py`.

2. **`src/control_state.py`** (~292 LOC = CRUD 1168–1413 + atomic-write helpers 1424–1469; kills
   dashboard D2 re-impl) — LOW–MED risk. Preserve `_atomic_json_dump`/`_fsync_dir`/`_fail_closed_state_write`
   semantics **verbatim**. Keep the superset signature (`ctrl_state=None`) so both dashboard and
   receiver call patterns survive (see D2 table). Must precede Phase 4 (order journal imports the
   atomic helpers).
   Verify: `test_phase3_runtime_controls.py`, `test_phase3_strategy_overrides.py`, `test_phase4_dashboard.py`,
   `test_dashboard_mode_aware.py`.

3. **`src/strategy/{records,readiness}.py`** (~301 LOC = receiver 330–491 + 1414–1423 + 1470–1598;
   kills dashboard D1 re-impl) — LOW–MED risk. Reuses the empty `src/strategy/` dir. Reconcile the D1
   signature difference before deleting the dashboard copy.
   Verify: `test_characterization_strategy_matching.py`, `test_phase6_strategy_schema_v2.py`, `test_phase5_exec_routing.py`.

4. **`src/orders/journal.py`** (~404 LOC, monolith 1599–2002) — MED risk. Self-contained state machine;
   owns the `_order_index()` cache (@1700) — move it with the functions. Imports the atomic-write
   helpers from `src/control_state.py` (`:1869`, `:1872`) → **Phase 2 must be done first.** Do before
   Phase 5 (reconcile calls `load_open_orders`@1954 and `record_order_state`@1890).
   Verify: `test_order_journal_checkpoint.py`, `test_order_state_machine.py`.

5. **`src/reconcile/`** package (~677 LOC = 2003–2191 + 2247–2734) — **HIGH risk, thinnest tested cluster.**
   - `reconcile/orders.py` — `map_order_outcome`, `reconcile_order_once`, `reconcile_order_with_backoff`,
     `reconcile_startup`
   - `reconcile/unknown_resolver.py` — `resolve_unknown_orders_once`, `unknown_resolver_loop`,
     `_resolve_planned_orphan`
   - `reconcile/drift.py` — `reconcile_position_drift`
   - `reconcile/executor_select.py` — `_effective_execution_config`, `_reconciliation_executor`,
     `_executor_for_order` (preserve per-order venue/mode read invariant)
   - `reconcile/alerts.py` — `emit_reconcile_alert` **only** (imports `emit_operator_alert` from
     `src/alerts.py`; the three shared emitters were moved to Phase 0).
   - Keep `pnl_ledger.reconcile_from_order_history` where it is; re-export both halves under
     `reconcile/__init__.py`.
   - **Write characterization tests FIRST** where coverage is 1 (`reconcile_order_once`,
     `resolve_unknown_orders_once`, `reconcile_position_drift` each have exactly 1 test today).

   **Prereqs: Phase 4 and Phase 0 only — NOT Phase 3.** Verified: grep across the reconcile cluster
   (monolith 2003–2734) finds **no** call to `load_strategy_files` / `build_strategy_execution_readiness`
   / `strategy_instrument`. The only cross-cluster deps are `load_open_orders` and `record_order_state`
   (both owned by Phase 4) and `emit_operator_alert` (Phase 0). The prior "depends on Phase 3" claim was
   false and is removed.
   Verify: `test_reconciliation_observe_only.py`, `test_receiver_reconcile_venue.py`,
   `test_unknown_resolver_controls.py`, `test_phase_b_robustness.py`, `test_phase_a_robustness.py`.

6. **`src/advisor.py`** (~129 LOC, monolith 2955–3083) — MED risk, **`execute_with_advisor`@3065 has
   ZERO tests today.** Write characterization tests first (anchor on `test_phase8_advisor.py` which
   covers `run_execution_advisor`@3032).
   Verify: `test_phase8_advisor.py`, `test_execution_gate_precedence.py`.

7. **`src/dashboard/` package split** — MED risk, do LAST. Sub-steps in order:
   1. `dashboard/snapshots.py` (676–1489, ~814 LOC) — executor construction + live/history snapshots;
      fold shared executor-build logic with `reconcile/executor_select.py`.
   2. `dashboard/model.py` (357–505 + 1490–1898, ~479 LOC) — data aggregation + P&L contracts + payloads.
      Define `DashboardModel`/`StrategyPnlContract`/`PortfolioContract` as `TypedDict`s here —
      highest-leverage typing move (types the seam feeding `render.py`).
   3. `dashboard/render.py` (506–675 + 1899–2968, ~1,240 LOC) — pure HTML/formatting functions of the model.
   4. `dashboard/server.py` (2969–3291) — `Handler`, auth, control routes; imports `src/control_state.py`.
   **Regression gate (hard):** `tests/test_pnl_api_contracts.py` — see the frontend-contract checklist
   below. Verify after each sub-step: `test_phase4_dashboard.py`, `test_dashboard_mode_aware.py`,
   `test_pnl_api_contracts.py`.

## Per-Phase Rollback & Verification Protocol

**Each phase = one commit, and the shim makes it git-revertible.** Because every phase leaves a
`from <pkg> import <fn>` re-export in the monolith (worked example above), the *only* things a phase
commit changes are: (a) the new package file(s), (b) the deleted function bodies + added re-export
lines in the monolith. `git revert <phase-commit>` restores the inline body and drops the import in one
step — the monolith is back to its pre-phase state with no manual surgery.

**Definition of "green" for a phase:**
1. `./.venv/bin/pytest tests/ -q` passes in full (the suite monkeypatches through `wr.<fn>`, so a
   broken shim surface fails here, not silently).
2. The phase-specific `Verify:` files listed above pass — these are the fast local gate before the full run.
3. `git diff --stat` shows **only** the new package file(s) and the monolith's deleted-body/added-import
   lines. Any *other* diff in the monolith means logic drifted during the move — reject the commit.

**Bisecting a regression across the shim boundary.** If a bug surfaces after phase N:
1. `git bisect` over the phase commits (they're linear and one-per-phase) isolates the suspect phase.
2. To confirm/deny the *extraction itself* (vs. a coincidental change): because the move was
   body-verbatim, you can **inline it back** — copy the function bodies from the new package file into
   the monolith, delete the `import` re-export, and run the suite. If the bug disappears, the
   extraction (import timing, a moved global, a lost monkeypatch seam) is the cause; if it persists, the
   bug predates the phase. This "move it back" test is only possible *because* the shim keeps the public
   surface identical.
3. Watch specifically for: a module-level global that didn't move with its functions (Phase 1
   `_SIGNAL_DEDUPE_INDEX`, Phase 4 `_order_index`), and monkeypatch targets that now resolve to the new
   module instead of `wr.` (tests must patch `wr.<fn>`, which the re-export preserves).

## Live-Deploy & Operational Safety

Verified against `deploy/deploy.sh`, `Dockerfile`, and both systemd units.

**Entrypoints do not move, so no deploy/CMD/unit path changes are needed:**
- `Dockerfile:75` — `CMD ["python", "src/webhook_receiver.py"]` (dashboard overrides CMD via compose).
- `deploy/hermx-receiver.service:12` — `ExecStart=/opt/hermx/.venv/bin/python src/webhook_receiver.py`.
- `deploy/hermx-dashboard.service:18` — `ExecStart=/opt/hermx/.venv/bin/python src/dashboard.py`.
None of these reference the internal module layout — they invoke the two top-level scripts, which keep
their paths and their public behavior across every phase.

**Docker copies `src/` wholesale — new subpackages need no Dockerfile edit:**
- `Dockerfile:37` — `COPY src/ ./src/`. New dirs (`src/signals/`, `src/control_state.py`,
  `src/strategy/*`, `src/orders/`, `src/reconcile/`, `src/advisor.py`, `src/dashboard/`, `src/alerts.py`)
  are picked up automatically. `deploy/deploy.sh:12` notes host deploys are `git pull` of `src/` — same
  story: whatever git tracks under `src/` ships. **No per-phase Dockerfile/deploy change required.**

**WAL-replay-on-restart is phase-agnostic.** `replay_intake_webhooks` (monolith 3417) stays in the
receiver and reads `raw-webhooks.jsonl` (the durable WAL) + the `signals.jsonl` processed-set. Neither
the WAL format nor the replay logic is touched by any phase (they're in the "stays" clusters). systemd
`Restart=always`/`RestartSec=5` means a restart can land **between** any two phase deploys; because each
phase is behavior-preserving (body-verbatim move + shim) and the on-disk ledger formats are invariant, a
mid-refactor restart replays identically regardless of which phase's image is running. The one rule:
**never split a phase across the WAL→queue ordering** (see invariants) — but no phase touches that path,
so replay safety holds throughout. It does **not** matter which phase is deployed when a restart fires.

## Circular-Import Risks — do NOT collapse these lazy points

Verified inline/lazy imports that exist specifically to break cycles. Future phases must leave them lazy:

- **`executors/ccxt_adapter.py:290-291`** — `import webhook_receiver as _wr` inside a function, commented
  `# lazy import avoids a receiver<->adapter import cycle at load time`. **The load-bearing cycle-breaker.**
  When Phase 5 extracts `reconcile/executor_select.py`, do not make the adapter import the new package at
  module top level — keep the receiver/adapter edge lazy.
- **`webhook_receiver.py:1106`** — `import jsonschema  # lazy: keep module import dependency-light`
  (optional heavy dep). Moves with `signals/normalize.py` (Phase 1); keep it function-local.
- **`dashboard.py` inline `from pnl_ledger import ...`** at `:968`, `:993`, `:1163`, `:1696`, `:1746`,
  `:1830` — deferred to keep `pnl_ledger` off dashboard's import-time graph. Phase 7 sub-steps must keep
  these function-local (do not hoist to `dashboard/model.py` or `snapshots.py` module top).

## Dockerfile / Frontend-Contract Checklist (Phase 7 gate)

- **Dockerfile:** `COPY src/ ./src/` already covers `src/dashboard/**` — no change. The dashboard SPA
  (`COPY --from=ui-builder /ui/out /app/dashboard-ui/out`, `Dockerfile:53`) and `STATIC_DIR` gate are
  untouched by the Python split.
- **API JSON contract (hard regression gate):** `tests/test_pnl_api_contracts.py` **exists** and pins the
  dashboard's public P&L API shape. It asserts `_strategy_pnl_contract(strategy, accounting_start_at,
  by_env, by_mode)` returns exactly the Phase-4 keys — `strategy_id`, `venue`, `mode`, `realized_gross`,
  `fees`, `realized_net`, `upl`, `total_net`, `trade_count`, `last_close_at_ms`, `accounting_start_at`,
  `closed_net_pnl_usd`, `closed_order_count` — with correct ledger-derived values, plus accounting-window
  filtering, per-mode row scoping, absent-ledger→zero, and the `portfolio` roll-up. When Phase 7-step-2
  moves `_strategy_pnl_contract`/`portfolio_contract` into `dashboard/model.py`, this test is the
  authority that the JSON the frontend consumes did not change shape. It must pass **after every Phase 7
  sub-step**, not just at the end.

## Dual-File Rules Maintenance (keep line-number rules in sync)

Several rules-file entries hardcode monolith line numbers that this refactor will move. Found via grep of
`.claude/rules/` and `.windsurf/rules/`; **current** correctness re-verified against the working tree:

| Rules reference (both `.claude/` and `.windsurf/` copies) | Points at | Actual now | Status |
|---|---|---|---|
| `webhook_receiver.py:280-284` (`load_engine_config`) | engine-config source | import @72, call `ENGINE_CONFIG = load_engine_config(...)` @295 | **already stale** |
| build_record gate "(line 2829)" / "(line 2831)" | `side`/`source` pre-schema gates | `side not in ALLOWED_SIDES` @3206, `source != "tradingview"` @3208–3209 | **already stale** |
| `dashboard.py:2335` (`STATIC_DIR`) | static-dir resolve | `STATIC_DIR = REPO_ROOT / ...` @2950 | **already stale** |
| `dashboard.py:2497-2518` (control-state write) | per-strategy mode write | `_save_control_state`@217; control routes ~3105–3170 | **already stale** |

The *rule content* is still correct in each case — only the line anchors drifted (they were never updated
after earlier edits). **New plan rule (add to the process):**

> Every phase that moves code referenced by a line-number anchor in a rules file MUST update **both**
> `.claude/rules/<file>` and `.windsurf/rules/<file>` in the **same commit** (dual-file protocol). At
> minimum, Phase 1 (schema/`build_record` gate refs), Phase 2 (control-state write ref), and Phase 7
> (`STATIC_DIR`, control-state write refs) touch code these anchors point at. Prefer replacing brittle
> `file:line` anchors with **symbol names** (`build_record`, `STATIC_DIR`, `_save_control_state`) so
> future moves don't re-stale them.

## Dashboard.py Typing Path (12% → incremental, no big-bang pass)

1. Type new modules at creation time (Phase 7 sub-steps) — don't retrofit later.
2. Define `TypedDict`s for the model boundary (`DashboardModel` etc.) in `dashboard/model.py` first —
   types `render.py`'s inputs for free.
3. Backfill the shrunken `dashboard.py` residue last, after Phase 7.
4. Add `mypy`/`pyright` (non-strict) scoped only to `src/dashboard/**`, `src/signals/`, `src/reconcile/`,
   `src/orders/`, `src/alerts.py` — gate CI on new dirs only, don't wall-of-errors the legacy files.

## Non-Goals & Invariants to Protect (every phase)

- **Money-ledger invariants** — `closed-trades.jsonl` append-only, NEVER pruned/rotated. Phase 4 moves
  order-journal rotation helpers (`_rotate_ledger_if_large`@879, order checkpoint/rotate @1839) — must
  not get wired to the closed-trades ledger during the move.
- **Read-side dedup key** `(exchange, inst_id, ord_id, mode)`, last-wins, preserve malformed rows —
  stays owned by `pnl_ledger.py`; don't reimplement in `reconcile/`.
- **WAL-before-queue** — `raw-webhooks.jsonl` fsync'd before enqueue; don't reorder intake→WAL→queue
  during the Handler/worker/replay touches.
- **Reconcile reads `(venue, mode)` from the order's own intent record**, never global defaults; skip
  non-terminal venue rows; never force UNKNOWN→REJECTED (Phase 5 `executor_select.py`).
- **Pre-schema gates precede jsonschema** in `build_record`: `side not in ALLOWED_SIDES` → 400
  (monolith @3206), `source != "tradingview"` → 202 (@3208–3209), both before `validate_alert_schema()`.
  Gate ordering must stay byte-identical when the schema validator (Phase 1) is split from record-build
  (stays in receiver). (Rules-file copies still cite the old lines 2829/2831 — update per dual-file rule.)
- **HMAC/replay/rate-limit order** in `Handler.do_POST` must not be reordered — the Handler stays in the
  receiver; only pure delegation moves.
- **Concurrency globals move WITH their functions**: `_SIGNAL_DEDUPE_INDEX`+lock (Phase 1),
  `_order_index()` cache (Phase 4), symbol fairness tickets (leave in receiver, bound to `worker_loop`).
- **`emit_reconcile_alert` stays in `reconcile/`** and imports `emit_operator_alert` from `src/alerts.py`
  (Phase 0) — never the reverse (no `alerts.py` → `reconcile/` edge).
- **Compatibility shim required every phase** — keep `wr.<fn>` re-export surface intact; the test suite
  monkeypatches through it.
- Explicit non-goals: don't collapse the lazy-import cycle-avoidance points enumerated above; don't touch
  `execution/service.py` or `executors/`.

## Execution Order (risk-adjusted value)

Phase 0 (shared alerts — unblocks 1/5) → 1 → 2 (before 4) → 3 (low-risk, kill re-impl, best-tested) →
4 (before 5) → 6 (add tests first) → 5 (highest risk; needs 4 + 0) → 7 (dashboard split, last,
type as you go; `test_pnl_api_contracts.py` gate).

Run full `tests/` suite (`./.venv/bin/pytest tests/ -q`) green after each phase; phase-specific `Verify:`
files are the fast local gate.
