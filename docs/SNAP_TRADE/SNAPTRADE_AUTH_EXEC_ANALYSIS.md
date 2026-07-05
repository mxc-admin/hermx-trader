# SnapTrade Integration — Auth & Execution Analysis

**Status:** Design analysis. No implementation.
**Date:** 2026-07-03
**Purpose:** Answer two specific questions against the SnapTrade design corpus:
1. How does the design authenticate a user and connect their brokerage account?
2. How does a TradingView signal become a real order in that account?

**Source docs (read in order):**
- [`SNAPTRADE_RESEARCH.md`](./SNAPTRADE_RESEARCH.md) — feasibility, pricing, go/no-go (cited "R §n").
- [`SNAPTRADE_API_FINDINGS.md`](./SNAPTRADE_API_FINDINGS.md) — grounded API contract (cited "F §n").
- [`SNAPTRADE_TECH_DESIGN.md`](./SNAPTRADE_TECH_DESIGN.md) — module/security blueprint (cited "TD §n").
- [`SNAPTRADE_EXECUTION_PLAN.md`](./SNAPTRADE_EXECUTION_PLAN.md) — phased build order (cited "EP Phase n").

This document is analysis only — it references sections, file paths, and method names from the design; it does not add new design or code.

---

## Part 1: User Authentication & Broker Connection Flow

### 1.0 The key mental model: three auth planes, HermX never holds broker creds

The design's central security claim (TD §6.1, R §6) is that there are **three distinct auth planes that are never conflated**:

| Plane | Who ↔ who | Secret | Governs |
|---|---|---|---|
| 1 | TradingView → HermX (inbound, **unchanged**) | TradingView HMAC | who may submit signals |
| 2 | HermX → SnapTrade (**new**) | `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` | HermX's identity to SnapTrade |
| 3 | SnapTrade → HermX (webhook, **new**) | **reuses** `SNAPTRADE_CONSUMER_KEY` | authenticity of push events |

The grounded finding (F §1) corrects an earlier assumption: the HermX↔SnapTrade plane is **not OAuth2**. SnapTrade uses a **proprietary HMAC request-signature scheme** ("Request Signatures") on every API call, and the pinned SDK (`snaptrade-python-sdk==11.0.212`, F §7) generates that signature automatically. OAuth *does* appear — but only *inside SnapTrade's connection portal*, between the **end-user and their brokerage**. HermX never touches broker OAuth (TD §6.2, R §6). This is the Plaid trust model: HermX's blast radius excludes broker passwords.

Because plane 3 reuses the plane-2 `consumerKey` (F §4 corrected TD's earlier "separate webhook secret" assumption), the design flags `consumerKey` as **the highest-value SnapTrade secret** — a leak compromises both API calls and webhook authenticity (TD §6.1 caveat).

### 1.1 Step-by-step user journey: "add broker" → "connected and ready"

Drawn from TD §2.2, §3 (API contract table), §6.4, and F §1 (exact endpoints):

1. **Register the SnapTrade user (one-time per user).**
   `SnapTradeAuthManager.register_user()` in `src/snaptrade/auth.py` → SDK `authentication.register_snap_trade_user` → `POST /snapTrade/registerUser` with body `{ "userId": "<caller-chosen>" }`.
   Returns `{ userId, userSecret }`. Constraints (F §1): `userId` is immutable, caller-chosen, and **must not be an email**; `userSecret` is SnapTrade-generated and **sensitive**.

2. **Generate the connection portal URL.**
   `SnapTradeAuthManager.login_portal_url()` → SDK `authentication.login_snap_trade_user` → `POST /snapTrade/login?userId=<>&userSecret=<>`.
   Returns `{ redirectURI, sessionId }` (F §1). The dashboard exposes this via a **new read-only endpoint** `POST /api/snaptrade/portal-url` (EP Phase 1, `src/dashboard.py` checklist item 9).

3. **User links their brokerage — directly, inside SnapTrade's portal.**
   The user opens `redirectURI` and authenticates **with their own broker** (OAuth or broker-native). HermX never sees these credentials (TD §6.2). On success SnapTrade holds the broker connection; HermX holds only the `userSecret` handle (R §6).

4. **Connection confirmed → account discoverable.**
   SnapTrade emits `CONNECTION_ADDED` / `NEW_ACCOUNT_AVAILABLE` webhooks (F §4). `GET /accounts` (F §2) then lists the account (each with an `id` UUID, `institution_name`, `total_value`). The `ConnectionStore` (`src/snaptrade/connections.py`) persists `{userId, userSecret(encrypted), broker, accountId, connection_status, linked_at, last_sync}` (TD §2.2, §8.1).

5. **Connected and ready.**
   `SnapTradeExecutor.health()` (`src/executors/snaptrade_adapter.py`) returns `{ok, exchange, connection_status, broker, last_sync, error}`, feeding both the auth-health gate and the dashboard connection panel (TD §2.1).

### 1.2 What the user sees (dashboard UI, TD §2.5)

- **Connection-status panel** — each linked broker, its `connection_status` (linked / expired / broken / re-link required), `last_sync`, and a **"Connect / Re-link"** action that opens the portal URL. A broken connection renders as a clear **fail-closed** state, mirroring how auth-health issues surface today.
- **Multi-broker holdings view** — unified positions/balances across crypto (CCXT) + external brokers (SnapTrade), driven entirely by the shared normalized shapes; group-by-broker with an aggregate net-exposure row.
- The concrete component is `dashboard-ui/components/ExternalBrokersPanel.tsx` (EP Phase 1 files-to-create), later joined by `MultiBrokerPortfolio.tsx` / `RiskAggregation.tsx` in Phase 3.

### 1.3 What happens server-side (`auth.py`, `connections.py`, `credentials.py`)

- **`src/snaptrade/auth.py` — `SnapTradeAuthManager`** (TD §2.2): resolves the **partner credential** (`clientId` + `consumerKey`) from env via the credentials module (never strategy files); owns user registration, portal-URL generation, and `userSecret` rotation.
- **`src/snaptrade/connections.py` — `ConnectionStore`** (TD §2.2, §8.1): persists per-user connection records under `HERMX_DATA_DIR` in `snaptrade-connections.json`, **`userSecret` encrypted**; exposes `connection_health(account) -> {ok, status, reason}` (the auth-health gate input); updates health on webhooks. Written atomically (tmp + rename, EP Phase 1 checklist item 4). **Not git-tracked** — the design explicitly warns `.gitignore` is inert for already-tracked files, so it must never be `git add`ed (EP §0 ground rules).
- **`src/security/credentials.py`** (TD §5.1, §6.3; EP Phase 1 checklist item 8 — **shared code, requires confirmation**): a new `snaptrade` branch in `resolve_exchange_credentials()`, returning credentials **only when the required pair is present** (fail-closed on a partial set, following the Hyperliquid precedent at `credentials.py:159-175`). The `mode` (demo/live) selects paper vs live. All new secret keys are added to the `redact_secrets` set (`credentials.py:200-234`).

### 1.4 Token lifecycle: registration → userSecret → encrypted at rest → rotation

Per TD §6.4 and F §1:

- **Register:** `register_user()` → persist `{userId, userSecret(encrypted)}`.
- **Encrypt at rest:** `userSecret` is treated as a credential — encrypted under `SNAPTRADE_ENCRYPTION_KEY`, stored in `HERMX_DATA_DIR`, never in git / strategy files / ledgers (TD §6.3, §8.1).
- **No bearer token:** F §1 confirms there is **no access/bearer token**. Every call is authenticated by signing with `consumerKey` and passing `userId`/`userSecret` as query params. The durable secrets are `userSecret` (per user) and `consumerKey` (partner).
- **Rotate:** `SnapTradeAuthManager.rotate_secret()` → SDK `authentication.reset_snap_trade_user_secret` → reset-user-secret endpoint (F §1 item 4), re-encrypting in place (TD §6.4).
- **Defense in depth:** partner `consumerKey` and per-user `userSecret` are stored separately — leaking one without the other is insufficient to act (TD §6.3).

### 1.5 Re-auth / expiry handling

Broker connections expire (broker-dependent). The design treats connection health as a **first-class, fail-closed gate input**, mirroring the existing venue auth-health gate (R §6, TD §7.4):

- **Push signal:** `CONNECTION_BROKEN` / `CONNECTION_FAILED` webhooks → `ConnectionStore.mark_unhealthy(account)`; the next `execute()` for that account fails closed at the auth-health gate. `CONNECTION_FIXED` → mark healthy (TD §2.6, F §4).
- **Re-link mechanism:** the same `POST /snapTrade/login` with the **`reconnect`** param set (passing the connection/authorization id) re-links a broken connection **without losing the HermX-side account→strategy mapping** (F §1 item 3, TD §6.4).
- **UI:** surfaces as "re-link required" with the portal action (TD §2.5, §7.4).
- **Never silent:** an expired connection must **never** silently no-op a submit — it fails closed (TD §6.4, §7.4).

### 1.6 Consent and privacy (TD §6.5, R §6, §9)

- **Read-only aggregation still moves financial data** through SnapTrade → HermX. The design says to document retention and to **default read-only holdings to memory/cache, not JSONL** (TD §8.3, EP §6 risk register P1).
- **Trade execution requires explicit per-user consent to place orders**, captured in the connection flow and **logged as a consent event in a ledger** (TD §6.5). In Phase 3 this becomes an enforced gate: **no consent → fail closed** (EP Phase 3 checklist item 2).

---

## Part 2: Order Execution Flow (Signal to Fill)

### 2.0 Framing: SnapTrade is an additive `BaseExecutor`, not a new pipeline

The entire execution pipeline above the adapter boundary — the `ExecutionService` gate ladder, write-ahead order journal, idempotency, reconciliation, kill switch, per-strategy pause/demo/live — is **venue-neutral and reused unchanged** (TD §1.1). SnapTrade gets its own *adapter* (`SnapTradeExecutor(BaseExecutor)`, `src/executors/snaptrade_adapter.py`), not its own pipeline. There is exactly **one** non-additive change: the adapter selector becomes per-strategy-derivable (§2.2 below).

### 2.1 How the signal reaches SnapTradeExecutor

Full path from TD §4.2:

1. **Intake (unchanged):** TradingView alert (e.g. `SPY` buy) → `src/webhook_receiver.py` → normalize → dedupe → **fsync `raw-webhooks.jsonl` (WAL)** → enqueue → dequeue → write `signals.jsonl` (dedupe ledger).
2. **Enrich → build `execution_readiness`** with `instrument.exchange = snaptrade:<broker>:<acct>` (e.g. `snaptrade:alpaca:paper`), `type = equity` (TD §5.3 strategy example).
3. **Adapter routing** — `resolve_execution_config()` maps the instrument slug to the adapter selector `"snaptrade"` and resolves the account (TD §2.7, §4.2).
4. **`ExecutionService.execute(record)`** runs the gate ladder (§2.4 below), journals, then calls `ExecutorFactory.create()` → `SnapTradeExecutor.execute(readiness)`.

### 2.2 The one structural change: per-strategy adapter routing

Today (`src/execution/service.py:15-54`) `resolve_execution_config()` resolves the *venue* (`ccxt_exchange`) from the instrument but leaves the *adapter selector* (`execution.exchange`) **global** — every strategy uses the same adapter class (TD §1.3). To route `SPY` → SnapTrade while `BTC-USDT-SWAP` stays on CCXT, the adapter selector must become per-strategy-derivable.

Design choice (TD §2.7, EP Phase 2 checklist item 1 — **shared code, requires confirmation**):
- **Option A (recommended):** a venue→adapter registry map — crypto CEX slugs (`okx`, `binance`…) → `ccxt`; SnapTrade broker/account slugs → `snaptrade`. Backward-compatible: every existing crypto slug maps to `ccxt`, so current configs are **byte-identical**.
- Option B: an explicit `instrument.backend` field (rejected as leaking an implementation detail into strategy files).

The selector is applied **identically to submit and reconcile** so the two can never diverge (existing invariant, `service.py:35-37`). `HERMX_EXEC_BACKEND` still overrides globally as the test/forcing escape hatch. The design explicitly flags this as the **shadow-config masked-default trap** (backend `snaptrade` vs venue `snaptrade:alpaca:acct`) to audit (EP §0 ground rules).

Factory registration (`src/executors/factory.py`, TD §2.8) is one line inside a `try/except`: if the SnapTrade SDK isn't installed, the key is absent and any SnapTrade-routed strategy **fails closed** at `create()` ("Unknown execution exchange") rather than guessing a venue.

### 2.3 How the adapter maps the readiness block to a SnapTrade order

`SnapTradeExecutor.execute(readiness)` (TD §2.1) translates the venue-neutral `execution_readiness` into a **one-step `POST /trade/place`** call (F §3 confirms one-step is recommended; the two-step `/trade/impact` → `/trade/{tradeId}` path — with its 5-min `tradeId` expiry — is reserved for `plan()`/dry-run).

The reverse mapping lives in `src/snaptrade/normalizer.py::to_order_intent_request(readiness)` (TD §2.3), building the `POST /trade/place` body (fields from F §3):

| readiness → | `/trade/place` field | Notes |
|---|---|---|
| account | `account_id` (uuid) | target account |
| side | `action` | `BUY`/`SELL`/`BUY_TO_OPEN`/… |
| symbol | `symbol` **or** `universal_symbol_id` | raw `symbol` skips a `ReferenceData_getSymbols` lookup, at cost of broker-side resolution |
| order type | `order_type` | `Market`/`Limit`/`Stop`/`StopLimit` |
| TIF | `time_in_force` | `Day`/`GTC`/`FOK`/`IOC`/`GTD` |
| size | `units` **or** `notional_value` | fractional allowed |
| limit/stop px | `price` / `stop` | required for Limit/StopLimit/Stop |
| **`cl_ord_id`** | **`client_order_id`** | idempotency key (§2.5) |

The submit response is mapped by `to_fill_summary` and the status enum map in `normalizer.py` (TD §2.3, F §2 authoritative enum): `EXECUTED → filled`; `PARTIAL/PARTIAL_CANCELED → partial`; `PENDING/ACCEPTED/QUEUED/TRIGGERED/ACTIVATED/REPLACE_PENDING/CANCEL_PENDING → pending`; `CANCELED/REPLACED/EXPIRED → canceled`; `REJECTED/FAILED → rejected`; unrecognized → `unknown`/`not_found`.

**Critical parse detail (F §3, TD §2.3):** SnapTrade returns quantities and prices as **decimal strings**, not floats — the normalizer must parse them. `pos_side` is `None` for cash equities.

### 2.4 How existing HermX gates apply — unchanged

Per TD §4.2 the SnapTrade write path runs the **same gate ladder** as CCXT (R §4b, §10 pre-condition 4; EP Phase 2 checklist):

| Gate | Behavior for SnapTrade |
|---|---|
| **Arming** (`strategy_submit_flag` / `live_execution_enabled`) | per-strategy pause/demo/live pill applies unchanged (TD §2.5) |
| **Auth health** | SnapTrade auth-health = `ConnectionStore.connection_health`; broken connection → fail closed |
| **Watchdog** | unchanged |
| **Kill switch** (`live_trading_kill_switch`) | live/non-sandbox submit requires `HERMX_LIVE_TRADING`; `close_only` bypasses (reduces exposure) — identical to CCXT |
| **Sandbox / live consistency** | `sandbox_only` / `live_sandbox_consistency` |
| **Symbol pause** | applies; bypassed for `close_only` |
| **Idempotency** (Gate 6) | `latest_order_record(cl_ord_id)` exists → block duplicate (`service.py:175`) |
| **Write-ahead journal** | journal PLANNED then SUBMITTED, **fail-closed on OSError** |

The `demo` execution mode for a SnapTrade strategy maps to a **paper account (Alpaca paper)**; `live` requires `HERMX_LIVE_TRADING` exactly like CCXT (TD §2.5, §5.3). Gate parity is a hard test target: `tests/test_snaptrade_service_gates.py` routes a SnapTrade strategy through the **real `ExecutionService`** and asserts every gate blocks identically to CCXT (EP Phase 2).

### 2.5 The idempotency story — now API-confirmed

This was the **hard blocker for live** (R §9, §10 pre-condition 1). F §3 resolves it **YES**:

`POST /trade/place` accepts **`client_order_id`** — verbatim: *"Optional caller-supplied identifier passed through to the brokerage for idempotent order placement. Must be a canonical 36-character UUID."* (F §3, TD §8.2).

Design mechanics (TD §8.2, EP Phase 2 checklist item 3):
- HermX generates `cl_ord_id` (existing idempotency key). At submit, `execute()` attaches `client_order_id` = **UUIDv5 of `cl_ord_id`** when `cl_ord_id` isn't already a UUID — **deterministic**, so a retry re-derives the same key and SnapTrade dedups the double-submit **at the source/brokerage**.
- This is a **source-level backstop on top of** — never a replacement for — the mandatory HermX-side defenses: `cl_ord_id` dedupe (Gate 6) + write-ahead journal + the **"never blind-retry writes"** rule (TD §7.3).
- **Caveat:** the two-step `/trade/impact` → `/trade/{tradeId}` path uses the single-use, 5-min-expiry `tradeId` for its idempotency instead — another reason to prefer one-step `/trade/place` for the live submit (TD §8.2).

Independently, the **order-id map** is stored at submit — `{cl_ord_id ↔ brokerage_order_id, client_order_id, account, inst_id, submitted_at}` — in the order journal `detail` (already keyed by `cl_ord_id`), so no new store is needed and later webhook/poll can resolve either direction (TD §8.2).

### 2.6 Async fill handling (webhook + poll reconciliation)

SnapTrade fills are often **asynchronous** (R §8, TD §7). The design handles this with **poll as the backstop, webhook as the optimization** (TD §2.6, §4.2, §4.3):

- **On ACK:** a submit response with `PENDING/ACCEPTED/QUEUED` maps to `mode="submit_enabled"` → journal **SUBMITTED** (not FILLED), exactly as the service already treats a CCXT ack (`service.py:220-228`). A synchronous `EXECUTED`/`PARTIAL` maps to `mode="filled"` (TD §2.1).
- **Poll reconciliation (backstop):** post-submit poll with backoff (`snaptrade.reconcile_poll_seconds` / `reconcile_max_attempts`) → `SnapTradeExecutor.get_order(cl_ord_id/ord_id)` → `can_transition(SUBMITTED, state)` → FILLED / REJECTED / UNKNOWN; mismatch → `emit_reconcile_alert` (TD §4.2).
- **Webhook (optimization):** `POST /snaptrade/webhook` (`src/webhook_receiver.py`, TD §2.6) with **separate auth** — `Signature` = base64(HMAC-SHA256(raw body, `SNAPTRADE_CONSUMER_KEY`)), **not** the TradingView HMAC, and **no separate webhook secret** (F §4 corrected TD). Handler is thin: verify signature → append raw event to durable `logs/snaptrade-events.jsonl` → dispatch by `eventType`:
  - `TRADE_DETECTION` / `TRADE_UPDATE` → resolve `brokerage_order_id` → `cl_ord_id` via the order-id map → nudge reconciliation.
  - `CONNECTION_BROKEN` / `CONNECTION_FAILED` → `mark_unhealthy` (→ fail-closed gate); `CONNECTION_FIXED` → healthy.
  - `ACCOUNT_HOLDINGS_UPDATED` → invalidate aggregation cache.
- **Delivery guarantees (F §4):** **at-least-once, no ordering**, retry ≤3× at ~30-min intervals. So the handler must be **idempotent** (relies on the journal's `can_transition` guard, `service.py:317`), and because webhook retry cadence is slow, **the poll — not webhook retry — is what bounds time-to-terminal** (TD §2.6). A missed webhook degrades to poll, never to a lost order.

### 2.7 State machine: PLANNED → SUBMITTED → (FILLED | REJECTED | UNKNOWN)

From TD §4.2 and §7.1, the error taxonomy (`src/snaptrade/errors.py`) maps SnapTrade failures onto modes the `ExecutionService` already understands (`service.py:219-238`) so **no new service branches** are needed:

| SnapTrade outcome | HermX mode | Journal terminal |
|---|---|---|
| Network timeout on submit | `submit_timeout` | **UNKNOWN** (order may have reached broker) |
| Exception mid-submit | `submit_exception` | **UNKNOWN** |
| Two-step, one leg failed | `submit_partial` | **UNKNOWN** + reconcile alert |
| Explicit broker reject (validation / insufficient funds; `status = REJECTED`/`FAILED`) | reject | **REJECTED** |
| ACK (`PENDING`/`ACCEPTED`/`QUEUED`) | `submit_enabled` | **SUBMITTED** → reconcile → terminal |
| Confirmed fill (`EXECUTED`) | `filled` | **FILLED** |

**The critical invariant (TD §7.1, F §6):** *any ambiguity is UNKNOWN, never REJECTED.* A timeout after the order may have reached the broker must not be recorded as rejected — that would corrupt position math. UNKNOWN forces reconciliation. An explicit brokerage `REJECTED`/`FAILED` is a **terminal state, not a transport error** (F §6). This is asserted in tests: `test_snaptrade_reconciliation.py` validates `not_found → pending → terminal`; `test_snaptrade_executor_paper.py` asserts ambiguity → UNKNOWN not REJECTED (EP Phase 2).

---

## Part 3: What This Means for the Operator

### 3.1 Single-user (solo operator) experience

- **Zero cost.** SnapTrade is free up to **5 connections** (R §7). The solo operator aggregating their own IBKR/Fidelity/Alpaca alongside crypto pays **$0** — making the read-only pilot (R §4a / EP Phase 1) zero-cost.
- **Unified view.** One dashboard shows crypto (CCXT) + equities/ETFs (SnapTrade) through the same normalized shapes (TD §2.5).
- **Latency is immaterial.** SnapTrade adds a hop and async fills (R §8), but HermX signals are TradingView bar-close events (2h–4h timeframes) — not a HFT path. The write-ahead-journal + reconcile model is *well-suited* to async confirmation.
- **Same controls.** A SnapTrade strategy is paused/armed/demoed exactly like a CCXT one; `HERMX_LIVE_TRADING` is the same global kill switch (TD §2.5, EP §5).

### 3.2 Multi-user scaling path

- **Phasing (R §10, EP):** aggregation (Phase 1, read, $0) → paper execution (Phase 2, Alpaca paper) → live gated (Phase 3, one broker first) → multi-account operator dashboard (R §4c).
- **Cost scales with *users*, not volume/AUM** (R §7): ~$1.50/connected-user/month beyond the free 5; enterprise floor ~$1,000/mo crosses over ~667 users. Phase 3 adds a **connected-user count + billing threshold alert** (EP Phase 3 checklist item 5).
- **Per-account isolation:** a broken/expired connection on one account fails closed for **that account only** — other accounts and the crypto path are unaffected (EP Phase 3 checklist item 6).
- **Deferred by design:** options (multi-leg/multiplier shape gap, R §5, TD non-goals) and at-scale multi-tenant management.
- **Legal is a hard gate** before live at multi-user scale (order-routing-on-behalf-of-users obligations; R §9, EP Phase 2 pre-condition 6 / risk register).

### 3.3 Security posture

- **HermX never holds broker credentials** — Plaid trust model; blast radius excludes broker passwords (TD §6.2).
- **`userSecret` encrypted at rest** under `SNAPTRADE_ENCRYPTION_KEY` in `HERMX_DATA_DIR`; never in git / strategy files / ledgers (TD §6.3, §8.1).
- **Three auth planes never conflated** at the endpoint level — distinct verifiers, endpoints, signed inputs; a cross-plane signature (TradingView HMAC on `/snaptrade/webhook` or vice-versa) **must fail** (TD §6.1, tested EP Phase 2).
- **`consumerKey` is the highest-value secret** (signs API calls *and* verifies webhooks after the F §4 correction) — rotation must be coordinated across both planes with a dual-accept window (EP §8).
- **Secrets redacted** from all error/log surfaces via the extended `redact_secrets` set (TD §6.3, `credentials.py:200-234`).
- **Consent logged** as a ledger event for trade execution (TD §6.5).

### 3.4 Fail-closed guarantees

The design's fail-closed posture is consistent across every failure mode:

- **Missing/broken adapter** (SDK absent) → strategy fails closed at `create()`; never guesses a venue (TD §2.8).
- **Unhealthy/expired connection** → auth-health gate blocks the write path; never silent no-op (TD §6.4, §7.4).
- **Partial credential set** → `credentials.py` returns nothing (Hyperliquid precedent, TD §5.1).
- **Kill switch off** → `HERMX_LIVE_TRADING` blocks all live submits (crypto + SnapTrade); the always-available emergency lever (EP §6 escalation).
- **Ambiguous submit** → UNKNOWN, never REJECTED — forces reconciliation, protects position math (TD §7.1).
- **Both feature flags default off** — build-time `engine-config.json` `snaptrade.enabled: false` and runtime `control-state.json` `snaptrade_enabled: false` → SnapTrade fully **dark** until deliberately enabled (EP §0, §5).
- **Blast-radius isolation:** a SnapTrade outage fails closed for SnapTrade venues **only**; the CCXT crypto path has zero SnapTrade dependency and is entirely unaffected (TD §7.5, R §9). Aggregation failure is cosmetic ("unavailable"), never a raise into the dashboard render.

---

## Summary

**Authentication (Part 1):** A user links a broker via a **portal-redirect flow** — HermX registers a SnapTrade user (`POST /snapTrade/registerUser` → `userSecret`), generates a portal URL (`POST /snapTrade/login` → `redirectURI`), and the user authenticates **directly with their broker** inside SnapTrade's portal. HermX↔SnapTrade auth is a **proprietary HMAC scheme** (not OAuth2), handled by the pinned SDK. The `userSecret` is encrypted at rest, rotatable, and never leaves `HERMX_DATA_DIR`. Broken connections fail closed and re-link via the `reconnect` param without losing the account mapping. HermX **never holds broker credentials**.

**Execution (Part 2):** A TradingView signal flows through the **unchanged** intake + gate ladder + journal; `resolve_execution_config()` routes the instrument slug to the `snaptrade` adapter; `SnapTradeExecutor.execute()` maps the readiness block to a **one-step `POST /trade/place`** carrying a **`client_order_id`** (API-confirmed idempotency key, the resolved hard blocker). Async fills are reconciled by **poll (backstop) + webhook (optimization)** through the state machine **PLANNED → SUBMITTED → (FILLED | REJECTED | UNKNOWN)**, with the invariant that ambiguity is always UNKNOWN.

**Operator (Part 3):** Solo operator runs at **$0** with a unified crypto+equities view and all existing controls; multi-user scales linearly per connected user behind a phased, legally-gated rollout; the security posture keeps broker creds out of HermX entirely; and every failure mode — missing adapter, expired connection, ambiguous submit, vendor outage — **fails closed**, isolated to SnapTrade venues, with the CCXT crypto path never at risk.
