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

## Webhook Authentication

The webhook secret is sent as an HTTP header, not in the alert body.

- Header name: `X-Webhook-Secret`
- Header value: `HERMX_SECRET` from `.env`
- This header is required; requests without it are rejected.
- The secret does **not** go in the alert JSON body and is **not** part of the URL.

TradingView Pro+ supports custom webhook headers, so the header can be set
directly on the alert. If your plan does not support custom headers, use the
HMAC relay path instead — see `setup/08-webhook-hmac-relay.md`.

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

