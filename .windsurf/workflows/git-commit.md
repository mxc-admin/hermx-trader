---
description: Stage and commit changed files to local git. Use after any editing session or when the user asks to save/commit work.
---

# /git-commit — Stage & Commit

## Step 1: Check status
// turbo
Run: `rtk git status && rtk git diff --stat`

## Step 2: Determine scope
All changes: `git add -A` | Specific: `git add [files]`

## Step 3: Craft message
Format: `type(scope): description` — types: feat, fix, refactor, chore, docs, perf
Use user's message verbatim if provided.

## Step 4: Commit
// turbo
Run: `git add [scope] && git commit -m "[message]"`

## Step 5: Confirm
```
✅ Committed: [hash] — [message]
   [N] files changed, [X]+ [Y]-
```
