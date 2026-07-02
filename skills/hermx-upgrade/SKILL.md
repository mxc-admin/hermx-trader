---
name: hermx-upgrade
description: Use when the operator wants to upgrade the HermX host to the latest code — pull, install deps, rebuild the dashboard UI, run tests, and restart services. Mutating (deploy + process lifecycle, not trading). Wraps `bash deploy/deploy.sh`, which has built-in auto-rollback on health-check failure. Always previews a dry-run (current git HEAD + what the deploy will do) and requires explicit "yes" before running. Passes through `--no-pull`, `--no-tests`, `--no-ui`. Never edits strategy files, never places or relays an order, never routes via `/webhook` or `/api/close`.
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. Required only when HERMX_DASH_AUTH=true, for the post-upgrade /api read."
    required_for: "Authenticated dashboard /api read after upgrade"
metadata:
  hermes:
    tags: [trading, hermx, upgrade, deploy, lifecycle, operations, mutating]
    related_skills: [hermx-restart, hermx-status, hermx-control, emergency-stop, hermx-positions]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX webhook receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
      - key: hermx.deploy_script
        description: "Config-safe deploy script (pull, deps, UI, tests, restart, auto-rollback)"
        default: "deploy/deploy.sh"
---

# /upgrade — upgrade the HermX host to the latest code

**Mutating (deploy + process lifecycle).** `/upgrade` or `/upgrade --no-pull` or
`/upgrade --no-tests` or `/upgrade --no-ui`

Runs `bash deploy/deploy.sh` to pull the latest code, install dependencies, build
the dashboard UI, run the offline test suite, and restart the services. The deploy
script snapshots operator config, captures a rollback point (`START_SHA`), and —
if the post-restart health check fails — **automatically rolls back** to that point
(hard reset + config restore + reinstall + rebuild + restart + re-probe). This is a
deploy/process-lifecycle action only — it never edits `strategies/*.json`, never
calls `/webhook` or `/api/close`, and never places, relays, or sizes an order.
Endpoint shapes and the UNKNOWN-never-flat rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- The operator wants to bring the host up to the latest committed code.
- A hotfix landed and needs deploying (`--no-tests` for a fast path, at your risk).
- The UI or deps changed and the running host is stale.
- **Not** for arming/mode changes (→ `/strategy-mode`, `/emergency-stop`), a live
  kill (→ `/emergency-stop kill`), or a plain bounce of an already-current host
  (→ `/restart`).

## Syntax
| Form                 | Behaviour                                                         |
|----------------------|------------------------------------------------------------------|
| `/upgrade`           | Full deploy: pull + deps + UI build + tests + restart + health check (auto-rollback on failure). |
| `/upgrade --no-pull` | Skip `git fetch/pull` (pip install **still** runs — deps live in `.venv/`, outside git). |
| `/upgrade --no-tests`| Skip pytest (hotfix path only).                                  |
| `/upgrade --no-ui`   | Skip the React/dashboard-UI build (UI unchanged).                |

Flags combine; pass through exactly what the operator supplied.

## Rules
- **Never deploy without an explicit `yes`.** Always preview first (dry-run) and
  stop for confirmation before invoking `deploy/deploy.sh`.
- **Warn about live trading.** Services **restart** during a deploy, so **open
  positions may be briefly unmonitored** and fills arriving mid-restart are not
  acted on until both services are back. If `/health` shows `arm.armed == true`,
  say so in the preview.
- **Webhooks are fire-and-forget.** TradingView alerts sent during the restart
  window (~5–10s, up to ~20s on rollback) will be **lost** — they are not queued.
  If the host is live, consider pausing TradingView alerts briefly, or accept the
  window as a known gap.
- **Pass flags through verbatim.** Only forward `--no-pull` / `--no-tests` /
  `--no-ui` when the operator supplied them; never add or drop flags silently.
- **UNKNOWN, never assumed-up.** A failed/timed-out `/health` read is
  **DOWN/UNKNOWN**, never "up". After deploy, an unverified endpoint is
  **UNKNOWN**, never "upgraded OK".
- **On failure, point to the backup, do not improvise.** If `deploy.sh` exits
  non-zero or health stays down, report failure and point the operator at the
  rollback backup under `.deploy-backups/` (operator config + a copy of the data
  dir). Do not hand-edit git state, config, or the venv in this path.
- Never edit strategy files, never `POST /webhook` or `/api/close` in this path.

## Procedure

### 1. Health + arm snapshot (read-only, first)
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
PY
```

### 2. Preview the deploy (dry-run, no action)
Show current git HEAD and exactly what the deploy will do — do not act yet:
```bash
rtk git log -1 --oneline HEAD
rtk git status --porcelain --branch | head -20
```
State in the preview:
- **Current HEAD** (`git log -1 --oneline`) — where the host is now.
- **What the deploy will do:** pull latest (unless `--no-pull`; pip install always
  runs), build the dashboard UI (unless `--no-ui`), run the offline tests (unless
  `--no-tests`), restart `hermx-dashboard` + `hermx-receiver`, health-check, and
  **auto-rollback to the current HEAD if the health check fails**.
- **Restart warning:** services will restart; if `armed == true`, open positions
  may be **briefly unmonitored** until both are back.
- **Estimated downtime: ~5–10s (up to ~20s on rollback). Webhooks arriving
  mid-restart are lost.**
- The exact command that will run, including any pass-through flags.

Then ask:
- `Run deploy/deploy.sh <flags> now? Services will restart. Type 'yes' to proceed.`

### 3. Deploy (only after the explicit `yes`)
```bash
# Pass through ONLY the flags the operator supplied.
rtk bash deploy/deploy.sh                 # full deploy
# e.g. rtk bash deploy/deploy.sh --no-tests
# e.g. rtk bash deploy/deploy.sh --no-pull --no-ui
```
`deploy.sh` handles snapshot → pull → pip install → UI build → tests → restart →
health check, and rolls back automatically if the health check fails.

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
print("result:", "up" if (d and r) else "STILL DOWN / UNKNOWN — investigate")
PY
```
- `deploy.sh` exit 0 **and** both `/health` reachable within 30s → **upgraded, up**.
  Confirm the new HEAD with `rtk git log -1 --oneline HEAD`.
- `deploy.sh` non-zero, **or** health stays down → **FAILED**. Do **not** report
  "upgraded OK". If `deploy.sh` auto-rolled back, the host should be on the prior
  HEAD; verify. Point the operator at the run's backup under `.deploy-backups/`
  (timestamped: operator config restored on rollback + a copy of the data dir) and
  the service logs (`journalctl -u hermx-dashboard -u hermx-receiver`). Re-check
  `arm` state once back.

## Outcomes
- `upgraded, up` — `deploy.sh` exit 0 and both `/health` reachable; note the new HEAD.
- `rolled back` — deploy failed its health check and `deploy.sh` reset to the prior
  HEAD; the host is on the old code, health should be back up — verify.
- `FAILED / UNKNOWN` — non-zero exit or health did not recover; state indeterminate,
  never "upgraded OK". Point at `.deploy-backups/` and the logs.

## Required log (every upgrade)
Record: time, operator, flags supplied, HEAD before, HEAD after, armed state at
upgrade, whether `deploy.sh` rolled back, the backup dir under `.deploy-backups/`,
and the final poll result (up / rolled back / failed / UNKNOWN).

## Verification checklist
- [ ] Health + arm snapshot runs first (read-only).
- [ ] Dry-run preview shows current HEAD and what the deploy will do (pull/tests/UI/restart + auto-rollback) before any action.
- [ ] Preview warns services restart and, when `arm.armed == true`, that open positions may be briefly unmonitored.
- [ ] No `deploy/deploy.sh` invocation before the explicit `yes`.
- [ ] Only the operator-supplied `--no-pull` / `--no-tests` / `--no-ui` flags are passed through — none added or dropped.
- [ ] Post-deploy poll runs ≤30s; non-zero exit or timeout → `FAILED`/`UNKNOWN`, never "upgraded OK".
- [ ] On failure, the report points to `.deploy-backups/` and the service logs; no hand-editing of git/config/venv.
- [ ] No `strategies/*.json` edit, no `/webhook`, no `/api/close`, no order size mentioned.
