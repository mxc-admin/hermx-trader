# Exchange Adapters

The exchange adapter converts generic execution instructions into exchange-specific API calls.

## Current Adapter

OKX:

- USDT perpetual swaps
- sandbox/demo first
- isolated margin
- 2x leverage
- market execution

## Generic Instruction

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "okx_inst_id": "SOL-USDT-SWAP",
  "target_side": "long",
  "target_notional_usd": 3000,
  "margin_mode": "isolated",
  "leverage": 2
}
```

## Adapter Responsibilities

- set or verify leverage
- set or verify margin mode
- calculate size
- close existing position
- verify close
- open new position
- return fill information

## Future Exchanges

Future exchanges should be added under `src/exchanges/`.

Do not add exchange-specific logic directly into the strategy engine.

