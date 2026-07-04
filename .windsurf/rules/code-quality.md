---
trigger: always_on
---
# Code Quality Rules
# Dual-file sync: .windsurf/rules/code-quality.md (no frontmatter) + .claude/rules/code-quality.md (has YAML frontmatter)

## Known Patterns (populated by /learn)
<!-- Entries added here as bugs and patterns are discovered -->

### `.gitignore` is inert for already-tracked files
A file listed in `.gitignore` that is already tracked in git remains tracked — `gitignore` only prevents *new* untracked files from being staged. `git check-ignore <file>` exits 1 for tracked files regardless of `.gitignore` contents. This means operator-edited tracked config files (`engine-config.json`, `strategies/*.json`) will still conflict on `git pull` even if `.gitignore` lists them. The fix is `git rm --cached` (one-time repo change), not adding them to `.gitignore`.

### normalize() is non-deterministic for time-less payloads
When a payload has no `tv_time`, `normalize()` falls back to `now_iso()`, yielding a different `signal_id` each call. On replay this breaks dedupe. Fix: drop time-less payloads on replay (never re-derive their id from wall-clock).

### Dashboard regression when deleting a config source
Deleting a config file or reducing a config function to `return {}` can break downstream consumers that relied on it for defaults. The `_dashboard_executor` set `exchange="ccxt"` when config was empty, but `"ccxt"` is a backend name — not a venue — so `CcxtExecutor._exchange_id()` returned `"ccxt"` instead of falling back to `"okx"`, causing `getattr(ccxt, "ccxt")` → `None`. When removing a config source, audit every consumer for masked defaults, especially where `or` chains mix backend and venue names.

### shadow-config.json is dead code
`src/dashboard_core.py:shadow_config()` returns `{}` as a no-op. The receiver (`webhook_receiver.py:280-284`) sources from `engine-config.json` via `load_engine_config()`. Any installer or build step referencing `shadow-config.json` is stale. Verify with grep before assuming config file relevance.

### Dashboard UI silently broken when dashboard-ui/out not in image
`dashboard.py:2335` resolves `STATIC_DIR = REPO_ROOT / "dashboard-ui" / "out"`. If the Dockerfile omits `COPY dashboard-ui/out`, the `.is_dir()` gate fails and the dashboard falls back to legacy server-rendered HTML with no error. Any Docker image serving the dashboard must include the built `out/` directory.

### Empty bind-mounted directory shadows baked image files
When a host directory is bind-mounted over an image directory (e.g., `./strategies:/app/strategies:ro`), an empty host directory completely replaces the image contents — the baked files are inaccessible. If the operator needs those files, the installer must seed the host directory from the image before the first `docker compose up`.

### Dashboard writes control-state.json but needs writable mount
`dashboard.py:2497-2518` writes `control-state.json` for per-strategy mode overrides. Running the dashboard `read_only: true` without a writable volume mount for `HERMX_DATA_DIR` causes silent write failures — mode toggles appear to work in the UI but do not persist. The compose file must mount `hermx-state:/app/data` (rw) even when `read_only: true` is set on the root filesystem.

### webhook_receiver validates `side`/`source` before the schema gate
`build_record` gates `side not in ALLOWED_SIDES` → hard 400 (line 2829) and `source != "tradingview"` → 202 `non_tradingview_source` (line 2831), both BEFORE `validate_alert_schema()`. Tests exercising `enforce_alert_schema` must pick a trigger that survives these gates — `tv_signal_price="not-a-price"` has no pre-schema gate and fails jsonschema `oneOf(number|string)`.

### Docker Compose namespaces volumes with the project name
Volumes declared without `name:`/`external:` are prefixed with the compose directory basename (e.g. `hermx-state` → `hermx_hermx-state`). Scripts using bare `hermx-state` in `docker run -v` silently create empty phantom volumes. Reference `<project>_hermx-state` or pin `name:` in the compose file.

### `find`/grep scripts must prune `node_modules` by name, not path
`-path './node_modules' -prune` only prunes the top-level dir; `dashboard-ui/node_modules` (535 `.md` files) evades it and produces false-positive failures. Use `-name node_modules -prune` to catch nested dirs at any depth.

### An inert monitor is worse than no monitor
A cron job that gates on a nonexistent flag (e.g. `risk_index_gate_enabled`) never fires but still appears in `hermes cron list` as active coverage — false reassurance that risk is watched. Fix: either implement the flag or delete the job. `hermx-risk-watch` was removed from the installer for exactly this reason.

### Installer must not contradict its own design doc (pin `--provider`/`--model`)
`EXECUTION_MONITORING.md` §5 mandates pinning `--provider`/`--model` on every LLM cron job so a global-default change can't silently dark-fire monitors (fail-closed skip). `install-cron-monitors.sh:98` said "intentionally NOT pinned" — a model-default change silently disables all LLM monitors. Pin them.

### Absence detection needs no new gate-lib primitive
Frequency/zero-intake ("absence") gates do NOT require a new primitive in `hermx_gate_lib.py`. The per-gate script reads a rolling window, computes a count, manufactures a synthetic condition, and feeds it to the existing `run_gate()`/`evaluate()`. Only the condition-derivation is gate-specific (it always lives in the script); the lib touch is at most a suppression-window key.

### Test fixtures must manage ALL env vars in a resolution chain
When root resolution uses `HERMX_ROOT or SHADOW_ROOT`, a test fixture that only manages `SHADOW_ROOT` fails when `HERMX_ROOT` is set in the environment. Whenever renaming or collapsing env var aliases, update ALL test fixtures to save/set/restore the full resolution chain — not just the old name.

### Executor hard-coded to a venue is a latent wrong-account landmine
`_effective_execution_config()` hard-coding `ccxt_exchange="okx"` with no `simulated_trading` key caused: (a) live OKX orders reconciled against OKX demo → not_found → UNKNOWN state, (b) KuCoin/Bybit orders reconciled against OKX → stuck forever. Whenever building an executor for reconciliation, read the venue and mode from the **order's own intent record**, not from global defaults. Default to OKX-demo only as last-resort fallback.

### Reconcile call sites must use the actual (venue, mode) read, not hardcoded literals
`reconcile_from_order_history(rows, "okx", "demo")` hardcoded in `dashboard.py` would mis-label live or non-OKX closes as OKX-demo. Always thread the actual `(venue, mode)` from the executor that fetched the rows into the reconcile call.

### P&L ledger must never inherit WAL size-rotation policy
`closed-trades.jsonl` is a lifetime record — unlike the raw-webhooks WAL which has a bounded `SIGNALS_MAX_N` rotation, the P&L ledger is append-only forever. Any refactor that uses WAL rotation helpers for the ledger file silently deletes financial history. Never apply pruning to `closed-trades.jsonl`.

### Net P&L must not be displayed until empirically verified per-venue
`ORDER_PNL_IS_NET` is False (gross) for all venues by default. Displaying net P&L before verifying the exchange's fee-inclusion semantics on a real close can overstate or understate P&L. Ship gross first, then flip the flag after empirical check.

### Hyperliquid cloid is 0x hex, not decimal
`_to_hyperliquid_cloid()` produces `0x{sha256[:32]}` — a hex string, not a decimal integer. Any `is_hermx_cl_ord_id()` guard using `text.isdigit()` misses it. Broaden the guard to `startswith("0x") or isdigit()` when resolving Hyperliquid cloids.

### Append-only ledger needs read-side dedup, not just write-side
Write-side dedup under a lock still leaves a TOCTOU window where a duplicate row can be appended between the check and the write (or by a concurrent writer). For a money ledger like `closed-trades.jsonl` that doubles P&L, add read-side dedup by composite key (`exchange`, `inst_id`, `ord_id`, `mode`), last-wins, and preserve malformed rows rather than dropping them.

### Skip non-terminal venue rows in reconcile
`reconcile_from_order_history` must only convert ledger rows whose venue state is terminal (`filled`, `canceled`) or explicitly `reduceOnly=True` into closes. In-flight partial fills otherwise get misclassified as closes, corrupting position and P&L attribution.

### Log fee-currency mismatch and None realized-PnL instead of silently writing zero
A fee reported in a non-quote currency, or a missing venue `pnl`/`realizedPnl` field, silently corrupts accounting if coerced to zero. Warn (log-and-continue) and persist the row with the anomaly recorded — never write a fabricated zero.

### `reconcile_from_order_history` writes `strategy_id=None` — never recoverable from cl_ord_id alone
Exchange order-history rows carry no `strategy_id`. `mxc{sha256(...)}` cl_ord_ids are not invertible. Any read path that filters `row.strategy_id == real_sid` will silently return $0 for every reconciled row. The only correct fix is a **submit-time map** (`pnl_strategy_map.py`): record `{cl_ord_id → strategy_id}` at the moment the order is submitted (when both are known). Never attempt hash reversal.

### Tests that hand-inject `strategy_id` bypass the reconcile attribution seam
If every ledger test seeds rows with `strategy_id` set directly (bypassing `reconcile_from_order_history`), the full `reconcile → aggregate_strategy_pnl` round-trip is untested. A single missing end-to-end test (`reconcile_from_order_history(hermx_rows) → aggregate_strategy_pnl(sid)`) was why C1 shipped silently. Always include at least one round-trip test for any ledger attribution path.

### `default_control_state()` merge filter silently drops keys not in the default dict
`load_control_state()` uses `{k for k in default}` to merge old files, silently dropping any key not present in `default_control_state()`. Every new `control-state.json` key (`accounting_windows`, `trading_state`, etc.) MUST be added to `default_control_state()` AND explicitly re-attached after merge. Missing this causes the key to vanish on the next state load.

### `fcntl.flock` must wrap the ENTIRE read-modify-write, not just the write
`append_closed_trades` originally called `_load_existing_keys` (the dedup read) BEFORE acquiring `flock(LOCK_EX)`. Two concurrent processes could each read a stale key set and both write the same `ordId` → duplicate rows → double-counted P&L. The lock must be acquired on an `a+` handle BEFORE reading keys, with the full read-filter-append inside the locked section.

### `operator_close_{symbol}_{sid}_{day}` is ambiguous with simple `rsplit`
Both `symbol` (e.g. `BTC_USDT`) and `strategy_id` (e.g. `my_strat_v2`) can contain underscores. A naive `rsplit("_", 2)` misparses the sid. The UTCday is always exactly 8 digits (`YYYYMMDD`, no underscores): strip the rightmost `_YYYYMMDD` suffix first, then use the submit map to resolve the rest. Never rely on split-counting alone for this format.

### Pre-trade notional ceiling must be independent-absolute, not derived from budget×leverage
A notional cap expressed as `budget_usd × leverage` is tautological — a fat-fingered `budget_usd` raises both the ceiling and the notional simultaneously, so the gate never fires. The ceiling must come from an independent source: `min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD_ENV)` where both are operator-set absolute values. Unset ceiling = no cap (safe default).

### `HALTED` trading state that blocks closes violates HermX's never-block-a-close invariant
A `HALTED` state that blocks all submissions including closes is unsafe — emergency flatten must work exactly when new entries are disabled. The correct safe state is `reducing`: blocks new reversals/opens, but `close_only=True` signals always pass regardless of state.

### Re-read current code before executing a planned change
Tests and code can change between when a plan is written and when it is executed (concurrent sessions, prior fixes). A plan item said to delete a hardcoded `reconcile_from_order_history(rows, "okx", "demo")` call, but a prior session had already fixed it by threading real `(venue, mode)` and a test now requires the call to exist — blind deletion would have regressed. Always re-read the current code (and its tests) immediately before acting on a planned edit.

### Static analysis can close empirical "needs a live check" validations
Not every "requires a live/production check" item actually does. `ord_id` uniqueness across venues and legacy-path reachability were both answered conclusively by reading the code plus existing tests. Attempt static resolution (source + tests) before scheduling a live experiment; reserve live checks for genuinely runtime-only facts (e.g. venue fee-inclusion semantics).

## Anti-Patterns (populated by /learn)
<!-- Entries added here as anti-patterns are identified -->

### Tests that re-implement the handler inline instead of calling production code
`test_intake_hardening.py::test_latest_corrupt_returns_503_not_500` re-implements the handler body in the test, so it passes even if production regresses. Tests must exercise the production code path, not a copy of it.

### Tests armed via a legacy config-flag chain
`test_unknown_resolver_controls.py::_armed_config` arms via the legacy config-flag path. If production moves to a different arming mechanism the test still passes against the dead path and masks a regression. Arm tests through the current production path.
