# HermX Monitoring via Hermes Built-in Cron — Technical Design

**Status:** DESIGN (design only — no implementation code in this pass)
**Supersedes:** `MONITOR_DAEMON_SPEC.md` (removed — the custom `src/monitor_daemon.py` design)
**Author:** research + design pass, 2026-07-02
**Audience:** the developer who will wire up the monitoring jobs in a follow-up session.

---

## 0. Summary

The custom Monitor Daemon (`src/monitor_daemon.py`, spec'd in `MONITOR_DAEMON_SPEC.md`) was
designed to add a scheduler, a deduplicator, a subprocess seam to `hermes -z`, operator
delivery, atomic state, a systemd unit, and a compose service — **all of which the Hermes
gateway already provides natively.** The Hermes gateway daemon (`hermes gateway`) is already
running on this host (`~/.hermes/gateway_state.json` → `pid 53312`, Telegram connected) and
already **ticks a cron scheduler every 60 seconds** (`InProcessCronScheduler`,
`cron/scheduler_provider.py`).

**We are not building a daemon. We are expressing each monitoring job as a Hermes cron job**
plus a small number of HermX-owned helper scripts and skill registrations. The proven *logic*
from `MONITOR_DAEMON_SPEC.md` (fingerprints, suppression windows, the MXC gate, skill mapping)
carries over verbatim — it just lives in ~20-line pre-check scripts and job specs instead of a
600-line daemon.

Design posture is unchanged and, in fact, better satisfied by the gateway:

- **Fail-open, never wedge the money path.** Cron jobs run in the gateway process, a *separate*
  process from the receiver/dashboard. A job crash, hang, or bad LLM reply cannot back-pressure
  onto the money path. The gateway's per-job fresh session + tick lock guarantee isolation.
- **Restarts are routine.** The gateway is already a managed service; `jobs.json` uses atomic
  writes and a tick lock (`~/.hermes/cron/.tick.lock`), so restarts don't double-fire or storm.
- **stdlib-first bridges.** The only HermX-side code we add is a handful of stdlib pre-check
  scripts that reuse `skills/hermx-ops/lib/hermx_ops.py`. No new long-running process, no new
  systemd unit, no new compose service.

---

## 1. Why we're pivoting

| Concern | Custom daemon (`MONITOR_DAEMON_SPEC.md`) | Hermes built-in cron |
|---|---|---|
| Scheduler loop | hand-written `while True: tick(); sleep(interval±jitter)` | gateway ticks every 60s (`InProcessCronScheduler`), 4 schedule formats (delay / `every Nh` / cron expr / ISO) |
| Fresh agent per event | `subprocess.run(["hermes","-z",prompt,"--skills",…])` seam we maintain | native — each due job runs a fresh `AIAgent` session with skills injected |
| Skill injection | our `--skills` CSV builder | native `skills=[…]` per job |
| Dedup / "notify on change" | `last_notified_at` map + per-category windows + escalation in our code | `[SILENT]` (suppress delivery) + `{"wakeAgent": false}` pre-check gate + no-agent empty-stdout, all built in |
| Cron idempotency (weekly) | `cron.weekly_summary.last_period` bookkeeping in our state | native cron expr `0 9 * * 1` — the scheduler owns next-run computation |
| Operator delivery | forward stdout to `HERMX_ALERT_WEBHOOK_URL` ourselves | native `--deliver telegram` (+ 20 platforms), fan-out, topics |
| Atomic state / crash safety | copy `_atomic_json_dump`, manage `monitor-state.json` | native atomic `jobs.json` + `.tick.lock` cross-process lock |
| Single-instance | optional PID/lock file we add | native tick lock; gateway is the sole launcher |
| Deployment | new `deploy/hermx-monitor.service` + compose service + install/deploy edits | **none** — jobs live in the already-installed gateway |
| Provider cost safety | n/a | native fail-closed on global default change (#44585); credential-pool rotation + fallback providers |
| Recursion safety | n/a | native — cron sessions have the `cronjob` toolset disabled |

Everything in the left column is undifferentiated infrastructure we would own, test, and
operate. The right column is already shipping, already running on this host, and already
delivering to the operator's Telegram. Building the daemon would duplicate a battle-tested
subsystem. **Net deletion: the entire `src/monitor_daemon.py`, its tests, its systemd unit,
its compose service, and the proposed `/api/monitor/alerts` endpoint.**

---

## 2. What the built-in cron provides out of the box

Verified against the installed agent (`~/.hermes/hermes-agent/`) and its docs
(`website/docs/user-guide/features/cron.md`, `developer-guide/cron-internals.md`,
`guides/cron-script-only.md`).

### 2.1 Scheduling
- **Formats:** relative one-shot (`30m`, `2h`, `1d`), interval (`every 5m`, `every 2h`), 5-field
  cron expr (`0 9 * * 1`), ISO timestamp. Natural language in chat via the `cronjob` tool.
- **Tick:** gateway ticks the scheduler every 60s. Due = `next_run_at <= now AND state ==
  "scheduled"`. Overlap-safe via `~/.hermes/cron/.tick.lock` (`fcntl.flock`).
- **Repeat:** one-shot runs once; interval/cron run forever until removed; `repeat=N` caps it.

### 2.2 Execution
- Each due job runs a **fresh `AIAgent` session** — no history, self-contained prompt required.
- **Skill injection:** `skills=[…]` loaded in order, each `SKILL.md` injected as context, then
  the prompt appended as the task.
- **Pre-check script:** `script=<name>` in `~/.hermes/scripts/` runs *before* the agent; its
  stdout is injected as context. Default 120s timeout (`HERMES_CRON_SCRIPT_TIMEOUT` /
  `cron.script_timeout_seconds`).
- **`wakeAgent` gate:** a pre-check script whose last stdout line is `{"wakeAgent": false}`
  **skips the agent entirely for that tick** — a $0 way to poll frequently and only spend tokens
  when state actually changed. `{"wakeAgent": true, "context": {…}}` passes context through.
- **`--no-agent` mode:** pure script on a schedule, stdout delivered verbatim, **zero LLM**.
  Empty stdout → silent tick. Non-zero exit / timeout → error alert (a broken watchdog can't
  fail silently). Scripts must live in `~/.hermes/scripts/`; `.sh`/`.bash` → `/bin/bash`, else
  `sys.executable`.
- **`context_from=[…]`:** inject another job's most-recent output as context (pipeline chaining).
- **`enabled_toolsets=[…]`:** restrict the per-job toolset (cost control on the tool schema).
- **`workdir=<abs dir>`:** run the job with that cwd; injects `AGENTS.md`/`CLAUDE.md`/
  `.cursorrules` and points file/terminal/exec tools there. Workdir jobs run **sequentially**
  on the tick (deliberate — process-global cwd), workdir-less jobs still run in parallel.

### 2.3 Delivery & dedup
- `--deliver` → `origin` | `local` | `telegram[:chat[:thread]]` | `discord` | `slack` | … | `all`
  | comma-lists like `telegram,discord`. Final agent response auto-delivered (no `send_message`
  needed).
- **`[SILENT]`** anywhere in a *successful* agent response → delivery suppressed entirely (still
  saved to `~/.hermes/cron/output/{job_id}/` for audit). Failed jobs always deliver regardless.
- **No-agent empty stdout** → silent tick. Same "only speak when something's wrong" pattern.
- Response wrapping (`cron.wrap_response`), continuable deliveries (`cron.mirror_delivery` /
  `attach_to_session`), Telegram cron topic (`TELEGRAM_CRON_THREAD_ID`).

### 2.4 Resilience & safety
- Atomic `jobs.json` writes; cross-process tick lock.
- Provider recovery: `fallback_providers` + credential-pool rotation on 429s.
- **Fail-closed on global default change** (#44585): an unpinned job whose global default
  provider/model changed **skips the run and alerts** rather than silently spending on a new
  paid model. → **we pin `provider`/`model` on money-adjacent monitor jobs.**
- **Recursion guard:** cron sessions cannot create/mutate cron jobs.
- Prompt-injection / secret-exfil scanning at create/update time.

### 2.5 Management surface
- CLI: `hermes cron create|list|edit|pause|resume|run|remove|status|tick`.
- Chat: `/cron add|list|pause|resume|run|edit|remove`, or plain natural language.
- Agent tool: single `cronjob(action=…)` tool.
- Name-based lookup (case-insensitive) everywhere; exact ID wins; ambiguous names refused.
- Storage: `~/.hermes/cron/jobs.json`; output: `~/.hermes/cron/output/{job_id}/{ts}.md`.

---

## 3. New architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                 HermX host                                      │
│                                                                                 │
│  receiver (src/webhook_receiver.py :8891)   dashboard (src/dashboard.py :8098)  │
│    • /health /latest                          • /health (no auth) /api (token)  │
│    • writes logs/*.jsonl, control-state.json  • reads shared state              │
│                     │                                    │                       │
│                     └──────────── shared files ──────────┘                       │
│                              (control-state.json, logs/alerts.jsonl, …)          │
│                                        ▲     ▲                                    │
│                    loopback HTTP + file reads (read-only)                         │
│                                        │     │                                    │
│  ┌─────────────────────────────────────┼─────┼──────────────────────────────┐   │
│  │  hermes gateway (ALREADY RUNNING, pid 53312)                              │   │
│  │    • ticks cron scheduler every 60s (InProcessCronScheduler)              │   │
│  │    • Telegram connected  ──deliver──▶  operator                           │   │
│  │                                                                            │   │
│  │   on each due job:                                                         │   │
│  │     [optional] pre-check script  ~/.hermes/scripts/hermx-*.py             │   │
│  │        → reads HermX state via hermx_ops (loopback + files)               │   │
│  │        → computes fingerprint, compares sidecar state                     │   │
│  │        → {"wakeAgent": false}  (nothing new → $0 skip)                    │   │
│  │              │ else {"wakeAgent": true, "context": {...}}                 │   │
│  │              ▼                                                             │   │
│  │     fresh AIAgent session  (--workdir <hermx repo>)                       │   │
│  │        + skills: hermx-status / hermx-positions / hermx-trace / …         │   │
│  │        → assess, produce operator summary  (or "[SILENT]")                │   │
│  │              │                                                             │   │
│  │              ▼  --deliver telegram                                        │   │
│  └────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two facts define the whole bridge:

1. **HermX is the data source; Hermes is the scheduler + brain + delivery.** The gateway reads
   HermX state exactly the way the operator skills already do — loopback HTTP to `/health` and
   `/api`, plus tolerant file reads of `control-state.json` / `logs/alerts.jsonl` — through
   `skills/hermx-ops/lib/hermx_ops.py`. It **never** mutates HermX state.
2. **No HermX process is added or changed.** The receiver, dashboard, and money path are
   untouched. The only HermX-repo additions are read-only helper scripts and skill registrations.

---

## 4. Gaps the built-in cron does NOT cover, and the bridges

The gateway gives us scheduling, sessions, dedup primitives, and delivery. It does **not**
inherently know anything about HermX's dashboard, ledgers, or control state. Four concrete gaps,
each with a minimal bridge:

### 4.1 Gap: HermX skills are not discoverable by cron jobs

**Finding (verified):** only `hermx-control` is registered with Hermes, via a symlink
`~/.hermes/skills/trading/hermx-control → /Users/anatolizurablev/dev projects/hermx/skills/hermx-control`.
`skills.external_dirs: []` is empty in `~/.hermes/config.yaml`. The read-only skills we want for
monitoring — `hermx-status`, `hermx-positions`, `hermx-trace`, `signal-memory` — are **not**
registered, so `--skills hermx-status` fails to resolve today. (The advisor works only because
it uses `hermx-control`, which is registered.)

**Bridge (one-time deployment step):** register the read-only skills the same way `hermx-control`
is registered — symlink each into the Hermes skills hub. Either add the repo `skills/` dir to
`skills.external_dirs` in `~/.hermes/config.yaml`, **or** symlink individually:

```bash
HERMX_SKILLS="/Users/anatolizurablev/dev projects/hermx/skills"
HUB=~/.hermes/skills/trading
for s in hermx-status hermx-positions hermx-trace signal-memory; do
  ln -sfn "$HERMX_SKILLS/$s" "$HUB/$s"
done
hermes -z "ping" --skills hermx-status   # resolution smoke test
```

Do **not** register the mutating skills (`hermx-close`, `hermx-restart`, `hermx-strategy-mode`,
`hermx-upgrade`, `emergency-stop`) or the relay-capable `hermx-control` for monitoring jobs —
monitoring is strictly read-only (see §5.4). `hermx-ops` is a shared **lib**, not a loadable
skill (no `SKILL.md`); never pass it to `--skills`.

### 4.2 Gap: cron jobs run detached, but HermX skills use relative paths + env

**Finding (verified):** the skills' helper (`hermx_ops.py`) resolves on-disk state relative to
`HERMX_DATA_DIR` (default `"."`), and the `SKILL.md` procedures do
`sys.path.insert(0, "skills/hermx-ops/lib")` — a **cwd-relative** import. Cron jobs default to
running detached from any repo, with the gateway's cwd. So both the import and the default
data-dir read would fail unless cwd is the HermX repo.

**Bridge:** every HermX cron job sets **`--workdir "/Users/anatolizurablev/dev projects/hermx"`**
and the gateway process must have `HERMX_DATA_DIR` (and `HERMX_SECRET` only if
`HERMX_DASH_AUTH=true`) in its environment. On the default host the loopback dashboard has auth
off, so no secret is needed; the loopback base URLs are the defaults `hermx_ops` already uses.
(Trade-off: workdir jobs run sequentially on the tick — fine for ≤6 monitor jobs.)

Env to make available to the gateway (via `~/.hermes/.env` or the gateway's service env):

```
HERMX_DATA_DIR=/Users/anatolizurablev/dev projects/hermx
# HERMX_SECRET=…            # only if HERMX_DASH_AUTH=true
# HERMX_DASHBOARD_BASE / HERMX_RECEIVER_BASE default to loopback — leave unset on this host
```

### 4.3 Gap: "only deliver on change" dedup with suppression windows and escalation

The gateway gives us three suppression primitives (`wakeAgent:false`, no-agent empty stdout,
`[SILENT]`), but the *change-detection logic* — fingerprints, per-category windows, escalation
bypass — is HermX domain knowledge. That logic already exists, fully specified, in
`MONITOR_DAEMON_SPEC.md` §4.1–4.4. We move it into **pre-check gate scripts** in
`~/.hermes/scripts/`, one per monitoring concern, ≤30 lines each.

**Bridge:** a pre-check script that (a) reads HermX state via `hermx_ops`, (b) computes the same
fingerprints from `MONITOR_DAEMON_SPEC.md` §4.2, (c) compares against a **sidecar state file**
`~/.hermes/scripts/.hermx-<concern>.state` holding `{fingerprint: last_notified_epoch,
last_severity}`, (d) emits `{"wakeAgent": false}` when every current condition is inside its
suppression window and not escalated, else `{"wakeAgent": true, "context": {"alerts": […]}}`.
This is the daemon's `Deduplicator` (spec §4.4) reduced to a stdout gate. The suppression windows
(health 900s, reconcile 1800s, risk 3600s) and escalation bypass (info<warning<error<critical)
port over unchanged. See §7 for the script contract.

Note the sidecar state file is HermX-owned and lives in `~/.hermes/scripts/`, **not** in HermX's
`HERMX_DATA_DIR` — it is monitoring bookkeeping, not trading state, and must never be confused
with `control-state.json`. (Also: never point a pre-check gate at Hermes's own `state.db` — it's
an internal schema; the docs warn against it.)

### 4.4 Gap: gateway-down blind spot for critical health

If the gateway is down, no cron tick fires — including the health watchdog that would tell you
the system is unhealthy. The in-gateway scheduler is the right tool for watching **external**
state (HermX), but a watchdog that must fire *even when Hermes itself is down* cannot live inside
Hermes.

**Bridge:** for the single most critical liveness check (receiver + dashboard reachable), also
register an **OS-level cron** (`crontab`) that `curl`s a Hermes **webhook subscription** or an
external alert endpoint — an independent process that does not depend on the gateway being up.
This is explicitly the pattern the Hermes docs recommend (`cron-script-only.md` §Comparison).
The in-gateway health job (§6.5) covers the common case; the OS-cron backstop covers "Hermes
itself died." This is optional but recommended for the health check specifically.

---

## 5. Skill design

### 5.1 Reuse the existing read-only skills

The read-only operator skills already encode exactly the reads a monitor needs, with the
UNKNOWN-never-flat contract baked in. No new skill is strictly required to ship §6.

| Skill | Reads | Monitoring use |
|---|---|---|
| `hermx-status` | `/health` (arm/mode/kill-switch), `/api` (executor, freshness), receiver `/health`/`/latest`, strategy count | health, weekly/daily digest posture line |
| `hermx-positions` | `/api` → `okx_live.positions` (exposure, uPnL), open orders | digest exposure line, reconcile "confirm not flat" |
| `hermx-trace` | joins intake→outcome on `received_at`, follows a signal end-to-end | reconcile / stuck-order assessment |
| `signal-memory` | prior signals / continuity ("did we already act on this?") | digest continuity, risk-change context |

### 5.2 Two optional new skills (additive, recommended but not blocking)

- **`hermx-monitor` skill** *(not implemented — not on disk)* — a read-only "digest" skill that composes
  status + positions + signal-memory into a **standardized operator digest format**. Benefit: the
  digest layout lives in one tested skill instead of being re-described in every cron prompt.
  Follow the frontmatter schema of `skills/hermx-status/SKILL.md`. Register it via §4.1.
- **`dashboard-risk` skill** *(not implemented — not on disk)* — the planned MXC risk-read skill
  (`docs/HERMX_AGENT_SYSTEM_DESIGN.md:496-525`; referenced in `related_skills` but not on disk).
  Reads the MXC Kinetic dashboard (`pp_acc`/`pp_vel`/`regime`/`risk_state`), honoring the
  `risk_index_gate_enabled` flag in `control-state.json`. Lets the risk job delegate MXC parsing
  to a skill instead of scraping in a pre-check script.

Both are additive: the §6 jobs work with the existing trio; adding these only changes the
`--skill` list on the relevant jobs.

### 5.3 Skill frontmatter parity

New skills mirror the existing schema: top-level `name`/`description`/`version`/`author`/
`license`/`platforms`/`required_environment_variables` + `metadata.hermes.{tags,related_skills,
config}`. Keep `HERMX_SECRET` in `required_environment_variables` (required only under
`HERMX_DASH_AUTH=true`) and the loopback base URLs in `metadata.hermes.config`, exactly as
`hermx-status` does.

### 5.4 Read-only invariant

Monitoring jobs must **only** load read-only skills. Never map a monitor job to a mutating skill
or to `hermx-control` (which can *relay* a signal to `/webhook`). An unattended cron session also
cannot answer an interactive `yes`, so a mutating skill would stall anyway. This mirrors
`MONITOR_DAEMON_SPEC.md` §5.2 verbatim.

---

## 6. Cron job specifications

Delivery target on this host: the operator Telegram DM is `7000111380`
(`~/.hermes/channel_directory.json` → "Tiger (MomentumX) Quant"). `TELEGRAM_HOME_CHANNEL` is
currently commented out in `~/.hermes/.env`, so **either** set `TELEGRAM_HOME_CHANNEL=7000111380`
(and `TELEGRAM_CRON_THREAD_ID` for a dedicated Cron topic) to use `--deliver telegram`, **or**
target the DM explicitly with `--deliver telegram:7000111380`. Specs below use `telegram`
(home channel); substitute the explicit id if the home channel is left unset.

`WORKDIR` below = `"/Users/anatolizurablev/dev projects/hermx"` (§4.2). Pin `--provider`/`--model`
on every job to avoid the fail-closed skip on a global-default change (§2.4). Times are UTC
(match `HERMX_MONITOR_SUMMARY_HOUR_UTC` semantics from the old spec).

The installer (`deploy/install-cron-monitors.sh`) provisions exactly **five** jobs — §6.1
`hermx-weekly`, §6.2 `hermx-reconcile`, §6.3 `hermx-daily`, §6.4 `hermx-signal-late`, §6.5
`hermx-health-check`. Idempotency is name-based, with two modes: **default** (human installer)
creates missing jobs and `cron edit`s existing ones to enforce the definition; **create-only**
(`HERMX_CRON_CREATE_ONLY=1`) creates missing jobs but **skips existing ones**, preserving operator
pauses and edits. `deploy/deploy.sh` runs the installer in create-only mode on every upgrade
(§5.5), so a deploy can never silently re-enable a paused monitor or overwrite a hand-tuned
schedule. The MXC risk job (`hermx-risk-watch`) that once lived here was **removed** — see §6.4.

### 6.1 Weekly summary — Monday 09:00 UTC (LLM, no pre-check needed)

Fires once per ISO week natively; no `last_period` bookkeeping (the scheduler owns next-run).

```bash
hermes cron create "0 9 * * 1" \
  "You are HermX's read-only weekly reporter. Using the loaded skills, read current status, \
open positions/exposure, and recent signal history. Produce a concise WEEKLY operator summary: \
arm state & mode, executor health, exposure and unrealized PnL, notable reconcile/stuck-order \
events this week, and the single most useful thing for the operator to check. Report UNKNOWN for \
any read that is stale or unavailable — never assume 'flat' or 'healthy'." \
  --skill hermx-status --skill hermx-positions --skill signal-memory \
  --workdir "$WORKDIR" \
  --deliver telegram \
  --provider <pinned> --model <pinned> \
  --name "hermx-weekly"
```

### 6.2 Reconcile-error alert — every 5 min, deliver only on change (pre-check gate + LLM)

Pre-check gate (`hermx-reconcile-gate.py`, §7) tails `logs/alerts.jsonl` for reconcile/state rows
whose severity ≥ warning that are **new since the last notified fingerprint** (window 1800s,
escalation bypass). If none → `{"wakeAgent": false}` ($0 tick). If new → wakes the agent with the
new rows as context; the agent uses `hermx-trace`/`hermx-positions` to assess and either reports
or replies `[SILENT]` if, on inspection, it's already resolved.

```bash
hermes cron create "every 5m" \
  "A reconcile/watchdog condition changed on HermX (details in the injected context). Use the \
loaded skills to trace the affected signal(s) end-to-end and confirm current exposure. Report a \
short operator summary: what fired, current CONFIRMED state, and the next check. If, after \
tracing, the condition is already resolved and benign, reply with only [SILENT]. Never assume \
'flat' — report UNKNOWN if a read is unavailable." \
  --script hermx-reconcile-gate.py \
  --skill hermx-trace --skill hermx-positions \
  --workdir "$WORKDIR" \
  --deliver telegram \
  --provider <pinned> --model <pinned> \
  --name "hermx-reconcile"
```

### 6.3 Daily digest — 08:00 UTC (LLM, no ask, no pre-check)

"Daily digest without asking" = a self-contained prompt that always produces the digest
(it is not conditional, so no gate and no `[SILENT]`).

```bash
hermes cron create "0 8 * * *" \
  "Produce the HermX DAILY digest using the loaded skills: arm state & mode, executor/freshness \
health, open positions with exposure and uPnL, count of reconcile/operator alerts in the last 24h, \
and any strategy-mode overrides currently set. Keep it under 200 words. UNKNOWN for stale reads." \
  --skill hermx-status --skill hermx-positions --skill signal-memory \
  --workdir "$WORKDIR" \
  --deliver telegram \
  --provider <pinned> --model <pinned> \
  --name "hermx-daily"
```

### 6.4 Signal-late (zero-intake) — every 30 min, gated (pre-check gate + LLM)

Pre-check gate (`hermx-intake-gate.py`, §7) reads the rolling intake window and computes how long
since the last TradingView intake. If a fresh intake is present (< 3 days) → `{"wakeAgent": false}`
($0 tick). If the gap exceeds 3 days → wakes the agent with the gap as context: the receiver may be
up while alerts stopped arriving, a silent observability hole. The agent replies `[SILENT]` if, on
inspection, a recent intake is present after all.

```bash
hermes cron create "every 30m" \
  "HermX has received NO TradingView intake for over 3 days (details in the injected context). \
The receiver may be up while alerts stopped arriving — a silent observability hole. Produce a short \
operator note: how long since the last intake, the likely cause (TV alert paused, webhook URL \
changed, network), and the recommended human check. If, on inspection, a recent intake is present \
after all, reply with only [SILENT]. Never assume 'quiet market' — report the gap plainly." \
  --script hermx-intake-gate.py \
  --workdir "$WORKDIR" \
  --deliver telegram \
  --name "hermx-signal-late"
```

> **Removed: risk monitoring (MXC) — `hermx-risk-watch`.** An MXC risk-index job was specified here
> but is **not installed**: its gate keyed on `risk_index_gate_enabled`, a flag that does not exist
> anywhere in the codebase, so the job could never fire — and an inert monitor is worse than none
> (false reassurance that risk is watched). The `hermx-risk-gate.py` script is kept in the repo but
> unwired. Re-add the flag (and pin `--provider`/`--model`) before wiring this job.

### 6.5 Health check — every 5 min (NO-AGENT script watchdog, $0)

Pure liveness of receiver + dashboard + arm/kill-switch. No reasoning needed → `--no-agent`, so
it never touches a model. Empty stdout when healthy (silent tick); one line per problem when not.
Non-zero exit / timeout auto-delivers an error alert.

```bash
hermes cron create "every 5m" \
  --no-agent \
  --script hermx-health-watch.py \
  --workdir "$WORKDIR" \
  --deliver telegram \
  --name "hermx-health-check"
```

### 6.6 Optional future jobs (drop-in, same pattern)

The old spec's candidate sources (`MONITOR_DAEMON_SPEC.md` §10.1) map 1:1 to new cron jobs:
`drawdown` (enforce `max_daily_loss_usd`, computed from positions), `advisor_veto` (run of `skip`
verdicts in `pipeline.jsonl`), `schema_error_spike`, `queue_lag`. Each is a new pre-check gate +
job, no other change.

---

## 7. Deduplication strategy (concrete)

Three layers, strongest (cheapest) first:

1. **Pre-check `wakeAgent` gate ($0, no LLM).** For any "notify on change" job (§6.2, §6.4). The
   gate is the daemon's `Deduplicator` as a script. Contract:

   - **stdin/args:** none (the gate reads HermX state itself).
   - **reads:** via `hermx_ops` — loopback `/health`/`/api` and/or tolerant file reads of
     `logs/alerts.jsonl`, `control-state.json`. Every read degrades to `UNKNOWN`, never a fake
     value.
   - **fingerprints:** identical templates to `MONITOR_DAEMON_SPEC.md` §4.2 (e.g.
     `reconcile:{alert}:{symbol}:{cl_ord_id}`, `risk:{risk_state}:{symbol}`). No timestamps or
     free-text in the fingerprint (that would defeat dedup — the `normalize()`/`signal_id`
     non-determinism failure in `.claude/rules/code-quality.md`).
   - **state:** sidecar `~/.hermes/scripts/.hermx-<concern>.state` (JSON:
     `{fingerprint: {last_notified_epoch, last_severity}}`), atomic write.
   - **decision:** a condition is *fresh* if unseen, or its window elapsed, or its severity
     escalated (info<warning<error<critical). Windows: health 900s, reconcile 1800s, risk 3600s.
   - **output:** last stdout line is `{"wakeAgent": false}` if no fresh condition; else
     `{"wakeAgent": true, "context": {"alerts": [ {category, severity, title, fingerprint,
     detail} … ] }}`. **The gate updates `last_notified_epoch` for the fresh fingerprints only
     when it decides to wake** (at-least-once: if the agent run then fails, the job auto-delivers
     the failure, so the operator still learns).

2. **`[SILENT]` (LLM decides nothing to report).** The gate is intentionally permissive — it
   wakes on *any* fresh fingerprint. The agent, after tracing with the read-only skills, may
   conclude the condition is already resolved/benign and reply `[SILENT]`, suppressing delivery
   while still logging to `~/.hermes/cron/output/`. This is the second-opinion layer the daemon
   never had.

3. **No-agent empty stdout (health job).** The §6.5 watchdog emits nothing when healthy → silent
   tick. One line per problem when not.

Escalation and windows live in layer 1; the agent's judgment is layer 2; the pure watchdog is
layer 3. Together they reproduce the daemon's dedup semantics with less HermX-owned code.

---

## 8. Configuration & provisioning

Jobs are **not** a HermX config file — they live in `~/.hermes/cron/jobs.json`, owned by the
gateway. HermX owns only: (a) the read-only skills (already in `skills/`), (b) the pre-check /
no-agent scripts (new, in `~/.hermes/scripts/`), (c) an idempotent install script that registers
skills and creates jobs.

### 8.1 `deploy/install-cron-monitors.sh` (new, idempotent)

1. Symlink read-only skills into `~/.hermes/skills/trading/` (§4.1).
2. Copy the pre-check / no-agent scripts from `deploy/hermes-scripts/` into `~/.hermes/scripts/`
   (`chmod +x`). *(Scripts are version-controlled in the HermX repo under `deploy/hermes-scripts/`
   and installed to the gateway's script dir — Hermes requires them under `~/.hermes/scripts/`.)*
3. Ensure gateway env has `HERMX_DATA_DIR` (§4.2).
4. For each job in §6: check `hermes cron list` for the job **name**; if absent, `hermes cron
   create …`; if present and definition drifted, `hermes cron edit <name> …`. (Name-based lookup
   makes this idempotent; names must be unique across our jobs.)
5. Smoke test: `hermes -z "ping" --skills hermx-status` and `hermes cron run <name>` for each,
   then inspect `~/.hermes/cron/output/<job_id>/`.

### 8.2 Which management surface

- **Provisioning / CI:** the CLI (`hermes cron create/edit`) driven by the install script above —
  reproducible, reviewable, diffable.
- **Ad-hoc operator changes:** natural language in chat (`/cron pause hermx-reconcile`) or the
  `cronjob` tool — the operator can pause a noisy monitor without touching the repo.
- **Never** hand-edit `~/.hermes/cron/jobs.json` (atomic-write file owned by the scheduler).

### 8.3 Pinning (cost safety)

Pin `provider`/`model` on every monitor job (§2.4). For the highest-frequency jobs (health, every
5m) prefer `--no-agent` (§6.5) or a cheap pinned model; the pre-check gates keep LLM ticks rare on
the reconcile/risk jobs. Consider `TELEGRAM_CRON_THREAD_ID` so all monitor deliveries land in a
dedicated "Cron" Telegram topic instead of the main DM.

---

## 9. Deployment

**No new systemd unit. No new compose service. No Dockerfile change.** The gateway that runs the
jobs is already installed and running (`~/.hermes/gateway_state.json`).

Checklist:
- [ ] Gateway installed as a service (`hermes gateway install` / `--system` on Linux) so it
      survives reboot and restarts on failure. (Verify it is, not just foreground.)
- [ ] Read-only skills registered (§4.1) and resolvable (`hermes -z "ping" --skills hermx-status`).
- [ ] Pre-check / no-agent scripts installed under `~/.hermes/scripts/` (§8.1).
- [ ] `HERMX_DATA_DIR` (and `HERMX_SECRET` iff `HERMX_DASH_AUTH=true`) present in gateway env.
- [ ] Delivery target set: `TELEGRAM_HOME_CHANNEL=7000111380` (+ optional `TELEGRAM_CRON_THREAD_ID`),
      or explicit `--deliver telegram:7000111380` on each job.
- [ ] Jobs created (§6) and each fired once via `hermes cron run <name>`; output inspected.
- [ ] (Optional, recommended for health) OS-cron backstop for the gateway-down blind spot (§4.4).

The bridge scripts *do* ship in the HermX repo (`deploy/hermes-scripts/`) and are installed to the
gateway host — but they run as short-lived subprocesses of the gateway tick, not as a HermX
service. Unlike the daemon spec's §7.1 note about `hermes` not being in the Docker image, there is
**no containerization problem here**: the gateway runs on the host where `hermes` and `~/.hermes`
already live.

---

## 10. Testing strategy

### 10.1 HermX-owned code (the bridge scripts) — unit tests in the HermX repo

The only new HermX code is the pre-check / no-agent scripts. Test them the same way the daemon
spec proposed to test collectors (`MONITOR_DAEMON_SPEC.md` §8), reusing repo conventions
(`tests/`, `tmp_path`, `monkeypatch.setattr(urllib.request, "urlopen", fake)` per
`test_hermx_ops.py`, **inject the clock as a parameter — never freeze time**):

| Area | Assert |
|---|---|
| Fingerprint stability | same condition → same fingerprint across ticks; volatile fields excluded |
| Window / escalation | first fresh condition → `wakeAgent:true`; second within window → `wakeAgent:false`; warning→error inside window → `wakeAgent:true` |
| Sidecar state | atomic write; corrupt/missing → treated as empty (notify-once, self-healing) |
| Reconcile gate | fed a fake `alerts.jsonl` (torn tail tolerated), emits the right fresh fingerprints; advances only past cleanly-parsed rows |
| Risk gate | `risk_index_gate_enabled` false/absent → `wakeAgent:false`; true + `elevated` transition → wake; MXC unreachable → `wakeAgent:false` (fail-open) |
| Health watchdog (`--no-agent`) | healthy → empty stdout; receiver down → one line; dashboard down → one line; non-zero exit on internal error path |
| `wakeAgent` JSON contract | last stdout line is valid JSON of the documented shape |

These run in the existing offline gate (`pytest -m "not integration and not okx_paper …"`),
stdlib-only, no gateway required.

### 10.2 Job wiring — exercised against the real gateway

- **Resolution:** `hermes -z "ping" --skills hermx-status` (and each skill) after registration.
- **Fire-once:** `hermes cron run <name>` for each §6 job; inspect
  `~/.hermes/cron/output/<job_id>/{ts}.md`.
- **Silent path:** run a reconcile/risk job when nothing is wrong → verify the gate returned
  `wakeAgent:false` (no LLM spend, no delivery) via the output/audit log.
- **Delivery:** create a staging duplicate with `--deliver local` to inspect exact output before
  pointing at Telegram; or use a throwaway Telegram topic.
- **`[SILENT]`:** force a benign-but-fresh condition and confirm the agent's `[SILENT]` suppresses
  delivery while still writing to `cron/output/`.

### 10.3 Do not test Hermes internals

The scheduler, tick lock, atomic `jobs.json`, delivery router, and fail-closed guardrail are
Hermes's own, with their own test suite (`~/.hermes/hermes-agent/tests/cron/`). We test our
bridge scripts and our job wiring, not the gateway.

---

## 11. Failure modes

| Failure | Detection | Behavior | Bridge / mitigation |
|---|---|---|---|
| Gateway down | no ticks fire | all in-gateway monitors silent | run gateway as a service (auto-restart); OS-cron backstop for critical health (§4.4) |
| Skill not registered | job errors at load | job **auto-delivers** the error (not silent) | §4.1 registration; §9 smoke test |
| `workdir` wrong / missing | skill relative import fails | agent reports error → delivered | pin `--workdir` on every HermX job (§4.2) |
| Dashboard / receiver unreachable | `hermx_ops` read → `UNKNOWN` | health watchdog emits a "DOWN" line; gates degrade fail-open (no false all-clear, no false alarm) | UNKNOWN-never-flat contract already in `hermx_ops` |
| `alerts.jsonl` torn tail | tolerant read | skip the torn trailing line, advance cursor only past clean rows | reuse `read_jsonl_stats` semantics in the gate |
| Pre-check script error / timeout (LLM job) | non-zero exit / 120s timeout | Hermes delivers an error alert; agent not woken | fix script; a broken gate can't fail *silently* |
| No-agent script error / timeout (health) | non-zero exit / timeout | error alert auto-delivered | same — broken watchdog surfaces |
| Global default provider/model changed | unpinned job at fire time | job **skips + alerts** (#44585) — no silent paid spend | pin `provider`/`model` on every monitor job (§8.3) |
| Sidecar state corrupt | JSON parse error in gate | treat as empty → notify-once per active condition (self-healing) | atomic write; §10.1 test |
| Cron creates cron (runaway) | n/a | impossible — recursion guard disables `cronjob` tool in cron sessions | native |
| Two ticks overlap | n/a | `.tick.lock` returns 0 immediately | native |
| Rate-limited provider | 429 | credential-pool rotation / fallback providers | native (`config.yaml`) |
| Noisy monitor | operator judgment | `/cron pause <name>` from chat, no repo change | native lifecycle |

**Invariant preserved:** nothing a monitor does can block, mutate, or fail the HermX money path —
now enforced *by process isolation* (separate gateway process) rather than by our own fail-open
discipline. Monitors are strictly read-only against HermX (§5.4).

---

## 12. Migration path from the custom-daemon spec

1. **Do not implement `src/monitor_daemon.py`.** It was never built (the spec is DESIGN-only). No
   code to remove; just stop.
2. **`MONITOR_DAEMON_SPEC.md` is superseded** (banner added there pointing here). Keep it: its
   §4.2 (fingerprint templates), §4.3 (suppression windows), §5.2 (skill mapping), §6.4 (MXC gate)
   are the **domain logic** we now implement inside the pre-check gate scripts. It remains the
   reference for *what* to detect; this doc is *how* to schedule and deliver it.
3. **Drop these daemon artifacts entirely** — the gateway supplies each:
   - `deploy/hermx-monitor.service` (systemd) → gateway is already a service.
   - the `monitor:` compose service in `docker-compose*.yml` → not needed.
   - `monitor-state.json` + `_atomic_json_dump` re-copy → sidecar gate state instead.
   - the proposed `/api/monitor/alerts` dashboard endpoint (spec §10.2) → gates read
     `logs/alerts.jsonl` / `/api` directly via `hermx_ops`; add the endpoint later only if we want
     to decouple the gate from the ledger (still optional, not blocking).
   - `engine-config.json:monitor` block + the ~30 `HERMX_MONITOR_*` env vars → replaced by job
     definitions in `jobs.json` and a few `~/.hermes/scripts/` sidecars.
4. **Port the logic, not the plumbing.** Move §4.2/§4.3/§5.2/§6.4 of the old spec into
   `deploy/hermes-scripts/hermx-{reconcile,risk}-gate.py` and `hermx-health-watch.py`. The daemon's
   collectors become gate scripts; its deduplicator becomes the gate's state comparison; its
   HermesInvoker becomes the native cron session; its Sink becomes `--deliver`.
5. **Implementation order (follow-up session):**
   1. Register read-only skills (§4.1) + verify resolution.
   2. Write + unit-test the three gate/watchdog scripts (§7, §10.1).
   3. `deploy/install-cron-monitors.sh` (§8.1) creating the §6 jobs with pinned provider/model.
   4. Fire each once (`hermes cron run`), inspect output, then enable delivery to Telegram.
   5. (Optional) author `hermx-monitor` / `dashboard-risk` skills; add the OS-cron health backstop.

---

## Appendix A — Verified facts (this host, 2026-07-02)

| Fact | Source |
|---|---|
| Gateway running, Telegram connected | `~/.hermes/gateway_state.json` (`pid 53312`, `platforms.telegram.state=connected`) |
| Cron scheduler ticks every 60s in gateway | `cron-internals.md` §Scheduler Runtime; `InProcessCronScheduler` |
| Cron config: jobs.json + output dir + tick lock | `~/.hermes/cron/` (`jobs.json`, `output/`, `.tick.lock`, `ticker_heartbeat`) |
| `jobs.json` currently empty | `~/.hermes/cron/jobs.json` (no jobs defined yet) |
| Only `hermx-control` registered (symlink) | `~/.hermes/skills/trading/hermx-control → repo/skills/hermx-control`; `skills.external_dirs: []` in `config.yaml` |
| Read-only skills NOT registered | no `hermx-status`/`positions`/`trace`/`signal-memory` under `~/.hermes/skills/` |
| Operator Telegram DM id | `~/.hermes/channel_directory.json` → `7000111380` "Tiger (MomentumX) Quant" |
| `TELEGRAM_HOME_CHANNEL` commented out | `~/.hermes/.env` |
| `hermes -z … --skills …` one-shot pattern | `src/webhook_receiver.py:2751-2762` (`_advisor_agent_query`) |
| HermX skills read via loopback + files, UNKNOWN-never-flat | `skills/hermx-ops/lib/hermx_ops.py` (`DASHBOARD_BASE`/`RECEIVER_BASE`, `HERMX_DATA_DIR`-relative reads) |
| Skill body uses cwd-relative import | `skills/hermx-status/SKILL.md` (`sys.path.insert(0, "skills/hermx-ops/lib")`) |
| `control-state.json` shape (gate flag, risk_limits) | repo `control-state.json` (`risk_limits.max_daily_loss_usd`, `symbol_pauses`) |
| Cron features: `[SILENT]`, `wakeAgent`, `--no-agent`, `context_from`, workdir serialization, fail-closed on default change, recursion guard | `~/.hermes/hermes-agent/website/docs/{user-guide/features/cron.md,guides/cron-script-only.md,developer-guide/cron-internals.md}` |
| Fingerprints / windows / skill map / MXC gate (ported) | `MONITOR_DAEMON_SPEC.md` (removed) §4.2, §4.3, §5.2, §6.4 |
```
