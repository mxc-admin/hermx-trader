# Setup Guide

This guide is written for a human operator or an AI agent installing the system from a fresh clone.

Goal: run the Kinetic Flow execution system in OKX demo/sandbox first.

Do not use real-money API keys during first install.

If an AI agent is reading this repository, give it `setup/AGENT_INSTALL_PROMPT.md` first. The prompt forces the agent to explain the system and safety gates before executing anything.

> **Execution layer note.** Orders are routed through CCXT — the sole execution backend — behind the `ExecutionService` chokepoint. **OKX demo is the only configured and verified path today** (the concrete flow below). Multi-venue support (e.g. KuCoin, Hyperliquid) is **planned** (`REFACTOR_PLAN.md` Phase 6): each venue will use its own per-venue *namespaced* credentials (`OKX_*`, `KUCOIN_*`, `BYBIT_*`, …) with no exchange borrowing another's keys, and a missing/partial set fails closed. There are no KuCoin/Hyperliquid setup steps yet — follow the OKX-demo flow below as the current path.

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

The receiver binds **loopback only** at `127.0.0.1:$SHADOW_PORT`. The code default
(`src/webhook_receiver.py`) is **8891**:

```text
http://127.0.0.1:8891/webhook
```

The webhook secret is sent as the **`X-Webhook-Secret` HTTP header** (not a query
string), matched against `SHADOW_WEBHOOK_SECRET`; an HMAC signature
(`X-Webhook-Timestamp` + `X-Webhook-Signature`) is also verified when
`HERMX_WEBHOOK_HMAC_KEY` is set. Public TradingView alerts reach this loopback port
only through the Cloudflare Tunnel (section 11), never directly.

> **Port consistency.** `setup/env.example` and `scripts/start_webhook.ps1` currently
> pin `SHADOW_PORT=8888`, which overrides the 8891 code default. Pick one value and keep
> it consistent everywhere — and note the Hermes Agent skill (`skills/hermx-control/SKILL.md`,
> section 13) is written for **8891**, so if you keep 8888 you must update the skill to match.

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

## 13. Optional: Hermes Agent operator interface

**Entirely optional.** The deterministic system (dashboard + receiver + gate chain)
runs fine without it. The Hermes Agent is an **external** Nous Research runtime that lets
you *ask* HermX questions ("what's open?", "are we armed?") and *relay* a sanctioned
signal — it **reads and relays only, never self-initiates**, and is constrained to the
local loopback API. Sizing and all money-safety stay in Python. Full design:
`docs/HERMES_AGENT_DESIGN.md`. Full step-by-step: **`setup/09-hermes-agent.md`**.

Quick version:

1. Install the Hermes Agent (macOS): `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`, then `source ~/.zshrc`. Verify with `hermes --version`.
2. Set an LLM provider key (**USER action**) — e.g. `hermes setup --portal` (Nous Portal OAuth, no key file) or `hermes model` to pick another provider; credentials live in `~/.hermes/.env`. Do not commit keys.
3. Register the skill: symlink it into the Hermes skills tree, e.g.
   `mkdir -p ~/.hermes/skills/trading && ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/trading/hermx-control`. Confirm with `hermes skills list` (look for `hermx-control` / `trading` / `enabled`).
4. Let the local agent reach the dashboard: either run the dashboard with `HERMX_DASH_AUTH=false` bound to loopback, **or** keep `HERMX_DASH_AUTH=true`, set `HERMX_DASH_AUTH_TOKEN`, and give the agent that token as the `X-Dashboard-Token` header.
5. Verify by asking the agent "what's open?" and "are we armed?" — the second reads the new `arm` block from `GET /health`.
