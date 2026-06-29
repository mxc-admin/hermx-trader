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

## Authentication

The alert must be sent with the `X-Webhook-Secret` HTTP header set to `HERMX_SECRET`.
The secret is **never** included in the JSON payload or in the URL — it travels only
in the `X-Webhook-Secret` header.

## Venue, Execution Mode, and Sizing Come From the Strategy

The alert carries only the **signal** — never venue routing, sizing, or execution mode. Those
are supplied by the strategy file matched on `strategy_id`:

- **Venue** is `strategy.instrument.exchange`, which is **authoritative for routing**. The alert's
  own `exchange` field is advisory only and is constrained by the alert schema
  (`schemas/tradingview-alert.schema.json`) to the four wired venues —
  `okx`, `kucoin`, `bybit`, `hyperliquid` — so an alert declaring any other venue fails validation
  when schema enforcement is on. The strategy may route to other configured venues via
  `instrument.exchange`; the alert `exchange` need only be one of the four schema-allowed values.
- **Execution mode** is `strategy.execution_mode`, a two-value enum — `demo` or `live`.
  **Only `live` is real-money**: it routes to the real account and additionally requires
  the global kill switch `HERMX_LIVE_TRADING=true`. `demo` routes to the venue's
  sandbox/paper account (always allowed; treated as `simulated_trading`).
- **Sizing** is computed in the receiver as `capital.budget_usd * leverage`. The alert has **no**
  size, notional, budget, or leverage field; any such value would be ignored.

This keeps the contract **exchange-agnostic**: the same alert shape works for every supported
venue, and the strategy file selects where and how it executes.

## Required TradingView Settings

- Condition: correct Duo Base Dev BUY or SELL signal.
- Timeframe: must match the strategy file.
- Alert frequency: once per bar close.
- Webhook URL: system webhook URL. Add the `X-Webhook-Secret` header with the `HERMX_SECRET` value.
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
