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
