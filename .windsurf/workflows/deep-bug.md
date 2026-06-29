---
description: Systematic approach to solving difficult bugs that resist simple fixes. Use when a bug has survived 2+ fix attempts or when root cause is unclear.
---

# /deep-bug — Deep Bug Investigation

## Step 1: Reproduce
Write a minimal reproduction case demonstrating the bug.

## Step 2: Isolate
1. Identify failing output
2. Trace backwards through call chain
3. Add logging at each layer boundary
4. Find exact layer where expected ≠ actual

## Step 3: Root cause analysis
- **What** is the actual root cause? (not symptom)
- **Why** does this code exist in its current form?
- **Where** else might this pattern exist?

## Step 4: Fix upstream
Fix at root cause. Prefer minimal changes.

## Step 5: Verify
Run reproduction from Step 1. Search for similar patterns.

## Step 6: Record
Run `/learn` to capture the bug pattern.
