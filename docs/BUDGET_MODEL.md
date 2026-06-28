# Budget Model

Budget means assigned margin capital per strategy. It is the `capital.budget_usd` field in the
strategy file (schema v2) — the old top-level `budget_usd` no longer exists.

Target notional is budget multiplied by leverage: `capital.budget_usd * leverage`.

## Current Budgets

Current assigned demo margin total: `$6,500`.

| Asset | Budget | Leverage | Target Notional |
|---|---:|---:|---:|
| SOLUSDT | 1500 | 2x | 3000 |
| ETHUSDT | 2000 | 2x | 4000 |
| XRPUSDT | 1500 | 2x | 3000 |
| BTCUSDT | 1500 | 2x | 3000 |

The current four-asset Duo Base Dev trial uses `$6,500` because each asset has its own assigned budget.

## Definitions

| Term | Meaning |
|---|---|
| Budget start | initial assigned budget |
| Budget now | budget start plus realized and live PnL |
| PnL now | current total PnL including open position movement |
| Realized PnL | PnL from closed positions |
| Live PnL | movement on currently open position |
| Fee | exchange fee from fills |
| Slippage | difference between alert price and exchange fill price |

## Important Rule

Paper/shadow budget and real account budget must not be mixed.

The dashboard should clearly label whether numbers come from:

- historical paper replay
- demo execution (`execution_mode: demo` — sandbox/paper account)
- live execution (`execution_mode: live` — real account, requires `HERMX_LIVE_TRADING=true`)
