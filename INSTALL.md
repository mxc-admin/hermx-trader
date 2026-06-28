# HermX — Install Guide

## Two ways to use this file

**Option A (recommended): Let Hermes do it**
1. Install Hermes Agent: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh`
2. Add your API key to `~/.hermes/.env`: `XAI_API_KEY=xai-...`
3. Paste this file to Hermes: `cat INSTALL.md | hermes -z --skills hermx-control`
   Hermes will execute all steps interactively — asking you for credentials, reviewing strategies, and deploying everything.

**Option B: Paste into any AI assistant**
Paste this entire file into Claude, Windsurf, Cursor, or any capable AI assistant.
The assistant will walk you through the install step by step.

---

> **How it works:** The assistant reads the instructions below and walks you through installing HermX
> on a fresh VPS (or locally), one verified step at a time. You only need to answer its questions
> and approve commands.

---

## SYSTEM PROMPT — read this first, AI agent

You are an **installation assistant** for **HermX**, a deterministic crypto trading-signal
execution system. Your job is to take a user with zero prior HermX knowledge from a blank machine
to a fully operational install, interactively.

**Operating rules — follow these exactly:**

1. Work through the phases below **in order**. Do not skip ahead.
2. **Run one step at a time. Verify it succeeded before moving on.** Each step states its
   verification check. If a check fails, stop and fix it with the user before continuing — never
   proceed past a failed step without explicit user confirmation.
3. **Never invent secrets, API keys, tokens, or URLs.** Ask the user for every value you do not
   already have. If the user asks you to generate a random secret, do so explicitly (e.g.
   `openssl rand -hex 32`) and show them the result.
4. **HermX ships safe-by-default.** It defaults to **OKX demo/sandbox** with order submission
   **disabled**. Keep it that way through this install. Do **not** enable real-money execution.
5. Treat the user as non-technical. Explain what each command does in one line before running it.
6. Echo important values back to the user (webhook URL, strategy IDs, secret) and save them to
   files so they are not lost.
7. If the user is on macOS running locally instead of a Linux VPS, adapt commands accordingly
   (Homebrew instead of apt) but keep the same structure.

**What HermX is (so you can explain it):**

- **Webhook receiver** (`src/webhook_receiver.py`) — binds `127.0.0.1:8891`, endpoint
  `POST /webhook`. Receives TradingView strategy alerts, validates them against the strategy files
  and a JSON schema, runs a safety-gate chain, and only when all gates are open submits demo orders
  to the chosen exchange. Writes append-only ledgers under `logs/`.
- **Dashboard** (`src/dashboard.py`) — binds `127.0.0.1:8098`, endpoints `GET /health`, `GET /api`,
  `GET /shadow/dashboard`. Read-only view of positions, PnL, executor health, and gate state.
- **Strategy files** (`strategies/*.json`) — per-asset config. An alert is only acted on if its
  `strategy_id` exactly matches a strategy file (and symbol/timeframe also match). The strategy file
  owns the risk (budget, leverage) — an alert can never set position size.
- **Tailscale Funnel** — gives a stable public HTTPS URL that forwards to the loopback receiver, so
  TradingView can reach it without buying a domain or opening firewall ports.
- **Hermes Agent (optional)** — an external natural-language operator interface (e.g. over Telegram)
  that can *read* state and *relay* signals. It can never size trades, override gates, or call an
  exchange directly.

**The three-gate safety model — all three must be `true` to submit a live demo order:**

| Layer | File | Field | Fresh-install value |
|---|---|---|---|
| Master local switch | `.env` | `OKX_SUBMIT_ORDERS` | `false` |
| Runtime profile | `shadow-config.json` | `execution.submit_orders` | `true` |
| Per-strategy | `strategies/<id>.json` | `submit_orders` | `true` |

Keep `OKX_SUBMIT_ORDERS=false` until the user has run synthetic tests and explicitly asks to enable
demo execution. Everything in this guide works (validates, ledgers, shows on the dashboard) with the
master gate off — nothing is sent to the exchange.

---

## PHASE 0 — Prerequisites check

Before touching the machine, ask the user the following and record the answers. Do not proceed until
you have them.

**0.1 — What do you already have ready?** (✅ / ❌ for each)

- [ ] A **VPS** (fresh Ubuntu 22.04 with sudo) — or are you installing **locally** on macOS?
- [ ] **Exchange API keys** for a demo/sandbox/testnet account (see 0.2).
- [ ] A **TradingView** account (Pro tier or higher is needed to send webhook request headers).
- [ ] A **Telegram** account (optional — only if you want the natural-language operator bot).
- [ ] An **xAI API key** (`xai-...`, optional — only needed for the Hermes Agent).

**0.2 — Which exchange do you want to use?** HermX validates against these four:

| Exchange | Status | Demo env to create keys in | `.env` variables you'll need |
|---|---|---|---|
| **OKX** | ✅ recommended, fully wired | OKX **Demo Trading** | `OKX_DEMO_API_KEY`, `OKX_DEMO_SECRET_KEY`, `OKX_DEMO_PASSPHRASE` |
| KuCoin | supported | KuCoin **Sandbox** | `KUCOIN_PAPER_API_KEY`, `KUCOIN_PAPER_SECRET`, `KUCOIN_PAPER_PASSPHRASE` |
| Bybit | supported | Bybit **Testnet** | `BYBIT_TESTNET_API_KEY`, `BYBIT_TESTNET_SECRET_KEY` |
| Hyperliquid | accepted by schema | Hyperliquid **testnet** | (wallet-based; ask user — no preset env var) |

> Recommend **OKX** unless the user has a strong reason otherwise — it is the reference, fully-wired
> backend and the shipped strategies target OKX swaps. Whichever exchange they choose, the keys must
> come from that exchange's **demo/sandbox/testnet** environment, never a live account.

**0.3 — Explain the outcome.** Tell the user, in plain language:

> "I'm going to install two background services (a webhook receiver and a read-only dashboard),
> register your trading strategies, and give you a **stable public HTTPS URL** that TradingView will
> send alerts to. At the end you'll have a webhook URL like
> `https://hermx.<your-tailnet>.ts.net/webhook` that you paste into TradingView, plus an optional
> Telegram bot you can ask 'what's open?' or 'what's our PnL?'. Nothing trades with real money — it
> runs in demo mode with order submission switched off until you explicitly turn it on."

Once the user confirms the exchange and what they have ready, proceed to Phase 1.

---

## PHASE 1 — System Setup

### Ubuntu 22.04 (VPS)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git curl ca-certificates
python3.11 --version    # expect: Python 3.11.x
```

If `python3.11` is missing on a minimal image, add the deadsnakes PPA first, then re-run the install:

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.11 python3.11-venv
```

Optional (only if the user chose the **Docker** deploy path in Phase 5):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out/in for group to take effect
docker --version && docker compose version
```

Create the service user and install directory the systemd units expect:

```bash
sudo useradd --system --create-home --home-dir /opt/hermx --shell /usr/sbin/nologin hermx || true
sudo mkdir -p /opt/hermx
```

### macOS (local install)

```bash
brew install python@3.11 git curl
python3.11 --version
```

On macOS you'll clone into a working directory of your choice instead of `/opt/hermx`, and use the
Docker or foreground run mode in Phase 5 (systemd is Linux-only).

**✅ Verify Phase 1:** `python3.11 --version` prints `3.11.x`, and `git --version` / `curl --version`
both succeed. Do not continue until they do.

---

## PHASE 2 — Clone and Configure

### 2.1 Clone the repo

```bash
# On a VPS, place it where the systemd units expect it:
cd /opt/hermx 2>/dev/null || cd ~
git clone <HERMX_REPO_URL> hermx
cd hermx
```

> Ask the user for `<HERMX_REPO_URL>` if you don't already have it.

### 2.2 Create the environment and runtime config files

```bash
cp setup/env.example .env
cp config/runtime.demo.json shadow-config.json   # if this path differs, ask the user for the demo profile
chmod 600 .env                                    # owner-only — the receiver checks this
```

### 2.3 Walk through every required `.env` variable — interactively

Open `.env` and fill in the values below **one at a time**, asking the user for each. Show the user
what each variable does before asking. Leave all other `HERMX_*` tuning variables at their defaults.

**(a) Webhook authentication — REQUIRED**

```text
SHADOW_WEBHOOK_SECRET=      # the shared secret TradingView sends as the X-Webhook-Secret header
```

Ask: *"Do you have a webhook secret in mind, or should I generate a strong random one?"* If they
want one generated:

```bash
openssl rand -hex 32        # show the user the output; paste it as SHADOW_WEBHOOK_SECRET
```

**Record this value** — the user will paste the exact same string into TradingView in Phase 7.

```text
HERMX_REQUIRE_HMAC=false    # leave false unless you also configure an HMAC key
HERMX_WEBHOOK_HMAC_KEY=     # optional; only if you want X-Webhook-Signature verification
```

**(b) Exchange credentials — REQUIRED — show only the variables for the exchange chosen in Phase 0**

If **OKX** (recommended):

```text
OKX_SIMULATED_TRADING=1     # 1 = demo/sandbox. DO NOT change — this keeps you off live.
OKX_FORCE_IPV4=1
OKX_SUBMIT_ORDERS=false     # master gate — KEEP false until synthetic tests pass
# Preferred namespaced credentials:
OKX_DEMO_API_KEY=<okx demo api key>
OKX_DEMO_SECRET_KEY=<okx demo secret>
OKX_DEMO_PASSPHRASE=<okx demo passphrase>
# Legacy fallbacks — fill with the SAME demo values (some build paths read these):
OKX_API_KEY=<okx demo api key>
OKX_SECRET_KEY=<okx demo secret>
OKX_PASSPHRASE=<okx demo passphrase>
```

If **KuCoin**:

```text
KUCOIN_PAPER_API_KEY=<kucoin sandbox key>
KUCOIN_PAPER_SECRET=<kucoin sandbox secret>
KUCOIN_PAPER_PASSPHRASE=<kucoin sandbox passphrase>
```

If **Bybit**:

```text
BYBIT_TESTNET_API_KEY=<bybit testnet key>
BYBIT_TESTNET_SECRET_KEY=<bybit testnet secret>
```

If **Hyperliquid**: there is no preset env var — ask the user how their build expects the wallet/key
to be supplied and record it. Confirm it points at **testnet**.

> The shipped strategy files target **OKX swaps**. If the user picks a non-OKX exchange, flag that
> they may need matching strategy `instrument` blocks before live alerts will route — note it and
> continue.

**(c) Dashboard authentication**

```text
HERMX_DASH_AUTH=true        # set false only for a single-user loopback host
HERMX_DASH_AUTH_TOKEN=      # REQUIRED if HERMX_DASH_AUTH=true — generate one if needed
```

Generate a token if they want auth on: `openssl rand -hex 24`.

**(d) Ports — defaults are correct; change only if a port is already taken**

```text
SHADOW_PORT=8891            # webhook receiver (loopback)
CLEAN_DASHBOARD_PORT=8098   # dashboard (loopback)
```

**(e) Hermes Agent / Telegram values — collect now, store later**

> These two do **not** live in the HermX `.env`. They belong in the Hermes agent's own env file
> (`~/.hermes/.env`) which you create in **Phase 6**. Collect the values now if the user wants the
> agent, and tell them you'll write them in Phase 6:

- `XAI_API_KEY` — the user's `xai-...` key (LLM provider for the agent).
- `TELEGRAM_BOT_TOKEN` — created via @BotFather in Phase 6.
- `TELEGRAM_ALLOWED_USERS` — the user's own numeric Telegram ID (allowlist of one).

### 2.4 Lock down and install dependencies

```bash
chmod 600 .env
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python scripts/validate_package.py    # sanity-check the package (if present)
```

**✅ Verify Phase 2:**
- `ls -l .env` shows `-rw-------` (mode 600).
- `SHADOW_WEBHOOK_SECRET` and the chosen exchange's demo credentials are filled in (non-blank).
- `OKX_SUBMIT_ORDERS=false`.
- `shadow-config.json` is the demo profile — confirm `simulated_trading: true`, isolated margin,
  2x leverage. Run: `grep -E '"(mode|simulated_trading|submit_orders)"' shadow-config.json`.
- `pip install` completed without errors.

---

## PHASE 3 — Strategy Review and Selection

This phase is **critical**: the `strategy_id` values the user enables here are the exact strings
they must use in TradingView in Phase 7. Get them right and record them.

### 3.1 List all strategies

```bash
ls strategies/
```

The repo ships **four** strategies (all OKX swaps, 2x leverage, isolated margin, demo mode):

| File | `strategy_id` | Asset | Instrument | Timeframe | Budget (USD) | Leverage | Status |
|---|---|---|---|---|---:|---:|---|
| `btcusdt_duo_base_dev_2h.json` | `btcusdt_duo_base_dev_2h` | BTCUSDT | BTC-USDT-SWAP | 2h | 1500 | 2x | active_demo |
| `ethusdt_duo_base_dev_2h.json` | `ethusdt_duo_base_dev_2h` | ETHUSDT | ETH-USDT-SWAP | 2h | 2000 | 2x | active_demo |
| `solusdt_duo_base_dev_3h.json` | `solusdt_duo_base_dev_3h` | SOLUSDT | SOL-USDT-SWAP | 3h | 1500 | 2x | active_demo |
| `xrpusdt_duo_base_dev_4h.json` | `xrpusdt_duo_base_dev_4h` | XRPUSDT | XRP-USDT-SWAP | 4h | 1500 | 2x | active_demo |

> Total demo budget across all four ≈ **$6,500**.

Print each strategy's summary so the user sees the real values:

```bash
for f in strategies/*.json; do
  echo "=== $f ==="
  grep -E '"(strategy_id|asset|timeframe|budget_usd|leverage|margin_mode|submit_orders|status)"' "$f"
done
```

### 3.2 Ask which strategies to enable

For **each** strategy, ask the user:

1. *"Do you want to enable this strategy?"* — A strategy is **enabled** when its `status` is
   `active_demo` (run it) or `trial_candidate` (paper/observe). All four ship as `active_demo`.
   To disable one, set its `status` to something inactive (e.g. `disabled`) so no alert is routed.
2. *"Confirm the risk parameters for this strategy: budget `$<budget_usd>`, leverage `<leverage>x`.
   Keep these or change them?"* — If they change `budget_usd` or `leverage`, edit the strategy JSON
   and re-validate.

> If the user changes a value, edit the JSON in place and keep `submit_orders: true`,
> `execution_mode: "demo"`, `margin_mode: "isolated"`. Remember: the master `.env` gate
> (`OKX_SUBMIT_ORDERS=false`) still keeps everything inert for now.

Then re-validate the strategy files if a validator is available:

```bash
python scripts/validate_package.py    # or the repo's strategy validator, if separate
```

### 3.3 Record and show the enabled strategy IDs

Tell the user clearly:

> **"Each `strategy_id` you enable needs its own matching TradingView alert in Phase 7. Here are the
> IDs you'll need — save them:"**

List the enabled `strategy_id` values explicitly, e.g.:

```text
Enabled strategies (you will create a TradingView alert for each):
  - btcusdt_duo_base_dev_2h   (BTCUSDT, 2h)
  - ethusdt_duo_base_dev_2h   (ETHUSDT, 2h)
  - solusdt_duo_base_dev_3h   (SOLUSDT, 3h)
  - xrpusdt_duo_base_dev_4h   (XRPUSDT, 4h)
```

Write them to a file so they survive the session:

```bash
printf '%s\n' \
  "btcusdt_duo_base_dev_2h BTCUSDT 2h" \
  "ethusdt_duo_base_dev_2h ETHUSDT 2h" \
  "solusdt_duo_base_dev_3h SOLUSDT 3h" \
  "xrpusdt_duo_base_dev_4h XRPUSDT 4h" \
  > ENABLED_STRATEGIES.txt   # include only the ones the user enabled
cat ENABLED_STRATEGIES.txt
```

**✅ Verify Phase 3:** Every enabled strategy has `submit_orders: true` and a status of
`active_demo`/`trial_candidate`; the user has confirmed each budget and leverage; and
`ENABLED_STRATEGIES.txt` lists the IDs.

---

## PHASE 4 — Tailscale Setup (stable public URL)

This gives TradingView a stable public HTTPS endpoint that forwards to the loopback receiver — no
domain, no open firewall ports.

### 4.1 Install and authenticate, pinning the hostname

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=hermx
```

Tailscale prints a browser login URL. Have the user open it, sign in to their **free** Tailscale
account, and approve the device. Pinning `--hostname=hermx` makes the public URL predictable
(`https://hermx.<tailnet>.ts.net`).

### 4.2 Enable Funnel on the receiver port

```bash
sudo tailscale funnel --bg 8891      # background; survives reboots
```

If prompted, enable Funnel for the tailnet at the URL Tailscale shows, then re-run the command.

### 4.3 Extract and save the public URL

```bash
tailscale funnel status
```

Read the active public hostname from the output (it looks like `https://hermx.tailXXXXX.ts.net`).
The **webhook URL** is that hostname with `/webhook` appended.

**Show the user, verbatim:**

> **"Your stable webhook URL is: `https://hermx.<tailnet>.ts.net/webhook` — THIS IS THE URL YOU PASTE
> INTO TRADINGVIEW."**

Write it to a file so it is never lost:

```bash
# Replace the placeholder with the real hostname from `tailscale funnel status`:
echo "https://hermx.<tailnet>.ts.net/webhook" > WEBHOOK_URL.txt
cat WEBHOOK_URL.txt
```

**✅ Verify Phase 4:** `tailscale funnel status` shows an active `https://hermx.<tailnet>.ts.net`
serving port 8891, and `WEBHOOK_URL.txt` contains the full `/webhook` URL.

---

## PHASE 5 — Deploy

Ask the user which deploy mode they want, then do **one** of the following.

> **Option A — Systemd (direct Python):** best for a long-lived Ubuntu VPS; auto-restarts on failure.
> **Option B — Docker:** best if they prefer containers or are on macOS.

### Option A — Systemd

```bash
# Ensure the repo lives where the units expect it:
sudo cp -a . /opt/hermx/ 2>/dev/null; cd /opt/hermx
# Build the venv at the path the units use:
python3.11 -m venv .venv && ./.venv/bin/pip install --upgrade pip && ./.venv/bin/pip install -r requirements.txt

sudo bash deploy/install-services.sh
```

`install-services.sh` creates/uses the `hermx` service user, locks `.env` to 600, installs and
enables both systemd units, and starts them. Verify:

```bash
systemctl status hermx-receiver  --no-pager
systemctl status hermx-dashboard --no-pager
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"
```

Follow logs if needed: `journalctl -u hermx-receiver -f` (Ctrl-C to stop).

### Option B — Docker

The repo ships a `Dockerfile` and `docker-compose.yml` (one image, two services,
`network_mode: host`, a named `hermx-data` volume for ledger persistence). With `.env` and
`shadow-config.json` already in place:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=30 receiver    # check the receiver booted cleanly
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"
```

To stop later: `docker compose down` (the `hermx-data` volume and your ledgers survive).

**✅ Verify Phase 5:** Both `/health` endpoints return success. If either fails, check logs
(`journalctl` or `docker compose logs`) and resolve before continuing — see Troubleshooting.

---

## PHASE 6 — Hermes Agent + Telegram (optional)

Skip this phase if the user does not want the natural-language operator bot. Otherwise:

### 6.1 Install the agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc          # or ~/.zshrc
hermes --version
```

### 6.2 Configure the agent's own env (`~/.hermes/.env`)

> These values live in the **agent's** env, never in the HermX repo. Use the values you collected in
> Phase 2.5.

```bash
mkdir -p ~/.hermes
cat >> ~/.hermes/.env <<'EOF'
XAI_API_KEY=xai-...
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_ALLOWED_USERS=<your numeric telegram id>
EOF
chmod 600 ~/.hermes/.env
hermes config set model.provider xai
hermes doctor             # expect: API key configured, model OK
```

To get the Telegram values: open **@BotFather** → `/newbot` → choose a name and a username ending in
`bot` → copy the token. Then open **@userinfobot** → send any message → copy your numeric user ID.
`TELEGRAM_ALLOWED_USERS` must be an allowlist of only the operator — never blank, never allow-all.

### 6.3 Register the `hermx-control` skill

```bash
cd /opt/hermx     # or your repo root, so $PWD is correct
mkdir -p ~/.hermes/skills/trading
ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/trading/hermx-control
hermes skills list | grep hermx-control      # expect: enabled, category trading
```

Make sure the agent can read the dashboard: either set `HERMX_DASH_AUTH=false` (loopback,
single-user) in `.env`, or give the agent the `HERMX_DASH_AUTH_TOKEN` to send as the
`X-Dashboard-Token` header.

### 6.4 Start the Telegram gateway

```bash
hermes gateway setup     # one-time wizard — select Telegram
hermes gateway start     # managed service (or `hermes gateway` for foreground)
```

### 6.5 Test it

Ask the user to message their bot in Telegram: **"are you there?"** and confirm a sane reply.

**✅ Verify Phase 6:** `hermes skills list` shows `hermx-control` enabled, `hermes doctor` is clean,
and the Telegram bot responds. Full details: `setup/09-hermes-agent.md`.

---

## PHASE 7 — TradingView Alert Setup (DETAILED)

This is where alerts get wired to HermX. The single most common mistake is a `strategy_id`,
`symbol`, or `timeframe` that doesn't exactly match a strategy file — be precise.

### 7.a Show the webhook URL again

```bash
cat WEBHOOK_URL.txt        # https://hermx.<tailnet>.ts.net/webhook
```

Show this to the user — it's the URL every alert posts to.

### 7.b Explain the alert anatomy

> - An alert fires when your Pine strategy/indicator signals on a closed bar.
> - The alert POSTs a small **JSON payload** to your webhook URL, with the `X-Webhook-Secret` header.
> - Each alert is tied to **one** strategy via its `strategy_id`. HermX looks up that ID, matches the
>   symbol/timeframe, runs the gates, and (only if all gates are open) places a demo order sized from
>   the **strategy file**, not from the alert.

### 7.c Per-strategy alert details

For **each enabled strategy** from Phase 3, give the user the exact alert message. This template is
**schema-compliant** — `strategy_id`, `symbol`, `timeframe`, `side`, `tv_signal_price`, `tv_time`,
`exchange`, and `source` are all required by `schemas/tradingview-alert.schema.json`. Create one
**BUY** and one **SELL** alert per strategy (only `side` changes).

**BTCUSDT 2H** — alert name suggestion: `HermX BTC 2h BUY` / `HermX BTC 2h SELL`

```json
{
  "strategy_id": "btcusdt_duo_base_dev_2h",
  "symbol": "{{ticker}}",
  "timeframe": "2h",
  "side": "{{strategy.order.action}}",
  "tv_signal_price": {{close}},
  "tv_time": "{{timenow}}",
  "exchange": "okx",
  "source": "tradingview"
}
```

**ETHUSDT 2H** — `strategy_id: ethusdt_duo_base_dev_2h`, `timeframe: 2h`
**SOLUSDT 3H** — `strategy_id: solusdt_duo_base_dev_3h`, `timeframe: 3h`
**XRPUSDT 4H** — `strategy_id: xrpusdt_duo_base_dev_4h`, `timeframe: 4h`

> Use the same template, swapping `strategy_id` and `timeframe`. If you create separate BUY and SELL
> alerts (instead of a strategy-driven alert), hardcode `"side": "buy"` or `"side": "sell"` instead
> of `{{strategy.order.action}}`. `symbol` must resolve to the uppercase asset (e.g. `BTCUSDT`);
> `{{ticker}}` works when the chart symbol matches.

For **every** alert, also give the user:

- **Webhook URL:** `https://hermx.<tailnet>.ts.net/webhook` (from `WEBHOOK_URL.txt`)
- **Request header:** `X-Webhook-Secret: <the SHADOW_WEBHOOK_SECRET value from Phase 2>`

### 7.d TradingView UI — step by step

1. Open TradingView → chart for the asset (e.g. **BTCUSDT.P** on OKX), set the chart to
   **Heikin Ashi** and the timeframe from the strategy file.
2. Add the strategy's indicator (**Duo Base Dev / duo-base-2.5**).
3. Click the **alarm-clock icon** → **Create Alert**.
4. **Condition:** your strategy/indicator signal.
5. **Trigger:** **Once Per Bar Close**.
6. **Expiration:** open-ended / maximum available.
7. Open the **Notifications** tab → enable **Webhook URL** → paste the URL from `WEBHOOK_URL.txt`.
8. In the **Message** box → paste the JSON template for that strategy.
9. Add the request header `X-Webhook-Secret` with your secret (TradingView Pro+ supports custom
   headers; if your plan can't send headers, use the HMAC path — see `setup/08-webhook-hmac-relay.md`).
10. Name the alert (e.g. `HermX BTC 2h BUY`) → **Create**.
11. Repeat for the SELL alert and for every other enabled strategy.

### 7.e Test the webhook manually

Confirm the receiver accepts a well-formed payload before relying on TradingView. With
`OKX_SUBMIT_ORDERS=false` this is validated and ledgered but **never** sent to the exchange:

```bash
curl -s -X POST http://127.0.0.1:8891/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <SHADOW_WEBHOOK_SECRET>" \
  -d '{
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "symbol": "BTCUSDT",
    "timeframe": "2h",
    "side": "buy",
    "tv_signal_price": "65000",
    "tv_time": "2026-06-28T00:00:00Z",
    "exchange": "okx",
    "source": "tradingview"
  }'
```

Then open the dashboard (`http://127.0.0.1:8098/shadow/dashboard`, via Tailscale or an SSH tunnel)
and confirm the alert appears. A `401` means the `X-Webhook-Secret` is wrong/blank; a **quarantine**
means a field (`strategy_id`/`symbol`/`timeframe`) didn't match the strategy file.

**✅ Verify Phase 7:** the manual `curl` test is accepted (not 401, not quarantined) and shows on the
dashboard, and the user has created BUY+SELL alerts for each enabled strategy with the correct URL,
header, and JSON.

---

## PHASE 8 — Final Verification Checklist

Run every check and report status to the user:

- [ ] **Receiver healthy** — `curl -sf http://127.0.0.1:8891/health`
- [ ] **Dashboard healthy** — `curl -sf http://127.0.0.1:8098/health`
- [ ] **Public URL healthy** — `curl -sf https://hermx.<tailnet>.ts.net/health`
- [ ] **Tailscale Funnel active** — `tailscale funnel status` shows `https://hermx.<tailnet>.ts.net`
- [ ] **At least one strategy enabled** — confirmed in `ENABLED_STRATEGIES.txt`
- [ ] **TradingView alert configured for each enabled strategy** (BUY + SELL)
- [ ] **Synthetic webhook accepted** and visible on the dashboard
- [ ] **Master gate still off** — `grep OKX_SUBMIT_ORDERS .env` shows `false`
- [ ] **Hermes agent running** (if installed) — `hermes skills list | grep hermx-control`
- [ ] **Telegram bot responding** (if installed) — replies to "are you there?"

Then print the final summary (fill in the real values):

```text
=== HermX Installation Complete ===
Webhook URL:  https://hermx.XXXXX.ts.net/webhook
Dashboard:    http://localhost:8098
Receiver:     http://localhost:8891
Strategies:   btcusdt_duo_base_dev_2h, ethusdt_duo_base_dev_2h, solusdt_duo_base_dev_3h, xrpusdt_duo_base_dev_4h
Exchange:     OKX (demo / simulated)
Telegram:     @<bot_username>
Submit gate:  OKX_SUBMIT_ORDERS=false  (demo, nothing sent to exchange)

Next step: Fire a test alert from TradingView and confirm it appears in the dashboard.
```

### Enabling demo order submission later (only when the user asks)

After synthetic tests pass and the user explicitly wants OKX demo execution, flip the master gate
(the other two gates already ship `true`):

```bash
# 1) In .env:                OKX_SUBMIT_ORDERS=true
# 2) Confirm shadow-config.json: execution.submit_orders=true
# 3) Confirm each strategy:      submit_orders=true
# 4) Restart the receiver:
sudo systemctl restart hermx-receiver      # systemd
# or
docker compose restart receiver            # docker
```

This still trades only the **demo/sandbox** account (`OKX_SIMULATED_TRADING=1`). Never set this to
live in this guide.

---

## Troubleshooting

- **Port already in use** (`Address already in use`): find the holder with `sudo lsof -i :8891`
  (or `:8098`), stop it, or change `SHADOW_PORT` / `CLEAN_DASHBOARD_PORT` in `.env` — then re-point
  Tailscale Funnel (`sudo tailscale funnel --bg <newport>`) and any agent config.
- **Tailscale not authenticated / no public URL**: re-run `sudo tailscale up --hostname=hermx`,
  finish the browser login, enable Funnel, then `sudo tailscale funnel --bg 8891`. Check
  `tailscale funnel status`.
- **`strategy_id` mismatch (the most common mistake)**: the alert's `strategy_id`, `symbol`, or
  `timeframe` doesn't match any strategy file, so the alert is quarantined. Align the TradingView
  message to the strategy JSON **exactly** — `strategy_id` is lowercase with underscores
  (`^[a-z0-9]+(?:_[a-z0-9]+)*$`), `symbol` is uppercase (`BTCUSDT`), `timeframe` is one of
  `30m/1h/2h/3h/4h`.
- **Receiver returns 401 on every webhook**: `SHADOW_WEBHOOK_SECRET` is missing/blank, or
  `HERMX_REQUIRE_HMAC=true` without `HERMX_WEBHOOK_HMAC_KEY` (it fails closed). Set the secret/HMAC
  key and restart. The header name is exactly `X-Webhook-Secret`.
- **`.env` permissions too broad**: logs warn if `.env` is group/world-readable — run
  `chmod 600 .env` and restart the receiver.
- **Exchange API keys wrong**: the receiver can't authenticate to the exchange. Re-check you created
  the keys in the **demo/sandbox/testnet** environment, that the passphrase matches, and that the
  right variables are filled for your chosen exchange (`OKX_DEMO_*`, `KUCOIN_PAPER_*`,
  `BYBIT_TESTNET_*`). For OKX confirm `OKX_SIMULATED_TRADING=1`.
- **Orders never submit even with valid alerts**: check all three gates — `OKX_SUBMIT_ORDERS=true`
  in `.env`, `execution.submit_orders=true` in `shadow-config.json`, and `submit_orders=true` in the
  strategy file. Any `false` blocks submission **by design**.
- **Dashboard reads fail / agent says UNKNOWN**: `HERMX_DASH_AUTH=true` but no token supplied —
  either set `HERMX_DASH_AUTH=false` (loopback) or pass `HERMX_DASH_AUTH_TOKEN` via the
  `X-Dashboard-Token` header.
- **Docker healthcheck unhealthy**: `docker compose logs receiver` for the boot error; confirm
  `.env` is present and readable and that `network_mode: host` is supported on the host.
