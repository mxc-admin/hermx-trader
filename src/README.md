# Runtime Source

This folder contains the runnable demo system.

```text
webhook_receiver.py   TradingView webhook intake, validation, strategy routing, ledger writes, execution dispatch
dashboard.py          Clean dashboard server
dashboard_core.py     Shared dashboard helpers and historical compatibility helpers
executors/            Exchange-agnostic execution layer (CCXT adapter + factory)
```

Run from the repository root so `SHADOW_ROOT` points to the package root.
