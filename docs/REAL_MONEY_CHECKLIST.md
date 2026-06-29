# Real Money Checklist

Real-money execution is not approved until this checklist is complete.

## Required Confirmations

- OKX demo has processed multiple valid alerts.
- At least one full flip has been tested: close, verify, open reverse.
- Dashboard matches OKX account state.
- No duplicate alerts caused pyramiding.
- Missing `strategy_id` alerts are quarantined.
- Strategy files validate.
- TradingView alerts are open-ended or maximum expiration.
- Emergency pause exists and is tested.
- Health checks are visible.
- API keys are stored outside the repo.
- User explicitly approves real-money execution.

## Go-Live Gate Settings

Going live is exactly two controls — both must be set, in this order:

1. The strategy has `execution_mode: "live"` (route to the real account instead of demo/sandbox).
2. The global kill switch is on: `HERMX_LIVE_TRADING=true` in the environment.

If any one is missing, no real-account order can submit. With `HERMX_LIVE_TRADING=false` (or unset),
every `execution_mode: live` order is blocked while demo trading continues unaffected — making the
kill switch the single lever to instantly disarm all live trading.

## Live Promotion Rule

Do not change demo credentials into live credentials casually.

Flipping a strategy from `execution_mode: demo` to `live` (and turning on `HERMX_LIVE_TRADING`)
must be a separate, deliberate deployment step — never an accidental side effect.

