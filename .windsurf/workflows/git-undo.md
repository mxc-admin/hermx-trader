---
description: Undo last git commit or unstage files. Handles soft/mixed/hard resets safely.
---

# /git-undo — Undo Last Action

## Step 1: Determine action
- Keep staged: `git reset --soft HEAD~1`
- Keep unstaged: `git reset --mixed HEAD~1`
- Discard all: `git reset --hard HEAD~1` ⚠️
- Unstage files: `git restore --staged [files]`

## Step 2: Confirm before destructive ops
Show what will be lost. Require explicit yes.

## Step 3: Execute
```
✅ Undone: [action] — HEAD: [hash] [message]
```
