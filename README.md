# Kinetic Flow Execution System

> Agent-first install: lee `setup/AGENT_INSTALL_PROMPT.md` y usalo como guia unica para entender, validar e instalar este sistema en OKX demo seguro.

Strategy-file-driven execution system for Kinetic Flow / MXC TradingView signals.

This repository is designed to be cloned, configured, tested in OKX demo, and then deployed to a VPS. It does not include private credentials and it does not approve real-money execution by default.

## What It Does

```text
TradingView alert
  -> webhook receiver
  -> strategy_id validation
  -> strategy JSON lookup
  -> execution planner
  -> OKX demo order route
  -> execution ledger
  -> clean dashboard
```

## Current Demo Trial

Total assigned demo budget: `$6,500`.

| Strategy ID | Asset | TF | Upper | Lower | Budget | Leverage | Mode |
|---|---|---:|---:|---:|---:|---:|---|
| `solusdt_duo_base_dev_3h` | SOLUSDT | 3h | 1.05 | 0.95 | 1500 | 2x | OKX demo |
| `ethusdt_duo_base_dev_2h` | ETHUSDT | 2h | 1.40 | 0.95 | 2000 | 2x | OKX demo |
| `xrpusdt_duo_base_dev_4h` | XRPUSDT | 4h | 1.20 | 0.95 | 1500 | 2x | OKX demo |
| `btcusdt_duo_base_dev_2h` | BTCUSDT | 2h | 1.40 | 0.95 | 1500 | 2x | OKX demo |

All current strategies use:

- Heikin Ashi charts
- Duo Base Dev / duo-base-2.5
- isolated margin
- 2x leverage
- OKX demo/sandbox execution
- one BUY and one SELL TradingView alert per strategy

## Repository Map

```text
README.md                  start here
SETUP.md                   full installation and validation guide
ARCHITECTURE.md            system design and runtime flow
requirements.txt           Python dependency entrypoint

src/
  webhook_receiver.py      TradingView webhook intake and strategy routing
  dashboard.py             clean dashboard server
  dashboard_core.py        dashboard helper layer
  okx_demo_executor.py     OKX demo/sandbox adapter

strategies/                one JSON file per active strategy
schemas/                   JSON validation contracts
config/                    runtime profiles, including config/runtime.demo.json
setup/                     focused setup guides
setup/AGENT_INSTALL_PROMPT.md handoff prompt for Codex, Claude, or Hermes Agent
docs/                      operational details
skills/                    agent/operator playbooks
scripts/                   validation and local start helpers
```

## Quick Start

If an AI agent is installing or auditing this repo, start with `setup/AGENT_INSTALL_PROMPT.md`.

Human quick start:

1. Read `SETUP.md`.
2. Copy `setup/env.example` to `.env`.
3. Fill only demo/sandbox credentials.
4. Copy `config/runtime.demo.json` to `shadow-config.json`.
5. Validate the package:

```powershell
python scripts/validate_package.py
```

6. Start the dashboard:

```powershell
.\scripts\start_dashboard.ps1
```

7. Start the webhook receiver:

```powershell
.\scripts\start_webhook.ps1
```

8. Create TradingView alerts using `setup/04-tradingview-alerts.md`.
9. Confirm every alert has `strategy_id`.
10. Confirm every alert is open-ended or uses the maximum available expiration.

## Important Safety Rules

- Do not commit `.env`.
- Do not put API keys in strategy files.
- Do not use real-money credentials during first install.
- Do not enable real-money execution until `docs/REAL_MONEY_CHECKLIST.md` is complete.
- If an alert is missing `strategy_id`, quarantine it.
- If a signal is same-direction, do not pyramid.
- If a signal is opposite-direction, close current position, verify close, then open reverse.

## Order Submission Safety Gates

The repository is prepared for OKX demo execution, but new installs are blocked by the `.env` master switch until the operator enables it.

All three layers must allow submission before any OKX demo order can be sent:

| Layer | Field | Initial package value | Purpose |
|---|---|---:|---|
| `.env` | `OKX_SUBMIT_ORDERS` | `false` | Master local safety switch |
| `shadow-config.json` | `execution.submit_orders` | `true` | Runtime profile allows demo execution |
| strategy JSON | `okx_submit_orders` | `true` | Individual strategy allows demo execution |

Recommended install flow:

1. Keep `OKX_SUBMIT_ORDERS=false`.
2. Run validation and synthetic webhook tests.
3. Confirm dashboard and quarantine behavior.
4. Set `OKX_SUBMIT_ORDERS=true` only when ready to submit OKX demo orders.
5. Never use real-money credentials unless the real-money checklist is complete and explicitly approved.
