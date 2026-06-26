# 05 VPS Deploy

Goal: deploy after local/demo validation.

Steps:

1. Copy package to VPS.
2. Install dependencies.
3. Add `.env` secrets outside git.
4. Configure runtime profile.
5. Configure system services.
6. Configure Cloudflare tunnel.
7. Start webhook.
8. Start dashboard.
9. Run health check.
10. Send synthetic alerts.
11. Confirm OKX demo behavior.

Success means:

- public webhook works
- public dashboard works
- valid alert reaches strategy log
- invalid alert is quarantined
- OKX demo order flow works

