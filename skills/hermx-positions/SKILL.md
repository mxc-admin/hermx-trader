---
name: hermx-positions
description: Use when the operator asks what positions are open on HermX — open size, side, entry, mark, unrealized PnL. Read-only. Reads the local dashboard /api okx_live.positions over loopback. On any read failure or a stale/degraded executor it reports UNKNOWN — never "flat". Never places or closes an order.
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. Required only when HERMX_DASH_AUTH=true."
    required_for: "Authenticated dashboard /api read"
metadata:
  hermes:
    tags: [trading, hermx, positions, read-only, operations]
    related_skills: [hermx-control, hermx-status, hermx-strategy-list, hermx-trace]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
---

# /positions — HermX open positions

Read-only. Reads `GET {dashboard}/api` → `okx_live.positions` and renders a table.

Endpoint shapes, auth, freshness, and the **UNKNOWN-never-flat** rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "what's open?", "are we in BTC?", "what's our unrealized PnL?"
- Do NOT use to close a position (Phase-2 mutating command).

## The one rule that matters: UNKNOWN, never "flat"
A read failure, `okx_live.ok == false`, `executor.degraded == true`, or `freshness.no_data`
must be reported as **UNKNOWN**. Only a **healthy, non-degraded** executor read may
report an empty positions map as genuinely **FLAT**. Never let a failed read look like
"$0 / no positions".

## Procedure
```bash
rtk python3 - <<'PY'
import sys; sys.path.insert(0, "skills/hermx-ops/lib")
import os, hermx_ops as h
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
print("freshness:", st["freshness"], "| status:", st["positions_status"])
print(h.format_positions(st["positions"]))
PY
```
- `format_positions(UNKNOWN)` → an explicit UNKNOWN line.
- Healthy + no open positions → `FLAT (no open positions)`.
- Otherwise → aligned table: SYMBOL, SIDE, POS, AVG_PX, MARK, UPL, LEV, MGN.

## Reporting
- Lead with the freshness/status flag so a stale table is never mistaken for live.
- All positions are OKX demo/paper unless the operator has confirmed live — do not
  imply live capital.
- Never compute or suggest a size; sizing is owned by the Python execution layer.

## Verification checklist
- [ ] Executor-down (`okx_live.ok == false`) → output says **UNKNOWN**, not flat.
- [ ] Stale executor (`executor.degraded`/`stale`) → **UNKNOWN** + STALE freshness.
- [ ] Healthy + empty positions → **FLAT (no open positions)**.
- [ ] Healthy + open positions → table with side/size/UPL populated.
- [ ] No POST issued; no close attempted; no size mentioned.
