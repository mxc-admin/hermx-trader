# hermx-identity — seed files for the Hermes Agent

Not a slash-command skill (no `SKILL.md`, nothing dynamically loaded at runtime).
This folder holds the two files the installer copies into place so a fresh Hermes
Agent instance identifies itself as HermX's assistant instead of the generic Nous
Research default. See install step in `setup/09-hermes-agent.md`.

## Why two files, not one

Validated against Nous Research's own guidance for Hermes Agent
(`hermes-agent.nousresearch.com/docs/user-guide/features/personality` and
`.../docs/guides/use-soul-with-hermes`):

- **`SOUL.md`** — durable identity, tone, and style. Global per Hermes instance
  (`~/.hermes/SOUL.md` / `$HERMES_HOME/SOUL.md`), injected as slot #1 in the system
  prompt, **completely replacing** the built-in default identity. Nous explicitly
  warns against loading it with project details, file paths, ports, or commands —
  their own troubleshooting doc calls "SOUL.md became too project-specific" the most
  common mistake and says to move that content to `AGENTS.md`.
- **`AGENTS.md`** — project architecture, endpoints, conventions, boundaries. This is
  where HermX's mechanics (loopback ports, the `/hx-*` command family, the
  UNKNOWN-never-flat rule, the advisor veto seam) belong.

Rule of thumb from the docs: *if it should follow the agent everywhere, it's
`SOUL.md`; if it belongs to one project, it's `AGENTS.md`.*

## What's in each file here

- **`SOUL.md`** — warm, direct, advisor-minded personality: growth over time, honest
  about uncertainty, cautious with real capital without being cold or robotic. No
  ports, no endpoints, no command names.
- **`AGENTS.md`** — what HermX is, the two jobs Hermes does today, the exact
  loopback surfaces it may call, the UNKNOWN-never-flat and mutation-confirmation
  rules, and the progressive-autonomy roadmap it should be aware of (advisor veto
  today; planned `dashboard-risk` / `kronos-validate` skills later).

## How the installer uses these

`setup/09-hermes-agent.md` step 2 copies `SOUL.md` to `~/.hermes/SOUL.md` (or
`$HERMES_HOME/SOUL.md`) and `AGENTS.md` to the HermX repo root as `AGENTS.md` (Hermes
reads project-scoped `AGENTS.md` from the working directory it's invoked in — the
repo root for both the interactive CLI and the advisor's one-shot subprocess call).

This is **not** applied automatically by any code path — it is an explicit, opt-in
install step an operator runs once, same as registering the `hermx-control` skill.
