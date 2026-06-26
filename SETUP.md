# Setup Guide

This guide is written for a human operator or an AI agent installing the system from a fresh clone.

Goal: run the Kinetic Flow execution system in OKX demo/sandbox first.

Do not use real-money API keys during first install.

If an AI agent is reading this repository, give it `setup/AGENT_INSTALL_PROMPT.md` first. The prompt forces the agent to explain the system and safety gates before executing anything.

## 1. Requirements

- Windows, macOS, Linux, or VPS with Python 3.11+
- TradingView account with access to the required MXC / Kinetic Flow indicators
- OKX demo/sandbox API key
- A webhook URL, either local tunnel or Cloudflare Tunnel
- Optional: a domain for public webhook/dashboard routes

## 2. Install

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/validate_package.py
```

On Linux/VPS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_package.py
```

## 3. Configure Environment

Copy the example file:

```powershell
Copy-Item setup/env.example .env
```

Fill:

```text
SHADOW_WEBHOOK_SECRET=
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=
OKX_SIMULATED_TRADING=1
OKX_SUBMIT_ORDERS=false
```

Keep `OKX_SUBMIT_ORDERS=false` until synthetic tests pass.

### Order Submission Safety Gates

The package has three separate gates for order submission. This is intentional.

| Layer | Field | Initial package value | What it controls |
|---|---|---:|---|
| `.env` | `OKX_SUBMIT_ORDERS` | `false` | Master local safety switch |
| `shadow-config.json` | `execution.submit_orders` | `true` | Runtime profile permission |
| strategy JSON | `okx_submit_orders` | `true` | Per-strategy permission |

For a fresh install, the `.env` value keeps order submission blocked even though the runtime profile and strategy files are prepared for OKX demo. To send OKX demo orders, all three layers must be `true`.

Do not set `OKX_SUBMIT_ORDERS=true` until synthetic tests pass and the operator intentionally wants OKX demo execution.

## 4. Configure Runtime

Create the active runtime config:

```powershell
Copy-Item config/runtime.demo.json shadow-config.json
```

Confirm these settings:

- execution mode is demo/sandbox
- margin mode is isolated
- leverage is 2x
- four strategies are present
- total assigned budget is `$6,500`

## 5. Validate Strategies

Check the four strategy files:

```text
strategies/solusdt_duo_base_dev_3h.json
strategies/ethusdt_duo_base_dev_2h.json
strategies/xrpusdt_duo_base_dev_4h.json
strategies/btcusdt_duo_base_dev_2h.json
```

Each TradingView alert must match its strategy file:

- same `strategy_id`
- same symbol
- same timeframe
- same indicator
- same chart type

## 6. Start Dashboard

```powershell
.\scripts\start_dashboard.ps1
```

Default local URL:

```text
http://127.0.0.1:8098/shadow/dashboard
```

Expected dashboard:

- one `Duo Base Dev Trial` view
- four asset cards
- unified execution ledger
- OKX demo status

## 7. Start Webhook Receiver

In a second terminal:

```powershell
.\scripts\start_webhook.ps1
```

Default local webhook:

```text
http://127.0.0.1:8888/webhook?secret=YOUR_SECRET
```

## 8. Create TradingView Alerts

Open `setup/04-tradingview-alerts.md`.

For each strategy, create:

- one BUY alert
- one SELL alert

Required TradingView settings:

- chart type: Heikin Ashi
- indicator: Duo Base Dev / duo-base-2.5
- timeframe: from strategy file
- frequency: once per bar close
- expiration: open-ended or maximum available
- webhook enabled
- message is valid JSON
- message includes `strategy_id`

If an AI agent controls the computer, it can create the alerts through the logged-in TradingView UI. The human must still verify the final alert list.

## 9. Synthetic Test

Before enabling order submission:

1. Send one valid test alert per strategy.
2. Send one alert with missing `strategy_id`.
3. Send one alert with wrong timeframe.
4. Confirm valid alerts are accepted.
5. Confirm invalid alerts are quarantined.
6. Confirm dashboard updates.

## 10. Enable OKX Demo Submission

Only after the synthetic test passes:

1. Set `OKX_SUBMIT_ORDERS=true` in `.env`.
2. Confirm `execution.submit_orders=true` in `shadow-config.json`.
3. Confirm each strategy has `okx_submit_orders=true`.
4. Restart the webhook receiver.
5. Wait for a real TradingView signal.
6. Confirm OKX demo order history and dashboard match.

If any one of the three gates is `false`, the system should not submit OKX demo orders.

## 11. VPS Deployment

Use `setup/05-vps-deploy.md` for VPS deployment.

Minimum VPS requirements:

- clone/copy this repo
- create `.env`
- create `shadow-config.json`
- configure systemd services for dashboard and webhook
- configure Cloudflare Tunnel or another HTTPS route
- verify dashboard and webhook health

## 12. Real Money

Do not enable real-money execution until:

- `docs/REAL_MONEY_CHECKLIST.md` is completed
- OKX demo has been observed through multiple real alerts
- dashboard equals OKX account state
- emergency stop is tested
- the operator explicitly approves real-money mode
