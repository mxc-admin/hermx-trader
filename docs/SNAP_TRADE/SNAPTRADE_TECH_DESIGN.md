# SnapTrade Integration — Technical Design

**Status:** Design. No implementation.
**Author:** Engineering
**Date:** 2026-07-03
**Prereq reading:** [`docs/SNAPTRADE_RESEARCH.md`](./SNAPTRADE_RESEARCH.md) (feasibility, pricing, go/no-go) and [`docs/SNAPTRADE_API_FINDINGS.md`](./SNAPTRADE_API_FINDINGS.md) (grounded API contract — real endpoint paths, field names, idempotency, webhooks, sandbox). This document is the *implementation blueprint* — the how, not the whether. Where the research doc concluded "GO for read-only aggregation, conditional/NO-GO for live execution," this doc designs both paths so the write path can be built behind gates and left dark until its pre-conditions clear.

> **API contract now grounded.** The endpoint/field references below were originally speculative ("vendor confirmation needed"). As of 2026-07-03 they are replaced with real values from `SNAPTRADE_API_FINDINGS.md`. Two findings materially change this design: (1) `POST /trade/place` **natively accepts `client_order_id`** (a UUID idempotency key) — the live blocker Q1 is resolved YES; (2) SnapTrade's sandbox is **read-only** (no order placement) — write-path testing needs a real **Alpaca Paper** connection. Webhook verification reuses `consumerKey` (no separate webhook secret).

**Scope of this doc:** module boundaries, API contract, data flow, config, security, error handling, persistence, and test strategy for adding SnapTrade as (1) a read-only aggregation source and (2) a `BaseExecutor` subclass, without disturbing the existing CCXT crypto path.

**Non-goals:** options multi-leg support (deferred — the single-`inst_id` normalized shape does not model contract multipliers/legs); multi-tenant user management at scale (§4c of research doc); replacing CCXT for crypto.

---

## 1. Architecture Overview

### 1.1 Where SnapTrade lives

SnapTrade is an **additive backend**, parallel to CCXT, plugged in at the two existing seams HermX already exposes:

1. **The executor seam** — `ExecutorFactory` selects a `BaseExecutor` subclass by config key. SnapTrade becomes a new registered key alongside `ccxt`.
2. **The read seam** — the dashboard and reconciliation consume the observe-only query contract (`get_positions`/`get_balance`/`get_order`/...). SnapTrade implements the same contract, so holdings render through the same normalized shapes with no dashboard rewrite.

Everything above the adapter boundary — the `ExecutionService` gate ladder, the write-ahead order journal, idempotency, reconciliation, the kill switch, per-strategy pause/demo/live — is venue-neutral and **reused unchanged**. SnapTrade does not get its own execution pipeline; it gets its own *adapter* under the existing pipeline.

### 1.2 Diagram in words

**Existing crypto path (unchanged):**

```
TradingView → webhook_receiver.py → (normalize, dedupe, WAL) →
  execution/service.py [gate ladder + journal] →
    ExecutorFactory.create() → CcxtExecutor →
      CCXT → OKX/Binance/Bybit/... →
        reconcile (observe-only) → JSONL ledgers → dashboard
```

**New SnapTrade write path (behind gates, dark until pre-conditions clear):**

```
TradingView (e.g. SPY signal) → webhook_receiver.py → (normalize, dedupe, WAL) →
  execution/service.py [SAME gate ladder + journal] →
    ExecutorFactory.create() → SnapTradeExecutor →
      src/snaptrade/ (client + auth + connection store) →
        SnapTrade API → user's IBKR / Alpaca / Schwab / ... →
          reconcile (observe-only + webhook-fed) → JSONL ledgers → dashboard
```

**New SnapTrade read/aggregation path (ship first, zero write risk):**

```
dashboard.py /api → SnapTrade aggregation reader (uses SnapTradeExecutor's
  observe-only methods in read-only construction) →
    src/snaptrade/ → SnapTrade holdings/balances/orders →
      normalizer.py → canonical position/balance/order shapes →
        "External Brokers" dashboard panel
```

**New SnapTrade event path (optional, feeds reconciliation):**

```
SnapTrade → HTTPS webhook (order filled / connection broken / holdings updated) →
  webhook_receiver.py new endpoint /snaptrade/webhook [separate auth: SnapTrade
    signature, NOT the TradingView HMAC] →
      route by event type:
        order-fill  → nudge reconciliation for the mapped cl_ord_id
        connection-broken → set connection-health = unhealthy (fail-closed gate input)
        holdings-updated → invalidate aggregation cache
```

### 1.3 The one structural change the seams require

The adapter selector is **currently global.** `ExecutorFactory.create()` reads `config['execution']['exchange']` and falls back to `EXEC_BACKEND` (default `ccxt`); `resolve_execution_config()` (`src/execution/service.py:15`) resolves the *venue* per-strategy from `readiness['instrument']['exchange']` into `ccxt_exchange`, but it never changes the *adapter* selector. So today every strategy uses the same adapter class.

To route `SPY` to SnapTrade while `BTC-USDT-SWAP` stays on CCXT, the **adapter selector must become per-strategy-derivable.** This is the single non-additive change in the design and is detailed in §2.7. It is small (a routing rule in `resolve_execution_config`) but it touches shared execution logic, so per project rule it needs explicit confirmation before implementation.

---

## 2. New / Modified Modules

### 2.1 `src/executors/snaptrade_adapter.py` — `SnapTradeExecutor(BaseExecutor)` (NEW)

Implements the full `BaseExecutor` contract from `src/executors/base.py`.

- `key = "snaptrade"` — the factory/config registration key.
- `__init__(self, config, root)` — builds a `SnapTradeClient` (§2.2) from env-resolved credentials and the connection store; resolves the target account from the strategy instrument block. Never holds brokerage credentials — only the SnapTrade `clientId`/`consumerKey` and the per-user `userId`/`userSecret` handle.
- `execute(self, readiness) -> dict` — the write path. Translates the venue-neutral `execution_readiness` into a **one-step `POST /trade/place`** call (the recommended path; the two-step `/trade/impact` → `/trade/{tradeId}` flow is reserved for `plan()`), attaching `client_order_id` (§8.2), submits, and returns the normalized envelope via `self.normalized_result(...)`. **Async-fill aware:** a SnapTrade submit response with status `PENDING/ACCEPTED/QUEUED` maps to `mode="submit_enabled"` (→ journal SUBMITTED, not FILLED), exactly as the service already treats a CCXT ACK (`service.py:220-228`). A synchronous `EXECUTED`/`PARTIAL` maps to `mode="filled"`; usually the fill arrives via reconcile/webhook.
- `health(self) -> dict` — connectivity + connection-status snapshot for the active account (feeds the auth-health gate and the dashboard connection panel). Returns `{ok, exchange, connection_status, broker, last_sync, error}`.
- `plan(self, readiness) -> dict` — preview the order intent without submitting (used by dry-run and the dashboard preview).
- **Observe-only reads** (all delegate to `normalizer.py`, all read-only, none arm submission):
  - `get_order(inst_id, ord_id=None, cl_ord_id=None)` → normalized order (or `state="not_found"`).
  - `get_open_orders(inst_id=None)` → list of normalized orders.
  - `get_positions(inst_id=None)` → list of normalized positions.
  - `get_balance(ccy=None)` → list of normalized balances.
  - `get_order_history_archive(inst_id=None, limit=100)` → normalized historical orders.
  - `get_order_history_raw(inst_ids=None, limit=100)` → raw SnapTrade activity rows (dashboard close-row enrichment; SnapTrade's richer activity feed — dividends, transfers, fees — flows through here).

**Construction discipline:** the adapter must be constructable in a **read-only mode** (aggregation path, §2.4) where `execute()` is never called and no write credentials/consent are asserted. The observe-only methods must not depend on any state that only `execute()` establishes.

### 2.2 `src/snaptrade/` package (NEW)

A self-contained package so all SnapTrade SDK/HTTP specifics live behind one boundary — the adapter depends on this package, never on the SnapTrade SDK directly. Mirrors how `ccxt_adapter.py` wraps CCXT.

```
src/snaptrade/
  __init__.py
  client.py          # SnapTradeClient — thin wrapper over the SnapTrade SDK/HTTP
  auth.py            # SnapTradeAuthManager — clientId/consumerKey + per-user secret lifecycle
  connections.py     # ConnectionStore — persist/rotate userId/userSecret + connection health
  normalizer.py      # SnapTrade responses → canonical HermX shapes (§2.3)
  errors.py          # SnapTrade exceptions → HermX error taxonomy (§7)
```

**`client.py` — `SnapTradeClient`**
- Wraps the official SnapTrade Python SDK (preferred) or a minimal HTTP client if the SDK is unsuitable. **Vendor confirmation needed** on SDK maturity/version pinning (§10).
- Owns timeouts, retry policy hooks (§7), and rate-limit backoff. All network I/O funnels here.
- Methods are verb-per-endpoint HermX façade names over the SDK: `list_accounts` (`account_information.list_user_accounts`), `get_positions` (`.get_user_account_positions`), `get_balances` (`.get_user_account_balance`), `place_order` (`trading.place_force_order` → `POST /trade/place`), `get_order_status` (`.get_user_account_orders`), `plan_order` (`trading.get_order_impact`), `cancel_order` (`POST /accounts/{id}/orders/cancel`), `list_activities` (`transactions_and_reporting.get_activities`), `get_user_connections` (`connections.list_brokerage_authorizations`) — all returning **raw** SnapTrade payloads. Normalization is `normalizer.py`'s job, not the client's — keeps the client a pure transport layer.
- Redacts secrets from any exception/log surface using the existing `redact_secrets` (`src/security/credentials.py:196`) plus SnapTrade-specific keys added to its redaction set.

**`auth.py` — `SnapTradeAuthManager`**
- Resolves the **partner credential** (`clientId` + `consumerKey`) from env via the credentials module (§6), never from strategy files.
- Owns the **user registration** flow (register a SnapTrade user → receive `userSecret`) and **connection-portal URL generation** (the redirect the end-user follows to link their brokerage directly — HermX never sees broker creds).
- Owns **`userSecret` rotation**.

**`connections.py` — `ConnectionStore`**
- Persists per-user `{userId, userSecret(encrypted), broker, accountId, connection_status, linked_at, last_sync}` under `HERMX_DATA_DIR` (§8).
- Exposes `connection_health(account) -> {ok, status, reason}` — the input to the auth-health gate for SnapTrade venues.
- Updates health on `connection-broken`/expiry webhooks (§2.6) so a broken connection **fails closed**.

### 2.3 `src/snaptrade/normalizer.py` (NEW)

Pure functions, no I/O. SnapTrade payload in → canonical HermX shape out. This is the field-mapping core (research doc §5). Isolating it as pure functions makes it the highest-value unit-test target (§9).

Real SnapTrade field names below are from `SNAPTRADE_API_FINDINGS.md` §2–3.

- `to_normalized_order(raw) -> dict` — SnapTrade order → `empty_normalized_order()` shape. Field map: `brokerage_order_id → ord_id`, `client_order_id → cl_ord_id`, `universal_symbol.symbol → inst_id` (or `option_symbol` for options), `action → side`, `filled_quantity → acc_fill_sz` (**string→float parse**), `execution_price → avg_px` (string→float), `order_type → ord_type`, `time_placed → ts`. **Status map (authoritative enum):** `EXECUTED → filled`; `PARTIAL/PARTIAL_CANCELED → partial`; `PENDING/ACCEPTED/QUEUED/TRIGGERED/ACTIVATED/REPLACE_PENDING/CANCEL_PENDING → pending`; `CANCELED/REPLACED/EXPIRED → canceled`; `REJECTED/FAILED → rejected`; `NONE`/unrecognized → `unknown`/`not_found`. `pos_side = None` for cash equities. **Note quantities/prices arrive as decimal strings**, not floats — parse in the normalizer.
- `to_normalized_position(raw) -> dict` — SnapTrade position → `{exchange, inst_id, pos, pos_side, avg_px, upl, raw}`. Map: `symbol.symbol.symbol → inst_id`, `units → pos` (signed; long-only cash → positive), `average_purchase_price → avg_px`, `open_pnl → upl`, `price` (last) available for mark. `pos_side` derived from sign (long-only cash → `None`).
- `to_normalized_balance(raw) -> dict` — SnapTrade balance → `{exchange, ccy, eq, avail, raw}`. Map: `currency.code → ccy`, **`cash → avail`** (the **conservative** field: `buying_power` includes margin and may be `null`, so use `cash`). Note: the `/balances` object has **no single "total equity"** field — derive `eq` from the account-level `total_value` (from `GET /accounts`) or leave it to the account object; a balance row alone yields `cash`/`buying_power` only.
- `to_fill_summary(raw, client_order_id) -> dict` — SnapTrade order response → `empty_fill_summary()` shape. `order_id ← brokerage_order_id`, `client_order_id ← client_order_id`, `avg_fill_price ← execution_price`, `filled_size ← filled_quantity`. `slippage_pct` computed from expected vs actual when both known, else `None`. `fee_usd` may be `None` (per-fill fee is broker-dependent; `/trade/impact` exposes *estimated* commission/forex fees but not always a realized per-fill fee — `None` is contract-legal).
- `to_order_intent_request(readiness) -> dict` — the **reverse** direction: venue-neutral `order_intent` → `POST /trade/place` body. Map: `account_id`, `action` (`BUY`/`SELL`/`BUY_TO_OPEN`/…), `symbol` **or** `universal_symbol_id` (raw `symbol` avoids a lookup call — see below), `order_type` (`Market`/`Limit`/`Stop`/`StopLimit`), `time_in_force` (`Day`/`GTC`/`FOK`/`IOC`/`GTD`), `units` **or** `notional_value`, `price` (Limit/StopLimit), `stop` (Stop/StopLimit), and **`client_order_id`** = a canonical UUID derived from HermX `cl_ord_id` (UUIDv5 if `cl_ord_id` is not already a UUID — §8.2). Symbol resolution: pass the raw `symbol` string to skip `ReferenceData_getSymbols`, at the cost of broker-side resolution; resolve to `universal_symbol_id` upfront only if a broker rejects raw tickers.

Every normalized shape carries `raw` (untouched SnapTrade payload) per the existing forensics contract. `exchange` field = the SnapTrade account/institution slug (e.g. `snaptrade:alpaca:<acct>`), **not** a CEX id — this keeps ledger rows self-describing and lets the dashboard group by broker.

### 2.4 Aggregation reader (read-only path) (NEW, small)

Rather than a second code path, the read-only aggregation (research §4a) is just the `SnapTradeExecutor` **constructed in read-only mode** and its observe-only methods called. A thin `src/snaptrade/aggregation.py` orchestrates "for each linked account, get_positions + get_balance + recent get_order_history_archive, tag with broker, return." The dashboard consumes the result through the same normalized shapes it already renders for CCXT.

This is why the read path can ship first with **zero write risk**: `execute()` is never invoked, no consent-to-trade is asserted, and the gate ladder is not involved.

### 2.5 Dashboard additions — `src/dashboard.py` + `dashboard-ui/` (MODIFIED)

- **`/api` extension:** the system-state payload gains an `external_brokers` section — a list of `{broker, account, connection_status, positions[], balances[]}` from the aggregation reader (§2.4). Additive; existing crypto fields untouched.
- **Connection-status panel (UI):** shows each linked broker, its `connection_status` (linked / expired / broken / re-link required), `last_sync`, and a "Connect / Re-link" action that opens the SnapTrade connection portal URL (§2.2). A broken connection renders as a clear fail-closed state, mirroring how auth-health issues surface today.
- **Multi-broker holdings view (UI):** unified positions/balances across crypto (CCXT) + external brokers (SnapTrade), driven entirely by the shared normalized shapes. Group-by-broker; aggregate net exposure row.
- **Per-strategy controls:** the existing pause/demo/live controls (`POST`/`DELETE /api/control/strategy/{id}`, `control-state.json`) apply to SnapTrade strategies unchanged — a SnapTrade strategy is paused/armed exactly like a CCXT one. `demo` for SnapTrade maps to a paper account (e.g. Alpaca paper); `live` requires `HERMX_LIVE_TRADING` just like CCXT.

### 2.6 Webhook endpoint — `src/webhook_receiver.py` (MODIFIED, additive)

SnapTrade → HermX push events enter through a **new, separate endpoint** `POST /snaptrade/webhook`, distinct from the TradingView alert intake.

- **Auth is separate.** TradingView intake uses the optional HMAC (`webhook_auth_config_healthy`). The SnapTrade endpoint uses **SnapTrade's own webhook verification**: the inbound `Signature` header is an **HMAC-SHA256 of the raw request body, keyed with `consumerKey`, base64-encoded** (grounded — findings §4). **Correction to the earlier design:** there is **no separate SnapTrade webhook secret** — verification reuses `SNAPTRADE_CONSUMER_KEY`; the payload's legacy `webhookSecret` field is deprecated and must not be relied on. The two auth planes are still never conflated (different key, different endpoint, different signed input: TradingView HMAC vs SnapTrade `consumerKey`-HMAC), but the SnapTrade plane is **not an independent secret** — it is the consumer key. Separate handler, separate verification function; the "separate secret env var" is dropped.
- **Handler is thin and side-effect-narrow.** It verifies the signature, appends the raw event to a durable log (`logs/snaptrade-events.jsonl`, same append-only JSONL discipline as `raw-webhooks.jsonl`), then dispatches by `eventType` (real event names, findings §4):
  - **`TRADE_DETECTION` / `TRADE_UPDATE`** (order fill / status change) → look up the SnapTrade `brokerage_order_id` in the order-id map (§8.2), resolve the HermX `cl_ord_id`, and nudge reconciliation (or write the terminal state if the fill is authoritative). This is the "webhook could reduce poll load" win from research §8.
  - **`CONNECTION_BROKEN` / `CONNECTION_FAILED`** → `ConnectionStore.mark_unhealthy(account)`; the next execute for that account fails closed at the auth-health gate. **`CONNECTION_FIXED`** → mark healthy again.
  - **`ACCOUNT_HOLDINGS_UPDATED`** → invalidate the aggregation cache so the dashboard reflects fresh holdings.
  - (Other events — `USER_REGISTERED`, `NEW_ACCOUNT_AVAILABLE`, `ACCOUNT_TRANSACTIONS_UPDATED`, `ACCOUNT_REMOVED`, `CONNECTION_ADDED/DELETED` — logged; wire as needed.)
- **Delivery guarantees (grounded, findings §4).** **At-least-once, no ordering guarantee.** SnapTrade retries a non-2xx handler (not `200/201/202/204`) **up to 3 times starting at ~30-min intervals with exponential backoff** — i.e. a transient handler failure is re-delivered over ~an hour, not seconds. The handler must be **idempotent** — replaying a `TRADE_UPDATE` must not double-transition the journal. The journal's `can_transition` guard (`service.py:317`) already enforces legal transitions; the handler relies on it rather than assuming exactly-once. Because retry cadence is slow (~30 min), the poll backstop — not webhook retry — is what bounds time-to-terminal.
- **Webhooks are an optimization, not the source of truth.** Poll-based reconciliation remains the backstop; a missed webhook degrades to the existing poll path, never to a lost order.

### 2.7 `resolve_execution_config()` routing change — `src/execution/service.py` (MODIFIED — shared code, needs confirmation)

Today (`service.py:15-54`) this resolves the *venue* (`ccxt_exchange`) from the instrument but leaves the *adapter selector* (`execution.exchange`) global. The change: derive the adapter selector from the instrument too.

Design options (pick one at implementation time):

- **(A) Registry-driven venue→adapter map (recommended).** A small mapping: crypto CEX slugs (`okx`, `binance`, ...) → `ccxt`; SnapTrade broker/account slugs → `snaptrade`. `resolve_execution_config` sets `execution["exchange"]` from this map based on `instrument.exchange`. Backward-compatible: every existing crypto slug maps to `ccxt`, so current configs are byte-identical.
- **(B) Explicit `instrument.backend` field.** Add an optional strategy field naming the adapter directly. More explicit, but pushes an implementation detail into strategy files. Prefer (A) unless the venue→adapter mapping proves ambiguous.

`HERMX_EXEC_BACKEND` continues to override globally (test/forcing escape hatch), applied identically to submit and reconcile so the two can never diverge (the existing invariant at `service.py:35-37`).

### 2.8 Factory registration — `src/executors/factory.py` (MODIFIED, one line)

Per the factory's own design ("adding a new exchange is a one-line registration"):

```
try:
    from .snaptrade_adapter import SnapTradeExecutor
except Exception:
    SnapTradeExecutor = None
...
if SnapTradeExecutor is not None:
    ExecutorFactory.register(SnapTradeExecutor.key, SnapTradeExecutor)
```

The `try/except` mirrors the CCXT guard: if the SnapTrade SDK isn't installed, the key is simply absent and any SnapTrade-routed strategy **fails closed** at `create()` ("Unknown execution exchange") rather than guessing a venue.

---

## 3. API Contract — SnapTrade endpoints ↔ HermX methods

SnapTrade endpoint paths below are **grounded** against the live API reference (`SNAPTRADE_API_FINDINGS.md`, base `https://api.snaptrade.com`). SDK method names are the `snaptrade-python-sdk==11.0.212` surface.

| HermX method (`BaseExecutor`) | SnapTrade endpoint (SDK method) | Direction | Normalizer function | Notes |
|---|---|---|---|---|
| `execute()` (submit) | `POST /trade/place` (`trading.place_force_order`) | write | `to_order_intent_request` → submit → `to_fill_summary` | **One-step, recommended.** Body carries `client_order_id` (UUID idempotency key). Status `PENDING/ACCEPTED/QUEUED` → SUBMITTED; `EXECUTED` → FILLED. |
| `plan()` | `POST /trade/impact` (`trading.get_order_impact`) | read | `to_order_intent_request` | No order placed. Returns `trade.id` + estimated commission/forex fees + post-trade `remaining_cash`. (`trade.id`/tradeId expires 5 min — do not use for the live submit path.) |
| `get_order()` | `GET /accounts/{accountId}/orders` filtered by id (`account_information.get_user_account_orders`) | read | `to_normalized_order` | Not found → `state="not_found"`. Order status resolved by `brokerage_order_id`. |
| `get_open_orders()` | `GET /accounts/{accountId}/orders` (open filter) | read | `to_normalized_order` (list) | |
| `get_positions()` | `GET /accounts/{accountId}/positions` (`account_information.get_user_account_positions`) | read | `to_normalized_position` | Core of aggregation path. `open_pnl`, `average_purchase_price`, `units`. |
| `get_balance()` | `GET /accounts/{accountId}/balances` (`account_information.get_user_account_balance`) | read | `to_normalized_balance` | `cash` = conservative `avail`. |
| `get_order_history_archive()` | `GET /accounts/{accountId}/orders` (history) | read | `to_normalized_order` (list) | Normalized rows. |
| `get_order_history_raw()` | account activities/transactions endpoint (`transactions_and_reporting.get_activities`) | read | passthrough (raw rows) | Richer feed: dividends, transfers, fees, corporate actions. |
| (cancel — future) | `POST /accounts/{accountId}/orders/cancel` (body `brokerage_order_id`) | write | — | For close/cancel path; deprecated → newer `Trading_cancelOrder`. |
| `health()` | `GET /accounts` + connection list (`connections.list_brokerage_authorizations`) | read | — | Connection status → auth-health gate + dashboard. |
| `SnapTradeAuthManager.register_user()` | `POST /snapTrade/registerUser` (`authentication.register_snap_trade_user`) | write (setup) | — | Returns `{userId, userSecret}` (store `userSecret` encrypted). |
| `SnapTradeAuthManager.login_portal_url()` | `POST /snapTrade/login` (`authentication.login_snap_trade_user`) | read (setup) | — | Returns `{redirectURI, sessionId}`. `reconnect` param drives re-auth. |
| `SnapTradeAuthManager.rotate_secret()` | `POST` reset-user-secret (`authentication.reset_snap_trade_user_secret`) | write (setup) | — | Rotate a compromised `userSecret`. |
| `ConnectionStore.mark_unhealthy()` | (driven by `CONNECTION_BROKEN`/`CONNECTION_FAILED` webhook) | event | — | → fail closed. `CONNECTION_FIXED` → healthy. |
| `/snaptrade/webhook` handler | receive SnapTrade webhooks (`Signature` = HMAC-SHA256/`consumerKey`) | event | — | Verified with `consumerKey` (no separate secret); idempotent dispatch. |

**Contract invariants:**
- Every read method returns a **canonical normalized shape** (or list thereof); reconciliation and the dashboard never see raw SnapTrade payloads except via the `raw` field.
- A not-found order is `{state: "not_found", raw: <error>}`, never an exception (matches `base.py:50` contract).
- `execute()` returns the standard envelope `{ok, mode, exchange, elapsed_ms, fill_summary, payload}` via `self.normalized_result(...)`.

---

## 4. Data Flow Diagrams

### 4.1 Read path — holdings sync (aggregation)

```
dashboard.py /api request
  → aggregation.py: for each linked account in ConnectionStore:
      → SnapTradeExecutor(read-only).get_positions()  ─┐
      → SnapTradeExecutor(read-only).get_balance()     ─┼─→ client.py → SnapTrade API
      → SnapTradeExecutor(read-only).get_order_history_archive() ─┘
      → normalizer.py maps each raw payload → canonical shape (tagged with broker slug)
  → merge with CCXT positions/balances (same shapes)
  → /api payload: { ...existing..., external_brokers: [ {broker, account, positions[], balances[]} ] }
  → dashboard-ui renders unified multi-broker view
```

No gates, no journal, no writes. Failure is cosmetic (a broker shows "unavailable"); it never affects crypto rendering or execution.

### 4.2 Write path — order submit

```
TradingView alert (e.g. SPY buy)
  → webhook_receiver.py: normalize → dedupe → fsync raw-webhooks.jsonl (WAL) → enqueue
  → dequeue → write signals.jsonl (dedupe ledger)
  → enrich → build execution_readiness (instrument.exchange = snaptrade:<broker>:<acct>)
  → resolve_execution_config(): instrument → adapter selector "snaptrade" (§2.7) + account
  → ExecutionService.execute(record):
        Gate 1  strategy_submit_flag (readiness.live_execution_enabled) + auth_health
                (SnapTrade auth_health = ConnectionStore.connection_health) + watchdog
        Gate 2  execution_mode canonical (demo|live)
        Gate 3  live_trading_kill_switch  (live/non-sandbox submit needs HERMX_LIVE_TRADING;
                                            close_only bypasses — reduces exposure)
        Gate 4  sandbox_only / live_sandbox_consistency
        Gate 5  symbol_pause (bypassed for close_only)
        Gate 6  idempotency: latest_order_record(cl_ord_id) exists? → block duplicate
     → journal PLANNED  (fail-closed on OSError)
     → journal SUBMITTED (fail-closed on OSError)
     → ExecutorFactory.create() → SnapTradeExecutor.execute(readiness):
          → normalizer.to_order_intent_request(readiness)  [attach idempotency key §8.2]
          → client.place_order(...)  [POST /trade/place + client_order_id]  → SnapTrade API → user's broker
          → ACK → normalized_result(ok=True, mode="submit_enabled", fill_summary=...)
     → service maps mode: ACK → stays SUBMITTED (not FILLED)
     → post-submit reconciliation with backoff:
          → SnapTradeExecutor.get_order(cl_ord_id/ord_id) → normalized state
          → can_transition(SUBMITTED, state)? → journal FILLED | REJECTED | UNKNOWN
          → mismatch → emit_reconcile_alert
     → append execution ledger
```

Note the **store the order-id map** step (§8.2) happens at submit so a later webhook/poll can resolve SnapTrade order id ↔ HermX `cl_ord_id`.

### 4.3 Event path — SnapTrade webhook

```
SnapTrade → POST /snaptrade/webhook (SnapTrade signature)
  → verify SnapTrade signature (NOT TradingView HMAC) → 401 on failure
  → append logs/snaptrade-events.jsonl (durable, append-only)
  → dispatch by event type (idempotent):
      order-fill / status-change:
        → order-id map: SnapTrade ord_id → cl_ord_id
        → reconcile that order (or apply authoritative terminal state via can_transition guard)
      connection-broken / expired:
        → ConnectionStore.mark_unhealthy(account)  → next execute fails closed at auth_health
        → dashboard shows "re-link required"
      holdings-updated:
        → invalidate aggregation cache
  → 200 (ack quickly; heavy work is journal-nudge, not inline broker calls)
```

Missed/duplicate/out-of-order webhooks degrade safely: poll reconciliation is the backstop, and `can_transition` prevents double-transition.

---

## 5. Configuration Design

### 5.1 Environment variables (NEW)

Partner + webhook secrets resolved from env, same discipline as CCXT keys (never in strategy files):

| Env var | Purpose |
|---|---|
| `SNAPTRADE_CLIENT_ID` | Partner client id (SnapTrade account). |
| `SNAPTRADE_CONSUMER_KEY` | Partner consumer/secret key. Signs every API request **and verifies inbound webhooks** (HMAC-SHA256; findings §4). Redacted from logs. |
| ~~`SNAPTRADE_WEBHOOK_SECRET`~~ | **DROPPED.** Webhook verification reuses `SNAPTRADE_CONSUMER_KEY` — there is no separate webhook secret (findings §4). |
| `SNAPTRADE_ENCRYPTION_KEY` | Key for encrypting `userSecret` at rest (§6, §8). Sourced from secrets manager/system env, never committed. |
| `SNAPTRADE_ENV` | `sandbox` \| `production`. Selected by **key type**, not a base-URL switch: the sandbox is enabled by default on non-production (personal / commercial-test) keys; production keys hit real brokerages. The sandbox is **read-only** (findings §8) — it validates the read/aggregation path but cannot place orders. |
| `HERMX_EXEC_BACKEND` | Existing global adapter override; can force `snaptrade` for testing. |

These are added to `resolve_exchange_credentials()` (`src/security/credentials.py`) as a new `snaptrade` branch and to the `redact_secrets` key set. The credential resolver's `mode` (`demo`/`live`) selects paper vs live where SnapTrade distinguishes (e.g. Alpaca paper). Per the "fail closed on partial set" precedent (Hyperliquid, `credentials.py:159-175`), return credentials only when the required pair is present.

### 5.2 `engine-config.json` fields (NEW, additive)

Baked from `config/runtime.*.demo.json` into `/app/engine-config.json`. New optional section, ignored by the CCXT path:

```json
{
  "snaptrade": {
    "enabled": false,
    "environment": "sandbox",
    "webhook_enabled": false,
    "timeout_seconds": 20,
    "reconcile_poll_seconds": 5,
    "reconcile_max_attempts": 6,
    "venue_adapter_map": { "snaptrade": "snaptrade" }
  }
}
```

`enabled: false` by default → SnapTrade fully dark. `webhook_enabled` gates registration of `/snaptrade/webhook`. `venue_adapter_map` backs the §2.7 routing option (A).

### 5.3 Strategy file extensions — `schemas/strategy.schema.json` (MODIFIED)

Strategy schema v2 already has an exchange-agnostic `instrument: {exchange, inst_id, type}`. SnapTrade strategies reuse it:

```json
{
  "schema_version": 2,
  "strategy_id": "spy_swing",
  "name": "SPY swing",
  "instrument": { "exchange": "snaptrade:alpaca:paper", "inst_id": "SPY", "type": "equity" },
  "timeframe": "4h",
  "indicator": "kinetic_flow",
  "capital": { "budget_usd": 2000, "reinvest": false },
  "execution_mode": "demo",
  "submit_orders": true
}
```

Schema changes required:
- **`instrument.type`** enum gains `equity` (and later `etf`; `option` already present but deferred at runtime).
- **`instrument.exchange`** pattern already permits `[a-z0-9_]+`; extend to allow the `:` separators for `snaptrade:<broker>:<account>` **or** keep the slug flat and carry broker/account in the map — decide at implementation (a flat slug avoids a pattern change).
- **Conditional validation:** `leverage` and `margin_mode` are perp-native. For `type: equity` (cash accounts) the schema must **reject** them (a `if instrument.type == equity then not leverage/margin_mode` clause), per research §5 mapping risks. Today v2 marks `leverage`/`margin_mode` as *required* — this becomes conditional on instrument type. **This is a shared-schema change requiring confirmation.**
- **No inline credentials** clause (`no_inline_credentials`) applies unchanged — SnapTrade `userSecret`/keys are never in strategy files.

`execution_mode: demo` for a SnapTrade strategy routes to the broker's paper account (Alpaca paper); `live` requires `HERMX_LIVE_TRADING` identically to CCXT.

---

## 6. Security Design

### 6.1 Three distinct auth planes (keep separate)

1. **TradingView → HermX (inbound, unchanged):** optional HMAC on the alert intake. Governs *who may submit signals*.
2. **HermX → SnapTrade (partner, new):** `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY`, env-resolved. Governs *HermX's identity to SnapTrade*.
3. **SnapTrade → HermX (webhook, new):** SnapTrade's `Signature` header = **HMAC-SHA256 of the raw body keyed with `SNAPTRADE_CONSUMER_KEY`** (base64). Governs *authenticity of push events*. **Note (correction):** this plane is **not a separate secret** — it reuses the plane-2 partner `consumerKey`. Planes 2 and 3 therefore share a key; only planes 1 (TradingView HMAC) and 2/3 (SnapTrade consumerKey) are independent secrets.

These are **never conflated at the endpoint level.** Distinct verification functions, distinct endpoints, distinct signed inputs. The `/snaptrade/webhook` handler verifies with the `consumerKey`-HMAC and explicitly does **not** accept the TradingView HMAC (and the TradingView intake does not accept SnapTrade's) — a cross-plane signature must fail. The one caveat vs the original design: because plane 3 reuses the consumer key, a leaked `consumerKey` compromises both HermX→SnapTrade calls *and* webhook authenticity — treat `consumerKey` as the highest-value SnapTrade secret.

### 6.2 Brokerage credentials — HermX never holds them

The core security upside (research §6): the end-user authenticates **directly with their brokerage** through the SnapTrade connection portal. HermX's blast radius **excludes broker passwords** — it holds only the SnapTrade `userSecret` handle. This is the Plaid trust model and is strictly better than HermX storing broker credentials.

### 6.3 `userSecret` storage — encrypted at rest

- `userSecret` is a **credential** and treated as one: encrypted at rest under `SNAPTRADE_ENCRYPTION_KEY`, stored in the connection store under `HERMX_DATA_DIR` (§8), never in git, never in strategy files, never in ledgers.
- Redaction: add `SNAPTRADE_CONSUMER_KEY`, `SNAPTRADE_ENCRYPTION_KEY`, and any `userSecret` value to the `redact_secrets` set (`credentials.py:200-234`) so they cannot leak through error strings, `submit_exception` payloads, or logs. (No `SNAPTRADE_WEBHOOK_SECRET` — dropped; webhook auth reuses `consumerKey`.)
- The partner `consumerKey` and per-user `userSecret` are separated: leaking the connection store (userSecrets) without the partner key, or vice-versa, is insufficient to act — defense in depth.

### 6.4 OAuth / connection token lifecycle

- **Register:** `SnapTradeAuthManager.register_user()` → persist `{userId, userSecret(encrypted)}`.
- **Link:** generate connection-portal URL → user links broker → SnapTrade holds the broker OAuth; HermX holds only the handle.
- **Health:** connection status is polled via `health()` and pushed via webhooks. A broken/expired connection sets health unhealthy → **auth-health gate fails closed** for that account (no silent no-op).
- **Rotate:** support `userSecret` rotation (re-encrypt in place); support broker re-link without losing the HermX-side account mapping.
- **Revoke:** deleting a connection removes the store entry and disables the strategy routed to it.

### 6.5 Consent & data privacy

- Read-only aggregation still transmits users' holdings through SnapTrade → HermX ledgers. Document retention; consider whether normalized holdings should persist in JSONL or be memory-only for read-only users (research §9 privacy risk).
- Trade execution requires **explicit per-user consent to place orders** — captured in the connection flow and logged (consent event in a ledger).

---

## 7. Error Handling & Resilience

### 7.1 Error taxonomy — `src/snaptrade/errors.py`

Map SnapTrade failures onto the modes the `ExecutionService` already understands (`service.py:219-238`), so no new service branches are needed:

| SnapTrade failure | HermX mode | Journal outcome |
|---|---|---|
| Network timeout on submit | `submit_timeout` | UNKNOWN (reconcile — order may have reached broker) |
| Exception mid-submit | `submit_exception` | UNKNOWN |
| Multi-step submit, one leg failed | `submit_partial` | UNKNOWN + reconcile alert |
| Explicit broker reject (validation, insufficient funds) | `ok=False`, reject | REJECTED |
| ACK accepted | `submit_enabled` | SUBMITTED (reconcile → terminal) |
| Confirmed fill | `filled` | FILLED |

The critical rule (already enforced by the service): **any ambiguity is UNKNOWN, never REJECTED.** A timeout after the order may have reached the broker must not be recorded as rejected — that would corrupt position math. UNKNOWN forces reconciliation.

### 7.2 Timeout mapping

- Submit timeout: `snaptrade.timeout_seconds` (default 20s), well under the service's `submit_timeout_seconds` (default 45s) so the adapter times out first and returns `submit_timeout` cleanly rather than the service killing it.
- Read timeouts (aggregation/reconcile): shorter; a slow read degrades to "unavailable" in the dashboard, never blocks.

### 7.3 Retry strategy

- **Reads:** safe to retry (idempotent). Bounded exponential backoff in `client.py`.
- **Writes:** **never blind-retry a submit** — a retry could double-submit real money. Instead, on a timeout/ambiguous submit, do **not** re-place; record UNKNOWN and let reconciliation determine whether the order landed. Re-placement, if ever, only after reconciliation confirms the prior attempt did *not* reach the broker. **Now backed by `client_order_id`** (findings §3, resolved §10 Q1): a re-place carrying the *same* deterministic `client_order_id` is deduped at the brokerage — but this is a safety net, not a license to auto-retry; the UNKNOWN-then-reconcile discipline stands.
- **Rate limits (429):** SnapTrade sends **no `Retry-After`** — compute the wait from **`X-RateLimit-Reset`** (seconds), then exponential backoff + jitter (findings §5). Limits: **250/min global; +10/min/account** on data endpoints for Personal keys. Treat 429 as retryable for reads, and as "defer" (not double-submit) for writes.

### 7.4 SnapTrade session / connection expiry

- Broker connections expire (broker-dependent). Handled as an **auth-health gate input** (§6.4): unhealthy → fail closed. Never silently no-op a submit against an expired connection.
- Expiry surfaces in the dashboard as "re-link required" with the portal action.

### 7.5 Fallback behavior & blast-radius isolation

- **SnapTrade outage must fail closed for SnapTrade venues only.** The crypto/CCXT path has zero SnapTrade dependency and must be entirely unaffected (research §9 dependency risk). The factory `try/except` and the per-strategy adapter routing (§2.7/§2.8) guarantee this: a missing/broken SnapTrade adapter blocks only strategies routed to it.
- **Aggregation failure is cosmetic** — a broker shows "unavailable"; nothing else degrades.
- **Webhook loss** degrades to poll reconciliation.

---

## 8. State & Persistence

### 8.1 Connection store

- **Location:** `HERMX_DATA_DIR` (the same writable mount the dashboard uses for `control-state.json`, `webhook_receiver.py:132`; `dashboard.py:56`). File: `snaptrade-connections.json` (or a small store), **`userSecret` field encrypted** under `SNAPTRADE_ENCRYPTION_KEY`.
- **Why here, not `logs/`:** `logs/` is append-only forensic JSONL; connections are mutable state (health flips, rotation) — belongs with `control-state.json` in `HERMX_DATA_DIR`.
- **Docker note:** per the known-pattern rules, the dashboard/receiver need `HERMX_DATA_DIR` mounted read-write (compose `hermx-state:/app/data`) even under `read_only: true`, or connection writes silently fail. The connection store inherits this requirement — document it in the installer.

### 8.2 Order-id mapping — SnapTrade order id ↔ HermX `cl_ord_id`

This is the linchpin of async reconciliation and webhook dispatch.

- HermX generates `cl_ord_id` (existing idempotency key, checked at `service.py:175`). At submit, `SnapTradeExecutor.execute()` records the mapping `{cl_ord_id ↔ brokerage_order_id, client_order_id(uuid), account, inst_id, submitted_at}`. Natural home: the **order journal** (already keyed by `cl_ord_id`) — store `brokerage_order_id` in the journal `detail`/intent so reconciliation and webhook (`TRADE_UPDATE`) handlers can resolve either direction without a new store.
- **Idempotency key — RESOLVED YES (was the hard blocker for live; findings §3):** `POST /trade/place` accepts **`client_order_id`** — *"Optional caller-supplied identifier passed through to the brokerage for idempotent order placement. Must be a canonical 36-character UUID."* HermX passes a UUID derived from `cl_ord_id` (UUIDv5 of `cl_ord_id` when it isn't already a UUID — deterministic so a retry re-derives the same key) → SnapTrade dedups a double-submit **at the source/brokerage**. This is a source-level backstop *on top of* the HermX-side defenses, which remain mandatory regardless: `cl_ord_id` dedupe (Gate 6) + write-ahead journal + the "never blind-retry writes" rule (§7.3). **Caveat:** the two-step `/trade/impact` → `/trade/{tradeId}` path uses the single-use, 5-min-expiry `tradeId` for its idempotency instead of `client_order_id` — another reason to prefer the one-step `/trade/place` path for the live submit.

### 8.3 Ledgers (append-only JSONL, existing discipline)

- `logs/snaptrade-events.jsonl` — raw inbound webhooks (durable, pre-dispatch), the WAL for the event path.
- Existing `pipeline.jsonl`, `alerts.jsonl`, `order-journal.jsonl` carry SnapTrade orders unchanged — the normalized shapes make SnapTrade rows indistinguishable in structure from CCXT rows (distinguished only by the `exchange` slug).
- Consider whether read-only aggregation holdings should persist to a ledger at all (privacy, §6.5) — default to memory/cache, not JSONL, for read-only accounts.

---

## 9. Testing Strategy

Per project rules: tests must exercise **production code paths**, never re-implement handlers inline, and must arm through the **current** production path (not legacy flag chains).

### 9.1 Unit tests

- **`normalizer.py` (highest value, pure functions):** table-driven tests mapping recorded SnapTrade payloads → expected canonical shapes. Cover: filled/pending/canceled/rejected/unknown order states; long & (if supported) short positions; cash vs margin balances; `fee_usd`/`slippage_pct` present and `None`; the reverse `to_order_intent_request` mapping. These need **no network** — golden fixtures in, canonical shapes out.
- **`errors.py`:** each SnapTrade failure class → correct HermX mode (§7.1). Assert timeout/exception/partial → UNKNOWN, explicit reject → REJECTED.
- **`credentials.py` SnapTrade branch:** fail-closed on partial credential set; demo vs live selection; redaction of new secret keys.
- **Schema:** v2 strategy with `type: equity` rejects `leverage`/`margin_mode`; accepts SnapTrade instrument slug; still forbids inline credentials.

### 9.2 Mock SnapTrade client

A `FakeSnapTradeClient` implementing the same method surface as `client.py`, returning canned raw payloads (including error/timeout/429/partial cases). The adapter and aggregation reader are tested against it — exercising the **real** `SnapTradeExecutor` and **real** `normalizer.py`, mocking only the transport boundary (mirrors how CCXT adapter tests mock the exchange, not the adapter).

### 9.3 Service-level tests (gate ladder reuse)

Route a SnapTrade strategy through the **real `ExecutionService`** with the fake client and assert:
- Kill switch: `HERMX_LIVE_TRADING` off blocks a SnapTrade `live` submit at `live_trading_kill_switch`, identically to CCXT (research pre-condition 4).
- Auth-health: an unhealthy connection (ConnectionStore) blocks at `auth_health`.
- Idempotency: duplicate `cl_ord_id` blocks at `idempotency` (Gate 6).
- Journal: PLANNED → SUBMITTED → (FILLED|UNKNOWN) transitions written; ambiguous submit → UNKNOWN not REJECTED.
- close_only bypasses kill switch + symbol pause for a SnapTrade close.

### 9.4 Webhook handler tests

- Valid SnapTrade signature accepted; TradingView HMAC **rejected** on `/snaptrade/webhook` (auth-plane separation).
- Idempotent replay of an `order-fill` does not double-transition (relies on `can_transition`).
- `connection-broken` flips ConnectionStore health → subsequent submit fails closed.
- Out-of-order events handled (fill before ack, etc.).

### 9.5 Integration / pilot plan (research phasing)

- **Phase 0 spike:** register a SnapTrade **sandbox** user (read-only simulated broker — covers positions/balances/orders/transactions + error scenarios) for the read fixtures, and link one **Alpaca Paper** account (sandbox can't place orders) for the place/impact fixtures. Run `normalizer.py` against real payloads to validate the mapping and **confirm** the §10 answers (most already resolved from the docs — findings doc).
- **Phase 1 aggregation:** live read-only against a real linked account (own account), verify dashboard panel. No writes.
- **Phase 2 paper execution:** end-to-end signal → `SnapTradeExecutor` → Alpaca paper → reconcile via poll **and** webhook. Validate idempotency (double-submit test with a deliberate retry), async fill, and the full journal state machine. This is the gate to any live enablement.

---

## 10. Open Questions / Deferred Decisions

Most were resolved from the docs on 2026-07-03 (`SNAPTRADE_API_FINDINGS.md`). Status legend: ✅ resolved · 🟡 partial (broker-specific — confirm on Alpaca paper in Phase 2) · ⬜ open (non-API).

| # | Question | Status | Finding |
|---|---|---|---|
| 1 | **Client-supplied idempotency key** on placement? | ✅ | **YES** — `client_order_id` (canonical UUID) on `POST /trade/place`, passed through to the brokerage for idempotent placement (findings §3, §8.2). Was the hard live blocker → **unblocked.** HermX-side dedupe + no-blind-retry still mandatory. |
| 2 | **One-step** or **check-impact-then-place**? | ✅ | **Both exist.** Use one-step `POST /trade/place`; use `POST /trade/impact` → `POST /trade/{tradeId}` (tradeId expires 5 min) only for `plan()` preview. `submit_partial` surface minimized by choosing one-step. |
| 3 | **Fill-confirmation model per broker** — sync ack / webhook / poll? | 🟡 | Webhooks `TRADE_DETECTION`/`TRADE_UPDATE` + poll on `/orders`; `wait_to_confirm` on the checked path. Sync-vs-async is **per broker** — measure on Alpaca paper in Phase 2. Webhook retry cadence is slow (~30 min) so **poll is the primary backstop**, webhook the optimization. |
| 4 | **Webhook events + delivery + signature**? | ✅ | Events enumerated (findings §4). **At-least-once, no ordering**, retry ≤3× @ ~30-min intervals. Signature = **HMAC-SHA256(raw body, `consumerKey`)** base64 — **no separate webhook secret** (§2.6, §6.1). |
| 5 | **Fee / slippage field availability** per broker. | 🟡 | `/trade/impact` gives *estimated* commission + forex fees; realized per-fill fee is broker-dependent — `None` remains contract-legal. Confirm per broker. |
| 6 | Exact **rate limits**. | ✅ | **250 req/min** global; **+10 req/min per account** on ~9 data endpoints for **Personal keys** (Commercial exempt). Headers `X-RateLimit-Limit/Remaining/Reset`; **429, no `Retry-After`** (findings §5). Sizes aggregation cadence: batch + cache per account. |
| 7 | **Python SDK** maturity / version to pin. | ✅ | **`snaptrade-python-sdk==11.0.212`** (2026-06-30, MIT, maintained), Py≥3.8, `from snaptrade_client import SnapTrade`. Handles request signing → no raw HTTP needed. Old `snaptrade`/`SnapTrade-Python` deprecated (findings §7). |
| 8 | **True sandbox** vs Alpaca-paper-only? | ✅ | **True sandbox exists** (default on non-prod keys) but is **READ-ONLY** — no place/cancel. So sandbox covers Phase-1 read/aggregation + connection/error scenarios; Phase-2 **order placement needs a real Alpaca Paper connection** (findings §8). |
| 9 | **Buying-power semantics** — conservative `avail`? | ✅ | Use **`cash`** (not `buying_power`; the latter includes margin and may be `null`) (findings §2). |
| 10 | **Regulatory framing** at intended scale? | ⬜ | Unchanged — legal review before §4c (research §9). Not an API question. |
| 11 | **Options representation** (multi-leg / multiplier). | ⬜ | Deferred. `place-option-order` (single/multi-leg) exists, but the single-`inst_id` shape needs a `base.py` extension — **not in this design.** |
| 12 | **Per-strategy adapter routing** — (A) map vs (B) `instrument.backend`. | ⬜ | Internal HermX decision (§2.7); shared-code change needs confirmation. |

---

## Appendix — Change Inventory

| File | Change | Shared? |
|---|---|---|
| `src/executors/snaptrade_adapter.py` | NEW — `SnapTradeExecutor(BaseExecutor)` | no |
| `src/snaptrade/` (client, auth, connections, normalizer, errors, aggregation) | NEW package | no |
| `src/executors/factory.py` | one-line registration + try/except guard | low |
| `src/execution/service.py` (`resolve_execution_config`) | per-strategy adapter routing (§2.7) | **yes — confirm** |
| `src/security/credentials.py` | `snaptrade` branch + redaction keys | **yes — confirm** |
| `src/webhook_receiver.py` | NEW `/snaptrade/webhook` endpoint (separate auth) | low (additive) |
| `src/dashboard.py` + `dashboard-ui/` | connection panel + multi-broker holdings | no |
| `schemas/strategy.schema.json` | `type: equity`, conditional leverage/margin, instrument slug | **yes — confirm** |
| `config/runtime.*.demo.json` / `engine-config.json` | `snaptrade` config section | low |

Per the project's dev rules, the three shared-code changes (execution service, credentials, strategy schema) require explicit confirmation, and a change touching more than three files should be broken into slices — the natural slicing is the research doc's phasing: **aggregation (read) → paper execution → live (gated).**
