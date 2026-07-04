# SnapTrade Integration — Research & Feasibility

**Status:** Research / feasibility. No implementation.
**Author:** Engineering
**Date:** 2026-07-03
**Scope:** Evaluate SnapTrade as a new execution + aggregation backend for HermX, alongside the existing CCXT crypto-exchange path.

---

## 1. Executive Summary

**SnapTrade** is a Canadian fintech (YC W22, ~16 people, ~$7.5M raised) offering a unified brokerage API — "Plaid for trading." A single integration reaches **35+ retail brokers** (Interactive Brokers, Fidelity, Schwab, Robinhood, Alpaca, Webull, Coinbase, and more). It normalizes **holdings, balances, transactions, and orders** across every connected account, and can **place market/limit orders** for stocks, ETFs, options, and crypto through the user's *existing* brokerage — without HermX ever holding brokerage credentials or becoming a broker itself.

**Why consider it for HermX.** HermX today is a **crypto-perpetuals-only** execution layer: TradingView → webhook → CCXT adapter → OKX/Binance/Bybit/Bitget/KuCoin/Hyperliquid/Gate/Coinbase. It cannot touch equities, ETFs, options, or the large universe of US retail brokers where most non-crypto capital actually lives. SnapTrade fills that gap with one middleware dependency instead of N per-broker integrations, each of which (unlike crypto CEX APIs) typically requires OAuth partnerships, per-broker order schemas, and compliance review.

**The core trade-off:** SnapTrade converts HermX from "asset-class specialist (crypto perps, direct API, free)" toward "multi-asset router (crypto direct + equities/options via paid middleware)." It adds an external dependency, per-connected-user pricing, and middleware latency, in exchange for an enormous surface expansion HermX cannot practically build alone.

**Bottom line (see §10):** **Conditional GO for a read-only aggregation pilot; NO-GO on live signal execution until a paper-trading pilot proves fill parity and the reconciliation model holds.** SnapTrade slots in as a *new `BaseExecutor` subclass* + a *read-only aggregation source*, not a rewrite.

---

## 2. HermX Current Architecture (recap)

**Signal → execution → reconcile:**

```
TradingView alert
   │  HTTP POST (HMAC-signed)
   ▼
webhook_receiver.py ── validate schema, dedupe, fsync to raw-webhooks.jsonl (WAL)
   │  enqueue → dequeue → write signals.jsonl (dedupe ledger)
   ▼
execution/service.py ── resolve strategy, apply gates
   │      gates: arming · auth health · watchdog · HERMX_LIVE_TRADING kill switch
   │             · symbol pause · idempotency (cl_ord_id dedupe) · write-ahead order journal
   ▼
executors/ccxt_adapter.py (BaseExecutor subclass) ── submit or dry-run
   │  returns normalized ExecutionResult (base.py)
   ▼
post-submit reconciliation ── get_order / get_positions / get_balance (observe-only)
   ▼
append-only JSONL ledgers: pipeline.jsonl · alerts.jsonl · order-journal.jsonl
   ▼
dashboard.py + Next.js SPA (:8098) ── positions / orders / signals, per-strategy pause/demo/live
```

**Executor model.** Every venue implements the `BaseExecutor` contract in `src/executors/base.py`:
- `execute(readiness) -> normalized result` (the write path)
- observe-only read path: `get_order`, `get_open_orders`, `get_positions`, `get_balance`, `get_order_history_*`

The receiver never knows which venue it's talking to — it builds a venue-neutral `execution_readiness` block and hands it to whichever executor the active config selected. Adapters return **canonical normalized shapes** (order / position / balance / fill_summary) so the dashboard and reconciliation are fully decoupled from venue payloads. A not-found order normalizes to `{state: "not_found", raw: ...}` rather than throwing — reconciliation maps it deterministically.

**Strategy files** (`strategies/*.json`, `schema_version: 2`) declare:
```json
{ "instrument": { "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap" },
  "capital": { "budget_usd": 1500, "reinvest": true },
  "execution_mode": "demo", "leverage": 2, "margin_mode": "isolated" }
```
Credentials are **env-resolved** — no inline secrets in strategy files. Execution mode is a per-strategy pill (`demo` / `live` / paused), and `HERMX_LIVE_TRADING` is the global kill switch above all of it.

**Key insight for this doc:** SnapTrade is *just another `BaseExecutor` subclass* plus a read-only aggregation source. The abstraction already exists; the question is whether SnapTrade's semantics (equities/options, OAuth per user, async fills) fit the contract cleanly, or strain it.

---

## 3. SnapTrade Capabilities vs HermX Gaps

### 3.1 Broker/venue coverage

| Capability | HermX today (CCXT) | SnapTrade | Gap filled |
|---|---|---|---|
| Crypto CEX (OKX, Binance, Bybit, Bitget, KuCoin, Gate) | ✅ direct API | ⚠️ partial (Coinbase, some) | — (HermX already stronger here) |
| Crypto perps / swaps / leverage | ✅ core competency | ❌ not the model | — (**HermX-only**) |
| Hyperliquid (on-chain perp DEX) | ✅ | ❌ | — (**HermX-only**) |
| **US equities / ETFs** | ❌ | ✅ | ✅ **new** |
| **Options** | ❌ | ✅ | ✅ **new** |
| **Interactive Brokers (IBKR)** | ❌ | ✅ | ✅ **new** |
| **Fidelity** | ❌ | ✅ | ✅ **new** |
| **Schwab / TD** | ❌ | ✅ | ✅ **new** |
| **Robinhood** | ❌ | ✅ | ✅ **new** |
| **Alpaca (paper + live)** | ❌ | ✅ | ✅ **new** (native paper-trading path) |
| **Webull, Vanguard, retail brokers (35+)** | ❌ | ✅ | ✅ **new** |

### 3.2 Functional coverage

| Function | HermX today | SnapTrade | Notes |
|---|---|---|---|
| Normalized holdings/positions | ✅ (`get_positions`) | ✅ across all brokers | Direct mapping (§5) |
| Normalized balances | ✅ (`get_balance`) | ✅ | Direct mapping |
| Normalized orders | ✅ (`get_order`, `get_open_orders`) | ✅ | Direct mapping |
| Transaction / activity history | ⚠️ order-journal only | ✅ full activity feed | SnapTrade richer (dividends, transfers, fees) |
| Place market / limit order | ✅ (crypto) | ✅ (equity/ETF/option/crypto) | New asset classes |
| Idempotent submit (client order id) | ✅ `cl_ord_id` dedupe | ⚠️ verify SnapTrade dedupe semantics | **Open question** — see §9 |
| Real-time push updates | ⚠️ poll-based reconcile | ✅ webhooks | Could feed HermX reconciliation |
| Paper / demo path | ✅ per-exchange demo configs | ✅ Alpaca paper natively | Complementary |
| Leverage / margin mode control | ✅ (perps) | ❌ (retail cash/margin accounts) | **HermX-only**; retail brokers don't expose perp-style leverage |

**Summary of gaps SnapTrade fills:** the entire **US retail equities/ETF/options universe** and **35+ brokers** HermX has zero reach into today — most notably IBKR, Fidelity, Schwab, Robinhood, and Alpaca (with a first-class paper-trading path). **Gaps SnapTrade does *not* fill:** crypto perpetuals, on-chain venues (Hyperliquid), and fine-grained leverage/margin control — HermX's direct-CCXT path remains superior and should stay primary for crypto.

---

## 4. Integration Scenarios

### (a) Read-only portfolio aggregation — *lowest risk, highest immediate value*
Link one or more brokerage accounts via SnapTrade; HermX **reads** normalized holdings/balances/orders and renders them in the dashboard alongside crypto positions. No order placement, so **no execution gates involved** and no exposure to fill/idempotency risk.

- **Implementation:** a new *read-only aggregation source* (not a full `BaseExecutor`) that calls SnapTrade's holdings/balances/activity endpoints and maps into the canonical `position` / `balance` / `order` shapes.
- **Dashboard:** new "External Brokers" panel driven by the same normalized shapes the existing panels already consume — minimal UI work.
- **Value:** unified net-worth / exposure view across crypto + equities. Pilot-friendly (free up to 5 connections).
- **Verdict:** **Start here.** Proves the API, mapping, and webhook plumbing with zero money at risk.

### (b) Signal-driven trade execution through retail brokers — *high value, highest risk*
A TradingView signal for, say, `SPY` routes through the normal pipeline but selects a **SnapTrade executor** instead of CCXT. HermX places the order in the user's IBKR/Alpaca/etc. account.

- **Implementation:** a `SnapTradeExecutor(BaseExecutor)` implementing `execute()` (place order) + observe-only reads (`get_order`, `get_positions`, `get_balance`).
- **Strategy file:** `instrument.exchange` becomes a SnapTrade broker/account reference; `type` becomes `equity` / `option`; leverage/margin_mode inapplicable for cash accounts (validator must reject them).
- **Gate reuse:** arming, auth health, watchdog, `HERMX_LIVE_TRADING`, symbol pause, and the write-ahead order journal all apply unchanged. **Idempotency and reconciliation are the open risks** (§9) — SnapTrade's async fill model and dedupe guarantees differ from a direct CEX API.
- **Verdict:** **Only after (a) ships and a paper-trading pilot on Alpaca proves fill parity + reconciliation.**

### (c) Multi-account operator dashboard — *natural extension of (a)*
An operator managing multiple end-users/accounts sees every linked brokerage in one dashboard: aggregate exposure, per-account P&L, cross-broker position drift.

- **Implementation:** builds directly on (a); adds per-user connection management (SnapTrade user registration + connection portal) and an account selector in the SPA.
- **Pricing sensitivity:** this is where **per-connected-user cost scales** (§7) — each linked user beyond the free 5 is $1.50/mo.
- **Verdict:** viable once (a) is proven; watch unit economics before onboarding many users.

---

## 5. Data Model Mapping

SnapTrade's normalized objects map cleanly onto HermX's canonical shapes in `src/executors/base.py`. This is the strongest technical argument for feasibility — both systems already normalize, so the adapter is a field-mapping exercise, not a paradigm shift.

### Order → `empty_normalized_order()` shape
HermX shape: `{exchange, inst_id, ord_id, cl_ord_id, state, acc_fill_sz(float), avg_px(float|None), ord_type, side, pos_side, ts, raw}`

| HermX field | SnapTrade source | Notes |
|---|---|---|
| `exchange` | brokerage/account identifier | Not a CEX id; use the SnapTrade account/institution slug |
| `inst_id` | universal symbol / ticker | e.g. `SPY`, or option OCC symbol |
| `ord_id` | brokerage order id | |
| `cl_ord_id` | client order id (if supported) | **Verify SnapTrade honors client-supplied ids** (§9) |
| `state` | order status | Map `EXECUTED/PENDING/CANCELED/REJECTED` → HermX states; unknown → `not_found` |
| `acc_fill_sz` | filled quantity | float |
| `avg_px` | execution price | float or None |
| `ord_type` | `MARKET` / `LIMIT` | direct |
| `side` | `BUY` / `SELL` | direct |
| `pos_side` | — | **N/A for cash equities** (no long/short position side); leave `None`. Options/margin may differ. |
| `ts` | order timestamp | |
| `raw` | untouched SnapTrade payload | forensics, per existing contract |

### Position → normalized `position`
HermX shape: `{exchange, inst_id, pos(float, signed), pos_side, avg_px, upl, raw}`

| HermX field | SnapTrade source | Notes |
|---|---|---|
| `pos` | quantity (signed) | long = +, short = −; most retail equity is long-only |
| `pos_side` | derive from sign | `None` for long-only cash accounts |
| `avg_px` | average purchase price | |
| `upl` | open P&L | SnapTrade provides unrealized P&L / market value |
| `raw` | SnapTrade payload | |

### Balance → normalized `balance`
HermX shape: `{exchange, ccy, eq(float), avail(float), raw}`

| HermX field | SnapTrade source | Notes |
|---|---|---|
| `ccy` | account currency | `USD`, `CAD`, etc. |
| `eq` | total equity / account value | |
| `avail` | buying power / cash available | Cash vs margin buying power differ — pick the conservative field |
| `raw` | SnapTrade payload | |

### fill_summary → `empty_fill_summary()`
`{status, order_id, client_order_id, avg_fill_price, filled_size, fee_usd, slippage_pct, position_after_order}` — maps directly from a SnapTrade order-status response. `slippage_pct` may require computing from expected vs actual fill (as CCXT adapter does today). `fee_usd` availability varies by broker.

**Mapping risks:**
- **`pos_side` / leverage / margin_mode** are perp-native concepts with no cash-equity equivalent — the SnapTrade adapter and strategy validator must treat them as N/A, not zero.
- **Options** introduce a multi-leg / contract-multiplier dimension the current single-`inst_id` shape doesn't model. Options support likely needs a shape extension — **defer options to a later phase.**
- **Fees/slippage** completeness varies per broker; `None` is acceptable per the existing contract (dashboards already tolerate `None`).

---

## 6. Authentication & Security

### Two different auth planes
1. **HermX inbound (unchanged):** TradingView → webhook_receiver uses **HMAC-signed** requests; HermX venue credentials are **env-resolved**, never in strategy files. This stays exactly as-is.
2. **HermX ↔ SnapTrade (new):** a **server-side** plane. HermX authenticates to SnapTrade with a **Client ID + Consumer Key** (env-resolved, same discipline as CCXT keys). Per-user access uses a **user id + user secret** issued at registration.

### End-user consent (OAuth-style connection flow)
- HermX registers a SnapTrade user → receives a `userSecret` (store **encrypted / env-managed**, treat as a credential).
- HermX generates a **connection portal URL / redirect**; the end-user authenticates *directly with their brokerage* (OAuth or broker-native flow).
- **HermX never sees brokerage credentials** — the same trust model as Plaid. This is a meaningful *security upside*: HermX's blast radius does not include broker passwords.
- On success, SnapTrade holds the brokerage connection; HermX holds only the SnapTrade `userSecret` handle.

### Token rotation & session expiry
- Brokerage connections **expire** (broker-dependent; some require periodic re-auth). SnapTrade emits **connection-broken / expiry webhooks** — HermX must handle these as a first-class state, mirroring the existing **auth-health gate**: a broken SnapTrade connection should **fail closed** (block execution for that account, surface in the dashboard), never silently no-op.
- `userSecret` rotation: support rotation and encrypted-at-rest storage from day one.
- **Design principle:** treat SnapTrade connection health exactly like HermX treats venue auth health today — a gate that blocks the write path when unhealthy.

### Consent & data-privacy posture
- Read-only scenarios (§4a) still transmit users' financial holdings through SnapTrade → HermX. Document data handling, retention, and that ledgers may persist normalized positions.
- Trade-execution scenarios require **explicit per-user consent** to place orders — surface this in the connection flow and log consent.

---

## 7. Pricing & Cost Model

**SnapTrade:** free up to **5 connections**, then **$1.50 / connected user / month**; enterprise from **$1,000/mo**.
**HermX today (CCXT):** effectively **$0** — direct exchange APIs, no middleware fee.

### Operator cost by scale

| Scenario | Connected users | Monthly SnapTrade cost | Notes |
|---|---|---|---|
| Personal / pilot | 1–5 | **$0** | Free tier covers solo operator + a few accounts |
| Small operator | 20 | ~$30 (20 × $1.50) | assuming standard per-user tier |
| Mid operator | 100 | ~$150 | linear until enterprise makes sense |
| Larger / SaaS | 500 | ~$750 | approaching enterprise-floor economics |
| Enterprise | 700+ | **$1,000+/mo floor** | flat/negotiated; per-user math crosses over ~667 users |

*(Verify current published pricing and whether "connection" vs "connected user" is billed identically before committing — pricing pages change.)*

### Cost interpretation
- **Solo operator (the current HermX profile): $0.** The free tier fully covers a single operator aggregating their own IBKR/Fidelity/Alpaca alongside crypto. **This makes the read-only pilot (§4a) zero-cost.**
- **Cost scales with *users*, not trade volume or AUM** — the opposite of exchange fee models. A high-frequency single-account strategy costs the same as a buy-and-hold one.
- **Crypto stays free:** keep CCXT primary for crypto; SnapTrade is additive, not a replacement. HermX never pays SnapTrade for OKX/Binance flow it already does directly.
- **Break-even vs building broker integrations yourself:** building even *one* compliant IBKR/Fidelity integration (OAuth partnership, order schema, cert, maintenance) far exceeds years of SnapTrade fees. For multi-broker reach, SnapTrade is drastically cheaper than in-house.

---

## 8. Latency & Performance

| Dimension | Direct CCXT (today) | Via SnapTrade | Impact |
|---|---|---|---|
| Order round-trip | 1 hop (HermX → exchange) | 2 hops (HermX → SnapTrade → broker) | **+middleware latency**; broker-dependent |
| Fill model | Mostly synchronous ack | Often **async** (submit → poll/webhook for fill) | Reconciliation must be **async-aware** |
| Rate limits | Per-exchange, well-understood | SnapTrade-imposed + per-broker | New quota surface to design around |
| Read/reconcile | Direct venue query | SnapTrade poll **or webhook push** | Webhooks could *reduce* poll load vs today |
| Market-data freshness | Real-time from exchange | Broker-dependent, may lag | Not a HFT path |

**Assessment.** SnapTrade is **not** for latency-sensitive strategies. Retail brokers + middleware add hops and often confirm fills **asynchronously** — HermX's write-ahead order journal + post-submit reconciliation model is *well-suited* to this (submit, journal, then reconcile via webhook or poll). But the reconciliation loop must not assume a synchronous fill. **This is fine:** HermX signals are TradingView bar-close events (2h–4h timeframes in current strategies), not sub-second — the added latency is immaterial at these horizons. **Positive side effect:** SnapTrade **webhooks** could replace some polling in reconciliation, potentially *reducing* load.

---

## 9. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **Idempotency semantics unknown.** SnapTrade dedupe/client-order-id guarantees may differ from CEX APIs; a retry could double-submit real money. | 🔴 High | **Blocker for §4b.** Verify client-order-id support in a paper pilot *before* any live path. Keep HermX-side `cl_ord_id` dedupe + write-ahead journal as the backstop; treat SnapTrade dedupe as best-effort until proven. |
| **Dependency risk.** ~16-person startup as a hard dependency in the money path. Outage/acquisition/shutdown breaks execution for all SnapTrade venues. | 🔴 High | Keep **CCXT primary** for crypto (no SnapTrade dependency there). Design the SnapTrade executor as **optional/degradable** — its outage must fail closed for its own venues only, never affect crypto. Aggregation (§4a) failure is read-only (cosmetic). |
| **Async fill / reconciliation gap.** A submitted order whose fill arrives late (or never, via webhook miss) leaves HermX state ambiguous. | 🟠 Med | Async-aware reconciliation; webhook + poll fallback; `not_found`→`pending`→terminal state machine. Reuse existing `state: "not_found"` normalization. |
| **Session expiry / re-auth churn.** Broker connections break periodically; users must re-link. | 🟠 Med | Treat connection health as an **auth-health gate** (fail closed). Handle expiry webhooks; surface a clear "re-link required" state in dashboard. |
| **Per-user pricing scaling.** Costs grow with connected users at operator scale. | 🟡 Low–Med | Model unit economics before multi-user onboarding (§7). Free tier covers solo/pilot. |
| **Regulatory framing.** Placing orders in users' accounts on their behalf. | 🟠 Med | **HermX does not become a broker** — SnapTrade is the regulated middleware and the user's own brokerage executes. But confirm HermX's role (order routing on behalf of users) doesn't trigger advisor/broker obligations in target jurisdictions. **Legal review before §4b at any multi-user scale.** |
| **Data privacy.** Users' full financial holdings flow through SnapTrade → HermX ledgers. | 🟠 Med | Document retention; encrypt `userSecret`; disclose data handling; consider whether normalized holdings should persist in JSONL or be memory-only. |
| **Options model gap.** Current single-`inst_id` shape doesn't model multi-leg/contract-multiplier. | 🟡 Low | **Defer options.** Ship equities/ETFs first; extend shape later. |
| **Vendor pricing change.** Startup pricing can shift. | 🟡 Low | Re-verify pricing at commit time; keep CCXT fallback so crypto is never hostage to SnapTrade terms. |

---

## 10. Go / No-Go Recommendation

### Recommendation
- **§4a Read-only portfolio aggregation — ✅ GO (pilot).** Low risk, zero cost at solo scale, high immediate value (unified crypto + equities view). Proves the API, mapping (§5), auth flow (§6), and webhook plumbing with **no money at risk**.
- **§4b Signal-driven execution — 🟡 CONDITIONAL GO → currently NO-GO.** Gated on a successful paper-trading pilot (Alpaca paper) that proves idempotency and reconciliation parity. Do **not** enable a live SnapTrade write path until pre-conditions below are met.
- **§4c Multi-account operator dashboard — 🟡 DEFER.** Build after §4a proves out; gate on unit-economics and legal review before onboarding many users.

### Architecture verdict
SnapTrade fits HermX's existing abstractions **cleanly**: a `SnapTradeExecutor(BaseExecutor)` for the write path and a read-only aggregation source for holdings/balances. Normalized shapes map directly (§5). Existing gates (arming, auth-health, watchdog, kill switch, symbol pause, journal, reconciliation) **reuse without redesign**. It is an *additive backend*, not a rewrite — and crucially, **crypto stays on direct CCXT** with no SnapTrade dependency.

### Pre-conditions before live execution (§4b)
1. **Idempotency proven.** Confirm SnapTrade honors client-supplied order ids (or that HermX-side `cl_ord_id` dedupe + journal fully covers double-submit). **Hard blocker.**
2. **Reconciliation proven.** Async fill handled end-to-end via webhook + poll fallback on Alpaca paper; `not_found→pending→terminal` state machine validated.
3. **Connection-health gate implemented.** Expiry/broken-connection webhooks fail closed, mirroring auth-health.
4. **Kill-switch coverage.** `HERMX_LIVE_TRADING` and per-strategy pause verified to block the SnapTrade path identically to CCXT.
5. **Strategy schema extended + validated.** `type: equity`, broker/account reference in `instrument`; validator **rejects** leverage/margin_mode for cash accounts; `execution_mode: demo` maps to Alpaca paper.
6. **Legal check.** Confirm order-routing-on-behalf-of-users doesn't trigger broker/advisor obligations at intended scale.
7. **Pricing re-verified** at commit time.

### Suggested phasing
1. **Phase 0 — Spike:** register a SnapTrade sandbox user, link one Alpaca paper account, prototype the read mapping. (days)
2. **Phase 1 — Aggregation (§4a):** ship read-only "External Brokers" dashboard panel. Zero write risk. (small)
3. **Phase 2 — Paper execution:** `SnapTradeExecutor` against Alpaca paper; validate pre-conditions 1–5. (medium)
4. **Phase 3 — Live (gated):** enable live for one broker behind `HERMX_LIVE_TRADING` after Phase 2 sign-off + legal (pre-conditions 6–7).
5. **Phase 4 — Multi-account (§4c):** only if operator use-case materializes; re-check unit economics.

---

## Appendix — Open Questions to Resolve in Phase 0

- Does SnapTrade support **client-supplied idempotency keys** on order placement? (Determines whether §4b is safe.)
- What is the **fill-confirmation model** per target broker (sync ack vs webhook vs poll-only)?
- Which **webhook events** exist (connection broken, order filled, holdings updated) and their delivery guarantees (at-least-once? ordering?)?
- **Fee/slippage field availability** per broker (affects `fill_summary` completeness).
- Exact **rate limits** (SnapTrade global + per-broker).
- Current **pricing** — "connection" vs "connected user" billing, and enterprise crossover.
- **Options order shape** — multi-leg/contract-multiplier representation (informs a future `base.py` extension).
