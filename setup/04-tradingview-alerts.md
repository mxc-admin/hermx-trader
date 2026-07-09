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

The webhook secret (`HERMX_SECRET`) can be sent two ways:

- **Default — `secret_key` in the alert JSON body.** TradingView's native webhook alert
  action **cannot send custom HTTP headers on any plan**, so a direct alert authenticates
  by including `"secret_key": "<HERMX_SECRET>"` in the message JSON (see the payload
  template below). This is the standard method for stock TradingView alerts.
- **Alternative — `X-Webhook-Secret` HTTP header.** For operators who run a relay/proxy in
  front of the receiver, the secret may instead be injected as this header. When present it
  takes precedence over the body field and must match.

One transport is required; requests with neither (or a wrong value) are rejected `401`. The
secret is **never** placed in the URL. The receiver strips `secret_key` immediately after
authenticating, so it never reaches any ledger.

If you run an HMAC signing relay (`HERMX_REQUIRE_HMAC=true`), see
`setup/08-webhook-hmac-relay.md`.

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
  "source": "tradingview",
  "secret_key": "<HERMX_SECRET>"
}
```

Use `side = sell` for sell alerts. Replace `<HERMX_SECRET>` with the actual value from
`.env`. Omit `secret_key` only if a relay injects the `X-Webhook-Secret` header instead.

