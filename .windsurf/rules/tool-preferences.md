---
trigger: always_on
---
# Tool Preferences: 3-Tier Architecture

## Guiding Principle: Delegation-First
Cascade is an **orchestrator**. Delegate to CC or CC2 by default. Native tools are last resort — only for micro-fixes that clearly do not warrant delegation.

---

## Tier 1 — Cascade (orchestrator — restricted)
**Allowed:**
- File reads, grep/search, shell commands, memory, deployment
- Mechanical edits ≤5 lines, single file, no logic change (e.g. fix typo, change a constant)

**MUST NOT use Cascade native edit/write tools for:**
- Any change involving logic or new code (even 1 function)
- Any new file creation
- Any change touching >1 file
- Any refactoring, restructuring, or extraction

---

## Tier 2 — CC (`mcp0_*`) — MUST use for standard code work
| Task | Tool |
|------|------|
| Code analysis, reviews, scoring | `mcp0_analyze_code` |
| Any code change >5 lines OR involving logic | `mcp0_refactor_code` |
| Multi-file coordinated changes | `mcp0_multi_file_edit` |
| Web research, documentation | `mcp0_web_search` |
| Parallel independent operations | `mcp0_batch_tool` |

**Trigger conditions (MUST delegate to CC):**
- Code change >5 lines
- New function, class, or module
- Logic changes (even short ones)
- Refactoring or renaming across a file
- Analysis of non-trivial code

---

## Tier 3 — CC2 (`mcp1_claude_code`) — MUST use for major work
**Trigger conditions (MUST delegate to CC2):**
- User explicitly says **"use cc2"**
- New file with business logic
- Task spanning >2 files
- Deep bugs (survived 2+ fix attempts) — use `/deep-bug`
- Building a feature, module, or script from scratch
- Task requires autonomous shell + file editing together
- Unknown scope — needs exploration before acting

> **CC2 call contract (mandatory):**
> - Always set `workFolder` param: `/Users/anatolizurablev/dev projects/hermx`
> - Always prepend prompt: `"Your work folder is /Users/anatolizurablev/dev projects/hermx\n\nTask: ..."`
> - CC2 auto-loads `.claude/CLAUDE.md` + `.claude/rules/` via `PWD` env
> - Timeout: 300s. Prefix `mcp1_` may shift after Windsurf restart — verify with test call.
