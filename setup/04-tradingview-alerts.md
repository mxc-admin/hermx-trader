# 04 TradingView Alerts

Goal: create one BUY and one SELL alert per strategy.

## Required Alert Settings

- indicator: Duo Base Dev during trial
- chart type: Heikin Ashi
- timeframe: from strategy file
- frequency: once per bar close
- expiration: open-ended or maximum available
- webhook: enabled
- message: valid JSON with `strategy_id`

## Active Alert Set

| Strategy | BUY | SELL |
|---|---:|---:|
| SOLUSDT 3H | yes | yes |
| ETHUSDT 2H | yes | yes |
| XRPUSDT 4H | yes | yes |
| BTCUSDT 2H | yes | yes |

## Payload Template

```json
{
  "strategy_id": "REPLACE_ME",
  "strategy_name": "REPLACE_ME",
  "indicator": "duo-base-dev",
  "symbol": "REPLACE_ME",
  "timeframe": "REPLACE_ME",
  "side": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "exchange": "okx",
  "source": "tradingview"
}
```

Use `side = sell` for sell alerts.

