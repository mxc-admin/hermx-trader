---
name: hx-help
description: "Use when the operator asks what HermX slash commands exist, how one works, or wants usage help — e.g. '/hx-help', '/hx-help close', 'what commands are there', 'how do I use /hx-strategy-mode'. Prints a human-readable overview of all HermX commands, or a detailed guide for one. Read-only text: no HTTP calls, no file writes, no mutations."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, hermx, help, docs, read-only, operations]
    related_skills: [hermx-control, hx-status, hx-positions, hx-strategy-list, hx-trace, hx-tv-alerts, hx-strategy-mode, hx-close, hx-emergency-stop, hx-restart, hx-upgrade, hx-exchange, hx-telegram]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
---

# /hx-help — HermX slash-command help

**Read-only text.** `/hx-help` or `/hx-help <command>`. Emits nothing but formatted
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
  `close`, `/hx-close`, `CLOSE`, `/Close` all resolve to `/hx-close`.
- Accept common aliases: `estop`/`kill` → `/hx-emergency-stop`, `strategies`/`list`
  → `/hx-strategy-list`, `mode` → `/hx-strategy-mode`, `deploy` → `/hx-upgrade`,
  `alerts`/`tv` → `/hx-tv-alerts`, `exchange`/`keys`/`creds` → `/hx-exchange`,
  `telegram`/`tg`/`gateway` → `/hx-telegram`.
- Unknown arg → say so, then print the overview so the operator can pick.

## All commands (`/hx-help`)

Twelve commands: five read-only diagnostics, five guarded mutations, plus
`/hx-exchange` (exchange-credential management) and `/hx-telegram` (Telegram
operator-gateway management) — both reads plus guarded SSH mutations.
None places or sizes an order. Every mutation confirms before it writes.

**Read-only**
- **`/hx-status`** — armed?, mode, dashboard/receiver up, last alert, strategy count.
  `/hx-status`
- **`/hx-positions`** — open positions: side, size, entry, mark, UPL, leverage.
  `/hx-positions`
- **`/hx-strategy-list`** — every strategy with `file_mode` vs `effective_mode` + paused.
  `/hx-strategy-list`
- **`/hx-trace`** — follow one signal intake → dedupe → pipeline → exec, joined on
  `received_at`. `/hx-trace BTCUSDT`
- **`/hx-tv-alerts`** — print copy-paste BUY + SELL TradingView Message templates for a
  strategy. `/hx-tv-alerts SOLUSDT`

**Mutating (dry-run + explicit `yes`)**
- **`/hx-strategy-mode`** — set a per-strategy override (pause/resume/demo/live).
  `/hx-strategy-mode btcusdt_duo_base_dev_2h demo`
- **`/hx-close`** — flatten ONE position, reduce-only, sizeless, via `/api/close`.
  `/hx-close BTCUSDT`
- **`/hx-emergency-stop`** — layered stop: kill / flatten all / demo one / pause a symbol.
  `/hx-emergency-stop flatten`
- **`/hx-restart`** — restart a down dashboard/receiver via systemd (with fallbacks).
  `/hx-restart`
- **`/hx-upgrade`** — pull + deps + UI + tests + restart with auto-rollback.
  `/hx-upgrade`
- **`/hx-exchange`** — add/update/remove/validate exchange API keys via SSH-dispatched
  `scripts/exchange.sh` (the skill never handles a key). `/hx-exchange add okx --demo`
- **`/hx-telegram`** — set up/rotate/allowlist the Telegram operator gateway via
  SSH-dispatched `scripts/hermes-gateway.sh` (the skill never handles the bot token).
  `/hx-telegram allow 987654321`

Ask `/hx-help <command>` for syntax, guards, and examples on any one.

## Command detail (`/hx-help <command>`)

### `/hx-status`
- **Type:** read-only.
- **Does:** loopback reads of dashboard `/health` + `/api` and receiver `/health` +
  `/latest`; reports armed, mode, reachability, last alert, strategy count, freshness.
- **Guards:** any read failure surfaces **DOWN/UNKNOWN**; `armed:false` only when
  `/health` returned `ok` with `arm.armed == false`. Freshness is bounded on bar time
  (`tv_time`), never server clock. No POST ever issued.
- **Examples:**
  - `rtk claude -p "/hx-status" --permission-mode dontAsk`
  - "is HermX armed right now?" → `/hx-status`
  - "when did the last alert land?" → `/hx-status` (reads `/latest`)

### `/hx-positions`
- **Type:** read-only.
- **Does:** reads `/api` → `okx_live.positions`; renders SYMBOL/SIDE/POS/AVG_PX/MARK/
  UPL/LEV/MGN.
- **Guards:** read failure, `okx_live.ok == false`, `executor.degraded`, or
  `freshness.no_data` → **UNKNOWN**. Only a healthy, non-degraded, empty book is FLAT —
  never infer "flat" from a failed read.
- **Examples:**
  - `rtk claude -p "/hx-positions" --permission-mode dontAsk`
  - "what are we holding?" → `/hx-positions`
  - Executor degraded → renders UNKNOWN, not an empty (flat) table.

### `/hx-strategy-list`
- **Type:** read-only.
- **Does:** lists `strategies/*.json` folded with `control-state.json`; shows
  `file_mode` vs `effective_mode` (override > pause > file) and `paused`.
- **Guards:** read-only; a corrupt/missing strategy file surfaces UNKNOWN fields
  instead of crashing. No writes.
- **Examples:**
  - `rtk claude -p "/hx-strategy-list" --permission-mode dontAsk`
  - "which strategies are live vs demo?" → `/hx-strategy-list`
  - "is anything paused?" → `/hx-strategy-list` (`paused` column)

### `/hx-trace`
- **Type:** read-only.
- **Syntax:** `/hx-trace <received_at | symbol>`
- **Does:** joins `raw-webhooks.jsonl` → `signals.jsonl` → `pipeline.jsonl` →
  `executions.jsonl` on `received_at`; shows where a signal stopped.
- **Guards:** time-less payloads (no `tv_time`) are flagged `time_less` and their
  non-deterministic `signal_id` is **never re-derived**. Intake-without-dedupe means
  "queued, not yet dequeued" — not an error. Read-only.
- **Examples:**
  - `rtk claude -p "/hx-trace BTCUSDT" --permission-mode dontAsk`
  - `rtk claude -p "/hx-trace 2026-07-02T14:03:12.481922Z" --permission-mode dontAsk`
  - "why didn't the last BTC alert fire?" → `/hx-trace BTCUSDT`

### `/hx-tv-alerts`
- **Type:** read-only.
- **Syntax:** `/hx-tv-alerts <name-or-id>`
- **Does:** resolves the arg to a `strategy_id`, reads `strategies/<id>.json`, and prints
  two schema-valid single-line alert Message payloads — one BUY (long), one SELL (short) —
  extracting `symbol` (from `instrument.inst_id`), `timeframe`, `strategy_id`, and venue
  (`instrument.exchange`). Also reports the webhook URL (`https://<host>/webhook` or the
  Tailscale Funnel URL) and the `X-Webhook-Secret: <HERMX_SECRET>` header.
- **Guards:** read-only text — no HTTP, no file write, never `/webhook`. An ambiguous
  symbol / fuzzy-only arg stops with candidates and emits **no** template. `exchange` is
  hard-coded to the strategy venue (not `{{exchange}}` — TradingView emits it uppercase
  and it can fail `alert_schema_invalid`); `timeframe` is hard-coded (not `{{interval}}`).
  `execution_mode` is shown as context only, never as an alert field.
- **Examples:**
  - `rtk claude -p "/hx-tv-alerts SOLUSDT" --permission-mode dontAsk`
  - `rtk claude -p "/hx-tv-alerts btcusdt_duo_base_dev_2h" --permission-mode dontAsk`
  - "what do I paste into TradingView for ETH?" → `/hx-tv-alerts ETHUSDT`

### `/hx-strategy-mode`
- **Type:** mutating.
- **Syntax:** `/hx-strategy-mode <name-or-id> <pause|resume|demo|live>` (`resume` →
  control `clear`).
- **Does:** resolves the arg to a `strategy_id`, previews current→target, then
  `POST {dashboard}/api/control/strategy/{id}`; writes only the control override —
  it never edits `strategies/*.json`.
- **Guards:** always dry-runs first; **`live` requires explicit `yes`**; an ambiguous
  arg stops with candidates; transport/5xx → UNKNOWN (never "applied"); re-reads to
  confirm the new `effective_mode`.
- **Examples:**
  - `rtk claude -p "/hx-strategy-mode btcusdt_duo_base_dev_2h demo" --permission-mode dontAsk`
  - `rtk claude -p "/hx-strategy-mode btc pause" --permission-mode dontAsk`
  - Promote to live → shows preview, waits for `yes` before the POST.

### `/hx-close`
- **Type:** mutating, reduce-only.
- **Syntax:** `/hx-close <symbol|strategy>`
- **Does:** confirms the position via `/api`, previews side + size, and on `yes` sends
  `POST {receiver}/api/close` with `{symbol, strategy_id, operator, reason}`.
- **Guards:** UNKNOWN read → **refuses to close** (won't assume flat); **no size field**
  in the body (server derives the reduce-only close); never routes via `/webhook`; a
  transport/5xx outcome is UNKNOWN, never reported as "flat".
- **Examples:**
  - `rtk claude -p "/hx-close BTCUSDT" --permission-mode dontAsk`
  - `rtk claude -p "/hx-close btcusdt_duo_base_dev_2h" --permission-mode dontAsk`
  - Stale executor → refuses; healthy + no position → "nothing to close".

### `/hx-emergency-stop`
- **Type:** mutating (layered).
- **Syntax:** `/hx-emergency-stop kill|flatten|demo <id>|pause-symbol <sym>`
- **Does:**
  - `kill` — global live kill: outputs the `HERMX_LIVE_TRADING=false` + receiver-restart
    steps, then confirms via `/health` `arm.kill_switch_engaged == true`.
  - `flatten` — closes every open position reduce-only (one `/api/close` each), then
    re-reads to verify flat.
  - `demo <id>` — forces one strategy to sandbox (mutating twin of `/hx-strategy-mode <id>
    demo`).
  - `pause-symbol <sym>` — adds the symbol to `control-state.json` `symbol_pauses` via
    the atomic safe updater.
- **Guards:** every action dry-runs, needs explicit `yes`, logs before/after positions,
  and treats UNKNOWN reads as indeterminate — never "flat"/"safe". `kill` stays
  unconfirmed until `/health` returns `true`.
- **Examples:**
  - `rtk claude -p "/hx-emergency-stop flatten" --permission-mode dontAsk`
  - `rtk claude -p "/hx-emergency-stop kill" --permission-mode dontAsk`
  - `rtk claude -p "/hx-emergency-stop pause-symbol BTCUSDT" --permission-mode dontAsk`

### `/hx-restart`
- **Type:** mutating (lifecycle).
- **Syntax:** `/hx-restart` or `/hx-restart force`
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
  - `rtk claude -p "/hx-restart" --permission-mode dontAsk`
  - `rtk claude -p "/hx-restart force" --permission-mode dontAsk`
  - Both services up → reports `up`, changes nothing.

### `/hx-upgrade`
- **Type:** mutating (deploy + lifecycle).
- **Syntax:** `/hx-upgrade` or `/hx-upgrade --no-pull` or `/hx-upgrade --no-tests` or
  `/hx-upgrade --no-ui`
- **Does:** runs `bash deploy/deploy.sh` — snapshot state + rollback point, pull latest
  (unless `--no-pull`; pip install always runs), build the UI (unless `--no-ui`), run
  offline tests (unless `--no-tests`), restart both services, then health-check. Health
  failure → **auto-rollback** to prior HEAD; polls `/health` up to 30s.
- **Guards:** always dry-runs first (current HEAD + plan) and requires explicit `yes`;
  a live host is warned positions may be briefly unmonitored; only operator-supplied
  flags pass through; non-zero exit or health-stays-down → FAILED/UNKNOWN (never
  "upgraded OK"), with a pointer to the run's `.deploy-backups/` snapshot.
- **Examples:**
  - `rtk claude -p "/hx-upgrade" --permission-mode dontAsk`
  - `rtk claude -p "/hx-upgrade --no-tests" --permission-mode dontAsk`
  - Health check fails post-deploy → deploy.sh rolls back; report ROLLED BACK.

### `/hx-exchange`
- **Type:** mutating (exchange-credential management, dispatched over SSH).
- **Syntax:** `/hx-exchange list` · `/hx-exchange status <exchange> [--demo|--live]` ·
  `/hx-exchange add|update|remove <exchange> --demo|--live`
- **Does:** runs the standalone `scripts/exchange.sh` on the VPS over SSH. `list`/`status`
  read (credential presence, resolver `OK`/`PARTIAL`/`MISSING`, precedence + adapter-wiring
  warnings, optional `--live` `fetch_balance` probe); `add`/`update`/`remove` upsert `.env`
  (backed up to `.env.bak`, `chmod 600`). Exchanges: okx, kucoin, bybit, binance, bitget,
  gate, hyperliquid, coinbase (coinbase live/spot only — no ccxt sandbox).
- **Guards:** the skill **never handles a credential** — each key is captured by `read -s`
  **inside the script** on the VPS, never as an argument, never echoed. Mutations run under
  `ssh -t`; `add`/`update`/`remove` need an explicit `--demo`/`--live`; `--live` needs a
  typed `yes, add live <exchange>`; `remove` needs `yes, remove <env> <exchange>`.
  **Adding keys does not arm the system** — `HERMX_LIVE_TRADING` + strategy `execution_mode`
  still gate live. Unreachable SSH → UNKNOWN, never "set/removed".
- **Examples:**
  - `rtk claude -p "/hx-exchange list" --permission-mode dontAsk`
  - `rtk claude -p "/hx-exchange add okx --demo" --permission-mode dontAsk`
  - "are the Bybit live keys valid?" → `/hx-exchange status bybit --live`

### `/hx-telegram`
- **Type:** mutating (Telegram operator-gateway management, dispatched over SSH).
- **Syntax:** `/hx-telegram status` · `/hx-telegram setup|rotate|remove` ·
  `/hx-telegram allow|revoke <numeric-user-id>` · `/hx-telegram start|stop|restart` ·
  `/hx-telegram test`
- **Does:** runs the standalone `scripts/hermes-gateway.sh` on the VPS over SSH to manage
  `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USERS` in `~/.hermes/.env` (the Hermes agent's
  env, **not** the HermX `.env`) and drive the `hermes gateway` lifecycle. `status` reads
  (masked token, allowlist, gateway process state); `setup`/`rotate` upsert the token
  (backed up to `.env.bak`, `chmod 600`, restore-on-failure); `allow`/`revoke` edit the
  allowlist; `remove` blanks both vars and stops the gateway.
- **Guards:** the skill **never handles the bot token** — it is captured by `read -s`
  **inside the script** on the VPS, never as an argument, never echoed. Mutations run
  under `ssh -t`; `allow`/`revoke` require an all-digits user id and a typed
  `yes, allow <id>` / `yes, revoke <id>`; `remove` needs `yes, remove telegram`; the
  allowlist is never blank-as-configured or allow-all. The gateway is comms-only —
  it grants no trading authority and its downtime never blocks execution.
- **Examples:**
  - `rtk claude -p "/hx-telegram status" --permission-mode dontAsk`
  - `rtk claude -p "/hx-telegram allow 987654321" --permission-mode dontAsk`
  - "rotate the telegram bot token" → `/hx-telegram rotate`

## Shared invariants (apply to every command)
- **UNKNOWN, never "flat".** Any read failure, `okx_live.ok == false`,
  `executor.degraded`, or `freshness.no_data` → UNKNOWN. Only a healthy, non-degraded,
  empty book is genuinely FLAT.
- **Mutations require preview + confirmation** — dry-run first, explicit `yes` before
  any write.
- **`/hx-close` is reduce-only and sizeless**; it never routes via `/webhook`.
- **Live transitions require explicit `yes`** — no implicit demo→live.
- **Sizing is owned by the execution layer** (from the strategy file); a skill never
  sets or suggests a size.
- **Freshness is bounded on bar time (`tv_time`)**, not server clock.

## Rules
- This skill is **pure text**: it issues no HTTP request, writes no file, mutates
  nothing. If asked to actually *run* a command, defer to that command's own skill.
- Never invent commands or flags — only the twelve above exist.
- Keep output terse and chat-formatted: markdown bullets and short paragraphs.

## Verification checklist
- [ ] `/hx-help` (no arg) lists all **twelve** commands, each with a one-liner + example.
- [ ] `/hx-help close`, `/hx-help /hx-close`, `/hx-help CLOSE` all resolve to the `/hx-close` detail.
- [ ] Aliases (`estop`, `kill`, `deploy`, `mode`, `list`, `alerts`, `tv`) map to the right command.
- [ ] An unknown arg reports "unknown command" then falls back to the overview.
- [ ] No HTTP call, no file write, no mutation is performed by this skill.
- [ ] Detail blocks state each command's type, syntax, guards, and 2–3 examples.
