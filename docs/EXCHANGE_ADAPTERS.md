# Exchange Adapters

The exchange adapter converts generic execution instructions into exchange-specific API calls.

## Current Adapter

The current adapter is the **CCXT unified adapter** (`src/executors/ccxt_adapter.py`), selected by `ExecutorFactory` from `config.execution.exchange` / `execution.ccxt_exchange`. CCXT is the **sole** execution backend (`ExecutorFactory.available() == ['ccxt']`); there is no hand-rolled per-exchange connector. The same adapter handles any CCXT-supported venue — the venue is chosen by config, not by separate code.

Status by venue:

- **OKX — live-verified.** Configured and tested today; a real OKX-demo submit → query → close passes through CCXT (gated test `tests/test_okx_paper_integration.py`). Trial posture: USDT perpetual swaps, sandbox/demo first, isolated margin, 2x leverage, market execution.
- **KuCoin / Hyperliquid / Bybit — supported by CCXT but unconfigured.** Not wired, not verified. (Per-exchange credentials exist for OKX/KuCoin/Bybit in `src/security/credentials.py`; Hyperliquid credential resolution is still planned — see `REFACTOR_PLAN.md` Phase 6.)

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

## Adding a Venue

A new venue is added **via CCXT config + credential namespacing — no new connector code**:

1. Register the venue in `ExecutorFactory` (or rely on the CCXT adapter resolving it from `execution.ccxt_exchange`).
2. Add its per-exchange *namespaced* credentials to `src/security/credentials.py` (e.g. `KUCOIN_*`) and the redaction key list — never reuse another exchange's keys.
3. Provide a per-venue runtime profile and the strategy instrument that selects it.
4. Verify with a gated sandbox write test mirroring the OKX-demo path before treating the venue as live-capable.

Note: Hyperliquid auth differs (wallet / private key, no passphrase), so it needs a dedicated branch — tracked in `REFACTOR_PLAN.md` Phase 6.

Do not add exchange-specific logic directly into the strategy engine. Venue translation belongs inside the CCXT adapter; risk policy belongs in `ExecutionService`.

