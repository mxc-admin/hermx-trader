# Constraints & Limitations
<!-- Populated by /learn. Framework limits, library behaviors, platform quirks. -->

### No close-only execution intent exists in HermX today
`build_strategy_execution_readiness` only produces open intents (`CLOSE_OPPOSITE_IF_ANY` → `OPEN_<dir>`). A standalone close does not exist. `hermx-control` SKILL.md explicitly says "Close is not supported yet." Any Telegram-instructed close requires new Python gate code first.

### Hermes Curator auto-creates and auto-patches skill files
After complex tasks (5+ tool calls), Hermes autonomously creates/patches SKILL.md files. Git-version `skills/` and review Curator diffs before staging to prevent silent advisory quality rot (degraded veto quality without money-safety failure).

### Advisory quality rot: gate chain doesn't validate skill prose
`ExecutionService` only reads advisor output (proceed/skip/unknown). It never reads the skill prose that produced it. A Curator rewrite can silently degrade veto logic (e.g. "elevated → skip" becomes "high-only → skip") with no alert from the gate chain.

### symbol_pauses has no operator API endpoint — set only by reconcile resolver
There is no API endpoint to manually set `symbol_pauses`. The only writer is the unknown/reconcile resolver (`UNKNOWN_RESOLVER_TIMEOUT` path). Operator clears it manually by editing `control-state.json`.

### Two MXC consumers will drift
The HermX Python health gate already reads MXC (`pp_acc`/`pp_vel`, `tab-health.jsonl`). The `dashboard-risk` skill will read the same source independently. Two consumers with different parse, cache, and timing will diverge. Accepted as a known trade-off; optionally serve the skill from HermX's `/api` view later.
