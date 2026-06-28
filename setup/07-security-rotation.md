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
