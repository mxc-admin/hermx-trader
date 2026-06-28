# Exchange Adapters

The exchange adapter converts generic execution instructions into exchange-specific API calls.

## Current Adapter

The current adapter is the **CCXT unified adapter** (`src/executors/ccxt_adapter.py`), selected by `ExecutorFactory` from `config.execution.exchange` / `execution.ccxt_exchange`. CCXT is the **sole** execution backend (`ExecutorFactory.available() == ['ccxt']`); there is no hand-rolled per-exchange connector. The same adapter handles any CCXT-supported venue — the venue is chosen by config, not by separate code.

## Supported Venues

Eight venues are **configured** (each has a per-venue runtime profile
`config/runtime.<exchange>.demo.json` and namespaced demo/sandbox credentials in
`src/security/credentials.py`), but only **four** are **wired for authenticated execution** in the
CCXT adapter's `_client()`: **OKX, KuCoin, Bybit, and Hyperliquid**. Binance, Bitget, Gate.io, and
Coinbase Advanced resolve credentials but the adapter does **not** apply them to the ccxt client, so
they would connect unauthenticated and cannot place authenticated orders — they are **config-only,
not yet wired**. The demo/sandbox posture is set by `execution.account: "demo"` in the runtime
profile and `execution_mode: "demo"` in the strategy; `execution_mode: "live"` switches to the real
account (and additionally requires `HERMX_LIVE_TRADING=true`).

| Venue | Status | Runtime config | Demo/sandbox credentials |
|---|---|---|---|
| **OKX** | **wired** — recommended, fully tested (demo + live) | `config/runtime.demo.json` | `OKX_DEMO_API_KEY`, `OKX_DEMO_SECRET_KEY`, `OKX_DEMO_PASSPHRASE` |
| **KuCoin** | **wired** — untested live | `config/runtime.kucoin.demo.json` | `KUCOIN_PAPER_API_KEY`, `KUCOIN_PAPER_SECRET`, `KUCOIN_PAPER_PASSPHRASE` |
| **Bybit** | **wired** — untested live | `config/runtime.bybit.demo.json` | `BYBIT_TESTNET_API_KEY`, `BYBIT_TESTNET_SECRET_KEY` |
| **Hyperliquid** | **wired** — untested live | `config/runtime.hyperliquid.demo.json` | `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PRIVATE_KEY` |
| **Binance** | config-only — **not yet wired** in `_client()` | `config/runtime.binance.demo.json` | `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_SECRET_KEY` |
| **Bitget** | config-only — **not yet wired** in `_client()` | `config/runtime.bitget.demo.json` | `BITGET_DEMO_API_KEY`, `BITGET_DEMO_SECRET_KEY`, `BITGET_DEMO_PASSPHRASE` |
| **Gate.io** | config-only — **not yet wired** in `_client()` | `config/runtime.gate.demo.json` | `GATE_TESTNET_API_KEY`, `GATE_TESTNET_SECRET_KEY` |
| **Coinbase Advanced** | config-only — **not yet wired** in `_client()` | `config/runtime.coinbase.demo.json` | `COINBASE_SANDBOX_API_KEY`, `COINBASE_SANDBOX_SECRET_KEY` |

OKX is the live-verified reference venue — a real OKX-demo submit → query → close passes through
CCXT (gated test `tests/test_okx_paper_integration.py`). Trial posture across venues: USDT
perpetual swaps, sandbox/demo first, isolated margin, 2x leverage, market execution. Hyperliquid
authenticates with a wallet address + private key (no API key/passphrase pair), resolved through
its own credential branch.

## Generic Instruction

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "inst_id": "SOL-USDT-SWAP",
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
3. Provide a per-venue runtime profile (`config/runtime.<exchange>.demo.json`) and the strategy instrument that selects it.
4. Verify with a gated sandbox write test mirroring the OKX-demo path before treating the venue as live-capable.

Note: Hyperliquid auth differs (wallet address / private key, no passphrase), so it uses a dedicated credential branch (`HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_PRIVATE_KEY`).

Do not add exchange-specific logic directly into the strategy engine. Venue translation belongs inside the CCXT adapter; risk policy belongs in `ExecutionService`.

