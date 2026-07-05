---
name: hx-troubleshoot
description: "Use when an order is stuck in a confusing state (e.g. UNKNOWN that never resolves, or a Hermes reconcile-gate alert pointing here). Investigates ALL open orders in one pass, classifies each against known corruption/ambiguity patterns, and offers a one-confirm fix ONLY for the one provably-safe pattern (a terminal state illegally overwritten in the journal's own history). Never offers a write for a genuinely ambiguous order -- those require manual investigation per HermX's not-found-is-not-rejected invariant."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard/receiver shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. The admin endpoints fail closed without a matching token."
    required_for: "Authenticated order-troubleshoot read + apply-action write"
metadata:
  hermes:
    tags: [trading, hermx, orders, diagnostics, mutating, operations]
    related_skills: [hx-trace, hermx-control, hx-close]
    config:
      - key: hermx.receiver_base
        description: "HermX webhook receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
        prompt: "HermX webhook receiver base URL"
---

# /hx-troubleshoot — investigate and (safely) fix confusing open orders

**Investigation is read-only. Any fix is mutating, single-order, and requires explicit
confirmation.** Classification never issues a live venue call -- it reads only what the
order journal / resolver has already recorded, so it never races or duplicates the
periodic UNKNOWN resolver.

## When to use
- "why is this order stuck?", "what's wrong with my open orders?"
- A `hermx-reconcile-gate` wake alert includes a `stuck order <cl_ord_id>` condition
  with the hint `investigate via /hx-troubleshoot`.

## The one rule that matters: only ONE pattern is ever auto-fixable
Every open `UNKNOWN` order is classified into exactly one of:

| `issue_type` | Meaning | Action offered |
|---|---|---|
| `terminal_overwritten` | The journal's OWN history shows a terminal state (`FILLED`/`REJECTED`) was reached, then illegally overwritten by a later record — provable from the audit trail alone. | `restore_terminal` — one-confirm fix |
| `ambiguous_unknown` | Never confirmed terminal by the venue. Genuinely ambiguous — a real fill could be hiding behind a "not found". | **None.** Report only; manual investigation required (check OKX order history + current position/balance) before any write. |
| `evidence_incomplete` | Older journal records were pruned by segment retention — a hidden terminal state cannot be ruled out. | **None.** Report only. |

`ambiguous_unknown` and `evidence_incomplete` are **never** auto-actionable by this
skill, by design — matching the resolver's own not-found-is-not-rejected invariant. If
the operator wants a manual override for one of these, that is a separate, explicitly
manual, off-skill runbook action — not something this tool offers.

## Procedure

### 1. Investigate (read-only, no args)
```bash
rtk python3 - <<'PY'
import os, sys; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
report = h.get_order_troubleshoot_report(h.RECEIVER_BASE, os.environ.get("HERMX_SECRET"))
if report == h.UNKNOWN:
    print("UNKNOWN -- could not read the troubleshoot report (transport/auth failure). Not safe to proceed.")
    sys.exit(1)
if not report:
    print("No confusing open orders found.")
    sys.exit(0)
for row in report:
    print(f"{row['cl_ord_id']}  {row['issue_type']}  {row['evidence']}")
    if row["actions"]:
        for a in row["actions"]:
            print(f"    action available: {a['id']} -- {a['label']}")
    else:
        print("    report only -- no action offered; manual investigation required")
PY
```
- A failed/UNKNOWN read means **stop** — do not assume "no issues".
- Print every row, grouped by whether it has an available action or is report-only.

### 2. For a report-only row (`ambiguous_unknown` / `evidence_incomplete`)
Do **not** attempt a fix. Tell the operator: check the OKX order history UI for this
`cl_ord_id` and the current live position/balance for the symbol, then decide manually
— this skill will not write a terminal state without venue confirmation.

### 3. For a row with `restore_terminal` available
1. Show the operator the exact proposed transition (`evidence.terminal_state`) and ask
   **"Confirm? [yes]"**. Never proceed without an explicit yes.
2. Only after "yes":
```bash
rtk python3 - "$CL_ORD_ID" <<'PY'
import os, sys; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
cl = sys.argv[1]
r = h.post_apply_order_action(h.RECEIVER_BASE, os.environ.get("HERMX_SECRET"),
                               cl, "restore_terminal", operator="operator", reason="hx-troubleshoot fix")
print("outcome:", r["outcome"], "| from:", r["from_state"], "| to:", r["to_state"], "| reason:", r["reason"])
PY
```
3. The server independently RE-DERIVES eligibility from the journal itself before
   writing anything — a stale or already-resolved row will come back `refused`, not an
   error. Report the outcome verbatim.

## Required log (every mutation)
Record: time, operator, reason, `cl_ord_id`, `from_state`/`to_state` (on `healed`) or the
refusal reason (on `refused`) — same discipline as `hx-close`.

## Outcomes
- `healed` — the journal's terminal state was restored; report `from_state`/`to_state`.
- `refused` — the server did not find this action currently eligible (stale row, race
  lost, or an unrecognized `action_id`). Nothing was written. Re-run step 1 to see
  current state before retrying.
- `UNKNOWN` — transport failure. Do **not** assume anything was or wasn't written;
  re-read the troubleshoot report before any retry.

## Common Pitfalls
1. Offering or attempting a write for `ambiguous_unknown`/`evidence_incomplete` — never
   do this; there is no action for these by design.
2. Treating a `refused` outcome as an error — it's the server correctly declining an
   action it re-verified is no longer (or was never) eligible.
3. Skipping the explicit "yes" confirmation before a `restore_terminal` call.
4. Assuming a failed/UNKNOWN report read means "no issues" — it means "could not check".

## Verification checklist
- [ ] Investigation step never writes anything, regardless of what it finds.
- [ ] `ambiguous_unknown` / `evidence_incomplete` rows are never offered a write action.
- [ ] `restore_terminal` is only invoked after an explicit operator "yes".
- [ ] A `refused` outcome is reported as a safe non-event, not an error.
- [ ] A transport/UNKNOWN read never proceeds to a write.
- [ ] Every mutation is logged with operator, reason, cl_ord_id, and the resulting state.
