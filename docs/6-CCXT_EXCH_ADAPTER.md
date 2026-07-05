# CCXT_EXCH_ADAPTER

Deep-dive reference for `src/executors/ccxt_adapter.py` — how HermX uses the CCXT library today and how to extend it. This doc absorbs the former `EXCHANGE_ADAPTERS.md` (venue table, instruction shape, adapter responsibilities); the per-venue unified-field audit (formerly CCXT_VENUE_NEUTRALITY_VALIDATION.md, doc since removed) is summarized under § Venue-Specific Normalization below.

## Overview

[CCXT](https://github.com/ccxt/ccxt) is a unified crypto-exchange client library: one Python API (`create_order`, `fetch_positions`, `fetch_balance`, …) with per-venue subclasses that translate to each exchange's REST API. HermX pins it as the **exchange transport layer only** — risk policy, idempotency, journaling, and reconciliation all live above the adapter in the controlled execution path (`src/executors/ccxt_adapter.py:2-5`).

HermX deliberately runs **one unified adapter, many venues via config** instead of per-venue connectors:

- Post the P5-06/P5-07 CCXT cutover, `ccxt` is the *only* execution backend; the legacy `okx_demo` CLI adapter was removed (`src/executors/factory.py:4-8`). `ExecutorFactory.available() == ["ccxt"]`.
- A new venue is a config + credential change, not new connector code — the venue-specific surface is confined to a `_client()` auth branch and a handful of normalization helpers.
- All eight wired venues share the same order-lifecycle semantics (`execute` legs, timeout→UNKNOWN mapping, normalized query shapes), so reconciliation and the dashboard never see venue payload shapes.

Position in the execution pipeline:

```
webhook_receiver (readiness block)
  → ExecutionService.submit (src/execution/service.py:317: executor_factory.create(...))
    → ExecutorFactory.create (src/executors/factory.py:60-74)
      → CcxtExecutor(config, root)  — key "ccxt" (src/executors/ccxt_adapter.py:313-314)
        → ccxt.<venue>(...) client (src/executors/ccxt_adapter.py:330-429)
```

## Current Architecture

### Instruction shape (the readiness block)

The adapter's input is the venue-neutral **readiness block** — the generic execution instruction built by the receiver/`ExecutionService`, never anything venue-shaped. The keys `execute()` actually reads:

```json
{
  "inst_id": "SOL-USDT-SWAP",
  "signal_side": "buy",
  "leverage": 2,
  "td_mode": "isolated",
  "close_only": false,
  "execution_intent": {
    "target_direction": "long",
    "planned_notional_usd": 3000,
    "actions": ["CLOSE_OPPOSITE_IF_ANY", "OPEN_LONG"],
    "client_order_id": "mxc…",
    "client_order_id_close": "mxc…",
    "client_order_id_open": "mxc…"
  }
}
```

- **Symbol**: `ccxt_symbol`, else `inst_id` / `instrument.inst_id` / `symbol` through `_inst_id_to_ccxt_symbol` (`ccxt_adapter.py:768-770`).
- **Direction**: `execution_intent.target_direction`, falling back to `signal_side` buy→long / sell→short (`_target_direction`, `ccxt_adapter.py:499-509`).
- **Size**: explicit `amount` (contracts) wins; else `execution_intent.planned_notional_usd` sized via the market spec (`_amount_from_readiness`, `ccxt_adapter.py:644-660`).
- **Reference price**: `signal_price`, else `okx_mark_price` / `okx_last_price` (`ccxt_adapter.py:581-583`).
- **`td_mode`** (isolated/cross) passes through as an OKX-style order param; **`leverage`** only informs the pre-trade balance check — neither is *set* on the venue (see Known Gaps).
- **`close_only`** marks an operator flatten (skips the direction gate, bypasses the live-trading connection gate).

(The old `EXCHANGE_ADAPTERS.md` sketched this instruction with illustrative names `target_side` / `target_notional_usd` / `margin_mode`; the keys above are the ones the code reads.)

Per instruction, the adapter's responsibilities are: resolve the symbol, size the order, close any existing/opposite position, open the new position, and return normalized fill information — each traceable in the method table and `execute()` semantics below. Leverage/margin-mode setting and post-submit close verification are **not** implemented (Known Gaps).

### Class structure of `CcxtExecutor`

`CcxtExecutor(BaseExecutor)` at `src/executors/ccxt_adapter.py:313`. One instance ↔ one venue+mode: the ccxt client is built lazily and cached on the instance (`self._cached_client`, `src/executors/ccxt_adapter.py:316-318,330-332`).

Public contract methods:

| Method | Line | Role |
|---|---|---|
| `execute(readiness)` | `ccxt_adapter.py:757` | Submit (multi-leg) orders for one readiness block; returns the normalized result envelope |
| `health()` | `ccxt_adapter.py:1228` | Balance + positions snapshot (`{ok, generated_at, exchange, account, positions}`) |
| `plan(readiness)` | — not overridden | Falls through to `BaseExecutor.plan` → `{"mode": "plan_not_implemented"}` (`src/executors/base.py:107-109`) |
| `get_order(inst_id, ord_id, cl_ord_id)` | `ccxt_adapter.py:1048` | Single order, normalized; `fetch_order` by id, else scan open/closed orders by client id |
| `get_open_orders(inst_id)` | `ccxt_adapter.py:1091` | Normalized pending orders; `[]` on any error |
| `get_order_history_raw(inst_ids, limit)` | `ccxt_adapter.py:1099` | Recent closed orders in **OKX-style raw row shape** (`instId`/`ordId`/`clOrdId`/`pnl`/`realized_pnl`/`reduceOnly`…) — feeds `reconcile_from_order_history` and dashboard close-row enrichment |
| `get_order_history_archive(inst_id, limit)` | `ccxt_adapter.py:1146` | Closed orders in the venue-neutral normalized order shape |
| `get_positions(inst_id)` | `ccxt_adapter.py:1154` | Normalized positions with signed `pos`, `upl`, and `realized_pnl` (`ccxt_adapter.py:1173`) |
| `get_balance(ccy)` | `ccxt_adapter.py:1181` | Per-currency list `{ccy, eq, avail, raw}` |
| `get_balance_summary(currency)` | `ccxt_adapter.py:1204` | Single-currency `{free, used, total, currency}` dict or `None` — backs the B2 balance-drift check; distinct from `get_balance` |

Key internals:

- `_exchange_id()` (`ccxt_adapter.py:320-328`) — venue resolution (below).
- `_client(close_only=False)` (`ccxt_adapter.py:330-429`) — builds/caches the ccxt client: venue class lookup via `getattr(ccxt, exchange_id)`, per-venue credential kwargs, `enableRateLimit=True`, `timeout=_submit_timeout_ms()` (derived from `HERMX_SUBMIT_TIMEOUT_SECONDS = 45.0`, `ccxt_adapter.py:31,115-119`), demo/live gating.
- `_market_spec(client, symbol)` (`ccxt_adapter.py:431-459`) — `load_markets()` + `market(symbol)` → `{contract_size, step, min_amount, min_cost, is_contract}`. **Fails closed**: a lookup failure re-raises so `execute()` records UNKNOWN instead of sizing on a fabricated `contract_size=1.0` (`ccxt_adapter.py:440-445`).
- `_position_snapshot(client, symbol)` (`ccxt_adapter.py:461-497`) — `fetch_positions([symbol])` → `{side, contracts, raw}`; recovers a blank ccxt `side` from venue-native `info.posSide`/`info.pos`/`info.positionAmt`, degrading to `"unknown"` (never defaults to `"long"`).
- `_expanded_actions(readiness, current_side)` (`ccxt_adapter.py:511-533`) — expands `CLOSE_OPPOSITE_IF_ANY` / synthesizes close+open legs from `target_direction`.
- `_order_params(...)` (`ccxt_adapter.py:554-576`) — per-venue order params: OKX-style `clOrdId`+`clientOrderId`+`tdMode` vs Hyperliquid `clientOrderId=<hashed cloid>` only; `reduceOnly=True` on close legs.
- `_contracts_for_notional` / `_amount_from_readiness` (`ccxt_adapter.py:620-660`) — notional→contracts sizing with decimal step flooring; distinguishes `below_instrument_min` (venue `limits.amount.min` / `limits.cost.min`) from plain `zero_size`.
- `_sufficient_free_balance(...)` (`ccxt_adapter.py:662-697`) — live-mode pre-trade margin check on the market's **settle currency**; OPEN leg only, fail-open, skipped entirely in demo.
- `_normalize_order` / `_state_from_ccxt` (`ccxt_adapter.py:699-734`) — ccxt order → normalized order dict; status mapping to `live/partially_filled/filled/canceled/not_found/unknown`.
- `_order_fully_filled(order, requested_amount)` (`ccxt_adapter.py:183-198`) — compares `filled` against the size *we* submitted (never the response's own `remaining`), so a partial IOC fill is never terminalized. Used only for Hyperliquid's fill-at-submit shortcut (`ccxt_adapter.py:978-992`).
- Module-level observe-only checks: `detect_position_drift` (B1, `ccxt_adapter.py:210-253`) and `check_balance_drift` (B2, `ccxt_adapter.py:256-310`) — never auto-correct, never block.

There are **no leverage / margin-mode setter helpers** in the adapter: `td_mode` passes through as the OKX `tdMode` order param (`ccxt_adapter.py:568-571`), and `leverage` is only consulted by the pre-trade balance check (`ccxt_adapter.py:915-920`). See Known Gaps.

### `execute()` semantics worth knowing before extending

- Multi-leg loop over expanded actions; each leg appends an `executed_orders` row with `status` in `{submitted, skipped, blocked, rejected}` (`ccxt_adapter.py:811-967`).
- Result `mode` mapping: all legs OK → `submit_enabled`; all legs skipped/failed → `submit_failed` (REJECTED); mixed success+failure → `submit_partial` (UNKNOWN, `ccxt_adapter.py:1012-1019`); ccxt timeout/NetworkError → `submit_timeout` (UNKNOWN, `_is_timeout_error`, `ccxt_adapter.py:122-135`); any other exception → `submit_exception` (UNKNOWN, `ccxt_adapter.py:1036-1046`). A timeout is *never* reported as a reject — the order may have reached the venue.
- `close_only=True` readiness (operator close / emergency flatten) skips the target-direction gate (`ccxt_adapter.py:773-785`) and, in live mode, bypasses the `HERMX_LIVE_TRADING` connection gate (`ccxt_adapter.py:415-426`) — the never-block-a-close invariant.
- Distinct client order ids per leg (`client_order_id_close` / `client_order_id_open`, `ccxt_adapter.py:762-765`) so a reversal's two legs aren't rejected as duplicates.
- All error strings pass through `redact_secrets()` before landing in payloads/logs (`src/security/credentials.py:196-238`).

### Venue selection

Two layers select "which code" and "which venue":

1. **Backend (adapter) selection** — `ExecutorFactory.create` reads `config["execution"]["exchange"]`, defaulting to `EXEC_BACKEND` (`src/executors/factory.py:66`), which is `HERMX_EXEC_BACKEND` env or `"ccxt"` (`src/webhook/config.py:10`). `resolve_key` lowercases and applies aliases — every legacy OKX key (`okx`, `okx_api`, `okx_sandbox`, `okx_demo`, `okx_ccxt`) maps to `ccxt` (`src/executors/factory.py:32-38,51-53`). Unknown keys raise with the available list. If the `ccxt` import failed at module load, the registry is empty and the receiver fails closed (`src/executors/factory.py:77-82`).
2. **Venue selection inside the adapter** — `_exchange_id()` reads `execution.ccxt_exchange`, falling back to `execution.exchange`; if the result is blank or the literal backend name `"ccxt"`, it falls back to `HERMX_CCXT_EXCHANGE` env, then `"okx"` (`src/executors/ccxt_adapter.py:320-328`). The `"ccxt"`-is-not-a-venue guard exists because `getattr(ccxt, "ccxt") → None` would otherwise silently disable reconciliation (see the regression note in `.claude/rules/code-quality.md`).

Reconciliation call sites must build the executor from the **order's own intent record** (venue, mode) — never from global defaults (code-quality rule "Executor hard-coded to a venue is a latent wrong-account landmine").

### Credential resolution and namespacing

`_client()` calls `resolve_exchange_credentials(exchange_id, os.environ, mode=...)` (`ccxt_adapter.py:341-342`), where `mode` is `"live"` iff `execution.simulated_trading` is falsey. `src/security/credentials.py:40-177` returns **only** the selected venue's vars, normalized to canonical names:

- Every venue has a namespaced sandbox tier and a plain (live) tier, e.g. `OKX_DEMO_API_KEY` → `OKX_API_KEY`, `BYBIT_TESTNET_*` → `BYBIT_*`, `BITGET_DEMO_*`, `GATE_TESTNET_*`, `COINBASE_SANDBOX_*`, `KUCOIN_PAPER_*`.
- `mode="demo"` prefers the sandbox-tier names with plain as fallback; `mode="live"` inverts the preference (`credentials.py:44-50`).
- **Hyperliquid exception** (`credentials.py:159-175`): auth is `HYPERLIQUID_WALLET_ADDRESS` + `HYPERLIQUID_PRIVATE_KEY` (wallet-based, no API key/passphrase), and the resolver **fails closed** — the pair is returned only when *both* are present, so a partial set yields `{}` (disarmed) and can never borrow another venue's keys. The adapter passes these as ccxt's `walletAddress`/`privateKey` kwargs (`ccxt_adapter.py:373-383`).
- `redact_secrets()` (`credentials.py:196-238`) scrubs every known credential value from error text; new venue keys must be added to its list.
- `resolve_executor_env()` (`credentials.py:180-193`) builds a least-privilege subprocess env (safe passthrough vars + selected-venue creds) — relevant only if a venue ever needs a subprocess helper.

### Demo vs live switch

Three layers, from strategy config down to the adapter:

1. Per-strategy `execution_mode` (demo/live) is threaded through the readiness block; the receiver resolves it to `simulated_trading` (`src/webhook_receiver.py:1191`), and `ExecutionService` copies both into the execution config handed to the factory (`src/execution/service.py:84-90`). The per-venue runtime profiles (`config/runtime.<venue>.demo.json`) document the demo posture via `execution.account: "demo"`.
2. The adapter reads **only** `execution.simulated_trading` (default `True`, i.e. safe): truthy → `client.set_sandbox_mode(True)`, and a venue whose ccxt class lacks sandbox support **fails closed** with `RuntimeError` (`ccxt_adapter.py:404-414`) — this is how Coinbase demo requests are refused.
3. Live mode additionally requires the global kill switch `HERMX_LIVE_TRADING=true` (`live_trading_enabled()`, `src/hermx_shared.py:54-73`; enforced at `ccxt_adapter.py:415-426` as defense-in-depth behind the service-level gate). Only a `close_only` flatten bypasses it.

### Normalized contracts (why venue-neutrality matters)

`BaseExecutor` fixes the contract, not the mechanism (`src/executors/base.py:10-16`):

- **`empty_fill_summary(client_order_id)`** (`base.py:26-36`): `{status, order_id, client_order_id, avg_fill_price, filled_size, fee_usd, slippage_pct, position_after_order}` — every adapter populates this shape (missing values stay `None`) so dashboards render any exchange identically.
- **`empty_normalized_order(exchange, state, raw)`** (`base.py:51-65`): `{exchange, inst_id, ord_id, cl_ord_id, state, acc_fill_sz, avg_px, ord_type, side, pos_side, ts, raw}` — the observe-only query shape. A not-found order is a normalized `{state: "not_found"}` row, **not** an exception, so reconciliation maps it deterministically (`base.py:49-50`).
- **`normalized_result(...)`** (`base.py:150-170`): the `{ok, mode, exchange, elapsed_ms, fill_summary, payload}` envelope every `execute()` returns; `payload` carries the raw venue response for forensics only.
- Query verbs default to safe no-ops (`state="not_implemented"` / `[]`, `base.py:111-147`) so a venue without a query path degrades instead of crashing reconciliation.

Reconciliation (`reconcile_from_order_history`) and the dashboard consume these shapes exclusively; any venue-specific field must be normalized **inside the adapter** before it crosses this boundary. That is the whole point of the next section.

## Venue-Specific Normalization (CCXT unified-field gaps)

The full audit with CCXT-source line evidence lived in CCXT_VENUE_NEUTRALITY_VALIDATION.md (doc since removed; it validated against installed CCXT 4.5.61 under `.venv`, not web docs). Summary of the ground truth: CCXT's `safe_order` emits `reduceOnly` and `fee` as **unified top-level keys**, but there is **no unified per-order realized-P&L key** — it only ever lives in each venue's raw `info` blob or a separate endpoint.

How the adapter handles each gap today:

### Realized P&L (NOT unified)

`_normalized_realized_pnl(order, info, exchange_id)` (`src/executors/ccxt_adapter.py:43-63`) is the single normalization point, feeding `get_order_history_raw`'s `realized_pnl` column (`ccxt_adapter.py:1132`):

| Venue | Source | Adapter behavior |
|---|---|---|
| OKX | `info.pnl` (string) | parsed to float (`ccxt_adapter.py:51-52`) |
| Hyperliquid | `info.closedPnl` — *different key*; a bare `info.get("pnl")` would silently record $0 | parsed to float (`ccxt_adapter.py:53-54`) |
| Binance | `trade.info.realizedPnl` — lives on **trades**, may be absent from the order row | best-effort from order `info.realizedPnl`, else `None` (`ccxt_adapter.py:55-57`) |
| Bybit | not in order history at all; only `fetch_positions_history` (`/v5/position/closed-pnl`) | `None`, debug-logged (`ccxt_adapter.py:58-63`) |
| Coinbase (spot) | none | `None` |

`None` is the accepted Phase-1 answer for venues without order-row P&L; Phase 2 backfill via positions-history / income endpoints is deferred (docstring at `ccxt_adapter.py:44-49`). Per the validation doc's #4: `fetch_positions_history` exists only on OKX and Bybit — do **not** build a portable P&L path on it. Consistent with the log-and-continue rule, a missing venue P&L is recorded as `None`, never coerced to a fabricated zero.

### `reduceOnly` (unified, with a venue quirk)

`_normalize_reduce_only(order, info)` (`ccxt_adapter.py:66-76`) reads the unified top-level `order["reduceOnly"]` first and falls back to the venue `info` blob; OKX returns the *string* `"true"`/`"false"` there, so string values are compared case-insensitively. Returns `None` when unknown. This closes the validation doc's Breaker 2 ("bare `info.get('reduceOnly')`" gap) — the fix it prescribed is now applied.

Coinbase (and any spot venue) never sets `reduceOnly` → `None`, so reconciliation's reduce-only close gate is a **derivatives-only assumption** (validation doc N3); a spot venue needs a different close-detection signal before its history can feed the ledger.

### Position-level `realizedPnl` (opt-in per venue)

Not part of CCXT's base position structure; only OKX and Bybit populate it. `get_positions()` now surfaces it as `realized_pnl` (`ccxt_adapter.py:1173`) and `health()` as `realizedPnl` with an `info` fallback (`ccxt_adapter.py:1250`) — closing the validation doc's N2. Consumers must treat it as OKX/Bybit-only; Binance and Hyperliquid positions report only unrealized P&L.

### Client order ids (Hyperliquid cloid)

Hyperliquid requires a 128-bit `0x`-prefixed hex cloid; `_to_hyperliquid_cloid` hashes the HermX id (`sha256[:32]`, `ccxt_adapter.py:88-94`) and it must be passed as `clientOrderId` (a `cloid` param is silently dropped by ccxt, `ccxt_adapter.py:558-563`). Because the venue echoes the *hashed* cloid, not the HermX `mxc…` id, the submit-time mapping is persisted via `pnl_cloid_map.record_cloid_mapping` (`_record_hl_cloid`, `ccxt_adapter.py:535-552`), and `get_order` hashes the query id before matching (`ccxt_adapter.py:1057-1082`). Note the hex-not-decimal guard rule: any `is_hermx_cl_ord_id` check must accept `startswith("0x")`, not just `isdigit()`.

## Supported Venues Today

All eight venues are wired for authenticated execution in `_client()`. **OKX is the only live-verified venue** (real demo submit → query → close via the gated `tests/test_paper_integration.py`); the other seven are untested against a real account. Trial posture across venues: USDT perpetual swaps, sandbox/demo first, isolated margin, 2x leverage, market execution. Per-venue conditionals inside `_client()`:

| Venue (`ccxt_exchange`) | Runtime profile | Demo credential env vars | `_client()` specifics |
|---|---|---|---|
| `okx` | `config/runtime.demo.json` | `OKX_DEMO_API_KEY/_SECRET_KEY/_PASSPHRASE` | apiKey+secret+password; `options.defaultType` from `ccxt_default_type`, default `"swap"` (`ccxt_adapter.py:348-356`) |
| `kucoin` | `config/runtime.kucoin.demo.json` | `KUCOIN_PAPER_API_KEY/_SECRET/_PASSPHRASE` | apiKey+secret+password, no defaultType override (`ccxt_adapter.py:357-364`) |
| `bybit` | `config/runtime.bybit.demo.json` | `BYBIT_TESTNET_API_KEY/_SECRET_KEY` | apiKey+secret (no passphrase); defaultType default `"swap"` (`ccxt_adapter.py:365-372`) |
| `hyperliquid` | `config/runtime.hyperliquid.demo.json` | `HYPERLIQUID_TESTNET_WALLET_ADDRESS/_PRIVATE_KEY` (or plain) | `walletAddress`+`privateKey`; fail-closed pair resolution (`ccxt_adapter.py:373-383`) |
| `binance` | `config/runtime.binance.demo.json` | `BINANCE_TESTNET_API_KEY/_SECRET_KEY` | defaultType default is `"future"` — not `"swap"` like OKX/Bybit (`ccxt_adapter.py:384-388`) |
| `bitget` | `config/runtime.bitget.demo.json` | `BITGET_DEMO_API_KEY/_SECRET_KEY/_PASSPHRASE` | apiKey+secret+password (`ccxt_adapter.py:389-392`) |
| `gate` / `gateio` | `config/runtime.gate.demo.json` | `GATE_TESTNET_API_KEY/_SECRET_KEY` | apiKey+secret; both id spellings accepted (`ccxt_adapter.py:393-395`) |
| `coinbase` / `coinbaseadvanced` | `config/runtime.coinbase.demo.json` | `COINBASE_SANDBOX_API_KEY/_SECRET_KEY` | apiKey+secret; demo **fails closed** because ccxt's coinbase class has no `set_sandbox_mode` (`ccxt_adapter.py:396-400,404-414`) |

Other adapter-level venue conditionals to be aware of (all keyed on `_exchange_id()`):

- Hyperliquid: reference price required even for market orders (slippage bound, `ccxt_adapter.py:807-809`); reduce-only close falls back to the position's own mark/entry price when the ticker feed is down (`_close_fallback_price`, `ccxt_adapter.py:601-618,843-847`); fully-filled submits are recorded `filled` directly because its order-status endpoint lags minutes behind a market IOC fill (`ccxt_adapter.py:978-992`) — OKX's submit→reconcile path stays byte-identical.
- Non-Hyperliquid venues get `clOrdId` + `clientOrderId` + optional `tdMode` order params (`ccxt_adapter.py:564-571`).
- Symbol mapping is shared: `BTC-USDT-SWAP → BTC/USDT:USDT`, `BTCUSDT → BTC/USDT`, `SOLUSDC → SOL/USDC` (Hyperliquid quotes in USDC) (`_inst_id_to_ccxt_symbol`, `ccxt_adapter.py:138-161`).

Runtime profiles are thin: they configure the strategy engine (schema enforcement, strategy routing), not the venue itself — see `config/runtime.demo.json` and `config/runtime.hyperliquid.demo.json` (which also documents the "new venue stays DISARMED until its gated write test passes" posture). The venue is picked by `execution.ccxt_exchange` in the execution config / readiness, not by the profile filename.

## Extending the Adapter

### Adding a new venue

The design goal is *zero new connector code* unless the venue's auth or order semantics are non-standard:

1. **Check CCXT support and sandbox capability.** The venue class must exist as `getattr(ccxt, "<id>")` and, for demo use, implement `set_sandbox_mode` — otherwise demo requests fail closed (`ccxt_adapter.py:337-339,404-414`), which is the correct behavior, not a bug.
2. **Factory registration** — usually nothing to do. The backend stays `ccxt`; only add an `ExecutorFactory.alias(...)` (`src/executors/factory.py:46-48`) if legacy configs use a different exchange key. A genuinely new *backend* (non-CCXT) would be a one-line `ExecutorFactory.register(...)` (`factory.py:40-43,77-82`).
3. **Namespaced credentials** — add a branch to `resolve_exchange_credentials` in `src/security/credentials.py` following the `<VENUE>_<SANDBOXTAG>_<FIELD>` → `<VENUE>_<FIELD>` convention (e.g. `BYBIT_TESTNET_*` → `BYBIT_*`), with the demo/live preference inversion. **Never reuse another venue's keys.** Add every new env name to the `redact_secrets` key list (`credentials.py:200-234`). If the venue's auth shape is non-standard, fail closed on a partial set the way the Hyperliquid branch does (`credentials.py:159-175`).
4. **Runtime profile** — add `config/runtime.<venue>.demo.json` (copy an existing one; note the DISARMED-until-write-test posture in the `notes`).
5. **`_client()` branch** — add an `elif exchange_id ...` mapping the resolved credential names onto the ccxt constructor kwargs (`ccxt_adapter.py:348-400`). Set `options.defaultType` if the venue needs it (check whether the venue calls perpetuals `"swap"` or `"future"` — Binance vs OKX differ). Non-standard auth (wallet keys, JWTs) follows the Hyperliquid pattern: dedicated kwargs, dedicated credential branch.
6. **Verify field semantics against the installed CCXT source** (`.venv/lib/python*/site-packages/ccxt/<venue>.py` — `parse_order`, `parse_position`, and `base/exchange.py::safe_order`) **before assuming any field is unified.** See the next-but-one subsection.
7. **Extend the normalization helpers** if the venue exposes realized P&L or other non-unified fields: `_normalized_realized_pnl` (`ccxt_adapter.py:43-63`) and, if needed, `_normalize_reduce_only`.
8. **Tests**: unit tests with a fake-client + fake-`ccxt`-namespace mirroring `tests/test_ccxt_adapter.py` (auth-kwargs test per venue, e.g. `test_ccxt_adapter_bitget_auth_kwargs`, `tests/test_ccxt_adapter.py:653-664`; plus any venue-specific order-param behavior), and a **gated sandbox write test** in `tests/test_paper_integration.py` behind `HERMX_RUN_<VENUE>_PAPER_TESTS` / `HERMX_RUN_<VENUE>_WRITE_TESTS` env flags (`tests/test_paper_integration.py:13-19`). The venue is not live-capable until the gated write test has passed against its real sandbox.

### Adding a new normalized capability

To add a query verb or a new fill-summary/normalized-order field:

1. **Extend the contract in `src/executors/base.py` first.** New query verbs get a safe default (empty list / `not_implemented` state) so other/legacy adapters degrade instead of crashing reconciliation (`base.py:111-147`). New fields go into `empty_fill_summary` (`base.py:26-36`) or `empty_normalized_order` (`base.py:51-65`) with a `None` default — every consumer must tolerate `None`.
2. Implement it in `CcxtExecutor`, normalizing venue fields at the adapter boundary (nothing venue-shaped crosses into reconciliation/dashboard). Follow the existing error posture: observe-only reads swallow exceptions into empty/`None` results (`get_positions`, `get_balance_summary`), while anything on the submit path maps timeouts to `submit_timeout`.
3. If the capability is distinct from an existing verb, keep it distinct — precedent: `get_balance_summary` was added as a new method rather than changing `get_balance`'s list contract (`ccxt_adapter.py:1204-1211`).
4. **Test coverage expected**: a shape test against a fake client (cf. `test_get_order_history_raw_shape`, `tests/test_ccxt_adapter.py:198-210`), a degradation test (venue read raising → empty/`None`), and — if the field feeds P&L or reconciliation — an end-to-end round-trip test through the real production path, not hand-injected rows (see the C1 lesson in `.claude/rules/code-quality.md`: tests that bypass the reconcile attribution seam mask regressions).

### Handling a new CCXT-unified-field gap

When a venue's number looks unified but isn't (the `info.pnl` vs `info.closedPnl` class of bug):

1. **Ground truth is the installed CCXT source**, `.venv/.../site-packages/ccxt/` — not web docs, not memory (method established in the former CCXT_VENUE_NEUTRALITY_VALIDATION.md audit). Check `base/exchange.py::safe_order` (or `parse_position`) for whether the key is a unified top-level field at all, then each venue's `parse_order`/`parse_position`/`parse_trade` for whether and where it's populated.
2. If it's not unified, add a per-venue mapping helper at module level (pattern: `_normalized_realized_pnl`), keyed on `exchange_id`, returning `None` for venues that don't expose it — never a fabricated zero, per the log-and-continue rule.
3. If it *is* unified but a venue populates it oddly (OKX's string `"true"`/`"false"` `reduceOnly`), read the unified key first with an `info` fallback and type-coerce defensively (pattern: `_normalize_reduce_only`, `ccxt_adapter.py:66-76`).
4. Document the finding here (§ Venue-Specific Normalization, or a successor audit doc) with CCXT source line references, and pin the behavior with a per-venue unit test so a CCXT version bump that changes the field surfaces as a test failure.

## Testing & Validation

- **Unit tests** (no network): `tests/test_ccxt_adapter.py` — run with `./.venv/bin/pytest tests/test_ccxt_adapter.py -x -q`. Patterns worth copying:
  - Fake ccxt client with recorded `create_order` calls for leg/param assertions (`_FakeClient`, `tests/test_ccxt_adapter.py:18-139`); always installed via `monkeypatch.setattr(ex, "_client", ...)`, never direct assignment.
  - Fake `ccxt` module namespace for `_client()` auth-kwargs tests (`_install_fake_ccxt`, `tests/test_ccxt_adapter.py:621-634`) — verifies credential injection per venue without importing real venue classes.
  - Regression pins for every venue-specific behavior: Hyperliquid cloid shape and param placement (`tests/test_ccxt_adapter.py:294-318`), OKX params byte-identical guard (`:332-366`), operator close without target direction (`:392-429`), market-spec fail-closed (`:447-474`), blank-side close recovery (`:507-557`), fill-at-submit Hyperliquid-only (`:921-958`), pre-trade balance check incl. never-block-a-close (`:1032-1135`).
- **Gated sandbox integration tests** (real venue sandbox, opt-in): `tests/test_paper_integration.py`, armed per venue via `HERMX_RUN_<VENUE>_PAPER_TESTS=true` (read-only) and `HERMX_RUN_<VENUE>_WRITE_TESTS=true` (submit/close) (`tests/test_paper_integration.py:13-19`). The OKX-demo submit → query → close pass through this file is what makes OKX the live-verified reference venue; a new venue mirrors it and stays disarmed until its write test passes.
- Full suite: `./.venv/bin/pytest -x -q` (~17s; network-leaking fixtures were stubbed — keep it that way by faking `_client` in any new dashboard/adapter test).

## Known Gaps / Deferred Work

- **`plan()` is not implemented** — falls through to `BaseExecutor.plan` → `{"mode": "plan_not_implemented"}` (`src/executors/base.py:107-109`). There is no dry-run order preview at the adapter level.
- **No leverage / margin-mode management.** "Set or verify leverage / margin mode" is a canonical adapter responsibility, but `CcxtExecutor` never calls ccxt's `set_leverage`/`set_margin_mode`; `td_mode` is passed per-order (OKX-style) and `leverage` only informs the balance check. Leverage must currently be pre-set on the venue account.
- **No post-submit close verification.** "Verify close" was likewise a listed responsibility, but after a close leg submits, `execute()` assumes `current_side = "flat"` and proceeds to the open leg without re-fetching the position (`ccxt_adapter.py:858-860`). The result mapper recognizes a `close_not_verified` leg status (`ccxt_adapter.py:971`; also handled in `src/skills/hermes_execution.py:209`) but nothing in the adapter produces it — close verification is left entirely to reconciliation.
- **Hedge-mode `posSide` is out of scope** — the `ccxt_pos_mode` branch was removed as dead code; orders never emit `posSide` (`tests/test_ccxt_adapter.py:189-190`). One-way position mode is assumed.
- **Realized P&L is `None` in order history for Bybit, KuCoin, Bitget, Gate, Coinbase** — Phase 1 accepts `None`; the Phase 2 backfill via `fetch_positions_history` (OKX/Bybit only) or income endpoints (Binance) is deferred (`ccxt_adapter.py:44-49,58-63`; validation doc #4).
- **Reduce-only close detection is derivatives-only** — spot venues (Coinbase) report `reduceOnly=None`, so reconciliation's reduce-only gate drops every spot close (validation doc N3). Spot needs a separate close signal before its history can feed `closed-trades.jsonl`.
- **Seven of eight venues are untested against a real account**; their gated write tests have not been run. OKX is the only live-verified venue.
- **`get_order_history_raw` with no `inst_ids` returns `[]`** — the target list is not discovered from open positions or markets (`ccxt_adapter.py:1102-1105`); callers must supply instruments.
- **Observe-only query verbs swallow exceptions into empty results** (`get_open_orders`, `get_positions`, `get_balance`, history verbs) — intentional fail-open for reconciliation, but it means a venue auth failure looks identical to "no data" on those paths; `health()` is the verb that surfaces the error (`ccxt_adapter.py:1273-1274`).
- **`ORDER_PNL_IS_NET` is gross-first**: displayed P&L semantics per venue (fee-inclusion) must be empirically verified on a real close before flipping to net — one of the genuinely runtime-only facts static analysis cannot close (`.claude/rules/code-quality.md`).
- **One cached client per executor instance** (`ccxt_adapter.py:330-332`) — an instance is permanently bound to the (venue, mode) it first connected with. Reconciliation must construct executors from each order's own intent record, never reuse a global instance across venues.
