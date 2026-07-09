# 08 — Webhook HMAC Relay Contract

> This relay is **only** needed when you run HMAC signing (`HERMX_REQUIRE_HMAC=true`) or
> want to authenticate via the `X-Webhook-Secret` header. For a plain direct TradingView
> alert, the **default and simplest** auth is the `secret_key` field in the alert JSON body
> (TradingView's native webhook cannot send custom headers) — no relay required. See
> `setup/04-tradingview-alerts.md`. Use this relay for header-based or HMAC-signed setups.

When `HERMX_REQUIRE_HMAC=true`, each webhook request must include:

- shared secret — either the `X-Webhook-Secret` header (relay/proxy) **or** the `secret_key`
  JSON body field (direct alerts); the header takes precedence when both are present
- `X-Webhook-Timestamp`: unix seconds (or parseable ISO time)
- `X-Webhook-Signature`: hex HMAC-SHA256 of `timestamp + raw_body`

## Signature algorithm

```text
expected = hex(hmac_sha256(HERMX_WEBHOOK_HMAC_KEY, X-Webhook-Timestamp + raw_body_bytes))
```

`X-Webhook-Signature` accepts either raw hex or `sha256=<hex>`.

## Replay window

`HERMX_REPLAY_WINDOW_SECONDS` defines the allowed skew window.
Requests outside the window are rejected with `401`.

## Fail-closed behavior

If `HERMX_REQUIRE_HMAC=true` and `HERMX_WEBHOOK_HMAC_KEY` is missing/blank,
all webhook requests fail closed with `401`.

## Relay requirements

- Preserve body bytes exactly between signing and forwarding.
- Preserve `X-Webhook-Timestamp` from signer to receiver.
- Ensure relay clocks are synchronized (NTP).
- Rotate `HERMX_SECRET` and `HERMX_WEBHOOK_HMAC_KEY` together.
