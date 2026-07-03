# P&L & Dashboard Accounting — File-by-File Execution Plan

> Status: proposal (plan only, no code). Author: accounting-correctness pass, 2026-07-03.
> Scope: fix durable closed P&L, fee-correct net, accounting window, strategy/portfolio
> contracts, React↔Python UPnL disagreement, dynamic budget, external-order attribution.

## Problem statement (verified)

The dashboard derives *all* P&L from the live venue position read (`exchange_live_snapshot` →
`executor.health()["positions"]`). When a position goes FLAT, the venue's per-position
`realizedPnl` is no longer attached to any open row, so **closed P&L vanishes from the
UI**. There is no durable HermX closed-trade ledger. Additional defects:

- `strategy_card` (`dashboard.py:1563,1598`) sets `pnl_now = realized + upl` and then
  **labels the total as "UPnL"** — while React `StrategyCard.tsx:132` labels UPnL as pure
  `upl`. Renderers disagree.
- Fees are ignored in the live path (`pnl_now = realized + upl`).
- No accounting-start / clean-window mechanism exists.
- `reinvest` flag exists in strategy JSON + `types.ts:25` but is never read.
- Exchange order history is bounded to 100 rows; `pipeline.jsonl` to 500 (`SIGNALS_MAX_N`).
  Closed P&L must not depend on these bounds.
- No `clOrdId` attribution — external/manual exchange orders can leak into strategy P&L.
- `position_pnl()` (`dashboard.py:671`) is dead code (confirmed: zero callers).

## Attribution key (foundation for the whole plan)

- Signal-path closes: `clOrdId` = `stable_client_order_id()` → `"mxc" + sha256(...)`,
  32 chars (`webhook_receiver.py:707-709`). **Prefix `mxc`.**
- Operator closes: `clOrdId` = `operator_close_<symbol>_<strategy_id>_<UTCday>`
  (`webhook_receiver.py:2571-2577`). **Prefix `operator_close_`.**
- Anything else (empty `clOrdId`, or a foreign prefix) = **external/manual** → excluded
  from strategy P&L.

> **Hyperliquid caveat.** Hyperliquid does not accept arbitrary client order ids; the adapter
> hashes `clOrdId` into the venue's numeric `cloid` space. Attribution on Hyperliquid must
> reverse that hash (or carry a side map) rather than assume the raw `mxc`/`operator_close_`
> prefix survives on the venue-returned order. Prefix matching is OKX-shaped and must not be
> assumed venue-universal.

The **`ordId`** field is the durable ledger dedupe key (stable across restarts and cache
expiry, unlike `received_at`).

## Design invariant

Closed P&L is made durable by **persisting each confirmed HermX reduce-only close into an
append-only ledger the moment it is observed in exchange order history**, keyed by `ordId`.
Once written, it never depends on live-position state or the 100-row history window again.
This mirrors the repo's WAL/ledger idiom (raw-webhooks.jsonl, signals.jsonl): durable
append-only file, dedupe on a collision-safe key, replay-safe.

New state file follows the mount rule: `HERMX_DATA_DIR / "closed-trades.jsonl"` (must be on
the `hermx-state:/app/data` rw mount — same requirement already documented for
`control-state.json`).

## Venue neutrality

The ledger is a HermX artifact, not an OKX artifact. It must remain correct as new venues
are added behind the same executor contract. Concretely:

1. **The executor contract is venue-agnostic.** `src/executors/base.py` defines the abstract
   surface (`health()`, `get_order_history_raw()`, etc.); the ledger and reconcile paths
   depend only on that contract, never on a concrete venue class.
2. **Reconcile reads CCXT-normalized fields, not raw exchange payloads.** Where a unified
   CCXT field exists (`order["reduceOnly"]`, `order["symbol"]`, `order["fee"]`), reconcile
   reads it. Raw `info.*` is a *fallback*, not the primary source.
3. **Per-venue realized-P&L extraction lives in the adapter.** Normalizing each venue's
   realized-P&L representation into a common field is the job of `ccxt_adapter.py`
   (`get_order_history_raw`), not the ledger. The ledger consumes one normalized field.
4. **`info.pnl` and `info.reduceOnly` are OKX-specific and must be abstracted.** OKX exposes
   realized P&L at `info.pnl` and the reduce-only flag at `info.reduceOnly`; other venues do
   not. These raw accesses must be wrapped by the adapter and surfaced as normalized
   `realized_pnl` / top-level `reduceOnly`, so the ledger never hard-codes an OKX field path.
5. **Hyperliquid `clOrdId` hashing.** As noted above, Hyperliquid rewrites client order ids
   into a numeric `cloid`; attribution logic must not assume the raw HermX prefix survives on
   the venue-returned order.

Where this plan still names OKX fields (`instId`, `info.pnl`, position `realizedPnl`), read
them as *the OKX instance of a normalized concept* — the concrete field is an implementation
detail of the adapter, and the ledger/contract layers stay venue-neutral.

---

## PHASE 1 — Durable closed-trade ledger (MVP, standalone value)

**Goal.** Persist every confirmed HermX close to `closed-trades.jsonl` so closed P&L
survives the position going FLAT and survives the 100-row history bound. Value even if no
later phase ships: a permanent, attributable record of realized trades on disk.

**Files changed.**
- `src/pnl_ledger.py` **(new module).**
  - `CLOSED_TRADES_FILE = Path(os.environ.get("HERMX_DATA_DIR", ROOT)) / "closed-trades.jsonl"`
  - `is_hermx_cl_ord_id(cl_ord_id) -> bool` — `startswith("mxc")` or `startswith("operator_close_")`.
  - `read_closed_trades(limit=None) -> list[dict]` — reverse-tail JSONL read, corrupt-line tolerant (mirror `read_jsonl_stats`).
  - `_existing_ord_ids() -> set[str]` — scan ledger once per reconcile.
  - `reconcile_from_order_history(history_rows, config) -> int` — for each history row that is
    reduce-only, `state` terminal (filled), HermX-attributed (`is_hermx_cl_ord_id`), and whose
    `ordId` not already in the ledger: append a normalized row. Returns count appended.
    **Reduce-only is read as `order.get("reduceOnly")` (CCXT top-level, venue-neutral) with an
    `info.reduceOnly` fallback** — never a bare `info.reduceOnly` access.
  - `append_closed_trades(rows)` — atomic append with fsync (reuse the atomic-write idiom
    from `save_control_state` / gate-lib sidecar).
  - Row schema v1: `{ord_id, cl_ord_id, strategy_id, symbol, inst_id, side, contracts,
    avg_px, notional_usd, gross_realized_pnl, fee, fee_ccy, u_time, c_time, recorded_at,
    source, venue_meta}`. All keys are snake_case; OKX-only raw fields (`tdMode`, `lever`,
    `posSide`, and the raw `instId` echo) live under the `venue_meta` sub-dict, never at the
    top level — so the top-level schema stays venue-neutral. `gross_realized_pnl` reads the
    **normalized `realized_pnl` field** the adapter populates (see below), not a raw
    venue-specific path. `strategy_id` derived by reversing `symbol_from_inst_id()` + (for
    `mxc` ids, left null in v1 — resolved in Phase 3 via cl_ord_id→strategy map; operator ids
    embed it).
  - **Normalized realized P&L (venue contract).** `get_order_history_raw` in the adapter must
    populate a normalized `realized_pnl` field per venue: OKX from `info.pnl`; Binance/Bybit
    from `fetchMyTrades` / income endpoints. The ledger reads only this normalized field and
    stays agnostic to how each venue reports realized P&L.
- `src/dashboard.py` — in `exchange_order_history_snapshot()` (line 774), after `rows =
  executor.get_order_history_raw(...)` succeeds, call
  `pnl_ledger.reconcile_from_order_history(rows, config)` inside a `try/except` that never
  fails the snapshot (reconcile is best-effort; a ledger error must not break the read path).
- `tests/test_pnl_ledger.py` **(new).**

**Changes.**
- Add the module + one call site. No existing behavior altered; the snapshot payload is
  unchanged. Reconcile is idempotent (ordId dedupe) so repeated renders are safe.
- Delete dead `position_pnl()` (`dashboard.py:671-684`) in this phase — zero callers,
  removes a misleading second P&L implementation. (Independent; can be split out.)

**Tests.**
- `test_reconcile_appends_new_hermx_closes` — feed history rows (mxc + operator_close),
  assert ledger has one row per unique ordId with correct fields.
- `test_reconcile_is_idempotent` — run twice, ledger length unchanged.
- `test_reconcile_excludes_external_orders` — foreign/empty clOrdId row is NOT written.
- `test_reconcile_excludes_open_and_non_reduceonly` — opens and non-reduceOnly filtered.
- `test_read_closed_trades_tolerates_corrupt_line` — partial trailing line ignored, prior
  rows returned (exercise the production reader, not a re-implementation).
- `test_ledger_survives_position_flat` — reconcile from history, then simulate empty live
  positions; `read_closed_trades()` still returns the closes.

**Rollback safety.** Delete `src/pnl_ledger.py` and the single call in
`exchange_order_history_snapshot`. The ledger file is additive; leaving it on disk is harmless.
No schema migration, no change to any served field.

**Known limitations (deliberate).** No fees math yet (raw `fee`/`pnl` stored, not netted).
No accounting window (whole ledger). `strategy_id` for `mxc` closes not yet resolved. Not
surfaced in any UI/API. Relies on reconcile running before a close ages out of the 100-row
window — mitigated in Phase 3 (cron safety net); acceptable for MVP given render cadence.

---

## PHASE 2 — Fee-correct net P&L + venue semantics reconciliation

**Goal.** Establish whether a venue's order `pnl` is gross or net of fees, then expose a
`net_realized_pnl` per closed trade. Prevent silently double-counting or ignoring fees.

**Files changed.**
- `src/pnl_ledger.py` — add `net_realized_pnl` to the row schema (v2):
  `net = gross_realized_pnl + fee` (on OKX `fee` is signed negative for paid fees; verified
  that `pnl` and `fee` are separate fields in `get_order_history_raw`,
  `ccxt_adapter.py:828-830`). Add `schema_version` to each row; `read_closed_trades`
  back-fills `net_realized_pnl` for v1 rows on read.
- `docs/PNL_ACCOUNTING_EXECUTION_PLAN.md` — record the empirical finding (this file).
- `tests/test_pnl_ledger.py` — extend.

**Changes.**
- **Verification task (one-time, empirical, per-venue, not a code change):** on the venue's
  demo account (OKX first), close a known position and compare `sum(gross_realized_pnl + fee)`
  from the ledger against the position-level `realizedPnl` reported by `health()` just before
  FLAT. Document which is fee-inclusive. If position `realizedPnl` already nets fees, the
  ledger's `net = pnl + fee` is the correct comparable; if `pnl` is already net, set
  `net = pnl` and keep `fee` informational. Encode the outcome as a module constant
  `ORDER_PNL_IS_NET` (**per-venue** — position-level `realizedPnl` is *not* a CCXT-unified
  field, so this check and its constant are keyed by venue, not global) with a comment citing
  the observed reconciliation.
- Net computation is pure/deterministic; no I/O added.

**Tests.**
- `test_net_equals_gross_plus_signed_fee` (parametrized over `ORDER_PNL_IS_NET`).
- `test_v1_rows_backfill_net_on_read`.
- `test_missing_fee_treated_as_zero_not_none` — `fee=None` → net == gross, no crash.

**Rollback safety.** Net is a derived field; drop the constant + net computation and rows
still read (gross/fee retained). No serialized contract consumer yet, so no downstream break.

**Known limitations.** Funding fees not modeled (perpetual funding is a separate venue ledger;
out of scope — noted for a future phase). Assumes `feeCcy` == quote currency (USDT); a
non-USDT fee currency is logged but not FX-converted. The Phase 2 empirical check is
per-venue: each new venue needs its own `ORDER_PNL_IS_NET` determination before its net
numbers are trusted.

---

## PHASE 3 — Strategy P&L contract + accounting window

**Goal.** Produce the `strategy_pnl` object from the ledger, scoped by a per-strategy
`accounting_start_at`, and expose it in the API payload. This is the first phase that
changes served data.

**Files changed.**
- `src/webhook_receiver.py` — add accounting-window storage in `control-state.json`
  (already the per-strategy override store, `_load_control_state`/`save_control_state`,
  lines 1162-1201). New helpers mirroring `set_strategy_override` (line 1259):
  `set_accounting_start(strategy_id, iso_ts)` and reader
  `accounting_start_for(strategy_id) -> str|None`, stored under a new
  `state["accounting_windows"][strategy_id]` key. No change to `strategy_overrides`.
- `src/pnl_ledger.py` — `aggregate_strategy_pnl(strategy_id, budget_usd, since_iso,
  cl_ord_id_to_strategy) -> dict` returning the contract:
  `{budget_usd, closed_realized_pnl_usd, closed_fees_usd, closed_net_pnl_usd,
  open_upl_usd, equity_now_usd, closed_order_count, accounting_start_at}`. Filters ledger
  rows to `strategy_id` (via cl_ord_id→strategy map for `mxc`, embedded id for operator
  closes) and `u_time >= since`. `open_upl_usd` is passed in from the live snapshot;
  `equity_now_usd = budget_usd + closed_net_pnl_usd + open_upl_usd`.
- `src/dashboard.py` — build `strategy_pnl` in the API assembly (where `strategy_card` /
  `api_payload` gather `exchange_live` + strategy), attach it per strategy. Provide the
  cl_ord_id→strategy map by deriving it from the strategy set (symbol/inst_id) — a signal
  close's strategy is resolvable via its `inst_id` when only one strategy trades a symbol;
  where ambiguous, fall back to inst_id-level attribution and flag `strategy_ambiguous`.

**Changes.**
- Add `strategy_pnl` to the payload (additive field; existing fields untouched).
- Add an optional cron safety-net entry (Hermes cron, per the monitor pivot memory) that
  calls the reconcile path on a fixed cadence so closes are captured even if the dashboard
  is idle and a close would otherwise age past the 100-row window. Pin `--provider/--model`
  if it's an LLM job (it is not — it's a plain reconcile call), so this is a non-LLM cron.

**Tests.**
- `test_aggregate_respects_accounting_start` — rows before `since` excluded.
- `test_equity_now_formula` — `equity = budget + closed_net + open_upl`.
- `test_closed_order_count_matches_rows`.
- `test_accounting_start_roundtrip` — `set_accounting_start` → `accounting_start_for`
  persists across a reload (writable-mount path).
- `test_strategy_pnl_absent_ledger` — empty ledger yields zeros, not None/errors.
- `test_ambiguous_symbol_falls_back_to_inst_level` with `strategy_ambiguous=True`.

**Rollback safety.** `strategy_pnl` is additive — remove the assembly call and the payload
reverts. `accounting_windows` is a new key in control-state; absent → treated as
epoch-0 (whole ledger), so removing the setters degrades gracefully. Consumers (React) not
yet reading it, so no UI break.

**Known limitations.** cl_ord_id→strategy resolution is heuristic for multi-strategy-per-
symbol setups (flagged, not solved). Accounting window is set via API/helper only; no UI
control yet (Phase 4). Portfolio/global equity not yet produced (Phase 5).

---

> **Phases 1-3 above are the minimal viable path to durable, fee-correct, windowed closed
> P&L. Each is independently committable and Phase 1 delivers standalone value.**

---

## PHASE 4 — Fix UPnL inconsistency + render `strategy_pnl`

**Goal.** Make Python and React render identical, correctly-labeled numbers sourced from
`strategy_pnl`. Kill the `realized+upl`-mislabeled-as-UPnL bug.

**Files changed.**
- `src/dashboard.py` `strategy_card` (lines 1560-1601) — replace the live-only math:
  - `budget` ← `strategy_pnl.budget_usd`
  - `"UPnL"` metric ← `strategy_pnl.open_upl_usd` (pure unrealized, **fixes line 1598**)
  - add `"Closed net"` ← `strategy_pnl.closed_net_pnl_usd`
  - `"Equity now"` ← `strategy_pnl.equity_now_usd`
  - color by the corresponding component's sign, not the conflated `pnl_now`.
- `dashboard-ui/components/StrategyCard.tsx` (lines 96,132) — source `equityNow` and the
  UPnL cell from the `strategy_pnl` fields on the position/strategy payload; UPnL stays
  `open_upl_usd`; add a "Closed net" line. Remove the local `budget + realized + upl`
  recompute so the number can't drift from Python.
- `dashboard-ui/lib/types.ts` — add `StrategyPnl` interface mirroring the Phase 3 contract;
  reference it from `Strategy`/payload types.

**Changes.** Both renderers read one server-computed contract; no client-side P&L arithmetic
remains. Labels: "UPnL" = unrealized only; realized shown separately.

**Tests.**
- Python: `test_strategy_card_upnl_is_open_only` — assert the UPnL cell equals
  `open_upl_usd`, not `realized+upl`.
- Python: `test_strategy_card_equity_uses_contract`.
- React: component test (existing test harness) asserting UPnL cell binds `open_upl_usd`
  and equity binds `equity_now_usd`; no local recompute.

**Rollback safety.** Revert the three files; the API still emits `strategy_pnl` (harmless if
unread) and the old live-only cards return. Pure presentation change.

**Known limitations.** No accounting-window reset control in the UI yet (button deferred).
Portfolio card not added here.

---

## PHASE 5 — Portfolio / account-level contract

**Goal.** A `portfolio` object with account/global equity independent of any single
strategy.

**Files changed.**
- `src/dashboard.py` — build `portfolio` from `exchange_live_snapshot()["account"]` (already
  captured at line 763) + ledger totals: `{account_equity_usd, total_budget_usd,
  total_closed_net_pnl_usd, total_open_upl_usd, hermx_equity_usd, unattributed_pnl_usd}`.
  `unattributed_pnl_usd` = account movement not explained by HermX ledger (bridges to
  Phase 7). Attach to `api_payload`.
- `dashboard-ui/lib/types.ts` — `Portfolio` interface.
- `dashboard-ui/app/page.tsx` (or the dashboard root component) — render a portfolio header
  card above the strategy grid.

**Tests.**
- `test_portfolio_totals_sum_strategies` — portfolio closed-net equals sum of per-strategy.
- `test_portfolio_account_equity_passthrough` — account block surfaced verbatim.
- React render test for the portfolio card.

**Rollback safety.** Additive payload field + one new component. Revert three files.

**Known limitations.** `unattributed_pnl_usd` is computed but not itemized until Phase 7.
Multi-account not modeled (single venue account assumed).

---

## PHASE 6 — Dynamic vs fixed budget modes

**Goal.** Give the dead `reinvest` flag meaning, or replace it with an explicit
`dynamic_budget`.

**Files changed.**
- `src/pnl_ledger.py` (or a small `budget.py` helper) — `effective_budget(strategy,
  strategy_pnl) -> float`: fixed mode → `capital.budget_usd`; dynamic mode →
  `budget_usd + closed_net_pnl_usd` (compounds realized net into working capital; open UPnL
  excluded to avoid marking-to-market the budget).
- `strategies/*.json` + loader — introduce `capital.dynamic_budget: bool` as the canonical
  field; treat legacy `capital.reinvest` as its alias for back-compat (read `dynamic_budget
  or reinvest`). Update the four strategy files' notes/comment, not their live values.
- `dashboard-ui/lib/types.ts` — add `dynamic_budget?: boolean` to `Capital`; keep
  `reinvest` for compatibility.

**Changes.** `strategy_pnl.budget_usd` in Phase 3 becomes `effective_budget(...)`; equity
formula unchanged (uses the effective budget). Fixed mode is the default (no behavior change
for existing strategies unless `reinvest/dynamic_budget` true).

**Tests.**
- `test_fixed_budget_ignores_pnl`.
- `test_dynamic_budget_adds_closed_net_only` — excludes open UPnL.
- `test_reinvest_alias_maps_to_dynamic`.
- `test_dynamic_budget_never_below_zero_guard` (clamp if closed net < -budget; log, don't
  crash).

**Rollback safety.** Default is fixed = today's behavior. Revert the helper wiring; the
`dynamic_budget` field becomes inert again (like `reinvest` is now).

**Known limitations.** No per-trade sizing change — this only affects *displayed* working
capital/equity, not order sizing in the executor (sizing wiring is a separate, riskier
change, explicitly out of scope).

---

## PHASE 7 — External / manual order detection

**Goal.** Surface (and exclude from strategy P&L) venue activity HermX did not originate.

**Files changed.**
- `src/pnl_ledger.py` — `classify_orders(history_rows)` partitioning into
  `hermx` vs `external` by `is_hermx_cl_ord_id`; `external_pnl(history_rows)` summing
  external reduce-only `pnl+fee`. Harden `enrich_close_rows_with_order_history`
  (`dashboard.py:871`) to **prefer clOrdId match** before the current
  inst_id+side+reduceOnly+time-delta heuristic, so a coincident manual order can't be matched
  to a HermX close.
- `src/dashboard.py` — set `portfolio.unattributed_pnl_usd` from `external_pnl(...)` and add
  an `external_activity` flag/count to the payload.
- `dashboard-ui/app/page.tsx` (or portfolio card) — show a "manual/external activity"
  indicator when nonzero.

**Tests.**
- `test_classify_splits_hermx_vs_external`.
- `test_external_pnl_excluded_from_strategy` — an external close does not move any
  `strategy_pnl`.
- `test_enrich_prefers_clordid_over_time_match` — given a manual order colliding in time with
  a HermX close, enrichment binds the HermX one.

**Rollback safety.** Classification is read-only; revert to restore the old enrichment
heuristic and drop the indicator. No stored data affected.

**Known limitations.** Cannot attribute external orders to intent (they're by definition
outside HermX). Assumes HermX always stamps `clOrdId` (true for both signal and operator
paths, verified). Detection is only as complete as the 100-row window for external rows
(HermX rows are already durable via the ledger); noted.

---

## Cross-cutting risk mitigations

- **History-window race (100 rows).** Closes must be reconciled before they age out.
  Mitigations: reconcile on every history fetch (Phase 1) + Hermes cron safety net
  (Phase 3). If a gap is ever suspected, `get_order_history_archive` (`ccxt_adapter.py:844`)
  or a venue archival endpoint (e.g. OKX `fills-history`) can seed a one-time backfill —
  documented, not automated.
- **Writable mount.** `closed-trades.jsonl` and `control-state.json` accounting keys require
  the `hermx-state:/app/data` rw mount even under `read_only: true` (existing documented
  requirement — verify the compose file before deploy).
- **Idempotency.** ordId dedupe makes reconcile safe under systemd `Restart=always`; no
  double-counting across restarts.
- **Fee-sign correctness.** Gated behind the Phase 2 empirical reconciliation before any net
  number is shown, so a sign error can't ship silently.
- **Shared-lib touch.** `webhook_receiver.py` control-state helpers are shared; the Phase 3
  additions are new keys/functions only (no change to `strategy_overrides` semantics) — but
  per dev-rules, get explicit confirmation before editing that file.
- **Dual renderer drift.** Phase 4 removes all client-side P&L math so React/Python can't
  diverge again; a contract test asserts both bind the same fields.

## Phase → file → constraint check

| Phase | Source files touched | ≤3? |
|-------|----------------------|-----|
| 1 | pnl_ledger.py (new), dashboard.py | ✅ |
| 2 | pnl_ledger.py | ✅ |
| 3 | webhook_receiver.py, pnl_ledger.py, dashboard.py | ✅ (3) |
| 4 | dashboard.py, StrategyCard.tsx, types.ts | ✅ (3) |
| 5 | dashboard.py, types.ts, page.tsx | ✅ (3) |
| 6 | pnl_ledger.py, types.ts, strategies loader | ✅ (3) |
| 7 | pnl_ledger.py, dashboard.py, page.tsx | ✅ (3) |

Test files are additive and not counted against the 3-source-file limit.
