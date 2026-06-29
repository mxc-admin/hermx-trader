# HermX

**Safe, deterministic execution of your TradingView strategies — with an optional AI co-pilot that cannot touch your money.**

HermX turns TradingView alerts into exchange orders through a hardened Python execution layer that refuses to place a trade unless *every* safety check passes. It ships demo-first, logs everything, and can optionally be paired with the Hermes Agent — an LLM-powered assistant that monitors your system and relays signals over Telegram, but has zero ability to size positions, override gates, or call exchanges directly.

If you have (or are building) solid strategies on TradingView and want reliable 24/7 execution without babysitting charts or trusting fragile custom bots, HermX was built for you.

## Why Should You Care?

Most traders face an uncomfortable tradeoff:

- **Manual trading** — You miss signals while sleeping, make emotional decisions, or waste hours staring at charts.
- **Build your own executor** — One bug, missed edge case, or credential issue can be extremely expensive.
- **Use black-box signal services** — You give up control, pay high fees, and still worry about execution quality.

**HermX offers a different path.**

It provides a **deterministic, auditable execution engine** that treats every alert as if real money is on the line — even in demo mode. Multiple independent safety layers (idempotency, kill switches, journaling, reconciliation, and more) must all agree before an order is submitted. There is a single execution chokepoint, and the entire money path is written in Python you can read and audit.

On top of this rock-solid foundation sits the optional **Hermes Agent**. This AI assistant lives in your Telegram, can answer questions about your positions and system health, and can relay sanctioned signals. 

**Most importantly**: The AI can **never** decide how much to trade, change risk parameters, bypass a safety gate, or talk directly to an exchange. All money logic stays in the deterministic Python layer.

**Result**: You get reliable automation of *your* strategies with institutional-grade safety rails + a helpful AI teammate that has no keys to the vault.

## How It Works

```
TradingView Pine Strategy
        ↓ (webhook alert)
Tailscale Funnel (stable public HTTPS URL — no domain or open ports needed)
        ↓
Webhook Receiver (validates, authenticates, rate-limits)
        ↓
Safety Gate Chain (execution_mode + idempotency + journal + reconciliation + more)
        ↓
CCXT Executor → Exchange (OKX fully tested; others supported)
        ↓
Append-only Ledger + Local Dashboard
        ↑
Hermes Agent (optional) — reads state → chats with you on Telegram
```

Everything is logged. Every decision is explainable. The system defaults to the safest possible state.

## Core Safety Guarantees

- **Fail-closed design** — If any check fails or required data is missing, no order is placed.
- **Single execution chokepoint** — All orders flow through one audited Python service (`ExecutionService`).
- **AI isolation** — The Hermes Agent is strictly read/relay. It cannot influence sizing or bypass any gate.
- **Demo-first** — Live trading is disabled by default (`HERMX_LIVE_TRADING=false`). You must explicitly enable it.
- **Full audit trail** — Every alert, gate decision, and order is permanently journaled.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete safety model and design philosophy.

## Quick Start (Recommended)

The fastest way to get running is to let the **Hermes Agent** guide you interactively.

### 1. Install the Hermes Agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh
hermes provider setup     # Choose your LLM (xAI, OpenAI, Anthropic, Ollama, etc.)
```

### 2. Register HermX and run the guided install

```bash
# From inside this repository
ln -sfn "$(pwd)/skills/hermx-control" ~/.hermes/skills/hermx-control
cat INSTALL.md | hermes -z --skills hermx-control
```

Hermes will walk you through exchange API keys (start with demo/sandbox), strategy review, Tailscale setup (gives you a permanent webhook URL), service deployment, and Telegram connection.

**Prefer no AI?** Run `./install.sh` instead — it follows the same guided flow without an LLM.

Full instructions: [INSTALL.md](INSTALL.md)

## Included Strategies

Four ready-to-run **demo** strategies ship with HermX (all on OKX, isolated margin, 2x leverage):

| Strategy | Timeframe | Symbol     | Budget (demo) | Notes |
|----------|-----------|------------|---------------|-------|
| BTC      | 2h        | BTC-USDT   | $1,500        | Active demo candidate |
| ETH      | 2h        | ETH-USDT   | $1,500        | Active demo candidate |
| SOL      | 3h        | SOL-USDT   | $1,500        | Active demo candidate |
| XRP      | 4h        | XRP-USDT   | $1,500        | Active demo candidate |

All use the `mxc duo-base v2.5` indicator logic. They are configured for demo/sandbox by default. Review and customize the JSON files in the `strategies/` folder. See [docs/STRATEGIES.md](docs/STRATEGIES.md) for the full schema and how to add your own strategies.

## Supported Exchanges

**Fully wired (live-tested)**:
- **OKX** (recommended — live-tested)

**Wired, not yet live-tested**:
- KuCoin
- Bybit
- Hyperliquid

**Credential profiles exist but execution not yet wired**:
- Binance, Bitget, Gate.io, Coinbase Advanced

## What You Get After Setup

- A permanent, stable webhook URL (`https://hermx.<your-tailnet>.ts.net/webhook`)
- TradingView alerts configured and sending signals
- Local read-only dashboard (`http://127.0.0.1:8098`)
- Hermes Agent in Telegram for monitoring and signal relay
- Complete peace of mind that safety logic lives in auditable Python code

## Requirements

- VPS (Ubuntu 22.04 recommended) or local Mac
- Python 3.11+
- Demo/sandbox API keys from a supported exchange (OKX recommended to start)
- TradingView Pro+ (for webhook headers)
- Free Tailscale account
- LLM API key (only needed for the optional Hermes Agent)

## Philosophy

> Safety lives in code, not in config or prose.  
> Fail-closed on the money path. Fail-open on intelligence.

HermX was built so serious traders can automate execution with real confidence.

---

**HermX is early-stage software.** Always begin in demo mode. Thoroughly test your strategies and understand the system before enabling live trading.

If HermX helps you trade more reliably, consider starring the repo and sharing your experience.
