---
description: Extract session learnings and write them to the right reference files. Use at the end of any session where new signals, bugs, architecture decisions, or patterns were discovered.
---

# /learn — Session Pattern Extraction

Extract what was learned this session and write it to the appropriate knowledge files.

## Step 1: Identify the target skill

Check which skills exist: list `.claude/skills/*/SKILL.md`.
- Default fallback: `_general` → `.claude/skills/_general/references/`
- Pick the most specific skill for each learning

## Step 2: Extract from session context

**A. Approaches tested and their verdict**
Target: `.claude/skills/<skill>/references/rejected-approaches.md`

**B. Architecture decisions made**
Target: `.claude/skills/<skill>/references/architecture-decisions.md`

**C. Bugs found and their fix pattern** (the pattern, not the one-off fix)
Target: `.claude/rules/code-quality.md` AND `.windsurf/rules/code-quality.md` (BOTH — dual-file)

**D. Proven patterns confirmed**
Target: `.claude/rules/code-quality.md` or `.claude/CLAUDE.md`

**E. Constraint discoveries** (library behavior, framework limits, platform quirks)
Target: `.claude/skills/<skill>/references/constraints.md`

**F. Domain-specific rule changes**
Target: appropriate `.claude/rules/<domain>.md` AND `.windsurf/rules/<domain>.md` (BOTH)

## Step 3: Write to reference files

1. Read the target file first to avoid duplicates
2. **Conflict check**: New learning contradicts existing entry? Flag: `⚠️ CONFLICT: [new finding] contradicts [existing entry in file:line]`
3. Append concisely: what + verdict + reason (3 lines max)
4. **Dual-file rule**: for files in both `.claude/rules/` and `.windsurf/rules/`:
   - Edit `.claude/` copy (has frontmatter)
   - Edit `.windsurf/` copy (no frontmatter)
   - Verify content below frontmatter is identical

For rejected approaches:
```
### [Approach Name]
- **What**: Brief description
- **Tested**: How evaluated
- **Verdict**: REJECTED
- **Reason**: Why it doesn't work
- **Date tested**: [Month Year]
```

For architecture decisions:
```
### [Decision Title]
- **Decision**: What was chosen
- **Alternatives**: What was rejected
- **Rationale**: Why (1-2 lines)
```

## Step 4: Save to Windsurf memory

For critical learnings (proven pattern change, new constraint, major bug):
- Use `create_memory` with appropriate tags
- Check if an existing memory should be updated rather than duplicated

## Step 5: Confirm

```
## /learn Summary
### Written to reference files:
- [file]: [what was added]
### Windsurf memories updated:
- [title]: [what was stored]
### Nothing found to record:
- [categories with no new learnings]
```
