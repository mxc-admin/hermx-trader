# 02 Cloudflare Domain

Goal: route TradingView webhooks to the system.

Options:

1. Existing domain plus Cloudflare Tunnel.
2. New domain plus Cloudflare Tunnel.
3. Temporary tunnel for testing only.

Recommended production-style setup:

```text
webhook.example.com  -> local VPS webhook service
dashboard.example.com -> local VPS dashboard service
```

Rules:

- webhook route must use HTTPS
- route must include secret validation
- dashboard route should not expose secrets
- tunnel should be monitored

Validation:

- open webhook health URL
- open dashboard health URL
- send test webhook

