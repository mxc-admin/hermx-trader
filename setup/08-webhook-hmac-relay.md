# 08 — Webhook HMAC Relay Contract

When `HERMX_REQUIRE_HMAC=true`, each webhook request must include:

- `X-Webhook-Secret`: shared secret (header auth)
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
