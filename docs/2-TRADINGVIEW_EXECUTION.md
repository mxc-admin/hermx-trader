# TradingView → HermX Execution

End-to-end reference: how a TradingView alert becomes an exchange order. Companion to
[3-TRADINGVIEW_ALERTS.md](3-TRADINGVIEW_ALERTS.md) (the payload contract, field-by-field) and
[6-CCXT_EXCH_ADAPTER.md](6-CCXT_EXCH_ADAPTER.md) (the adapter below the execution service).

## 1. Overview

A TradingView alert fires a webhook `POST /webhook` at the HermX receiver
(`src/webhook_receiver.py`). The receiver authenticates the request, fsyncs the raw payload
to the `raw-webhooks.jsonl` write-ahead log, and acknowledges `200 {"status":"queued"}`.
A worker then normalizes the payload, validates it (schema + strategy match), deduplicates it
against `signals.jsonl`, and — when a strategy file matches — builds an execution-readiness
block and hands it to `ExecutionService` (`src/execution/service.py`), which runs the money-safety
gates (kill switch, notional cap, trading state, idempotency), journals the order, and dispatches
it through the CCXT adapter to the venue selected by the strategy file.

```
TradingView alert
  → POST /webhook (auth, WAL append, queue)          src/webhook_receiver.py:do_POST
    → worker: normalize → validate → dedupe          build_record / signals/{normalize,dedupe}.py
      → readiness block (venue, sizing, cl_ord_ids)  src/strategy/readiness.py
        → ExecutionService.execute (gates, journal)  src/execution/service.py
          → CcxtExecutor → venue                     src/executors/ccxt_adapter.py
```

## 2. Alert Definition

Validated against `schemas/tradingview-alert.schema.json`. See
[3-TRADINGVIEW_ALERTS.md](3-TRADINGVIEW_ALERTS.md) for full semantics; summary:

**Required** (schema `required`):

| Field | Type | Notes |
|---|---|---|
| `strategy_id` | string | `^[a-z0-9]+(?:_[a-z0-9]+)*$`; must match a file in `strategies/` |
| `symbol` | string | e.g. `SOLUSDT`; receiver uppercases and strips `OKX:` / `-` / `/` |
| `timeframe` | string | enum `30m` `1h` `2h` `3h` `4h`; must match the strategy file |
| `tv_signal_price` | number or string | use `{{close}}` |
| `tv_time` | string or number | use `{{time}}` |
| `source` | string | must be `"tradingview"` |

**Direction** (schema `anyOf` — at least one required):

| Field | Type | Notes |
|---|---|---|
| `action` | string | enum `buy` `sell` `close`; primary field. `close` flattens reduce-only |
| `side` | string | enum `buy` `sell`; legacy. If both present as `buy`/`sell`, they must match |

**Optional**: `strategy_name`, `indicator`, `signal_id` (receiver derives one when absent),
`extras` (object, observe-only debug context — see 3-TRADINGVIEW_ALERTS.md).

There is **no `exchange` field** in the alert schema. Venue routing comes exclusively from
the strategy file (`strategy.instrument.exchange`, § 4). `normalize()` backfills an
informational `exchange: "okx"` into the normalized record when absent
(`src/signals/normalize.py:96`), but it never routes an order.

Example alert message (TradingView placeholders substituted at fire time):

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "{{ticker}}",
  "timeframe": "3h",
  "side": "{{strategy.order.action}}",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

Keep `timeframe` hard-coded (not `{{interval}}`) so an alert placed on the wrong chart is
quarantined as `strategy_timeframe_mismatch` instead of silently accepted.

## 3. Intake Process

### Stage 1 — Transport (synchronous, `do_POST`)

These run in order; the HTTP status is returned directly to TradingView:

1. **Path**: only `/webhook` (and its alias `/shadow/webhook`) accept alerts → otherwise `404 not_found`.
2. **Content-Length**: unparseable/negative → `400 invalid_content_length`; body over
   `HERMX_MAX_BODY_BYTES` (262144) → `413 payload_too_large`.
3. **Rate limit**: sliding window per client IP (120 requests / 60 s) → `429 rate_limited`.
4. **JSON parse**: malformed body → `400 invalid_json`. (Parsing happens before auth; the
   raw bytes are also needed for HMAC verification.)
5. **Auth** (`authenticate_webhook_request`, `src/security/webhook_auth.py`):
   - `X-Webhook-Secret` header must equal `HERMX_SECRET` (constant-time compare). A
     missing/blank server-side secret fails closed: every request gets
     `401 missing_webhook_secret`. A mismatch → `401 forbidden`.
   - When `HERMX_REQUIRE_HMAC=true`: `X-Webhook-Timestamp` + `X-Webhook-Signature` must carry
     a hex HMAC-SHA256 of `timestamp || body` keyed by `HERMX_WEBHOOK_HMAC_KEY` (an optional
     `sha256=` prefix is accepted), with the timestamp inside a 300 s replay window
     (`HERMX_REPLAY_WINDOW_SECONDS`). Failures → `401` with `hmac_header_missing`,
     `hmac_timestamp_invalid`, `hmac_replay_window`, or `hmac_mismatch`.
6. **WAL append**: the raw payload is written to `logs/raw-webhooks.jsonl` with
   `phase: "intake"` **before** queueing — this file, not the in-memory queue, is the
   recovery source (startup replay re-queues accepted-but-unprocessed rows).
7. **Queue**: put on `PROCESS_QUEUE` (maxsize 200). Full → `503 queue_full`, with a
   `phase: "dropped"` WAL marker so replay never resurrects the dropped row.
8. Success → `200 {"ok": true, "status": "queued", "received_at": ..., "queue_depth": ...}`.

### Stage 2 — Intake / semantic (asynchronous, `build_record`)

A worker dequeues and processes; outcomes are written to `logs/pipeline.jsonl`
(stages `error`, `quarantine`, `dedup_reject`, `strategy_match`, `decision`) — **never returned
to the caller**. The statuses below are the `status` field in those rows. Order:

1. **`normalize()`** (`src/signals/normalize.py`): lowercases `action`/`side`, uppercases
   `symbol` and strips `OKX:` / `-` / `/`, canonicalizes `timeframe`, backfills a missing
   `tv_time` with server time, and derives `signal_id =
   sha256(strategy_id|symbol|action|timeframe|tv_time)` when the alert didn't send one.
2. **Conflict gate**: `action` and `side` both present as opposing open sides → status 400
   `action_side_conflict`.
3. **Close branch**: `action=close` diverts to the reduce-only close path *before* the side
   gate (a close carries no side). It flows through the operator-close machinery with
   `close_only=True` (§ 4).
4. **Side gate**: `side` not `buy`/`sell` after lowercasing → status 400 `side_not_allowed`.
5. **Source gate**: `source != "tradingview"` → status 202 `non_tradingview_source`
   (acknowledged, never processed).
6. **Schema validation** (`validate_alert_schema` against
   `schemas/tradingview-alert.schema.json`): observe-only by default (logged + counted, still
   processed). With the runtime-config key `strategy_engine.enforce_alert_schema` set true
   (default false), an invalid alert is quarantined with status 202, reason
   `alert_schema_invalid:<field>`.
7. **Strategy match** (`validate_strategy_alert`): missing `strategy_id` (for a recognized
   strategy alert), unknown `strategy_id`, `strategy_symbol_mismatch`, or
   `strategy_timeframe_mismatch` → status 202, quarantined. A non-strategy alert whose symbol
   no loaded strategy trades → status 400 `symbol_not_allowed`.
8. **Dedupe** (`check_and_mark_signal`, `src/signals/dedupe.py`): duplicate by `signal_id` or
   by the composite key `strategy_id|symbol|side|timeframe|tv_time` within a 24 h window →
   recorded as `dedup_reject`, **not executed**. A first-seen signal is appended to
   `logs/signals.jsonl` — the single dedup authority and the hard backstop against
   double-execution on replay.
9. A strategy-matched, non-duplicate alert gets an execution-readiness block and is handed to
   the execution path (`execute_with_advisor` → `execute_if_enabled` → `ExecutionService`).
   With no strategy match the alert is recorded observe-only and never executed.

## 4. Processing / Execution

### Venue routing and sizing come from the strategy file

`build_strategy_execution_readiness` (`src/strategy/readiness.py`) reads the matched strategy
file, e.g. `strategies/solusdt_duo_base_dev_3h.json`:

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "instrument": { "exchange": "okx", "inst_id": "SOL-USDT-SWAP", "type": "swap" },
  "capital": { "budget_usd": 1500, "reinvest": true },
  "execution_mode": "demo",
  "leverage": 2,
  "margin_mode": "isolated"
}
```

- **Venue**: `strategy.instrument.exchange` flows into the readiness block and
  `resolve_execution_config` (`src/execution/service.py:53`) sets it as the active CCXT venue
  (`execution.ccxt_exchange`); `instrument.type` sets `ccxt_default_type`. The alert payload
  never selects a venue.
- **Mode**: `execution_mode` (`demo` | `live`) — `demo` routes to the venue sandbox
  (`simulated_trading`), `live` to the real account. Per-strategy dashboard overrides in
  `control-state.json` are applied live per signal.
- **Sizing**: planned notional = sizing budget × `leverage`, where the sizing budget is
  `capital.budget_usd`, or (with `reinvest: true`) equity = seed + realized net P&L from the
  closed-trade ledger.
- **Client order ids**: two deterministic ids per signal —
  `stable_client_order_id(identity, role)` = `"mxc" + sha256(identity|role)` truncated to 32
  chars, one for the close leg (flatten the opposite position) and one for the open leg.

### ExecutionService gates (in precedence order)

`ExecutionService.execute` (`src/execution/service.py:115`) refuses to submit — recording
which gate fired — unless every gate passes:

1. **Arming + health** — strategy `submit_orders` flag (Pause sets it false), webhook auth
   config healthy, liveness watchdog not paused.
2. **Canonical mode** — `execution_mode` must be `demo` or `live`; anything else is a config
   typo and fails closed.
3. **Live kill switch** — any submit that resolves to a real venue requires
   `HERMX_LIVE_TRADING=true`. **A `close_only` record bypasses this gate** (and the symbol
   pause): a close only reduces exposure, and HermX never blocks a close. Live/sandbox
   consistency is also enforced here (no live/sim mixing).
4. **Pre-trade notional cap** (`_check_pretrade_risk`) — planned notional must not exceed
   `min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD)`. Both are operator-set
   *absolute* values, deliberately independent of `budget_usd × leverage` (a fat-fingered
   budget would raise a derived ceiling along with the notional). Unset → no cap.
5. **Global `trading_state`** — `active` (normal) or `reducing` (risk-off, set via
   `control-state.json`). In `reducing`, every non-close order is blocked
   (`trading_state_reducing:reversal_blocked`); `close_only` records always pass.
6. **Equity stop** — a reinvest-sized strategy whose equity is depleted to ≤ 0 cannot open
   new risk; closes always pass, and unknown equity fails safe (never blocks).
7. **Idempotency** — a `cl_ord_id` already present in the order journal is refused
   (`duplicate_cl_ord_id`).

### Submit

Past the gates, the service write-ahead journals the order (`PLANNED` → `SUBMITTED`), then —
before the order reaches the venue — records the **submit-time attribution map**
(`pnl_strategy_map.record_submit_strategy`): `{cl_ord_id → strategy_id, venue, mode}` for both
legs. Exchange order-history rows carry no `strategy_id` and the hashed cl_ord_id is not
invertible, so this map is the only way a later reconciled close attributes P&L to the right
strategy. The map write is best-effort and can never block the trade.

The order is then dispatched: `executor_factory.create(resolved_config)` → `CcxtExecutor.execute(readiness)`
(see [6-CCXT_EXCH_ADAPTER.md](6-CCXT_EXCH_ADAPTER.md)). An adapter ACK leaves the order
`SUBMITTED`; only a confirmed fill is `FILLED`; timeouts/exceptions/partial multi-leg submits
go to `UNKNOWN` (never a fabricated `REJECTED`), and post-submit reconciliation resolves the
terminal state against the venue.

## 5. Setting Up Alerts on TradingView (Practical Guide)

1. **Open the chart** for the strategy's symbol and timeframe (e.g. SOLUSDT 3H for
   `solusdt_duo_base_dev_3h`). The chart timeframe must match the strategy file's
   `timeframe`, or every alert quarantines as `strategy_timeframe_mismatch`.
2. **Create the alert**: Condition = the indicator/strategy BUY or SELL signal;
   Trigger = **Once per bar close** (intrabar triggers fire on unconfirmed signals and
   spam the dedup window); Expiration = open-ended or the longest available.
3. **Enable the Webhook URL** notification and set it to the system webhook URL — the path
   is `/webhook` (e.g. `https://<your-host>/webhook`).
4. **Set the secret header**: add the `X-Webhook-Secret` header with the `HERMX_SECRET`
   value in the alert's webhook settings. Never put the secret in the message body or the
   URL. (If the deployment runs with `HERMX_REQUIRE_HMAC=true`, requests must also carry
   `X-Webhook-Timestamp`/`X-Webhook-Signature` — TradingView cannot compute a per-request
   HMAC, so that mode requires a signing proxy in front of the receiver.)
5. **Paste the message JSON** (see § 2, or the ready-made per-strategy templates in
   [3-TRADINGVIEW_ALERTS.md](3-TRADINGVIEW_ALERTS.md) § *TradingView Message Templates*). Placeholders:
   `{{ticker}}` → `symbol`, `{{strategy.order.action}}` → `side` (emits `buy`/`sell`),
   `{{close}}` → `tv_signal_price`, `{{time}}` → `tv_time`. Hard-code `strategy_id`,
   `timeframe`, and `source`.
6. **Test with curl before going live**:

   ```bash
   curl -sS -X POST "https://<your-host>/webhook" \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Secret: $HERMX_SECRET" \
     -d '{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","side":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}'
   ```

   Expect `200 {"ok": true, "status": "queued", ...}`, then check `logs/pipeline.jsonl` for
   the async outcome (`strategy_match`/`decision`, or a `quarantine` reason). Re-sending the
   same body within 24 h is a `dedup_reject` — change `tv_time` to test again.

**Common pitfalls**

- `side` values other than `buy`/`sell` (e.g. `long`/`short`) → `side_not_allowed`. Casing
  is fine (the receiver lowercases), the *value* is not.
- Both `action` and `side` present but disagreeing → `action_side_conflict` (400).
- Alert placed on the wrong chart/timeframe → `strategy_symbol_mismatch` /
  `strategy_timeframe_mismatch` quarantine.
- `timeframe` outside the schema enum (`30m 1h 2h 3h 4h`) fails schema validation.
- Missing `X-Webhook-Secret` header → `401 forbidden`; TradingView shows the webhook as
  failing but gives no detail — verify the header in the alert's webhook settings.
- TradingView webhooks require a paid plan, do not retry failed deliveries, and silently
  stop at alert expiration — set the longest expiration and monitor for signal absence.
- Using `{{interval}}` for `timeframe` — hard-code it instead (§ 2).

## 6. Troubleshooting

| Symptom | Likely cause | Where to check |
|---|---|---|
| TradingView marks webhook failed, nothing in logs | Wrong URL/path (must be `/webhook`), receiver down, or TLS issue | Receiver process status; `curl` test from § 5 |
| `401 forbidden` / `missing_webhook_secret` | `X-Webhook-Secret` missing/wrong, or server `HERMX_SECRET` unset (fails closed) | Alert webhook settings; receiver env |
| `401 hmac_*` | `HERMX_REQUIRE_HMAC=true` without a signing proxy, clock skew past the 300 s replay window, wrong `HERMX_WEBHOOK_HMAC_KEY` | Proxy signing logic; server clock |
| `400 invalid_json` | Malformed message body (trailing comma, unquoted placeholder) | Alert message box; the exact body in the 400 `detail` |
| `200 queued` but no order | Rejected/quarantined asynchronously, or no strategy matched | `logs/pipeline.jsonl` (`quarantine`/`error` stage, `reason` field) |
| `quarantine: unknown_strategy_id` | `strategy_id` has no file in `strategies/` | `strategies/` directory |
| `quarantine: strategy_symbol_mismatch` / `strategy_timeframe_mismatch` | Alert on the wrong chart or wrong hard-coded `timeframe` | Alert vs strategy file |
| `dedup_reject` | Same `signal_id` or same `strategy_id\|symbol\|side\|timeframe\|tv_time` within 24 h | `logs/signals.jsonl` (`first_seen_at`) |
| Signal processed but `not_submitted` | An ExecutionService gate fired — the `gate` field says which (kill switch, `pretrade_notional`, `trading_state`, `symbol_pause`, `idempotency`, `equity_stop`, …) | `okx_execution` result in the execution ledger / pipeline rows; dashboard |
| Alerts vanish after a restart | Normal restarts replay from the WAL; time-less payloads are dropped by design (non-deterministic `signal_id`) | `logs/raw-webhooks.jsonl` (`phase: intake/dropped`); receiver startup log |
| `429 rate_limited` / `503 queue_full` | Alert storm exceeding 120 req/60 s per IP, or worker stalled (queue maxsize 200) | Receiver logs; queue-saturation operator alerts |
