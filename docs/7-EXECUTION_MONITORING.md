# HermX Execution Monitoring — Hermes Cron Deep Dive

**Status:** SHIPPED (describes the running system)
**Implements:** `deploy/install-cron-monitors.sh` + `deploy/hermes-scripts/*.py`
**History:** this file was previously `HERMES_CRON_MONITOR_DESIGN.md`, the design-pass document
for the pivot away from a custom monitor daemon. The design has since been implemented; this is
the deep-dive overview of what actually runs.
**Companion:** the failure-mode map formerly in MONITORING_GAPS_BRAINSTORM.md (doc since
removed) — the gaps this system does *not* yet cover are summarized in § 8 (limitations) below.

---

## 1. Purpose — watching a live trading engine for silent failures

HermX moves real money unattended: TradingView alerts arrive at the webhook receiver, are
validated, and dispatched to exchanges through CCXT adapters. The failures that actually cost
money in such a system are overwhelmingly **silent**:

- an order stuck in `UNKNOWN` that nobody notices until the next signal collides with it;
- a `RECONCILE_MISMATCH` row written to `logs/alerts.jsonl` that no human ever reads;
- TradingView silently stopping — the receiver stays up, `/health` stays green, and every
  in-process check reports healthy while zero signals arrive;
- the P&L ledger feed stalling, so closes stop being recorded;
- the receiver or dashboard process simply being dead.

In-process checks cannot be trusted to report their own death, and HermX's core invariant is that
**nothing on the observability path may ever block, mutate, or corrupt the money path**. So
monitoring lives in a *separate process*: the Hermes gateway's built-in cron scheduler, which was
already running on the host, already ticking every 60 seconds, and already delivering to the
operator's Telegram. HermX contributes only small, read-only bridge scripts and job definitions —
no new daemon, no new systemd unit, no new compose service. (The alternative, a ~600-line custom
`src/monitor_daemon.py`, was designed and rejected; see
`.claude/skills/_general/references/rejected-approaches.md`.)

The design posture, end to end:

- **Fail-open.** A monitor that cannot read state emits nothing (never a false all-clear and
  never a false alarm); a monitor that crashes surfaces the crash. Monitoring failures never
  back-pressure onto trading.
- **Read-only.** Monitor jobs load only read-only skills and read HermX state via loopback HTTP
  and tolerant file reads. The only files a gate ever writes are its own sidecars.
- **Only speak on change.** Suppression windows and fingerprint dedup keep the operator's
  Telegram quiet unless something is genuinely new, escalated, or overdue.

---

## 2. End-to-end flow

```
Hermes gateway (separate process, already a managed service)
  │  ticks InProcessCronScheduler every 60s; due jobs fire under ~/.hermes/cron/.tick.lock
  ▼
cron job (defined in ~/.hermes/cron/jobs.json, provisioned by install-cron-monitors.sh)
  │
  ├─ [gated LLM job]   pre-check script in ~/.hermes/scripts/ runs first (120s timeout)
  │     │  reads HermX state read-only via skills/hermx-ops/lib/hermx_ops.py
  │     │  derives condition dicts, runs them through hermx_gate_lib.run_gate()
  │     ├─ nothing fresh → last stdout line {"wakeAgent": false} → tick ends, $0, no LLM
  │     └─ fresh conditions → {"wakeAgent": true, "context": {"alerts": [...]}}
  │           ▼
  │        fresh AIAgent session (no history), read-only skills injected,
  │        --workdir = the HermX repo; agent assesses using the injected context
  │           ├─ something to report → operator summary → --deliver telegram
  │           └─ resolved/benign on inspection → replies "[SILENT]" → delivery suppressed
  │                (still written to ~/.hermes/cron/output/{job_id}/ for audit)
  │
  ├─ [--no-agent job]  script only, zero LLM ever
  │     ├─ empty stdout → silent tick
  │     ├─ non-empty stdout → delivered verbatim to Telegram
  │     └─ non-zero exit / timeout → Hermes auto-delivers an error alert
  │        (a broken watchdog cannot fail silently)
  │
  └─ [ungated LLM job] (daily/weekly digests) agent always runs; self-contained prompt
```

**How scripts read HermX state.** Every gate script bootstraps the shared operator library
`skills/hermx-ops/lib/hermx_ops.py` (via `hermx_gate_lib.import_hermx_ops()`, absolute-path,
cwd-independent). That library reads exactly what the operator slash-command skills read:
loopback HTTP to the dashboard `/health` and `/api` and the receiver `/health`, plus tolerant
line-by-line reads of the JSONL ledgers (`logs/alerts.jsonl`, `logs/raw-webhooks.jsonl`, torn
tails skipped). It carries the **UNKNOWN-never-flat** contract: a degraded or unreachable read
degrades to `UNKNOWN`, never to a fabricated "flat"/"healthy" value.

**Where things live.**

| Artifact | Location | Owner |
|---|---|---|
| Job definitions | `~/.hermes/cron/jobs.json` (atomic writes, never hand-edited) | Hermes gateway |
| Bridge scripts (source of truth) | `deploy/hermes-scripts/` in this repo | HermX |
| Bridge scripts (installed copies) | `~/.hermes/scripts/` | installer |
| Gate sidecar state | `~/.hermes/scripts/.hermx-<concern>.state` | each gate |
| Run output / audit trail | `~/.hermes/cron/output/{job_id}/{ts}.md` | Hermes gateway |
| Read-only skills | repo `skills/`, symlinked into `~/.hermes/skills/trading/` | installer |

The sidecar files are monitoring bookkeeping, deliberately kept *outside* `HERMX_DATA_DIR` so
they can never be confused with trading state (`control-state.json`).

---

## 3. Gate evaluation — `hermx_gate_lib.py`

All change-detection logic is concentrated in one stdlib-only shared library,
`deploy/hermes-scripts/hermx_gate_lib.py` (~190 lines). Each gate script derives **conditions**
from HermX state and hands them to the lib; the lib decides which are worth waking on.

### 3.1 Conditions and fingerprints

A condition is a dict: `{fingerprint, severity, category, title, detail}`.

The **fingerprint** is the stable identity of a problem — e.g.
`reconcile:RECONCILE_MISMATCH:BTC-USDT:mxc1234…`, `reconcile:stuck_order:{cl_ord_id}`,
`health:receiver_down`, `frequency:zero_intake:global`. Fingerprints carry **no timestamps,
counters, or free text**: a volatile field would make the "same" problem look new on every tick
and defeat dedup entirely (the same failure class as the `normalize()`/`signal_id`
non-determinism gotcha in `.claude/rules/code-quality.md`).

**Severity** is ranked `info < warning < error < critical`, matching the receiver's
`emit_operator_alert` vocabulary.

### 3.2 Freshness: `is_fresh()` / `evaluate()`

A condition is *fresh* — worth waking the operator for — if any of:

1. its fingerprint has never been notified (unseen);
2. its **suppression window** has elapsed since the last notification; or
3. its severity **escalated** past the last-notified severity (escalation bypasses the window:
   a `warning` that becomes an `error` mid-window still wakes).

Per-concern suppression windows (`hermx_gate_lib.WINDOW`):

| Concern | Window | Rationale |
|---|---|---|
| `health` | 900 s | liveness problems deserve a re-nag every 15 min |
| `reconcile` | 1800 s | reconcile conditions evolve on the resolver's timescale |
| `risk` | 3600 s | risk-state changes are slow, hourly re-notification is enough |
| `signal_late` | 3600 s | zero-intake is a slow absence condition; don't spam |

`evaluate(conditions, state, window, now_epoch)` splits the input into the fresh set and the
next sidecar state. Two deliberate properties:

- **The clock is injected** (`now_epoch` parameter) — never read inline inside the decision
  functions — so tests pin it instead of freezing time (repo convention).
- **Wake == write.** `run_gate()` persists the sidecar *only* when it decides to wake
  (at-least-once semantics: if the downstream agent run then fails, Hermes auto-delivers the
  failure, so the operator still learns something fired).

### 3.3 Sidecar state — atomic and self-healing

The sidecar (`.hermx-<concern>.state`) maps
`fingerprint → {last_notified_epoch, last_severity}`. Writes are atomic
(temp file → fsync → `os.replace`). Reads never raise: a missing, corrupt, or non-dict sidecar
degrades to `{}`, which means "notify every currently-active condition once" — self-healing, not
a hard failure.

### 3.4 Presence vs. absence detection

The fingerprint machinery naturally detects **presence** (a bad row appeared). **Absence**
conditions (rows that *stopped* appearing) need no new lib primitive: the gate script reads a
rolling window, computes a count or recency, and *synthesizes* a condition when the value is
anomalously stale — which then flows through `run_gate()`/`evaluate()` unchanged. The intake gate
(§4.5) is the first instance of this shape; the only lib touch it needed was its suppression
window key. This "proportionate, not gold-plated" split — condition derivation always in the
per-gate script, dedup/suppression always in the lib — is a deliberate boundary
(see `.claude/CLAUDE.md`, Proven Patterns).

The lib is covered by `tests/test_monitor_cron_gates.py`, which calls the real production
functions (never a re-implementation of them) and runs in the offline pytest gate.

---

## 4. The monitor jobs

`deploy/install-cron-monitors.sh` provisions **six** jobs. Summary, then detail per job:

| Job name | Cadence | Type | Script | Watches |
|---|---|---|---|---|
| `hermx-health-check` | every 5 m | `--no-agent` | `hermx-health-watch.py` | receiver/dashboard liveness, kill switch |
| `hermx-reconcile` | every 5 m | gate + LLM | `hermx-reconcile-gate.py` | reconcile/watchdog alerts, stuck orders, ledger mismatches, rejected orders |
| `hermx-ledger-reconcile` | every 10 m | `--no-agent` | `hermx-ledger-reconcile.py` | drives the P&L ledger reconcile |
| `hermx-signal-late` | every 30 m | gate + LLM | `hermx-intake-gate.py` | zero TradingView intake (absence) |
| `hermx-daily` | 08:00 UTC | LLM | — | daily operator digest |
| `hermx-weekly` | Mon 09:00 UTC | LLM | — | weekly operator summary |

Two jobs are deliberately **not installed**: `hermx-risk-watch`, specified but cut before it
ever shipped (§4.8), and `hermx-reconcile-lag`, retired in favor of the
ledger-mismatch/rejected-order conditions inside `hermx-reconcile` (§4.4).

### 4.1 `hermx-health-check` — process liveness (every 5 m, `--no-agent`, $0)

`hermx-health-watch.py` checks, over loopback: dashboard `/health` reachable and `ok`
(→ `health:dashboard_unreachable`, critical), receiver `/health` reachable and `ok`
(→ `health:receiver_down`, critical), and — only when the dashboard answered — the arm block:
kill switch engaged while `HERMX_LIVE_TRADING` is truthy (→ `health:kill_switch_engaged`,
warning; on demo/shadow hosts an engaged kill switch is the normal fail-closed state, so it does
not alert there), and disarmed while `HERMX_HEALTH_REQUIRE_ARMED` is set (→ `health:disarmed`,
warning).

Even though it is a `--no-agent` script (plain text out, never the `wakeAgent` JSON contract), it
still routes its problems through the shared suppression window (`health`, 900 s) so a
steady-state problem prints once per window instead of flooding Telegram every 5 minutes.
Healthy → empty stdout → silent tick. Any internal error exits non-zero, which Hermes
auto-delivers — **a broken watchdog surfaces instead of failing silently**.

**Failure mode if its inputs vanish:** none hidden — an unreachable endpoint *is* the alert
condition, so this monitor cannot go inert by a missing dependency.

### 4.2 `hermx-reconcile` — reconcile/watchdog conditions (every 5 m, gate + LLM)

`hermx-reconcile-gate.py` derives four families of conditions:

- **Alert rows:** `logs/alerts.jsonl` rows with `kind ∈ {reconcile, state, operator}`,
  severity ≥ `warning`, within the lookback (`HERMX_RECONCILE_LOOKBACK_SECONDS`, default = the
  1800 s reconcile window, so a first run never surfaces stale history). This catches
  `RECONCILE_MISMATCH`, `UNKNOWN_RESOLVER_TIMEOUT`, `PLANNED_ORDER_ABANDONED`,
  `WATCHDOG_DEGRADED`, `QUEUE_SATURATION`, etc. Fingerprint:
  `reconcile:{alert}[:{symbol}][:{cl_ord_id}]`.
- **Stuck orders:** dashboard `/api` open-orders rows with `state == "UNKNOWN"` →
  `reconcile:stuck_order:{cl_ord_id}` (warning), with a `/hx-troubleshoot` hint in the title.
  An unreachable dashboard yields nothing (fail-open — reachability is the health watchdog's
  job; never a false all-clear on trading state).
- **Ledger mismatch:** a strategy's most recent *close-implying* intake signal (an
  `action == "close"` payload, or a buy/sell that flips the strategy's running side —
  every signal implies `CLOSE_OPPOSITE_IF_ANY` per `strategy/readiness.py`) resulted in a
  `FILLED` order at the venue, yet no matching `closed-trades.jsonl` row exists →
  `reconcile:ledger_mismatch:{strategy_id}` (warning). Correlation is file-only, no `src/`
  imports: signal → cl_ord_ids via the submit-time map `cl-ord-strategy-map.jsonl`,
  cl_ord_id → terminal state via the live order-journal segment
  (`logs/order-journal.jsonl`), cl_ord_id/strategy → ledger row via `closed-trades.jsonl`.
  It fires only past a grace window (`HERMX_LEDGER_MISMATCH_GRACE_SECONDS`, default
  1800 s). The grace is bounded by the system's **own order-resolution ceiling** —
  `UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS` (900 s) plus one `hermx-ledger-reconcile` tick
  (600 s) plus buffer — *not* by a market-activity estimate (that mistake is what
  miscalibrated the retired `hermx-reconcile-lag` gate, §4.4). A signal whose order ended
  `REJECTED` is skipped (the rejected-order family owns it); no terminal state yet is
  skipped (still resolving, or already surfaced as a stuck order).
- **Rejected orders:** live order-journal rows with terminal `state == "REJECTED"` within
  the lookback → `reconcile:rejected_order:{cl_ord_id}` (warning). This closes a genuine
  gap: a rejected close/flip leg leaves the position open, and nothing else alerts on it —
  the periodic unknown-resolver journals a `REJECTED` resolution silently, and
  `reconcile_position_drift` is unwired code.

Both journal-reading families scan only the **live** segment via the tolerant line reader.
Segment rotation seals the live file after 1000 records — orders of magnitude beyond what a
30–60 min window produces at HermX signal cadence — and a record missed to rotation degrades
to no alert (fail-open, consistent with the gate's read-gap posture elsewhere), never a
false one.

Fresh conditions wake an LLM agent (skills: `hermx-trace`, `hermx-positions`,
`hermx-status`) that traces the affected signal end-to-end, confirms current exposure and
trading/arm state, and either reports a short operator summary or replies `[SILENT]` if the
condition is already resolved — the second-opinion layer that pure threshold monitors don't
have.

**Failure mode:** the alert-row family is a *presence* detector over `alerts.jsonl` — it can
only surface what the receiver actually emits. The ledger-mismatch and rejected-order
families close the previously documented clean-`REJECTED` blind spot
(the former MONITORING_GAPS_BRAINSTORM.md Scenario E) by reading the order journal directly; what
remains invisible is anything that never reaches the WAL, journal, or alerts ledger at all.

### 4.3 `hermx-ledger-reconcile` — P&L ledger safety net (every 10 m, `--no-agent`)

`hermx-ledger-reconcile.py` closes the "close ages out of the 100-row exchange-history window
before anyone opens the dashboard" race: on a fixed cadence it GETs the dashboard `/api`, whose
model build already iterates the distinct `(venue, mode)` pairs across the strategy set and calls
`pnl_ledger.reconcile_from_order_history(rows, venue, mode)` for each. It deliberately does
**not** re-implement the reconcile — reusing the shipped, tested feed keeps venue/mode resolution
in exactly one place (the hardcoded-OKX-demo landmine in `.claude/rules/code-quality.md`).
Unreachable dashboard → prints a JSON status line and exits 0 (fail-open). Distinct from both the
LLM `hermx-reconcile` watchdog and the receiver's `HERMX_RECONCILE_ENABLED` post-submit
reconcile.

### 4.4 Retired: `hermx-reconcile-lag` — superseded by the §4.2 conditions

`hermx-reconcile-lag-gate.py` read `closed-trades.jsonl`, took `max(recorded_at_ms)` across
schema-v3 rows, and emitted one `reconcile:lag` condition when the newest row was more than
`HERMX_MAX_RECONCILE_LAG_MS` (20 min) behind wall-clock. It was **retired from the
installer** for two reasons:

1. **Miscalibrated threshold.** "Time since the last close" measures market activity, not
   pipeline health — real intake data shows healthy inter-close gaps up to 48 h, so a
   20-minute wall-clock threshold pages on quiet markets and says nothing about whether a
   close that *did* happen was ledgered.
2. **Sidecar race.** Its `run_gate("reconcile", …)` shared the `.hermx-reconcile.state`
   sidecar with `hermx-reconcile-gate.py` — an unlocked read-modify-write race between two
   independent crons (5 m and 15 m ticks) that could drop or resurrect suppression entries.

Both genuine failure modes it gestured at are now detected precisely by the
`ledger_mismatch` and `rejected_order` conditions inside `hermx-reconcile-gate.py` (§4.2) —
one job, one sidecar owner, so the race is gone by construction. Per the `hermx-risk-watch`
precedent (§4.8), the script and its tests are kept in the repo but deliberately unwired.
The reserved `stuck_unknown_count` / `ageout_fires` metrics remain TODOs in the unwired
script until their `/api` fields and an alert counter exist.

**On an already-provisioned host** the installer never deletes an existing job — run
`hermes cron delete "hermx-reconcile-lag"` (or pause it) manually.

### 4.5 `hermx-signal-late` — zero-intake absence detection (every 30 m, gate + LLM)

`hermx-intake-gate.py` closes the single biggest silent observability hole: TradingView stops
sending while the receiver stays up, so every liveness check keeps reporting healthy. It reads
the durable intake WAL `logs/raw-webhooks.jsonl` for `phase == "intake"` rows, takes the newest
`received_at`, and synthesizes one `frequency:zero_intake:global` condition (error) when that is
older than `HERMX_INTAKE_MAX_AGE_SECONDS` (default 3 days). Fail-open by construction: a missing
or empty WAL, no parseable timestamp, or a recent-enough intake all yield no alert. When it does
wake, the agent produces a short operator note (likely cause: TV alert paused, webhook URL
changed, network) or `[SILENT]` if a recent intake is present after all.

A per-strategy frequency baseline (each strategy's own cadence, learned from history) is the
planned evolution of this gate but is **intentionally not implemented** — prerequisites (enough
per-strategy history, a sidecar schema extension, answers on paused/new strategies and WAL
rotation) are documented in the script's TODO block and were mapped in the former
MONITORING_GAPS_BRAINSTORM.md Scenario A.

### 4.6 `hermx-daily` — daily digest (08:00 UTC, LLM, ungated)

Self-contained prompt, always fires: arm state and mode, executor/freshness health, open
positions with exposure and uPnL, alert count over the last 24 h, active strategy-mode overrides;
under 200 words; `UNKNOWN` for stale reads. Skills: `hermx-status`, `hermx-positions`,
`signal-memory`.

### 4.7 `hermx-weekly` — weekly summary (Monday 09:00 UTC, LLM, ungated)

Same skill set, weekly altitude: posture, exposure/uPnL, notable reconcile/stuck-order events of
the week, and the single most useful thing for the operator to check. Cron-expression scheduling
(`0 9 * * 1`) means the scheduler owns next-run computation — no "last period" bookkeeping.

Both digests carry the never-assume rule in the prompt: report `UNKNOWN` for any stale or
unavailable read — never assume "flat" or "healthy".

### 4.8 Removed: `hermx-risk-watch` — the inert-monitor lesson

An MXC risk-index job was originally specified (every 15 m: read
`control-state.json:risk_index_gate_enabled`, and when enabled fetch the MXC Kinetic dashboard
and wake on a transition into `{elevated, high, risk_off}`). It was **removed from the
installer** because its gate keys on `risk_index_gate_enabled` — a flag that does not exist
anywhere in the codebase. The gate fail-opens when the flag is absent, so the job could **never
fire** — yet it would still appear in `hermes cron list` as active risk coverage.

**An inert monitor is worse than no monitor**: it is false reassurance that risk is watched.
Presence in the cron list is not coverage; a monitor is only coverage if the condition it gates
on can actually occur. `hermx-risk-gate.py` is kept in the repo (and its tests assert the
fail-open behavior) but is deliberately **unwired**. Re-wiring it requires (a) implementing the
`risk_index_gate_enabled` flag end-to-end (written by the dashboard, surviving
`default_control_state()` merges — see the key-drop gotcha in `.claude/rules/code-quality.md`),
and (b) pinning `--provider`/`--model` on the job (§5).

This generalizes to a review rule for every gate: **ask what happens when the flag, field, or
file the gate depends on doesn't exist.** Every shipped gate answers "fail-open, and the
condition it watches is produced by live code today"; the risk gate answered "fail-open, and the
enabling flag is produced by nothing" — which is why it was cut.

---

## 5. LLM monitor invocation and `--provider`/`--model` pinning

Each LLM job fire is a **fresh Hermes agent session**: no conversation history, the job's
read-only skills injected as context, the prompt appended as the task, and
`--workdir "<hermx repo>"` so the skills' cwd-relative imports and `HERMX_DATA_DIR`-relative
reads resolve. Monitoring jobs must only ever load **read-only** skills (`hermx-status`,
`hermx-positions`, `hermx-trace`, `signal-memory`) — never a mutating skill and never the
relay-capable `hermx-control`. Cron sessions also have the `cronjob` toolset disabled natively
(no monitor can spawn monitors).

**Why pinning matters.** Hermes is fail-closed on provider/model resolution: if a job does not
pin `--provider`/`--model` and the *global default* provider or model changes, the job **skips
its run** rather than silently spending on a new paid model. That is the right default for cost
safety — but for a monitor it means one global settings change dark-fires the entire LLM
monitoring layer: the jobs still sit in `hermes cron list` looking like coverage while every
tick is skipped. This is the same false-reassurance failure as the inert risk monitor, arriving
via configuration instead of a missing flag. The rule (recorded in
`.claude/rules/code-quality.md`) is therefore: **pin `--provider`/`--model` on every LLM cron
job.**

> **Current state (known deviation):** `deploy/install-cron-monitors.sh` does **not** yet pin
> provider/model on the four LLM jobs (`hermx-weekly`, `hermx-reconcile`, `hermx-daily`,
> `hermx-signal-late`) — the installer comment explicitly accepts the fail-closed-skip risk.
> This contradicts the rule above and is an open follow-up (§8). The two `--no-agent` jobs
> never invoke a model, so no pin applies to them.

**Cost control** is layered so LLM spend stays rare even at 5-minute cadences: the `wakeAgent`
pre-check gates make the common tick $0 (no model invoked at all); `--no-agent` jobs never touch
a model by construction; and `[SILENT]` suppresses delivery when a woken agent finds nothing
worth saying (the run is still audited in `~/.hermes/cron/output/`).

---

## 6. Provisioning, auditability, and operations

### 6.1 The installer — `deploy/install-cron-monitors.sh`

Idempotent; safe to re-run. Steps:

1. **Register read-only skills** — symlink `hermx-status`, `hermx-positions`, `hermx-trace`,
   `signal-memory` from the repo `skills/` dir into `~/.hermes/skills/trading/`.
2. **Install bridge scripts** — copy `hermx_gate_lib.py` and the gate/watchdog scripts from
   `deploy/hermes-scripts/` (the version-controlled source of truth) into `~/.hermes/scripts/`
   (where Hermes requires them), `chmod +x`.
3. **Ensure gateway env** — `TELEGRAM_HOME_CHANNEL` (operator DM) and `HERMX_DATA_DIR` (the repo
   path) in `~/.hermes/.env`; existing values are left as-is. The gateway must be restarted to
   pick up new env.
4. **Create/update the six jobs** by name (`ensure_job`). Two modes:
   - **default** (human-run): create missing jobs; `hermes cron edit` existing ones to enforce
     the checked-in definition.
   - **`HERMX_CRON_CREATE_ONLY=1`** (used by `deploy/deploy.sh` on every upgrade): create
     missing jobs, **skip existing ones entirely** — so a deploy can never silently re-enable a
     monitor the operator paused or overwrite a hand-tuned schedule.
5. **Smoke test** — skill resolution (`hermes -z "ping" --skills hermx-status`) and one manual
   fire per job (`hermes cron run <name>`; skip with `HERMX_CRON_SMOKE=0`).

`HERMX_CRON_DRY_RUN=1` prints every action without executing.

### 6.2 Auditability — `hermes cron list` and the output dir

`hermes cron list` is the coverage audit surface: every monitor, its schedule, and its state in
one place, reviewable without touching the repo. Every fire — delivered or `[SILENT]` — is
persisted to `~/.hermes/cron/output/{job_id}/{ts}.md`, so "did the gate wake?", "what did the
agent conclude?", and "why was nothing delivered?" are all answerable after the fact.

The §4.8 caveat applies when auditing: the list shows *scheduled* jobs, not *effective*
coverage. Auditing coverage means checking both that a job exists **and** that its gating
condition can actually occur.

### 6.3 Operator controls

- Pause a noisy monitor from chat: `/cron pause hermx-reconcile` (no repo change; a subsequent
  create-only deploy preserves the pause).
- Fire one manually: `hermes cron run <name>`, then inspect the output dir.
- Never hand-edit `~/.hermes/cron/jobs.json` — it is an atomic-write file owned by the
  scheduler; use the CLI/chat surfaces.
- Tuning knobs are env vars read by the scripts at run time: `HERMX_INTAKE_MAX_AGE_SECONDS`,
  `HERMX_RECONCILE_LOOKBACK_SECONDS`, `HERMX_LEDGER_MISMATCH_GRACE_SECONDS`,
  `HERMX_LIVE_TRADING`, `HERMX_HEALTH_REQUIRE_ARMED`, `HERMX_SCRIPTS_DIR` (sidecar location,
  used by tests). (`HERMX_MAX_RECONCILE_LAG_MS` belonged to the retired reconcile-lag gate,
  §4.4.)

---

## 7. Why this design — key benefits

- **Early detection without touching the money path.** Monitors run in the gateway — a separate
  process — and are read-only against HermX by construction. A monitor crash, hang, or bad LLM
  reply cannot block a close or corrupt a ledger; the isolation is enforced by process boundary,
  not just discipline.
- **Decoupled cron checks instead of in-process code.** The receiver's own watchdog can only see
  what the receiver process sees (and dies with it). Out-of-process cron checks catch the
  receiver being dead, the dashboard being dead, and — via absence detection — the world going
  quiet while everything reports healthy.
- **$0 default tick.** The `wakeAgent` gates and `--no-agent` jobs mean the steady state costs
  nothing: no model call, no delivery, one sidecar read. Money is spent only when a condition is
  fresh.
- **Signal, not noise.** Fingerprint dedup + per-concern suppression windows + severity
  escalation + the agent's `[SILENT]` second opinion keep Telegram quiet enough that a message
  actually means something.
- **Auditable.** `hermes cron list` for coverage, `~/.hermes/cron/output/` for every fire,
  version-controlled scripts and installer for the definitions.
- **Extensible with one script + one `ensure_job`.** A new gate is a ≤100-line stdlib script
  that derives conditions and calls `run_gate()` — the lib already handles dedup, suppression,
  sidecars, and the wake contract, for presence *and* absence conditions alike. No new service,
  unit, or deploy artifact.
- **Testable against production code.** The gates are plain functions with an injected clock;
  `tests/test_monitor_cron_gates.py` exercises the real `run_gate()`/`evaluate()`/gate functions
  in the offline pytest gate — never a re-implementation of the handler.
- **Nothing to operate.** No new daemon, systemd unit, or compose service; restarts, tick
  locking, atomic job storage, delivery fan-out, and provider fallback are the gateway's problem
  and already battle-tested.

---

## 8. Known limitations and follow-ups

1. **LLM jobs are not provider/model-pinned yet.** The installer explicitly accepts the
   fail-closed-skip risk (§5). Until pinned, a global default provider/model change silently
   disables all four LLM monitors. Follow-up: add pinned `--provider`/`--model` to the four
   LLM `ensure_job` calls.
2. **Risk monitoring is unwired.** `hermx-risk-gate.py` exists and is tested, but
   `risk_index_gate_enabled` is implemented by nothing, so the job is deliberately not installed
   (§4.8). Follow-up: implement the flag end-to-end (including `default_control_state()`
   survival), then re-add the job with pinning.
3. **Gateway-down blind spot.** If the Hermes gateway is down, no cron tick fires — including
   the health check that would report the outage. The recommended backstop — an OS-level
   `crontab` entry curling an endpoint independent of the gateway — is designed but not
   installed. Mitigation today: the gateway runs as a managed, auto-restarting service.
4. **Presence-detector gaps.** The most dangerous uncovered failure modes were catalogued in
   the former MONITORING_GAPS_BRAINSTORM.md (doc since removed): position drift (strategy-expected vs exchange-actual —
   undetected on the live path; the rejected-order condition in §4.2 now covers the
   rejected-close slice of it), per-strategy frequency baselines (un-trainable until enough
   history accrues), sustained exchange read outages (current-read only, no
   consecutive-failure escalation), and stale pauses. Clean order rejections — formerly
   Scenario E — are now covered by §4.2's `rejected_order` condition. Each remaining gap maps
   to a new gate script or a small producer change.
5. **Reserved reconcile-lag metrics.** `stuck_unknown_count` and `ageout_fires` in the
   retired reconcile-lag gate script await their `/api` fields and an alert counter (§4.4).
6. **Digest jobs are unconditional LLM spend.** `hermx-daily`/`hermx-weekly` always run their
   agent (by design — a digest that only appears on change is not a digest), so they are the
   jobs most exposed to limitation 1.
