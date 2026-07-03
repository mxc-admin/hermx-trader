# Kajinx + Hermes vs. TradersPost — Competitive Positioning

_Prepared 2026-07-03. TradersPost details from public research (traderspost.io, docs.traderspost.io, third-party reviews, user forums). Kajinx + Hermes details from the HermX execution codebase._

---

## 1. Executive Summary

**Kajinx + Hermes is an agent-native execution and operations platform for leveraged crypto trading.** It is not another "TradingView alerts → broker automation" tool. It is a different interface paradigm for running a trading operation.

Two layers make up the product:

- **Kajinx (HermX) — the execution layer.** A self-hosted, fail-closed, write-ahead-journaled engine built for crypto derivatives. Correctness-first: every order passes a gate chain, uncertainty is never counted as success, and restarts replay a durable journal instead of double-firing.
- **Hermes — the agent layer.** A conversational, command-driven interface that lives in Telegram. You operate the system by issuing commands and interacting with an intelligent agent — not by clicking through a dashboard. Hermes is read-only by default on critical actions and is designed to **evolve**: from assistant (monitor, trace, manage) to advisor (risk and macro judgment) and eventually toward a self-evolving agent that helps decide when to trade, when to reduce risk, and when to stay flat.

The dashboard is a **read-only observability layer** — performance, logs, PnL, and a unified view at a glance. The active workspace is the Telegram chat where you command the system and where all monitoring, notifications, and interactions flow.

**TradersPost has no meaningful equivalent to this.** It is a classic no-code webhook-to-broker automation SaaS — a good one — but not an agentic operations layer.

---

## 2. What is TradersPost?

**What it is:** A cloud-hosted, no-code trading-automation SaaS founded in 2021 (Nashville, TN). It receives TradingView / TrendSpider alerts via webhooks and routes orders to connected brokers and exchanges across multiple asset classes.

| | |
|---|---|
| **Paradigm** | Traditional webhook-to-broker SaaS automation — configure rules in a web dashboard, connect brokers, let alerts fire orders |

**Asset classes & venues**
- **Stocks/ETFs** (long + short): TradeStation, Alpaca, Robinhood, Interactive Brokers, E*TRADE, tastytrade, Tradier, Webull
- **Futures**: Tradovate, TradeStation (+ ProjectX / prop-firm eval accounts)
- **Options** (beta): TradeStation, Tradier
- **Crypto (spot only, 24/7)**: Coinbase, Alpaca, Kraken, Bybit, Binance
- 17+ total broker/exchange integrations; paper-trading broker included

**Core capabilities**
- TradingView webhook → broker order routing, no code required
- **Multi-account parallel execution** — one alert fans out to many linked live/paper/prop accounts simultaneously
- Order types with broker-native **Stop Loss** (stop / stop_limit / trailing_stop), **Take Profit** (relative %, absolute price, or portfolio-PnL), and **Trailing Stops** incl. multi-leg variants
- **Risk-per-position sizing** — quantity derived from a fixed dollar risk and stop distance
- **Max Strategy Positions** cap (counts *all* broker-held positions, not just TradersPost's)
- Strategy tester / backtest, script library, dashboard order entry, TradingView price-line trading
- Prop-firm and multi-account management; user-management on top tier

**Pricing** (billed yearly; ~15% off vs. monthly)
| Plan | Price (yearly) | Live acct | Paper acct | Asset classes |
|---|---|---|---|---|
| Free | $0 (7-day trial) | manual submit only | automated | — |
| Starter | ~$41.65/mo ($49 monthly) | 1 | 4 | 1 |
| Basic ("most popular") | ~$84.15/mo | 2 | 6 | 2 |
| Pro | ~$169.15/mo | 3 | 8 | 3 |
| Premium | ~$254.15/mo | 6 | 10 | All |
- Add-on accounts: **+$10/mo live**, **+$5/mo paper** (up to 50)
- Support: in-app/email (all), scheduled video calls (Basic+), enterprise SLA plans

**Known limitations & complaints (from reviews/docs)**
- **Explicitly "NOT a high-frequency trading platform"** — sub-1-minute timeframes unsupported; webhooks may be disabled for accounts sending excessive requests
- Reported **~2-second order fill latency** in some user reviews (vs. sub-100ms elsewhere)
- Paper-trading bugs reported (limit orders in pre-market / RTH)
- Strategy tester accuracy criticized for complex strategies
- 7-day free trial counts weekends (when most markets are closed)
- **Cloud SaaS only, shared multi-tenant environment — no self-hosting**
- Public docs/FAQ do **not** describe HMAC signatures, replay windows, kill switches, or duplicate-signal handling; webhook auth is effectively a shared secret token in the payload
- **No conversational or agentic interface** — the product is the web dashboard

---

## 3. What is Kajinx + Hermes?

**What it is:** An agent-native trading operations platform. You run a self-hosted, correctness-first crypto execution engine (**Kajinx**) and operate it conversationally through an intelligent agent (**Hermes**) from Telegram. The dashboard exists for observability; the command line of the operation is a chat.

### Layer 1 — Kajinx (execution layer): self-hosted, fail-closed, crypto-derivatives-native

- TradingView webhook → CCXT order execution (OKX live-tested; KuCoin, Bybit, Hyperliquid wired; Binance, Bitget, Gate.io, Coinbase profiles present)
- **Crypto derivatives / perps with leverage** — per-strategy `budget_usd`, `leverage`, `execution_mode`, instrument config
- **7-layer fail-closed safety gate chain**: strategy-active → auth-health → watchdog → execution-mode validation → global live-trading kill switch → symbol-pause → idempotency (duplicate `cl_ord_id` blocking)
- **Write-ahead journaling**: `PLANNED → SUBMITTED` durably recorded *before* any exchange API call; replayed on restart
- Post-submit **reconciliation** against exchange readback (observe-only, never auto-trades)
- **`UNKNOWN` as a first-class state** — uncertainty is never treated as success; triggers lifecycle backstop alerts + symbol pause
- **Demo-first**: every strategy defaults to sandbox; live trading requires explicit `HERMX_LIVE_TRADING=true`
- Webhook auth: **constant-time secret match + optional HMAC-SHA256** over timestamp‖body with a **replay window**; per-source rate limiting, body-size guard, **jsonschema Draft 2020-12** validation, **24h dedup**
- All services bind **127.0.0.1 only**; public surface is a Tailscale Funnel HTTPS URL you own

### Layer 2 — Hermes (agent layer): conversational, command-driven, read-only by default, evolving toward risk intelligence

- **Telegram is the primary workspace.** You issue commands, pull status, trace signals, and manage strategies conversationally — no dashboard clicking required.
- Operator slash commands: `/hx-status`, `/hx-positions`, `/hx-strategy-list`, `/hx-trace`, `/hx-strategy-mode`, `/hx-close`, `/hx-emergency-stop`, `/hx-restart`, `/hx-upgrade`
- **Read-only by default on critical actions.** Hermes can relay signals and answer questions but **never** changes size/leverage/side or bypasses safety gates; it fails open to deterministic execution.
- All monitoring flows to Telegram: weekly/daily digests, reconcile gate, health watch, intake gate, and a **zero-intake (absence) alert** that catches "the signals stopped coming."
- **Built to evolve.** Today Hermes is an assistant (monitor, trace, manage). The design path takes it toward an advisor that incorporates risk dashboards and macro context — helping judge when to be aggressive, when to reduce risk, and when to stay flat — and eventually toward a self-evolving agent that participates in those decisions.

### Observability — the dashboard as a read-only layer

- Local **Next.js dashboard** for at-a-glance performance and logs: strategy cards, unified trade log, PnL, slippage, fees, live exchange position readback. It is where you *look*, not where you *operate*.

### Deployment & ops

- Docker Compose with published GHCR image; systemd services for receiver + dashboard
- **Auto-rollback deploy** (`deploy.sh`): snapshots config, stashes tracked files, health-checks, rolls back on failure
- Transaction state is **append-only and never rolled back** on deploy
- Append-only ledgers (`executions.jsonl`, `order-journal.jsonl`, `advisor-decisions.jsonl`, `shadow-intake.jsonl`), `latest.json` dashboard state, 44+ test files

---

## 4. Head-to-Head Comparison

### Interface & Paradigm

| Category | TradersPost | Kajinx + Hermes |
|---|---|---|
| **Interface paradigm** | Web dashboard + forms + clicks | **Telegram chat + conversational commands** |
| **Agent layer** | None | **Hermes — read-only by default, evolving from assistant → advisor → co-decision maker** |
| **Primary workspace** | Web app (dashboard is where you operate) | **Telegram chat** (dashboard is read-only observability) |
| **Interaction model** | Configure rules, then let alerts fire | **Command-driven**: issue orders, trace signals, manage strategies conversationally |
| **Monitoring & alerts** | Dashboard + email | **Telegram digests + AI agent + cron monitors + slash commands + zero-intake absence alerts** |
| **AI / operator tools** | None | **Evolving agent**, 9+ slash commands (`/hx-status`, `/hx-trace`, `/hx-emergency-stop`...), emergency stop, auto-rollback deploy |
| **Support model** | Email, video calls, enterprise SLA | **Self-serve by design — the agent is first-line support** |

### Execution & Safety

| Category | TradersPost | Kajinx + Hermes |
|---|---|---|
| **Execution correctness** | Cloud-managed (opaque) | **Write-ahead journal** (`PLANNED → SUBMITTED` before exchange call) + restart-safe WAL replay |
| **Safety gate model** | Broker-native SL/TP/trailing + max positions | **7-layer fail-closed gate chain** + global kill switch + symbol pause + idempotency |
| **Handling of uncertainty** | Not documented | **`UNKNOWN` as first-class state** — never counted as success; triggers alerts + auto-pause |
| **Reconciliation** | Broker sync (opaque) | **Explicit exchange readback**, observe-only, never auto-trades |
| **Duplicate/replay protection** | Not documented | **24h dedup + idempotent `cl_ord_id` + HMAC replay window** |
| **Webhook security** | Shared secret token in payload | **Constant-time secret + optional HMAC-SHA256 over timestamp‖body + per-source rate limiting** |
| **Crash recovery** | Managed by platform | **Append-only ledgers + durable journal replay** — zero acknowledged-transaction loss across restart |
| **Demo posture** | Paper broker included | **Demo-first by default** — every strategy starts in sandbox; live requires explicit `HERMX_LIVE_TRADING=true` |

### Asset Coverage & Markets

| Category | TradersPost | Kajinx + Hermes |
|---|---|---|
| **Crypto** | **Spot only** (Coinbase, Alpaca, Kraken, Bybit, Binance) | **Derivatives / perps with leverage** (OKX live, Bybit, KuCoin, Hyperliquid, Binance, Bitget, Gate.io, Coinbase via CCXT) |
| **Non-crypto** | **Stocks, options, futures** across 17+ brokers | ❌ Not yet (hybrid via SnapTrade on roadmap) |
| **Multi-account fan-out** | **✅ Core strength** — one alert → many live/paper/prop accounts | ❌ Single-operator, per-strategy (parallel execution on roadmap) |
| **Timeframe support** | ❌ Sub-1-minute banned; ~2s fills reported | **✅ No artificial floor** — async per-symbol queue, bound by your host + exchange |
| **Backtesting** | **✅ Built-in** (accuracy criticized by users) | ❌ Not included — use best-in-class external tools |
| **Built-in SL/TP/trailing** | **✅ Rich broker-native order types** | ⚠️ Signal-driven (not a first-class UI feature) |

### Infrastructure & Cost

| Category | TradersPost | Kajinx + Hermes |
|---|---|---|
| **Hosting model** | Cloud SaaS, shared multi-tenant | **Self-hosted**, 127.0.0.1-bound, Tailscale Funnel — your keys never leave your box |
| **Deployment safety** | Managed by platform | **Auto-rollback deploy** with config snapshot + append-only state that never rolls back |
| **Pricing** | **$0–$254/mo** tiered + $10/live + $5/paper add-ons | **No SaaS fee** — pay infra cost only (VPS + Tailscale) |
| **Onboarding** | **No-code, live in minutes** | Technical setup for operator-grade power and control |
| **Source code** | Closed-source black box | **Open, auditable Python** — every gate and journal writer is in code you can read |

---

## 5. Why Kajinx + Hermes Wins

1. **A new operating paradigm: command + agent instead of clicks and dashboards.** Most platforms force you into their web UI. Kajinx + Hermes puts the operator in a Telegram chat with an intelligent agent. You issue commands, get status, receive monitoring alerts, and interact with the system conversationally. The dashboard exists for at-a-glance performance and logs — not as the primary workspace.

2. **Hermes starts as assistant, grows into autonomous risk intelligence.** Unlike static automation platforms, Hermes is designed to evolve. It begins by helping you monitor, trace, and manage strategies via commands. Over time it can incorporate risk dashboards and macro context to advise — and eventually help decide — when to be aggressive, when to reduce risk, or when to stay flat. **TradersPost has no equivalent agent layer or evolutionary path.**

3. **Provable correctness at the execution layer + intelligent oversight at the agent layer.** You get both: a fail-closed, write-ahead-journaled execution engine *and* an agent that can develop higher-level judgment. Write-ahead journaling records `PLANNED → SUBMITTED` before the exchange call, and `UNKNOWN` is a first-class state that never gets counted as success. This combination — provable correctness underneath, evolving intelligence on top — is rare.

4. **You stay in control while gaining leverage from intelligence.** The agent is read-only by default on critical actions. It cannot bypass safety gates, change size, leverage, or side, and it fails open to deterministic execution. You always have the final say through commands, while gaining an always-available intelligent collaborator that improves over time.

5. **Crypto derivatives native, with a clear path to hybrid execution.** Built from day one for perps and leverage across OKX, Bybit, Hyperliquid, and more via CCXT — the leveraged-crypto niche TradersPost concedes (spot-only). The same agentic interface can eventually orchestrate across a hybrid CCXT + SnapTrade stack when you want TradFi exposure.

6. **Your infrastructure, your keys, your agent.** Self-hosted execution with all services bound to `127.0.0.1`, exposed only through a Tailscale Funnel URL you control, plus a sovereign agent that lives in *your* environment via Telegram. No shared multi-tenant SaaS black box; your API keys and order flow never leave infrastructure you own.

7. **No throttle, no "not for HFT" asterisk.** TradersPost explicitly disclaims high-frequency use, bans sub-1-minute timeframes, and may disable webhooks for "excessive requests," with ~2s fills reported. Kajinx's per-symbol async queue is bound only by your host and the exchange — no shared-tenant throttle, no artificial timeframe floor.

---

## 6. TradersPost Advantages We Don't Prioritize (Yet)

These are deliberate prioritization choices, not accidental gaps.

1. **Broad multi-asset coverage (stocks / futures / options).** TradersPost spans 17+ brokers across equities, options, and futures. **We are crypto-derivatives first.** Hybrid execution via SnapTrade is on the roadmap for when TradFi exposure matters.

2. **One-click multi-account fan-out.** TradersPost broadcasts one alert across many live/paper/prop accounts in parallel. **We can add parallel execution** — our focus is deeper per-strategy intelligence via the agent rather than simple broadcast.

3. **No-code onboarding.** TradersPost is live in minutes with no technical setup. **We optimize for capable operators** who want power and control over simplicity.

4. **Built-in backtester.** TradersPost ships a strategy tester. **We recommend best-in-class external tools**, and the agent layer can eventually incorporate forward-looking risk analysis rather than a bolted-on backtest.

5. **Managed support / SLA.** TradersPost offers email, video calls, and enterprise SLAs. **We are self-serve by design — the agent IS the first-line support**, and it gets better over time.

---

## 7. Recommended Positioning

**Tagline direction:** _"Kajinx + Hermes — an agent-native trading operations platform. A conversational, command-driven system where an intelligent agent becomes your co-pilot (and eventually co-decision maker) for crypto derivatives execution."_

**Target persona:** serious crypto operators who want an **agentic interface, not a dashboard** — technically capable, custody- and correctness-conscious, running leverage, and looking for an intelligent collaborator that lives where they work (Telegram) rather than a web app they have to babysit.

**Messaging pillars**

1. **Agent-native interface.** You operate through commands and a conversational agent in Telegram; the dashboard is read-only observability. This is a different paradigm from clicks-and-forms automation.

2. **Provable correctness.** Fail-closed gate chain, write-ahead journal, `UNKNOWN`-as-first-class, restart-safe replay. Built to be right when things go wrong.

3. **Crypto derivatives with leverage.** Purpose-built for perps and leverage across multiple venues — the niche TradersPost concedes.

4. **Sovereign infrastructure.** Your box, your keys, your agent. Self-hosted, 127.0.0.1-bound, no shared multi-tenant cloud.

5. **Evolutionary intelligence.** Hermes grows from assistant to advisor to co-decision maker, incorporating risk and macro context over time. The platform gets smarter with you.

**Be honest about fit.** Kajinx + Hermes is not the no-code, multi-asset, prop-firm, or "live in five minutes" product — that's TradersPost's home turf, and it's a genuinely different paradigm serving a different buyer. Where the two overlap (crypto TradingView automation), we win on **interface paradigm, correctness, custody, leverage, and evolving intelligence** — an agentic operations layer versus a web automation tool.

---

## Sources

- [TradersPost — home](https://traderspost.io/) · [TradingView signals](https://traderspost.io/signals/tradingview) · [Assets](https://traderspost.io/assets) · [Connections](https://traderspost.io/connections) · [Pricing](https://traderspost.io/pricing) · [Reference manual](https://traderspost.io/reference) · [Stop-loss type](https://traderspost.io/reference/strategy-field/stop-loss-type)
- [TradersPost docs — getting started](https://docs.traderspost.io/docs) · [FAQ](https://docs.traderspost.io/docs/additional-information/faq) · [Webhooks](https://docs.traderspost.io/docs/core-concepts/webhooks)
- [LuneFi — TradersPost 2026 review](https://lunefi.com/blog/traderspost-2026-full-review-no-code-trading-automation) · [TradingPlatforms.ai review](https://tradingplatforms.ai/review/traderspost-review) · [Trustpilot reviews](https://www.trustpilot.com/review/traderspost.io) · [AlgoWay TradersPost alternative](https://algoway.trade/traderspost-alternative)
- Kajinx + Hermes: HermX codebase (`src/webhook_receiver.py`, `src/executors/ccxt_adapter.py`, `src/execution/service.py`, `src/dashboard.py`, `.claude/CLAUDE.md`)
