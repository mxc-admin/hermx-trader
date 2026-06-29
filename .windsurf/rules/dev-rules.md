---
trigger: always_on
---
# Development Behavior Rules
# Dual-file sync: .windsurf/rules/dev-rules.md (no frontmatter) + .claude/rules/dev-rules.md (has YAML frontmatter)

## Before Writing Code
1. Describe your approach first and wait for approval before implementing.
2. If requirements are ambiguous, ask clarifying questions — do not assume.

## While Writing Code
3. If a task requires changes to more than 3 files, stop and break it into smaller tasks first.
4. Any change touching shared libraries requires explicit confirmation.

## After Writing Code
5. After finishing any code, list the edge cases and suggest test cases to cover them.

## Debugging
6. When there's a bug, write a test or minimal reproduction case first, then fix it until the test passes.

## Learning from Corrections
7. Every time you are corrected, reflect on what went wrong and state a plan to avoid the same mistake. Write the pattern to the appropriate rules file if it's likely to recur — update BOTH `.windsurf/rules/` AND `.claude/rules/` versions.

## Output Management
8. Always prefix shell commands likely to produce noisy output with `rtk` (git, tests, builds, lint, logs).
9. For Cascade `run_command` calls, pipe through `| tail -n 30` or `| grep <pattern>` when full output isn't needed.

## MCP & Secrets
10. Never store API keys or secrets in `mcp_config.json`. Use system env or a secrets manager.
11. Disable unused MCP servers — they consume context on every session startup regardless of whether they are called.

## Dual-File Rule: Shared Rules Live in Two Places
When updating any dual-file rule:
- Always update BOTH `.windsurf/rules/<file>` AND `.claude/rules/<file>`
- `.claude/` copy has YAML frontmatter, `.windsurf/` copy does not
- The rule content below frontmatter must be identical
- These files must stay in sync — one is for Cascade, one is for CC2
