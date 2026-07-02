# Architecture Decisions
<!-- Populated by /learn. Format: ### [Title] / Decision / Alternatives / Rationale -->

### Safety lives in Python gate code, not skill prose
- **Decision:** Hermes Curator is allowed to evolve/rewrite skill files. No prose locks.
- **Alternatives:** Lock skill files (Curator lockout + SHA pin).
- **Rationale:** `ExecutionService.execute()` is the money-safety gate. Skill prose is non-authoritative advisory guidance. A rewritten skill cannot widen an agent's authority because authority lives in code.

### HermesExecutionSkill → rename to HermesRelayAdapter
- **Decision:** Rename the Python class in `src/skills/hermes_execution.py` and the doc `docs/hermes-execution.md` to "relay adapter".
- **Alternatives:** Delete it (rejected), keep current name.
- **Rationale:** "Skill" collides with Hermes SKILL.md terminology. The Python component is an internal HermX relay adapter, not a Hermes Agent skill. The naming collision causes architectural confusion.

### Close-only path requires new Python gate code
- **Decision:** Operator-instructed close (via Telegram) requires a new reduce-only execution intent + `ExecutionService` close gate before any skill can expose it.
- **Alternatives:** Expose close as a skill capability without new Python gate (rejected — no safety boundary).
- **Rationale:** "Safety in code, not prose" means a close capability must have a Python boundary: reduce-only, must-have-existing-position, fully journaled. A prose-only unlock is unsafe by the project's own principle.

### Kill-switch semantics: closes bypass HERMX_LIVE_TRADING entry block
- **Decision:** When `HERMX_LIVE_TRADING=false`, new entry opens are blocked, but close/flatten operations must still be permitted.
- **Alternatives:** Block all submissions including closes when the kill switch is off.
- **Rationale:** Emergency flatten must work exactly when you've disabled new entries. Blocking closes during the kill-switch defeats its purpose as a "stop new entries" control.

### risk_index_gate_enabled flag on local HermX dashboard
- **Decision:** The toggle to enable/disable MXC risk-index veto lives on the local HermX dashboard (`127.0.0.1:8098`), stored in `control-state.json`, exposed in `GET /api`.
- **Alternatives:** Toggle on the MXC global dashboard (`https://mxc-kinetic-crypto.replit.app/`), or via the `HERMX_ADVISOR_SKILLS` env var.
- **Rationale:** Operator should control their own veto gate locally without depending on an external service. Consistent with the existing `control-state.json` pattern (`symbol_pauses`, `manual_pause`, etc).

### dashboard-risk skill reads risk_index_gate_enabled first
- **Decision:** The `dashboard-risk` skill checks `GET /api` → `risk_index_gate_enabled` before calling MXC. If false → return unknown (fail-open). If true → fetch MXC → evaluate.
- **Alternatives:** Always call MXC regardless of the gate flag.
- **Rationale:** Prevents unnecessary external calls when the gate is disabled. Keeps the skill self-contained and respects operator intent cleanly.

### symbol_pauses is an auto-safety net, not an operator control
- **Decision:** `symbol_pauses` stays as-is; do not remove or refactor.
- **Alternatives:** Remove as an apparently unused operator control.
- **Rationale:** `symbol_pauses` is automatically set by the unknown/reconcile resolver on `UNKNOWN_RESOLVER_TIMEOUT`. It's a self-protection gate, not an operator toggle. There's no API endpoint to set it manually — only the resolver writes to it. Removing it would lose automatic safety on order-reconciliation failures.

### Queue durability via startup replay from raw-webhooks.jsonl
- **Decision:** On startup, replay `raw-webhooks.jsonl` to refill the in-memory queue. No new queue store.
- **Alternatives:** Redis/SQLite queue, hybrid WAL file (both rejected).
- **Rationale:** The durable primitive already exists — every intake is fsync'd to `raw-webhooks.jsonl` before queue put. You need a replay reader, not a new queue.

### Freshness bound on tv_time, not received_at
- **Decision:** The replay freshness gate compares signal bar time (`tv_time`), not server receive time (`received_at`).
- **Alternatives:** Bound on `received_at`.
- **Rationale:** After an outage the server clock is current but the signal is stale; bounding on `received_at` would execute stale bars. The bar time is the true staleness measure.

### Option A — drop time-less payloads on replay
- **Decision:** Payloads lacking a time field are dropped during replay rather than enriched.
- **Alternatives:** Option B (enrich `signal_id` at intake), Option C (hybrid WAL).
- **Rationale:** `normalize()` falls back to `now_iso()` when `tv_time` is absent, producing a different `signal_id` on replay (non-deterministic dedupe). Dropping avoids the ambiguity and keeps the intake hot path cheap.

### control-state.json as cross-process IPC (dashboard ↔ receiver)
- **Decision:** Dashboard and receiver coordinate via atomic writes to `control-state.json`, live-read per signal.
- **Alternatives:** Shared memory, socket/RPC, env vars.
- **Rationale:** Atomic file writes + per-signal read give simple, crash-safe IPC between two processes with no broker. Already the pattern for `symbol_pauses`, `manual_pause`, etc.

### Per-strategy mode override (shadow/demo/live) — single enum, not two flags
- **Decision:** Each strategy carries one mode override (shadow|demo|live) instead of two independent booleans.
- **Alternatives:** Two separate flags (e.g. demo_enabled + live_enabled).
- **Rationale:** A single enum eliminates invalid combinations (e.g. live+shadow simultaneously). The 3-state pill maps 1:1 to the enum.

### Reload endpoint (not mtime poll) for strategy file changes
- **Decision:** Strategy file changes take effect via an explicit reload endpoint.
- **Alternatives:** mtime poll / file watcher (rejected — race with in-flight signals).
- **Rationale:** Explicit reload is deterministic and sub-second, with no race against signals being processed mid-reload.

### Config splits into three sources of truth
- **Decision:** Runtime config lives in `engine-config.json` (strategy_engine + advisor), per-strategy config lives in `strategies/*.json` (instrument, leverage, budget, execution_mode), exchange metadata comes from CCXT (fees, funding, venue details)
- **Alternatives:** Monolithic `shadow-config.json` with all keys merged (dead code — retained for historical context only); or two files with exchange data still hardcoded
- **Rationale:** Separating concerns prevents config drift. Strategy files are the single source for per-instrument settings. CCXT is the single source for exchange reality. `engine-config.json` is minimal and stable.

### ALLOWED_SYMBOLS derives from STRATEGIES, not a config blob
- **Decision:** `ALLOWED_SYMBOLS = frozenset(s.get("asset") for s in STRATEGIES.values())` instead of `CONFIG["assets"].keys()`
- **Alternatives:** Keep a global `assets` block in config
- **Rationale:** If a strategy file defines an asset, it's allowed by definition. No separate allow-list to maintain. Removes a class of "strategy exists but asset not in config" failures.

### Multi-stage Dockerfile with Node UI builder
- **Decision**: Build `dashboard-ui/out` inside a `node:20-slim` stage, copy into Python runtime stage.
- **Alternatives**: CI pre-build (A2) — commit build artifacts to repo; host-mount `dashboard-ui/out` from laptop.
- **Rationale**: Self-contained, no Node on VPS, no build-artifact commits. Stage 1 rebuilds clean from `package-lock.json` regardless of host state.

### Bake tracked config/runtime.demo.json as engine-config.json
- **Decision**: `COPY config/runtime.demo.json /app/engine-config.json` in Dockerfile.
- **Alternatives**: `COPY engine-config.json` directly (rejected — file is gitignored, breaks clean/CI builds).
- **Rationale**: `runtime.demo.json` is tracked, venue-agnostic, and byte-identical to code defaults. The image is self-runnable standalone. Compose bind-mounts `:ro` for operator overrides.

### Seed strategies from image in installer before first compose up
- **Decision**: Installer extracts `strategies/` from the pulled image into the host install dir before `docker compose up`.
- **Alternatives**: Trust the baked fallback (rejected — empty host bind-mounted `:ro` dir shadows baked files → zero strategies → all alerts quarantined).
- **Rationale**: Docker bind-mounts are all-or-nothing; an empty host dir completely replaces the image contents. Pre-seeding guarantees the operator has editable strategy files.

### Config-safe deploy via stash of tracked operator-editable files
- **Decision:** Before `git pull`, stash tracked config files (`engine-config.json`, `strategies/`, `config/`), do `git pull --ff-only`, then `git stash pop`.
- **Alternatives:** `git checkout --` (discards edits permanently), `git rm --cached` (root fix but one-time repo change).
- **Rationale:** Operator-edited tracked files conflict on every upstream config change. Stash preserves edits in `git stash list` and provides a conflict-resolution path. `checkout --` is destructive; `git rm --cached` is the durable fix but outside the deploy script.

### Rollback boundary: code + config + deps only, never WAL
- **Decision:** On health-check failure, `git reset --hard START_SHA`, restore operator config, reinstall deps, rebuild UI, restart. Transaction state (`logs/`, `control-state.json`) is append-only and NEVER rewound.
- **Alternatives:** Roll back everything including `logs/` and `control-state.json`.
- **Rationale:** WAL entries are money events. Rewinding them would erase real trades and break reconciliation. Code and deps are disposable; state is sacred. Idempotency makes replaying existing WAL rows on rolled-back code safe.

### `pip install` is mandatory on every deploy (even `--no-pull`)
- **Decision:** `pip install -r requirements.txt` runs unconditionally in `deploy.sh` regardless of `--no-pull`.
- **Alternatives:** Skip pip when `--no-pull` is set (assumes deps unchanged).
- **Rationale:** `git pull` updates source code; `.venv/` lives outside git. The pulled commit may bump `requirements.txt` or import a new package. Skipping pip risks `ImportError` on restart. pip is a ~1s no-op when already satisfied.

### `git pull --ff-only` for deterministic deploy history
- **Decision:** Deploy script uses `--ff-only` instead of bare `git pull`.
- **Alternatives:** Plain `git pull` (may create surprise merge commits on divergent history).
- **Rationale:** A deploy box should never carry local commits. `--ff-only` aborts on non-fast-forward history, preventing silent merge commits that complicate rollback (`START_SHA` would point to a merge commit, not a clean prior state).

### Pre-deploy snapshot of config + transaction state
- **Decision:** Before touching anything, copy operator config → `.deploy-backups/<ts>/config` and transaction state → `.deploy-backups/<ts>/state`.
- **Alternatives:** No snapshot (rely on gitignore survival + operator discipline).
- **Rationale:** Provides a forensic safety net for botched rollback or operator error. Config snapshot is actively used for rollback restore; state snapshot is insurance only (never restored).


- **Decision**: `hermx-data` (ledgers) + `hermx-state` (snapshots) as named volumes; bind-mounts only for operator-editable config.
- **Alternatives**: Host directories for everything (rejected — permissions mess with non-root uid 10001); bake state into image (rejected — destroyed on every update).
- **Rationale**: Named volumes have independent lifecycle, preserve data across image pulls, and inherit correct ownership from the image's pre-created mount points.

### `exchange` removed from the alert contract
- **Decision:** `exchange` removed from `schemas/tradingview-alert.schema.json` (both `required` and `properties`). The alert now has 7 required fields.
- **Alternatives:** Keep `exchange` as an optional field; keep it required.
- **Rationale:** Strategy is the single source of truth for venue routing. The receiver (`webhook_receiver.py:1002`) backfills `"okx"` when absent (fail-open). Duplicating venue in the alert invites strategy/alert divergence.

### `/hx-tv-alerts` symbol hard-coded from `inst_id`, not TV `{{ticker}}`
- **Decision:** The template's `symbol` field is emitted from `strategy.instrument.inst_id` (may be `BTC-USDT-SWAP`), not the TradingView `{{ticker}}` placeholder.
- **Alternatives:** Use the `{{ticker}}` placeholder so TV fills it at fire time.
- **Rationale:** `{{ticker}}` emits the chart-feed name, which can differ from the strategy's exact instrument format. Hard-coding from the strategy guarantees the `strategy_symbol_mismatch` gate passes.

### `/docker-update` re-seed decoupled from `--force`
- **Decision:** `--force` skips confirmations but does NOT auto-accept strategy re-seed; a separate `--reseed` flag gates re-seeding.
- **Alternatives:** Let `--force` imply re-seed (single flag for all non-interactive behavior).
- **Rationale:** CI/automation (`--force`) must default to safe — never silently overwrite operator strategy files. Re-seed is destructive and must be opted into explicitly.
