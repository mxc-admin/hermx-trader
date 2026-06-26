# Strategies

Strategies are the center of the system.

Each strategy is one JSON file in `strategies/`.

The strategy file tells the system:

- which asset to trade
- which timeframe to use
- which indicator/version generated the signal
- which optimized parameters belong to that strategy
- which OKX instrument to route to
- how much budget is assigned
- which leverage and margin mode to use
- whether the strategy is demo, paper, shadow, disabled, or live-ready

## Active Demo Strategies

Current total assigned demo budget: `$6,500`.

| File | Purpose |
|---|---|
| `solusdt_duo_base_dev_3h.json` | SOLUSDT 3H Duo Base Dev candidate |
| `ethusdt_duo_base_dev_2h.json` | ETHUSDT 2H Duo Base Dev candidate |
| `xrpusdt_duo_base_dev_4h.json` | XRPUSDT 4H Duo Base Dev candidate |
| `btcusdt_duo_base_dev_2h.json` | BTCUSDT 2H Duo Base Dev candidate |

## Required Strategy Fields

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "name": "SOLUSDT Duo Base Dev 3H",
  "asset": "SOLUSDT",
  "okx_inst_id": "SOL-USDT-SWAP",
  "timeframe": "3h",
  "chart_type": "heikin_ashi",
  "indicator": "mxc duo-base",
  "indicator_version": "duo-base-2.5",
  "upper_band_mult": 1.05,
  "lower_band_mult": 0.95,
  "auto_alpha": false,
  "budget_usd": 1500,
  "leverage": 2,
  "margin_mode": "isolated",
  "execution_mode": "demo",
  "okx_submit_orders": true,
  "status": "active_demo"
}
```

## Strategy Rules

- `strategy_id` must be unique.
- `asset` must match the TradingView alert.
- `timeframe` must match the TradingView alert.
- `okx_inst_id` must be the actual OKX swap instrument.
- `budget_usd` is the margin budget, not the leveraged notional.
- `leverage` determines target notional.
- `execution_mode = demo` means OKX sandbox/demo only.

## Future Strategy Extensions

Possible later fields:

- `exchange`: `okx`
- `primary_execution`: true/false
- `paper_only`: true/false
- `max_daily_loss_usd`
- `max_position_age_bars`
- `stop_loss_pct`
- `take_profit_pct`
- `cooldown_bars`

Do not add these until the runtime actually supports them.
