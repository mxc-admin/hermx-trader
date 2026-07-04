# Rejected Approaches
<!-- Populated by /learn. Format: ### [Name] / What / Tested / Verdict / Reason / Date -->

### Delete src/skills/hermes_execution.py as "dead code"
- **What:** Proposed deleting the Python `HermesExecutionSkill` class because it's not imported in the production receiver.
- **Tested:** grep + test-file analysis confirmed it's used in 4 test files (`test_phase5_hermes_skill.py`, `test_okx_paper_integration.py`, `test_kucoin_paper_integration.py`, `test_hyperliquid_paper_integration.py`) as kill-switch regression coverage.
- **Verdict:** REJECTED
- **Reason:** Deleting would break pytest in 4 files and remove kill-switch proof coverage across 3 venues. Not dead code — it's a tested reference seam. (See architecture-decisions: rename to `HermesRelayAdapter`.)
- **Date:** June 2026

### Curator lockout / SHA pin for HermX skills
- **What:** Lock skill files from Hermes Curator auto-edit; pin SHA to detect drift.
- **Tested:** Design review — analysed where money-safety actually lives.
- **Verdict:** REJECTED
- **Reason:** Safety lives in Python gate code, not skill prose. Skill files are non-authoritative; Curator rewrites cannot widen authority. Correct mitigation is git-versioning + diff review, not a lock.
- **Date:** June 2026

### messenger-gateway as a Hermes SKILL.md
- **What:** Build a messenger-gateway skill for Telegram interaction.
- **Tested:** Official Hermes docs + ARCHITECTURE.md §7.5 cross-check.
- **Verdict:** REJECTED
- **Reason:** Telegram is Hermes' native gateway (`hermes gateway`), not a skill. A skill duplicates built-in functionality. HERMX_AGENT_SYSTEM_DESIGN §4.2/§5.e is a doc error.
- **Date:** June 2026

### Helper scripts directory in skills/hermx-control/scripts/
- **What:** Add curl wrapper scripts so the agent doesn't construct HTTP calls inline.
- **Tested:** Design analysis.
- **Verdict:** REJECTED
- **Reason:** The agent (LLM) makes HTTP calls itself using its own tools. Shell scripts add a second code path that must stay in sync with the API, for zero capability gain.
- **Date:** June 2026

### TV CDP (tradingview-chart skill) wired into inline VPS advisor
- **What:** Add `tradingview-chart` skill to `HERMX_ADVISOR_SKILLS` on the VPS so it runs inline at submit time.
- **Tested:** Architecture analysis.
- **Verdict:** REJECTED (removed from plan entirely for now)
- **Reason:** Inline advisor runs as a VPS subprocess at submit time; the chart lives on the operator's Mac, which may be off/asleep. Would be UNKNOWN (fail-open) almost always — near-zero value, real plumbing cost. TV CDP to be installed on VPS later; revisit then.
- **Date:** June 2026

### hermx-skill-bundle.yaml manifest
- **What:** A YAML bundle manifest grouping all HermX skills for one-command install.
- **Tested:** Research — checked for any Hermes loader that consumes bundle manifests.
- **Verdict:** REJECTED (deferred)
- **Reason:** Nothing reads the file today. Add only once ≥3 skills are installed and the use case is real.
- **Date:** June 2026

### Redis/SQLite persistent queue for queue durability (Option A in analysis)
- **What:** Replace in-memory `PROCESS_QUEUE` with a Redis- or SQLite-backed durable queue.
- **Tested:** Durability design analysis.
- **Verdict:** REJECTED
- **Reason:** Over-engineered; adds a network/process dependency. `raw-webhooks.jsonl` is already a durable WAL — a replay reader recovers the queue with no new store. (See architecture-decisions: startup replay.)
- **Date:** June 2026

### Hybrid WAL queue (Option C) for durability
- **What:** A dedicated write-ahead queue file alongside `raw-webhooks.jsonl`.
- **Tested:** Durability design analysis.
- **Verdict:** REJECTED
- **Reason:** Duplicates `raw-webhooks.jsonl` and introduces divergence failure modes (two logs that can disagree). One durable primitive is correct.
- **Date:** June 2026

### Option B — intake_signal_id enrichment on the hot path
- **What:** Compute and persist a stable `signal_id` at intake by calling `normalize()` before queue put.
- **Tested:** Durability design analysis.
- **Verdict:** REJECTED (in favor of Option A: drop time-less payloads on replay)
- **Reason:** Adds a `normalize()` call to the hot intake path. Option A keeps intake cheap and avoids the non-determinism entirely by dropping payloads without a time field.
- **Date:** June 2026

### File watcher / mtime poll for strategy reload
- **What:** Reload strategies/ when the directory mtime changes or a file watcher fires.
- **Tested:** Strategy-reload design analysis.
- **Verdict:** REJECTED
- **Reason:** Race conditions with in-flight signals (a reload mid-signal yields inconsistent strategy state). An explicit reload endpoint is deterministic and sub-second.
- **Date:** June 2026

### Preserving shadow-config.json as read-only fallback
- **What:** Keep `shadow-config.json` for legacy callers while migrating to `engine-config.json`
- **Tested:** grep audit showed zero remaining reads of `shadow-config.json` in `src/`
- **Verdict:** REJECTED
- **Reason:** The receiver, dashboard, and executor already read `engine-config.json`. A fallback file the system ignores creates silent divergence — the installer would produce a broken setup. Delete entirely and update all callers in one pass.
- **Date:** June 2026

### Preserving DEFAULT_CHART_TYPE, POLICY_KEYS, and synthetic fee/funding constants
- **What:** Keep `DEFAULT_CHART_TYPE="heikin_ashi"`, `POLICY_KEYS`, `PERP_MAKER_FEE_RATE`, etc. as env-overridable defaults
- **Tested:** Code review with user — questioned necessity of each constant
- **Verdict:** REJECTED
- **Reason:** `DEFAULT_CHART_TYPE` was never consumed by production (normalize uses payload or None). `POLICY_KEYS` served a dead policy engine. Fee/funding constants are fiction — CCXT provides real per-venue values. All deleted.
- **Date:** June 2026

### PyPI package as primary distribution
- **What**: Package HermX as `pip install hermx` with entry-point scripts.
- **Tested**: Compared against Docker Compose alternative for VPS install.
- **Verdict**: REJECTED
- **Reason**: PyPI buries strategies in `site-packages/` (read-only, hard to edit). `pip install --upgrade` can overwrite package data including jsonl files unless explicitly externalized. Docker named volumes solve both problems natively.
- **Date tested**: June 2026

### COPY engine-config.json in Dockerfile
- **What**: `COPY engine-config.json /app/engine-config.json` in the Dockerfile.
- **Tested**: `git check-ignore engine-config.json` — file is gitignored (`.gitignore:7`).
- **Verdict**: REJECTED
- **Reason**: A fresh clone / CI checkout does not contain `engine-config.json`, so `docker build` fails with `COPY failed: no such file or directory`. Replaced with `COPY config/runtime.demo.json` (tracked, identical defaults).
- **Date tested**: June 2026

### `git checkout --` to discard operator config before pull
- **What:** Use `git checkout -- engine-config.json strategies/` before `git pull` so pull never conflicts.
- **Tested:** Deploy script design review.
- **Verdict:** REJECTED
- **Reason:** `checkout --` permanently discards uncommitted operator edits with no recovery path. Stash preserves them in `git stash list` and allows manual resolution if pop conflicts. (See architecture-decisions: config-safe deploy via stash.)
- **Date tested:** June 2026

### SIGTERM drain handler as a deploy concern
- **What:** Require an app-level SIGTERM handler in `webhook_receiver.py` before accepting the deploy script.
- **Tested:** Code review of WAL (`raw-webhooks.jsonl` fsync before queue put), startup replay, and reconciliation.
- **Verdict:** REJECTED as deploy requirement (deferred to app improvement)
- **Reason:** The WAL + startup replay + reconcile_startup + deterministic cl_ord_id idempotency already guarantee zero acknowledged-transaction loss across a hard restart. SIGTERM drain reduces restart-window reprocessing/noise but does not change the correctness guarantee. It is an app optimization, not a deploy blocker.
- **Date tested:** June 2026

### Schema migration framework for deploy
- **What:** Add a versioned migration step in `deploy.sh` keyed off ledger `schema_version`.
- **Tested:** Design review — checked if any schema break has ever occurred.
- **Verdict:** REJECTED
- **Reason:** `schema_version` / `checkpoint_version` exist and are verified on load, but no schema break has occurred in practice. A migration framework would be premature abstraction today. If a break occurs, handle it as a one-off migration script.
- **Date tested:** June 2026


- **What**: Remove `read_only: true` from the dashboard compose service so it can write `control-state.json`.
- **Tested**: Code review of `dashboard.py:2497-2518` (`_save_control_state`) and compose service definition.
- **Verdict**: REJECTED
- **Reason**: Removing `read_only` weakens container hardening for a single file write. Better: add `HERMX_DATA_DIR=/app/data` + `hermx-state:/app/data` (rw) mount. Root fs stays read-only; only the volume mount is writable.
- **Date tested**: June 2026

### `side="long"` as a schema-invalid test trigger
- **What:** Use `side="long"` (or `source="webhook"`) to trigger `validate_alert_schema()` rejection, replacing the removed `exchange="binance"` trigger.
- **Tested:** Traced `build_record` gate order in `webhook_receiver.py`.
- **Verdict:** REJECTED
- **Reason:** `side not in ALLOWED_SIDES` hard-400s at `build_record:2829` before the jsonschema gate — never reaches `validate_alert_schema()`. `source="webhook"` 202s early via the `non_tradingview_source` path. Valid schema-invalid trigger: `tv_signal_price="not-a-price"` (no pre-schema gate; fails jsonschema `oneOf(number|string)`).
- **Date:** July 2026

### SHADOW_ROOT as backward-compat fallback (HERMX_ROOT or SHADOW_ROOT)
- **What:** Retain `SHADOW_ROOT` as a fallback in root resolution (`HERMX_ROOT or SHADOW_ROOT`) for operator backward compatibility.
- **Tested:** User explicitly rejected it — "we do not need to support this legacy."
- **Verdict:** REJECTED
- **Reason:** Legacy fallbacks create test isolation failures (a stale `HERMX_ROOT` in the environment overrides test-managed `SHADOW_ROOT`) and cause confusion about which var is canonical. Remove completely; operators rename their `.env`.
- **Date:** July 2026

### Per-strategy executors (one executor per strategy_id)
- **What:** Build a separate `CcxtExecutor` per strategy for position reads and reconciliation.
- **Tested:** Architecture analysis during Phase 0.5.
- **Verdict:** REJECTED
- **Reason:** Two strategies on the same `(venue, mode)` share one authenticated account; per-strategy executors would create duplicate connections and hit rate limits. The correct isolation key is `(venue, mode)`, not `strategy_id`.
- **Date:** July 2026

### Gross P&L displayed before empirical fee-inclusion check
- **What:** Display `net_realized_pnl` immediately on Phase 2 landing, relying on assumed `ORDER_PNL_IS_NET` values.
- **Tested:** Design review.
- **Verdict:** REJECTED
- **Reason:** Each exchange has unique fee-inclusion semantics in the `pnl` / `realizedPnl` field. Displaying net before an empirical close-and-compare risks overstating or understating P&L by the fee amount. Ship gross (`pnl_gross`) first; flip `ORDER_PNL_IS_NET` after verification.
- **Date:** July 2026

### Bare Docker volume names in backup/restore scripts
- **What:** Reference `hermx-state`/`hermx-data` directly in `docker run -v hermx-state:/state ...`.
- **Tested:** Compared script volume names against compose-created volume names.
- **Verdict:** REJECTED
- **Reason:** Compose namespaces volumes as `<project>_<volume>` (e.g. `hermx_hermx-state`). Bare names silently create empty phantom volumes — backup tars empty dirs, restore writes to a volume the stack never mounts. Use `<project>_hermx-state` or pin `name:` in the compose file.
- **Date:** July 2026

### Top-level-only `node_modules` prune in find/grep scripts
- **What:** Exclude `node_modules` from dead-link scans with `-path './node_modules' -prune`.
- **Tested:** Ran the scan; 1484 false-positive dead-link failures + spurious exit 1.
- **Verdict:** REJECTED
- **Reason:** `-path './node_modules' -prune` only prunes the top-level dir. `dashboard-ui/node_modules` (535 `.md` files) evades it. Prune by name at any depth: `-name node_modules -prune`.
- **Date:** July 2026

### Custom monitor daemon (`docs/MONITOR_DAEMON_SPEC.md`)
- **What:** A ~600-line custom daemon for scheduling, dedup, delivery, and resilience of HermX monitors.
- **Tested:** Compared against Hermes built-in cron capabilities.
- **Verdict:** REJECTED (superseded)
- **Reason:** Hermes' built-in cron (60s tick scheduler + `hermes cron create/edit/run/list`) already provides scheduler, dedup, delivery, and resilience. A custom daemon is a whole new process/store for zero capability gain.
- **Date:** July 2026

### "Absence detection needs a new gate-lib primitive"
- **What:** Add a new primitive to `hermx_gate_lib.py` to support frequency/zero-intake absence gates (brainstorm doc §7).
- **Tested:** Reviewed the existing lib's condition/sidecar/suppression handling.
- **Verdict:** REJECTED (over-stated)
- **Reason:** The lib already handles arbitrary conditions + sidecar + suppression. An absence gate's script computes a synthetic condition and feeds existing `evaluate()`. Only condition-derivation differs, and that always lives in the per-gate script — no new primitive.
- **Date:** July 2026

### Keeping `hermx-risk-watch` in the installer
- **What:** Leave the risk-watch cron job wired in `install-cron-monitors.sh`.
- **Tested:** grep for `risk_index_gate_enabled` in `src/` — zero hits.
- **Verdict:** REJECTED
- **Reason:** It gates on `risk_index_gate_enabled`, a flag that does not exist in the codebase, so the job can never fire. An inert-but-listed monitor is false reassurance — worse than no monitor. Removed from installer; script kept in repo but unwired.
- **Date:** July 2026
