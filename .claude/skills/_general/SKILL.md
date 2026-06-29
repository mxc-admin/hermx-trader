---
name: general-knowledge
description: General project knowledge base. Fallback target for /learn when no specific skill matches. Stores cross-cutting architecture decisions and rejected approaches.
allowed-tools: "Read, Grep, Glob"
model: sonnet
---

# General Knowledge

Fallback skill for project-wide learnings.

## Workflow

### Step 1: Check existing knowledge
Read [references/rejected-approaches.md](references/rejected-approaches.md) and [references/architecture-decisions.md](references/architecture-decisions.md).

### Step 2: Answer the question
Use reference files to provide context-aware answers about project decisions.

### Step 3: Report
```
## Knowledge Query: {question}
### Answer: [based on reference files]
### Sources: [file:line]
```
