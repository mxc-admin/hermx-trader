# 07 — Security Rotation and Secret Hygiene

## 1) Use namespaced credentials per exchange

Prefer the namespaced blocks from `setup/env.example`:

- `OKX_DEMO_*`
- `KUCOIN_PAPER_*`
- `BYBIT_TESTNET_*`

This prevents one adapter from receiving another exchange's keys.

## 2) Rotate one exchange at a time

1. Disable submission: `HERMX_LIVE_TRADING=false`
2. Update only one exchange block in `.env`
3. Restart service
4. Run health checks for that exchange
5. Re-enable submission only after verification

## 3) Harden `.env` permissions

Set `.env` to owner-read/write only:

```bash
chmod 600 .env
```

## 4) Pre-commit secret scanning

This repo includes `.pre-commit-config.yaml` with `detect-secrets`.

Initialize once:

```bash
pre-commit install
pre-commit run --all-files
```

## 5) Webhook key rotation

- Rotate `HERMX_SECRET` and, if enabled, `HERMX_WEBHOOK_HMAC_KEY` together. The fastest path is `bash run.sh --new-secret`, which regenerates `HERMX_SECRET`.
- Coordinate sender cutover during a short maintenance window.
- Keep `HERMX_REQUIRE_HMAC=true` once sender signature delivery is validated.

## 6) Unified-secret blast radius

`HERMX_SECRET` is the **single** secret authenticating **both** the webhook
(`X-Webhook-Secret`) **and** the dashboard (`X-Dashboard-Token` / Bearer / Basic). There
are no legacy fallbacks (`SHADOW_WEBHOOK_SECRET` / `HERMX_DASH_AUTH_TOKEN` were removed).

Consequences to plan for:

- **One leak = both surfaces.** A compromised `HERMX_SECRET` exposes the webhook intake
  *and* the dashboard at once. Rotate it (and the HMAC key) immediately on any suspicion;
  `bash run.sh --new-secret` does the webhook+dashboard rotation in one step.
- **Fail-closed by default.** A blank/missing `HERMX_SECRET` does not "open up" access —
  it makes every webhook return `401` and every protected dashboard route `401`.
- **Defense in depth beats the shared secret alone.** The shared secret is replayable if
  captured. For any non-loopback exposure, turn on HMAC (next section) so a captured
  request cannot be replayed outside the freshness window.

## 7) Recommended posture: HMAC on for non-loopback

- Loopback-only deploys (receiver + dashboard bound to `127.0.0.1`, reached via a single
  Tailscale Funnel) can run with `HERMX_REQUIRE_HMAC=false` — the only public surface is
  the Funnel and the shared secret guards it.
- The moment the receiver binds a **non-loopback** interface (e.g. `HERMX_BIND_HOST=0.0.0.0`
  or a LAN IP), set `HERMX_REQUIRE_HMAC=true` and configure `HERMX_WEBHOOK_HMAC_KEY`. The
  receiver logs a SECURITY warning at boot if it binds non-loopback with HMAC off, because
  the webhook is then reachable off-host protected only by the (replayable) shared secret.
- HMAC adds a per-request `X-Webhook-Signature` over `timestamp‖body` with a replay window
  (`HERMX_REPLAY_WINDOW_SECONDS`, default 300s) — independent of the business idempotency
  window (`HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS`).
