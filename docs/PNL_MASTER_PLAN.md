# P&L & Dashboard Accounting — Master Plan

> **Status:** proposal (plan only, no code). Author: accounting-correctness consolidation, 2026-07-03.
> **Supersedes:** `PNL_ACCOUNTING_EXECUTION_PLAN.md` and `CCXT_VENUE_NEUTRALITY_VALIDATION.md` as the single source of truth. Those remain as source material; this document is authoritative where they disagree.
> **Scope:** durable closed P&L, fee-correct net, accounting window, demo/live separation, venue-neutral normalization, React↔Python contract, dynamic budget, external-order attribution.

This is the definitive reference for the P&L/dashboard accounting work. All findings are verified against code (`dashboard.py`, `ccxt_adapter.py`, `webhook_receiver.py`, `StrategyCard.tsx`) and the installed CCXT source (`4.5.61`).

---

## Section 1 — Issue Registry

19 verified issues, ranked by severity. Each fix references these numbers in Section 4.

| # | Severity | Issue | Impact | Current workaround | Reference |
|---|----------|-------|--------|--------------------|-----------|
| **10** | **CRITICAL** | **No durable HermX closed-trade ledger.** `ORDER_JOURNAL_FILE` tracks state transitions only; `pipeline.jsonl` is bounded. No strategy-scoped, append-only execution ledger exists. This is the architectural root: everything downstream re-derives P&L from ephemeral state. | There is no permanent record of realized trades. All other P&L defects follow from this. | None — closed P&L is simply lost. | `ORDER_JOURNAL_FILE`; `pipeline.jsonl` |
| **1** | **CRITICAL** | **P&L vanishes on FLAT.** `strategy_card` and `StrategyCard.tsx` compute equity as `budget + realized_pnl + upl`, where `realized_pnl`/`upl` come from `okx_live_snapshot()` → `executor.health()` → `fetch_positions()`. When a position closes, `realizedPnl` detaches from the (now-absent) open row → closed P&L disappears from the UI. | Operator sees equity snap back to budget the moment a trade closes; realized gains/losses are invisible. | Screenshot P&L before closing; read the venue UI directly. | `dashboard.py:1560-1564`, `StrategyCard.tsx:96` |
| **2** | **CRITICAL** | **Dashboard reads ONLY the demo/sandbox account.** `_dashboard_executor()` never sets `simulated_trading`, so the adapter defaults to `True` → `set_sandbox_mode(True)`. The dashboard always reads the OKX demo account regardless of strategy mode. Live-account positions are **never** displayed. | Operators running live strategies see demo positions/P&L — a dangerous false picture of real money. | Read the venue's own web UI for live positions. | `dashboard.py:697-716` |
| **3** | **HIGH** | **Demo/Live toggle is cosmetic for the dashboard.** `control-state.json` override changes only badge color and receiver execution routing. The positions snapshot (`okx_live_snapshot`) never consults `strategy_overrides`. | Toggling a strategy to "live" gives no live data on the dashboard; reinforces the Finding-2 illusion. | Same as #2. | `okx_live_snapshot`, `strategy_overrides` |
| **4** | **HIGH** | **Fees completely ignored.** `pnl_now = realized + upl` has no fee term. `position_pnl()` computes a fee-aware net but is **dead code** (zero callers). | Displayed P&L overstates net by cumulative fees; compounds over trade count. | Mentally discount for fees. | `dashboard.py:671` (dead), live path `1560-1564` |
| **5** | **HIGH** | **React and Python disagree on UPnL.** React labels UPnL as pure `upl` (`StrategyCard.tsx:132`); Python labels `realized + upl` as "UPnL" (`strategy_card:1598`). Two renderers, two different numbers under the same label. | The same metric shows different values depending on which renderer is active; the Python value is mislabeled. | Know which surface you're looking at. | `StrategyCard.tsx:132`, `dashboard.py:1598` |
| **8** | **HIGH** | **OKX order history bounded to 100 records.** `get_order_history_raw` default `limit=100`; `okx_order_history_snapshot` passes 100. No pagination. | A close can age out of the window before it is ever recorded → permanent loss once a ledger depends on this read. | None. | `get_order_history_raw`, `okx_order_history_snapshot` |
| **11** | **HIGH** | **`info.get("pnl")` is OKX-only.** CCXT `safe_order` has no `pnl` key (verified base `4403-4429`). Per-order realized P&L lives only in each venue's raw `info`: OKX `info.pnl`, **Hyperliquid `info.closedPnl`**, Binance `trade.info.realizedPnl`, Bybit `fetch_positions_history` closed-pnl. A bare `info.get("pnl")` returns **`None` → silent $0** on Hyperliquid. | Ledger records $0 realized P&L on every non-OKX close, no error, no log. | OKX-only deployment. | `ccxt_adapter.py:828-830`; CCXT `okx.py:3827`, `hyperliquid.py:3075` |
| **15** | **HIGH** | **Hyperliquid `clOrdId` hashing breaks attribution.** Hyperliquid rejects arbitrary client order ids; the adapter hashes HermX `mxc…` into the venue's numeric `cloid`. `startswith("mxc")` attribution fails on the venue-returned order. | HermX closes on Hyperliquid are misclassified as external → excluded from strategy P&L. | OKX-only deployment. | `webhook_receiver.py:707-709`; CCXT `hyperliquid.py` cloid path |
| **16** | **HIGH** | **Reduce-only gate is derivatives-only.** Coinbase/spot venues return `reduceOnly=None` (CCXT `coinbase.py:3204`), so a reconcile filter of "row is reduce-only" drops **every** spot close. | On any spot venue, no closes are ever reconciled into the ledger. | Derivatives-only deployment. | CCXT `coinbase.py:3204` |
| **17** | **HIGH** | **No `cl_ord_id`-based P&L attribution.** `enrich_close_rows_with_okx_history` matches by `instId` + `side` + `reduceOnly` + time-delta. An external/manual order coincident in time can be bound to a HermX close. | Manual trades can be miscredited to a strategy, corrupting its P&L. | Don't trade manually on the same symbol. | `enrich_close_rows_with_okx_history`, `dashboard.py:871` |
| **6** | **MEDIUM** | **No accounting start / clean window.** No `accounting_start_at` anywhere. Old exchange history contaminates new dashboard state. | Numbers include pre-HermX or pre-reset history with no way to zero a strategy. | None. | (absent) |
| **9** | **MEDIUM** | **`pipeline.jsonl` bounded to 500 events.** `SIGNALS_MAX_N = 500`. Not a lifetime ledger. | Cannot be used as the durable P&L source; silently truncates. | None. | `SIGNALS_MAX_N` |
| **12** | **MEDIUM** | **`info.get("reduceOnly")` is OKX-only.** CCXT unifies `reduceOnly` at top-level (`safe_order:4422`), but `ccxt_adapter.py:831` still reads `info.get("reduceOnly")`. Works on OKX only because OKX's raw key coincidentally matches (and is a string `'true'`/`'false'`). | Reduce-only detection is fragile/venue-coincidental; downstream truthiness must handle the `"false"` string. | OKX-only deployment. | `ccxt_adapter.py:831`; CCXT base `4422` |
| **13** | **MEDIUM** | **Position `realizedPnl` not unified, and not surfaced.** Only OKX (`okx.py:5969`) and Bybit (`bybit.py:6493`) set it in `parse_position`; Binance/Hyperliquid don't. `get_positions()`/`health()` never read `row.get("realizedPnl")` (`ccxt_adapter.py:852-876` maps only `unrealizedPnl → upl`). | The Phase-2 empirical fee check is not executable today without an adapter change, and is OKX/Bybit-only. | None. | `ccxt_adapter.py:852-876` |
| **14** | **MEDIUM** | **`fetch_positions_history` only OKX + Bybit.** Binance/Hyperliquid/Coinbase do not implement it. Not a portable primitive. | Cannot build the ledger's realized-P&L path on it. | N/A (informs design). | CCXT `okx.py`, `bybit.py:8486` |
| **19** | **MEDIUM** | **Writable-mount requirement.** Dashboard writes `control-state.json` and must have `hermx-state:/app/data` mounted rw even with `read_only: true` on root fs; otherwise writes fail silently. New `closed-trades.jsonl` inherits this. | Accounting-window resets and the ledger silently fail to persist if the mount is missing. | Ensure compose mounts `hermx-state`. | code-quality memory; `dashboard.py:2497-2518` |
| **7** | **LOW** | **`reinvest` flag is dead.** Present in `capital.reinvest: true` and `types.ts:25`, never read in business logic. | Operators may believe reinvestment is on; it does nothing. | None. | `strategies/*.json`, `types.ts:25` |
| **18** | **LOW** | **`budget_usd` lives in strategy config JSON.** `capital.budget_usd` in `strategies/*.json` mixes accounting state into strategy definition. | Strategy config is polluted with mutable accounting state; hard to reset without editing strategy files. | Edit the strategy JSON. | `strategies/*.json` |

> Two additional structural facts, not defects but load-bearing constraints:
> - **`fetch_positions_history` closed-pnl endpoints (Bybit)** are the *correct* Bybit realized-P&L source, not `fetchMyTrades` (the original plan §113 was wrong on this).
> - **`order['fee'].cost` is negative for paid fees on OKX** (`okx.py:2405`), so `net = pnl + fee` is the right sign convention *for OKX* and must be re-verified per venue.

---

## Section 2 — Root Cause

The dashboard was built to **render live venue state, not to keep books.** Every P&L number is re-derived on each render from a single ephemeral read — the live positions snapshot (`fetch_positions()`) — so any quantity not attached to a currently-open position simply does not exist: closed P&L, fees, and historical realized gains have no home. Because that read was wired to the demo/sandbox account by an unset default and never made mode-aware, the surface also shows the wrong *account*. Layered on top, P&L math was duplicated independently in Python and React with no shared contract, so the two drifted in both formula and label. The deeper flaw is the **absence of a durable, venue-neutral accounting layer** between the exchange adapters and the renderers: there is no append-only ledger of attributed, normalized closed trades that both surfaces read from. Every symptom in Section 1 is a consequence of computing money-facts from live position state instead of persisting them the moment they are observed.

---

## Section 3 — Design Principles

Invariants any fix must preserve. The first six were established in review; the last three were discovered during CCXT validation and mount analysis.

1. **Venue neutrality.** The ledger must not depend on OKX-specific `info` blobs. The adapter normalizes each venue's realized-P&L representation into a common field; the ledger consumes only unified/normalized fields. Raw `info.*` is a fallback, never the primary source. (Drives fixes for #11, #12, #13, #14, #16.)
2. **Mode separation.** Demo and live P&L are tracked separately. A strategy toggled demo→live starts with zero live P&L. The dashboard must read the account that matches the strategy's mode. (Drives #2, #3.)
3. **Append-only durability.** The ledger is WAL-style, deduped by `ordId`, never rewritten. It mirrors the repo idiom (`raw-webhooks.jsonl`, `signals.jsonl`): durable append, dedupe on a collision-safe key, replay-safe under `Restart=always`. (Drives #10, #8, #9.)
4. **Backward compatibility.** Existing cards keep working during migration. New fields are additive; no served field changes shape until a renderer is deliberately switched over.
5. **Fail-closed.** A missing field (e.g. `pnl=None` on an unsupported venue, `reduceOnly=None` on spot) must not crash or produce a wrong number. Log and degrade gracefully; never silently emit $0 as if it were real. (Directly targets the #11 silent-$0 trap and #16 spot drop.)
6. **Single source of truth.** The Python backend computes P&L; the UI renders. No client-side P&L arithmetic remains after Phase 4. (Drives #5.)
7. **Attribution before matching.** HermX ownership is established by `clOrdId` prefix (`mxc` / `operator_close_`) *before* any time/heuristic match, and the `ordId` is the durable dedupe key. Prefix matching is OKX-shaped and must be reversed/mapped on venues that rewrite client ids (Hyperliquid `cloid`). (Drives #15, #17.)
8. **Accounting state is not strategy config.** Budget and accounting window are accounting state, stored in the state store (`control-state.json` / ledger), not baked into `strategies/*.json`. (Drives #6, #18.)
9. **Persistence requires a writable mount.** Any new state file lives under `HERMX_DATA_DIR` on the `hermx-state:/app/data` rw mount, even under `read_only: true`. Verify the compose file before deploy. (Drives #19.)

---

## Section 4 — Execution Plan

Revised and re-numbered from the source plan to fold in demo/live separation (now Phase 0, a new prerequisite), venue-neutral adapter normalization, Hyperliquid `closedPnl`, Bybit `fetch_positions_history`, and spot close-detection. Each phase touches ≤3 source files (test files are additive, not counted). Phases 0–4 are the minimal viable path; each is independently committable.

**Foundations that span phases:**
- **Attribution key.** Signal closes: `clOrdId = "mxc" + sha256(...)` (`webhook_receiver.py:707-709`). Operator closes: `operator_close_<symbol>_<strategy_id>_<UTCday>` (`webhook_receiver.py:2571-2577`). Anything else = external/manual. `ordId` = durable dedupe key.
- **New state file.** `HERMX_DATA_DIR / "closed-trades.jsonl"` on the rw mount (Principle 9).
- **Venue-neutral read.** Reduce-only via `order.get("reduceOnly")` with `info.reduceOnly` fallback; realized P&L via an adapter-normalized `realized_pnl` field, never a raw path.

---

### PHASE 0 — Demo/live account separation (NEW prerequisite)

**Goal.** Make the dashboard read the account that matches each strategy's mode, and namespace all P&L state by mode so live and demo never mix. Without this, every later phase records numbers from the wrong account.

**Files changed.**
- `src/dashboard.py` — `_dashboard_executor()` (`697-716`) resolves `simulated_trading` from the strategy's effective mode (via `strategy_overrides` / `control-state.json`) instead of defaulting to `True`. Snapshot path consults mode so live strategies read the live account.
- `src/executors/ccxt_adapter.py` — ensure `simulated_trading` is honored end-to-end (sandbox flag set per resolved mode, not per adapter default).
- `src/webhook_receiver.py` *(control-state read helper only, if needed)* — expose the resolved mode to the dashboard path. **Requires explicit confirmation before touching (shared file).**

**Tests.** `test_dashboard_executor_live_reads_live_account`; `test_dashboard_executor_demo_reads_sandbox`; `test_mode_toggle_switches_account_source`; `test_default_mode_is_demo_when_unset`.

**Rollback safety.** Revert the resolution logic → adapter falls back to sandbox default (today's behavior). No stored data.

**Known limitations.** Ledger mode-namespacing (`closed-trades.jsonl` keyed/segregated by mode, or a `mode` column) is defined here but consumed starting Phase 1. Single venue account assumed per mode.

**Fixes:** #2, #3 (and unblocks correct data for all later phases).

---

### PHASE 1 — Durable closed-trade ledger + venue-neutral adapter reads (MVP)

**Goal.** Persist every confirmed HermX close to `closed-trades.jsonl` so closed P&L survives FLAT and the 100-row bound. Apply the venue-neutral adapter fixes the ledger depends on.

**Files changed.**
- `src/pnl_ledger.py` **(new).** `is_hermx_cl_ord_id()`; `read_closed_trades()` (reverse-tail, corrupt-line tolerant); `reconcile_from_order_history(history_rows, config)` (append normalized row per reduce-only, terminal, HermX-attributed row whose `ordId` is new; returns count); `append_closed_trades()` (atomic + fsync). Row schema v1 is snake_case with OKX-only raw fields under a `venue_meta` sub-dict; `mode` recorded per row.
- `src/executors/ccxt_adapter.py` — **(a)** reduce-only via `order.get("reduceOnly")` + `info.reduceOnly` fallback, handling the OKX `"false"` string (fixes #12, line 831); **(b)** populate a normalized `realized_pnl` per venue in `get_order_history_raw`: OKX `info.pnl`, **Hyperliquid `info.closedPnl`**, Binance `trade.info.realizedPnl`, Bybit via `fetch_positions_history` closed-pnl (fixes #11, #14); **(c)** surface position `realizedPnl` from `row.get("realizedPnl")` in `get_positions()`/`health()` (fixes #13, unblocks Phase 2).
- `src/dashboard.py` — in `exchange_order_history_snapshot()` (`774`), after the raw read succeeds, call `reconcile_from_order_history(...)` inside a `try/except` that never fails the snapshot.

**Deletion.** Remove dead `position_pnl()` (`dashboard.py:671-684`) — zero callers, misleading second implementation (fixes #4's dead-code half).

**Tests.** `test_reconcile_appends_new_hermx_closes`; `test_reconcile_is_idempotent`; `test_reconcile_excludes_external_orders`; `test_reconcile_excludes_open_and_non_reduceonly`; `test_read_closed_trades_tolerates_corrupt_line` (exercise production reader); `test_ledger_survives_position_flat`; `test_reduceonly_reads_unified_field`; `test_hyperliquid_closedpnl_normalized`; `test_spot_reduceonly_none_is_logged_not_dropped_silently`.

**Rollback safety.** Delete the module + call site; the adapter changes are additive normalizations (revert independently). Ledger file is additive.

**Known limitations.** No fee netting yet (raw `pnl`/`fee` stored). No accounting window. `strategy_id` for `mxc` closes unresolved (Phase 3). **Spot venues (#16):** reduce-only gate is documented derivatives-only; spot closes are **logged and skipped, never silently dropped** — a spot close signal is deferred (Open Decision). **Hyperliquid attribution (#15):** deferred to Phase 3 (cloid reverse-map). Relies on reconcile running before a close ages out of the 100-row window — mitigated by the Phase 3 cron safety net.

**Fixes:** #10, #1 (durability), #4 (dead code), #11, #12, #13, #14; documents #16.

---

### PHASE 2 — Fee-correct net P&L + venue semantics reconciliation

**Goal.** Determine per venue whether order `pnl` is gross or net, then expose `net_realized_pnl`.

**Files changed.**
- `src/pnl_ledger.py` — add `net_realized_pnl` (schema v2): `net = gross_realized_pnl + fee` (OKX fee signed negative). Add `schema_version`; `read_closed_trades` back-fills v1 rows on read. Encode per-venue outcome as `ORDER_PNL_IS_NET` (**per-venue**, since position `realizedPnl` is not CCXT-unified).
- `docs/PNL_MASTER_PLAN.md` — record the empirical finding.
- `tests/test_pnl_ledger.py` — extend.

**Empirical task (one-time, per-venue, not code).** On the demo account (OKX first — now readable via Phase-0 mode + Phase-1 `realizedPnl` surfacing), close a known position and compare `sum(gross + fee)` against position `realizedPnl` from `health()` just before FLAT. Document which is fee-inclusive; set the constant with a citing comment.

**Tests.** `test_net_equals_gross_plus_signed_fee` (parametrized over `ORDER_PNL_IS_NET`); `test_v1_rows_backfill_net_on_read`; `test_missing_fee_treated_as_zero_not_none`.

**Rollback safety.** Net is derived; drop the constant + computation and rows still read.

**Known limitations.** Funding fees not modeled. Assumes `feeCcy == USDT` (non-USDT logged, not FX-converted). The empirical check is per-venue and OKX/Bybit-only (only they expose position `realizedPnl`).

**Fixes:** #4 (fee math).

---

### PHASE 3 — Strategy P&L contract + accounting window + attribution

**Goal.** Produce a `strategy_pnl` object from the ledger, scoped by per-strategy `accounting_start_at`, correctly attributed (including Hyperliquid), and expose it in the API. First phase that changes served data.

**Files changed.**
- `src/webhook_receiver.py` **(explicit confirmation required — shared file).** Accounting-window storage in `control-state.json` under a new `accounting_windows[strategy_id]` key. Helpers `set_accounting_start()` / `accounting_start_for()` mirroring `set_strategy_override` (`1259`). No change to `strategy_overrides`.
- `src/pnl_ledger.py` — `aggregate_strategy_pnl(strategy_id, budget_usd, since_iso, cl_ord_id_to_strategy) -> dict` returning `{budget_usd, closed_realized_pnl_usd, closed_fees_usd, closed_net_pnl_usd, open_upl_usd, equity_now_usd, closed_order_count, accounting_start_at}`. Attribution resolves `mxc` via the cl_ord_id→strategy map (with **Hyperliquid cloid reverse-map / side-map**, fixing #15) and operator ids via embedded id.
- `src/dashboard.py` — build `strategy_pnl` in API assembly, attach per strategy; provide the cl_ord_id→strategy map from the strategy set; flag `strategy_ambiguous` where a symbol is traded by >1 strategy.

**Also.** Add a non-LLM Hermes cron safety net calling reconcile on a fixed cadence (captures closes even when the dashboard is idle). Per monitor-pivot memory; no `--provider/--model` pin needed (not an LLM job).

**Tests.** `test_aggregate_respects_accounting_start`; `test_equity_now_formula`; `test_closed_order_count_matches_rows`; `test_accounting_start_roundtrip` (writable-mount path); `test_strategy_pnl_absent_ledger` (zeros, not errors); `test_ambiguous_symbol_falls_back_to_inst_level`; `test_hyperliquid_close_attributes_to_strategy`.

**Rollback safety.** `strategy_pnl` is additive; `accounting_windows` absent → epoch-0 (whole ledger). React not yet reading it.

**Known limitations.** Multi-strategy-per-symbol resolution is heuristic (flagged, not solved). Window set via API only (no UI control until Phase 4). Portfolio/global equity in Phase 5.

**Fixes:** #6, #15, #17 (via clOrdId-first attribution); establishes the contract that closes #5.

---

### PHASE 4 — Fix UPnL inconsistency + render `strategy_pnl`

**Goal.** Python and React render identical, correctly-labeled numbers from `strategy_pnl`. Kill the `realized+upl`-mislabeled-as-UPnL bug and remove client-side math.

**Files changed.**
- `src/dashboard.py` `strategy_card` (`1560-1601`) — `budget ← budget_usd`; `"UPnL" ← open_upl_usd` (**fixes line 1598**); add `"Closed net" ← closed_net_pnl_usd`; `"Equity now" ← equity_now_usd`; color by each component's own sign.
- `dashboard-ui/components/StrategyCard.tsx` (`96,132`) — source `equityNow`/UPnL from `strategy_pnl`; UPnL stays `open_upl_usd`; add "Closed net"; **remove the `budget + realized + upl` recompute** so it cannot drift.
- `dashboard-ui/lib/types.ts` — add `StrategyPnl` interface mirroring the Phase-3 contract.

**Tests.** `test_strategy_card_upnl_is_open_only`; `test_strategy_card_equity_uses_contract`; React component test (UPnL binds `open_upl_usd`, equity binds `equity_now_usd`, no local recompute).

**Rollback safety.** Revert three files; API still emits `strategy_pnl` (harmless if unread). Pure presentation.

**Known limitations.** No accounting-window reset button yet. No portfolio card.

**Fixes:** #5, #1 (correct display), #4 (fee-correct display).

---

### PHASE 5 — Portfolio / account-level contract

**Goal.** A `portfolio` object with account/global equity independent of any strategy.

**Files changed.**
- `src/dashboard.py` — build `portfolio` from `exchange_live_snapshot()["account"]` (`763`) + ledger totals: `{account_equity_usd, total_budget_usd, total_closed_net_pnl_usd, total_open_upl_usd, hermx_equity_usd, unattributed_pnl_usd}`. Attach to `api_payload`.
- `dashboard-ui/lib/types.ts` — `Portfolio` interface.
- `dashboard-ui/app/page.tsx` — portfolio header card above the strategy grid.

**Tests.** `test_portfolio_totals_sum_strategies`; `test_portfolio_account_equity_passthrough`; React render test.

**Rollback safety.** Additive payload + one component.

**Known limitations.** `unattributed_pnl_usd` computed but not itemized until Phase 7. Multi-account not modeled.

**Fixes:** (foundation; itemization in #17 completes in Phase 7).

---

### PHASE 6 — Dynamic vs fixed budget modes

**Goal.** Give the dead `reinvest` flag meaning, or replace it with explicit `dynamic_budget`; move budget out of strategy config semantics.

**Files changed.**
- `src/pnl_ledger.py` (or small `budget.py`) — `effective_budget(strategy, strategy_pnl)`: fixed → `capital.budget_usd`; dynamic → `budget_usd + closed_net_pnl_usd` (compounds realized net; open UPnL excluded).
- `strategies/*.json` + loader — introduce `capital.dynamic_budget: bool` as canonical; treat `capital.reinvest` as a back-compat alias (`dynamic_budget or reinvest`). Update notes only, not live values.
- `dashboard-ui/lib/types.ts` — add `dynamic_budget?: boolean`; keep `reinvest`.

**Tests.** `test_fixed_budget_ignores_pnl`; `test_dynamic_budget_adds_closed_net_only`; `test_reinvest_alias_maps_to_dynamic`; `test_dynamic_budget_never_below_zero_guard` (clamp + log if closed net < -budget).

**Rollback safety.** Default fixed = today's behavior; revert wiring → `dynamic_budget` inert.

**Known limitations.** Affects *displayed* working capital/equity only — not executor order sizing (out of scope, riskier).

**Fixes:** #7; partially addresses #18 (budget semantics; full migration of `budget_usd` out of strategy JSON deferred — Open Decision).

---

### PHASE 7 — External / manual order detection

**Goal.** Surface and exclude venue activity HermX did not originate.

**Files changed.**
- `src/pnl_ledger.py` — `classify_orders()` (hermx vs external by `is_hermx_cl_ord_id`); `external_pnl()` summing external reduce-only `pnl+fee`. Harden `enrich_close_rows_with_order_history` (`dashboard.py:871`) to **prefer clOrdId match** before the inst_id+side+reduceOnly+time-delta heuristic (completes #17).
- `src/dashboard.py` — set `portfolio.unattributed_pnl_usd` from `external_pnl(...)`; add `external_activity` flag/count.
- `dashboard-ui/app/page.tsx` — "manual/external activity" indicator when nonzero.

**Tests.** `test_classify_splits_hermx_vs_external`; `test_external_pnl_excluded_from_strategy`; `test_enrich_prefers_clordid_over_time_match`.

**Rollback safety.** Read-only classification; revert restores old heuristic.

**Known limitations.** Cannot attribute external orders to intent. External-row detection is only as complete as the 100-row window (HermX rows are already durable via the ledger).

**Fixes:** #17 (completes attribution hardening).

---

### Phase → file → constraint check

| Phase | Source files | ≤3? | Fixes |
|-------|--------------|-----|-------|
| 0 | dashboard.py, ccxt_adapter.py, webhook_receiver.py* | ✅ (3) | #2, #3 |
| 1 | pnl_ledger.py (new), ccxt_adapter.py, dashboard.py | ✅ (3) | #10, #1, #4, #11, #12, #13, #14 |
| 2 | pnl_ledger.py (+doc) | ✅ | #4 |
| 3 | webhook_receiver.py*, pnl_ledger.py, dashboard.py | ✅ (3) | #6, #15, #17 |
| 4 | dashboard.py, StrategyCard.tsx, types.ts | ✅ (3) | #5, #1, #4 |
| 5 | dashboard.py, types.ts, page.tsx | ✅ (3) | (portfolio) |
| 6 | pnl_ledger.py, types.ts, strategies loader | ✅ (3) | #7, #18 |
| 7 | pnl_ledger.py, dashboard.py, page.tsx | ✅ (3) | #17 |

`*` `webhook_receiver.py` is a shared file — per dev-rules, get explicit confirmation before editing.

---

## Section 5 — Open Decisions

Pending operator input before the affected phase ships.

1. **`webhook_receiver.py` touch (Phases 0, 3).** Shared file; dev-rules require explicit confirmation. Phase 0 may only *read* resolved mode; Phase 3 *writes* accounting-window keys. Confirm both, or design Phase 3 to route accounting-window storage through a non-shared module.
2. **Phase 2 empirical P&L check.** Requires a real demo close on OKX to determine `ORDER_PNL_IS_NET`. Who runs it, on which account, and is the result trusted before any net number ships? Each new venue needs its own determination.
3. **Spot-venue support (#16).** Do we support spot venues (Coinbase) at all? If yes, a non-`reduceOnly` close signal is required (e.g. position-delta or trade-side inference). If no, document derivatives-only as a product constraint and keep the "log-and-skip spot closes" behavior. **Default recommendation: derivatives-only for now, log-and-skip.**
4. **Hyperliquid cloid reverse-map (#15).** Confirm the adapter can recover HermX ownership from the numeric `cloid` (deterministic reverse of the hash, or a side/time map carried at submit). If not reversible, Hyperliquid attribution needs a submit-time local map file.
5. **Budget migration (#18).** Fully move `budget_usd` out of `strategies/*.json` into accounting state, or leave it as the canonical seed and layer `effective_budget` on top? Full migration is a larger, riskier change touching the strategy loader.
6. **History-window backfill (#8).** Accept the "reconcile-before-age-out" mitigation (render + cron), or build a one-time `get_order_history_archive` / venue `fills-history` backfill? Currently documented but not automated.
7. **Ledger mode-namespacing shape.** Single `closed-trades.jsonl` with a `mode` column, or separate demo/live files? Affects Phase 0/1 schema.

---

## Section 6 — Risk Register

| Risk | Prob. | Impact | Mitigation |
|------|-------|--------|------------|
| **History-window race** — a close ages out of the 100-row window before reconcile records it → permanent loss (#8). | Medium | High | Reconcile on every history fetch (Phase 1) + Hermes cron safety net (Phase 3). One-time archive backfill available if a gap is suspected (Open Decision 6). |
| **Silent $0 on non-OKX venue** — bare `info.get("pnl")` returns None on Hyperliquid (#11). | High if any non-OKX venue enabled | High | Adapter normalization table (Phase 1) + fail-closed logging (Principle 5); `test_hyperliquid_closedpnl_normalized`. Never emit $0 as if real. |
| **Spot closes dropped** — reduce-only gate drops every spot close (#16). | High if spot venue enabled | High | Log-and-skip (never silent) in Phase 1; gate spot support behind Open Decision 3. |
| **Wrong-account data** — dashboard reads demo while strategy is live (#2). | Currently certain | Critical | Phase 0 makes reads mode-aware; ships before any ledger records numbers. Tests assert account source per mode. |
| **Fee-sign error** — wrong sign convention ships a wrong net number. | Medium | High | Gated behind the Phase 2 per-venue empirical reconciliation before any net number is displayed. |
| **Reconcile double-counts across restarts** under `Restart=always`. | Low | Medium | `ordId` dedupe (Principle 3) makes reconcile idempotent; `test_reconcile_is_idempotent`. |
| **Silent persistence failure** — missing rw mount, writes fail quietly (#19). | Medium | High | Principle 9; verify compose mounts `hermx-state:/app/data` rw pre-deploy; `test_accounting_start_roundtrip` exercises the mount path. |
| **Renderer drift returns** — a future edit re-adds client-side math (#5). | Low (after Phase 4) | Medium | Phase 4 removes all client-side P&L math; contract test asserts both surfaces bind the same fields. |
| **Manual order miscredited to a strategy** (#17). | Medium | Medium | clOrdId-first attribution (Phase 3) before any time heuristic; `test_enrich_prefers_clordid_over_time_match`. |
| **Multi-strategy-per-symbol misattribution** — ambiguous inst_id. | Medium | Medium | `strategy_ambiguous` flag surfaced (Phase 3); heuristic documented, not silently guessed. |
| **Shared-file regression** — `webhook_receiver.py` edit breaks intake. | Low | High | Additive keys/functions only; no change to `strategy_overrides` semantics; explicit confirmation gate (Open Decision 1). |

---

*End of master plan. This document supersedes the two source docs as the authoritative reference for the P&L/dashboard accounting work.*
