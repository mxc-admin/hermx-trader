---
name: hx-strategy-mode
description: "Use when the operator wants to CHANGE a HermX strategy's execution mode — pause, resume, demo, or live. Mutating. Resolves a name/id/symbol to a strategy_id, previews current→target, and POSTs the override to the dashboard control endpoint (never edits strategies/*.json). Always dry-run first; live transitions require explicit 'yes'. On any read failure it reports UNKNOWN and refuses."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. Required when HERMX_DASH_AUTH=true; the control endpoint fails closed without it."
    required_for: "Authenticated dashboard /api/control/strategy write"
metadata:
  hermes:
    tags: [trading, hermx, strategies, mutating, control, operations]
    related_skills: [hermx-control, hx-strategy-list, hx-positions, hx-emergency-stop]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.strategies_dir
        description: "Directory of strategy files"
        default: "strategies"
      - key: hermx.control_state
        description: "Per-strategy mode/pause overrides"
        default: "control-state.json"
---

# /hx-strategy-mode — change a HermX strategy's execution mode

**Mutating.** `/hx-strategy-mode <name-or-id> <pause|resume|demo|live>`

Sets a per-strategy override via `POST {dashboard}/api/control/strategy/{id}`. This
writes `control-state.json` `strategy_overrides` **through the dashboard** — it NEVER
edits `strategies/*.json`. Endpoint shapes, auth, and `effective_mode` resolution live
in [`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## Modes → wire values
| arg      | meaning                                   | wire `mode` |
|----------|-------------------------------------------|-------------|
| `pause`  | validate + ledger, submit nothing         | `pause`     |
| `resume` | clear the override, restore the file mode | `clear`     |
| `demo`   | submit to the exchange **sandbox**        | `demo`      |
| `live`   | submit to the **real account** (money)    | `live`      |

`resume` maps to `clear` (removes the override; the strategy reverts to its file's
`execution_mode`). `post_strategy_mode()` does this mapping.

## Rules
- **Always dry-run first.** Show resolved id, current effective mode, target mode.
- **`live` requires explicit `yes`.** No implicit live transitions.
- **After the POST, re-read** `/api`/`/health` to confirm the override applied.
- **Never edit `strategies/*.json`** in this path — only the control override.
- Ambiguous/unresolved arg → stop and show candidates; never guess.

## Procedure

### 1. Resolve + preview (dry-run, no write)
```bash
rtk python3 - "$1" <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
arg, target = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None
res = h.resolve_strategy(arg, "strategies")
if not res["resolved"]:
    print("UNRESOLVED:", res["reason"], "| candidates:", res["candidates"]); sys.exit(1)
sid = res["resolved"]
rows = {r["id"]: r for r in h.list_strategies("strategies", str(h.CONTROL_STATE_PATH))}
cur = rows.get(sid, {}).get("effective_mode", h.UNKNOWN)
print(f"resolved: {sid} (via {res['match']})")
print(f"current effective mode: {cur}")
print(f"target mode: {target}  (wire: {h._MODE_WIRE.get(target, target)})")
if target == "live":
    print("!! LIVE = REAL MONEY. Requires explicit 'yes' to proceed.")
PY
```
Pass the target as the second arg (`"$1" "$2"` in the wrapper).

### 2. Apply (only after confirmation; `yes` required for `live`)
```bash
rtk python3 - "$1" "$2" <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
arg, target = sys.argv[1], sys.argv[2]
sid = h.resolve_strategy(arg, "strategies")["resolved"]
secret = os.environ.get("HERMX_SECRET")
r = h.post_strategy_mode(h.DASHBOARD_BASE, secret, sid, target)
print("outcome:", r["outcome"], "| reason:", r["reason"], "| http:", r["http"])
# Re-read to confirm the override applied.
rows = {x["id"]: x for x in h.list_strategies("strategies", str(h.CONTROL_STATE_PATH))}
print("confirmed effective mode:", rows.get(sid, {}).get("effective_mode", h.UNKNOWN))
PY
```

## Outcomes
- `applied` — override written; the confirm re-read shows the new effective mode.
- `rejected` — server said no. `http_401` → HERMX_SECRET missing/wrong; `http_404` →
  unknown id; `http_400` → invalid mode. Report the reason; nothing changed.
- `UNKNOWN` — transport failure or 5xx. Do **not** claim the mode changed; re-read
  `/api` before retrying.

## Required log (every mutation)
Record: time, operator, reason, `strategy_id` affected, mode before → after, and the
confirm re-read result. `live` transitions additionally log the explicit `yes`.

## Reporting
- Lead with resolved id + current→target so the operator sees exactly what changes.
- Never restate `budget_usd`/`leverage` as an order size — sizing is owned by the
  execution layer.
- A `rejected`/`UNKNOWN` outcome must never be reported as "done".

## Verification checklist
- [ ] Dry-run prints resolved id, current effective mode, target mode — no write yet.
- [ ] Ambiguous symbol (maps to >1 strategy) → stops with candidates, no POST.
- [ ] `resume` sends wire `clear` and the re-read reverts to the file's `execution_mode`.
- [ ] `live` refuses without an explicit `yes`.
- [ ] 401 (missing/wrong `HERMX_SECRET`) → `rejected`/unauthorized, not "applied".
- [ ] 404 (unknown id) and 400 (invalid mode) → `rejected` with the server reason.
- [ ] Transport failure / 5xx → `UNKNOWN`; the skill re-reads before asserting state.
- [ ] `strategies/*.json` is byte-for-byte unchanged (only the override moved).
