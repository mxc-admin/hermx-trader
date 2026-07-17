# TradingView Alert Contract

Every TradingView alert must send JSON.

Every active execution alert must include `strategy_id`.

## Required Payload

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "action": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

### Direction Field — `action`

- **`action`** (string, required): Direction + intent. Enum: `buy` (enter long), `sell`
  (enter short), `close` (flatten open position, reduce-only). This is the **sole** accepted
  direction field — it is required by the schema.

The legacy `side` field is **no longer read or accepted**. Alerts must send `action`.

## Authentication

The alert carries the shared secret (`HERMX_SECRET`) via one of two transports:

- **Default — `secret_key` JSON body field.** TradingView's native webhook alert action
  **cannot send custom HTTP headers on any plan**, so a direct TradingView alert
  authenticates by including `"secret_key": "<HERMX_SECRET>"` in the alert Message JSON.
  This is the **standard method** for stock TradingView alerts. The receiver strips
  `secret_key` immediately after authenticating, so it never lands in any ledger.
- **Alternative — `X-Webhook-Secret` HTTP header.** For operators who run a relay/proxy
  in front of the receiver, the secret may instead be injected as the `X-Webhook-Secret`
  header. When the header is present it takes precedence and must match; a present-but-wrong
  header is rejected and does **not** fall through to the body field.

The secret is **never** placed in the URL (path or query string). If both transports are
present, the header wins.

## Venue, Execution Mode, and Sizing Come From the Strategy

The alert carries only the **signal** — never venue routing, sizing, or execution mode. Those
are supplied by the strategy file matched on `strategy_id`:

- **Venue** is `strategy.instrument.exchange`, which is **authoritative for routing** and the
  **sole** source of venue selection. The alert payload carries **no** `exchange` field at all —
  it is not part of the schema (`schemas/tradingview-alert.schema.json`). The strategy file
  alone decides where an order routes via `instrument.exchange`.
- **Execution mode** is `strategy.execution_mode`, a two-value enum — `demo` or `live`.
  **Only `live` is real-money**: it routes to the real account and additionally requires
  the global kill switch `HERMX_LIVE_TRADING=true`. `demo` routes to the venue's
  sandbox/paper account (always allowed; treated as `simulated_trading`).
- **Sizing** is computed in the receiver as `capital.budget_usd * leverage`. The alert has **no**
  size, notional, budget, or leverage field; any such value would be ignored.

This keeps the contract **exchange-agnostic**: the same alert shape works for every supported
venue, and the strategy file selects where and how it executes.

## Close Semantics — `action=close`

An alert with `action` set to `close` **flattens the strategy's open position, reduce-only**.
It does **not** open a new position.

- Requires a **matched strategy** (routing via `strategy.instrument`), same as any other alert.
- No direction field is needed for a close; `action=close` is sufficient on its own.
- **Bypasses the kill switch and symbol pause** (same as the operator `/hx-close` button) — a
  reduce-only order cannot increase exposure, so it is always allowed to flatten.
- **Deduplicated per bar close signal**, same as buy/sell signals.

## Required TradingView Settings

- Condition: correct Duo Base Dev BUY or SELL signal.
- Timeframe: must match the strategy file.
- Alert frequency: once per bar close.
- Webhook URL: system webhook URL. Authenticate by adding `"secret_key":"<HERMX_SECRET>"` to
  the alert Message JSON (default for native alerts), or — for relay/proxy setups — the
  `X-Webhook-Secret` header with the `HERMX_SECRET` value.
- Expiration: open-ended or longest available.

## Validation Rules

The receiver must reject or quarantine:

- missing `strategy_id`
- unknown `strategy_id`
- wrong symbol for that strategy
- wrong timeframe for that strategy
- missing `action`
- `action` not one of `buy`, `sell`, or `close`
- malformed JSON

## Why `strategy_id` Matters

The same asset can have multiple strategies.

Example:

- SOLUSDT Duo Base Dev 3H
- SOLUSDT alternate production strategy
- SOLUSDT research/paper strategy

Without `strategy_id`, the system cannot know which one should execute.

## Alert Examples

### SOL 3H Buy

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "action": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

### ETH 2H Sell

```json
{
  "strategy_id": "ethusdt_duo_base_dev_2h",
  "strategy_name": "ETHUSDT Duo Base Dev 2H",
  "indicator": "duo-base-dev",
  "symbol": "ETHUSDT",
  "timeframe": "2h",
  "action": "sell",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

### BTC 2H Close

Flatten the strategy's open position (reduce-only). `action=close` is sufficient on its own.

```json
{
  "strategy_id": "btcusdt_duo_base_dev_2h",
  "symbol": "BTCUSDT",
  "timeframe": "2h",
  "action": "close",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```

## Optional `extras` Debugging Field

The alert may carry an optional top-level `extras` object. It is **observe-only**:
the receiver preserves it through normalization and logs it to `pipeline.jsonl`
(promoted to the event's top-level `extras` key for easy grepping), but it **never
influences routing, sizing, mode, or execution**. Use it to attach chart/debug
context that helps correlate a signal to what you saw on TradingView.

```json
{
  "strategy_id": "solusdt_duo_base_dev_3h",
  "strategy_name": "SOLUSDT Duo Base Dev 3H",
  "indicator": "duo-base-dev",
  "symbol": "SOLUSDT",
  "timeframe": "3h",
  "action": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview",
  "extras": { "chart_note": "post-breakout retest", "alert_id": "tv-9931" }
}
```

`extras` must be a JSON **object** when present. The schema (`schemas/tradingview-alert.schema.json`)
constrains it to `{"type": "object"}`; a non-object `extras` is a schema violation and is
rejected when schema enforcement is on. Any non-core field belongs **inside** `extras`, not at
the root, so future core-schema additions never collide with your debug keys.

## Validation Errors Reference

Validation happens in two stages. Understanding which stage rejects a payload tells you
where to look for the failure.

**Stage 1 — Transport (synchronous).** `do_POST` validates the HTTP request itself and
returns the status **directly to the caller** before queueing. These are the only rejections
TradingView's webhook client observes in the HTTP response.

**Stage 2 — Intake / semantic (asynchronous).** A `200 {"status":"queued"}` acknowledges
receipt; the payload is then normalized and validated on a worker. These outcomes are **not**
returned to the caller — they are written to `pipeline.jsonl` (stage `error`, `quarantine`,
`dedup_reject`, `strategy_match`, or `decision`) with the status shown below as the `status`
field. A quarantined alert is stored with a `reason` and never executed. Inspect `pipeline.jsonl`
and `raw-webhooks.jsonl` to diagnose these.

> Note on freshness fields: `normalize()` **backfills** a missing `tv_time` with server time,
> so a missing `tv_time` alone is never a rejection reason (it degrades freshness, not validity).
> A missing `tv_signal_price` normalizes to `null` and is only rejected under schema enforcement.

### Malformed JSON — `invalid_json` (HTTP 400, synchronous)

Body is not valid JSON.

Invalid:
```
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT",  // trailing comma, comment
```
Corrected:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","strategy_name":"SOLUSDT Duo Base Dev 3H","indicator":"duo-base-dev","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

Other Stage-1 rejections (same synchronous path): `invalid_content_length` (400) for a bad/negative
`Content-Length`; `payload_too_large` (413) when the body exceeds `HERMX_MAX_BODY_BYTES`;
`rate_limited` (429) when the per-source rate bucket is exhausted; `queue_full` (503) when the
processing queue is saturated.

### Missing or invalid `action` — `side_not_allowed` (status 400)

`action` is absent, or not one of `buy` / `sell` / `close` after lowercasing.

Invalid:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"long","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected (`action` must be `buy`, `sell`, or `close`):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Missing `strategy_id` — `missing_strategy_id` (status 202, quarantined)

A Duo Base Dev alert (recognized by `indicator`/`strategy_name`) arrives with no `strategy_id`.
With `strategy_engine.require_strategy_id=true` the reason is `missing_strategy_id_required` instead.

Invalid:
```json
{"strategy_name":"SOLUSDT Duo Base Dev 3H","indicator":"duo-base-dev","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected (add the `strategy_id`):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","strategy_name":"SOLUSDT Duo Base Dev 3H","indicator":"duo-base-dev","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Unknown `strategy_id` — `unknown_strategy_id` (status 202, quarantined)

`strategy_id` has no matching file in `strategies/`.

Invalid (no `strategies/solusdt_duo_base_dev_9h.json` exists):
```json
{"strategy_id":"solusdt_duo_base_dev_9h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected (use a `strategy_id` that exists on disk):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Wrong `symbol` for the strategy — `strategy_symbol_mismatch` (SOFT warning, still executes)

`symbol` (uppercased, separators stripped) does not equal the strategy's derived asset.
Matching is strategy_id-first, so this NO LONGER quarantines: the alert executes on the
strategy's own `instrument` block and the record carries
`strategy_warning: "strategy_symbol_mismatch"`. A close is never blocked by a symbol
mismatch. Still fix the alert — the warning signals the alert sits on the wrong chart.

Warned (`solusdt_duo_base_dev_3h` trades `SOLUSDT`, not `BTCUSDT`):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"BTCUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Wrong `timeframe` for the strategy — `strategy_timeframe_mismatch` (status 202, quarantined)

`timeframe` (canonicalized) does not match the strategy file's `timeframe`.

Invalid (`solusdt_duo_base_dev_3h` is a 3h strategy):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"2h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Symbol not wired — `symbol_not_allowed` (status 400)

A non-strategy alert (no `strategy_id`) whose `symbol` is not one of the assets any loaded
strategy trades.

Invalid:
```json
{"symbol":"DOGEUSDT","timeframe":"3h","action":"buy","tv_signal_price":"0.16","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected (route through a real strategy):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Schema validation failure — `alert_schema_invalid:<detail>` (status 202, quarantined)

Only enforced when `HERMX_ENFORCE_ALERT_SCHEMA=true` (`strategy_engine.enforce_alert_schema`).
When enforcement is **off** the same failure is logged + counted but the alert still processes
(observe-only). The normalized alert is validated against `schemas/tradingview-alert.schema.json`;
`<detail>` names the offending field. Common triggers: missing `tv_signal_price` (normalizes to
`null`), a bad `timeframe` enum value, or a non-object `extras`.

Invalid (missing `tv_signal_price`):
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```
Corrected:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

### Non-TradingView source — `non_tradingview_source` (status 202, ignored)

`source` is not `tradingview`; the alert is acknowledged but never processed.

Invalid:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"manual"}
```
Corrected:
```json
{"strategy_id":"solusdt_duo_base_dev_3h","symbol":"SOLUSDT","timeframe":"3h","action":"buy","tv_signal_price":"171.42","tv_time":"2026-07-01T12:00:00Z","source":"tradingview"}
```

## TradingView Message Templates

Paste one of these into the TradingView alert's **Message** box (Condition → *once per bar close*).
Pine Script placeholders — `{{ticker}}`, `{{strategy.order.action}}`, `{{close}}`, `{{time}}`,
`{{interval}}` — are substituted by TradingView at fire time. The `symbol` uses `{{ticker}}`
(the receiver uppercases it and strips `OKX:` / `-` / `/`), and `action` uses
`{{strategy.order.action}}` (emits `buy`/`sell`). Keep `timeframe` **hard-coded** to the
strategy's bar so an alert placed on the wrong chart is quarantined as `strategy_timeframe_mismatch`
rather than silently accepted — do **not** use `{{interval}}` for it.

Authenticate the alert with the shared secret. For a **direct TradingView alert** (the
default), add `"secret_key":"<HERMX_SECRET>"` to the Message JSON below — TradingView's
native webhook cannot send custom headers. For **relay/proxy setups**, set the
`X-Webhook-Secret` header to `HERMX_SECRET` in the webhook settings instead. Never put the
secret in the URL.

### BTCUSDT Duo Base Dev 2H
```json
{"strategy_id":"btcusdt_duo_base_dev_2h","strategy_name":"BTCUSDT Duo Base Dev 2H","indicator":"duo-base-dev","symbol":"{{ticker}}","timeframe":"2h","action":"{{strategy.order.action}}","tv_signal_price":"{{close}}","tv_time":"{{time}}","source":"tradingview","secret_key":"<HERMX_SECRET>"}
```

### ETHUSDT Duo Base Dev 2H
```json
{"strategy_id":"ethusdt_duo_base_dev_2h","strategy_name":"ETHUSDT Duo Base Dev 2H","indicator":"duo-base-dev","symbol":"{{ticker}}","timeframe":"2h","action":"{{strategy.order.action}}","tv_signal_price":"{{close}}","tv_time":"{{time}}","source":"tradingview","secret_key":"<HERMX_SECRET>"}
```

### SOLUSDT Duo Base Dev 3H
```json
{"strategy_id":"solusdt_duo_base_dev_3h","strategy_name":"SOLUSDT Duo Base Dev 3H","indicator":"duo-base-dev","symbol":"{{ticker}}","timeframe":"3h","action":"{{strategy.order.action}}","tv_signal_price":"{{close}}","tv_time":"{{time}}","source":"tradingview","secret_key":"<HERMX_SECRET>"}
```

### XRPUSDT Duo Base Dev 4H
```json
{"strategy_id":"xrpusdt_duo_base_dev_4h","strategy_name":"XRPUSDT Duo Base Dev 4H","indicator":"duo-base-dev","symbol":"{{ticker}}","timeframe":"4h","action":"{{strategy.order.action}}","tv_signal_price":"{{close}}","tv_time":"{{time}}","source":"tradingview","secret_key":"<HERMX_SECRET>"}
```

Replace `<HERMX_SECRET>` with the actual `HERMX_SECRET` value. This `secret_key` field is
the default auth transport for direct TradingView alerts; omit it only if you inject the
`X-Webhook-Secret` header via a relay/proxy. The receiver strips `secret_key` right after
authenticating, so it never reaches any ledger.

To attach debug context, add an `extras` object (observe-only, see above), e.g.
`...,"source":"tradingview","extras":{"tv_alert":"{{time}}"}}`.
