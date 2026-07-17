# Strategies

Strategies are the center of the system.

Each strategy is one JSON file in `strategies/`.

The strategy file tells the system:

- which instrument to trade (exchange + `inst_id`), from which the asset is derived
- which timeframe to use
- which indicator generated the signal
- how much budget is assigned (`capital.budget_usd`)
- which leverage and margin mode to use
- which `execution_mode` (`demo` or `live`) — demo uses the sandbox/paper account, live uses the real account

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
  "leverage": 2,
  "margin_mode": "isolated",
  "notes": "Active OKX sandbox/demo candidate."
}
```

The **asset is derived** from `instrument.inst_id` — `SOL-USDT-SWAP` → `SOLUSDT`. An optional
explicit `instrument.asset` (uppercase `[A-Z0-9]+`, e.g. `BTCUSDT`) overrides the derivation
when the inst_id doesn't compact cleanly; precedence is `instrument.asset` → legacy top-level
`asset` → derived from `inst_id` (a diverging explicit value is honored but logged as a
warning). Fields removed in v2: top-level `asset`, `status`, `validation_source`, `auto_alpha`,
`chart_type`, `upper_band_mult`, `lower_band_mult`, `indicator_version`, `okx_inst_id`,
and the top-level `budget_usd` (now `capital.budget_usd`).

## Strategy Rules

- `strategy_id` must be unique.
- The asset (explicit `instrument.asset` or derived from `instrument.inst_id`) should match
  the TradingView alert's `symbol`; a mismatch is a soft `strategy_symbol_mismatch` warning
  (the alert still executes on the strategy's instrument — matching is `strategy_id`-first).
- `timeframe` must match the TradingView alert.
- `instrument.inst_id` must be the actual exchange swap instrument; `instrument.exchange` selects the venue.
- `capital.budget_usd` is the margin budget, not the leveraged notional.
- `leverage` determines target notional.
- `execution_mode` is a two-value enum: `demo` or `live`. **Only `live` is a real-money mode**
  — it routes to the real account and additionally requires `HERMX_LIVE_TRADING=true`.
  `demo` routes to the exchange sandbox/paper account (treated as `simulated_trading`).
- `side_policy` (optional, enum `long_only` / `short_only` / `long_short`, default
  `long_short`) is the strategy-level directional constraint. `long_only` suppresses the
  OPEN leg of short signals (and `short_only` the mirror); the opposite-close leg always
  runs, so a policy change can never strand an open position. Absent = `long_short`
  (both directions trade normally).

## Future Strategy Extensions

Possible later fields:

- `primary_execution`: true/false
- `max_daily_loss_usd`
- `max_position_age_bars`
- `stop_loss_pct`
- `take_profit_pct`
- `cooldown_bars`

Do not add these until the runtime actually supports them.
