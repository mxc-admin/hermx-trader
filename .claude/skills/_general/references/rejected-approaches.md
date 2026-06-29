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
