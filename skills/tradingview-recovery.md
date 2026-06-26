# Skill: TradingView Recovery

Use this when TradingView alerts or charts appear broken.

## Symptoms

- alert calculation error
- alert expired
- wrong chart timeframe
- wrong indicator version
- alert not firing
- TradingView logout

## Steps

1. Confirm the alert exists and is active.
2. Confirm expiration is open-ended or maximum available.
3. Confirm condition uses the correct indicator.
4. Confirm chart timeframe matches strategy.
5. Confirm payload includes `strategy_id`.
6. If chart shows indicator error, reload chart.
7. If still broken, remove and re-add indicator.
8. Reapply strategy parameters.
9. Save chart/layout.
10. Send or wait for a test alert.

## Escalate If

- invite-only indicator access is missing
- indicator version changed unexpectedly
- TradingView session cannot be restored

