---
name: hermx-restart
description: "Use when the operator wants to restart the HermX services because the dashboard or receiver is down or unresponsive. Mutating (process lifecycle, not trading). First health-checks dashboard /health and receiver /health over loopback; if both are UP it reports 'up' and does nothing. If one or both are down it previews a restart plan and requires explicit 'yes' before restarting via systemd (preferred) or a start script (fallback). `/hx-restart force` restarts both regardless of state with a stronger prompt. Never edits strategy files, never places or relays an order."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. Required only when HERMX_DASH_AUTH=true, for the post-restart /api read."
    required_for: "Authenticated dashboard /api read after restart"
metadata:
  hermes:
    tags: [trading, hermx, restart, lifecycle, operations, mutating]
    related_skills: [hermx-status, hermx-control, emergency-stop, hermx-positions]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX webhook receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
      - key: hermx.systemd_units
        description: "systemd units for dashboard + receiver"
        default: "hermx-dashboard hermx-receiver"
---

# /hx-restart — restart the HermX dashboard / receiver

**Mutating (process lifecycle).** `/hx-restart` or `/hx-restart force`

Restarts the HermX **dashboard** (`{dashboard}/health`, default `http://127.0.0.1:8098`)
and/or **receiver** (`{receiver}/health`, default `http://127.0.0.1:8891`) when one is
down or unresponsive. This is a process-lifecycle action only — it never edits
`strategies/*.json`, never calls `/webhook`, and never places, relays, or sizes an
order. Endpoint shapes and the UNKNOWN-never-flat rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- The dashboard or receiver is unreachable / hung and needs a bounce.
- After a deploy, systemd `Restart=always` didn't bring a unit back cleanly.
- **Not** for arming/mode changes (→ `/hx-strategy-mode`, `/hx-emergency-stop`) or for a
  live kill (→ `/hx-emergency-stop kill`).

## Syntax
| Form              | Behaviour                                                              |
|-------------------|-----------------------------------------------------------------------|
| `/hx-restart`        | Health-check first. Both UP → report `up`, **do nothing**. One/both DOWN → preview plan, require `yes`, restart only the affected services. |
| `/hx-restart force`  | Restart **both** services regardless of current state, with a stronger confirmation prompt. |

## Rules
- **Never restart without an explicit `yes`.** Both forms preview first and stop for
  confirmation. `force` requires typing `yes, restart both`.
- **`/hx-restart` with both services UP does nothing** — it reports `up` and exits. Use
  `force` to bounce a healthy system.
- **Warn about live trading.** If `/health` shows `arm.armed == true` (live), warn the
  operator that **open positions may be briefly unmonitored during the restart** and
  that fills arriving mid-restart are not acted on until both services are back.
- **UNKNOWN, never assumed-up.** A failed/timed-out `/health` read is **DOWN/UNKNOWN**,
  never "up". After restart, an unverified endpoint is **UNKNOWN**, never "restarted OK".
- Never edit strategy files, never `POST /webhook` or `/api/close` in this path.

## Procedure

### 1. Health check (always first, read-only)
```bash
rtk python3 - <<'PY'
import sys; sys.path.insert(0, "skills/hermx-ops/lib")
import os, hermx_ops as h
secret = os.environ.get("HERMX_SECRET")  # None unless HERMX_DASH_AUTH=true
st = h.read_state(secret=secret)
d = "UP" if st["reachable"]["dashboard"] else "DOWN"
r = "UP" if st["reachable"]["receiver"] else "DOWN"
print("dashboard:", d)
print("receiver :", r)
print("armed    :", st["armed"], "(LIVE — positions briefly unmonitored on restart)" if st["armed"] is True else "")
print("both_up  :", st["reachable"]["dashboard"] and st["reachable"]["receiver"])
PY
```
- Both `UP` and **not** `force` → report `up`, **stop here** (no restart).
- One/both `DOWN`, or `force` → continue to the plan.

### 2. Preview the restart plan (dry-run, no action)
State exactly what will happen — do not act yet:
- Which service(s) are DOWN (or "both, forced").
- Which mechanism will be used (resolve in this order):
  1. **systemd (preferred):** `systemctl restart hermx-dashboard hermx-receiver`
     — restart only the affected unit(s) for `/hx-restart`; both for `force`.
     Verify units exist first: `systemctl list-unit-files 'hermx-*'`.
  2. **Start script (fallback), if no systemd units:** `run.sh` at repo root.
     Relevant flags:
     - Default `bash run.sh` — validates + runs the pytest suite + starts both
       services in the **foreground**. Not ideal for a restart (blocks, and the
       tests delay recovery).
     - `--skip-tests` — skips pytest (faster recovery).
     - `--honor-submit` — does **not** force `HERMX_LIVE_TRADING=false`.
     By default `run.sh` forces `HERMX_LIVE_TRADING=false` — it is a foreground
     dev/smoke runner, **not** a live relaunch. For a restart fallback, recommend
     `bash run.sh --skip-tests`. **If the host was live**, the operator should
     decide whether to add `--honor-submit` or to re-arm afterward — otherwise the
     service comes back in dry-run. Say so in the preview; do not silently drop a
     live host into dry-run.
  3. **Docker deploy:** `docker compose restart hermx-dashboard hermx-receiver`
     (document only; use if the host runs the compose stack).
- If `armed == true`: include the live-trading warning from **Rules**.

Then ask:
- `/hx-restart` → `Restart <service(s)>? Type 'yes' to proceed.`
- `/hx-restart force` → `Force-restart BOTH dashboard and receiver now? Type 'yes, restart both' to proceed.`

### 3. Restart (only after the exact confirmation)
Preferred (systemd) — restart the affected units:
```bash
# /hx-restart: pass only the down unit(s); force: pass both
rtk sudo systemctl restart hermx-dashboard hermx-receiver
```
Fallback (no systemd units present) — `run.sh` at repo root:
```bash
# Local/dev recovery. Forces HERMX_LIVE_TRADING=false unless --honor-submit;
# NOT a live relaunch. --skip-tests speeds recovery by skipping pytest.
rtk bash run.sh --skip-tests
# If the host was LIVE, add --honor-submit (or re-arm afterward) so it does not
# come back in dry-run:
#   rtk bash run.sh --skip-tests --honor-submit
```
Docker deploy (document; use when the compose stack owns the services):
```bash
rtk docker compose restart hermx-dashboard hermx-receiver
```

### 4. Poll health for up to 30s and report
```bash
rtk python3 - <<'PY'
import sys, time; sys.path.insert(0, "skills/hermx-ops/lib")
import os, hermx_ops as h
secret = os.environ.get("HERMX_SECRET")
deadline = 30.0; step = 2.0; waited = 0.0
d = r = False
while waited <= deadline:
    st = h.read_state(secret=secret)
    d = st["reachable"]["dashboard"]; r = st["reachable"]["receiver"]
    if d and r:
        break
    time.sleep(step); waited += step
def label(ok): return "up" if ok else "STILL DOWN"
print(f"after {int(waited)}s -> dashboard: {label(d)} | receiver: {label(r)}")
print("result:", "up" if (d and r) else "STILL DOWN / UNKNOWN — investigate logs")
PY
```
- Both reachable within 30s → **up**.
- Timed out → **STILL DOWN / UNKNOWN** — do **not** report "restarted OK". Point the
  operator at unit/service logs (`journalctl -u hermx-dashboard -u hermx-receiver`, or
  the `run.sh`/compose console) and re-check `arm` state once back.

## Outcomes
- `up` — both `/health` reachable; if the system was live, note positions are monitored again.
- `STILL DOWN` — one/both endpoints unreachable after 30s; restart did not recover the service.
- `UNKNOWN` — health reads failed/timed out; state indeterminate, never "restarted OK".

## Required log (every restart)
Record: time, operator, `/hx-restart` vs `force`, which service(s) were DOWN before,
mechanism used (systemd / script / compose), the confirmation string typed, armed
state at restart, and the final poll result (up / still down / UNKNOWN).

## Verification checklist
- [ ] Health check runs first; both UP + no `force` → reports `up` and issues **no** restart.
- [ ] One service DOWN → preview names the down service and the exact mechanism; waits for `yes`.
- [ ] No restart command runs before the explicit confirmation (`yes`, or `yes, restart both` for force).
- [ ] `force` restarts both even when both are UP, and uses the stronger prompt.
- [ ] When `arm.armed == true`, the preview warns open positions may be briefly unmonitored.
- [ ] systemd path preferred; falls back to `bash run.sh --skip-tests` only if no `hermx-*` units, then `docker compose restart`; flags that `run.sh` forces `HERMX_LIVE_TRADING=false` unless `--honor-submit` and prompts re-arm on a live host.
- [ ] Post-restart poll runs ≤30s; timeout → `STILL DOWN`/`UNKNOWN`, never "restarted OK".
- [ ] No `strategies/*.json` edit, no `/webhook`, no `/api/close`, no order size mentioned.
