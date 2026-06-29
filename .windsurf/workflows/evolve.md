---
description: Consolidate accumulated session learnings into skills and rules. Run periodically (weekly or after 5+ /learn sessions) to promote patterns to permanent architecture.
---

# /evolve — Knowledge Consolidation

## Step 1: Read all reference files

Read ALL files in:
- `.claude/skills/*/references/*.md`
- `.claude/rules/*.md`
- `.windsurf/rules/*.md`
- `.claude/CLAUDE.md`

## Step 2: Sync-check dual-file pairs

Strip frontmatter before diffing (plain diff always false-positives):

```bash
diff <(sed '1,/^---$/{ /^---$/,/^---$/d }' .claude/rules/code-quality.md) .windsurf/rules/code-quality.md
diff <(sed '1,/^---$/{ /^---$/,/^---$/d }' .claude/rules/dev-rules.md) .windsurf/rules/dev-rules.md
```

If ANY diff is non-empty:
1. `⚠️ DRIFT DETECTED: [file]` — show the diff
2. Resolve before proceeding — drift means one AI has stale rules

## Step 3: Identify consolidation opportunities

**A.** Promote to rule — pattern confirmed multiple times → `.claude/rules/`
**B.** Retire stale — rejected approach >6 months old AND conditions changed → flag
**C.** Merge duplicates — same info in multiple files → keep most complete
**D.** New skill candidates — repeated task, no skill yet. Decision tree:
  - Has repeatable workflow / validation steps? → Create domain skill
  - Just accumulated knowledge? → Keep in `_general`
**E.** CLAUDE.md drift — still accurate? Over ≤50 line budget? Promote excess to rules/references.

## Step 4: Propose changes

```
## /evolve Proposal
### Promote to rules: [rule] + [what] — reason
### Retire/update stale: [file:line] — suggest [action]
### Merge duplicates: [entry] in [A] and [B] — keep [A]
### New skill candidates: [task] × [N] times → SKILL.md
### CLAUDE.md: [N]/50 lines — [action]
### Clean: [categories unchanged]
```

## Step 5: Execute approved changes

1. Make each edit
2. Dual-file rule: update BOTH copies, respecting frontmatter asymmetry
3. Append/update operations only — never rewrite

## Step 6: Confirm

```
## /evolve Complete
### Files updated: [file]: [what changed]
### Net: Rules +N / Retired N / Merged N / Skills flagged N
### Next /evolve: after [N] more /learn sessions or [date]
```
