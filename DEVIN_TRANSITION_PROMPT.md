# Portable Prompt: Generate a Devin Transition Doc for THIS repo

> **How to use:** Copy everything in the fenced block below and paste it as a single message to your coding agent (Cascade, Claude Code, Cursor, etc.) **while it has this repository open**. It will produce a `DEVIN_TRANSITION_PLAN.md` tailored to the current project. Run it once per repo.
>
> Re-run it any time the repo's agent-context files change.

---

```text
You are preparing this repository to migrate from its current AI coding setup
(Windsurf/Cascade, Claude Code, Cursor, etc.) to Devin. Produce a single,
self-contained markdown file named DEVIN_TRANSITION_PLAN.md at the repo root that
I can paste into Devin to onboard it onto THIS project, including everything that
does NOT migrate to Devin automatically.

=== FACTS ABOUT DEVIN (use these; do not guess) ===
- Devin has two relevant constructs:
  - KNOWLEDGE: general conventions, context, and "gotchas." Retrieved by a TRIGGER
    (the more specific the trigger, the better). Devin reads the ENTIRE matched item,
    so each item must be small and single-purpose. Items can be pinned:
      * pin to a specific repo  -> always used when working in that repo
      * pin to all repos        -> used in every session (for universal habits)
      * no pin                  -> only used when the trigger matches
    Group items in folders for bulk enable/disable.
  - PLAYBOOKS: step-by-step procedures for repeatable tasks. One imperative step per
    line; cover setup -> task -> delivery. Use Playbooks for procedures, Knowledge
    for conventions.
- Devin AUTO-PULLS knowledge (only if the repo is connected) from these files:
    CLAUDE.md, AGENTS.md, .windsurf/ (rules), .cursorrules, .rules, .mdc,
    plus README + file structure.
  Devin does NOT auto-pull generic .md files (skills/*.md, workflows/*.md, docs/*.md,
  ARCHITECTURE.md, etc.).
- The local memory/knowledge databases of other agents (Windsurf/Cascade memory,
  MCP "memory" knowledge graph, Cursor memories) are NOT in the repo and will be
  LOST. They must be transcribed by hand.
- MCP servers configured in the old tool do NOT transfer; Devin has its own
  integration model.

=== WHAT TO DO ===
1. INVENTORY this repo's agent-context surface. Read and summarize whichever exist:
   - CLAUDE.md, .claude/CLAUDE.md, AGENTS.md
   - .windsurf/rules/*, .claude/rules/*, .cursorrules, .rules, .mdc, .cursor/rules/*
   - .windsurf/workflows/*, any custom slash-commands
   - skills/**/SKILL.md and skills/*.md
   - README*, ARCHITECTURE*, docs/**, INSTALL*, setup/**
   - any MCP config (mcp_config.json, .mcp.json, etc.)
   Use codebase search + file reads. Do not assume a file exists — verify.

2. CLASSIFY each piece by Devin destination and pin scope:
   - GLOBAL (pin: all repos)  -> project-agnostic working habits, dev-behavior rules,
     secrets hygiene, bug-fixing discipline, generic procedures (learn/evolve/deep-bug,
     git flows). These should be written ONCE org-wide; in this doc, list them under a
     clearly marked "GLOBAL LAYER (already covered if you maintain a shared set)" so I
     don't duplicate them per repo.
   - REPO (pin: this repo) -> architecture, key files, code-quality gotchas, runbooks,
     domain rules, MCP integrations specific to this project.
   - TRIGGER-ONLY (no pin) -> narrow, rarely-needed notes.

3. CAPTURE LOST MEMORY. The old agent's local memory DB cannot be read from the repo.
   - If you (the agent running this prompt) have access to retrieved/long-term memories
     for this project, transcribe the high-value ones VERBATIM into a "Memories that
     will be lost" section, each with a Trigger + Pin scope.
   - If you do NOT have access, insert a clearly marked TODO list of prompts for me to
     run to surface them (e.g. "ask the old agent: what deploy/test/config gotchas have
     you stored about this repo?"), so nothing is silently dropped.

4. TRANSLATE procedures (workflows, slash-commands, SKILL.md runbooks) into Devin
   PLAYBOOKS using imperative one-step-per-line style.

5. TRANSLATE conventions/gotchas (rules files, SKILL.md context, README/architecture
   highlights) into Devin KNOWLEDGE items. For EACH item, write the exact Trigger
   Description and the Pin scope.

6. NOTE what auto-migrates (so I don't re-enter it) vs. what is lost.

=== OUTPUT FORMAT (DEVIN_TRANSITION_PLAN.md) ===
Produce these sections, in order:
  0. Scope note: this repo's name, and a one-line "global vs repo layer" reminder.
  1. What Devin auto-migrates (table: source file -> present? -> notes).
  2. What is LOST unless copied (bullet list).
  3. Memories that will be lost (verbatim items OR TODO prompts to surface them),
     each with Trigger + Pin scope.
  4. Knowledge items (each: title, Trigger, Pin scope, body). Mark each GLOBAL or REPO.
  5. Playbooks (each: title, Trigger, numbered imperative steps).
  6. MCP servers / integrations mapping (table: old server -> role -> Devin action).
  7. Transition checklist (ordered, actionable).
  8. Quick-reference mapping table (old concept -> lives in -> migrates? -> Devin home).

=== RULES ===
- Keep Knowledge items SMALL and single-purpose; split rather than merge.
- Do not invent file paths, endpoints, env vars, or config keys — cite only what you
  verified in the repo, and mark anything uncertain as "VERIFY".
- Anything money-/safety-/security-critical: flag it explicitly and state the hard
  constraints.
- Be terse and factual. No filler.
- After writing the file, print a short summary: counts of Knowledge items (global vs
  repo), Playbooks, lost-memory items, and any open VERIFY/TODO flags.
```

---

## Notes

- The prompt is **self-contained** — it bakes in the Devin facts so it works even if the agent has no web access.
- It handles the **lost-memory** problem two ways: transcribe verbatim if the agent has memory access, otherwise emit TODO prompts so nothing is dropped silently.
- It produces the **same section structure** as the HermX `DEVIN_TRANSITION_PLAN.md`, so all your repos' docs stay consistent.
- It marks each Knowledge item **GLOBAL vs REPO** so you avoid re-writing the global layer per project.
