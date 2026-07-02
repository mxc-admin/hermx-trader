# HermX Monitor Daemon — Technical Specification

> ## ⚠️ SUPERSEDED — do not implement `src/monitor_daemon.py`
>
> This custom-daemon design has been **superseded by
> [`HERMES_CRON_MONITOR_DESIGN.md`](./HERMES_CRON_MONITOR_DESIGN.md)**. The Hermes gateway
> (already running on this host) has a **built-in cron scheduler** that natively provides the
> scheduler loop, fresh-agent-per-event, skill injection, `[SILENT]`/`wakeAgent` deduplication,
> atomic state, single-instance locking, and multi-platform delivery this document proposed to
> hand-build. Building the daemon would duplicate a battle-tested subsystem.
>
> **This file is retained for its domain logic, not its plumbing.** Sections **§4.2** (fingerprint
> templates), **§4.3** (suppression windows), **§5.2** (read-only skill mapping), and **§6.4** (MXC
> risk gate) are still the reference for *what* to detect — they are now implemented inside the
> Hermes pre-check gate scripts described in the new design. The daemon plumbing (§2 state file,
> §7 systemd/compose, §10.2 `/api/monitor/alerts`) is **not** built. See the new doc's §12
> (Migration path) for the exact mapping.

**Status:** SUPERSEDED (design only — never implemented; see banner above)
**Target file:** ~~`src/monitor_daemon.py`~~ (not built — replaced by Hermes cron jobs)
**Author:** research + design pass, 2026-07-02
**Audience:** the developer who will implement this in a follow-up session.

---

## 0. Summary

The Monitor Daemon is a lightweight, **proactive** long-running process that fills the gap
between HermX's two existing agent-invocation modes:

- `hermes gateway start` — a persistent **reactive** daemon (operator messages in → agent replies out).
- `hermes -z "<prompt>" --skills …` — a **one-shot** agent call (used today only by the
  in-process execution advisor, `webhook_receiver.py:_advisor_agent_query`).

Neither watches the system on a schedule. HermX has **no built-in scheduler, cron, or
proactive monitoring** (confirmed: no `weekly`/`cron`/`scheduler` concept anywhere in `src/`).
The Monitor Daemon adds exactly that: it **polls** HermX state on a configurable interval,
**deduplicates** what it finds, and shells out to `hermes -z` **only when there is something
genuinely new**, aggregating multiple findings into a single agent invocation.

Design posture, inherited from the codebase's proven patterns:

- **Fail-open, never wedge the money path.** The daemon is strictly observational. It never
  submits, cancels, closes, or mutates control-state. A monitor crash must never affect the
  receiver or dashboard. (Mirrors the advisor's fail-open contract, `webhook_receiver.py:2795-2825`.)
- **Restarts are routine** (systemd `Restart=always`/`RestartSec=5`). All dedup state is
  persisted atomically so a restart cannot cause a notification storm or a silent gap.
- **stdlib-first.** Same runtime shape as the receiver/dashboard: `python src/monitor_daemon.py`,
  no uvicorn, no framework, config from import-time env vars, deps limited to what's already
  in `requirements.txt` (`ccxt`, `jsonschema` + stdlib).

---

## 1. Architecture

### 1.1 Where it fits

```
                          ┌─────────────────────────────────────────────┐
                          │                 HermX host                    │
                          │                                               │
  TradingView ──POST──►   │  receiver  (src/webhook_receiver.py :8891)    │
                          │    • /health (liveness only)                  │
                          │    • /latest                                  │
                          │    • writes logs/*.jsonl, control-state.json  │
                          │                     │  shared files           │
                          │                     ▼  (hermx-state/-data)    │
                          │  dashboard (src/dashboard.py :8098)           │
                          │    • /health  (no auth)                       │
                          │    • /api     (auth: X-Dashboard-Token)       │
                          │    • /api/signals                             │
                          │                                               │
   ┌──────────────────┐   │  ┌──────────────────────────────────────┐    │
   │ MXC Kinetic       │◄──┼──┤  monitor  (src/monitor_daemon.py)     │    │
   │ replit.app (risk) │   │  │   poll → dedup → aggregate → dispatch │    │
   └──────────────────┘   │  └──────────────┬───────────────────────┘    │
                          │                 │ subprocess                  │
                          │                 ▼                             │
                          │        hermes -z "<prompt>" --skills <skill>  │
                          │        (external binary on PATH)              │
                          └─────────────────────────────────────────────┘
```

The monitor is a **peer service** alongside `receiver` and `dashboard`, shipped in the same
Docker image and launched the same way. It reads state through two channels:

1. **HTTP polling** of the dashboard (`/api`, `/health`) and receiver (`/health`) over loopback —
   the authoritative "current view".
2. **Direct ledger reads** of `logs/alerts.jsonl` (the unified operator/reconcile/state alert
   ledger) — because there is **no HTTP endpoint that surfaces reconcile status or the raw alert
   stream** today (see §3.3). Reads are bounded/tolerant (reuse `dashboard_core.read_jsonl_stats`).

It writes nothing to shared HermX state. Its only outputs are: its own state file
(`monitor-state.json`), its own log (`logs/monitor.log`), and `hermes -z` subprocess calls.

### 1.2 Internal components

```
                          ┌───────────────────────────────────────────────┐
                          │              monitor_daemon.py                  │
                          │                                                 │
   env + engine-config ─► │  Config          (import-time, HERMX_-prefixed) │
                          │                                                 │
                          │  StateStore      monitor-state.json (atomic)    │
                          │      ▲     ▲                                     │
                          │      │     │                                     │
   loop tick ──────────►  │  ┌───┴─────┴────────────────────────────────┐   │
   (interval + jitter)    │  │ Collectors (AlertSource plugins)          │   │
                          │  │   • HealthCollector      → /health,/api   │   │
                          │  │   • ReconcileCollector   → /api,alerts.jsonl│ │
                          │  │   • RiskCollector (MXC)  → replit.app+flag │   │
                          │  │   • SummaryCollector     → cron trigger    │   │
                          │  └───────────────┬──────────────────────────┘   │
                          │                  │ list[Alert]                   │
                          │                  ▼                               │
                          │  Deduplicator    (last_notified_at + windows)   │
                          │                  │ fresh alerts only             │
                          │                  ▼                               │
                          │  Aggregator      (batch this tick's fresh alerts)│
                          │                  │ one AlertBatch                │
                          │                  ▼                               │
                          │  HermesInvoker   (prompt template → subprocess)  │
                          │                  │ stdout / exit code            │
                          │                  ▼                               │
                          │  Sink            log + optional webhook forward  │
                          └───────────────────────────────────────────────┘
```

### 1.3 Data flow (one tick)

1. **Poll.** Each enabled `Collector` runs. A collector does its own HTTP/ledger read and
   returns `list[Alert]`. A collector that raises or times out yields `[]` and increments its
   own backoff counter (it does **not** abort the tick).
2. **Detect meta-failures.** If the dashboard/receiver poll fails N consecutive times, the
   HealthCollector itself synthesizes a `health:dashboard_unreachable` alert — an outage is a
   monitorable condition, not a reason to go silent.
3. **Deduplicate.** Each `Alert` has a stable `fingerprint`. The `Deduplicator` drops any
   alert whose `fingerprint` was notified within its suppression window (unless severity
   escalated — see §4).
4. **Aggregate.** Surviving alerts for this tick are collected into a single `AlertBatch`.
5. **Dispatch.** If the batch is non-empty, `HermesInvoker` builds one contextual prompt,
   selects a skill (§5.2), shells out to `hermes -z` once, and captures stdout/exit code.
6. **Persist.** On a completed dispatch attempt, `last_notified_at[fingerprint] = now` for
   every alert in the batch, then the state file is written atomically. (Ordering & crash
   semantics: §4.4.)
7. **Sleep.** `interval ± jitter`, then repeat. Cron-style sources (weekly summary) are
   evaluated every tick but only fire when their schedule boundary is crossed (§4.5).

### 1.4 Threading model

Single-threaded synchronous loop. There is no need for a worker pool — a tick's work is a
handful of short HTTP GETs plus at most one bounded subprocess call. The subprocess `timeout`
is the only long-pole and it is hard-bounded (§5.3). This mirrors the receiver's design
principle that a fresh `hermes -z` per event cannot wedge the parent (a hung agent dies at the
subprocess timeout). If the daemon later needs to also expose an HTTP `/health` endpoint for
its own compose healthcheck, run a `ThreadingHTTPServer` on a daemon thread (§7.4) — that is
the only additional thread.

---

## 2. State Management

### 2.1 File location & resolution

The state file lives under `HERMX_DATA_DIR` (the mutable-snapshot volume), **exactly** like
`control-state.json` and `latest.json`. Resolve it with the same idiom the receiver and
dashboard use so all processes agree (`webhook_receiver.py:126-145`, `dashboard.py:38,56`):

```python
ROOT      = Path(os.environ.get("SHADOW_ROOT", Path(__file__).resolve().parents[1]))
LOG_DIR   = ROOT / "logs"
DATA_DIR  = Path(os.environ.get("HERMX_DATA_DIR", ROOT))
STATE_FILE   = DATA_DIR / "monitor-state.json"
MONITOR_LOG  = LOG_DIR / "monitor.log"
ALERTS_LEDGER = LOG_DIR / "alerts.jsonl"      # read-only consumption
```

Rationale: under Docker, `hermx-state → /app/data` is the persistent rw volume; putting the
state file there means it survives container recreation. `logs/` (`hermx-data`) holds the
alert ledger the monitor reads. Both volumes are already mounted for the existing services.

### 2.2 Schema (`monitor-state.json`)

Modeled on `control-state.json` (`version` int, `updated_at` microsecond-ISO, keyed maps):

```json
{
  "version": 1,
  "updated_at": "2026-07-02T18:04:11.204551+00:00",
  "last_notified_at": {
    "health:executor_degraded": "2026-07-02T17:59:02.101334+00:00",
    "reconcile:UNKNOWN_RESOLVER_TIMEOUT:SOLUSDT:clord-abc123": "2026-07-02T17:40:55.900012+00:00",
    "risk:elevated:SOLUSDT": "2026-07-02T16:10:00.000000+00:00"
  },
  "last_severity": {
    "health:executor_degraded": "error"
  },
  "cursors": {
    "alerts_jsonl_last_ts": "2026-07-02T17:59:02.101334+00:00"
  },
  "cron": {
    "weekly_summary": {
      "last_period": "2026-W26",
      "last_run_at": "2026-06-30T09:00:03.551200+00:00"
    }
  },
  "backoff": {
    "dashboard_poll": { "consecutive_failures": 0, "next_attempt_at": null }
  }
}
```

Field semantics:

| Key | Purpose |
|---|---|
| `last_notified_at[fingerprint]` | ISO timestamp of the last `hermes` dispatch that included this fingerprint. The core dedup datum. |
| `last_severity[fingerprint]` | Last severity notified for this fingerprint. Enables **escalation bypass** of the suppression window (info→error re-notifies immediately). |
| `cursors.alerts_jsonl_last_ts` | High-water mark for the `alerts.jsonl` tail read, so a restart does not re-surface historical alert rows. Uses the row `ts` (microsecond-ISO join key), never a byte offset (the ledger is append-only but may rotate/quarantine a trailing line). |
| `cron.weekly_summary.last_period` | ISO week label (`%G-W%V`) of the last weekly summary fired. Makes the cron trigger idempotent across restarts (§4.5). |
| `backoff[collector]` | Per-collector failure counter + earliest next-attempt time for exponential backoff (§3.4). |

### 2.3 Atomic persistence (reuse the canonical pattern)

Copy `_atomic_json_dump` from `webhook_receiver.py:1307-1317` **verbatim** (temp file in the
same dir → write → `flush` → `os.fsync(fd)` → `Path.replace` → `_fsync_dir(parent)`). This is
the repo's canonical durable-JSON write and gives crash-atomicity on the same filesystem:

```python
def _fsync_dir(path: Path) -> None:                      # webhook_receiver.py:1295-1304
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except (OSError, AttributeError):
        pass

def _atomic_json_dump(path: Path, obj: dict) -> None:    # webhook_receiver.py:1307-1317
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)
```

The daemon is single-threaded, so no `threading.RLock` is needed (unlike
`save_control_state`, `webhook_receiver.py:1163`, which guards multi-threaded writers). If a
`/health` thread is added, it must be read-only w.r.t. the state file.

### 2.4 Fail-safe load (reuse the merge-against-default pattern)

Mirror `load_control_state` (`webhook_receiver.py:1181-1202`): on missing/corrupt/non-dict
file, return `default_monitor_state()` rather than crashing. Merge only known keys and coerce
map fields to dicts. A corrupt state file therefore degrades to "notify everything once,
fresh" — safe and self-healing — never a hard failure. (See failure-mode table, §9.)

```python
def default_monitor_state() -> dict:
    return {"version": 1, "updated_at": now_iso(), "last_notified_at": {},
            "last_severity": {}, "cursors": {}, "cron": {}, "backoff": {}}
```

### 2.5 Idempotency across restarts

Because `last_notified_at`, `cron.last_period`, and `cursors.alerts_jsonl_last_ts` are all
persisted atomically **before** the process can exit a tick, a restart resumes exactly where
it left off:

- A still-active condition inside its suppression window is **not** re-notified.
- The weekly summary that already ran this ISO week is **not** re-run.
- The alert ledger is **not** re-scanned from the beginning.

The one bounded exception is a crash *between* a successful `hermes` dispatch and the state
write — at worst one batch is re-notified once (at-least-once delivery). This is acceptable for
advisory notifications and is called out explicitly in §4.4.

---

## 3. Polling Strategy

### 3.1 Endpoints & sources

| Source | Transport | Auth | Used for |
|---|---|---|---|
| `GET http://127.0.0.1:8098/health` | HTTP | **none** | dashboard liveness; `arm` block (kill switch, live/demo counts). `dashboard.py:1293-1308` |
| `GET http://127.0.0.1:8098/api` | HTTP | `X-Dashboard-Token: $HERMX_SECRET` | the rich model: `executor.degraded/error/stale`, `ledger_health`, `freshness`, `open_orders.rows` (stuck), `reconcile_alerts.rows`, `operator_alerts.rows`, `okx_live.positions` (exposure/uPnL). `dashboard.py:1254-1274` |
| `GET http://127.0.0.1:8891/health` | HTTP | none | receiver liveness only (no watchdog/queue detail). `webhook_receiver.py:3291-3300` |
| `logs/alerts.jsonl` | file (bounded tail) | n/a | raw `kind∈{operator,reconcile,state}` rows w/ `severity` — the only source of reconcile/watchdog/queue events not otherwise on an endpoint. `webhook_receiver.py:2046-2110` |
| `GET https://mxc-kinetic-crypto.replit.app/` | HTTPS | none | risk index (`pp_acc`, `pp_vel`, `regime`, `risk_state`) — **gated** by `risk_index_gate_enabled` in control-state. `docs/HERMX_AGENT_SYSTEM_DESIGN.md:500-525` |

Base URLs are configurable (default loopback), matching the existing `hermx-ops` client
convention (`skills/hermx-ops/lib/hermx_ops.py:30-31`):

```python
DASHBOARD_BASE = os.environ.get("HERMX_DASHBOARD_BASE", "http://127.0.0.1:8098")
RECEIVER_BASE  = os.environ.get("HERMX_RECEIVER_BASE",  "http://127.0.0.1:8891")
MXC_BASE       = os.environ.get("HERMX_MXC_BASE",       "https://mxc-kinetic-crypto.replit.app")
```

> **Note on `/api/monitor/alerts`.** The task brief references this endpoint; **it does not
> exist today** (verified — no `/api/monitor/*` route in `dashboard.py`). The daemon therefore
> reads reconcile/alert data from `GET /api` (`.reconcile_alerts.rows`, `.operator_alerts.rows`)
> plus `logs/alerts.jsonl`. §10.1 specs an **optional** companion endpoint to add later; the
> collector interface (§10) is written so that adding it is a drop-in swap, not a refactor.

### 3.2 HTTP client

Reuse the stdlib `urllib.request` pattern from `hermx_ops.py:47-69` (a `_get_json(base, path,
secret, timeout)` helper returning `(payload|None, error|None)`). Send the dashboard token as
`X-Dashboard-Token` (simplest of the three accepted forms; see `dashboard.py:2353-2372`). No
third-party HTTP library — `requests`/`httpx` are not in `requirements.txt` and tests mock
`urllib.request.urlopen` directly.

### 3.3 Intervals

| Env var | Default | Meaning |
|---|---|---|
| `HERMX_MONITOR_INTERVAL_SECONDS` | `60` | base loop interval |
| `HERMX_MONITOR_JITTER_PCT` | `0.1` | ± fraction of interval added as uniform jitter (avoids lock-step polling and thundering-herd on the dashboard) |
| `HERMX_MONITOR_HTTP_TIMEOUT_SECONDS` | `5` | per-request timeout (matches compose healthcheck `timeout: 5s`) |
| `HERMX_MONITOR_MXC_INTERVAL_SECONDS` | `300` | MXC risk polled less often than local state (external, slower-changing) |

Jitter uses a per-tick uniform draw. **Deterministic-clock caveat for tests:** the sleep
duration must be computed from an injectable `rng`/`now` so tests can pin it (the repo injects
the clock as a parameter rather than freezing time — `test_replay_startup.py`). Do not call
`Math.random`-equivalent inline; take `random.Random(seed)` from config or default.

### 3.4 Backoff on failure

Per-collector exponential backoff, stored in `state.backoff[collector]`:

- On a failed poll: `consecutive_failures += 1`; `next_attempt_at = now + min(base * 2**(n-1),
  cap)` with `base = HERMX_MONITOR_BACKOFF_BASE_SECONDS` (default `30`), `cap =
  HERMX_MONITOR_BACKOFF_CAP_SECONDS` (default `600`). A collector whose `next_attempt_at` is in
  the future is skipped this tick.
- On success: reset `consecutive_failures = 0`, `next_attempt_at = null`.
- **Meta-alert threshold:** when `consecutive_failures` for the dashboard poll reaches
  `HERMX_MONITOR_UNREACHABLE_THRESHOLD` (default `3`), emit `health:dashboard_unreachable`
  (severity `error`) so the operator learns the dashboard is down even though the very source
  the monitor would normally read is unavailable. This alert has its own suppression window so
  a sustained outage notifies once, not every tick.

Backoff is per-collector so an MXC outage never suppresses local health polling and vice versa.

---

## 4. Alert Deduplication

### 4.1 The `Alert` object

```python
@dataclass(frozen=True)
class Alert:
    category: str        # "health" | "reconcile" | "risk" | "summary"
    fingerprint: str     # stable identity across ticks (dedup key)
    severity: str        # "info" | "warning" | "error" | "critical"  (matches emit_operator_alert)
    title: str           # one-line human summary
    detail: dict         # structured context passed into the hermes prompt
    observed_at: str     # now_iso() when detected
```

`severity` reuses HermX's existing four-level scale (`emit_operator_alert`,
`webhook_receiver.py:2046-2079`).

### 4.2 Fingerprints (exact construction)

The fingerprint is the dedup identity. It must be **stable while the same condition persists**
and **distinct across genuinely different conditions**. Construction per category:

| Category | Fingerprint template | Example |
|---|---|---|
| health (executor) | `health:executor_degraded` | `health:executor_degraded` |
| health (stale/freshness) | `health:freshness_stale` | `health:freshness_stale` |
| health (dashboard down) | `health:dashboard_unreachable` | `health:dashboard_unreachable` |
| health (receiver down) | `health:receiver_unreachable` | `health:receiver_unreachable` |
| health (kill switch/pause) | `health:manual_pause` / `health:watchdog_degraded` | `health:watchdog_degraded` |
| reconcile (from alerts.jsonl) | `reconcile:{alert}:{symbol}:{cl_ord_id}` | `reconcile:UNKNOWN_RESOLVER_TIMEOUT:SOLUSDT:clord-abc` |
| reconcile (stuck open order) | `reconcile:stuck_order:{cl_ord_id}` | `reconcile:stuck_order:clord-abc` |
| queue saturation | `health:queue_saturation` | `health:queue_saturation` |
| risk (MXC) | `risk:{risk_state}:{symbol}` | `risk:elevated:SOLUSDT` |
| summary | `summary:weekly:{iso_week}` | `summary:weekly:2026-W27` |

Rules:
- Do **not** put a timestamp, a monotonically-increasing counter, or a free-text `reason` in
  the fingerprint — that would defeat dedup by making every observation unique (this is exactly
  the `normalize()`/`signal_id` non-determinism failure recorded in
  `.claude/rules/code-quality.md`). Volatile fields go in `detail`, never the fingerprint.
- For reconcile rows read from `alerts.jsonl`, derive `symbol`/`cl_ord_id` from the row's
  `detail`; if absent, omit that segment (`reconcile:{alert}:{symbol}` or `reconcile:{alert}`).
  The reconcile emitters already dedupe per `(symbol, cl_ord_id, state)` on the write side
  (`webhook_receiver.py` resolver), so the monitor's fingerprint aligns with the source's own
  identity.

### 4.3 Suppression windows

Per-category default windows (each individually env-overridable):

| Category | Env var | Default | Rationale |
|---|---|---|---|
| health | `HERMX_MONITOR_WINDOW_HEALTH_SECONDS` | `900` (15 min) | avoid re-paging a flapping executor every minute |
| reconcile | `HERMX_MONITOR_WINDOW_RECONCILE_SECONDS` | `1800` (30 min) | stuck-order/reconcile events are durable; hourly-ish is enough |
| risk | `HERMX_MONITOR_WINDOW_RISK_SECONDS` | `3600` (60 min) | risk regime changes slowly |
| summary | n/a | governed by cron boundary, not a window | fires once per ISO week |

### 4.4 The algorithm

```
for each alert A in this tick's collected alerts:
    last = state.last_notified_at.get(A.fingerprint)
    prev_sev = state.last_severity.get(A.fingerprint)
    window = window_for(A.category)

    escalated = severity_rank(A.severity) > severity_rank(prev_sev)   # info<warning<error<critical
    stale     = (last is None) or (epoch(now) - epoch(last) >= window)

    if stale or escalated:
        FRESH.append(A)                    # will be dispatched this tick
    # else: suppressed — do nothing

if FRESH is non-empty:
    batch = AlertBatch(FRESH)
    result = hermes_invoker.dispatch(batch)         # §5 — may fail-open
    now2 = now_iso()
    for A in FRESH:
        state.last_notified_at[A.fingerprint] = now2
        state.last_severity[A.fingerprint]   = A.severity
    state.updated_at = now2
    atomic_write(STATE_FILE, state)                 # persist AFTER dispatch
```

- **Escalation bypass:** a condition first seen at `warning` then observed at `error` re-notifies
  immediately even inside the window, because the operator's risk picture changed. De-escalation
  does not re-notify.
- **Persist-after-dispatch ordering & crash window:** `last_notified_at` is written *after* the
  dispatch attempt. If the process dies between `hermes` returning and the state write, the same
  batch re-notifies once on restart → **at-least-once**. The alternative (persist-before-dispatch)
  risks **at-most-once** — a lost notification if `hermes` then fails — which is worse for an
  alerting system. We deliberately choose at-least-once. The window (§4.3) bounds any further
  duplication.
- **`hermes` failure does not update `last_notified_at`** only if you prefer retry-until-success;
  the default spec **does** update on any completed dispatch attempt (success or non-zero exit)
  to prevent a persistently-broken `hermes` from re-paging every tick. Retry is instead handled
  by a separate per-fingerprint dispatch backoff if `HERMX_MONITOR_RETRY_ON_HERMES_FAILURE=true`
  (default `false`). Document the chosen default clearly in the implementation docstring.

### 4.5 Cron trigger (weekly summary) — idempotent

The weekly summary is not window-based; it is a boundary-crossing cron:

```
current_period = utc_now().strftime("%G-W%V")          # ISO year-week, e.g. "2026-W27"
scheduled = (utc_now().weekday() == HERMX_MONITOR_SUMMARY_DOW      # default 0 = Monday
             and utc_now().hour >= HERMX_MONITOR_SUMMARY_HOUR_UTC) # default 9
already_ran = state.cron.weekly_summary.last_period == current_period

if scheduled and not already_ran:
    emit Alert(category="summary", fingerprint=f"summary:weekly:{current_period}", severity="info", …)
    # on successful dispatch: state.cron.weekly_summary = {last_period, last_run_at}
```

Because `last_period` is persisted, a restart on Monday afternoon does not re-fire a summary
already sent Monday morning; and a daemon down all Monday fires the summary Tuesday (first tick
after it comes back, since `already_ran` is still false and `hour >= 9`). Set
`HERMX_MONITOR_SUMMARY_CATCHUP=false` to instead skip a missed week entirely.

---

## 5. Hermes Invocation

### 5.1 How the daemon shells out

Reuse the **exact** subprocess seam pattern from `webhook_receiver.py:2751-2762`
(`_advisor_agent_query`). Keep it a single, small, monkeypatch-able function so tests never
spawn a real agent:

```python
def _monitor_agent_query(prompt: str, skills: str) -> tuple[int, str, str]:
    """Transport seam (monkeypatched in tests). Runs hermes one-shot; returns
    (returncode, stdout, stderr). Never raises for a non-zero exit — the caller
    decides. Missing binary / timeout propagate as exceptions the caller catches
    and treats as fail-open (do not crash the loop)."""
    cmd = [HERMX_MONITOR_COMMAND, "-z", prompt, "--skills", skills]
    if HERMX_MONITOR_MODEL:
        cmd += ["-m", HERMX_MONITOR_MODEL]
    proc = subprocess.run(
        cmd,
        capture_output=True, text=True,
        timeout=HERMX_MONITOR_HERMES_TIMEOUT_SECONDS,
        cwd=HERMX_MONITOR_HERMES_CWD or None,      # deterministic AGENTS.md/rules loading
    )
    return proc.returncode, proc.stdout, proc.stderr
```

Facts baked into this (from research on the external `hermes_cli`):

- `hermes` is an **external binary on `PATH`** (`~/.hermes/hermes-agent/…`, reached via a shim
  at `~/.local/bin/hermes`). It is not in this repo. Depend on the command string (default
  `"hermes"`), and treat a missing binary as a normal runtime condition (fail-open), exactly as
  the advisor test `test_advisor_missing_hermes_binary_fails_open` requires.
- `-z/--oneshot` prints **only the final response** to stdout (no banner/spinner/tool previews)
  and **auto-bypasses approvals** internally (`oneshot.py` sets `HERMES_YOLO_MODE=1`), so you do
  **not** pass `--yolo`. A one-shot never hangs on a prompt.
- There is **no `--timeout` and no `--permission-mode` flag** — wall-clock is the caller's job
  via the subprocess `timeout` (that is the only thing preventing a hung agent from wedging the
  loop).
- `--skills` accepts a **comma-separated** string or repeated `-s`. Pass a CSV.
- Exit codes: `0` success; `1` agent error or empty response; `2` arg-validation. Empty stdout on
  `0` is possible per the CLI but the monitor should treat empty/whitespace stdout as a soft
  failure (log, don't crash).
- **`cwd`:** the advisor passes none (hermes inherits CWD and loads `AGENTS.md`/rules from it).
  For the daemon, set `HERMX_MONITOR_HERMES_CWD` (default `/opt/hermx` in systemd, `/app` in
  Docker) so skill/rule loading is deterministic regardless of where systemd starts it.

### 5.2 Skill selection per alert category

Default to **read-only, observational** skills (a passive daemon must never trigger a mutating
skill — and all mutating skills require an interactive `yes` an unattended daemon cannot give,
so they would stall anyway). Mapping (env-overridable via `HERMX_MONITOR_SKILL_<CATEGORY>`):

| Alert category | Default skill(s) | Why |
|---|---|---|
| health | `hermx-status` | read-only posture: armed?, mode, dashboard/receiver up, last alert. `skills/hermx-status/SKILL.md` |
| reconcile / stuck trade | `hermx-trace,hermx-positions` | trace the signal end-to-end + confirm exposure. Both read-only; report UNKNOWN, never "flat". |
| risk (MXC) | `hermx-status,signal-memory` | posture + "did we already act on this?" continuity context |
| summary (weekly) | `hermx-status,hermx-positions,signal-memory` | compose the digest from the observational trio |

Do **not** default to `hermx-control` (mixed read + **relay** — it can originate a trade to
`/webhook`) for passive monitoring. Never map any collector to `hermx-close`, `hermx-restart`,
`hermx-strategy-mode`, `hermx-upgrade`, or `emergency-stop`; those are human-in-the-loop
escalations. `hermx-ops` is **not** a loadable skill (no `SKILL.md`; it's the shared lib) — do
not pass it to `--skills`.

When a batch spans multiple categories, take the union of the mapped skills (dedup, cap at a
sane count) so one invocation can reason across all findings.

### 5.3 Prompt templating

Follow the advisor's convention (`_advisor_build_prompt`, `webhook_receiver.py:2739-2748`): a
fixed system preamble + an explicit skill hint + a compact, deterministic JSON payload
(`sort_keys=True`) + a clear instruction. The monitor's payload is the aggregated batch:

```python
MONITOR_SYSTEM_PROMPT = (
    "You are the HermX Monitor Daemon's advisory agent. The daemon has detected one or more "
    "NEW conditions on the HermX trading system and invoked you to assess them and produce an "
    "operator-facing summary. You are READ-ONLY: do not relay signals, place, close, or modify "
    "orders, and do not change any strategy mode. Use the loaded read-only skills to read live "
    "state before concluding. If a read is stale or unavailable, report UNKNOWN — never assume "
    "'flat' or 'healthy'."
)

def build_prompt(batch: AlertBatch) -> str:
    snapshot = {
        "detected_at": now_iso(),
        "alerts": [
            {"category": a.category, "severity": a.severity, "title": a.title,
             "fingerprint": a.fingerprint, "detail": a.detail}
            for a in batch.alerts
        ],
    }
    return (
        MONITOR_SYSTEM_PROMPT
        + "\n\nUse the loaded skill(s) to read current status/positions before deciding.\n"
        + "Detected conditions (FIXED — assess, do not restate verbatim):\n"
        + json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        + "\n\nProduce a concise operator summary: what happened, current confirmed state, and "
          "the single most useful next check or action for a human. Output ONLY that summary."
    )
```

Optionally, for machine-forwardable output, instruct strict JSON (like the advisor's
`_advisor_parse`, `webhook_receiver.py:2765-2792`) — e.g. `{"summary": "...", "severity": "...",
"suggested_action": "..."}` — and parse tolerantly (try `json.loads`, else extract the
outermost `{...}`). Default is free-text summary; set `HERMX_MONITOR_STRUCTURED_OUTPUT=true`
for JSON mode.

### 5.4 What happens to the output

`hermes -z` output is advisory text on stdout. The daemon:

1. **Always** logs the invocation and (truncated) output to `logs/monitor.log`.
2. **Optionally** forwards the summary to an operator sink, reusing the receiver's existing
   alert-webhook convention: if `HERMX_ALERT_WEBHOOK_URL` is set, POST the summary (short
   timeout, best-effort, never blocks/raises — mirrors `emit_operator_alert`,
   `webhook_receiver.py:2046-2079`). This is how the operator actually *sees* the notification
   if the loaded skill did not itself message out.
3. Appends a structured record to `logs/monitor.log` (and optionally its own
   `logs/monitor-notifications.jsonl` via `append_jsonl`) with `{ts, fingerprints, severity,
   skill, exit_code, forwarded: bool}` for auditability and post-hoc dedup verification.

### 5.5 Error handling if hermes fails (fail-open matrix)

| Failure | Detection | Daemon response |
|---|---|---|
| binary missing | `FileNotFoundError` from `subprocess.run` | log `WARN monitor: hermes binary not found`, treat batch as un-notified per the retry policy (§4.4), continue loop |
| timeout | `subprocess.TimeoutExpired` | log `WARN`, continue; increment a hermes-timeout counter for observability |
| non-zero exit | `returncode != 0` | log `WARN` with first 200 chars of stderr (advisor convention), mark dispatched (default) or retry (if enabled) |
| empty/whitespace stdout on exit 0 | `not stdout.strip()` | log `WARN empty hermes response`, treat as soft failure |
| unparseable JSON (structured mode) | `json.loads` + brace-extract both fail | log `WARN`, fall back to logging raw stdout as the summary |

In every case the loop continues. A `hermes` problem is never allowed to crash the daemon or
back-pressure onto HermX.

---

## 6. Configuration

### 6.1 Style — inline import-time env reads

Follow the repo's universal pattern (no `python-dotenv`, no settings class; every knob read at
module top via `os.environ.get(...)` with a literal default, `HERMX_`-prefixed):

```python
# numeric:  TYPE(os.environ.get("NAME", "<default>") or "<default>")
# boolean:  (os.environ.get("NAME") or "<default>").strip().lower() in {"1","true","yes"}
# string:   (os.environ.get("NAME") or "<default>").strip()
```

`.env` is loaded by the shell (`run.sh`) / systemd `EnvironmentFile` / compose `env_file`, not
by Python. Reading at import time is required so tests can `importlib.reload` or subprocess-import
the module with a controlled env (the `test_docker_state.py` contract).

### 6.2 Full env-var reference

| Env var | Default | Meaning |
|---|---|---|
| `HERMX_MONITOR_ENABLED` | `true` | master on/off (fail-safe: daemon exits 0 cleanly if false) |
| `HERMX_MONITOR_INTERVAL_SECONDS` | `60` | base loop interval |
| `HERMX_MONITOR_JITTER_PCT` | `0.1` | ± jitter fraction |
| `HERMX_MONITOR_HTTP_TIMEOUT_SECONDS` | `5` | per HTTP request |
| `HERMX_MONITOR_MXC_INTERVAL_SECONDS` | `300` | MXC poll cadence |
| `HERMX_DASHBOARD_BASE` | `http://127.0.0.1:8098` | dashboard base URL |
| `HERMX_RECEIVER_BASE` | `http://127.0.0.1:8891` | receiver base URL |
| `HERMX_MXC_BASE` | `https://mxc-kinetic-crypto.replit.app` | MXC risk dashboard |
| `HERMX_SECRET` | *(required if `HERMX_DASH_AUTH=true`)* | dashboard `X-Dashboard-Token` |
| `HERMX_MONITOR_COMMAND` | `hermes` | agent CLI command |
| `HERMX_MONITOR_MODEL` | `""` | optional `-m` model override |
| `HERMX_MONITOR_HERMES_TIMEOUT_SECONDS` | `60` | subprocess wall-clock bound |
| `HERMX_MONITOR_HERMES_CWD` | `""` → process CWD | deterministic rule/AGENTS.md loading |
| `HERMX_MONITOR_STRUCTURED_OUTPUT` | `false` | JSON vs free-text agent output |
| `HERMX_MONITOR_RETRY_ON_HERMES_FAILURE` | `false` | retry a batch whose dispatch failed |
| `HERMX_MONITOR_WINDOW_HEALTH_SECONDS` | `900` | health suppression window |
| `HERMX_MONITOR_WINDOW_RECONCILE_SECONDS` | `1800` | reconcile suppression window |
| `HERMX_MONITOR_WINDOW_RISK_SECONDS` | `3600` | risk suppression window |
| `HERMX_MONITOR_SUMMARY_DOW` | `0` | weekly summary day-of-week (0=Mon) |
| `HERMX_MONITOR_SUMMARY_HOUR_UTC` | `9` | weekly summary hour (UTC) |
| `HERMX_MONITOR_SUMMARY_CATCHUP` | `true` | fire a missed weekly summary on next boot |
| `HERMX_MONITOR_BACKOFF_BASE_SECONDS` | `30` | backoff base |
| `HERMX_MONITOR_BACKOFF_CAP_SECONDS` | `600` | backoff cap |
| `HERMX_MONITOR_UNREACHABLE_THRESHOLD` | `3` | consecutive poll fails → dashboard-down alert |
| `HERMX_MONITOR_SOURCES` | `health,reconcile,risk,summary` | comma-list of enabled collectors |
| `HERMX_ALERT_WEBHOOK_URL` | `""` | optional operator sink (reuses receiver's var) |
| `HERMX_DATA_DIR` | `ROOT` | state-file location (shared volume) |
| `SHADOW_ROOT` | repo root | root for `logs/` + default data dir |

### 6.3 `engine-config.json` block (parity with `advisor`)

`engine-config.json` already has an `advisor` block read with env override. Add a parallel
`monitor` block so operators can configure defaults without env, and env still wins (mirror
`_ADVISOR_CFG` resolution, `webhook_receiver.py:294-298`):

```json
{
  "strategy_engine": { "…": "…" },
  "advisor": { "enabled": false, "command": "hermes", "skills": "hermx-control", "model": "", "timeout_seconds": 30.0 },
  "monitor": {
    "enabled": true,
    "interval_seconds": 60,
    "command": "hermes",
    "model": "",
    "hermes_timeout_seconds": 60,
    "sources": ["health", "reconcile", "risk", "summary"],
    "skills": {
      "health": "hermx-status",
      "reconcile": "hermx-trace,hermx-positions",
      "risk": "hermx-status,signal-memory",
      "summary": "hermx-status,hermx-positions,signal-memory"
    },
    "windows": { "health": 900, "reconcile": 1800, "risk": 3600 },
    "summary": { "dow": 0, "hour_utc": 9, "catchup": true }
  }
}
```

Resolution precedence: **env var > `engine-config.json:monitor.*` > hard-coded default.**

### 6.4 MXC risk gate integration

The MXC RiskCollector honors the **local** toggle `risk_index_gate_enabled` stored in
`control-state.json` (same pattern as `symbol_pauses`; see `DEVIN_TRANSITION_PLAN.md:91-99`):

1. Read `control-state.json` (or `GET /api` → the flag if the dashboard surfaces it).
2. If `risk_index_gate_enabled` is false/absent → RiskCollector returns `[]` (fail-open: no
   risk alerts). This matches the planned `dashboard-risk` skill contract.
3. If true → `GET $HERMX_MXC_BASE/`, parse `pp_acc`/`pp_vel`/`regime`/`risk_state`
   (`docs/HERMX_AGENT_SYSTEM_DESIGN.md:500-525`). If `risk_state ∈ {elevated, high, risk_off}`,
   emit `risk:{risk_state}:{symbol}`.
4. Unreachable/parse-fail → `status: unknown` → emit nothing (degrade context, never
   false-alarm). The MXC read never blocks the tick (own timeout + backoff).

---

## 7. Deployment

### 7.1 Docker Compose service

The daemon ships in the **existing image** (already covered by `COPY src/ ./src/` in the
`Dockerfile`). Add one service to both `docker-compose.yml` and `docker-compose.host.yml`,
modeled on the hardened `dashboard` service (read-only root fs — the monitor writes only to its
state volume and logs):

```yaml
  monitor:
    image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}
    command: ["python", "src/monitor_daemon.py"]
    restart: always
    env_file: .env
    environment:
      - HERMX_DATA_DIR=/app/data          # MUST match receiver/dashboard (shared state volume)
      - HERMX_DASHBOARD_BASE=http://dashboard:8098   # reach dashboard by service name
      - HERMX_RECEIVER_BASE=http://receiver:8891
    depends_on:
      - dashboard
      - receiver
    read_only: true
    cap_drop: [ALL]
    tmpfs: [/tmp]
    volumes:
      - ./engine-config.json:/app/engine-config.json:ro
      - hermx-data:/app/logs               # rw: writes logs/monitor.log (+ optional jsonl)
      - hermx-state:/app/data              # rw: writes monitor-state.json
    healthcheck:
      test: ["CMD", "python", "-c", "import sys,os,time;
              p='/app/data/monitor-state.json';
              sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 300 else 1)"]
      interval: 60s
      timeout: 5s
      start_period: 30s
      retries: 3
```

Notes:
- **Networking:** in bridge compose (`docker-compose.yml`), services reach each other by
  **service name** (`dashboard:8098`, `receiver:8891`), so set `HERMX_DASHBOARD_BASE`/`_RECEIVER_BASE`
  accordingly. In `docker-compose.host.yml` (`network_mode: host`) keep the loopback defaults.
- **Healthcheck without an HTTP server:** the default uses a state-file freshness probe (the
  monitor touches `monitor-state.json` at least every `interval`; if it's >5 min stale the loop
  is wedged). If you add the optional `/health` endpoint (§7.4), switch to `curl -sf
  http://127.0.0.1:<port>/health` like the other services.
- `hermx-data` is mounted **rw** here (the monitor writes `logs/monitor.log`), unlike the
  dashboard which mounts it `:ro`. Confirm the volume permits it (it does — `uid/gid 10001`
  owns `/app/logs`).
- **`hermes` in Docker:** the external `hermes` binary is **not in the image**. If the monitor
  must run inside the container, either bake/install the hermes agent into the image or run the
  monitor as a **host** systemd service (§7.2) where `hermes` is already on `PATH`. Recommended:
  **run the monitor on the host via systemd** (it needs the host's `hermes`), and use the Docker
  service only if the hermes agent is added to the image. Call this out in `INSTALL.md`.

### 7.2 systemd unit (`deploy/hermx-monitor.service`)

Copy `deploy/hermx-dashboard.service` and change `Description`/`SyslogIdentifier`/`ExecStart`.
This is the **recommended** deployment (host has `hermes` on `PATH`):

```ini
[Unit]
Description=HermX Monitor Daemon
After=network-online.target hermx-receiver.service hermx-dashboard.service
Wants=network-online.target

[Service]
Type=simple
User=hermx
Group=hermx
WorkingDirectory=/opt/hermx
EnvironmentFile=/opt/hermx/.env
ExecStart=/opt/hermx/.venv/bin/python src/monitor_daemon.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermx-monitor

# prevent runaway restarts
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
```

Conventions preserved from the existing units: `Type=simple`, `User/Group=hermx`,
`WorkingDirectory=/opt/hermx`, `EnvironmentFile=/opt/hermx/.env`, **absolute venv interpreter +
relative script path**, `Restart=always`/`RestartSec=5`, journal logging, runaway guard. The
`After=` ordering additionally waits for receiver+dashboard so the monitor's first poll has
something to talk to (a startup miss is harmless — backoff handles it — but this reduces noise).

**Important:** the systemd unit runs as user `hermx`, but `hermes` lives under a specific user's
home (`~/.hermes`, `~/.local/bin/hermes`). Ensure `hermes` is on the `hermx` user's `PATH`
(add `Environment=PATH=…` or set `HERMX_MONITOR_COMMAND` to the absolute shim path,
e.g. `/home/<user>/.local/bin/hermes`) and that the hermes agent is installed/authorized for
that user. This is the single most likely deployment footgun — document it in `INSTALL.md`.

### 7.3 Install/deploy script integration

- `deploy/install-services.sh`: add `cp deploy/hermx-monitor.service /etc/systemd/system/` and a
  `systemctl enable --now hermx-monitor` line (after receiver/dashboard).
- `deploy/deploy.sh`: add `hermx-monitor` to `restart_services()`. It should **not** be in the
  health-probe gate that triggers auto-rollback (the monitor is non-critical; a monitor failure
  must not roll back a good receiver/dashboard deploy). Optionally add a soft post-restart check
  that only warns.

### 7.4 Optional self `/health` endpoint

For a cleaner compose healthcheck, run a tiny `ThreadingHTTPServer` on a daemon thread (pattern:
`dashboard.py:2560`; tested via ephemeral-port servers in `test_phase2_webhook_security.py`)
exposing `GET /health` → `{"ok": true, "service": "hermx_monitor", "last_tick_at": …,
"consecutive_hermes_failures": N, "sources": [...]}`. Bind `HERMX_BIND_HOST` (default
`127.0.0.1`) on a new `HERMX_MONITOR_PORT` (suggest `8099`). This is optional — the state-file
freshness probe (§7.1) works without opening a port.

### 7.5 Logging

Reuse the receiver's `logging.basicConfig` recipe (`webhook_receiver.py:449-455`): root logger,
`format="%(asctime)s %(message)s"`, `datefmt="%Y-%m-%dT%H:%M:%SZ"`, UTC `Formatter.converter`
override, dual handlers — `FileHandler(LOG_DIR / "monitor.log")` + `StreamHandler(sys.stdout)`
(stdout → journal under systemd, → container logs under Docker). Log at least: each tick's
collector results counts, every dispatch (fingerprints + skill + exit code), every fail-open
event, and backoff transitions.

---

## 8. Testing Strategy

All tests go in `tests/test_monitor_*.py`, **unmarked** so they run in the offline gate
(`pytest -m "not integration and not okx_paper and not kucoin_paper and not hyperliquid_paper"`,
used by `run.sh` and `deploy.sh`). Stdlib-only mocking (`unittest.mock` + pytest `monkeypatch` +
`urllib`/`subprocess`); **inject the clock as a parameter**, never freeze time (repo convention).

### 8.1 Unit tests

| Area | What to assert | Pattern |
|---|---|---|
| Fingerprint stability | same condition → same fingerprint across ticks; volatile fields excluded | pure-function tests |
| Dedup window | first observation dispatches; second within window suppressed; after window re-dispatches | pass `now` explicitly |
| Escalation bypass | warning→error inside window re-dispatches; de-escalation does not | table test |
| Cron idempotency | fires once per ISO week; restart same week does not re-fire; missed week fires on next boot (catchup) | pass `now`, pre-seed `state.cron` |
| Backoff | consecutive failures grow `next_attempt_at`; success resets; skip while backed off | pass `now` |
| Config precedence | env > engine-config.json > default | `monkeypatch.setenv` + temp config file |
| State load fail-safe | corrupt/missing/non-dict file → `default_monitor_state()` | write junk to `tmp_path` |
| Atomic write | temp file replaced; no partial file on simulated write error | `tmp_path` + fault injection |

### 8.2 Integration tests (mocked dashboard)

- **Fake dashboard/receiver via `monkeypatch.setattr(urllib.request, "urlopen", fake)`** — the
  `test_hermx_ops.py:29-66` `_FakeResp` pattern. Feed canned `/health` and `/api` payloads
  (healthy, executor-degraded, stuck-open-order, reconcile-alert-present) and assert the right
  `Alert`s are produced. Support URL-conditional responses and raising `urllib.error.HTTPError`
  for the dashboard-down path.
- **Full tick, mocked hermes:** monkeypatch `_monitor_agent_query` to a stub returning
  `(0, "OK summary", "")` (and error variants), run one `tick(now)`, assert: which fingerprints
  dispatched, that `monitor-state.json` on `tmp_path` now records `last_notified_at`, and that a
  second identical tick within the window dispatches nothing.
- **Restart simulation:** run a tick, write state, then construct a fresh daemon instance
  pointed at the same `tmp_path` state file (mirrors `test_replay_startup.py` / the `reload_wr`
  cold-boot idiom) and assert no re-notification inside the window and the cron `last_period` is
  respected.
- **MXC gate:** with `risk_index_gate_enabled` false → RiskCollector yields `[]`; true +
  elevated payload → one `risk:*` alert; MXC unreachable → `[]` (fail-open).

### 8.3 Env-contract test (subprocess import)

Copy the `test_docker_state.py::_resolve` pattern: import `monitor_daemon` in a clean subprocess
with `HERMX_DATA_DIR`/`SHADOW_ROOT` overrides and assert `STATE_FILE`, `MONITOR_LOG`, base URLs,
and interval resolve correctly — including that `STATE_FILE` relocates under `HERMX_DATA_DIR`
and `MONITOR_LOG` stays under `ROOT/logs`.

### 8.4 Optional-endpoint test

If §7.4 is implemented, drive the `/health` server over an ephemeral-port
`ThreadingHTTPServer(("127.0.0.1", 0), Handler)` (the `test_phase2_webhook_security.py` idiom)
and assert the payload shape and staleness reporting.

---

## 9. Failure Modes

| Failure | Detection | Behavior | Rationale |
|---|---|---|---|
| Dashboard down / unreachable | HTTP error/timeout on `/api`,`/health` | back off that collector; after `UNREACHABLE_THRESHOLD` consecutive fails emit `health:dashboard_unreachable` (own window). Never crash. | an outage is a monitorable event, and the monitor must survive the very thing it watches |
| Receiver down | `/health` on 8891 fails | emit `health:receiver_unreachable`; continue reading dashboard/ledger | receiver and dashboard fail independently |
| `alerts.jsonl` missing/torn tail | bounded tolerant read (`read_jsonl_stats`) reports `truncated_tail`/`skipped` | tolerate a torn trailing line (append-in-flight); count skips; advance cursor only past cleanly-parsed rows | matches the ledger's own crash-tolerance contract |
| `hermes` binary missing | `FileNotFoundError` | log WARN, fail-open, continue (batch handled per retry policy) | hermes is external + optional; its absence must not stop monitoring |
| `hermes` timeout | `subprocess.TimeoutExpired` | log WARN, continue; the subprocess is killed | hard wall-clock bound is the only thing preventing a hung agent from wedging the loop |
| `hermes` non-zero exit / empty stdout | `returncode`/`stdout` check | log WARN (stderr[:200]); mark dispatched (default) so a persistent break doesn't re-page every tick | avoid notification storms from a broken agent |
| `monitor-state.json` corrupt | JSON/parse error on load | fall back to `default_monitor_state()` (empty maps) | self-healing; worst case is one fresh notification per active condition |
| Disk full on state write | `OSError` from atomic write | log ERROR, keep in-memory state, retry next tick; do **not** crash | matches `_atomic_json_dump` propagating `OSError` — but the daemon catches it (unlike the money path, which fails closed) |
| MXC unreachable / unparseable | HTTP/parse error | RiskCollector yields `[]` (fail-open), own backoff | never false-alarm on an external dependency |
| Clock skew after outage | n/a (design) | freshness/staleness of *trades* is judged by the dashboard on `tv_time`, not the monitor's clock; the monitor's own windows use wall-clock deltas, which are self-correcting | inherits the "freshness bounded on bar time, never server time" rule |
| Two monitor instances running | (operational) | last-writer-wins on the atomic state file → at worst duplicate notifications, never corruption. Optionally add a PID/lock file (mirror `~/.hermes/gateway.pid`) to enforce single-instance | atomic `os.replace` guarantees no torn file |

**Invariant:** every failure path logs and continues. The daemon has exactly one legitimate
clean exit (`HERMX_MONITOR_ENABLED=false` → exit 0). Nothing the daemon does can block, mutate,
or fail the receiver/dashboard/money path.

---

## 10. Future Extensibility

### 10.1 Collector (`AlertSource`) plugin interface

New alert sources are added **without touching** the loop, dedup, aggregation, or dispatch
code. Each collector implements one method:

```python
class AlertSource(Protocol):
    name: str                                  # backoff key + config key
    def collect(self, ctx: TickContext) -> list[Alert]: ...

@dataclass
class TickContext:
    now: datetime                              # injected clock (testability)
    http: HttpClient                           # shared, timeout-bounded
    state: dict                                # read-only view of monitor state
    config: MonitorConfig
```

Registration is data-driven from `HERMX_MONITOR_SOURCES` / `engine-config.json:monitor.sources`:

```python
REGISTRY = {
    "health":    HealthCollector,
    "reconcile": ReconcileCollector,
    "risk":      RiskCollector,
    "summary":   SummaryCollector,
}
active = [REGISTRY[name]() for name in config.sources if name in REGISTRY]
```

Adding, e.g., a **slippage** or **daily-PnL-drawdown** source is: write `SlippageCollector`,
add it to `REGISTRY`, add `"slippage"` to `sources`, add a suppression-window default and a
skill mapping. No other file changes. The dedup/dispatch machinery treats every `Alert`
uniformly by `category`/`fingerprint`/`severity`.

Candidate future sources (already have data behind them):
- **`drawdown`** — enforce `max_daily_loss_usd` (declared in strategy files, `docs/STRATEGIES.md:74`,
  but **not enforced anywhere in `src/` today** — a real gap the monitor could cover as an
  advisory, computed from `okx_live.positions[*].realized_pnl`/`upl`).
- **`advisor_veto`** — surface a run of advisor `skip` verdicts / low `score` from
  `pipeline.jsonl` (`stage="advisor"`), i.e. the trading system repeatedly self-vetoing.
- **`schema_error_spike`** — `ALERT_SCHEMA_METRICS` / validation-error rate in `pipeline.jsonl`
  (a TradingView/webhook health-domain signal per `docs/HEALTH_AND_RECOVERY.md:14`).
- **`queue_lag`** — queue depth / oldest-item age if a receiver metrics endpoint is added.

### 10.2 New dashboard endpoint `/api/monitor/alerts` (optional companion)

To reduce the monitor's coupling to `logs/alerts.jsonl`, add a **read-only** endpoint on the
dashboard that surfaces the alert stream + reconcile status directly. This is a drop-in new
branch in `Handler.do_GET` (`dashboard.py:2436`), gated by `self._dashboard_auth_ok()`, mirroring
the `/api` block:

```python
elif path in {"/api/monitor/alerts", "/dashboard/api/monitor/alerts"}:
    if not self._dashboard_auth_ok():
        self._auth_challenge(); return
    payload = monitor_alerts_payload(since=qs.get("since"))   # tail of alerts.jsonl, filtered
    self.send_bytes(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
```

Response shape (proposed): `{"ok": true, "generated_at": …, "alerts": [{ts, kind, alert,
severity, detail}], "cursor": "<last_ts>", "reconcile": {"startup_complete": bool,
"startup_at": …}}`. The `RECEIVER` currently holds reconcile-startup status only in module
globals (`RECONCILE_STARTUP_COMPLETE`/`_AT`, `webhook_receiver.py:273-274`); exposing it here (or
via a receiver `/status` endpoint) would let the monitor drop its direct-ledger read entirely.
The `ReconcileCollector` should **prefer this endpoint if it returns 200** and fall back to the
ledger read otherwise — so shipping the endpoint later requires no monitor change.

### 10.3 A dedicated `hermx-monitor` / `dashboard-risk` skill

There is no observational "digest"/"risk-read" skill today (`dashboard-risk` and `kronos-validate`
are referenced in `related_skills` but **do not exist on disk**). Two clean extension points:

- Author `skills/dashboard-risk/SKILL.md` (the planned MXC risk-read skill,
  `docs/HERMX_AGENT_SYSTEM_DESIGN.md:496-525`) following the frontmatter schema in
  `skills/hermx-status/SKILL.md`. The RiskCollector could then delegate MXC parsing to the skill
  instead of scraping in Python.
- Author a purpose-built `skills/hermx-monitor/SKILL.md` (read-only) that composes
  `status`+`positions`+`signal-memory` reads into a standardized operator digest, so the
  daemon passes a single skill and the digest format lives in the skill, not the prompt string.

Both are additive; the daemon only changes its skill-mapping config (§6.3), no code.

---

## 11. Open Questions / Decisions for the Implementer

1. **Where does the operator actually *see* the notification?** Options: (a) the loaded skill
   messages out via hermes' own tools; (b) the daemon forwards hermes stdout to
   `HERMX_ALERT_WEBHOOK_URL`; (c) both. The spec supports (b) out of the box; confirm the desired
   channel (Telegram via gateway? webhook?) before implementation.
2. **Docker vs host for the daemon.** Because `hermes` is a host binary not in the image, the
   **recommended** deployment is host systemd. Decide whether to also bake hermes into the image
   for the Docker path (§7.1), or ship the monitor host-only.
3. **Retry policy default** (`HERMX_MONITOR_RETRY_ON_HERMES_FAILURE`) — spec defaults to
   "mark-dispatched, no retry" to avoid storms. Confirm this is the desired trade-off vs.
   guaranteed delivery.
4. **Single-instance enforcement** — add a PID/lock file (mirror `~/.hermes/gateway.pid`) or rely
   on systemd being the sole launcher? Atomic state writes make double-run safe (only duplicate
   notifications), so a lock is optional.
5. **Add the `/api/monitor/alerts` endpoint now or later?** The daemon works without it (ledger
   read). Shipping it (§10.2) is a small dashboard change that improves decoupling — schedule it
   as a fast-follow.

---

## Appendix A — Key source references (verified)

| Concern | Source |
|---|---|
| Dashboard routing (stdlib http.server, manual path match) | `src/dashboard.py:2436-2473`, `:2557-2560` |
| `/health` payload (arm/kill-switch block) | `src/dashboard.py:1293-1308` |
| `/api` payload (executor, ledger_health, freshness, open_orders, reconcile_alerts) | `src/dashboard.py:1254-1274`, executor verdict `:1127-1135` |
| Dashboard auth (`X-Dashboard-Token` / Bearer / Basic) | `src/dashboard.py:2353-2372` |
| Ports (8098 dashboard, 8891 receiver) | `dashboard.py:40`, `webhook_receiver.py:91` |
| Advisor subprocess seam (copy this) | `src/webhook_receiver.py:2739-2792` (`_advisor_agent_query`, `_advisor_build_prompt`, `_advisor_parse`) |
| Atomic JSON write (`_atomic_json_dump`, `_fsync_dir`) | `src/webhook_receiver.py:1295-1317` |
| Fail-safe state load (`load_control_state`) | `src/webhook_receiver.py:1181-1202` |
| `now_iso()` microsecond-ISO timestamp | `src/webhook/timeutil.py:18-19` |
| Data-dir resolution (`HERMX_DATA_DIR`) | `src/webhook_receiver.py:126-152`; `dashboard.py:38,56` |
| Unified alert ledger (`alerts.jsonl`, kinds/severity) | `src/webhook_receiver.py:2046-2110`, `:152` |
| Order states / "stuck" definition | `src/webhook_receiver.py:195-203`, `:1453-1466`, resolver `:2313-2396` |
| Reconcile alert kinds | `src/webhook_receiver.py:235-242` |
| Kill switch / control-state | `src/hermx_shared.py:54-72`; control-state default `webhook_receiver.py:1149-1160` |
| MXC risk (`pp_acc`/`pp_vel`/`regime`/`risk_state`, gate flag) | `docs/HERMX_AGENT_SYSTEM_DESIGN.md:500-525`; `DEVIN_TRANSITION_PLAN.md:91-99` |
| hermes CLI (`-z`, `--skills`, external binary) | external `hermes_cli/_parser.py`; usage `docs/HERMES_AGENT_DESIGN.md:160`, `ARCHITECTURE.md:295` |
| Read-only skills catalog | `skills/hermx-status|positions|trace/SKILL.md`, `skills/signal-memory/SKILL.md` |
| Compose services / volumes / networking | `docker-compose.yml`, `docker-compose.host.yml` |
| Dockerfile (python:3.11-slim, uid 10001, CMD override) | `Dockerfile` |
| systemd unit conventions | `deploy/hermx-dashboard.service`, `deploy/install-services.sh`, `deploy/deploy.sh` |
| Run model (`python src/x.py`, venv resolution) | `run.sh:80-93,459-465` |
| Test conventions (conftest, tmp_path, urlopen mock, subprocess import, injected clock) | `tests/conftest.py`, `test_docker_state.py`, `test_replay_startup.py`, `test_hermx_ops.py`, `pytest.ini` |
```
