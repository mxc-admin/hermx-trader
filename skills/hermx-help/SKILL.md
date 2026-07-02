---
name: hermx-help
description: Use when the operator asks what HermX slash commands exist, how one works, or wants usage help — e.g. "/help", "/help close", "what commands are there", "how do I use /strategy-mode". Prints a human-readable overview of all HermX commands, or a detailed guide for one. Read-only text: no HTTP calls, no file writes, no mutations.
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, hermx, help, docs, read-only, operations]
    related_skills: [hermx-control, hermx-status, hermx-positions, hermx-strategy-list, hermx-trace, hermx-strategy-mode, hermx-close, emergency-stop, hermx-restart, hermx-upgrade]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
---

# /help — HermX slash-command help

**Read-only text.** `/help` or `/help <command>`. Emits nothing but formatted
markdown — **no HTTP calls, no file writes, no mutations**. This SKILL.md *is* the
source of truth; you do not need the helper library or contract loaded to answer.

For the canonical endpoint/auth/response detail behind each command, point the
operator at the shared helper
[`../hermx-ops/lib/hermx_ops.py`](../hermx-ops/lib/hermx_ops.py) and the API contract
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## Input parsing
- **No arg → overview.** Print the "All commands" section below.
- **One arg → detail.** Print that command's "Command detail" block.
- Match **case-insensitively** and **with or without the leading `/`**:
  `close`, `/close`, `CLOSE`, `/Close` all resolve to `/close`.
- Accept common aliases: `estop`/`kill` → `/emergency-stop`, `strategies`/`list`
  → `/strategy-list`, `mode` → `/strategy-mode`, `deploy` → `/upgrade`.
- Unknown arg → say so, then print the overview so the operator can pick.

## All commands (`/help`)

Nine commands: four read-only diagnostics, five guarded mutations. None sets an
order size; none calls an exchange directly. Every mutation dry-runs then needs an
explicit `yes`.

**Read-only**
- **`/status`** — armed?, mode, dashboard/receiver up, last alert, strategy count.
  `/status`
- **`/positions`** — open positions: side, size, entry, mark, UPL, leverage.
  `/positions`
- **`/strategy-list`** — every strategy with `file_mode` vs `effective_mode` + paused.
  `/strategy-list`
- **`/trace`** — follow one signal intake → dedupe → pipeline → exec, joined on
  `received_at`. `/trace BTCUSDT`

**Mutating (dry-run + explicit `yes`)**
- **`/strategy-mode`** — set a per-strategy override (pause/resume/demo/live).
  `/strategy-mode btcusdt_duo_base_dev_2h demo`
- **`/close`** — flatten ONE position, reduce-only, sizeless, via `/api/close`.
  `/close BTCUSDT`
- **`/emergency-stop`** — layered stop: kill / flatten all / demo one / pause a symbol.
  `/emergency-stop flatten`
- **`/restart`** — restart a down dashboard/receiver via systemd (with fallbacks).
  `/restart`
- **`/upgrade`** — pull + deps + UI + tests + restart with auto-rollback.
  `/upgrade`

Ask `/help <command>` for syntax, guards, and examples on any one.

## Command detail (`/help <command>`)

### `/status`
- **Type:** read-only.
- **Does:** loopback reads of dashboard `/health` + `/api` and receiver `/health` +
  `/latest`; reports armed, mode, reachability, last alert, strategy count, freshness.
- **Guards:** any read failure surfaces **DOWN/UNKNOWN**; `armed:false` only when
  `/health` returned `ok` with `arm.armed == false`. Freshness is bounded on bar time
  (`tv_time`), never server clock. No POST ever issued.
- **Examples:**
  - `rtk claude -p "/status" --permission-mode dontAsk`
  - "is HermX armed right now?" → `/status`
  - "when did the last alert land?" → `/status` (reads `/latest`)

### `/positions`
- **Type:** read-only.
- **Does:** reads `/api` → `okx_live.positions`; renders SYMBOL/SIDE/POS/AVG_PX/MARK/
  UPL/LEV/MGN.
- **Guards:** read failure, `okx_live.ok == false`, `executor.degraded`, or
  `freshness.no_data` → **UNKNOWN**. Only a healthy, non-degraded, empty book is FLAT —
  never infer "flat" from a failed read.
- **Examples:**
  - `rtk claude -p "/positions" --permission-mode dontAsk`
  - "what are we holding?" → `/positions`
  - Executor degraded → renders UNKNOWN, not an empty (flat) table.

### `/strategy-list`
- **Type:** read-only.
- **Does:** lists `strategies/*.json` folded with `control-state.json`; shows
  `file_mode` vs `effective_mode` (override > pause > file) and `paused`.
- **Guards:** read-only; a corrupt/missing strategy file surfaces UNKNOWN fields
  instead of crashing. No writes.
- **Examples:**
  - `rtk claude -p "/strategy-list" --permission-mode dontAsk`
  - "which strategies are live vs demo?" → `/strategy-list`
  - "is anything paused?" → `/strategy-list` (`paused` column)

### `/trace`
- **Type:** read-only.
- **Syntax:** `/trace <received_at | symbol>`
- **Does:** joins `raw-webhooks.jsonl` → `signals.jsonl` → `pipeline.jsonl` →
  `executions.jsonl` on `received_at`; shows where a signal stopped.
- **Guards:** time-less payloads (no `tv_time`) are flagged `time_less` and their
  non-deterministic `signal_id` is **never re-derived**. Intake-without-dedupe means
  "queued, not yet dequeued" — not an error. Read-only.
- **Examples:**
  - `rtk claude -p "/trace BTCUSDT" --permission-mode dontAsk`
  - `rtk claude -p "/trace 2026-07-02T14:03:12.481922Z" --permission-mode dontAsk`
  - "why didn't the last BTC alert fire?" → `/trace BTCUSDT`

### `/strategy-mode`
- **Type:** mutating.
- **Syntax:** `/strategy-mode <name-or-id> <pause|resume|demo|live>` (`resume` →
  control `clear`).
- **Does:** resolves the arg to a `strategy_id`, previews current→target, then
  `POST {dashboard}/api/control/strategy/{id}`; writes only the control override —
  it never edits `strategies/*.json`.
- **Guards:** always dry-runs first; **`live` requires explicit `yes`**; an ambiguous
  arg stops with candidates; transport/5xx → UNKNOWN (never "applied"); re-reads to
  confirm the new `effective_mode`.
- **Examples:**
  - `rtk claude -p "/strategy-mode btcusdt_duo_base_dev_2h demo" --permission-mode dontAsk`
  - `rtk claude -p "/strategy-mode btc pause" --permission-mode dontAsk`
  - Promote to live → shows preview, waits for `yes` before the POST.

### `/close`
- **Type:** mutating, reduce-only.
- **Syntax:** `/close <symbol|strategy>`
- **Does:** confirms the position via `/api`, previews side + size, and on `yes` sends
  `POST {receiver}/api/close` with `{symbol, strategy_id, operator, reason}`.
- **Guards:** UNKNOWN read → **refuses to close** (won't assume flat); **no size field**
  in the body (server derives the reduce-only close); never routes via `/webhook`; a
  transport/5xx outcome is UNKNOWN, never reported as "flat".
- **Examples:**
  - `rtk claude -p "/close BTCUSDT" --permission-mode dontAsk`
  - `rtk claude -p "/close btcusdt_duo_base_dev_2h" --permission-mode dontAsk`
  - Stale executor → refuses; healthy + no position → "nothing to close".

### `/emergency-stop`
- **Type:** mutating (layered).
- **Syntax:** `/emergency-stop kill|flatten|demo <id>|pause-symbol <sym>`
- **Does:**
  - `kill` — global live kill: outputs the `HERMX_LIVE_TRADING=false` + receiver-restart
    steps, then confirms via `/health` `arm.kill_switch_engaged == true`.
  - `flatten` — closes every open position reduce-only (one `/api/close` each), then
    re-reads to verify flat.
  - `demo <id>` — forces one strategy to sandbox (mutating twin of `/strategy-mode <id>
    demo`).
  - `pause-symbol <sym>` — adds the symbol to `control-state.json` `symbol_pauses` via
    the atomic safe updater.
- **Guards:** every action dry-runs, needs explicit `yes`, logs before/after positions,
  and treats UNKNOWN reads as indeterminate — never "flat"/"safe". `kill` stays
  unconfirmed until `/health` returns `true`.
- **Examples:**
  - `rtk claude -p "/emergency-stop flatten" --permission-mode dontAsk`
  - `rtk claude -p "/emergency-stop kill" --permission-mode dontAsk`
  - `rtk claude -p "/emergency-stop pause-symbol BTCUSDT" --permission-mode dontAsk`

### `/restart`
- **Type:** mutating (lifecycle).
- **Syntax:** `/restart` or `/restart force`
- **Does:** health-checks `{dashboard}/health` + `{receiver}/health`. Both UP → does
  nothing. One/both DOWN → previews the plan and on `yes` restarts via `systemctl
  restart hermx-dashboard hermx-receiver` (preferred), else `bash run.sh --skip-tests`,
  else `docker compose restart`; polls `/health` up to 30s. `force` restarts both
  regardless of state.
- **Guards:** never restarts without explicit `yes` (`force` needs `yes, restart both`);
  a live host is warned open positions may be briefly unmonitored; failed reads →
  DOWN/UNKNOWN, never "up"; process-lifecycle only — no strategy edits, no `/webhook`,
  no `/api/close`. The `run.sh` fallback forces `HERMX_LIVE_TRADING=false` unless
  `--honor-submit`.
- **Examples:**
  - `rtk claude -p "/restart" --permission-mode dontAsk`
  - `rtk claude -p "/restart force" --permission-mode dontAsk`
  - Both services up → reports `up`, changes nothing.

### `/upgrade`
- **Type:** mutating (deploy + lifecycle).
- **Syntax:** `/upgrade` or `/upgrade --no-pull` or `/upgrade --no-tests` or
  `/upgrade --no-ui`
- **Does:** runs `bash deploy/deploy.sh` — snapshot state + rollback point, pull latest
  (unless `--no-pull`; pip install always runs), build the UI (unless `--no-ui`), run
  offline tests (unless `--no-tests`), restart both services, then health-check. Health
  failure → **auto-rollback** to prior HEAD; polls `/health` up to 30s.
- **Guards:** always dry-runs first (current HEAD + plan) and requires explicit `yes`;
  a live host is warned positions may be briefly unmonitored; only operator-supplied
  flags pass through; non-zero exit or health-stays-down → FAILED/UNKNOWN (never
  "upgraded OK"), with a pointer to the run's `.deploy-backups/` snapshot.
- **Examples:**
  - `rtk claude -p "/upgrade" --permission-mode dontAsk`
  - `rtk claude -p "/upgrade --no-tests" --permission-mode dontAsk`
  - Health check fails post-deploy → deploy.sh rolls back; report ROLLED BACK.

## Shared invariants (apply to every command)
- **UNKNOWN, never "flat".** Any read failure, `okx_live.ok == false`,
  `executor.degraded`, or `freshness.no_data` → UNKNOWN. Only a healthy, non-degraded,
  empty book is genuinely FLAT.
- **Mutations require preview + confirmation** — dry-run first, explicit `yes` before
  any write.
- **`/close` is reduce-only and sizeless**; it never routes via `/webhook`.
- **Live transitions require explicit `yes`** — no implicit demo→live.
- **Sizing is owned by the execution layer** (from the strategy file); a skill never
  sets or suggests a size.
- **Freshness is bounded on bar time (`tv_time`)**, not server clock.

## Rules
- This skill is **pure text**: it issues no HTTP request, writes no file, mutates
  nothing. If asked to actually *run* a command, defer to that command's own skill.
- Never invent commands or flags — only the nine above exist.
- Keep output terse and chat-formatted: markdown bullets and short paragraphs.

## Verification checklist
- [ ] `/help` (no arg) lists all **nine** commands, each with a one-liner + example.
- [ ] `/help close`, `/help /close`, `/help CLOSE` all resolve to the `/close` detail.
- [ ] Aliases (`estop`, `kill`, `deploy`, `mode`, `list`) map to the right command.
- [ ] An unknown arg reports "unknown command" then falls back to the overview.
- [ ] No HTTP call, no file write, no mutation is performed by this skill.
- [ ] Detail blocks state each command's type, syntax, guards, and 2–3 examples.
