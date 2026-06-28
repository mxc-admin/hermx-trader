# Skill: TradingView Alert Setup

Use this when creating or auditing TradingView alerts.

## Inputs

- Strategy JSON file.
- Webhook URL.
- Webhook secret.
- TradingView logged-in session.

## Steps

1. Open the chart for the strategy asset.
2. Set the strategy timeframe.
3. Confirm chart type is Heikin Ashi if required by the strategy.
4. Confirm the correct indicator and version.
5. Confirm strategy parameters match the JSON file.
6. Create BUY alert.
7. Create SELL alert.
8. Set frequency to once per bar close.
9. Set expiration to open-ended or maximum available.
10. Paste JSON payload with exact `strategy_id`.
11. Add the `X-Webhook-Secret` header using the `HERMX_SECRET` value from `.env` (requires TradingView Pro+; lower-tier plans cannot send custom headers and need an HMAC relay — see `setup/08-webhook-hmac-relay.md`).
12. Save alert.
13. Send or wait for test signal.
14. Confirm webhook receives it.

## Audit Checklist

- `strategy_id` exists.
- `symbol` matches strategy.
- `timeframe` matches strategy.
- `side` is correct.
- `tv_signal_price` uses `{{close}}`.
- `tv_time` uses `{{time}}`.
- The `X-Webhook-Secret` header is set to the correct `HERMX_SECRET` value.
- Alert is active.
- Alert is not expired.
- Alert has no calculation error.

