---
name: hermx-status
description: "Use when the operator asks about HermX system status — is it armed, what mode, is the dashboard/receiver up, when was the last alert, how many strategies. Read-only. Calls the local dashboard /health and /api plus the receiver /health and /latest over loopback. Never relays a signal, never places an order."
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
    tags: [trading, hermx, status, read-only, operations]
    related_skills: [hermx-control, hermx-positions, hermx-strategy-list, hermx-trace]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX webhook receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
---

# /hx-status — HermX system status

Read-only posture snapshot. Answers: **armed?**, **mode**, **reachability**,
**last alert**, **strategy count**. Never submits or relays.

Endpoint shapes, auth, and the UNKNOWN-never-flat rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "is HermX up / armed / live?"
- "what mode are we in?"
- "when was the last alert?"
- Quick health check before any other HermX action.

## Reads (loopback, no auth on default host)

| Field           | Source                                    |
|-----------------|-------------------------------------------|
| armed / mode / kill switch | `GET {dashboard}/health` → `arm`, `mode` |
| executor / freshness       | `GET {dashboard}/api` → `executor`, `freshness` |
| dashboard reachable        | `GET {dashboard}/health` `ok`            |
| receiver reachable         | `GET {receiver}/health` `ok`             |
| last alert                 | `GET {receiver}/latest`                  |
| strategy count             | `GET {dashboard}/health` `strategy_files` len |

## Procedure
1. Run the helper (it applies UNKNOWN-never-flat and freshness derivation):

   ```bash
   rtk python3 - <<'PY'
   import sys; sys.path.insert(0, "skills/hermx-ops/lib")
   import os, hermx_ops as h
   secret = os.environ.get("HERMX_SECRET")  # None unless HERMX_DASH_AUTH=true
   st = h.read_state(secret=secret)
   print("dashboard:", "UP" if st["reachable"]["dashboard"] else "DOWN")
   print("receiver :", "UP" if st["reachable"]["receiver"] else "DOWN")
   print("armed    :", st["armed"])
   print("mode     :", st["mode"])
   print("kill_sw  :", st["kill_switch_engaged"])
   print("freshness:", st["freshness"])
   print("strategies:", st["strategy_count"])
   PY
   ```

2. Fetch the last processed alert from the receiver:

   ```bash
   rtk curl -fsS http://127.0.0.1:8891/latest
   ```
   - `404 no_latest_yet` → "no alert processed yet" (not an error).
   - `503 latest_unreadable` → report **UNKNOWN** (corrupt), not "no alerts".

3. Report a terse summary: armed?, mode, dashboard/receiver up/down, last alert
   time (`received_at`/`tv_time` from `/latest`), strategy count, freshness OK/STALE.

## Rules
- A dashboard/receiver read failure ⇒ report that surface as **DOWN/UNKNOWN**; never
  infer "flat" or "not armed" from a failed read. `armed` is only `false` when
  `/health` returned `ok` with `arm.armed == false`.
- Freshness is bounded on bar time (`tv_time`), not server time — a current clock
  after an outage does not mean fresh data.
- This skill never calls `/webhook` or `/api/close`.

## Verification checklist
- [ ] `read_state` returns without traceback; dashboard + receiver reachability shown.
- [ ] `armed` reflects `/health` `arm.armed` (demo-only ⇒ `false`, not armed).
- [ ] A stale/degraded executor surfaces `freshness: STALE`, not `OK`.
- [ ] `/latest` `503` reported as UNKNOWN, `404` reported as "no alert yet".
- [ ] No POST issued; no order relayed; no size mentioned.
