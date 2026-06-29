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
- **Alternatives:** Monolithic `shadow-config.json` with all keys merged; or two files with exchange data still hardcoded
- **Rationale:** Separating concerns prevents config drift. Strategy files are the single source for per-instrument settings. CCXT is the single source for exchange reality. `engine-config.json` is minimal and stable.

### ALLOWED_SYMBOLS derives from STRATEGIES, not a config blob
- **Decision:** `ALLOWED_SYMBOLS = frozenset(s.get("asset") for s in STRATEGIES.values())` instead of `CONFIG["assets"].keys()`
- **Alternatives:** Keep a global `assets` block in config
- **Rationale:** If a strategy file defines an asset, it's allowed by definition. No separate allow-list to maintain. Removes a class of "strategy exists but asset not in config" failures.
