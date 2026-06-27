# Architecture

## Design Goal

Create a clean execution system where strategies are defined by files, alerts are validated by contract, execution is routed through exchange adapters, and the dashboard reflects real state.

## Main Principle

The system must be understandable and installable by a human or an agent.

That means:

- no hardcoded strategy decisions hidden in dashboard code
- no secrets inside strategy files
- no mixing research-only policies with active execution
- no assuming TradingView, OKX, or Cloudflare state without verification

## Runtime Flow

```text
1. TradingView emits BUY/SELL alert.
2. Webhook receives alert and responds quickly.
3. Alert is normalized.
4. Alert is matched to a strategy JSON by strategy_id.
5. Strategy file is validated.
6. Symbol and timeframe are checked.
7. Execution planner checks current state.
8. If needed, current position is closed and verified.
9. New direction is opened.
10. Fills, fees, slippage, and PnL are logged.
11. Dashboard updates from logs and exchange readback.
```

## Layered Execution Architecture

Execution is organized as four layers. The lower three are built and tested today; the top layer is a planned reasoning cap. The brain only ever calls *down* into the deterministic stack — it cannot bypass any gate or journal.

```text
Layer 1  [PLANNED]  Hermes Agent brain (Nous Research)
                    venue/action selection, learning, cron scans — advisory ONLY
   │  emits {venue, intent}; the layer below validates and may VETO it
   ▼
Layer 2  [BUILT]    HermesExecutionSkill   (src/skills/hermes_execution.py)
                    only agent-facing execution surface; deterministic.
                    Builds a normalized execution_intent, calls Layer 3.
                    Owns no intelligence and no money-safety policy of its own.
   ▼
Layer 3  [BUILT]    ExecutionService       (src/execution/service.py)
                    single chokepoint for all money-safety: kill-switch +
                    gate precedence, write-ahead PLANNED->SUBMITTED journal,
                    idempotency, FILLED/REJECTED/UNKNOWN outcome state machine,
                    post-submit reconciliation, secret redaction.
   ▼
Layer 4  [BUILT]    CCXT adapter           (src/executors/ccxt_adapter.py)
                    selected by ExecutorFactory; OKX demo LIVE-verified.
                    kucoin/hyperliquid/bybit supported by CCXT but unconfigured.
```

This is the deterministic substrate: all risk policy (idempotency, journal transitions, reconciliation semantics, symbol pause, kill switches) lives in first-party HermX code at Layers 2–3. CCXT (Layer 4) is transport and venue normalization only — it owns no risk policy. The planned brain (Layer 1) adds no money-safety; the deterministic layer can veto any brain recommendation. See `docs/HERMES_AGENT_DESIGN.md`.

## Venue Selection & Multi-Exchange

- **The strategy selects the exchange via its instrument**, never by carrying secrets. A strategy file picks its venue + instrument; the execution stack resolves which exchange to talk to from that selection. (Today the strategy schema is still OKX-pinned; the exchange-agnostic `instrument.exchange` shape is Phase 6 — see `REFACTOR_PLAN.md`.)
- **Credentials are resolved per-exchange, namespaced** (`src/security/credentials.py`, per `REFACTOR_PLAN.md` §0.4). Each exchange has its own credential set under a stable prefix (`OKX_*`, `KUCOIN_*`, `BYBIT_*`). No exchange borrows another's keys; a missing/partial set fails closed (disarmed, no fallback). OKX, KuCoin, and Bybit are wired in the resolver today; Hyperliquid is **not** yet.
- **CCXT is the transport.** One unified adapter (`src/executors/ccxt_adapter.py`) handles exchange I/O and normalization across venues. Adding a venue is a config + credential-namespacing change, not new connector code.
- **Configured/verified today: OKX demo only.** Other CCXT-supported venues are planned and unverified.

## Built vs Planned

| Capability | Status | Where |
|---|---|---|
| HermesExecutionSkill (agent-facing, deterministic) | **Built** | `src/skills/hermes_execution.py`, `skills/hermes-execution.md` |
| ExecutionService chokepoint (gates, journal, idempotency, reconcile, redaction) | **Built** | `src/execution/service.py` |
| CCXT adapter as sole execution backend (read + write contract) | **Built** | `src/executors/ccxt_adapter.py`, `factory.py` |
| Per-exchange namespaced credentials (OKX, KuCoin, Bybit) | **Built** | `src/security/credentials.py` |
| OKX demo live-verified submit → query → close | **Built** | `tests/test_okx_paper_integration.py` (gated) |
| Hyperliquid credential resolution + adapter auth (wallet/key) | **Planned** | Phase 6 (`REFACTOR_PLAN.md`) |
| Multi-venue LIVE routing (KuCoin / Hyperliquid configured + verified) | **Planned** | Phase 6 |
| Exchange-agnostic strategy/alert schema (instrument block, widened enums) | **Planned** | Phase 6 (M1/M2/M3) |
| Hermes Agent brain (venue selection, learning, cron, autonomous skills) | **Planned** | Phase 8 (`docs/HERMES_AGENT_DESIGN.md`) |

## Current Trial Runtime

The current operating trial is strategy-file-driven Duo Base Dev.

Active strategies:

| Asset | Timeframe | Upper | Lower | Budget | Leverage |
|---|---:|---:|---:|---:|---:|
| SOLUSDT | 3h | 1.05 | 0.95 | 1500 | 2x |
| ETHUSDT | 2h | 1.40 | 0.95 | 2000 | 2x |
| XRPUSDT | 4h | 1.20 | 0.95 | 1500 | 2x |
| BTCUSDT | 2h | 1.40 | 0.95 | 1500 | 2x |

Total assigned demo budget: `$6,500`.

## Strategy-Driven Dashboard

The dashboard should not hardcode assets.

It should read:

- strategy ID
- asset
- timeframe
- budget
- leverage
- execution mode
- status

Then it should generate one card per active strategy.

## Demo vs Live

Demo and live must be separate runtime profiles.

```text
runtime.demo.json  -> OKX sandbox/demo only
runtime.live.json  -> real account, created later from explicit approval
```

No code should infer live execution from a strategy file alone.

Live requires:

- runtime profile approval
- operator confirmation
- exchange API confirmation
- emergency pause mechanism
- health checks passing

## Exchange Adapter Pattern

The strategy engine should not care whether the exchange is OKX, Bybit, Binance, or another venue.

It should produce an instruction:

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "asset": "SOLUSDT",
  "target_side": "long",
  "target_notional_usd": 3000,
  "margin_mode": "isolated",
  "leverage": 2
}
```

The exchange adapter translates that instruction into exchange-specific API calls.

Today this is the **CCXT unified adapter** (`src/executors/ccxt_adapter.py`), selected by `ExecutorFactory` from `config.execution.exchange` / `execution.ccxt_exchange`. CCXT is the sole execution backend — `ExecutorFactory.available() == ['ccxt']`; there is no hand-rolled OKX connector anymore (the former `src/okx_demo_executor.py` was removed in the P5 cutover). The adapter implements both the write path (`execute`) and the normalized read/query contract (`get_order`, `get_open_orders`, `get_positions`, `get_balance`, history), and maps a submit timeout to a first-class `UNKNOWN` outcome. New venues are added by registering them in the factory and namespacing their credentials — not by writing new connector code.

## State Sources

| State | Source of Truth |
|---|---|
| Strategy definition | `strategies/*.json` |
| Alert contract | `schemas/tradingview-alert.schema.json` |
| Runtime mode | `config/runtime.*.json` and environment variables |
| Secrets | `.env` or secure vault outside git |
| Orders | Exchange API and execution ledger |
| Dashboard | Strategy files plus logs plus exchange readback |

## What This Clean System Excludes

- research-only dashboards
- paper replay experiments
- unsupported indicator logic
- hardcoded selected policies
- private API keys
- TradingView passwords
- real-money execution defaults
