# Skill: Emergency Stop

Use this when execution must be paused immediately. The system has two operative
controls — a per-strategy `execution_mode` (`demo` or `live`) and the global
`HERMX_LIVE_TRADING` switch — so a stop can be global, per-strategy, or per-symbol.

## Stop Levels

### Level 0: Global live kill switch (fastest, no redeploy)

A single environment variable, `HERMX_LIVE_TRADING`, gates **all** real-money
(`execution_mode: "live"`) submission. It is fail-closed and a positive enable
flag — live trading is permitted ONLY when it is explicitly truthy:

- **Unset** — live trading DISABLED (safe default).
- **Falsey** (`false`, `0`, `no`, `""`/blank, case-insensitive) — DISABLED.
- **Truthy** (`true`, `1`, `yes`) — live trading enabled.

To stop all live submission instantly, set it false (or unset it) and restart the
receiver:

```
HERMX_LIVE_TRADING=false
```

```
python src/webhook_receiver.py   # or the start script
```

A `live` strategy then returns mode `not_submitted` (`reason: "live_trading_disabled"`)
and writes that record to `logs/executions.jsonl` — no order is sent to the real
account. (`live_trading_enabled()` in `src/hermx_shared.py` is the single source of
truth, read by `ExecutionService.execute()` and mirrored in the dashboard `/health`
`arm` block as `kill_switch_engaged`.)

> Note: `demo` strategies route to the exchange **sandbox** and do not consult this
> switch. To stop one of those too, use Level 1 (per-strategy) below.

### Level 1: Stop a single strategy

Switch the strategy's `execution_mode` from `"live"` to `"demo"` in its
`strategies/<id>.json` and restart the receiver. The strategy still validates and
ledgers, but routes to the sandbox instead of the real account.

To fully stop a strategy from submitting (even to sandbox), remove its strategy file
from `strategies/` or use the per-symbol pause in `control-state.json`.

### Level 2: Pause a single symbol

Use the per-symbol pause registry in `control-state.json` (`symbol_pauses`) — unchanged.
A paused symbol returns `not_submitted` (`reason: "symbol_paused"`) regardless of mode.

### Level 3: Stop webhook processing

Stop the receiver service. The dashboard may remain online for read-only status.

### Level 4: Flatten exchange

- close open positions on the venue,
- verify flat,
- set `HERMX_LIVE_TRADING=false` and/or switch each strategy's `execution_mode` to `"demo"` to keep
  it flat.

## Execution control model

Two controls decide whether and where an order is placed:

1. **Per-strategy** — `execution_mode` (`demo` or `live`) in `strategies/<id>.json`.
   **Only `live` is real-money** and routes to the real account; `demo` routes to the
   exchange sandbox (treated as `simulated_trading`).
2. **Global** — `HERMX_LIVE_TRADING` (env). Required truthy for any `live` order;
   irrelevant to non-`live` (sandbox) modes.

`ExecutionService.execute()` blocks submission (fail-safe `not_submitted`) on any of:
strategy without valid `execution_mode` / auth unhealthy / watchdog paused; a `live`
strategy when `HERMX_LIVE_TRADING` is not truthy (`live_trading_disabled`); a paused
symbol; or a duplicate `cl_ord_id`. The system never submits on uncertainty.

## Required Log

Every emergency stop must log:

- time
- operator
- reason
- strategies affected
- exchange position before
- exchange position after
