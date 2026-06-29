---
description: Push local commits to remote. Use after committing when ready to sync.
---

# /git-push — Push to Remote

## Step 1: Check unpushed
// turbo
Run: `rtk git log --oneline @{u}..HEAD 2>/dev/null || echo "No upstream set"`

## Step 2: Push
// turbo
Run: `git push` (or `git push -u origin [branch]` if no upstream)

## Step 3: Confirm
```
✅ Pushed: [N] commits to [remote]/[branch]
```
