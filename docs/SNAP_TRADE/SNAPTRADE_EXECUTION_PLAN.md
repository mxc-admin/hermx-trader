# SnapTrade Integration — Execution Plan

**Status:** Execution plan. Ready to code Phase 1.
**Author:** Engineering
**Date:** 2026-07-03
**Prereq reading (read in order):**
1. [`SNAPTRADE_RESEARCH.md`](./SNAPTRADE_RESEARCH.md) — feasibility, pricing, go/no-go.
2. [`SNAPTRADE_API_FINDINGS.md`](./SNAPTRADE_API_FINDINGS.md) — grounded API contract (real endpoints, field names, idempotency, webhooks, rate limits, SDK, sandbox).
3. [`SNAPTRADE_TECH_DESIGN.md`](./SNAPTRADE_TECH_DESIGN.md) — module boundaries, API contract, security. **This plan assumes you have read it** and references its sections (e.g. "TD §2.7").

> **Doc-research update (2026-07-03).** The SnapTrade docs answered most Phase-0 blockers *before* touching a vendor account (findings doc). Net effect on this plan: **Phase 0 shrinks** (idempotency Q1 = YES, rate limits, SDK, webhook scheme, buying-power all known from docs); the residual Phase-0 work is **validating** the doc claims against real payloads + capturing fixtures, not discovering them. One finding reshapes testing: SnapTrade's **sandbox is read-only** — it cannot place/cancel orders — so **order-placement testing must use a real Alpaca Paper connection**, while the sandbox is ideal for Phase-1 read/aggregation fixtures and connection/error scenarios.

**What this document is:** the actionable, file-by-file, phase-by-phase build order. A developer should be able to open this and start coding Phase 1 today. It does **not** re-argue the design (that's the tech design) or the business case (that's the research doc). It sequences the work, names every file with an absolute path, lists tests, and sets the go/no-go gates and rollback for each phase.

**What this document is NOT:** code. No implementation is written here. Where a mechanism is already designed, this plan points at the tech-design section rather than restating it.

---

## 0. Ground rules (HermX house style — non-negotiable)

These are the project rules that shape every task below. They are called out here once so each phase can assume them.

- **Approach-first (`dev-rules.md` #1):** describe the approach and get approval before writing code for each **shared-code** change. The three shared-code touches — `src/execution/service.py` (`resolve_execution_config`), `src/security/credentials.py`, `schemas/strategy.schema.json` — each require explicit confirmation (TD Appendix).
- **>3 files → slice it (`dev-rules.md` #3):** each phase below is already sliced so no single reviewable unit sprawls. If a task grows past 3 files, stop and re-slice.
- **Tests exercise production code paths (`code-quality.md` anti-patterns):** never re-implement a handler inline in a test; never arm a test through a legacy config-flag chain. Mock only the transport boundary (the SnapTrade client), never the adapter or normalizer.
- **Run tests before every commit:** `./.venv/bin/pytest tests/ -x -q`. SnapTrade-scoped: `./.venv/bin/pytest tests/test_snaptrade_*.py -x -q`.
- **Git flow:** stage → commit → push. Branch off `main`; do not commit to `main` directly for feature work. Checkpoint commits (`checkpoint: …`) are fine mid-slice.
- **Audit new config sources for masked defaults.** The shadow-config regression (deleting a config source made `_dashboard_executor` fall back to `exchange="ccxt"`, a **backend** name not a **venue**, breaking `CcxtExecutor._exchange_id()`) is the cautionary tale: every new config default must be traced through every consumer. SnapTrade's `venue_adapter_map` and the `exchange` slug (`snaptrade:<broker>:<acct>`) are exactly the kind of backend-vs-venue confusion that bit us before — audit them.
- **Feature flag lives in `control-state.json`,** same pattern as `symbol_pauses` and per-strategy mode overrides. It is **not** the same thing as `engine-config.json`'s `snaptrade.enabled` (that is the *baked build-time* enable; `control-state.json` is the *operator runtime* toggle). Both must be off by default → SnapTrade fully dark.
- **`control-state.json` needs a writable mount.** The dashboard/receiver write it under `HERMX_DATA_DIR`; compose must mount `hermx-state:/app/data` **rw** even under `read_only: true`, or toggles silently no-op. The SnapTrade connection store (`snaptrade-connections.json`) inherits this exact requirement.
- **`.gitignore` is inert for already-tracked files.** Do not add `snaptrade-connections.json` or any operator-mutable state to git and expect `.gitignore` to protect it — if it must not be tracked, never `git add` it in the first place; if it is already tracked, `git rm --cached` it.

---

## Phase Breakdown — Overview

| Phase | Name | Write risk | Precondition to start | Est. |
|---|---|---|---|---|
| **0** | Spike / **validate** doc findings + capture fixtures | none | vendor account | **1–2 d** (was 2–4 — docs pre-answered the blockers) |
| **1** | Read-Only Aggregation | none | Phase 0 mapping validated | 5–8 d (was 6–9) |
| **2** | Paper Trading (demo-gated) | paper only | Phase 1 shipped + go criteria | 10–15 d |
| **3** | Live Trading + Multi-Account | real money (gated) | Phase 2 sign-off + legal | 12–18 d |

**Why Phase 0 shrank.** It was scoped to *discover* the TD §10 blockers with a live account. The docs (findings doc) already answer Q1 (idempotency = `client_order_id` UUID, YES), Q2 (one-step `POST /trade/place`), Q4 (webhook events + HMAC/`consumerKey` scheme), Q6 (rate limits), Q7 (SDK `snaptrade-python-sdk==11.0.212`), Q8 (read-only sandbox), Q9 (`cash` = conservative `avail`). Phase 0 is now **confirmation + fixture capture**, not research. It stays a spike (no production merges).

Phase 0 is a **spike**, not a shipping phase — it exists to answer TD §10's open questions with real payloads before any production code is trusted. It is folded into Phase 1's effort estimate below but called out separately because its output (recorded fixtures + answered blockers) is a hard input to everything after.

---

## Phase 0 — Spike (answer the blockers)

**Not shippable. No production code merges from this phase** — its artifacts are recorded fixtures and answered questions.

### Goals
**Validate** the doc-sourced answers (findings doc) against real payloads and **capture golden fixtures** for the normalizer tests. Registration/portal/reads use the **SnapTrade sandbox** (read-only, default on non-prod keys); the **order-placement** legs use a real **Alpaca Paper** connection (the sandbox cannot place orders).

### Confirm-against-real (each already answered by docs — verify, don't discover)
- **Q1 (idempotency)** — docs: `POST /trade/place` accepts `client_order_id` (UUID). **Confirm** an Alpaca-paper order carrying a `client_order_id` is idempotent (submit the same key twice → one order). **Still the hard gate for Phase 3, but now expected-pass.**
- **Q2 (one-step)** — docs: use one-step `POST /trade/place`. Confirm response shape/status on paper.
- **Q3 (fill model per broker)** — the genuinely broker-specific one: on Alpaca paper, is the fill sync in the `/trade/place` response, or via `TRADE_UPDATE` webhook / `/orders` poll? Sizes reconcile backoff.
- **Q4 (webhooks)** — docs: events enumerated; `Signature` = HMAC-SHA256(body, `consumerKey`), at-least-once. Confirm a real webhook body + verify a signature end-to-end.
- **Q5 (fee/slippage)** — capture a real fill and note which fee/price fields Alpaca paper populates.
- **Q9 (buying-power)** — docs: `cash` is conservative. Confirm against a real balances payload.
- (Q6 rate limits, Q7 SDK, Q8 sandbox — **taken as resolved from docs**, no live check needed.)

### Deliverables
- `SNAPTRADE_PHASE0_FINDINGS.md` (new, to be created under `docs/`) — one row per question: doc answer + **confirmed?** + evidence (payload snippet). (Cross-reference `SNAPTRADE_API_FINDINGS.md`.)
- `tests/fixtures/snaptrade/` — recorded real raw payloads (secrets stripped): a positions response, a balances response, an activities/transactions response, order objects for each status (`EXECUTED`/`PENDING`/`CANCELED`/`REJECTED`/`PARTIAL`), a `POST /trade/place` response, a `POST /trade/impact` response, and a sample webhook body per event type (`TRADE_UPDATE`, `CONNECTION_BROKEN`, `ACCOUNT_HOLDINGS_UPDATED`). Sandbox covers the read fixtures + error scenarios; Alpaca paper covers the place/impact fixtures.

### Testing / verification
- Manual: `snaptrade-python-sdk==11.0.212` — register sandbox user (`POST /snapTrade/registerUser`), generate portal URL (`POST /snapTrade/login` → `redirectURI`), connect the sandbox simulated broker (pick scenarios: self-directed, cash-only, invalid-creds, rate-limited), pull positions/balances/orders. Then link **Alpaca paper**, place one order via `POST /trade/place` with a `client_order_id`, observe the fill path (response? `TRADE_UPDATE` webhook? `/orders` poll?), and re-submit the same `client_order_id` to confirm idempotency.
- No pytest yet — this is exploratory.

### Go/no-go to Phase 1
- Field mapping (TD §2.3 / findings §2–3) validated against real payloads — no unmapped critical field; `cash`→`avail` confirmed (Q9); decimal-string quantity/price parsing confirmed.
- Read fixtures captured and committed under `tests/fixtures/snaptrade/`.
- (Q1/Q3 order-placement confirmations may lag to the Phase-1→2 boundary — they gate **Phase 2/3**, not Phase 1's read path.)

### Rollback
Delete the spike branch. Nothing shipped.

**Effort:** **1–2 developer days** (down from 2–4 — the docs pre-answered the discovery work; this is confirmation + fixture capture). Included in Phase 1's range.

---

## Phase 1 — Read-Only Aggregation

**Ship this first. Zero write risk:** `execute()` is never called, no consent-to-trade asserted, the gate ladder is not involved (TD §2.4).

### Scope & goals
Link one or more brokerage accounts via SnapTrade; HermX **reads** normalized holdings / balances / recent orders and renders them in the dashboard alongside crypto, in an "External Brokers" panel. No order placement. The value is a unified crypto + equities exposure view at **zero cost** (free tier, solo operator).

The read path is the `SnapTradeExecutor` **constructed in read-only mode** with its observe-only methods called (TD §2.4) — not a separate code path. This forces the adapter's construction discipline (must be constructable without asserting write credentials) from day one.

### Files to create

| Path | Purpose |
|---|---|
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/__init__.py` | package marker; export the public surface (`SnapTradeClient`, `ConnectionStore`, normalizer fns) |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/errors.py` | SnapTrade exception → HermX error taxonomy (TD §7.1) — needed early so client/normalizer can raise/classify |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/client.py` | `SnapTradeClient` — thin wrapper over **`snaptrade-python-sdk==11.0.212`** (`from snaptrade_client import SnapTrade`; SDK handles request signing → no raw HTTP). **Read methods only** in Phase 1: `account_information.get_user_account_positions`, `.get_user_account_balance`, `.get_user_account_orders`, `transactions_and_reporting.get_activities`, `connections.list_brokerage_authorizations`, `account_information.list_user_accounts` |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/auth.py` | `SnapTradeAuthManager` — `register_user()`, `login_portal_url()`, `userSecret` lifecycle (TD §2.2) |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/connections.py` | `ConnectionStore` — persist `{userId, userSecret(encrypted), broker, accountId, connection_status, linked_at, last_sync}` under `HERMX_DATA_DIR` |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/normalizer.py` | pure functions: `to_normalized_position`, `to_normalized_balance`, `to_normalized_order` (read direction only in Phase 1) |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/aggregation.py` | orchestrator: for each linked account → get_positions + get_balance + recent order history, tag with broker slug, return merged list (TD §2.4) |
| `/Users/anatolizurablev/dev projects/hermx/src/executors/snaptrade_adapter.py` | `SnapTradeExecutor(BaseExecutor)` — **observe-only methods + `health()` only** in Phase 1; `execute()` raises "not enabled" |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_normalizer.py` | table-driven pure-function tests against Phase-0 fixtures |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_client.py` | client against a `FakeSnapTradeClient` / recorded fixtures; redaction of secrets in errors |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_connections.py` | connection store CRUD + encryption at rest + health flip |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_aggregation.py` | aggregation reader against fake client; failure = "unavailable", never a raise |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_dashboard.py` | `/api` payload gains `external_brokers` section; existing crypto fields untouched |
| `/Users/anatolizurablev/dev projects/hermx/tests/fixtures/snaptrade/` | (from Phase 0) recorded payloads |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/ExternalBrokersPanel.tsx` | connection-status + holdings panel (name per existing component conventions) |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_RUNBOOK.md` | operator runbook stub (connection flow, re-link) — expanded each phase |

### Files to modify

| Path | Change | Shared? |
|---|---|---|
| `/Users/anatolizurablev/dev projects/hermx/src/security/credentials.py` | add `snaptrade` branch to `resolve_exchange_credentials()`; add `SNAPTRADE_*` keys to `redact_secrets` set (TD §5.1, §6.3) | **yes — confirm first** |
| `/Users/anatolizurablev/dev projects/hermx/src/dashboard.py` | `/api` payload `external_brokers` section from aggregation reader (TD §2.5); new read-only API endpoints for connection list + portal URL generation | no (additive) |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/app/…` | wire `ExternalBrokersPanel` into the dashboard page | no |
| `/Users/anatolizurablev/dev projects/hermx/config/runtime.demo.json` (+ per-exchange variants as needed) | add optional `snaptrade` section, `enabled: false` (TD §5.2) | low |
| `/Users/anatolizurablev/dev projects/hermx/Dockerfile` | ensure `src/snaptrade/` is copied into the Python stage; pin **`snaptrade-python-sdk==11.0.212`** in the Python deps layer | low |
| `/Users/anatolizurablev/dev projects/hermx/docker-compose.yml` | confirm `hermx-state:/app/data` **rw** mount present (connection store lives there); add `SNAPTRADE_*` env passthrough | low |

### HermX-specific implementation checklist (file by file)

1. **`errors.py`** — enumerate SnapTrade failure classes; map each to the HermX mode table (TD §7.1). Reads only need `not_found` / network / 429 / auth-expired classes in Phase 1. Every raised error must be redaction-safe.
2. **`client.py`** — use the **`snaptrade-python-sdk==11.0.212`** (Q7 resolved; SDK signs requests). Pin exactly — the SDK is OpenAPI-codegen'd and version-churny. All network I/O funnels here; owns timeouts and bounded exponential backoff for reads. **429 handling: no `Retry-After`** — read `X-RateLimit-Reset` (seconds) then back off + jitter (findings §5). Mind the **Personal-key per-account 10/min** limit: batch positions+balances+orders per account, cache aggressively. Redact secrets via `redact_secrets` (`src/security/credentials.py`) extended with SnapTrade keys. Return **raw** payloads — no normalization here.
3. **`auth.py`** — resolve partner creds (`SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY`) from env via credentials module, never strategy files. Implement `register_user()` and `login_portal_url()`. `userSecret` returned by registration is a credential — hand it to `ConnectionStore` encrypted.
4. **`connections.py`** — store under `HERMX_DATA_DIR` (same mount as `control-state.json`), file `snaptrade-connections.json`, `userSecret` field **encrypted** under `SNAPTRADE_ENCRYPTION_KEY`. Atomic write (tmp + rename), same discipline as the cron gate sidecar. Expose `connection_health(account)`. **Do not `git add` this file.**
5. **`normalizer.py`** — pure, no I/O. Implement the three read mappings (TD §2.3, real field names): position `symbol.symbol.symbol→inst_id`, `units→pos`, `average_purchase_price→avg_px`, `open_pnl→upl`; balance `currency.code→ccy`, **`cash→avail`** (conservative — not `buying_power`, Q9); order `brokerage_order_id→ord_id`, `client_order_id→cl_ord_id`, `universal_symbol.symbol→inst_id`, status enum → HermX state (TD §2.3 map). **Parse decimal strings → float** (SnapTrade returns quantities/prices as strings). `exchange` field = `snaptrade:<broker>:<acct>` slug. `pos_side = None` for cash equities. Every shape carries `raw`.
6. **`aggregation.py`** — for each linked account in `ConnectionStore`: read positions + balances + recent order history, tag with broker slug, merge with the same normalized shapes CCXT emits. **Failure is cosmetic** — a broker shows "unavailable"; never raise into the dashboard render, never touch crypto rendering.
7. **`snaptrade_adapter.py`** — implement observe-only methods + `health()`. **`execute()` must exist but raise a clear "SnapTrade execution not enabled" error** in Phase 1 (it is wired but dark). Construction discipline (TD §2.1): constructable read-only, no write-credential assertion.
8. **`credentials.py` (shared — confirm)** — new `snaptrade` branch: return creds only when the required pair is present (fail-closed on partial set, per the Hyperliquid precedent). `mode` (demo/live) selects paper vs live where SnapTrade distinguishes. Add all new secret keys to the redaction set. **Audit for masked defaults** — do not let an empty SnapTrade config resolve to a crypto default.
9. **`dashboard.py`** — additive `external_brokers` section; new endpoints: `GET /api/snaptrade/connections`, `POST /api/snaptrade/portal-url` (generate link). No write endpoints. Trace the new config through `_dashboard_executor` for the shadow-config-style masked-default trap.
10. **`ExternalBrokersPanel.tsx`** — render per-broker connection status (linked / expired / broken / re-link required), `last_sync`, holdings/balances grouped by broker, a "Connect / Re-link" action opening the portal URL. Rebuild `dashboard-ui/out` (Node stage) so the Docker Python stage bakes it (TD §2.5; multi-stage build).

### Testing checklist

**Unit (pure, no network):**
- `normalizer.py` — table-driven: filled/pending/canceled/rejected/unknown orders; long positions; cash vs margin balances; `avail` = conservative field; `raw` always present; `exchange` slug format. Run against Phase-0 fixtures.
- `credentials.py` snaptrade branch — fail-closed on partial set; demo vs live selection; new secrets redacted from error strings.
- `connections.py` — encrypt-at-rest round-trip; health flip; atomic write.

**Integration (fake transport):**
- `FakeSnapTradeClient` returning canned raw payloads (incl. 429, timeout, auth-expired). Exercise the **real** `SnapTradeExecutor` read methods + **real** `aggregation.py` + **real** `normalizer.py` — mock only the transport boundary.
- Dashboard `/api` payload gains `external_brokers`; crypto fields byte-identical to before.
- Aggregation failure → broker "unavailable", crypto render unaffected, no exception.

**Manual verification:**
- Register a real sandbox user, generate portal URL, link own Alpaca paper / real read-only broker.
- Load dashboard → External Brokers panel shows holdings + balances + connection status.
- Kill the SnapTrade network path → panel shows "unavailable", crypto panels still render.

**Run:** `./.venv/bin/pytest tests/test_snaptrade_*.py -x -q` then full `./.venv/bin/pytest tests/ -x -q` before commit.

### Go/no-go to Phase 2
- All Phase 1 tests green; full suite green.
- Aggregation panel renders real linked-account holdings correctly; connection-broken renders as a clear fail-closed state.
- SnapTrade outage proven cosmetic (crypto path unaffected) in a manual kill test.
- `credentials.py` shared change reviewed + confirmed; no masked-default regression found in the audit.
- Connection store encryption verified; `snaptrade-connections.json` confirmed **not** tracked in git.

### Rollback
- Runtime: set `engine-config.json` `snaptrade.enabled: false` (default) → aggregation reader not called; panel hidden. No redeploy needed if the flag is read at request time.
- Code: the whole package is additive and import-guarded (factory `try/except`, TD §2.8). Reverting the `dashboard.py` diff removes the panel; the crypto path never depended on any of it.
- Data: delete `snaptrade-connections.json` from the state volume to purge links.

**Effort:** 6–9 developer days (incl. the 2–4 d Phase-0 spike).

---

## Phase 2 — Paper Trading (demo-gated)

**Write path enabled, but hard-gated to demo/paper accounts only.** No real money can move. This phase proves idempotency and reconciliation on Alpaca paper before Phase 3 is even considered.

### Scope & goals
A TradingView signal (e.g. `SPY`) routes through the **normal pipeline** and selects the `SnapTradeExecutor`, which places the order in a **paper** account (Alpaca paper). The full execution path runs — gate ladder, write-ahead journal, idempotency, reconciliation — but `live_trading_kill_switch` + the demo-only gate block any live/non-sandbox submit. Reconciliation is validated via **both** poll and webhook.

### Files to create

| Path | Purpose |
|---|---|
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_executor_paper.py` | `execute()` write path against fake client → SUBMITTED journal; ack/async-fill; UNKNOWN-not-REJECTED on ambiguity |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_service_gates.py` | route a SnapTrade strategy through the **real `ExecutionService`**; assert every gate blocks identically to CCXT |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_webhook.py` | `/snaptrade/webhook` auth-plane separation, idempotent replay, connection-broken → fail closed |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_reconciliation.py` | async fill via poll + webhook; `not_found→pending→terminal` state machine; order-id map resolution |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_PAPER_PILOT.md` | the paper-pilot runbook + pre-condition sign-off checklist (the Phase-3 gate) |

### Files to modify

| Path | Change | Shared? |
|---|---|---|
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/client.py` | add write methods: **`trading.place_force_order`** (`POST /trade/place`, one-step, carries `client_order_id`) + **`trading.get_order_impact`** (`POST /trade/impact`, for `plan()` preview); order status via `account_information.get_user_account_orders`; cancel via `POST /accounts/{id}/orders/cancel` | no |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/normalizer.py` | add reverse mapping `to_order_intent_request` (→ `/trade/place` body incl. `client_order_id` UUID from `cl_ord_id`) + `to_fill_summary` (TD §2.3) | no |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/errors.py` | complete the write-failure → mode map: `submit_timeout`/`submit_exception`/`submit_partial` → UNKNOWN; explicit reject → REJECTED (TD §7.1) | no |
| `/Users/anatolizurablev/dev projects/hermx/src/executors/snaptrade_adapter.py` | implement real `execute()` + `plan()`; store order-id map at submit (TD §8.2); async-fill-aware ACK → `submit_enabled` | no |
| `/Users/anatolizurablev/dev projects/hermx/src/executors/factory.py` | one-line registration + `try/except` guard (TD §2.8) | low |
| `/Users/anatolizurablev/dev projects/hermx/src/execution/service.py` | `resolve_execution_config()` per-strategy adapter routing (TD §2.7, option A registry map) | **yes — confirm first** |
| `/Users/anatolizurablev/dev projects/hermx/src/webhook_receiver.py` | new `POST /snaptrade/webhook` endpoint, **separate auth** — verify `Signature` = HMAC-SHA256(body, **`SNAPTRADE_CONSUMER_KEY`**), not TradingView HMAC (no separate webhook secret; findings §4); durable `logs/snaptrade-events.jsonl`; idempotent dispatch (TD §2.6) | low (additive) |
| `/Users/anatolizurablev/dev projects/hermx/schemas/strategy.schema.json` | `type: equity`; conditional-reject `leverage`/`margin_mode` for cash; SnapTrade instrument slug (TD §5.3) | **yes — confirm first** |
| `/Users/anatolizurablev/dev projects/hermx/src/dashboard.py` | per-strategy pause/demo/live controls apply to SnapTrade strategies; order preview (`plan()`) endpoint | no |
| `/Users/anatolizurablev/dev projects/hermx/docker-compose.yml` | `SNAPTRADE_ENCRYPTION_KEY` env passthrough (webhook verify reuses `SNAPTRADE_CONSUMER_KEY` — no separate secret); ensure webhook port exposed | low |

### HermX-specific implementation checklist (file by file)

1. **`service.py` `resolve_execution_config` (shared — confirm before coding).** Implement TD §2.7 option A: a venue→adapter registry map. Every existing crypto slug maps to `ccxt` → **current configs byte-identical**. SnapTrade slugs → `snaptrade`. Apply the adapter selector **identically to submit and reconcile** so they can never diverge (existing invariant, `service.py:35-37`). `HERMX_EXEC_BACKEND` still overrides globally. **Audit for the masked-default trap:** a strategy with no explicit venue must not silently resolve to the wrong adapter — this is the exact shadow-config failure class.
2. **`factory.py`.** One-line register inside a `try/except` (mirror the CCXT guard). If the SnapTrade SDK isn't installed, the key is absent and any SnapTrade-routed strategy **fails closed** at `create()` ("Unknown execution exchange") — never guesses a venue.
3. **`snaptrade_adapter.py::execute()`.** Translate `execution_readiness` → `to_order_intent_request` → `client.place_force_order` (`POST /trade/place`). Attach **`client_order_id`** = UUIDv5 of `cl_ord_id` (deterministic → a retry re-derives the same key → brokerage-side dedup; findings §3). Map the response `status`: `PENDING/ACCEPTED/QUEUED` → `mode="submit_enabled"` (journal SUBMITTED, not FILLED), exactly like the CCXT ack path (`service.py:220-228`); a synchronous `EXECUTED` → `mode="filled"`. Record the order-id map `{cl_ord_id ↔ brokerage_order_id, client_order_id}` in the journal detail at submit (TD §8.2). **Never blind-retry a submit** (TD §7.3) — ambiguity → UNKNOWN, let reconcile decide (now backstopped by `client_order_id`).
4. **`webhook_receiver.py` `/snaptrade/webhook`.** Separate handler + separate verification function — **must reject a TradingView HMAC and vice-versa** (TD §6.1). Verify the `Signature` header = base64(HMAC-SHA256(raw body, `SNAPTRADE_CONSUMER_KEY`)) — **no separate webhook secret** (findings §4). Append raw event to `logs/snaptrade-events.jsonl` (fsync, append-only, same discipline as `raw-webhooks.jsonl`) → dispatch by `eventType` (`TRADE_UPDATE`/`TRADE_DETECTION`, `CONNECTION_BROKEN`/`CONNECTION_FAILED`/`CONNECTION_FIXED`, `ACCOUNT_HOLDINGS_UPDATED`). Idempotent: replaying `TRADE_UPDATE` must not double-transition (rely on the journal's `can_transition` guard, `service.py:317`). **At-least-once, no ordering, slow retry (~30-min intervals)** — so the poll backstop, not webhook retry, bounds time-to-terminal. Ack fast (return 2xx); heavy work is a journal-nudge, not an inline broker call.
5. **`strategy.schema.json` (shared — confirm before coding).** Add `equity` to `instrument.type`. Make `leverage`/`margin_mode` conditional: **reject** them when `type == equity` (cash accounts). Keep `no_inline_credentials` unchanged. Decide the slug shape (flat vs `:`-separated) — a flat slug avoids a pattern change. Add/adjust `test_phase6_strategy_schema_v2.py`-style tests.
6. **Reconciliation.** Post-submit poll with backoff (`snaptrade.reconcile_poll_seconds`/`reconcile_max_attempts`); `get_order` → `can_transition(SUBMITTED, state)` → FILLED/REJECTED/UNKNOWN. Webhook is the optimization; **poll is the backstop** — a missed webhook degrades to poll, never to a lost order.
7. **Auth-health gate wiring.** `ConnectionStore.connection_health` feeds the SnapTrade auth-health gate. `connection-broken`/expiry webhook → `mark_unhealthy` → next execute fails closed. Never silently no-op an expired connection.
8. **Timeout budget.** `snaptrade.timeout_seconds` (default 20s) < service `submit_timeout_seconds` (default 45s) so the adapter times out first and returns `submit_timeout` cleanly (TD §7.2).

### Testing checklist

**Unit:**
- `normalizer.to_order_intent_request` reverse mapping + `to_fill_summary` (fee/slippage present and `None`).
- `errors.py` write map: timeout/exception/partial → UNKNOWN; explicit reject → REJECTED. **Assert ambiguity is never REJECTED** (corrupts position math).

**Service-level (real `ExecutionService`, fake client):**
- Kill switch: `HERMX_LIVE_TRADING` off blocks a SnapTrade `live` submit at `live_trading_kill_switch`, identically to CCXT.
- Demo-only enforcement: `live`/non-sandbox submit blocked; paper submit allowed.
- Auth-health: unhealthy connection blocks at `auth_health`.
- Idempotency: duplicate `cl_ord_id` blocks at Gate 6.
- Journal: PLANNED → SUBMITTED → (FILLED|UNKNOWN); ambiguous submit → UNKNOWN not REJECTED.
- `close_only` bypasses kill switch + symbol pause for a SnapTrade close.

**Webhook:**
- Valid SnapTrade signature accepted; TradingView HMAC **rejected** on `/snaptrade/webhook`.
- Idempotent replay of `order-fill` → no double-transition.
- `connection-broken` → health flips → next submit fails closed.
- Out-of-order events (fill before ack) handled.

**Manual (Alpaca paper, real):**
- End-to-end: TradingView paper signal → `SnapTradeExecutor` → Alpaca paper → reconcile via poll **and** webhook.
- **Double-submit test:** deliberately retry a submit; confirm no duplicate order (validates Q1 idempotency + HermX dedupe).
- Async fill: submit, observe SUBMITTED, then FILLED after webhook/poll.
- Connection expiry: force a broken connection; confirm fail-closed + "re-link required" in dashboard.

**Run:** `./.venv/bin/pytest tests/test_snaptrade_*.py -x -q` then full suite.

### Go/no-go to Phase 3 (the pre-condition sign-off — research §10)
1. **Idempotency proven** (Q1): docs confirm SnapTrade honors a client idempotency key (`client_order_id` UUID on `POST /trade/place`, findings §3) — **this go/no-go is now a confirmation, not a discovery.** Prove it end-to-end on Alpaca paper: a deliberate double-submit with the same `client_order_id` yields **exactly one** order, and HermX `cl_ord_id` dedupe + journal independently cover it. **Still the hard blocker for Phase 3** (must pass on paper before real money).
2. **Reconciliation proven:** async fill handled end-to-end via webhook + poll fallback on Alpaca paper; `not_found→pending→terminal` validated.
3. **Connection-health gate:** expiry/broken webhooks fail closed.
4. **Kill-switch coverage:** `HERMX_LIVE_TRADING` + per-strategy pause block SnapTrade identically to CCXT.
5. **Schema extended + validated:** `type: equity`, rejects leverage/margin for cash, demo → paper.
6. **Legal check** initiated (order-routing-on-behalf-of-users obligations at intended scale) — must **clear** before Phase 3 live enable.
7. **Pricing re-verified** at commit time.
- All above signed off in `SNAPTRADE_PAPER_PILOT.md` (new, to be created under `docs/`).

### Rollback
- Runtime: `engine-config.json` `snaptrade.enabled: false` and/or `control-state.json` `snaptrade_enabled: false` → adapter dark, webhook endpoint gated off (`webhook_enabled: false`). SnapTrade strategies fail closed at `create()`.
- Any SnapTrade strategy can be paused individually (`DELETE /api/control/strategy/{id}`) without touching crypto.
- Code: the `service.py` routing change is the only non-additive touch; its map makes every crypto slug → `ccxt`, so reverting is safe and crypto is never affected either way. Revert restores the global selector.

**Effort:** 10–15 developer days.

---

## Phase 3 — Live Trading + Multi-Account Portfolio

**Real money. Gated behind `HERMX_LIVE_TRADING` + explicit per-user consent + legal sign-off.** Enable live for **one broker first**, one account, close monitoring, then widen.

### Scope & goals
- **Live execution:** `execution_mode: live` for a SnapTrade strategy places real orders in the user's real brokerage, gated exactly like CCXT live (`HERMX_LIVE_TRADING` kill switch + per-strategy live pill + auth-health + consent).
- **Multi-account portfolio:** operator dashboard shows every linked brokerage — aggregate exposure, per-account P&L, cross-broker net exposure. Per-user connection management (register + portal) at operator scale.
- **Risk aggregation:** unified net exposure across crypto (CCXT) + equities/options (SnapTrade), driven by the shared normalized shapes.

### Files to create

| Path | Purpose |
|---|---|
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_live_gates.py` | live enable requires `HERMX_LIVE_TRADING`; consent required; per-account isolation |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_multi_account.py` | multi-account aggregation + risk roll-up; per-account P&L correctness |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/MultiBrokerPortfolio.tsx` | account selector + aggregate exposure + per-account P&L |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/RiskAggregation.tsx` | cross-broker net exposure roll-up (crypto + equities) |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_LIVE_RUNBOOK.md` | live ops runbook: re-auth, billing monitoring, incident escalation |

### Files to modify

| Path | Change | Shared? |
|---|---|---|
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/connections.py` | multi-user connection management; consent event logging; `userSecret` rotation + broker re-link without losing the account mapping | no |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/aggregation.py` | per-account tagging, per-account P&L, cross-broker net exposure roll-up | no |
| `/Users/anatolizurablev/dev projects/hermx/src/dashboard.py` | multi-account API: account selector, per-account P&L, risk aggregation endpoints | no |
| `/Users/anatolizurablev/dev projects/hermx/src/security/credentials.py` | (if needed) live-mode credential selection hardening | **yes — confirm** |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_RUNBOOK.md` | expand to full operational runbook | no |

### HermX-specific implementation checklist
1. **Live gate parity.** A SnapTrade `live` submit passes through `live_trading_kill_switch` (needs `HERMX_LIVE_TRADING`), `sandbox_only`/`live_sandbox_consistency`, symbol pause, idempotency — **identical ladder to CCXT**. `close_only` bypasses kill switch + symbol pause (reduces exposure) exactly as CCXT.
2. **Consent enforcement.** Live execution requires explicit per-user consent captured in the connection flow and logged as a consent event in a ledger (TD §6.5). No consent → fail closed.
3. **One-broker-first rollout.** Enable live for a single broker/account, monitor, then widen. Keep the venue→adapter map + per-strategy pill as the blast-radius control.
4. **Risk aggregation** reuses the shared normalized shapes — group-by-broker, aggregate net exposure row spanning crypto + equities. No new shape.
5. **Billing monitoring.** Per-connected-user cost scales here (research §7). Add a dashboard count of connected users and a threshold alert before the free-tier / enterprise crossover.
6. **Multi-account isolation.** A broken/expired connection on one account fails closed for **that account only**; other accounts and the crypto path are unaffected.

### Testing checklist
**Unit/service:**
- Live submit blocked without `HERMX_LIVE_TRADING`; allowed with it + consent.
- Per-account isolation: unhealthy account A does not block account B or crypto.
- Multi-account aggregation + per-account P&L correctness against fake multi-account client.
- Consent-missing → fail closed.

**Manual (staged, real money, tiny size):**
- Single live order in one real broker, smallest legal size; verify fill + reconcile + journal terminal state.
- Kill switch: flip `HERMX_LIVE_TRADING` off mid-session → live submits blocked immediately.
- Re-auth flow: force a live connection expiry → fail closed → operator re-links → resumes.
- Billing: confirm connected-user count + alert threshold fire.

**Run:** full suite `./.venv/bin/pytest tests/ -x -q` + SnapTrade suite; **never** ship Phase 3 on a red suite.

### Go/no-go (to declare "live, production")
- All Phase 2 pre-conditions signed off; legal cleared; pricing re-verified.
- Staged single-broker live order filled + reconciled correctly with real money at minimal size.
- Kill switch + per-strategy pause + per-account isolation all proven live.
- Billing alerting live.

### Rollback
- **Fastest:** `HERMX_LIVE_TRADING` off → all live submits (crypto + SnapTrade) blocked; existing positions untouched. This is the existing emergency-stop lever — SnapTrade inherits it.
- SnapTrade-only: `control-state.json` `snaptrade_enabled: false` or pause the specific SnapTrade strategies → crypto keeps trading.
- Per-account: revoke a single connection in `ConnectionStore` → that account's strategies fail closed; nothing else affected.
- Code: revert to Phase 2 (paper-only) by defaulting `snaptrade.enabled: false` — no schema/journal migration to unwind (additive throughout).

**Effort:** 12–18 developer days (excl. legal lead time, which runs in parallel and can dominate wall-clock).

---

## 3. Combined Implementation Checklist (all phases, file by file)

Phase column: **P1** = read-only, **P2** = paper, **P3** = live/multi-account.

| File (absolute) | New/Mod | Phase | Shared? | Notes |
|---|---|---|---|---|
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/__init__.py` | New | P1 | no | package surface exports |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/errors.py` | New→ext | P1→P2 | no | read classes P1; full write→mode map P2 (TD §7.1) |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/client.py` | New→ext | P1→P2 | no | read methods P1; `place_force_order`/impact P2. Pin `snaptrade-python-sdk==11.0.212` (Q7) |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/auth.py` | New | P1 | no | partner creds + register + portal URL |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/connections.py` | New→ext | P1→P3 | no | store P1; multi-user/consent/rotation P3. Encrypted, not git-tracked |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/normalizer.py` | New→ext | P1→P2 | no | read maps P1; `to_order_intent_request`/`to_fill_summary` P2. **Pure — top test target** |
| `/Users/anatolizurablev/dev projects/hermx/src/snaptrade/aggregation.py` | New→ext | P1→P3 | no | single-account P1; multi-account risk roll-up P3 |
| `/Users/anatolizurablev/dev projects/hermx/src/executors/snaptrade_adapter.py` | New→ext | P1→P2 | no | observe-only+health P1; real `execute()`/`plan()`+order-id map P2 |
| `/Users/anatolizurablev/dev projects/hermx/src/executors/factory.py` | Mod | P2 | low | one-line register + try/except guard (TD §2.8) |
| `/Users/anatolizurablev/dev projects/hermx/src/execution/service.py` | Mod | P2 | **yes** | `resolve_execution_config` per-strategy adapter routing (TD §2.7) — **confirm** |
| `/Users/anatolizurablev/dev projects/hermx/src/security/credentials.py` | Mod | P1(,P3) | **yes** | `snaptrade` branch + redaction keys — **confirm**; fail-closed on partial set |
| `/Users/anatolizurablev/dev projects/hermx/src/webhook_receiver.py` | Mod | P2 | low | `/snaptrade/webhook` separate-auth endpoint (TD §2.6) |
| `/Users/anatolizurablev/dev projects/hermx/src/dashboard.py` | Mod | P1,P2,P3 | no | external_brokers P1; controls P2; multi-account P3 |
| `/Users/anatolizurablev/dev projects/hermx/schemas/strategy.schema.json` | Mod | P2 | **yes** | `type: equity`, conditional leverage/margin — **confirm** |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/ExternalBrokersPanel.tsx` | New | P1 | no | connection status + holdings |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/MultiBrokerPortfolio.tsx` | New | P3 | no | account selector + per-account P&L |
| `/Users/anatolizurablev/dev projects/hermx/dashboard-ui/components/RiskAggregation.tsx` | New | P3 | no | cross-broker net exposure |
| `/Users/anatolizurablev/dev projects/hermx/config/runtime.demo.json` (+ variants) | Mod | P1 | low | `snaptrade` section, `enabled: false` (TD §5.2) |
| `/Users/anatolizurablev/dev projects/hermx/Dockerfile` | Mod | P1 | low | copy `src/snaptrade/`; pin `snaptrade-python-sdk==11.0.212`; bake rebuilt `dashboard-ui/out` |
| `/Users/anatolizurablev/dev projects/hermx/docker-compose.yml` | Mod | P1,P2 | low | `hermx-state:/app/data` rw (confirm); `SNAPTRADE_*` env; webhook port |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_normalizer.py` | New | P1 | — | table-driven, fixtures |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_client.py` | New | P1 | — | fake client + redaction |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_connections.py` | New | P1 | — | encryption + health |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_aggregation.py` | New | P1 | — | failure = unavailable |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_dashboard.py` | New | P1 | — | `/api` external_brokers |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_executor_paper.py` | New | P2 | — | write path |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_service_gates.py` | New | P2 | — | real ExecutionService gate parity |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_webhook.py` | New | P2 | — | auth-plane separation + idempotency |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_reconciliation.py` | New | P2 | — | async fill state machine |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_live_gates.py` | New | P3 | — | live kill-switch + consent |
| `/Users/anatolizurablev/dev projects/hermx/tests/test_snaptrade_multi_account.py` | New | P3 | — | multi-account risk roll-up |
| `/Users/anatolizurablev/dev projects/hermx/tests/fixtures/snaptrade/` | New | P0 | — | recorded real payloads |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_PHASE0_FINDINGS.md` | New | P0 | — | answered open questions |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_RUNBOOK.md` | New→ext | P1→P3 | — | operator runbook |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_PAPER_PILOT.md` | New | P2 | — | pre-condition sign-off (Phase-3 gate) |
| `/Users/anatolizurablev/dev projects/hermx/docs/SNAPTRADE_LIVE_RUNBOOK.md` | New | P3 | — | live ops + billing |

**Slicing note:** the shared-code rule (>3 files → slice) is satisfied because each phase is itself sliced, and within a phase the three shared touches (`service.py`, `credentials.py`, `strategy.schema.json`) land as **separate, individually-confirmed commits**, not one mega-diff.

---

## 4. Testing Strategy (cross-cutting)

### Unit test patterns
- **Normalizer = pure functions = highest-value target.** Table-driven: recorded SnapTrade payload in → expected canonical shape out. No network. Cover every order state, long/short positions, cash vs margin balances, `fee_usd`/`slippage_pct` present-and-`None`, and the reverse `to_order_intent_request`. Fixtures come from Phase 0.
- **`FakeSnapTradeClient`** — same method surface as `client.py`, returns canned raw payloads including error/timeout/429/partial cases. Tests exercise the **real** `SnapTradeExecutor` + **real** `normalizer.py`, mocking only the transport (mirrors how CCXT adapter tests mock the exchange, not the adapter). **Do not re-implement handlers inline** (anti-pattern).
- **`errors.py`** — each failure class → correct HermX mode; assert ambiguity → UNKNOWN, explicit reject → REJECTED.
- **`credentials.py` snaptrade branch** — fail-closed on partial set; demo/live selection; redaction.

### Integration tests
- **Fake server / recorded fixtures + env-var setup** (`SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY`, `SNAPTRADE_ENCRYPTION_KEY` as test values via `conftest.py`; webhook-verify tests sign with `SNAPTRADE_CONSUMER_KEY` — no separate webhook secret).
- Route a SnapTrade strategy through the **real `ExecutionService`** and assert full gate-ladder parity with CCXT (kill switch, auth-health, idempotency, journal transitions, close_only bypass).
- Webhook handler: signature accepted/rejected, idempotent replay, connection-broken fail-closed, out-of-order events.

### Manual testing checklist — real endpoint targets
- **Connection flow (SnapTrade sandbox — read-only, ideal here):** `registerUser` → `login`→`redirectURI` → connect the simulated broker (scenarios: self-directed / cash-only / invalid-creds / rate-limited) → `GET /accounts`, `/positions`, `/balances`, `/orders` render. Sandbox exercises the error scenarios (broken-connection, rate-limited) with zero real linking.
- **Order placement (Alpaca Paper — sandbox CANNOT place orders):** signal → `POST /trade/place` (with `client_order_id`) → SUBMITTED → reconcile (`GET /orders`) → FILLED; **double-submit the same `client_order_id` → exactly one order** (validates findings §3 + HermX dedupe).
- **Reconciliation:** poll path (`GET /accounts/{id}/orders`) and webhook path (`TRADE_UPDATE`) both drive terminal state; kill the webhook → poll backstop still resolves (webhook retry is ~30-min cadence, so poll is primary).
- **Connection expiry:** trigger `CONNECTION_BROKEN` → fail closed → re-link via `login?reconnect=` → `CONNECTION_FIXED` → resume.
- **Blast radius:** SnapTrade outage → crypto path unaffected.
- **Webhook signature:** confirm a real body verifies against HMAC-SHA256(body, `SNAPTRADE_CONSUMER_KEY`); a wrong key / TradingView HMAC is rejected.

### How to run
```
# SnapTrade-scoped, fail fast, quiet
./.venv/bin/pytest tests/test_snaptrade_*.py -x -q

# Full suite before every commit
./.venv/bin/pytest tests/ -x -q
```

---

## 5. Deployment & Rollout

### Feature flags (two layers — do not conflate)
- **Build-time:** `engine-config.json` → `snaptrade.enabled` (baked from `config/runtime.*.demo.json`), plus `webhook_enabled`, `venue_adapter_map`, timeouts (TD §5.2). Default `enabled: false` → SnapTrade dark in the image.
- **Runtime operator toggle:** `snaptrade_enabled` in `control-state.json`, **same pattern as `symbol_pauses` and strategy mode overrides**. This is the lever an operator flips without a redeploy. Default off.
- **Per-strategy:** existing pause/demo/live pill applies unchanged — a SnapTrade strategy is armed/paused exactly like a CCXT one.
- **Global kill switch:** `HERMX_LIVE_TRADING` gates all live submits (crypto + SnapTrade) — unchanged, inherited.

### Env / secrets (TD §5.1)
`SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY`, `SNAPTRADE_ENCRYPTION_KEY`, `SNAPTRADE_ENV`. **No `SNAPTRADE_WEBHOOK_SECRET`** — webhook verification reuses `SNAPTRADE_CONSUMER_KEY` (findings §4). `SNAPTRADE_ENV` selects sandbox vs production by **key type** (sandbox = non-prod keys, read-only). From system env / secrets manager — **never in `mcp_config.json`, never committed, never in strategy files** (`dev-rules.md` #10). Added to `resolve_exchange_credentials()` and the `redact_secrets` set.

### Docker
- Multi-stage build unchanged in shape: Node builds `dashboard-ui/out` (now including `ExternalBrokersPanel`), Python stage bakes it. Ensure the Dockerfile `COPY`s `dashboard-ui/out` (known pattern: omit it → dashboard silently falls back to legacy HTML).
- Pin **`snaptrade-python-sdk==11.0.212`** in the Python deps layer (OpenAPI-codegen'd → version-churny; bump deliberately). The SDK handles request signing.
- `docker-compose.yml`: `hermx-state:/app/data` mounted **rw** even under `read_only: true` (connection store + control-state both write there — known pattern, silent write failure otherwise). Named volumes `hermx-data`, `hermx-state` unchanged.
- Expose the webhook port for `/snaptrade/webhook` if not already public.

### Migration steps
- **Phase 1: none — additive only.** No schema/journal migration. Enabling is a flag flip.
- **Phase 2:** strategy schema gains `type: equity` (backward-compatible — existing crypto strategies validate unchanged); `resolve_execution_config` map makes every existing crypto slug → `ccxt` (configs byte-identical).
- **Phase 3:** none beyond flag + consent capture.
- **Config-file conflict caution:** operator-edited tracked config (`engine-config.json`, `strategies/*.json`) still conflicts on `git pull` regardless of `.gitignore` (known pattern — `.gitignore` is inert for tracked files). Document the `git rm --cached` remedy in the runbook if operators edit the new `snaptrade` section.

### Monitoring
- **New dashboard panels:** External Brokers (P1), Multi-Broker Portfolio + Risk Aggregation (P3), connection-status/health per broker.
- **Alerts (reuse the Hermes cron gate discipline — proportionate, fingerprint + suppression window):**
  - Connection-broken / expired → operator alert ("re-link required").
  - Reconciliation mismatch → `emit_reconcile_alert` (existing).
  - Connected-user count approaching a billing threshold (P3).
  - Zero-fill / stuck-SUBMITTED absence gate (an order SUBMITTED but never reaching terminal within N reconcile attempts) — reuse the absence-detection pattern (rolling window count → synthetic condition → existing `run_gate()`; no new gate-lib primitive needed).
- **Do not add an inert monitor** (known pattern): a gate keyed on a nonexistent flag is false reassurance. Every SnapTrade monitor must gate on a real, implemented flag or not exist.

---

## 6. Risk Register

| Phase | Risk | Sev | Mitigation | Escalation |
|---|---|---|---|---|
| P1 | Aggregation exposes holdings in ledgers (privacy) | 🟠 | Default read-only holdings to memory/cache, not JSONL (TD §6.5, §8.3) | Eng lead → data-handling review |
| P1 | Masked-default regression from new config (shadow-config class) | 🟠 | Audit every consumer of the new slug/map; backend-vs-venue confusion test | Eng lead |
| P1 | Connection store write silently fails (missing rw mount) | 🟡 | Compose `hermx-state:/app/data` rw asserted in `test_docker_state.py`-style test | Ops |
| P2 | **Double-submit real money** (idempotency now doc-confirmed: `client_order_id` UUID, findings §3) | 🟠 (was 🔴) | Confirm on paper (double-submit same `client_order_id` → one order); never blind-retry writes; HermX `cl_ord_id`+journal backstop **in addition** to source-level key | Eng lead → **halt Phase 3** if paper test fails |
| P2 | Async fill / reconciliation gap (missed webhook) | 🟠 | Poll backstop; `not_found→pending→terminal`; `can_transition` guard | On-call → reconcile alert |
| P2 | Webhook auth-plane confusion (TV HMAC ↔ SnapTrade `consumerKey`-HMAC) | 🟠 | Separate handler/verifier; test rejects cross-plane auth. Note: SnapTrade webhook reuses `SNAPTRADE_CONSUMER_KEY` (no separate secret) → treat `consumerKey` as highest-value secret | Security review |
| P2/3 | Dependency risk (16-person vendor in money path) | 🔴 | CCXT stays primary for crypto (zero SnapTrade dep); adapter fails closed for its venues only | Eng lead → emergency stop |
| P3 | Session expiry / re-auth churn | 🟠 | Auth-health gate fails closed; expiry webhook; "re-link required" UI | Operator re-link |
| P3 | Regulatory framing (routing on behalf of users) | 🟠 | **Legal review before live at scale** (research §9) | **Legal — hard gate** |
| P3 | Per-user pricing scaling | 🟡 | Billing count + threshold alert; model unit economics before onboarding many users | Ops → finance |
| P3 | Live blast radius on a bad order | 🔴 | `HERMX_LIVE_TRADING` global kill; per-strategy pause; per-account isolation; one-broker-first | On-call → **kill switch** |

**Escalation path (general):** on-call operator → engineering lead → (for money-loss or regulatory) halt via `HERMX_LIVE_TRADING` off + pause SnapTrade strategies → post-incident review. The kill switch is the always-available lever; use it first, diagnose second.

---

## 7. Success Criteria

**Phase 1 (aggregation):**
- Real linked-account holdings/balances render in the dashboard alongside crypto, correctly mapped.
- Connection-broken renders as an unambiguous fail-closed state.
- SnapTrade outage is provably cosmetic (crypto unaffected).
- Full test suite green; zero write path exercised.

**Phase 2 (paper):**
- End-to-end paper signal → submit → reconcile → correct terminal journal state, via both poll and webhook.
- Double-submit test produces exactly one order.
- Every gate blocks a SnapTrade strategy identically to CCXT (proven in `test_snaptrade_service_gates.py`).
- All seven Phase-3 pre-conditions signed off.

**Phase 3 (live + multi-account):**
- Staged single-broker live order fills + reconciles correctly at minimal size.
- Kill switch + per-strategy pause + per-account isolation proven live.
- Multi-broker portfolio + risk aggregation renders correct per-account P&L and net exposure.
- Billing alerting live; legal cleared.

**Metrics to watch (all phases):**
- Reconciliation mismatch rate (should trend to ~0).
- Stuck-SUBMITTED count / time-to-terminal (absence gate).
- Connection-broken frequency (re-auth churn).
- SUBMITTED→REJECTED vs SUBMITTED→UNKNOWN ratio (ambiguity should be UNKNOWN, not REJECTED).
- Connected-user count vs billing tier (P3).

---

## 8. Post-Launch

### Operational runbook (`SNAPTRADE_RUNBOOK.md` → `SNAPTRADE_LIVE_RUNBOOK.md`, both new under `docs/`)
- **Connection expiry / re-auth:** how to spot "re-link required", regenerate the portal URL, re-link without losing the account→strategy mapping, verify health flips back.
- **`userSecret` rotation:** rotate + re-encrypt in place; verify no downtime.
- **Revoke:** delete a connection → the routed strategy fails closed; confirm crypto unaffected.
- **Billing monitoring:** connected-user count, free-tier (5) and enterprise-floor crossover (~667 users), threshold alert response.
- **Consumer-key rotation:** webhook verification and API signing both use `SNAPTRADE_CONSUMER_KEY` (findings §4), so rotating it must be coordinated across both planes (dual-accept window on the webhook verifier) to avoid dropping events. There is no independent webhook secret to rotate.
- **Emergency stop:** `HERMX_LIVE_TRADING` off + pause SnapTrade strategies; verify.
- **Config edits & `git pull`:** the `git rm --cached` remedy for operator-edited tracked config (`.gitignore` is inert for tracked files).

### Documentation updates
- Update `CLAUDE.md` / `.claude/CLAUDE.md` Key Files to list `src/snaptrade/` and `snaptrade_adapter.py`.
- Add SnapTrade venues to the slash-command reference (`skills/hermx-help/SKILL.md`) where operator commands (`/hx-positions`, `/hx-close`) now span brokers.
- Record any new proven pattern in **both** `.claude/rules/code-quality.md` and `.windsurf/rules/code-quality.md` (dual-file rule) — e.g. the backend(`snaptrade`)-vs-venue(`snaptrade:alpaca:acct`) distinction, which is exactly the class of bug the shadow-config regression taught us to guard.
- Close out `SNAPTRADE_PHASE0_FINDINGS.md` open questions as each is answered; fold Q10 (regulatory) and Q11 (options, deferred) into a future-work note.

---

## Appendix — Edge cases to cover in tests (per `dev-rules.md` #5)

- Time-less / malformed payloads from SnapTrade → normalizer returns a safe shape, never crashes (mirrors the `normalize()` non-determinism lesson — never re-derive an id from wall-clock).
- Order status SnapTrade doesn't document → maps to `unknown`/`not_found`, not an exception.
- Partial multi-step submit (if two-step per Q2) → `submit_partial` → UNKNOWN + reconcile alert, never REJECTED.
- 429 on a read → backoff; 429 on a write → **defer, do not double-submit**.
- Webhook replay, out-of-order (fill before ack), and cross-plane auth (TV HMAC on `/snaptrade/webhook`) → all rejected/idempotent.
- Connection store write with a read-only mount → surfaces loudly, not a silent no-op.
- SnapTrade config present but `enabled: false` → adapter fully dark; strategy routed to it fails closed at `create()`, never guesses a venue.
- Empty/missing SnapTrade config → no masked default to a crypto venue (the shadow-config trap).
