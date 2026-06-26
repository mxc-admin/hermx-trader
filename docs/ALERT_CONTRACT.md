# TradingView Alert Contract

Every TradingView alert must send JSON.

Every active execution alert must include `strategy_id`.

## Required Payload

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "side": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "exchange": "okx",
  "source": "tradingview"
}
```

## Required TradingView Settings

- Condition: correct Duo Base Dev BUY or SELL signal.
- Timeframe: must match the strategy file.
- Alert frequency: once per bar close.
- Webhook URL: system webhook URL plus secret.
- Expiration: open-ended or longest available.

## Validation Rules

The receiver must reject or quarantine:

- missing `strategy_id`
- unknown `strategy_id`
- wrong symbol for that strategy
- wrong timeframe for that strategy
- missing side
- side not `buy` or `sell`
- malformed JSON

## Why `strategy_id` Matters

The same asset can have multiple strategies.

Example:

- SOLUSDT Duo Base Dev 3H
- SOLUSDT alternate production strategy
- SOLUSDT research/paper strategy

Without `strategy_id`, the system cannot know which one should execute.

## Alert Examples

### SOL 3H Buy

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "side": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "exchange": "okx",
  "source": "tradingview"
}
```

### ETH 2H Sell

```json
{
  "strategy_id": "ethusdt_duo_base_dev_2h",
  "strategy_name": "ETHUSDT Duo Base Dev 2H",
  "indicator": "duo-base-dev",
  "symbol": "ETHUSDT",
  "timeframe": "2h",
  "side": "sell",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "exchange": "okx",
  "source": "tradingview"
}
```
