# HermX API Contract (canonical)

Single source of truth for every HermX read-only slash-command skill. All skills
reference this file for endpoints, auth, response shapes, and the UNKNOWN-never-flat
rule. Values here are derived from `src/dashboard.py` (`api_payload`, `health_payload`,
`okx_live_snapshot`, `freshness_summary`, `executor_health_summary`) and
`src/webhook_receiver.py` (`/health`, `/latest`).

## Bases

| Service  | Base                    | Purpose                                   |
|----------|-------------------------|-------------------------------------------|
| Dashboard| `http://127.0.0.1:8098` | State reads: `/api`, `/health`, signals   |
| Receiver | `http://127.0.0.1:8891` | Intake: `/health`, `/latest`, `/webhook`  |

Both bind loopback on the same VPS. Never call an exchange directly.

## Auth

- Header: `X-Dashboard-Token: {HERMX_SECRET}`.
- `HERMX_DASH_AUTH` defaults **on** (unset ⇒ enabled), so `/api` and `/api/signals`
  **require** the token. Only `/health` (dashboard and receiver) is unauthenticated.
  A header is required unless the operator has explicitly set `HERMX_DASH_AUTH=false`.
- `HERMX_SECRET` comes from the HermX `.env`. Skill processes don't inherit that env,
  so `lib/hermx_ops.py::_load_secret()` resolves it from `HERMX_SECRET` or the `.env`
  (`${HERMX_DATA_DIR}/.env`, then repo-root `.env`).
- If a `/api` read returns `401 unauthorized`, the token is required and missing/wrong
  — treat as a read failure (→ UNKNOWN), not as "flat" / "no positions".

## Endpoints

### `GET {dashboard}/health` → `health_payload()`
```json
{
  "ok": true,
  "service": "hermx_dashboard",
  "mode": "demo_live",
  "policies": [],
  "primary_policy": null,
  "arm": {
    "kill_switch_engaged": true,
    "live_trading_enabled": false,
    "demo_strategies": 4,
    "live_strategies": 0,
    "armed": false
  },
  "strategy_files": ["btcusdt_duo_base_dev_2h", "..."],
  "timestamp": "2026-07-02T00:00:00+00:00"
}
```
- `arm.armed` = `live_strategies > 0 AND live_trading_enabled`. Demo-only ⇒ not armed.
- `arm.kill_switch_engaged` = `NOT live_trading_enabled` (kill switch engaged ⇒ live disabled).

### `GET {dashboard}/api` → `api_payload()` (auth-gated)
Top-level keys used by skills:
```json
{
  "generated_at": "ISO",
  "strategies": [ { "strategy_id": "...", "effective_mode": "demo", ... } ],
  "strategy_overrides": { "<sid>": { "mode": "demo", "submit_orders": true } },
  "okx_live": {
    "ok": true,
    "generated_at": "ISO",
    "account": { ... },
    "positions": {
      "BTCUSDT": {
        "inst_id": "BTC-USDT-SWAP",
        "side": "LONG|SHORT|FLAT",
        "pos": 4.61,
        "avg_px": 65000.0,
        "notional_usd": 1500.0,
        "upl": 12.3,
        "realized_pnl": 0.0,
        "leverage": "2",
        "margin_mode": "isolated",
        "mark_px": 65010.0,
        "last": 65010.0,
        "imr": 0.0
      }
    },
    "error": null
  },
  "executor": {
    "ok": true, "healthy": true, "error": null,
    "stale": false, "degraded": false,
    "age_seconds": 3.1, "generated_at": "ISO"
  },
  "freshness": {
    "generated_at": "ISO", "data_at": "ISO", "age_seconds": 5.0,
    "stale": false, "no_data": false, "refresh_interval_seconds": 30
  },
  "ledger_health": { "total_skipped": 0, "truncated_tails": 0, "ledgers": {} }
}
```
- When the executor read fails, `okx_live.ok` is `false`, `positions` is `{}` (or
  absent), and `okx_live.error` is set. `executor.degraded` is `true`.

### `GET {receiver}/health` → `{ "ok": true, "service": "mxc-vps-shadow-receiver", "port": 8891, "mode": "shadow_only", "latest": "<path>" }`

### `GET {receiver}/latest` → last processed payload
- `200` body = the last webhook payload/outcome JSON.
- `404 {"error":"no_latest_yet"}` — no alert processed yet.
- `503 {"error":"latest_unreadable"}` — file corrupt → read failure, not "no alerts".

### Mutating endpoints (NOT used by Phase-1 read-only skills)
- `POST {receiver}/webhook` — the only order path.
- `POST {receiver}/api/close` — operator flatten (auth-gated). Phase 2.

## Freshness rules

- Bound freshness on **signal bar time (`tv_time`)**, never server time. After an
  outage the clock is current but the bar is stale.
- Executor freshness: `executor.stale = age_seconds > refresh_interval_seconds`
  (`REFRESH_INTERVAL_SECONDS`, currently 30s). `executor.ok = healthy AND NOT stale`.
- `freshness.stale` / `freshness.no_data` describe the whole model's data age.
- Derived flag: `OK` when `executor.ok` and `NOT freshness.stale`; otherwise `STALE`.

## UNKNOWN-never-flat rule (money-safety invariant)

A read failure or a stale/degraded executor must **never** render as "flat" / "no
positions" / "$0". Report `UNKNOWN`. Conditions that force UNKNOWN:

1. `/api` request errored / timed out / returned non-200 (incl. `401`).
2. `okx_live.ok` is `false` (executor read failed) — regardless of `positions`.
3. `executor.degraded` is `true` (unhealthy **or** stale).
4. `freshness.no_data` is `true`.

Only when `okx_live.ok == true` AND `NOT executor.degraded` may an empty
`positions` map be reported as genuinely **FLAT**. `side: "FLAT"` on a fresh, healthy
read is a real flat position and is reported as FLAT (not UNKNOWN).

The shared helper `lib/hermx_ops.py::read_state()` encodes this: on any failure it
returns UNKNOWN sentinels rather than an empty/flat structure.

## Correlation / trace contract

- Log files (under `logs/`): `raw-webhooks.jsonl` (intake WAL), `signals.jsonl`
  (dedupe ledger, written **after** dequeue), `pipeline.jsonl` (per-stage),
  `executions.jsonl` (execution outcome).
- **Join key: `received_at`** (microsecond ISO stamped at intake, collision-safe).
  `signals.jsonl` rows carry it as `first_seen_at` (fallback `ts`).
- `raw-webhooks.jsonl` is the durable WAL — the recovery source, not the in-memory
  queue. `signals.jsonl` partitions "processed" from "queued but not dequeued".
- **Time-less payloads:** a payload with no `tv_time` gets a wall-clock-derived,
  non-deterministic `signal_id` (`normalize()` → `now_iso()`). **Never re-derive its
  id** to correlate — join purely on the stable `received_at`, and flag the row as
  time-less.

## Sizing invariant

Sizing is computed from the strategy file (`capital.budget_usd`, `leverage`) by the
Python execution layer. A skill/agent **never** sets or suggests an order size.

## Strategy file shape (`strategies/*.json`)

```json
{
  "strategy_id": "btcusdt_duo_base_dev_2h",
  "name": "BTCUSDT Duo Base Dev 2H",
  "timeframe": "2h",
  "instrument": { "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap" },
  "capital": { "budget_usd": 1500, "reinvest": true },
  "execution_mode": "demo",
  "leverage": 2,
  "margin_mode": "isolated"
}
```
- Symbol is derived from `instrument.inst_id`: `BTC-USDT-SWAP` → `BTCUSDT`.
- No top-level `symbol`/`budget_usd`; read `instrument.inst_id` / `capital.budget_usd`.

## hermx-strategy file-write contract

Strategy add/update/archive (`/hx-strategy`) are **direct filesystem operations** on
`strategies/*.json` — no dashboard/receiver endpoint exists for strategy CRUD.

- **Not hot-reloaded.** `webhook_receiver.py` builds `STRATEGIES =
  load_strategy_files()` at import time (`src/webhook_receiver.py:500`) and
  `dashboard.py` globs `STRATEGIES_DIR` in its own `load_strategy_files()`
  (`src/dashboard.py:144`). Any add/update/archive is inert until both restart
  (`/hx-restart`).
- **Atomic writes.** add/update write a temp file in the same dir then `os.replace` —
  a crash never leaves a partial `strategies/<id>.json`.
- **Archive = move, not delete.** Files move to `strategies/_archive/<file>.json`.
  `_archive/` sits outside both loaders' non-recursive `strategies/*.json` glob, so
  archived strategies drop out of the live set with history preserved.
- Files are git-tracked; the skill never runs `git add`/`git commit` — committing is
  the operator's step. Mode overrides stay in `control-state.json` via
  `/hx-strategy-mode`, never in this path.

## control-state.json shape

```json
{
  "symbol_pauses": { "BTCUSDT": { "paused": true, "set_at": "ISO" } },
  "strategy_overrides": {
    "<strategy_id>": { "mode": "demo", "execution_mode": "demo",
                        "submit_orders": true, "set_at": "ISO" }
  }
}
```

### effective_mode resolution (matches `_effective_strategy_mode`)
1. override `mode` present → use it (`demo` / `live` / `pause`).
2. else strategy `submit_orders` explicitly `false` → `pause`.
3. else strategy `execution_mode` (default `demo`).

`paused` (skill-level) is true when `effective_mode == "pause"` **or** the strategy's
symbol has a truthy entry in `control-state.json` `symbol_pauses`.
