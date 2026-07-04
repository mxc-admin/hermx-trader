# P&L & Dashboard Accounting — Master Plan

> **Status:** in progress. **Phases 0–2 shipped; Phase 3 shipped (accounting windows + receiver reconciler #20a).** `HERMX_LIVE_TRADING` kill-switch gating and venue×mode-aware snapshot/reconcile resolution are live in `dashboard.py`/`ccxt_adapter.py`/`webhook_receiver.py`; the durable ledger (`pnl_ledger.py`), fee-correct net (Phase 2), per-strategy `accounting_start_at` clean windows, the `strategy_pnl`/`aggregate_strategy_pnl` contract, and the venue/mode-threaded order-state reconciler (#20a) are implemented. Phase 0 shipped the **mode** half of environment separation; the **venue** half (venue-aware executor/snapshot resolution) shipped as **Phase 0.5**. **Phase 4 (React parity) and Phases 5–7 remain plan-only.** Author: accounting-correctness consolidation, 2026-07-03.
> **Supersedes:** `PNL_ACCOUNTING_EXECUTION_PLAN.md` and `CCXT_VENUE_NEUTRALITY_VALIDATION.md` as the single source of truth. Those remain as source material; this document is authoritative where they disagree.
> **Scope:** durable closed P&L, fee-correct net, accounting window, demo/live separation, venue-neutral normalization, React↔Python contract, dynamic budget, external-order attribution.

This is the definitive reference for the P&L/dashboard accounting work. All findings are verified against code (`dashboard.py`, `ccxt_adapter.py`, `webhook_receiver.py`, `StrategyCard.tsx`) and the installed CCXT source (`4.5.61`), and against the environment-flag catalog (see **Flag Dependencies**, end of Section 4).

---

## Section 1 — Issue Registry

20 verified issues, ranked by severity. Each fix references these numbers in Section 4. (#20 added 2026-07-03: reads/reconcile feed hard-wired to OKX-demo — an **environment collapse** where every read targets a global `(okx, demo)` account instead of each strategy's own `(venue, mode)`. It is the ledger-feed residual of #2 that Phase 0 did **not** close, plus the wrong-venue dimension #2 never covered; its venue half is fixed by the new **Phase 0.5**.)

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
| **17** | **HIGH** | **No `cl_ord_id`-based P&L attribution.** `enrich_close_rows_with_okx_history` matches by `instId` + `side` + `reduceOnly` + time-delta. An external/manual order coincident in time can be bound to a HermX close. | Manual trades can be miscredited to a strategy, corrupting its P&L. | Don't trade manually on the same symbol. | `enrich_close_rows_with_okx_history`, `dashboard.py:913` |
| **6** | **MEDIUM** _(RESOLVED 2026-07-03, Phase 3)_ | **No accounting start / clean window.** ~~No `accounting_start_at` anywhere.~~ Now stored per-strategy in `control-state.json` (`accounting_windows`), filtered in `read_closed_trades`/`aggregate_strategy_pnl`, and set via `POST /api/control/strategy/{id}`. | Numbers include pre-HermX or pre-reset history with no way to zero a strategy. | None. | `control-state.json` `accounting_windows`; `pnl_ledger.read_closed_trades` |
| **9** | **MEDIUM** | **`pipeline.jsonl` bounded to 500 events.** `SIGNALS_MAX_N = 500`. Not a lifetime ledger. | Cannot be used as the durable P&L source; silently truncates. | None. | `SIGNALS_MAX_N` |
| **12** | **MEDIUM** | **`info.get("reduceOnly")` is OKX-only.** CCXT unifies `reduceOnly` at top-level (`safe_order:4422`), but `ccxt_adapter.py:831` still reads `info.get("reduceOnly")`. Works on OKX only because OKX's raw key coincidentally matches (and is a string `'true'`/`'false'`). | Reduce-only detection is fragile/venue-coincidental; downstream truthiness must handle the `"false"` string. | OKX-only deployment. | `ccxt_adapter.py:831`; CCXT base `4422` |
| **13** | **MEDIUM** | **Position `realizedPnl` unified only on OKX/Bybit; adapter surfaces only the *raw* OKX value.** Only OKX (`okx.py:5969`) and Bybit (`bybit.py:6493`) set `realizedPnl` in `parse_position`; Binance/Hyperliquid don't. `health()` **does** now surface it, but from the raw OKX blob — `realizedPnl: info.get("realizedPnl")` (`ccxt_adapter.py:925`) — which is OKX-only and not the CCXT-unified `row.get("realizedPnl")`. `get_positions()` still maps only `unrealizedPnl → upl` (`ccxt_adapter.py:872`). | The Phase-2 empirical fee check is executable today on OKX (raw `realizedPnl` is already surfaced) but venue-fragile; a portable check needs the unified read. Phase 1(c) is an **upgrade** (raw→unified + add to `get_positions()`), not net-new. | None. | `ccxt_adapter.py:872` (`get_positions`), `:925` (`health`) |
| **14** | **MEDIUM** | **`fetch_positions_history` only OKX + Bybit.** Binance/Hyperliquid/Coinbase do not implement it. Not a portable primitive. | Cannot build the ledger's realized-P&L path on it. | N/A (informs design). | CCXT `okx.py`, `bybit.py:8486` |
| **19** | **MEDIUM** | **Writable-mount + env-driven path requirement.** The ledger and `control-state.json` both resolve under `HERMX_DATA_DIR` (defaults to `HERMX_ROOT`, which itself falls back to the repo root). In production `HERMX_DATA_DIR=/app/data` on the `hermx-state:/app/data` rw mount, which must be writable even with `read_only: true` on root fs; otherwise writes fail silently. New `closed-trades.jsonl` inherits both the path resolution and the mount requirement. | Accounting-window resets and the ledger silently fail to persist if the mount is missing or `HERMX_DATA_DIR` points off the rw mount. | Ensure compose mounts `hermx-state`; leave `HERMX_DATA_DIR` at its `/app/data` default. | code-quality memory; `dashboard.py:56` (`HERMX_DATA_DIR`), `webhook_receiver.py:133,139`, `dashboard.py:2497-2518` |
| **20** | **HIGH** _(20a RESOLVED 2026-07-03, Phase 3)_ | **Reconcile/read feed is hard-wired to OKX-demo (wrong-account + wrong-venue).** The root cause is an **environment collapse**: every read/reconcile targets a single global `(okx, demo)` account instead of each strategy's own `(venue, mode)`. Three inherit points: **(a)** the receiver's order-state reconciler — `_effective_execution_config()` (`webhook_receiver.py:2128-2138`) seeds `ccxt_exchange = EXECUTION_DEFAULTS["ccxt_exchange"] = "okx"` unconditionally and never sets `simulated_trading`, so the adapter defaults `True` → OKX demo sandbox for **every** order regardless of the strategy's live mode or actual venue (even though the submit path already resolves per-order venue+mode in `build_strategy_execution_readiness`, `:1356-1390`). **(b)** the dashboard executor — `_dashboard_executor()` pins `ccxt_exchange="okx"` unconditionally (`dashboard.py:695-696`), so even the mode-aware positions read is single-venue. **(c)** the P&L-ledger feed — `okx_order_history_snapshot()` calls `_dashboard_executor(config)` (defaults demo) then `reconcile_from_order_history(rows, "okx", "demo")` with **literal** venue/mode (`dashboard.py:819`). Phase 0 made `okx_live_snapshot` (positions) *mode*-aware but neither *venue*-aware nor the order-history feed mode-aware. `pnl_ledger.reconcile_from_order_history` is venue/mode-parametric and its dedupe key already carries `(exchange,…,mode)` — the ledger is correct; the defect is entirely the upstream feeds. **Fix locus:** Phase 0.5 (venue-aware `(venue,mode)` snapshots + the `_dashboard_executor` OKX-pin), Phase 1 (the `dashboard.py:819` literal → the snapshot's actual `(venue,mode)`), Phase 3 (receiver reconciler `_effective_execution_config` per-order venue/mode). | Live OKX closes and all non-OKX (Bybit/Binance/HL) closes are **never** reconciled into the ledger; a Bybit order checked by the receiver reconciler is queried on OKX → not-found → stuck UNKNOWN forever. If the snapshot is later made mode-aware without updating the literals, live rows get stamped `mode="demo"` → mis-attribution. | Demo-only, OKX-only deployment (current posture). | `webhook_receiver.py:2128-2138`, `dashboard.py:695-696,819`; adapter default `ccxt_adapter.py:222,282` |
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
2. **Environment separation (venue × mode).** Each strategy has exactly one *environment* = its venue (from the strategy instrument) × its mode (`demo`|`live`). Every read (positions, order history) and every reconcile for a strategy targets **that** environment's account; results are namespaced by `(venue, mode)` and never mixed. A strategy toggled demo→live starts with zero live P&L; a KuCoin strategy never reads OKX. The isolation key is `(venue, mode)`, **not** `strategy_id` — strategies sharing an environment share one account/read. The ledger dedupe key already carries both dimensions: `(exchange, inst_id, ord_id, mode)`. Phase 0 shipped the *mode* half (mode-aware account reads); Phase 0.5 adds the *venue* half (venue-aware executor/snapshot resolution). (Drives #2, #3, #20.)
3. **Append-only durability.** The ledger is WAL-style, deduped by `ordId`, never rewritten. It mirrors the repo idiom (`raw-webhooks.jsonl`, `signals.jsonl`): durable append, dedupe on a collision-safe key, replay-safe under `Restart=always`. (Drives #10, #8, #9.)
4. **Backward compatibility.** Existing cards keep working during migration. New fields are additive; no served field changes shape until a renderer is deliberately switched over.
5. **Fail-closed.** A missing field (e.g. `pnl=None` on an unsupported venue, `reduceOnly=None` on spot) must not crash or produce a wrong number. Log and degrade gracefully; never silently emit $0 as if it were real. (Directly targets the #11 silent-$0 trap and #16 spot drop.)
6. **Single source of truth.** The Python backend computes P&L; the UI renders. No client-side P&L arithmetic remains after Phase 4. (Drives #5.)
7. **Attribution before matching.** HermX ownership is established by `clOrdId` prefix (`mxc` / `operator_close_`) *before* any time/heuristic match, and the `ordId` is the durable dedupe key. Prefix matching is OKX-shaped and must be reversed/mapped on venues that rewrite client ids (Hyperliquid `cloid`). (Drives #15, #17.)
8. **Accounting state is not strategy config.** Budget and accounting window are accounting state, stored in the state store (`control-state.json` / ledger), not baked into `strategies/*.json`. (Drives #6, #18.)
9. **Persistence requires a writable mount.** Any new state file lives under `HERMX_DATA_DIR` on the `hermx-state:/app/data` rw mount, even under `read_only: true`. Verify the compose file before deploy. (Drives #19.)
10. **Ledger path is env-driven; rotation constants are module-level, not env-facing — and the P&L ledger never prunes.** `closed-trades.jsonl` resolves from `HERMX_DATA_DIR` (→ `HERMX_ROOT` → repo root), never a hard-coded path — identical resolution to `control-state.json` and the receiver `DATA_DIR`, so all processes agree. Size-rotation constants (`HERMX_LEDGER_ROTATE_MAX_BYTES` = 64 MB, `HERMX_LEDGER_ROTATE_RETENTION` = 5) are module-level in `webhook_receiver.py` and govern the **WAL** ledgers (`raw-webhooks.jsonl`), which are prune-safe because they are only replayed for *recent* recovery. The P&L ledger is a **lifetime** book (the whole point of #10): it must **not** reuse `_rotate_ledger_if_large`'s pruning path — sealing is acceptable, but no segment holding a recorded close may ever be deleted. (Drives #10, #19; guards the WAL/ledger rotation distinction.)

---

## Section 4 — Execution Plan

Revised and re-numbered from the source plan to fold in demo/live separation (Phase 0), **venue-aware read/reconcile resolution (Phase 0.5 — the venue half of environment separation)**, venue-neutral adapter normalization, Hyperliquid `closedPnl`, Bybit `fetch_positions_history`, and spot close-detection. Each phase touches ≤3 source files (test files are additive, not counted). Phases 0–4 are the minimal viable path; each is independently committable.

**Foundations that span phases:**
- **Attribution key.** Signal closes: `clOrdId = "mxc" + sha256(...)` (`webhook_receiver.py:707-709`). Operator closes: `operator_close_<symbol>_<strategy_id>_<UTCday>` (`webhook_receiver.py:2571-2577`). Anything else = external/manual. `ordId` = durable dedupe key.
- **New state file.** `HERMX_DATA_DIR / "closed-trades.jsonl"` — same env resolution as `control-state.json` (`HERMX_DATA_DIR` → `HERMX_ROOT` → repo root), on the rw mount (Principles 9, 10). Lifetime ledger: never pruned by the WAL rotation path.
- **Mandatory `mode` column.** Every ledger row carries `mode` (`demo` | `live`), decided at write time from the strategy's effective mode (Phase 0). This is **required**, not optional (resolves Open Decision 7 in favor of a single file + column). Demo and live rows never mix; aggregation filters on `mode` (Principle 2).
- **Venue-neutral read.** Reduce-only via `order.get("reduceOnly")` with `info.reduceOnly` fallback; realized P&L via an adapter-normalized `realized_pnl` field, never a raw path.

---

### PHASE 0 — Demo/live account separation (SHIPPED)

**Goal.** Make the dashboard read the account that matches each strategy's mode, and namespace all P&L state by mode so live and demo never mix. Without this, every later phase records numbers from the wrong account.

**Status.** Shipped. The mode-aware snapshot resolution and the `HERMX_LIVE_TRADING` gate are live: `okx_live_snapshot()` fail-closes to demo unless the global `HERMX_LIVE_TRADING` kill switch is armed (`dashboard.py:729-734`), and the live snapshot is requested only when at least one strategy is effectively live (`dashboard.py:1241-1251`). `ccxt_adapter.py` refuses live submission without `HERMX_LIVE_TRADING=true` (`:264-271`). Paths resolve via `HERMX_ROOT` (→ repo-root fallback) and `HERMX_DATA_DIR`.

**Shipped implementation (verified against code):**
- `_dashboard_executor(config, simulated_trading=True)` (`dashboard.py:697`) accepts a mode param instead of defaulting the adapter to sandbox; the sandbox flag is resolved from the strategy's effective mode.
- `okx_live_snapshot()` (`dashboard.py:725`) keys its cache per-mode — `snapshot:demo` / `snapshot:live` via `f"snapshot:{mode_key}"` (`:741`, where `mode_key = "demo" if simulated else "live"`, `:727`) — so demo and live reads never share a cache slot.
- `strategy_card` (`dashboard.py:1609`) picks the snapshot matching each strategy's `effective_mode` via `_snapshot_for_mode(okx_live_by_mode, mode)` (`:805`, called at `:1620`).
- `HERMX_LIVE_TRADING` fail-closed gate: a live snapshot requested while the kill switch is disarmed degrades to the demo read with a logged warning (`dashboard.py:732-738`) — never a connect error, never a silent wrong-account read.

**Files changed.**
- `src/dashboard.py` — `_dashboard_executor()` (`697-716`) resolves `simulated_trading` from the strategy's effective mode (via `strategy_overrides` / `control-state.json`) instead of defaulting to `True`. Snapshot path consults mode so live strategies read the live account; the live read is additionally gated by the global `HERMX_LIVE_TRADING` kill switch (fail-closed to demo when disarmed).
- `src/executors/ccxt_adapter.py` — `simulated_trading` honored end-to-end (sandbox flag per resolved mode); live submission blocked unless `HERMX_LIVE_TRADING=true`.
- `src/webhook_receiver.py` *(control-state read helper only, if needed)* — expose the resolved mode to the dashboard path. **Requires explicit confirmation before touching (shared file).**

**Tests.** `test_dashboard_executor_live_reads_live_account`; `test_dashboard_executor_demo_reads_sandbox`; `test_mode_toggle_switches_account_source`; `test_default_mode_is_demo_when_unset`.

**Rollback safety.** Revert the resolution logic → adapter falls back to sandbox default (today's behavior). No stored data.

**Known limitations.** Ledger mode-namespacing is fixed as a **mandatory `mode` column** on a single `closed-trades.jsonl` (Foundations; Open Decision 7 resolved), decided at write time from the Phase-0 effective mode and consumed starting Phase 1. **Venue-deferred:** Phase 0 resolves *mode* only; the executor is still pinned to OKX (`_dashboard_executor` `:695-696`), so reads for a non-OKX strategy would hit the wrong venue. Correct for the current single-venue posture; the venue half is **Phase 0.5** (below). This is the disclosed gap, not a regression.

**Fixes:** #2, #3 (and unblocks correct data for all later phases).

---

### PHASE 0.5 — Venue-aware executor & snapshot resolution (prerequisite; the venue half of Phase 0)

**Goal.** Generalize Phase 0's mode-aware reads to be *venue*-aware, so a strategy on any venue (KuCoin, Bybit, Binance, …) reads **its** venue's account, not a hard-pinned OKX. Phase 0 keyed reads by `mode`; this keys them by the full environment `(venue, mode)` (Principle 2).

**Why a separate phase, not a Phase 0 reopen.** Phase 0 correctly shipped mode separation for the current single-venue posture and *honestly deferred* venue in its own Known Limitations. This phase closes that gap and nothing else. It is a pure read/resolution change — **no ledger/schema change** (the dedupe key `(exchange, inst_id, ord_id, mode)` already carries venue) and **no new state**.

**Executor granularity — global per-`(venue, mode)`, not per-strategy.** An executor is one authenticated CCXT client to one account; the account is identified by `(venue, mode)`, not by strategy. Two strategies both on OKX-live share one account and one positions/order-history read. So: enumerate the **distinct** `(venue, mode)` pairs across the strategy set, build one executor + one snapshot per pair, and have each strategy card pick the snapshot for **its** pair. This is `_snapshot_for_mode` generalized from a 1-D mode key to a 2-D `(venue, mode)` key. Per-strategy executors are **rejected**: they open N duplicate clients to the same account and multiply rate-limit usage.

**Files changed.**
- `src/dashboard.py` —
  - `_dashboard_executor(config, simulated_trading=True)`: stop unconditionally pinning `ccxt_exchange="okx"` (`695-696`). Resolve the venue from the (per-strategy) config's instrument; keep `okx` only as the *global default* when no venue is resolvable (matching `HERMX_CCXT_EXCHANGE`).
  - `okx_live_snapshot` → per-environment read: cache key `f"snapshot:{venue}:{mode}"` (extends the current `snapshot:{mode}`, `:725`). Build the executor for `(venue, mode)`.
  - `okx_order_history_snapshot` → same generalization (it is **misnamed** — it is the ledger feed for whatever venue/mode is queried, not an OKX-specific read).
  - `_snapshot_for_mode` → `_snapshot_for_env(by_env, venue, mode)`: pick by `(venue, mode)`, fall back to demo-of-same-venue, then empty (never raise).
  - `dashboard_model()`: enumerate distinct `(venue, mode)` pairs from the strategy set and build one snapshot per pair (replacing the fixed `okx_live_demo` / `okx_live_live` pair at `1243-1244`); each `strategy_card` selects its own pair.

**Rename note.** `okx_live_snapshot` / `okx_order_history_snapshot` are OKX-shaped names for now-venue-generic functions → `venue_live_snapshot` / `venue_order_history_snapshot` (keep old names as thin aliases for one release to avoid a wide rename in one commit).

**Tests.** `test_snapshot_keyed_by_venue_and_mode`; `test_kucoin_strategy_reads_kucoin_not_okx`; `test_two_strategies_same_venue_mode_share_one_executor`; `test_unresolvable_venue_falls_back_to_default`; `test_okx_only_deployment_unchanged` (byte-for-byte regression guard for the current posture).

**Rollback safety.** Revert the resolution/keying change → single global OKX executor (today's behavior). No stored data; the cache is in-memory.

**Known limitations.** Assumes one account per `(venue, mode)` — multi-subaccount per venue is out of scope. Venue resolved from the strategy instrument; a strategy with no instrument falls back to `HERMX_CCXT_EXCHANGE` (`okx`). The live read stays gated by `HERMX_LIVE_TRADING` (Phase 0), orthogonal to venue.

**Fixes:** #20 (venue half — the OKX-pin in `_dashboard_executor`); completes #2/#3 for multi-venue. Unblocks a correct per-`(venue, mode)` reconcile feed in Phase 1.

---

### PHASE 1 — Durable closed-trade ledger + venue-neutral adapter reads (MVP)

**Goal.** Persist every confirmed HermX close to `closed-trades.jsonl` so closed P&L survives FLAT and the 100-row bound. Apply the venue-neutral adapter fixes the ledger depends on.

**Files changed.**
- `src/pnl_ledger.py` **(new).** Resolves its file as `Path(os.environ.get("HERMX_DATA_DIR", ROOT)) / "closed-trades.jsonl"` — the same expression `dashboard.py:56` uses for `control-state.json`, so receiver and dashboard agree. `is_hermx_cl_ord_id()`; `read_closed_trades()` (reverse-tail, corrupt-line tolerant); `reconcile_from_order_history(history_rows, config)` (append normalized row per reduce-only, terminal, HermX-attributed row whose `ordId` is new; returns count) — a **distinct** function from the receiver's `reconcile_post_submit_enabled()`/`HERMX_RECONCILE_ENABLED` submit-path reconcile, which it must not be confused with or gated by; `append_closed_trades()` (atomic + fsync, **append-only, no size-pruning** per Principle 10). Row schema v1 is snake_case with OKX-only raw fields under a `venue_meta` sub-dict; the mandatory `mode` column is recorded per row.
- `src/executors/ccxt_adapter.py` — **(a)** reduce-only via `order.get("reduceOnly")` + `info.reduceOnly` fallback, handling the OKX `"false"` string (fixes #12, line 831); **(b)** populate a normalized `realized_pnl` per venue in `get_order_history_raw`: OKX `info.pnl`, **Hyperliquid `info.closedPnl`**, Binance `trade.info.realizedPnl`, Bybit via `fetch_positions_history` closed-pnl (fixes #11, #14); **(c)** upgrade position-`realizedPnl` surfacing from raw→unified: `health()` already emits the raw OKX value (`info.get("realizedPnl")`, `ccxt_adapter.py:925`), so this switches it to the CCXT-unified `row.get("realizedPnl")` and adds the same field to `get_positions()` (which today surfaces only `upl`) — an upgrade, not net-new (fixes #13, unblocks Phase 2).
- `src/dashboard.py` — in the (Phase-0.5) venue/mode-aware order-history snapshot, after the raw read succeeds, call `reconcile_from_order_history(rows, venue, mode)` with the snapshot's **actual** `(venue, mode)` — **never** the literal `("okx", "demo")` currently at `dashboard.py:819` — inside a `try/except` that never fails the (read-only) snapshot. The ledger dedupe key `(exchange, inst_id, ord_id, mode)` already isolates venues and modes, so the only change is the call site. **Depends on Phase 0.5**: without venue-aware snapshots the feed still reads one venue, and stamping live rows with a literal `"demo"` would mis-attribute them.

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

**Status (2026-07-03) — IMPLEMENTED, gross still displayed.**
- `src/pnl_ledger.py` ships `net_realized_pnl` (schema v2), `SCHEMA_VERSION = 2`, `_compute_net_realized(gross, fee, exchange_id)`, and `net_realized_for_strategy(strategy_id, mode=None)`. `read_closed_trades` back-fills net for v1 rows on read (no file mutation).
- `ORDER_PNL_IS_NET` per-venue config exists; **all venues default to `False` (gross)** pending empirical verification — including OKX.
- `ccxt_adapter.get_order_history_raw` already populates `fee` from CCXT-unified `order['fee'].cost` (signed negative for paid fees), so no adapter change was needed.
- Display is unchanged (Decision ②: gross first, verify net later). `dashboard.py` carries a `TODO(Phase 2+)` at the strategy-card P&L computation to switch to `net_realized_for_strategy` once verified.
- **Next (empirical, not code):** run the per-venue close test above. If OKX's `pnl` turns out already fee-inclusive, set `ORDER_PNL_IS_NET["okx"] = True`; otherwise the default `False` is correct.
- Tests: `tests/test_pnl_net.py` (net math, fee signs, per-venue params, v1 back-fill, strategy sums/mode filter).

**Fixes:** #4 (fee math).

---

### PHASE 3 — Strategy P&L contract + accounting window + attribution

**Goal.** Produce a `strategy_pnl` object from the ledger, scoped by per-strategy `accounting_start_at`, correctly attributed (including Hyperliquid), and expose it in the API. First phase that changes served data.

**Files changed.**
- `src/webhook_receiver.py` **(explicit confirmation required — shared file).** Two additive changes in the one file:
  - **Accounting window.** Storage in `control-state.json` under a new `accounting_windows[strategy_id]` key. Helpers `set_accounting_start()` / `accounting_start_for()` mirroring `set_strategy_override` (`1259`). No change to `strategy_overrides`.
  - **Order-state reconciler venue/mode threading (#20a).** `_effective_execution_config()` (`2128-2138`) currently seeds one global `ccxt_exchange = EXECUTION_DEFAULTS["ccxt_exchange"]` and no `simulated_trading`, so every order reconciles against OKX-demo. The submit path **already** resolves per-order venue+mode in `build_strategy_execution_readiness` (`execution_mode`/`simulated_trading` `:1356-1369`, instrument/venue `:1388-1390`); the reconciler must resolve the **same** `(venue, mode)` from the order's journal row / strategy — so a Bybit-live order is checked on Bybit-live, not OKX-demo — reusing the exact environment key the ledger and dashboard use. Still 3 files total (all changes in-file).
- `src/pnl_ledger.py` — `aggregate_strategy_pnl(strategy_id, budget_usd, since_iso, cl_ord_id_to_strategy) -> dict` returning `{budget_usd, closed_realized_pnl_usd, closed_fees_usd, closed_net_pnl_usd, open_upl_usd, equity_now_usd, closed_order_count, accounting_start_at}`. Attribution resolves `mxc` via the cl_ord_id→strategy map (with **Hyperliquid cloid reverse-map / side-map**, fixing #15) and operator ids via embedded id.
- `src/dashboard.py` — build `strategy_pnl` in API assembly, attach per strategy; provide the cl_ord_id→strategy map from the strategy set; flag `strategy_ambiguous` where a symbol is traded by >1 strategy.

**Also.** Add a non-LLM Hermes cron safety net calling the **ledger** `reconcile_from_order_history` on a fixed cadence (captures closes even when the dashboard is idle). It iterates the **distinct `(venue, mode)` pairs** in the strategy set (Phase 0.5) — one reconcile per pair — so it covers live and non-OKX environments, not just OKX-demo. This is the P&L-ledger reconcile — **not** the receiver's `HERMX_RECONCILE_ENABLED` submit-path reconcile — and is unaffected by that flag. Per monitor-pivot memory; no `--provider/--model` pin needed (not an LLM job).

**Tests.** `test_aggregate_respects_accounting_start`; `test_equity_now_formula`; `test_closed_order_count_matches_rows`; `test_accounting_start_roundtrip` (writable-mount path); `test_strategy_pnl_absent_ledger` (zeros, not errors); `test_ambiguous_symbol_falls_back_to_inst_level`; `test_hyperliquid_close_attributes_to_strategy`.

**Rollback safety.** `strategy_pnl` is additive; `accounting_windows` absent → epoch-0 (whole ledger). React not yet reading it.

**Known limitations.** Multi-strategy-per-symbol resolution is heuristic (flagged, not solved). Window set via API only (no UI control until Phase 4). Portfolio/global equity in Phase 5.

**Status (2026-07-03) — IMPLEMENTED (accounting windows + receiver reconciler #20a).**
- **Accounting windows.** `control-state.json` gained an additive `accounting_windows[strategy_id] = {accounting_start_at: ms, set_at}` key (in `default_control_state()`, re-attached in `load_control_state()` so the "keep only default keys" merge can't drop it). Receiver helpers `set_accounting_start()` / `clear_accounting_start()` / `accounting_start_for()` mirror `set_strategy_override`; `None` clears. Dashboard mirrors them (`_set_accounting_start` / `_clear_accounting_start` / `_accounting_start_for`).
- **Ledger filter + contract.** `pnl_ledger.read_closed_trades(..., accounting_start_at)` drops rows older than the later of `since_ms`/`accounting_start_at`. `net_realized_for_strategy(..., accounting_start_at)` and new `aggregate_strategy_pnl(strategy_id, *, budget_usd, mode, accounting_start_at, open_upl_usd) -> {budget_usd, closed_realized_pnl_usd, closed_fees_usd, closed_net_pnl_usd, open_upl_usd, equity_now_usd, closed_order_count, accounting_start_at}` are window-aware. Absent ledger → zeros, never an error.
- **API.** `POST /api/control/strategy/{id}` now accepts an optional `accounting_start_at` (int ms sets; JSON `null` clears; bool/non-int → 400) alongside/independent of `mode`; `DELETE` still clears only the mode override, leaving the window. `/api` exposes per-strategy `accounting_start_at` + a `strategy_pnl` object and a top-level `accounting_windows` map (all additive; React adopts in Phase 4).
- **#20a receiver reconciler.** The order-journal intent (`_order_intent_from_readiness`) now persists the resolved `venue` / `mode` / `simulated_trading`. `_effective_execution_config(order_intent)` and `_reconciliation_executor(order_intent)` build the query executor from that `(venue, mode)`; `reconcile_startup` / `resolve_unknown_orders_once` resolve a **per-order** executor (cached by env) so a Bybit-live order is checked on Bybit-live — not OKX-demo. Explicitly-passed executors are unchanged (backward compatible); orders journalled before the field fall back to the OKX-demo default. The post-submit reconcile (`HERMX_RECONCILE_ENABLED`, default OFF) is deliberately left OKX-demo-default here to keep the phase to its files and not break its zero-arg test stubs — a documented follow-up, not a #20a regression.
- **Cron safety net.** `deploy/hermes-scripts/hermx-ledger-reconcile.py` (non-LLM, `--no-agent`, every 10m) GETs the dashboard `/api`, which drives `dashboard_model()`'s per-`(venue,mode)` `reconcile_from_order_history` feed — reusing the shipped feed rather than duplicating executor/credential handling. Registered in `install-cron-monitors.sh`. Not gated by `HERMX_RECONCILE_ENABLED`.
- **Deferred to a later pass:** Hyperliquid cloid attribution (#15) and the `strategy_ambiguous` multi-strategy-per-symbol flag — the aggregation ships without them; attribution stays best-effort as in Phase 1/2.
- Tests: `tests/test_receiver_reconcile_venue.py` (new); `tests/test_pnl_ledger.py` (window + `aggregate_strategy_pnl`); `tests/test_phase3_strategy_overrides.py` (receiver storage + dashboard endpoint/api_payload).

**Fixes:** #6, #20a (receiver reconciler venue/mode); establishes the contract that closes #5. (#15/#17 attribution hardening deferred within this phase.)

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
- `src/dashboard.py` — build `portfolio` from `okx_live_snapshot()["account"]` + ledger totals: `{account_equity_usd, total_budget_usd, total_closed_net_pnl_usd, total_open_upl_usd, hermx_equity_usd, unattributed_pnl_usd}`. Attach to `api_payload`.
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
- `src/pnl_ledger.py` — `classify_orders()` (hermx vs external by `is_hermx_cl_ord_id`); `external_pnl()` summing external reduce-only `pnl+fee`. Harden `enrich_close_rows_with_okx_history` (`dashboard.py:913`) to **prefer clOrdId match** before the inst_id+side+reduceOnly+time-delta heuristic (completes #17).
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
| 0.5 | dashboard.py | ✅ (1) | #20 (venue half) |
| 1 | pnl_ledger.py (new), ccxt_adapter.py, dashboard.py | ✅ (3) | #10, #1, #4, #11, #12, #13, #14, #20 (feed literal) |
| 2 | pnl_ledger.py (+doc) | ✅ | #4 |
| 3 | webhook_receiver.py*, pnl_ledger.py, dashboard.py | ✅ (3) | #6, #15, #17, #20a |
| 4 | dashboard.py, StrategyCard.tsx, types.ts | ✅ (3) | #5, #1, #4 |
| 5 | dashboard.py, types.ts, page.tsx | ✅ (3) | (portfolio) |
| 6 | pnl_ledger.py, types.ts, strategies loader | ✅ (3) | #7, #18 |
| 7 | pnl_ledger.py, dashboard.py, page.tsx | ✅ (3) | #17 |

`*` `webhook_receiver.py` is a shared file — per dev-rules, get explicit confirmation before editing.

---

### Flag Dependencies

Reconciled against the full environment-flag catalog. **The P&L work introduces no new env flags.** It depends only on flags/constants that already exist:

| Mechanism | Kind | Default | Role in the P&L plan |
|-----------|------|---------|----------------------|
| `HERMX_DATA_DIR` | env-facing | `HERMX_ROOT` | Directory for `closed-trades.jsonl` and `control-state.json`. Same resolution across receiver + dashboard (`dashboard.py:56`, `webhook_receiver.py:139`). |
| `HERMX_ROOT` | env-facing | repo root | Base path; `HERMX_DATA_DIR` falls back to it. Sole root variable — the legacy `SHADOW_ROOT` fallback was removed (no `or SHADOW_ROOT` anywhere); tests bind `HERMX_ROOT` directly. |
| `HERMX_LIVE_TRADING` | core, fail-closed | disarmed | Global kill switch. Gates the Phase-0 live-account read (`dashboard.py:729-734`) and live submission (`ccxt_adapter.py:264-271`). No live P&L is read when disarmed. |
| `HERMX_CCXT_EXCHANGE` | core | `okx` | Default venue when a strategy instrument is empty (`ccxt_adapter.py:176`). Determines which venue's raw `realized_pnl` normalization applies. |
| `HERMX_LEDGER_ROTATE_MAX_BYTES` / `_RETENTION` | module-level constant | 64 MB / 5 | Governs **WAL** ledger rotation, **not** the P&L ledger. Called out so the P&L ledger is deliberately excluded from pruning (Principle 10). Not env-facing. |
| `SIGNALS_MAX_N`, `REPLAY_LOOKBACK_SECONDS` | module-level constant | 500, 300 s | Bound `pipeline.jsonl` (#9) and replay freshness. Confirm the P&L ledger is independent of both — it is a separate, unbounded file. |

**Flags checked and confirmed *not* to affect the plan:**
- `HERMX_RECONCILE_ENABLED` (default OFF) — gates the receiver's **post-submit** reconcile (`reconcile_post_submit_enabled`, `webhook_receiver.py:1899`). This is a different mechanism from the plan's `reconcile_from_order_history` ledger reconcile; the ledger path and the Phase-3 cron are neither gated by nor confused with it. Naming-collision hazard only — documented in Phase 1 and Phase 3.
- `HERMX_ADVISOR_ENABLED` (default from advisor cfg) — gates the Hermes LLM advisor (`webhook_receiver.py:305`, `dashboard.py:49`). No P&L accounting impact.

**Net effect on breakers:** none introduced. The only newly-surfaced hazard is a *design constraint*, not a code breaker — the P&L ledger must not inherit the WAL's size-pruning (Principle 10). Tests bind `HERMX_ROOT` directly (the `SHADOW_ROOT` fallback was removed), so test isolation is unchanged.

---

## Section 5 — Open Decisions

Pending operator input before the affected phase ships.

1. **`webhook_receiver.py` touch (Phases 0, 3).** Shared file; dev-rules require explicit confirmation. Phase 0 may only *read* resolved mode; Phase 3 *writes* accounting-window keys. Confirm both, or design Phase 3 to route accounting-window storage through a non-shared module.
2. **Phase 2 empirical P&L check.** Requires a real demo close on OKX to determine `ORDER_PNL_IS_NET`. Who runs it, on which account, and is the result trusted before any net number ships? Each new venue needs its own determination.
3. **Spot-venue support (#16).** Do we support spot venues (Coinbase) at all? If yes, a non-`reduceOnly` close signal is required (e.g. position-delta or trade-side inference). If no, document derivatives-only as a product constraint and keep the "log-and-skip spot closes" behavior. **Default recommendation: derivatives-only for now, log-and-skip.**
4. **Hyperliquid cloid reverse-map (#15).** Confirm the adapter can recover HermX ownership from the numeric `cloid` (deterministic reverse of the hash, or a side/time map carried at submit). If not reversible, Hyperliquid attribution needs a submit-time local map file.
5. **Budget migration (#18).** Fully move `budget_usd` out of `strategies/*.json` into accounting state, or leave it as the canonical seed and layer `effective_budget` on top? Full migration is a larger, riskier change touching the strategy loader.
6. **History-window backfill (#8).** Accept the "reconcile-before-age-out" mitigation (render + cron), or build a one-time `get_order_history_archive` / venue `fills-history` backfill? Currently documented but not automated.
7. **Ledger mode-namespacing shape. — RESOLVED (required).** Single `closed-trades.jsonl` with a **mandatory `mode` column** (not separate demo/live files). Chosen because Phase 0 already resolves the effective mode at write time, one file keeps the reverse-tail reader and `ordId` dedupe simple, and aggregation filters on `mode`. No longer optional; the schema in Phase 1 must include `mode` on every row.

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
| **P&L ledger inherits WAL pruning** — a future edit wires `_rotate_ledger_if_large` (64 MB / retention 5) onto `closed-trades.jsonl`, silently deleting sealed segments that hold recorded closes → the exact lifetime-loss #10 exists to prevent. | Low | High | Principle 10: P&L ledger is append-only, never pruned; `append_closed_trades()` does not call the rotation helper. Distinct from the WAL, which is prune-safe. |
| **`reconcile` naming collision** — a future edit gates the ledger reconcile on `HERMX_RECONCILE_ENABLED` (the receiver's unrelated post-submit flag, default OFF) → ledger silently stops recording. | Low | High | Flag Dependencies + Phase 1/3 note the two reconciles are distinct; the ledger path and cron are never gated by that flag. |

---

*End of master plan. This document supersedes the two source docs as the authoritative reference for the P&L/dashboard accounting work.*
