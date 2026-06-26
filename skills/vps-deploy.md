# Skill: VPS Deploy

Use this when deploying the clean system to a VPS.

## Inputs

- VPS IP and user.
- Domain or Cloudflare route.
- Runtime profile.
- `.env` secrets.
- Strategy files.

## Steps

1. Copy clean package to VPS.
2. Install dependencies.
3. Place `.env` outside public folders.
4. Validate strategy files.
5. Start webhook service.
6. Start dashboard service.
7. Configure Cloudflare tunnel.
8. Confirm local ports.
9. Confirm public routes.
10. Send synthetic valid alert.
11. Send synthetic invalid alert.
12. Confirm dashboard updates.
13. Confirm OKX demo health.

## Services

Recommended services:

- webhook receiver
- dashboard server
- health monitor
- optional TradingView/CDP helper

## Do Not

- Do not deploy live credentials without approval.
- Do not replace a running production system without backup.
- Do not expose API keys in logs.

