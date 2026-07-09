# HermX — Read-Only Code Review Report

**Date:** 2026-07-06
**Scope:** Full-project audit (money path, executors, webhook intake/auth, dashboard + control-state, deployment, dashboard-ui, tests). Read-only — no source files were modified.
**Method:** Core money-path files (`pnl_ledger.py`, `pnl_strategy_map.py`, `pnl_cloid_map.py`, `orders/journal.py`, `execution/service.py`, `control_state.py`, `advisor.py`, `alerts.py`, `security/webhook_auth.py`, `signals/normalize.py`, `executors/base.py`, `executors/factory.py`, `reconcile/orders.py`) reviewed directly; executors, webhook receiver, dashboard, and deploy/UI reviewed via four parallel deep-dive passes. Every finding below was re-verified against the actual code.

**Confidence bar:** Only high-confidence, code-grounded findings are listed. Known-intentional tradeoffs documented in `CLAUDE.md` / `ARCHITECTURE.md` / `docs/` (e.g. `ORDER_PNL_IS_NET=False` gross default, report-driven `UNKNOWN` reconcile, fail-open advisor, log-and-continue on the observability path, `.gitignore`-inert-for-tracked-files) were checked and are **not** reported as bugs.

**Summary count:** Critical 0 · High 1 · Medium 7 · Low 9

> **Overall posture:** The money-*execution* path is genuinely well-guarded — write-ahead order journal, TOCTOU-safe ledger `flock`, close-bypass invariants, constant-time auth compares, fail-closed gates, and read-side dedup are all correctly implemented and were verified. No auth bypass, ledger double-count, or state-corruption bug was confirmed. The findings cluster in three areas: (1) **observability/display correctness** (a wrong-account UI read, false reconcile-alert storms), (2) **cross-process/cross-thread control-state coordination**, and (3) **deployment coverage that looks active but is inert**.

---

## HIGH

### H1 — Every strategy card displays position (side / UPnL / mark) from the OKX-**demo** account regardless of the strategy's real venue and mode
**Files:** `dashboard-ui/components/StrategyGrid.tsx:8` and `:33`; `dashboard-ui/components/StrategyCard.tsx:92,101,116-117,246`; backend `src/dashboard/model.py:486` (`okx_live = okx_live_demo`), exposed at `src/dashboard/model.py:710`; correct data available but unused at `src/dashboard/model.py:480` (`exch_live_by_env`) / `:555-557` (`strategy_pnl.upl`) / `api_payload` `:712`; missing type in `dashboard-ui/lib/types.ts:278` (declares `okx_live?` only, no `exch_live_by_env`).

**Description:** `StrategyGrid` sources positions from `data?.okx_live?.positions` (`StrategyGrid.tsx:8`) and passes `positions[strategy.asset]` into each card (`:33`). The backend deliberately keeps `okx_live` pinned to the **demo** snapshot for backward-compatible consumers (`model.py:486`, "Singular okx_live stays the demo snapshot"). The correct per-strategy snapshot is keyed in `exch_live_by_env[venue:mode]` and the correct open UPnL is already computed per the strategy's own `(venue, mode)` and surfaced as `strategy.strategy_pnl.upl` — but the UI consumes neither, and `exch_live_by_env` isn't even declared in `types.ts`. The `Equity now` / `Performance` tiles *do* use the correct `strategy_pnl`, so the card is internally self-contradictory.

**Failure scenario:** Installer supports 8 venues + live mode (`install.sh:69-78`).
- A KuCoin strategy holding an open SOL long → card reads `okx_live.positions["SOL…"]` from OKX-demo (no such position) → renders **FLAT, UPnL —**. Operator believes they are flat while a real position is open.
- An OKX-**live** strategy → card shows its OKX-**demo** position side/UPnL/mark instead of the live one.
- `uplPct` (`StrategyCard.tsx:116-117`) compounds it: demo `position.upl` numerator over durable `strategy_pnl` capital denominator.
Only the default single-strategy OKX-demo install is coincidentally correct (there `okx_live` == the strategy's env), which is why it escaped notice. Same wrong-account class as the documented "Executor hard-coded to a venue is a latent wrong-account landmine".

**Fix direction:** Source the card's position from `exch_live_by_env[strategy.env_key].positions[strategy.asset]` (add the field to `types.ts` and consume it), and/or drive the card's UPnL from `strategy.strategy_pnl.upl` and derive side/mark from the per-env snapshot. Do not fix by touching the shared money path — this is a pure read/display remap.

---

## MEDIUM

### M1 — Rate limiter trusts spoofable forwarding headers on any non-loopback bind → shared-secret brute-force throttle bypass + unbounded memory growth
**Files:** `src/webhook_receiver.py:723-729` (`_rate_limit_key`, `trust = HERMX_BIND_HOST not in _LOOPBACK_BIND_HOSTS`); `src/security/webhook_auth.py:47-64` (`client_ip` prefers `CF-Connecting-IP` / `X-Forwarded-For`); `src/security/webhook_auth.py:94-99` (`rate_limit_allow` always does `buckets[source_key] = events`, never evicts). Auth runs *after* the rate-limit (`webhook_receiver.py:1934` rate-limit vs `:1944` auth).

**Description:** `_rate_limit_key` infers "a trusted reverse proxy exists" purely from "the bind host is non-loopback." There is no independent trusted-proxy signal. The bridge compose sets `HERMX_BIND_HOST=0.0.0.0` (see `webhook_receiver.py:279-281`), so any deployment that publishes the port without a header-stripping proxy trusts attacker-supplied forwarding headers.

**Failure scenario:** Receiver bound to `0.0.0.0`, no fronting proxy. Attacker sends each request with a fresh `X-Forwarded-For: <random-ip>`. `rate_limit_key` mints a unique bucket per request → (a) the sliding window never trips → unlimited `X-Webhook-Secret` guesses (the limiter is the only brute-force throttle on the shared secret), and (b) `_RATE_LIMIT_BUCKETS` accumulates one never-pruned key per distinct spoofed IP → unbounded memory growth. Each failed attempt also fires `emit_auth_failure_alert` → one synchronous outbound webhook POST (`src/alerts.py:57-61`), amplifying the abuse. (Mitigating: the shared secret is high-entropy, so practical brute-force is infeasible — this is defense-in-depth + a resource leak, not a direct compromise.)

**Fix direction:** Gate `trust_forwarding` on an explicit "trusted proxy present" config (env flag or trusted-proxy CIDR allowlist), not on the bind address. Separately evict empty buckets (`buckets.pop(key, None)` when `events == []`) or cap the dict size.

### M2 — Balance-drift check hardcodes `"USDT"` → false `RECONCILE_MISMATCH` alert storm for USDC-settled venues (Hyperliquid)
**Files:** `src/executors/ccxt_adapter.py:257` (`check_balance_drift(..., currency: str = "USDT")`); caller `src/reconcile/unknown_resolver.py:99` passes no currency; `get_balance_summary` reads only the requested bucket (`ccxt_adapter.py:1253-1272`); Hyperliquid quotes/settles in USDC (adapter special-cases this at `ccxt_adapter.py:157-160`); equity input is a USD figure from `src/pnl_ledger.py:495` (`_account_equity_estimate`).

**Description:** `_run_balance_drift_checks` sweeps every live `(venue, mode)` and calls `check_balance_drift` with the default `"USDT"`. For a USDC venue, `get_balance_summary("USDT").total` is `0.0`, while the HermX equity estimate is a nonzero USD sum.

**Failure scenario:** A live Hyperliquid strategy with real equity ~$1000 → venue USDT balance = 0.0 → `drift = |0 - 1000| = 1000`, `drift_pct ≈ 100000%` → WARNING + `RECONCILE_MISMATCH` emitted every ~10th resolver tick, forever. Observe-only (won't block), but it floods the alert ledger and destroys the monitor's signal value.

**Fix direction:** Derive the settle/quote currency per venue (from `market["settle"]`, or a per-venue map, e.g. Hyperliquid→USDC) and thread it into `check_balance_drift`.

### M3 — CLOSE amount floored to zero when market precision fails to load → emergency close silently skipped (never-block-a-close violation)
**Files:** `src/executors/ccxt_adapter.py:826` (`close_amount = _decimal_floor(current_contracts, market_spec.get("step") or 1.0)`); `src/executors/ccxt_adapter.py:451` (`step = precision_step or min_amount or 1.0`); skip path `ccxt_adapter.py:855-862` (`status="skipped", reason="zero_size"`).

**Description:** `_market_spec` correctly re-raises when `load_markets`/`client.market` throws (so a total failure → UNKNOWN). But when the market object loads yet lacks both `precision.amount` and `limits.amount.min` (sparse metadata on some venues), `step` defaults to `1.0`. A fractional position (e.g. `0.3` coin) then floors to `close_amount = 0` → the CLOSE leg is appended as `skipped / zero_size` and the position is never flattened.

**Failure scenario:** A coin-denominated / linear venue returns a market with empty `precision.amount` and no `limits.amount.min`; a 0.3-size open position receives a CLOSE_LONG → floored to 0 → skipped. The emergency flatten silently no-ops while the system reports `ok`. (Partly surfaced: `ExecutionService`'s under-execution alert at `src/execution/service.py:411` fires only when `mode == "submit_failed"` and all skip reasons ⊆ `{zero_size, below_instrument_min, insufficient_balance}` — so the operator *may* get an alert, but the position still isn't closed.) This violates the core never-block-a-close invariant.

**Fix direction:** When `close_amount` floors to 0 but `current_contracts > 0`, do not silently skip — submit the raw position size (reduce-only makes over-precision a venue-side no-op) or return an explicit non-terminal error so the stuck-open position is alerted, not reported as handled.

### M4 — Unsynchronized read-modify-write on `control-state.json` → lost updates, including the safety `trading_state=reducing` and programmatic `pause_symbol`
**Files:** dashboard writers with **no lock**: `src/dashboard.py:227` (`_save_control_state`) used by `_set_strategy_override` (`:247`), `_clear_strategy_override` (`:264`), `_set_accounting_start` (`:280`), `_clear_accounting_start` (`:305`), `_set_trading_state` (`:353`); receiver writer `src/control_state.py:60` (`save_control_state`, guarded only by the per-process `wr._STATE_WRITE_LOCK`); programmatic safety write `src/reconcile/unknown_resolver.py:269` (`pause_symbol`). `ThreadingHTTPServer` at `src/dashboard.py:550` runs one thread per request.

**Description:** Every `_set_*` does `load → mutate one key → save`. The atomic `os.replace` prevents a *torn file* but not a *lost update* on a multi-key document. Intra-process: two concurrent dashboard control requests race (no lock at all). Cross-process: the dashboard and receiver share no `flock` on `control-state.json` — the `threading` lock is per-process and useless across the two systemd services. The in-code comment (`dashboard.py:63-66,230-231`) frames "last-writer-wins, never corruption" as safe, which is incorrect for a read-modify-write of independent keys.

**Failure scenario:** Operator sets `trading_state=reducing` (risk-off) and near-simultaneously toggles a strategy mode. The override thread read the file *before* the reducing write committed; its save still carries `trading_state=active` and its `os.replace` clobbers the reducing state. `get_trading_state()` then returns `active` and the `ExecutionService` gate (`service.py:222`) stops blocking reversals — risk-off silently lost mid-incident. The same window can silently drop a programmatic safety `pause_symbol` written by the unknown-resolver (`unknown_resolver.py:269`) when it collides with any dashboard write.

**Fix direction:** Wrap the full read-mutate-write of both writers in a lock, and for cross-process safety take an `fcntl.flock` over the whole RMW (mirroring the fix already applied to `append_closed_trades`). At minimum, re-read under the lock immediately before writing.

### M5 — Corrupt `control-state.json` + a dashboard write silently drops all non-default keys
**Files:** `src/dashboard.py:209-224` (`_load_control_state` returns `{}` on any JSON parse error, `:223-224`), feeding the same writers as M4.

**Description:** Unlike the receiver's `load_control_state()` (which merges onto `default_control_state()` so a full default key set is always re-emitted), the dashboard writers build on the raw `_load_control_state()` result. If that read returns `{}` (parse error), the writer persists `{}` + its one field — a partial document.

**Failure scenario:** `control-state.json` is momentarily unparseable (observed mid-write, or a partial file from a prior crash). Operator sets a strategy override via the dashboard → corrupt read yields `{}` → the write erases all `accounting_windows`, every other `strategy_overrides`, and any `trading_state=reducing`. The receiver's next `load_control_state()` backfills `trading_state=active` from defaults — the risk-off state does not return.

**Fix direction:** In the dashboard writers, distinguish a *parse error* from a genuinely missing file; on parse error, refuse to write / fail loudly rather than persisting a partial state. Or route dashboard writes through the receiver's `load_control_state()`-based merge.

### M6 — `hermx-ledger-reconcile.py` cron is inert; it drives no reconcile
**Files:** `deploy/hermes-scripts/hermx-ledger-reconcile.py:40-43` (documents "GET /api forces `dashboard_model()` to rebuild, which reconciles every `(venue, mode)`"); `src/dashboard/model.py:428-436` (`dashboard_model()` returns the cached model unconditionally, never checks `expires_at`); reconcile side-effect at `src/dashboard/model.py:483` (`strategy_order_history_snapshot`); real driver `src/dashboard/server.py:339-356` (`_refresh_dashboard_cache_loop`, every 15s).

**Description:** The cron's stated mechanism is false: a GET `/api` calls `api_payload → dashboard_model()`, which returns the cache without rebuilding, so the order-history→ledger reconcile side-effect never fires from the cron. The reconcile is actually performed only by the dashboard's own 15s background thread.

**Failure scenario:** The every-10-minute cron shows as active coverage in `hermes cron list` but does nothing (the documented "an inert monitor is worse than no monitor" anti-pattern). Financially safe *today* only because the 15s loop covers the history-window race — but a maintainer who later throttles/changes that loop believing the 10m cron is a real safety net would be wrong.

**Fix direction:** Either make `dashboard_model()` honor `expires_at` (so the cron's GET forces a rebuild), or delete the cron and document the 15s loop as the sole reconcile driver.

### M7 — LLM cron monitors are not `--provider`/`--model` pinned → a global default change dark-fires all four (known, acknowledged deviation)
**Files:** `deploy/install-cron-monitors.sh:108` ("Provider/model intentionally NOT pinned … accepts fail-closed skip risk"); unpinned jobs `hermx-weekly` (`:142`), `hermx-reconcile` (`:147`), `hermx-daily` (`:153`), `hermx-signal-late` (`:180`). Contradicts `docs/7-EXECUTION_MONITORING.md` §5 (lines 364-378, open items 471-473) and the `code-quality.md` rule "Installer must not contradict its own design doc (pin `--provider`/`--model`)".

**Description:** Hermes is fail-closed on provider/model resolution: if the global default provider/model changes, an unpinned LLM job silently skips (dark-fire) instead of running. All four LLM monitors would go dark simultaneously and invisibly.

**Failure scenario:** Operator changes the global Hermes model default → the four LLM monitors stop firing, still listed as active, no error. This is explicitly listed as a *known deviation / open follow-up* in the design doc, so it is real but already acknowledged, not novel.

**Fix direction:** Add pinned `--provider`/`--model` to the four LLM `ensure_job` calls, aligning the installer with §5 of its own design doc.

---

## LOW

### L1 — `get_positions()` mis-signs a blank-side position as positive → false `position_drift` alerts
**File:** `src/executors/ccxt_adapter.py:1211-1212` (`side = str(row.get("side") or "").lower(); signed = contracts if side == "long" else (-contracts if side == "short" else contracts)`); consumed by `detect_position_drift` (`ccxt_adapter.py:225-253`). Contrast the correct recovery in `_position_snapshot` (`ccxt_adapter.py:477-493`).

**Description:** When ccxt leaves `side` blank, `signed` becomes **positive** regardless of true direction — unlike `_position_snapshot`, which recovers side from `info.posSide` / `info.pos` / `positionAmt` before defaulting. A real short of size X read with a blank side → `venue_qty = +X` vs journal `−X` → `drift = 2X` → false `position_drift` alert. Observe-only.

**Fix direction:** Apply the same native-field side recovery used in `_position_snapshot` inside `get_positions`, falling through to `pos_side` / signed `info.pos` before defaulting the sign.

### L2 — `canceled_fill_size_unavailable` guard is unreachable; a fill-size-less canceled order is rejected as flat
**Files:** `src/reconcile/orders.py:91-92` (None-guard on `acc_fill_sz`); but normalization coerces the field to a float: `src/executors/base.py:58` (`empty_normalized_order` → `acc_fill_sz: 0.0`) and `_normalize_order` at `src/executors/ccxt_adapter.py:755` (`_to_float(order.get("filled"), 0.0)`).

**Description:** Every order reaching `map_order_outcome` was produced by the adapter's normalization, which turns a missing `filled` into `0.0` — never `None`. So the `acc_fill_sz is None` guard at `orders.py:91` never fires, and a canceled order whose venue omitted `filled` falls through to `orders.py:93` `canceled_zero_fill` → **REJECTED** (dropped as flat), the exact outcome the guard was written to prevent. Latent until a venue returns canceled-after-partial with a null `filled`.

**Fix direction:** Preserve `None` through normalization for the fill-size field (a distinct sentinel) so `map_order_outcome` can distinguish "0 filled" from "fill size unknown," instead of coercing missing `filled` to `0.0`.

### L3 — HMAC replay timestamp skips the epoch-millisecond normalization the rest of the code applies
**Files:** `src/security/webhook_auth.py:108-119` (`parse_replay_timestamp` returns `float(text)` directly) vs `src/webhook/timeutil.py:29-33` (`parse_tv_time` divides values `> 10_000_000_000` by 1000).

**Description:** A signer emitting `X-Webhook-Timestamp` in epoch **milliseconds** yields ~`1.72e12`; `abs(now - ts_value)` becomes enormous → `hmac_replay_window` → 401 on every request. Only reachable when `HERMX_REQUIRE_HMAC=true` and the signer uses millis — a client-contract footgun, not a live-traffic bug.

**Fix direction:** Apply the same `>10^10 ⇒ /1000` normalization in the numeric branch of `parse_replay_timestamp`, or document epoch-seconds explicitly in the signer contract.

### L4 — A single mid-file-corrupt line in `signals.jsonl` wedges ALL signal processing, not just dedupe
**Files:** `src/webhook/ledger_io.py:84-85` (`read_jsonl_tolerant` raises on non-trailing corruption) reached via `src/signals/dedupe.py:66` inside `_load_signal_dedupe_index`, which sets `loaded=True` only at the end (`dedupe.py:91`). Startup quarantine (`startup_quarantine_partial_ledgers`) cleans only *trailing* tears.

**Description:** A mid-file corrupt line makes every `check_and_mark_signal → _load_signal_dedupe_index` raise; `loaded` never flips, so the read re-runs and re-raises for every subsequent signal. `process_payload_async` catches it (records `error`) — so nothing double-executes (fail-closed, safe) — but intake is fully halted until manual repair. Consistent with the deliberate "fail loud on mid-file corruption" posture; flagged as an operational sharp edge (append is all-or-nothing, so a mid-file tear is unexpected).

**Fix direction:** At startup, quarantine a mid-file-corrupt `signals.jsonl` line (with a loud alert) so intake degrades to at-least-once rather than halting entirely; at minimum emit an operator alert distinguishing this from a normal quarantine.

### L5 — Health page renders "—" for a healthy-but-stale executor
**Files:** `dashboard-ui/app/health/page.tsx:64` (`executor?.ok ? 'OK' : executor?.error ? 'ERROR' : '—'`); backend `src/dashboard/model.py:288,277` (`ok = healthy and not stale`; `error = None when healthy`).

**Description:** A healthy-but-stale read (`ok=false, error=null`) falls through to `'—'` instead of a `STALE`/`OK` indicator. `ExecutorHealthCard.tsx` and `SummaryCards.tsx` handle staleness explicitly; the health page does not. Display-only.

**Fix direction:** Mirror the staleness handling from `ExecutorHealthCard` on the health page (treat `ok=false && error==null` as `STALE`).

### L6 — Summary "demo/live" counts ignore control-state overrides
**Files:** `dashboard-ui/components/SummaryCards.tsx:54-59` (counts by `s.execution_mode`, the file value); per-card pill uses override-aware `effective_mode` (`dashboard-ui/components/StrategyCard.tsx:95`).

**Description:** A `control-state.json` override (e.g. pause→live) makes the summary "X demo / Y live" disagree with the per-card mode pills. Display-only inconsistency.

**Fix direction:** Count by `effective_mode` in `SummaryCards`, matching the pill source.

### L7 — `dashboard_model()` never consults `expires_at`; the cache freezes if the refresh loop is absent
**Files:** `src/dashboard/model.py:419-436` (read path returns cache whenever `model is not None`) vs `:512-517` (build stamps `expires_at`); the only refresh driver is `_refresh_dashboard_cache_loop`, started solely from `__main__` (`src/dashboard.py:553` / `src/dashboard/server.py:339-356`).

**Description:** `expires_at` is dead code on the read path. In production the loop runs, so this is not a live failure — but any import/embedding that calls `dashboard_model()` without starting the daemon thread builds once and serves that model forever, silently. This is also the root cause of M6.

**Fix direction:** Honor `expires_at` on the read path (stale-while-revalidate with a real TTL check) so correctness does not depend solely on the background daemon being started.

### L8 — `pnl_cloid_map`: full-file `readlines()` per lookup (unbounded) + no `fsync` on write (inconsistent with the sibling map)
**Files:** `src/pnl_cloid_map.py:56-57` (`resolve_cloid` reads the entire append-only file into memory each call); `src/pnl_cloid_map.py:40-41` (`record_cloid_mapping` writes without `flush`/`fsync`, unlike `src/pnl_strategy_map.py:104-106` which fsyncs).

**Description:** For a Hyperliquid deployment, `cloid-map.jsonl` grows forever and every reconcile-time `resolve_cloid` slurps the whole file (O(file) memory + time per lookup). Separately, the write is not durably flushed, so a crash immediately after a submit can lose the cloid→mxc mapping, orphaning that order's attribution on reconcile — whereas the strategy map (same purpose) fsyncs. Minor today (Hyperliquid-only, low volume).

**Fix direction:** Read newest-first with an early-exit/streaming reverse read (or cache the map), and add `flush()` + `os.fsync()` to `record_cloid_mapping` for parity with `pnl_strategy_map.record_submit_strategy`.

### L9 — Fee-currency mismatch warning is skipped for non-dash instrument ids
**File:** `src/pnl_ledger.py:647-650` (`_parts = str(inst_id).split("-")`; quote derived only when `len(_parts) > 1`).

**Description:** The fee-currency-mismatch guard derives the instrument's quote by splitting `inst_id` on `-`. For a normalized/underscore or slash inst_id (`BTC_USDT`, `BTC/USDT`) the split yields no quote, so the mismatch check is silently skipped and no warning is emitted for a genuinely non-quote fee. The row still persists correctly (the mismatched fee is excluded from the USD total downstream), so this only weakens the operator observability warning, not the accounting.

**Fix direction:** Normalize `inst_id` (accept `-`, `/`, `_` and the `:settle` suffix) before extracting the quote, so the mismatch warning fires for every venue's id shape.

---

## Areas reviewed with NO high-confidence finding (verified sound)

- **Order-execution money path** (`execution/service.py`, `orders/journal.py`): write-ahead PLANNED/SUBMITTED journaling before submit, `order_state_can_transition` re-validation under lock against the authoritative latest state, idempotency gate on `cl_ord_id`, UNKNOWN-on-timeout, and the C1 submit-time strategy-map write are all correct. `_record_tentative_outcome`'s SUBMITTED→SUBMITTED skip is intentional and correct.
- **Ledger correctness** (`pnl_ledger.py`): `append_closed_trades` takes the `flock` before reading dedup keys (TOCTOU-closed), read-side composite-key dedup preserves fully-degenerate/malformed rows, net back-fill is read-only/non-persisted, and the append-only ledger is never pruned — all consistent with the documented invariants.
- **Never-block-a-close invariant:** verified end-to-end — kill switch (`service.py:177`), symbol pause (`:192`), `trading_state=reducing` (`:222`), and equity stop (`:237`) all exempt `close_only`; webhook closes route through `_build_close_record`/`execute_operator_close` (`webhook_receiver.py:1414`) and never reach the advisor, so the advisor veto cannot block a close.
- **Auth / HMAC:** constant-time `hmac.compare_digest` for both secret and HMAC, fail-closed on blank secret / missing HMAC key, symmetric replay window over `timestamp‖body`. (See M1/L3 for the rate-limit and ms-timestamp edges — the core compares are sound.)
- **`default_control_state()` merge** (`control_state.py:79-105`): all dict-valued keys re-attached after the `{k in default}` filter; `trading_state` present in defaults. The documented key-drop class is handled.
- **normalize / dedupe determinism:** `_has_time_field` matches `normalize`'s `tv_time` fallback list; close-bar ids hash on `action` (deterministic on replay); time-less payloads fall back to `now_iso()` and are correctly dropped on replay.
- **Reconcile UNKNOWN handling** (`reconcile/orders.py`): absence/not-found stays UNKNOWN (report-driven), only FILLED/REJECTED terminalize — matches the intentional Nautilus-aligned design.
- **Per-order (venue, mode) threading** (`reconcile/executor_select.py`, `orders/journal.py:437-446`): venue/mode/`simulated_trading` read from each order's own intent record with an OKX-demo last-resort fallback; no hardcoded `("okx","demo")` literals in the reconcile call sites.
- **Secrets & git hygiene:** `.env`, `HERMX_SECRET.txt`, `control-state.json`, `closed-trades.jsonl` are all git-ignored and untracked (verified via `git check-ignore`); `dashboard-ui/.env.local` (tracked) contains no secret, only a localhost API base + refresh interval.
- **Docker / deploy pitfalls:** `dashboard-ui/out` is baked (`Dockerfile:53`); `read_only: true` dashboard keeps `hermx-state:/app/data` rw with matching `HERMX_DATA_DIR` (`docker-compose.yml:47-56`); the installer seeds `strategies/`, `engine-config.json`, `serve.json` from the image before first `up` (`scripts/install-docker.sh:79-86`) — the documented empty-bind-mount-shadowing and volume traps are handled.
- **`useDashboard.ts`:** request-generation guard, visibility-based timer teardown, and `setTimeout` (not `setInterval`) polling — no overlap/leak/stale-write races.
- **`executor.health()` timestamp parsing, snapshot cache keys** (`snapshots.py`): cache keys are distinct per `mode` / `venue:mode` with no cross-read collisions; `generated_at` fed to `parse_dt` is an ISO string — no false-stale.

---

## Test-coverage gaps (spot-check, not exhaustive)

- **Wrong-account UI read (H1):** untested end-to-end. `StrategyCard.test.tsx` exists but does not assert that a non-OKX / live strategy's position comes from its own env snapshot rather than `okx_live` (demo). A test with a KuCoin strategy + a populated `exch_live_by_env` and an empty `okx_live` would have caught it.
- **Control-state RMW race (M4/M5):** no test exercises two concurrent writers (dashboard thread + receiver `pause_symbol`) to assert neither `trading_state` nor a symbol pause is lost. `test_phase3_strategy_overrides.py` covers single-writer behavior only.
- **Close-floored-to-zero (M3):** no adapter test feeds a market spec lacking both `precision.amount` and `limits.amount.min` against a fractional position to assert the close is not silently skipped.
- **`canceled_fill_size_unavailable` (L2):** the guard has no test that drives it through the real `_normalize_order` path — which is exactly why its unreachability went unnoticed (matches the documented "tests that hand-inject bypass the reconcile seam" anti-pattern).
