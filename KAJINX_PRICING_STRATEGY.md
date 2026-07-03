# Kajinx + Hermes — Pricing Strategy (Future State with SnapTrade)

_Prepared 2026-07-03. Models the post-SnapTrade product: crypto derivatives (CCXT) **+** 20+ US brokerages and multi-account execution (SnapTrade SDK/API). Pricing research from vendor pages and third-party reviews (see Sources). Companion to [KAJINX_VS_TRADERPOST.md](KAJINX_VS_TRADERPOST.md)._

---

## 1. Executive Summary

**Recommended model: open-core + agent subscription + managed hosting.**

The single most important pricing fact we discovered is structural: **SnapTrade charges per *connected user*, not per API call or per trade.** The first 5 connected users are free; beyond that it is **$2/connected-user/month** (real-time + trading). A single sovereign operator connecting their own brokerages is **one connected user → $0 marginal cost.** This means the self-hosted path can offer full stocks/options/crypto execution with **zero pass-through cost** — a structural advantage no reseller-priced SaaS competitor can match.

That fact dictates the whole strategy:

- **Don't monetize execution.** Execution is a commodity race-to-the-bottom (open-source bots are free; TradersPost sells "seats"). The engine — Kajinx — stays **open source and free to self-host**, forever. This is also the philosophical non-negotiable.
- **Monetize the agent.** Hermes — the evolving intelligence layer — is the differentiator and the thing with real, growing marginal value (LLM inference, risk models, macro context). Charge for **intelligence**, not for the right to place an order.
- **Monetize convenience.** A managed/hosted tier for operators who want the power without running a box, where we absorb SnapTrade's $2/user and thin infra cost inside a fat-margin subscription.

**Headline prices:**

| Tier | Price | Who |
|---|---|---|
| **Sovereign** (open-source self-host) | **$0** | Technical operators; brings own SnapTrade free tier |
| **Hermes Pro** (agent subscription, any host) | **$39/mo** ($390/yr) | Serious operators who want the intelligent agent |
| **Kajinx Managed** (we host + Hermes Pro) | **$99/mo** | Operators who want power without infra |
| **Team / Enterprise** | **$349/mo+** | Prop desks, multi-operator, white-label, SLA |

At equivalent capability, **Managed ($99) undercuts TradersPost Premium ($254) by 60%** while being dramatically more powerful, and the **Sovereign path is free**. We win on both value and unit economics because our cost floor for the self-host segment is ~$0.

---

## 2. Competitive Pricing Landscape

### 2.1 TradersPost (the direct comparable)

Billed yearly (~15% off monthly):

| Plan | Yearly-equiv | Live | Paper | Asset classes |
|---|---|---|---|---|
| Free | $0 (7-day trial) | manual only | automated | — |
| Starter | ~$41.65/mo | 1 | 4 | 1 |
| Basic | ~$84.15/mo | 2 | 6 | 2 |
| Pro | ~$169.15/mo | 3 | 8 | 3 |
| Premium | ~$254.15/mo | 6 | 10 | all |

**Hidden / additive costs:** +$10/mo per extra live account, +$5/mo per extra paper (up to 50). A prop trader running 10 live accounts on Premium pays **$254 + $40 = ~$294/mo**. Asset-class gating means a crypto+stocks+futures operator is forced to Pro/Premium regardless of account count. **The pricing axis is "seats × asset classes"** — you pay more as you *do more of the same thing*.

### 2.2 SnapTrade (our upstream cost input)

| Plan | Price | Terms |
|---|---|---|
| Free | $0 | 1 connected user, real-time data, **20 brokerage connections**, trading, Discord support |
| Pay-as-you-go (real-time + trading) | **$2 / connected user / mo** | first **5 users free**, unlimited users, no contract |
| Pay-as-you-go (daily, read-only) | $1 / connected user / mo | first 5 free; manual sync $0.05/sync |
| Custom | from **$1,000/mo** | 1,000 users, volume discounts, higher rate limits, priority support |

**The decisive detail:** billing is **per end-user who links a brokerage**, *not* per API request or per executed order. Implications:

- **Self-hosted single operator = 1 connected user = $0** (inside both the free tier and the 5-free PAYG window). Even connecting Robinhood + Schwab + Fidelity is still *one* connected user with multiple brokerage connections.
- **Kajinx-hosted marginal cost = $2/customer/mo** until we cross ~500 hosted customers, where the Custom plan ($1,000/mo flat) becomes cheaper than PAYG.
- There is **no per-trade tax** — high-frequency signal flow does not increase SnapTrade cost. This aligns perfectly with a subscription (not usage) model on our side.

### 2.3 Other TradingView-automation alternatives

| Tool | Model | Entry | Top tier | Notes |
|---|---|---|---|---|
| **Alertatron** | Cloud SaaS | ~$59/mo (free: 5 alerts/day) | ~$199/mo | Crypto-only, "<1s" fills, closest crypto comparable |
| **3Commas** | Cloud SaaS | ~$22/mo | ~$50–130/mo | DCA/grid + signal bot |
| **Bitsgap** | Cloud SaaS | ~$29/mo | ~$50–130/mo | Broad exchange support |
| **WunderTrading** | Cloud SaaS | Free / $19 Basic | ~$89 Premium | Strong TradingView signal bot |
| **Cryptohopper** | Cloud SaaS | Free / $29 | ~$129 Hero | Marketplace + AI as up-sell |
| **Pionex** | Exchange-native | Free | — | 0.05% trading fee only; funds custodied on exchange |
| **OctoBot** | Open-core | Self-host free | $9.99/mo cloud | Closest OSS analog; no correctness/agent layer |
| **Hummingbot / OpenAlgo / robswc** | OSS self-host | Free | — | Frameworks, not products; no agent, no fail-closed guarantees |
| **QuantVPS** (infra proxy) | VPS hosting | $59/mo | ~$399/mo | Proves operators pay $59–400/mo *just for infra/latency* |

### 2.4 What the landscape tells us about willingness to pay

1. **The market clears at $30–$130/mo for cloud automation**, $200–$300 for multi-account/prop, and $0 for OSS self-host. There is a clear **"serious tier" ceiling around $250–300/mo** that TradersPost Premium and Alertatron top-end both hit.
2. **Operators already pay $59–$400/mo for pure infrastructure** (QuantVPS). Willingness-to-pay for *correctness + latency + control* is demonstrably high and separable from the trading logic itself.
3. **OSS is free but never a product** — every open-source tool is a framework you must assemble and babysit. **Nobody sells the gap between "free framework" and "$250 SaaS."** That gap — a free, correct, self-hostable engine with a paid intelligence layer — is exactly where Kajinx + Hermes sits.
4. **AI/intelligence is consistently the up-sell** (Cryptohopper gates AI to Hero; WunderTrading bundles it into Premium). The market already accepts **"pay more for smarter."** Hermes is a purer version of that value.

---

## 3. Kajinx Value Proposition at Different Price Points

**Free (open-source self-host):** the entire *execution* value. Kajinx engine, all 7 fail-closed gates, WAL journaling, `UNKNOWN`-as-first-class, reconciliation, crypto perps via CCXT, **stocks/options via SnapTrade (user's own free tier)**, the read-only dashboard, and **deterministic** Hermes commands (`/hx-status`, `/hx-close`, `/hx-emergency-stop`, digests). Everything you need to run a correct operation, sovereign, at $0. This is the moat *and* the marketing — the free tier is genuinely full-featured, GitLab-style.

**Paid (the intelligence layer):** the *evolving* Hermes — LLM-driven advisor, risk intelligence, macro context, anomaly narration, natural-language operation, and the roadmap toward co-decision-making. This is not a feature-gate on execution; it is a genuinely different, compounding capability that costs us real money (inference) and delivers growing value.

**Premium (scale + convenience + assurance):** managed hosting, multi-account fan-out, multi-operator/team controls, white-label, priority support and SLA — the things prop desks and less-technical operators will pay a premium for.

The line is clean: **free = correctness & control; paid = intelligence & convenience.** We never charge someone for the right to place their own order on their own keys.

---

## 4. Recommended Pricing Tiers — Three Model Options

### Option A — Pure Open Source + Donations (rejected)
Fully free, sponsor/donation funded (OpenBB-lite).
- **Pros:** maximal trust, zero friction, strongest sovereignty signal.
- **Cons:** no durable revenue; can't fund inference costs of the agent; agent layer (the actual differentiator) never gets resourced. Target ARPU ≈ $0–3. **Not viable** for a product whose edge is a compute-hungry AI.

### Option B — Pay-Per-Execution / Usage-Based (rejected as primary)
Meter on orders or signals (e.g. $0.01/order or credits).
- **Pros:** usage-based models grow ~29% faster; aligns cost with activity.
- **Cons:** **directly contradicts our upstream economics** — SnapTrade does *not* charge us per trade, so a per-trade tax is pure margin extraction that punishes exactly the high-frequency operators we court (the ones TradersPost bans). It also reintroduces the "shared-tenant throttle" feeling we position *against*. Metering the agent's *inference* (not the trades) is defensible, but too complex as a headline. **Reject as the primary axis; keep as an enterprise lever.**

### Option C — Open-Core + Agent Subscription + Managed Hosting (RECOMMENDED)
Engine open/free; Hermes intelligence as flat subscription; hosting as premium convenience; team/enterprise on top.
- **Pros:** monetizes the differentiator (agent) not the commodity (execution); flat pricing suits operators who hate per-trade surprises; managed tier absorbs SnapTrade's tiny per-user cost at fat margin; every tier undercuts TradersPost at equivalent capability; philosophically consistent (self-host stays free and sovereign).
- **Cons:** self-host users may never convert (mitigated — the agent is a *recurring compute cost* they can't easily replicate, and hosting is genuinely convenient); requires we operate inference reliably.
- **Target ARPU:** blended ~$55–70/mo across paying users; **conversion:** ~8–15% of active self-host installs → Hermes Pro, higher for Managed among non-technical inbound.
- **Positioning:** "free and correct forever; pay for the brain, or pay us to run it."

**Recommendation: Option C**, with Option B's usage lever reserved for enterprise (inference-heavy or high-connected-user deployments).

---

## 5. Pricing Model Recommendation

**Adopt open-core + agent subscription + managed hosting (Option C).**

**Why it beats TradersPost on value:** at the capability TradersPost charges $169–254/mo for (multi-asset, multi-account), Kajinx delivers **more** (crypto derivatives *with leverage*, fail-closed correctness, WAL replay, an evolving agent, sovereignty) at **$0 self-hosted** or **$99 managed**. TradersPost's axis is "pay more to do more of the same"; ours is "the base is free and correct; pay for intelligence." A buyer comparing feature-for-feature sees a 60% price cut *and* a capability superset.

**Why it beats TradersPost on unit economics:** our marginal cost per self-host user is **~$0** (they bring their own SnapTrade free tier, run their own VPS, hold their own keys). TradersPost carries multi-tenant infra, broker integration maintenance, and support for every seat. We only take on marginal cost when we choose to (Managed tier), and there it's **~$2 SnapTrade + a few dollars of VPS/inference against a $99 price** — >90% gross margin. We can undercut them indefinitely because our cost floor is structurally lower.

**How it captures the serious operator without alienating them:** serious operators *distrust* seat-based SaaS that meters their edge and *value* correctness and control. Giving them the full engine free — auditable, sovereign, no throttle — earns the trust that converts them to Hermes Pro voluntarily. They pay for the agent because it's a compounding compute asset (risk intelligence, macro context) that's genuinely hard to self-build, not because they're forced past a paywall to trade. **We monetize their respect, not their lock-in.**

**The SnapTrade cost question — hybrid pass-through:**
- **Sovereign / self-host:** **pass-through, but effectively $0.** The operator uses SnapTrade's own free tier (1 connected user, 20 brokerage connections, trading included). We add no markup. Their TradFi execution is free because SnapTrade's per-user model makes a single operator free.
- **Managed:** **absorbed.** We pay PAYG $2/connected-user/mo and fold it into the $99 fee. One managed customer is typically 1 connected user → $2 cost → trivial.
- **Enterprise / high-scale hosted:** **absorbed with a volume lever.** Past ~500 hosted connected users the SnapTrade Custom plan ($1,000/mo flat) beats PAYG; enterprise pricing carries that as a line item, optionally passed through for very large white-label deployments.

Net: **SnapTrade never becomes a per-trade tax on the operator**, and it never threatens margin because its cost scales with *customers we're already charging*, not with trade volume.

---

## 6. Pricing Tiers (Specific Numbers)

### 🟢 Sovereign — $0 (open source, self-hosted)
**Included:**
- Full Kajinx engine: 7-gate fail-closed chain, WAL journaling, `UNKNOWN` state, reconciliation, idempotency, HMAC/replay/constant-time auth
- Crypto derivatives + leverage via CCXT (OKX, Bybit, KuCoin, Hyperliquid, …)
- **Stocks & options via SnapTrade** using the operator's own SnapTrade free tier (20 broker connections, trading)
- Read-only Next.js dashboard; append-only ledgers; auto-rollback deploy
- **Deterministic Hermes**: slash commands, digests, health/absence alerts, emergency stop
- Community support (Discord/GitHub)

**Target customer:** technical, custody-conscious operators. **Marginal cost to us: ~$0.** This tier is the moat and the funnel.

### 🔵 Hermes Pro — $39/mo ($390/yr, save ~17%)
**Everything in Sovereign, plus the intelligent agent (self-host *or* managed):**
- LLM-driven conversational operation and natural-language commands
- Advisor-grade **risk intelligence**: exposure, correlation, drawdown, regime/macro context
- Anomaly narration, intelligent digests, "why did this happen" tracing
- Priority access to the evolving agent roadmap (assistant → advisor → co-decision)
- Email support

**Target customer:** the serious operator who wants the brain. **Our cost:** LLM inference (~$3–8/user/mo) → healthy margin. Priced below every "AI tier" competitor (Cryptohopper Hero $129, Alertatron $199) while doing more.

### 🟣 Kajinx Managed — $99/mo ($990/yr)
**Everything in Hermes Pro, but we run it:**
- Hosted execution engine + Hermes Pro (no VPS to manage)
- SnapTrade PAYG **absorbed** (multi-broker TradFi + crypto perps out of the box)
- Managed upgrades, backups, uptime monitoring
- Multi-account execution (fan-out to several linked brokerages)
- Priority support

**Target customer:** operators who want the power and correctness without running infrastructure. **Undercuts TradersPost Pro/Premium ($169–254) by 40–60% with a superset of capability.** Our cost: ~$2 SnapTrade + ~$10–20 infra/inference → >75% margin.

### 🟠 Team / Enterprise — from $349/mo
**Everything in Managed, plus:**
- Multi-operator seats, role controls, shared strategy library
- Multi-account fan-out across many live/paper/prop accounts (the TradersPost prop use-case, done better)
- **White-label** Hermes/dashboard; custom exchange or broker integrations
- SLA, dedicated support channel, onboarding
- Optional inference/usage lever for very high-volume or high-connected-user deployments (maps to SnapTrade Custom at scale)

**Target customer:** prop desks, trading teams, signal-service operators reselling to their audience. Priced above TradersPost Premium *only where we deliver team + white-label + SLA it can't match.*

**Ladder at a glance:** $0 → $39 → $99 → $349+. Every rung is cheaper than the TradersPost rung of equivalent capability, and each adds intelligence/convenience rather than merely more seats.

---

## 7. Revenue Model Mechanics

**How Kajinx monetizes despite an open, self-hosted core:**

1. **Hermes Pro subscription (primary).** The agent needs recurring inference and improving models — a genuine recurring cost that justifies a recurring price and is hard for a user to replicate. This is the flagship revenue line and the one most **aligned with product philosophy**: we charge for *intelligence we operate*, never for the user's own execution on their own keys.
2. **Managed hosting (secondary, highest ARPU).** Convenience + SnapTrade absorption + ops. Converts the non-technical slice of inbound that would otherwise pick TradersPost.
3. **Team/Enterprise & white-label (highest LTV).** Prop desks and signal-services who need seats, fan-out, SLA, and their own brand. This is also where SnapTrade's Custom plan and any usage lever live.
4. **Support / integration contracts (opportunistic).** Custom exchange or broker adapters, onboarding, retainers — classic open-core services revenue; take selectively, don't build the business on it.

**Most philosophy-aligned:** **#1 (agent subscription).** It preserves sovereignty absolutely — the user can *always* self-host the full engine and run deterministic Hermes for free — while charging only for the layer that is (a) our genuine differentiator, (b) a real recurring cost, and (c) optional. It monetizes the brain, not the hands. **We explicitly do not monetize execution, seats, or trades.**

**Why not per-execution as primary:** SnapTrade doesn't charge us per trade, high-frequency operators are our beachhead, and metering trades reintroduces the throttle dynamic we position against. Keep any usage metering on *inference* and only at enterprise scale.

---

## 8. Risk Factors & Mitigations

| Risk | Mitigation |
|---|---|
| **Self-host cannibalizes paid** — everyone runs free, nobody buys | The free tier is deterministic-only; **Hermes Pro's intelligence is a live compute asset**, not a code feature they can fork. Managed adds genuine ops value. Expect 8–15% conversion — healthy for open-core; the free base *is* the funnel and the trust engine, not lost revenue. |
| **SnapTrade raises per-user price or changes model** | Self-host is insulated (user's own SnapTrade account). Managed cost is $2/user on a $99 price — absorbs a 3–5× increase before margin hurts. CCXT crypto path has **no** SnapTrade dependency, so the core product survives any SnapTrade change. |
| **SnapTrade per-user cost at hosted scale** | Below ~500 hosted users, PAYG $2/user; above, switch to Custom ($1,000 flat) — cheaper per user as we grow. Enterprise pricing carries it explicitly. Cost scales with paying customers, never with trades. |
| **TradersPost undercuts on price** | Structurally can't win the floor — our self-host is **$0** and our managed cost basis is ~$12 vs their multi-tenant + broker-maintenance + support burden per seat. If they cut to $99, we still have free self-host and a capability superset (leverage, correctness, agent). We compete on **paradigm**, not price wars. |
| **Inference cost balloons on Hermes Pro** | Tiered model routing (cheap models for routine narration, premium for risk reasoning); cache digests; per-account inference budget on enterprise. Price already carries 4–10× the inference cost. |
| **Trust erosion — "open source but nickel-and-dimed"** | Hard rule, stated publicly: **execution is free forever; we only charge for intelligence and hosting.** Keep the free tier genuinely full-featured (correctness, all gates, all venues). Never gate a safety feature behind a paywall. |
| **Support load from free users** | Community-first (Discord/GitHub); Hermes *is* first-line support and improves over time; paid tiers get email/priority. Self-serve by design is a feature, not a cost problem. |
| **Regulatory/liability of a paid execution product** | We sell the *agent and hosting*, not trading advice or custody; keys and custody stay with the operator (self-host) or with SnapTrade OAuth (no keys stored). Reinforces the "your keys, your box" positioning legally as well as technically. |

---

## Bottom Line

SnapTrade's per-connected-user pricing hands us a structural gift: **TradFi execution is essentially free for a sovereign operator, and ~$2/customer when we host.** That collapses the cost of the thing TradersPost sells seats for. So we give execution away — open source, self-hosted, correct, sovereign — and charge for the one thing that actually compounds in value and cost: **Hermes, the intelligence layer.** Free where they charge, cheaper where we overlap, and monetizing the brain instead of the order. That's a position TradersPost's seat-and-asset-class model cannot structurally answer.

---

## Sources

- [SnapTrade — Pricing](https://snaptrade.com/pricing) · [Home](https://snaptrade.com/) · [Docs](https://docs.snaptrade.com/) · [Developer Terms](https://snaptrade.com/developer-terms-of-use)
- [TradersPost — Pricing](https://traderspost.io/pricing) (see [KAJINX_VS_TRADERPOST.md](KAJINX_VS_TRADERPOST.md))
- [Alertatron](https://alertatron.com/) · [FINESTEL — Alertatron review 2026](https://finestel.com/blog/alertatron-review/) · [flipr.cloud — Alertatron alternative](https://flipr.cloud/alertatron-alternative)
- [3Commas](https://3commas.io/) · [Blockster — Crypto trading bots 2026](https://blockster.com/crypto-trading-bots-in-2026-ranked-reviewed-compared-beginners-to-pros) · [uncoded.ch — bot platform comparison](https://uncoded.ch/blogs/3commas-vs-cryptohopper-vs-bitsgap-vs-wundertrading-an)
- [WunderTrading — Pricing](https://wundertrading.com/en/account/subscription/pricing) · [Cryptohopper review 2026](https://cryptoadventure.com/cryptohopper-review-2026-cloud-trading-bots-marketplace-strategies-and-pricing/)
- [OctoBot](https://github.com/Drakkar-Software/OctoBot) · [Hummingbot](https://hummingbot.org/) · [robswc/tradingview-webhooks-bot](https://github.com/robswc/tradingview-webhooks-bot) · [OpenAlgo](https://github.com/marketcalls/openalgo) · [CoinCodeCap — free OSS bots](https://coincodecap.com/free-open-source-trading-bots)
- [QuantVPS — trading VPS pricing/latency](https://www.quantvps.com/blog/best-vps-algorithmic-trading)
- [Monetizely — monetizing open-source / open-core pricing](https://www.getmonetizely.com/articles/monetizing-open-source-software-pricing-strategies-for-open-core-saas) · [Revenera — SaaS pricing models 2026](https://www.revenera.com/blog/software-monetization/saas-pricing-models-guide/) · [dev.to — OSS monetization 2026](https://dev.to/zny10289/open-source-software-monetization-how-developers-are-actually-making-money-in-2026-4ddh)
