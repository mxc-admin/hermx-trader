---
name: hx-positions
description: "Use when the operator asks what positions are open on HermX — open size, side, entry, mark, unrealized PnL. Read-only. Reads the local dashboard /api over loopback, aggregating every active (venue, mode) env from exch_live_by_env (legacy fallback: okx_live.positions). On any read failure or a stale/degraded executor it reports UNKNOWN — never 'flat'. Never places or closes an order."
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
    related_skills: [hermx-control, hx-status, hx-strategy-list, hx-trace]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
---

# /hx-positions — HermX open positions

Read-only. Reads `GET {dashboard}/api` and renders a table. Positions aggregate
every active `(venue, mode)` env from `exch_live_by_env` (an open position on any
env wins over flat); older dashboards without that map fall back to
`okx_live.positions`.

> Note (Positions-First): `/api` also carries a `positions` block — `open`
> (venue truth enriched with ledger open time / strategy), `closed`
> (ledger-derived round trips), and `drift` (observe-only ledger-vs-venue
> mismatch rows). If `positions.drift.count > 0`, surface it alongside the
> table — drift means the venue view alone may not tell the whole story.

Endpoint shapes, auth, freshness, and the **UNKNOWN-never-flat** rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "what's open?", "are we in BTC?", "what's our unrealized PnL?"
- Do NOT use to close a position (Phase-2 mutating command).

## The one rule that matters: UNKNOWN, never "flat"
A read failure, any active env snapshot with `ok == false` (legacy:
`okx_live.ok == false`), `executor.degraded == true`, or `freshness.no_data`
must be reported as **UNKNOWN**. Only when **every** active env reads healthy and
non-degraded may an empty positions map be reported as genuinely **FLAT**. Never let a failed read look like
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
- [ ] Executor-down (any active env snapshot `ok == false`) → output says **UNKNOWN**, not flat.
- [ ] Stale executor (`executor.degraded`/`stale`) → **UNKNOWN** + STALE freshness.
- [ ] Healthy + empty positions → **FLAT (no open positions)**.
- [ ] Healthy + open positions → table with side/size/UPL populated.
- [ ] No POST issued; no close attempted; no size mentioned.
