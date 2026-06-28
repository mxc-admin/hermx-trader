# Budget Model

Budget means assigned margin capital per strategy. It is the `capital.budget_usd` field in the
strategy file (schema v2) — the old top-level `budget_usd` no longer exists.

Target notional is budget multiplied by leverage: `capital.budget_usd * leverage`.

## Current Budgets

Current assigned demo margin total: `$6,000`.

| Asset | Budget | Leverage | Target Notional |
|---|---:|---:|---:|
| SOLUSDT | 1500 | 2x | 3000 |
| ETHUSDT | 1500 | 2x | 3000 |
| XRPUSDT | 1500 | 2x | 3000 |
| BTCUSDT | 1500 | 2x | 3000 |

The current four-asset Duo Base Dev trial uses `$6,000` because each asset has its own assigned budget.

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
- sandbox execution (`execution_mode: demo`, `paper`, or `shadow` — all route to the
  sandbox/paper account; any non-`live` mode is treated as `simulated_trading`)
- live execution (`execution_mode: live` — the only real-money mode; real account, requires
  `HERMX_LIVE_TRADING=true`)
