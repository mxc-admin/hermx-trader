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
- Runtime profile clearly says live.
- User explicitly approves real-money execution.

## Live Promotion Rule

Do not change demo credentials into live credentials casually.

Live mode must be a separate deployment step.

