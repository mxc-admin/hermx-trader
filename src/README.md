# Runtime Source

This folder contains the runnable demo system.

```text
webhook_receiver.py   TradingView webhook intake, validation, strategy routing, ledger writes, OKX demo submission
dashboard.py          Clean dashboard server
dashboard_core.py     Shared dashboard helpers and historical compatibility helpers
okx_demo_executor.py  OKX demo/sandbox API adapter and execution helper
```

Run from the repository root so `SHADOW_ROOT` points to the package root.
