# HermX Slash Commands — Operator Quick Reference

One-page reference for the HermX slash-command skills: read-only status/diagnostics plus guarded, reduce-only mutations over loopback. No command ever calls an exchange directly or sets an order size.

## Quick reference

| Command | Type | What it does | Safety guard |
|---|---|---|---|
| `/help` | read | Lists all 9 commands, or explains one (`/help <command>`) with syntax + examples | Pure text: no HTTP, no writes, no mutations |
| `/status` | read | Armed?, mode, dashboard/receiver up, last alert, strategy count | Read failure → DOWN/UNKNOWN, never "not armed" |
| `/positions` | read | Open positions: side, size, entry, mark, UPL | Failed/stale/degraded read → UNKNOWN, never "flat" |
| `/strategy-list` | read | All strategies + `file_mode` vs `effective_mode`, paused | Corrupt strategy file → UNKNOWN fields, no crash |
| `/trace` | read | Follow one signal intake→dedupe→pipeline→exec, joined on `received_at` | Never re-derives a time-less `signal_id`; read-only |
| `/strategy-mode <name-or-id> <pause\|resume\|demo\|live>` | mutate | Sets a per-strategy override via dashboard control endpoint | Dry-run first; `live` needs explicit `yes`; never edits `strategies/*.json` |
| `/close <symbol\|strategy>` | mutate | Flattens ONE position, reduce-only, via `/api/close` | UNKNOWN read → refuses; no size sent; never `/webhook` |
| `/emergency-stop kill\|flatten\|demo\|pause-symbol` | mutate | Global kill / flatten all / force strategy to demo / pause a symbol | Dry-run + explicit `yes`; UNKNOWN never rendered as flat |
| `/restart [force]` | mutate | Health-checks dashboard + receiver; restarts the down service(s) via systemd (fallback `bash run.sh --skip-tests`, then `docker compose restart`) | Both up → does nothing; explicit `yes` required; live host warned positions briefly unmonitored |
| `/upgrade [--no-pull\|--no-tests\|--no-ui]` | mutate | Runs `bash deploy/deploy.sh`: pull + deps + UI build + tests + restart, with auto-rollback on health-check failure | Dry-run preview (HEAD + plan) + explicit `yes`; services restart, live positions briefly unmonitored; failure → `.deploy-backups/` |

## Commands

### `/help` — command help (read-only text)
- **Syntax:** `/help` or `/help <command>`
- **Does:** with no arg, prints a concise overview of all nine commands (one-liner +
  example each); with a command arg, prints that command's syntax, safety guards, and
  2–3 examples. Case-insensitive; matches with or without the leading `/` (and common
  aliases like `estop`, `deploy`, `mode`).
- **Guard:** pure text — issues no HTTP request, writes no file, mutates nothing. If
  asked to actually run a command, it defers to that command's own skill.
- **Example:** `rtk claude -p "/help close" --permission-mode dontAsk`

### `/status` — system posture
- **Syntax:** `/status`
- **Does:** loopback reads of dashboard `/health` + `/api` and receiver `/health` + `/latest`; reports armed, mode, reachability, last alert, strategy count, freshness.
- **Guard:** a read failure reports that surface DOWN/UNKNOWN; `armed:false` only when `/health` returned `ok` with `arm.armed == false`.
- **Example:** `rtk claude -p "/status" --permission-mode dontAsk`

### `/positions` — open positions
- **Syntax:** `/positions`
- **Does:** reads `/api` → `okx_live.positions`, renders SYMBOL/SIDE/POS/AVG_PX/MARK/UPL/LEV/MGN.
- **Guard:** read failure, `okx_live.ok == false`, `executor.degraded`, or `freshness.no_data` → **UNKNOWN**. Only a healthy, non-degraded, empty map is **FLAT**.
- **Example:** `rtk claude -p "/positions" --permission-mode dontAsk`

### `/strategy-list` — strategies & effective modes
- **Syntax:** `/strategy-list`
- **Does:** lists `strategies/*.json` folded with `control-state.json` overrides/pauses; shows `file_mode` vs `effective_mode` (override > pause > file) and `paused`.
- **Guard:** read-only; corrupt/missing file surfaces as UNKNOWN fields, not a crash.
- **Example:** `rtk claude -p "/strategy-list" --permission-mode dontAsk`

### `/trace` — follow one signal
- **Syntax:** `/trace <received_at | symbol>`
- **Does:** joins `raw-webhooks.jsonl` → `signals.jsonl` → `pipeline.jsonl` → `executions.jsonl` on `received_at`; shows where the signal stopped.
- **Guard:** time-less payloads (no `tv_time`) are flagged `time_less`; their non-deterministic `signal_id` is never re-derived. Intake-without-dedupe = "queued, not yet dequeued", not an error.
- **Example:** `rtk claude -p "/trace BTCUSDT" --permission-mode dontAsk`

### `/strategy-mode` — change a strategy's mode (mutating)
- **Syntax:** `/strategy-mode <name-or-id> <pause|resume|demo|live>` (`resume` → wire `clear`)
- **Does:** resolves the arg to a `strategy_id`, previews current→target, `POST {dashboard}/api/control/strategy/{id}`; writes only the control override.
- **Guard:** always dry-run first; `live` requires explicit `yes`; ambiguous arg stops with candidates; transport/5xx → UNKNOWN (never "applied"); re-reads to confirm.
- **Example:** `rtk claude -p "/strategy-mode btcusdt_duo_base_dev_2h demo" --permission-mode dontAsk`

### `/close` — flatten one position (mutating, reduce-only)
- **Syntax:** `/close <symbol|strategy>`
- **Does:** confirms the position via `/api`, previews side+size, and on `yes` sends `POST {receiver}/api/close` with body `{symbol, strategy_id, operator, reason}`.
- **Guard:** UNKNOWN read → refuses to close; **no size field**; never `/webhook`; reduce-only (close bypasses only kill switch + symbol pause). UNKNOWN outcome never reported as flat.
- **Example:** `rtk claude -p "/close BTCUSDT" --permission-mode dontAsk`

### `/emergency-stop` — layered stop (mutating)
- **Syntax:** `/emergency-stop kill|flatten|demo <id>|pause-symbol <sym>`
- **Does:**
  - `kill` — global live kill: outputs the `HERMX_LIVE_TRADING=false` + receiver-restart steps (no HTTP endpoint), then confirms via `/health` `arm.kill_switch_engaged == true`.
  - `flatten` — closes every open position reduce-only, one `/api/close` per position, then re-reads to verify flat.
  - `demo <id>` — forces one strategy to sandbox via the control override (mutating twin of `/strategy-mode <id> demo`).
  - `pause-symbol <sym>` — adds the symbol to `control-state.json` `symbol_pauses` via the atomic safe updater.
- **Guard:** every action dry-runs, requires explicit `yes`, logs before/after positions, and treats UNKNOWN reads as indeterminate — never "flat"/"safe". `kill` unconfirmed until `/health` returns `true`.
- **Example:** `rtk claude -p "/emergency-stop flatten" --permission-mode dontAsk`

### `/restart` — restart the dashboard / receiver (mutating, lifecycle)
- **Syntax:** `/restart` or `/restart force`
- **Does:** health-checks `{dashboard}/health` + `{receiver}/health` via `read_state()`. Both UP → reports `up` and does nothing. One/both DOWN → previews the plan (which service is down, which units/scripts restart) and on `yes` restarts via `systemctl restart hermx-dashboard hermx-receiver` (preferred), else `bash run.sh --skip-tests` at repo root, else `docker compose restart`. Polls `/health` up to 30s and reports up / still down / UNKNOWN. `force` restarts both regardless of state with a stronger prompt.
- **Guard:** never restarts without explicit `yes` (`force` needs `yes, restart both`); a live host is warned open positions may be briefly unmonitored; failed reads → DOWN/UNKNOWN, never "up"; process-lifecycle only — no strategy edits, no `/webhook`, no `/api/close`, no sizing. The `run.sh` fallback forces `HERMX_LIVE_TRADING=false` unless `--honor-submit` (dev recovery, not a live relaunch) — on a live host add `--honor-submit` or re-arm afterward.
- **Example:** `rtk claude -p "/restart" --permission-mode dontAsk`

### `/upgrade` — upgrade the host to latest code (mutating, deploy + lifecycle)
- **Syntax:** `/upgrade` or `/upgrade --no-pull` or `/upgrade --no-tests` or `/upgrade --no-ui`
- **Does:** runs `bash deploy/deploy.sh` — snapshot state + capture rollback point, pull latest (unless `--no-pull`; pip install always runs), build the dashboard UI (unless `--no-ui`), run offline tests (unless `--no-tests`), restart `hermx-dashboard` + `hermx-receiver`, then health-check. If the health check fails, `deploy.sh` **auto-rolls back** to the prior HEAD. Then polls `/health` up to 30s and reports upgraded / rolled back / failed / UNKNOWN.
- **Guard:** always dry-run first (shows current HEAD + what the deploy will do) and requires explicit `yes`; services restart, so a live host is warned open positions may be briefly unmonitored; only operator-supplied flags are passed through; non-zero exit or health-stays-down → FAILED/UNKNOWN, never "upgraded OK", with a pointer to the run's backup under `.deploy-backups/`; deploy/lifecycle only — no strategy edits, no `/webhook`, no `/api/close`, no sizing.
- **Example:** `rtk claude -p "/upgrade" --permission-mode dontAsk`

## Shared invariants
- **UNKNOWN, never "flat".** Any read failure, `okx_live.ok == false`, `executor.degraded`, or `freshness.no_data` → UNKNOWN. Only a healthy, non-degraded, empty book is genuinely FLAT.
- **Mutations require preview + confirmation.** Every mutating command dry-runs first and needs an explicit `yes` before any write.
- **`/close` is reduce-only and sizeless.** Body carries no size; the receiver derives the reduce-only close from the live position; it never routes via `/webhook`.
- **Live transitions require explicit `yes`.** No implicit `demo→live`; a `rejected`/`UNKNOWN` outcome is never reported as "done".
- **Sizing is owned by the execution layer.** Sizing is computed from the strategy file (`capital.budget_usd`, `leverage`) by the Python execution layer; a skill/agent never sets or suggests a size.
- **Freshness is bounded on bar time (`tv_time`), not server time.** A current clock after an outage does not mean fresh data.

## Setup
- **Where the skills live:** `skills/hermx-help/`, `hermx-status/`, `hermx-positions/`, `hermx-strategy-list/`, `hermx-trace/`, `hermx-strategy-mode/`, `hermx-close/`, `hermx-restart/`, `hermx-upgrade/` (each a `SKILL.md`), plus `skills/emergency-stop.md`. Shared code + contract live in `skills/hermx-ops/`.
- **Load in Hermes:** each `SKILL.md` carries `metadata.hermes` (tags, `related_skills`, config with loopback defaults `dashboard=http://127.0.0.1:8098`, `receiver=http://127.0.0.1:8891`). Invoke as `claude -p "/<skill-name> <args>" --permission-mode dontAsk`.
- **`HERMX_SECRET`:** required **only when `HERMX_DASH_AUTH` is on**. When set, requests send `X-Dashboard-Token: {HERMX_SECRET}` (from the HermX `.env`). On loopback with auth off (the host default) no header is needed. A `401` is a read failure → UNKNOWN, not "flat". The `/api/close` and control-write endpoints fail closed without a matching token.

## Reference
- **Shared helper library:** `skills/hermx-ops/lib/hermx_ops.py` — `read_state()` (encodes UNKNOWN-never-flat), `format_positions()`, `list_strategies()`, `resolve_strategy()`, `find_traces_by_symbol()`, `correlate_trace()`, `post_strategy_mode()`, `post_close()`, `safe_update_control_state()`. Every skill imports it via `sys.path.insert(0, "skills/hermx-ops/lib")`.
- **API contract (canonical):** `skills/hermx-ops/references/api-contract.md` — endpoints, auth, response shapes, freshness rules, the UNKNOWN-never-flat rule, the trace join contract, and the sizing invariant. Derived from `src/dashboard.py` and `src/webhook_receiver.py`.
