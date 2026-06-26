# Architecture

## Design Goal

Create a clean execution system where strategies are defined by files, alerts are validated by contract, execution is routed through exchange adapters, and the dashboard reflects real state.

## Main Principle

The system must be understandable and installable by a human or an agent.

That means:

- no hardcoded strategy decisions hidden in dashboard code
- no secrets inside strategy files
- no mixing research-only policies with active execution
- no assuming TradingView, OKX, or Cloudflare state without verification

## Runtime Flow

```text
1. TradingView emits BUY/SELL alert.
2. Webhook receives alert and responds quickly.
3. Alert is normalized.
4. Alert is matched to a strategy JSON by strategy_id.
5. Strategy file is validated.
6. Symbol and timeframe are checked.
7. Execution planner checks current state.
8. If needed, current position is closed and verified.
9. New direction is opened.
10. Fills, fees, slippage, and PnL are logged.
11. Dashboard updates from logs and exchange readback.
```

## Current Trial Runtime

The current operating trial is strategy-file-driven Duo Base Dev.

Active strategies:

| Asset | Timeframe | Upper | Lower | Budget | Leverage |
|---|---:|---:|---:|---:|---:|
| SOLUSDT | 3h | 1.05 | 0.95 | 1500 | 2x |
| ETHUSDT | 2h | 1.40 | 0.95 | 2000 | 2x |
| XRPUSDT | 4h | 1.20 | 0.95 | 1500 | 2x |
| BTCUSDT | 2h | 1.40 | 0.95 | 1500 | 2x |

Total assigned demo budget: `$6,500`.

## Strategy-Driven Dashboard

The dashboard should not hardcode assets.

It should read:

- strategy ID
- asset
- timeframe
- budget
- leverage
- execution mode
- status

Then it should generate one card per active strategy.

## Demo vs Live

Demo and live must be separate runtime profiles.

```text
runtime.demo.json  -> OKX sandbox/demo only
runtime.live.json  -> real account, created later from explicit approval
```

No code should infer live execution from a strategy file alone.

Live requires:

- runtime profile approval
- operator confirmation
- exchange API confirmation
- emergency pause mechanism
- health checks passing

## Exchange Adapter Pattern

The strategy engine should not care whether the exchange is OKX, Bybit, Binance, or another venue.

It should produce an instruction:

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "asset": "SOLUSDT",
  "target_side": "long",
  "target_notional_usd": 3000,
  "margin_mode": "isolated",
  "leverage": 2
}
```

The exchange adapter translates that instruction into exchange-specific API calls.

## State Sources

| State | Source of Truth |
|---|---|
| Strategy definition | `strategies/*.json` |
| Alert contract | `schemas/tradingview-alert.schema.json` |
| Runtime mode | `config/runtime.*.json` and environment variables |
| Secrets | `.env` or secure vault outside git |
| Orders | Exchange API and execution ledger |
| Dashboard | Strategy files plus logs plus exchange readback |

## What This Clean System Excludes

- research-only dashboards
- paper replay experiments
- unsupported indicator logic
- hardcoded selected policies
- private API keys
- TradingView passwords
- real-money execution defaults
