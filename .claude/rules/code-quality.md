---
globs: ["**/*"]
---
# Code Quality Rules
# Dual-file sync: .windsurf/rules/code-quality.md (no frontmatter) + .claude/rules/code-quality.md (has YAML frontmatter)

## Known Patterns (populated by /learn)
<!-- Entries added here as bugs and patterns are discovered -->

### normalize() is non-deterministic for time-less payloads
When a payload has no `tv_time`, `normalize()` falls back to `now_iso()`, yielding a different `signal_id` each call. On replay this breaks dedupe. Fix: drop time-less payloads on replay (never re-derive their id from wall-clock).

## Anti-Patterns (populated by /learn)
<!-- Entries added here as anti-patterns are identified -->

### Tests that re-implement the handler inline instead of calling production code
`test_intake_hardening.py::test_latest_corrupt_returns_503_not_500` re-implements the handler body in the test, so it passes even if production regresses. Tests must exercise the production code path, not a copy of it.

### Tests armed via a legacy config-flag chain
`test_unknown_resolver_controls.py::_armed_config` arms via the legacy config-flag path. If production moves to a different arming mechanism the test still passes against the dead path and masks a regression. Arm tests through the current production path.
