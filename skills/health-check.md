# Skill: Health Check

Use this to verify the system is healthy.

## Checks

1. Webhook service is running.
2. Dashboard service is running.
3. Cloudflare route is reachable.
4. Strategy files validate.
5. Latest alerts are not stale.
6. TradingView alerts are active and not expired.
7. OKX API read works.
8. OKX open positions match local ledger.
9. Dashboard matches OKX readback.
10. Quarantine log has no unexpected valid alerts.

## Status Labels

| Status | Meaning |
|---|---|
| healthy | all checks pass |
| degraded | system works but one non-critical issue exists |
| blocked | alert intake or execution is unsafe |
| paused | operator intentionally paused execution |

## Critical Failures

- webhook down
- OKX API unreachable
- strategy_id mismatch
- unknown live position
- TradingView alerts expired
- dashboard/OKX mismatch

