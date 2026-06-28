# Strategies

Strategies are the center of the system.

Each strategy is one JSON file in `strategies/`.

The strategy file tells the system:

- which instrument to trade (exchange + `inst_id`), from which the asset is derived
- which timeframe to use
- which indicator generated the signal
- how much budget is assigned (`capital.budget_usd`)
- which leverage and margin mode to use
- which `execution_mode` (`demo`, `paper`, `live`, or `shadow`) and whether `submit_orders` is on

## Active Demo Strategies

Current total assigned demo budget: `$6,000` (4 × `$1,500`).

| File | Purpose |
|---|---|
| `solusdt_duo_base_dev_3h.json` | SOLUSDT 3H Duo Base Dev candidate |
| `ethusdt_duo_base_dev_2h.json` | ETHUSDT 2H Duo Base Dev candidate |
| `xrpusdt_duo_base_dev_4h.json` | XRPUSDT 4H Duo Base Dev candidate |
| `btcusdt_duo_base_dev_2h.json` | BTCUSDT 2H Duo Base Dev candidate |

## Required Strategy Fields (schema v2)

```json
{
  "schema_version": 2,
  "strategy_id": "solusdt_duo_base_dev_3h",
  "name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "mxc duo-base v2.5",
  "timeframe": "3h",
  "instrument": {
    "exchange": "okx",
    "inst_id": "SOL-USDT-SWAP",
    "type": "swap"
  },
  "capital": {
    "budget_usd": 1500,
    "reinvest": true
  },
  "execution_mode": "demo",
  "submit_orders": true,
  "leverage": 2,
  "margin_mode": "isolated",
  "notes": "Active OKX sandbox/demo candidate."
}
```

The **asset is derived** from `instrument.inst_id` — `SOL-USDT-SWAP` → `SOLUSDT`. There is no
separate `asset` field. Fields removed in v2: `asset`, `status`, `validation_source`, `auto_alpha`,
`chart_type`, `upper_band_mult`, `lower_band_mult`, `indicator_version`, `okx_inst_id`,
`okx_submit_orders`, and the top-level `budget_usd` (now `capital.budget_usd`).

## Strategy Rules

- `strategy_id` must be unique.
- The asset derived from `instrument.inst_id` must match the TradingView alert.
- `timeframe` must match the TradingView alert.
- `instrument.inst_id` must be the actual exchange swap instrument; `instrument.exchange` selects the venue.
- `capital.budget_usd` is the margin budget, not the leveraged notional.
- `leverage` determines target notional.
- `submit_orders` must be `true` for the strategy to place any order.
- `execution_mode` is a four-value enum: `demo`, `paper`, `live`, `shadow`. **Only `live` is a
  real-money mode** — it routes to the real account and additionally requires
  `HERMX_LIVE_TRADING=true`. `demo`, `paper`, and `shadow` all route to the exchange
  sandbox/paper account (treated as `simulated_trading`); any non-`live` value is sandboxed.

## Future Strategy Extensions

Possible later fields:

- `primary_execution`: true/false
- `paper_only`: true/false
- `max_daily_loss_usd`
- `max_position_age_bars`
- `stop_loss_pct`
- `take_profit_pct`
- `cooldown_bars`

Do not add these until the runtime actually supports them.
