# HermX

**Deterministic crypto signal execution, operated by an AI assistant.**

HermX turns TradingView strategy alerts into safe, gated exchange orders. A deterministic Python
stack owns all money-safety — validation, idempotency, journaling, kill switches — and submits
demo/sandbox orders only when every safety gate is open. On top of that sits the optional **Hermes
Agent**: a natural-language operator that *reads* state and *relays* sanctioned signals over Telegram.
It can never size a trade, override a gate, or call an exchange directly.

Execution runs through CCXT, with **OKX** as the live-verified venue (KuCoin, Bybit, and Hyperliquid
are wired in the resolver and planned). Public TradingView alerts reach a loopback-only receiver
through a **Tailscale Funnel** — a stable HTTPS URL with no domain to buy and no firewall ports to open.

## How it works

```text
TradingView alert
  -> Tailscale Funnel (stable public HTTPS URL)
  -> Webhook receiver  (loopback 127.0.0.1:8891; validates strategy_id + schema)
  -> Gate chain        (kill switch + 3 safety gates; idempotency, journal, reconcile)
  -> CCXT adapter      (venue translation)
  -> Exchange          (OKX demo — live-verified)
  -> Execution ledger -> Dashboard (loopback 127.0.0.1:8098, read-only)
```

The **Hermes Agent** reads the dashboard/health state and relays operator questions and sanctioned
signals over Telegram. All risk policy stays in Python — the LLM is advisory and read/relay only.

## Key properties

- **Fail-closed safety** — orders submit only when all three gates are open; anything missing disarms.
- **Loopback-only servers** — receiver and dashboard bind `127.0.0.1`; nothing is exposed directly.
- **Stable URL, no domain** — Tailscale Funnel gives a free, reboot-surviving HTTPS endpoint.
- **Supervised services** — runs under systemd (VPS) or Docker, auto-restarting on failure.
- **Low-frequency by design** — 1–2 signals/day on 2h–4h strategies; not a high-frequency bot.

## Quick Start

### Step 1 — Install Hermes Agent (your AI assistant)

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh
hermes provider setup   # choose your LLM provider (xAI, OpenAI, Anthropic, Ollama, etc.)
```

### Step 2 — Register the HermX skill and start the install

```bash
# From inside this repo:
ln -sfn "$(pwd)/skills/hermx-control" ~/.hermes/skills/hermx-control
cat INSTALL.md | hermes -z --skills hermx-control
```

Hermes walks you through all 8 install phases interactively — asking for exchange API keys,
reviewing strategies, setting up Tailscale (outputs your stable webhook URL), deploying services,
and configuring the Telegram gateway. You answer questions; it runs the commands.

**No Hermes?** Paste `INSTALL.md` into any AI assistant (Claude, Windsurf, Cursor, etc.) and follow along manually.
See [INSTALL.md](INSTALL.md) for the full guide.

## What you'll have after install

- A stable public webhook URL (`https://hermx.<tailnet>.ts.net/webhook`)
- TradingView alerts configured for each enabled strategy (BUY + SELL)
- Hermes available on Telegram to query state and relay signals
- Webhook receiver + dashboard supervised by systemd or Docker

## Strategies

Four default strategies ship ready to run — BTC, ETH, SOL, and XRP USDT perpetuals on OKX, 2h–4h
timeframes, isolated margin, 2x leverage, ~$6,500 total demo budget. **All are demo/sandbox by
default**, and the strategy file (never the alert) owns position sizing. During install you review
and confirm each strategy's risk parameters before anything is enabled. See
[docs/STRATEGIES.md](docs/STRATEGIES.md) for detail.

## Architecture

A four-layer design: a deterministic execution substrate (built and tested) capped by a planned
reasoning layer that only ever calls *down* into it. See [ARCHITECTURE.md](ARCHITECTURE.md) for the
full design, runtime flow, and built-vs-planned status.

## Safety

Every demo order must pass a **three-gate** model — a master `.env` switch (`OKX_SUBMIT_ORDERS`),
a runtime-profile gate (`execution.submit_orders`), and a per-strategy gate (`submit_orders`). All
three must be `true` to submit; any one `false` blocks submission by design, and fresh installs ship
with the master switch off. Sizing, idempotency, journaling, and reconciliation all live in
first-party Python — **the LLM never touches sizing or money-safety**.

## Requirements

- A VPS (fresh Ubuntu 22.04) **or** a local Mac
- Python 3.11+
- OKX **demo** API keys (or KuCoin/Bybit/Hyperliquid testnet keys)
- TradingView Pro+ (needed to send webhook request headers)
- A free Tailscale account
- An LLM provider API key (xAI, OpenAI, Anthropic, etc.) — only for the optional Hermes Agent
