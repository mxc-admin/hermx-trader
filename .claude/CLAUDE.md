# Hermx — Claude Code Context

## Response Style
Respond as concisely as possible. Remove unnecessary politeness and explanations. Be terse and direct.

## Project
Hermx is a Python-based crypto trading execution layer that receives TradingView alerts, validates them, and dispatches orders through CCXT exchange adapters. It includes a FastAPI webhook receiver, a local dashboard, and a paper/demo trading path.

## Key Files
- `src/webhook_receiver.py` — FastAPI alert receiver & validation
- `src/executors/ccxt_adapter.py` — CCXT exchange adapter
- `src/execution/service.py` — order dispatch & execution logic
- `src/dashboard.py` / `src/dashboard_core.py` — local dashboard backend
- `config/runtime.*.demo.json` — per-exchange runtime configuration

## Rules (auto-loaded from .claude/rules/)
- `dev-rules.md` — behavior, RTK policy, MCP hygiene, dual-file protocol (always)
- `code-quality.md` — known patterns and anti-patterns (always)
- `tool-preferences.md` lives in `.windsurf/rules/` for Cascade

## Dual-File Rule
`code-quality.md` and `dev-rules.md` exist in BOTH:
- `.claude/rules/` — CC2 reads this (has YAML frontmatter)
- `.windsurf/rules/` — Cascade reads this (no frontmatter, GUI-activated)
When updating any of these, update BOTH locations.

## Skills (auto-discovered from .claude/skills/*/SKILL.md)
- `_general` — fallback knowledge base for cross-cutting decisions
- `hermx-control` — system-specific control & emergency procedures

Invoke: `claude -p "/<skill-name> <args>" --permission-mode dontAsk`

## Proven Patterns (money-system correctness)
- **`raw-webhooks.jsonl` is the durable WAL.** Every intake is fsync'd to it before the queue put, so it — not the in-memory `PROCESS_QUEUE` — is the recovery source. For durability, replay it at startup; don't add a new queue store.
- **The dedupe ledger (`signals.jsonl`) is written AFTER dequeue.** This cleanly partitions "processed" from "queued but not yet dequeued", making it the correctness backstop on replay — not the correlation logic.
- **`received_at` (microsecond ISO) is the join key** between intake and outcome rows. Collision-safe; use it to correlate, not as a freshness measure.
- **Freshness is bounded on signal bar time (`tv_time`), never server time (`received_at`).** After an outage the server clock is current but the bar is stale.
- **Restarts are routine, not rare:** systemd `Restart=always` / `RestartSec=5`. Design recovery for frequent restarts.
- **The cron gate library is proportionate, not gold-plating.** Fingerprint + suppression window + escalation + atomic sidecar (~269 lines in `hermx_gate_lib.py`) is the actual job of a money-adjacent dedup gate. Its tests (~361 lines / 32–40 cases) call the real production functions, avoiding the "re-implement the handler" anti-pattern.
- **Log-and-continue on the observability / money path.** Anomalies (fee-currency mismatch, `None` realized-PnL, reconcile lag) are warned and the row is persisted with the anomaly recorded — never dropped, never coerced to a fabricated zero. Consistent with the fail-open posture: observability failures must not block or corrupt the money path.
