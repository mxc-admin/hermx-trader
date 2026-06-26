# Health And Recovery

The system should detect problems before they become trading risk.

## Health Domains

| Domain | Checks |
|---|---|
| TradingView | alerts active, not expired, no calculation error |
| Webhook | route online, secret validation, fast response |
| Strategy files | schema valid, active status clear |
| Exchange | API read works, positions match expected |
| Dashboard | online, not stale, matches exchange |
| Logs | append-only, no unexpected quarantine spike |

## Recovery Principle

If state is uncertain, pause execution before taking new risk.

## Common Recovery Actions

- restart webhook
- restart dashboard
- refresh TradingView chart
- repair alert payload
- disable strategy
- flatten OKX demo position
- restore from log/state

