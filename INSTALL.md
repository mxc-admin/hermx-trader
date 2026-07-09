# HermX — Install Guide

## Two ways to use this file

**Option A (recommended): Let Hermes do it**
1. Install Hermes Agent: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh`
2. Configure your LLM provider:
   ```
   hermes setup
   ```
   Follow the prompts — supports xAI/Grok, OpenAI, Anthropic, Ollama, and others.
   You will need an API key for your chosen provider.
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
  `GET /dashboard/`. Read-only view of positions, PnL, executor health, and gate state.
- **Strategy files** (`strategies/*.json`) — per-asset config. An alert is only acted on if its
  `strategy_id` exactly matches a strategy file (and symbol/timeframe also match). The strategy file
  owns the risk (budget, leverage) — an alert can never set position size.
- **Tailscale Funnel** — gives a stable public HTTPS URL that forwards to the loopback receiver, so
  TradingView can reach it without buying a domain or opening firewall ports.
- **Hermes Agent (optional)** — an external natural-language operator interface (e.g. over Telegram)
  that can *read* state and *relay* signals. It can never size trades, override gates, or call an
  exchange directly.

**The execution-control model — two independent controls decide whether (and where) an order is placed:**

| Control | File | Field | Fresh-install value |
|---|---|---|---|
| Per-strategy mode | `strategies/<id>.json` | `execution_mode` | `"demo"` (sandbox) |
| Global live switch | `.env` | `HERMX_LIVE_TRADING` | `false` (unset = live disabled) |

`execution_mode: "demo"` always routes to the exchange sandbox/demo account — no real money, no global
switch needed. `execution_mode: "live"` routes to the real account and **additionally** requires
`HERMX_LIVE_TRADING=true`. Keep every strategy in `demo` (and `HERMX_LIVE_TRADING` unset) until the
user has run synthetic tests and explicitly asks to enable live execution. Everything in this guide
works (validates, ledgers, shows on the dashboard) in demo — nothing hits a live account.

---

> **Automated path:** Run `bash install.sh` from the repo root for a script-driven
> install covering Phases 1–5 + verification. This guide remains the reference for
> every decision the script makes — consult it when a step fails or you want to
> understand why.

---

## PHASE 0 — Prerequisites check

Before touching the machine, ask the user the following and record the answers. Do not proceed until
you have them.

**0.1 — What do you already have ready?** (✅ / ❌ for each)

- [ ] A **VPS** (fresh Ubuntu 22.04 with sudo) — or are you installing **locally** on macOS?
- [ ] **Exchange API keys** for a demo/sandbox/testnet account (see 0.2).
- [ ] A **TradingView** account (Pro tier or higher is needed to send webhook request headers).
- [ ] A **Telegram** account (optional — only if you want the natural-language operator bot).
- [ ] An **LLM provider API key** (xAI, OpenAI, Anthropic, etc., optional — only needed for the Hermes Agent).

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

Create the `hermx` service user and the install directory the systemd units expect. The user's home
is `/opt/hermx` (matching `WorkingDirectory=/opt/hermx` in the units and `install-services.sh`). Do
not let `useradd` populate the directory — Phase 2 clones the repo straight into it:

```bash
sudo useradd --system --home-dir /opt/hermx --shell /usr/sbin/nologin hermx || true
sudo mkdir -p /opt/hermx
sudo chown "$(id -un)" /opt/hermx   # lets you clone without sudo; install-services.sh hands it to hermx later
```

### macOS (local install)

```bash
brew install python@3.11 git curl
python3.11 --version
```

On macOS you'll clone into a working directory of your choice instead of `/opt/hermx`, and use the
foreground run mode in Phase 5 (Option A · Mac) — systemd is Linux-only. The default Docker compose now
uses bridge networking (it works on Docker Desktop for Mac); only the `docker-compose.host.yml` fallback
relies on `network_mode: host`, which Docker Desktop for Mac does not support.

**✅ Verify Phase 1:** `python3.11 --version` prints `3.11.x`, and `git --version` / `curl --version`
both succeed. Do not continue until they do.

---

## PHASE 2 — Clone and Configure

### 2.1 Clone the repo

```bash
# On a VPS, clone straight into the directory the systemd units expect (/opt/hermx):
git clone https://github.com/mxc-admin/hermx-trader.git /opt/hermx
cd /opt/hermx
```

On **macOS / local dev**, clone wherever you like instead:

```bash
git clone https://github.com/mxc-admin/hermx-trader.git ~/hermx
cd ~/hermx
```

> Cloning directly into `/opt/hermx` means `WorkingDirectory=/opt/hermx` in the systemd units and the
> `.venv` you build next line up exactly — no copying the tree around later.

### 2.2 Create the environment and runtime config files

```bash
cp setup/env.example .env
cp config/runtime.demo.json engine-config.json   # if this path differs, ask the user for the demo profile
chmod 600 .env                                    # owner-only — the receiver checks this
```

### 2.3 Walk through every required `.env` variable — interactively

Open `.env` and fill in the values below **one at a time**, asking the user for each. Show the user
what each variable does before asking. Leave all other `HERMX_*` tuning variables at their defaults.

**(a) Unified secret — REQUIRED**

```text
HERMX_SECRET=              # ONE secret for BOTH webhook and dashboard auth
                          #   - webhook: sent by a direct TradingView alert as the "secret_key"
                          #     JSON body field (default); or as an X-Webhook-Secret header via a relay
                          #   - dashboard: used as the X-Dashboard-Token / Bearer / Basic password
```

Ask: *"Do you have a secret in mind, or should I generate a strong random one?"* If they
want one generated:

```bash
openssl rand -hex 32        # show the user the output; paste it as HERMX_SECRET
```

**Record this value** — the user will paste the exact same string into TradingView in Phase 7,
and use it to log into the dashboard.

> `run.sh` generates `HERMX_SECRET` automatically on first run if it is blank, and `bash run.sh
> --new-secret` regenerates it on demand. There is no automatic time-based rotation.

> **Single secret — and its blast radius.** `HERMX_SECRET` is the only source for both the
> webhook (body `secret_key` field, or `X-Webhook-Secret` header via a relay) and dashboard
> auth; there are no legacy fallbacks. If it is
> blank, auth fails closed (every webhook gets `401` and protected dashboard routes return
> `401`). Because **one** value guards **both** surfaces, a leak exposes the intake *and* the
> dashboard at once — rotate immediately on any suspicion with `bash run.sh --new-secret`
> (which rotates both in one step). See `setup/07-security-rotation.md`.

> **Recommended posture: HMAC on for non-loopback.** A loopback-only deploy (everything on
> `127.0.0.1`, reached via a single Tailscale Funnel) can run with `HERMX_REQUIRE_HMAC=false`.
> But if you bind a non-loopback interface (`HERMX_BIND_HOST=0.0.0.0` or a LAN IP), set
> `HERMX_REQUIRE_HMAC=true` and an `HERMX_WEBHOOK_HMAC_KEY` — the shared secret alone is
> replayable, and HMAC adds per-request signature + replay-freshness. The receiver logs a
> SECURITY warning at boot if it binds non-loopback with HMAC off.

```text
HERMX_REQUIRE_HMAC=false    # leave false ONLY for loopback-only deploys; true for non-loopback
HERMX_WEBHOOK_HMAC_KEY=     # required when HMAC is on; adds X-Webhook-Signature verification
```

**(b) Exchange credentials — REQUIRED — show only the variables for the exchange chosen in Phase 0**

If **OKX** (recommended):

```text
HERMX_LIVE_TRADING=false    # global live switch — KEEP false; demo strategies route to the sandbox regardless
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
```

The dashboard authenticates with the same `HERMX_SECRET` from step (a) — there is no separate
dashboard token. Send it as the `X-Dashboard-Token` header, or as the Bearer / Basic password.

**(d) Ports — defaults are correct; change only if a port is already taken**

```text
HERMX_RECEIVER_PORT=8891    # webhook receiver (loopback)
HERMX_DASHBOARD_PORT=8098   # dashboard (loopback)
# Legacy aliases (deprecated, still honored for backward compatibility):
# SHADOW_PORT=8891          # deprecated alias for HERMX_RECEIVER_PORT
# CLEAN_DASHBOARD_PORT=8098 # deprecated alias for HERMX_DASHBOARD_PORT
```

**(e) Hermes Agent / Telegram values — collect now, store later**

> These two do **not** live in the HermX `.env`. They belong in the Hermes agent's own env file
> (`~/.hermes/.env`) which you create in **Phase 6**. Collect the values now if the user wants the
> agent, and tell them you'll write them in Phase 6:

- An **LLM provider API key** (xAI, OpenAI, Anthropic, etc.) — for the agent's chosen provider.
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
- `HERMX_SECRET` and the chosen exchange's demo credentials are filled in (non-blank).
- `HERMX_LIVE_TRADING=false` (or unset) — live execution disabled; demo strategies route to the sandbox.
- `engine-config.json` is the demo profile — confirm it carries the `strategy_engine` block:
  `grep -E '"(strategy_engine|strategies_dir|require_strategy_id)"' engine-config.json`.
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

| File | `strategy_id` | Asset | Instrument | Timeframe | Budget (USD) | Leverage | `execution_mode` |
|---|---|---|---|---|---:|---:|---|---|
| `btcusdt_duo_base_dev_2h.json` | `btcusdt_duo_base_dev_2h` | BTCUSDT | BTC-USDT-SWAP | 2h | 1500 | 2x | demo |
| `ethusdt_duo_base_dev_2h.json` | `ethusdt_duo_base_dev_2h` | ETHUSDT | ETH-USDT-SWAP | 2h | 1500 | 2x | demo |
| `solusdt_duo_base_dev_3h.json` | `solusdt_duo_base_dev_3h` | SOLUSDT | SOL-USDT-SWAP | 3h | 1500 | 2x | demo |
| `xrpusdt_duo_base_dev_4h.json` | `xrpusdt_duo_base_dev_4h` | XRPUSDT | XRP-USDT-SWAP | 4h | 1500 | 2x | demo |

> Total demo budget across all four ≈ **$6,000**. These are `schema_version: 2` strategy files — the
> instrument and budget live in nested blocks (`instrument.inst_id`, `capital.budget_usd`); there is
> **no `asset` or `status` field**. A strategy is inert only when its file is removed from `strategies/`.

Print each strategy's summary so the user sees the real values:

```bash
for f in strategies/*.json; do
  echo "=== $f ==="
  grep -E '"(strategy_id|inst_id|timeframe|budget_usd|leverage|margin_mode|execution_mode)"' "$f"
done
```

### 3.2 Ask which strategies to enable

For **each** strategy, ask the user:

1. *"Do you want to enable this strategy?"* — A strategy is **enabled** when its file is present
   in `strategies/` (and `execution_mode` decides where it routes — `demo` = sandbox). All four ship with
   `execution_mode: "demo"`. To make one inert, remove its file from `strategies/`.
2. *"Confirm the risk parameters for this strategy: budget `$<budget_usd>`, leverage `<leverage>x`.
   Keep these or change them?"* — If they change `budget_usd` or `leverage`, edit the strategy JSON
   and re-validate.

> If the user changes a value, edit the JSON in place and keep `execution_mode: "demo"`,
> `margin_mode: "isolated"`. Remember: the master `.env` gate
> (`HERMX_LIVE_TRADING=false`) keeps live execution disabled — demo strategies route to the sandbox.

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

**✅ Verify Phase 3:** Every enabled strategy has `execution_mode: "demo"`;
the user has confirmed each budget and leverage; and `ENABLED_STRATEGIES.txt` lists the IDs.

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

### 4.4 Funnel the dashboard on its own public port (Option A)

The webhook funnel above publishes the receiver on `:443`. Tailscale Funnel only permits
`443`, `8443`, and `10000` as public ports, so the **dashboard gets its own Funnel on
`:8443`** (the webhook keeps `:443`), forwarding to the loopback dashboard on `8098`:

```bash
sudo tailscale funnel --bg --https=8443 8098
```

The dashboard is then reachable at:

> `https://hermx.<tailnet>.ts.net:8443/dashboard/`

Because this URL is public, it **still requires the unified secret** — send the
`HERMX_SECRET` value from `.env` (Phase 2.3a) as the `X-Dashboard-Token` header,
or as the Bearer/Basic password. `bash install.sh` enables this funnel for you (prompting,
or automatically when `TS_AUTHKEY` is set), and `bash run.sh` does the same for local smoke
runs; both save the URL to `DASHBOARD_URL.txt`. Save it manually otherwise:

```bash
# Replace the placeholder with the real hostname from `tailscale funnel status`:
echo "https://hermx.<tailnet>.ts.net:8443/dashboard/" > DASHBOARD_URL.txt
cat DASHBOARD_URL.txt
```

**✅ Verify 4.4:** `tailscale funnel status` shows a second entry on `:8443` serving port
`8098`, and opening the URL with the token returns the dashboard (a `401` means the token is
missing or wrong).

---

## PHASE 5 — Deploy HermX Services

> **Not sure? Use Option A. It works everywhere.**
>
> Prefer the automated path? Run `bash install.sh` from the repo root instead.

There are exactly **two** ways to run HermX:

| Option | Where | What you get |
|--------|-------|--------------|
| **A — Source install** | **VPS *and* Mac** | systemd on a Linux VPS; foreground processes on Mac/local |
| **B — Docker** | **VPS (and Mac)** | Image-isolated containers, bridge networking, Tailscale sidecar |

Option A is the **base path** and works on every platform. Option B (the default
bridge compose) also runs anywhere Docker runs, including Docker Desktop for Mac;
only its `docker-compose.host.yml` fallback is Linux-only (see Option B).

Ask the user which option they want, then do **one** of the following.

### Option A — Source install (VPS: systemd · Mac: foreground)

This is the path that works everywhere. On a Linux VPS it installs OS-supervised
systemd services; on macOS/local it runs the two processes in the foreground.

#### A · VPS (systemd)

You already cloned to `/opt/hermx` and built `/opt/hermx/.venv` in Phase 2, which is exactly where the
units expect them — so deployment is just running the installer:

```bash
cd /opt/hermx
sudo bash deploy/install-services.sh
```

`install-services.sh` creates/uses the `hermx` service user (home `/opt/hermx`), takes ownership of the
tree, locks `.env` to 600, installs and enables both systemd units, and starts them. Verify:

```bash
systemctl status hermx-receiver  --no-pager
systemctl status hermx-dashboard --no-pager
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"
```

Follow logs if needed: `journalctl -u hermx-receiver -f` (Ctrl-C to stop).

To update a running systemd/bare-metal host later, use the config-safe deploy script rather than
a raw `git pull`:

```bash
cd /opt/hermx
bash deploy/deploy.sh
```

`deploy/deploy.sh` snapshots operator config, pulls, runs `pip install`, rebuilds the UI, runs the
offline test gate, restarts the services, and **auto-rolls back** to the prior commit if the
post-restart health check fails.

#### A · Mac / local (foreground)

No supervisor — you run the two processes directly in two terminals and stop them with Ctrl-C. The
processes read configuration from the environment (there is no auto-loaded `.env`), so source `.env`
into each shell first. Paths are resolved from the repo root, so running from `src/` is correct.

```bash
# From the repo root you cloned in Phase 2:
cd ~/hermx                                   # or wherever you cloned it
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Terminal 1 — webhook receiver:
cd src && set -a && source ../.env && set +a && ../.venv/bin/python webhook_receiver.py

# Terminal 2 — dashboard:
cd src && set -a && source ../.env && set +a && ../.venv/bin/python dashboard.py
```

Then probe both health endpoints from a third terminal:

```bash
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"
```

Stop either service with Ctrl-C in its terminal.

### Option B — Docker (bridge networking + Tailscale)

The repo ships a `Dockerfile` and `docker-compose.yml`: one image, three services
(`receiver`, `dashboard`, and a `tailscale` sidecar). The Docker path uses
**Tailscale** as its tunnel — the same tunnel you run directly; **cloudflared is
not used**. Key properties of the default compose:

- **Bridge networking** (no `network_mode: host`). Each service binds `0.0.0.0`
  *inside* its container (`HERMX_BIND_HOST=0.0.0.0`), and host port publishing is
  pinned to `127.0.0.1:8891` / `127.0.0.1:8098` — reachable from the host but never
  exposed on a public interface.
- **Non-root runtime** — processes run as the `hermx` user (uid/gid `10001`).
- **Read-only config + strategies** — `engine-config.json` and `strategies/` are
  bind-mounted **`:ro`** into both services; edit them on the host, then restart.
- **Two named volumes** — both services set `HERMX_DATA_DIR=/app/data`. `hermx-data` holds
  the append-only ledgers/logs (`/app/logs`, receiver rw, dashboard ro); `hermx-state` holds
  the four mutable state snapshots (`/app/data`: `paper-state.json`, `control-state.json`,
  `seen-signals.json`, `latest.json`). The dashboard shares `hermx-state` **rw** (writable
  even with its `read_only: true` root fs) so live mode overrides it writes to
  `control-state.json` land in the same volume the receiver reads.
- **Hardened dashboard** — `read_only: true`, `cap_drop: [ALL]`, `tmpfs: /tmp`.
- **Public ingress via Tailscale** — the `tailscale` sidecar joins your tailnet as
  `hermx` using `TS_AUTHKEY` from `.env` and proxies traffic per
  `config/tailscale/serve.json`: **Funnel** publishes the receiver publicly
  (`https://hermx.<tailnet>.ts.net/webhook` → `receiver:8891`) and **serve** exposes
  the dashboard to the tailnet only (`https://hermx.<tailnet>.ts.net:8443/` →
  `dashboard:8098`). It runs in userspace-networking mode, so it needs no extra host
  capabilities or `/dev/net/tun` (works on Linux and Docker Desktop for Mac).
  cloudflared is **not** part of this path.

**Tailscale quick-start (default public ingress):**

1. In the Tailscale **admin console** → **Settings → Keys**, generate a
   **reusable** (and optionally **ephemeral**) **auth key**.
2. Set it in `.env`: `TS_AUTHKEY=tskey-...` (the installer's Docker branch prompts
   for this). `TS_STATE_DIR` is optional and defaults to `/var/lib/tailscale`.
3. To expose the receiver to the public internet for TradingView, enable **Funnel**
   for your tailnet (admin console → **DNS/Settings**; Funnel must be allowed in the
   tailnet's ACL). The serve config already requests Funnel on `:443`.

With `.env`, `engine-config.json`, and `TS_AUTHKEY` in place:

> Source clones build locally; for a repo-less install use Option C below.

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=30 receiver      # check the receiver booted cleanly
docker compose logs --tail=30 tailscale     # confirm the node authed + serve/funnel is up
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"
```

Your stable webhook URL is `https://hermx.<tailnet>.ts.net/webhook`. To stop later:
`docker compose down` (the `hermx-data`, `hermx-state`, and `tailscale-state` volumes
and your ledgers/state/node identity survive).

> **Host-networking fallback.** If the host already runs Tailscale (so the
> receiver/dashboard on `127.0.0.1` are reachable on the tailnet without a sidecar),
> use the legacy compose instead: `docker compose -f docker-compose.host.yml up -d`.
> That file keeps `network_mode: host` and has no tunnel sidecar. Docker Desktop for
> Mac does **not** implement `network_mode: host` the way Linux does, so the fallback
> is Linux-only; the default bridge compose works anywhere Docker runs.

### Option C — Docker package (no repo clone)

For a fresh VPS where you want HermX without cloning the source. One command pulls the
published image, seeds an `/opt/hermx` install dir from it, walks you through `.env`, and
starts the stack:

```bash
curl -fsSL https://raw.githubusercontent.com/mxc-admin/hermx-trader/main/scripts/install-docker.sh | bash
```

You will be prompted for: the exchange (1–8), its demo/sandbox/testnet credentials, and a
Tailscale auth key. A `HERMX_SECRET` is generated for you. `HERMX_LIVE_TRADING=false` is
written by default — nothing reaches a live exchange.

What ends up on the VPS (`/opt/hermx/`):

```
/opt/hermx/
├── docker-compose.yml          # extracted from the image
├── docker-compose.host.yml     # host-networking fallback
├── .env                        # your secrets (chmod 600)
├── engine-config.json          # baked baseline; edit to tune the strategy engine
├── strategies/                 # seeded from the image; edit these post-install
│   ├── btcusdt_duo_base_dev_2h.json ...
└── config/tailscale/serve.json # tailscale sidecar serve/funnel config
```
Named volumes (survive updates): `hermx_hermx-data`, `hermx_hermx-state`, `hermx_tailscale-state`.

Verify, update, and edit:

```bash
cd /opt/hermx
docker compose ps
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"

# Update to a new release (state is preserved by the named volumes):
docker compose pull && docker compose up -d

# Edit strategies / engine config, then apply:
nano strategies/btcusdt_duo_base_dev_2h.json
docker compose restart
```

#### Migrating an existing Docker deployment

If you are upgrading a host that previously ran the old `network_mode: host` compose:

- **Volume ownership.** Containers now run as uid/gid `10001` (`hermx`), so the
  pre-existing `hermx-data` volume (written by root before) must be re-owned once:

  ```bash
  docker compose down
  docker run --rm -v hermx_hermx-data:/v alpine chown -R 10001:10001 /v
  docker compose up -d --build
  ```

  (Replace `hermx_hermx-data` with the actual volume name from `docker volume ls`;
  Compose prefixes it with the project directory name.)
- **State snapshots.** The four mutable JSON files (`paper-state.json`,
  `control-state.json`, `seen-signals.json`, `latest.json`) were **never persisted**
  in the old compose — they lived inside the container and were lost on every
  recreate. There is therefore **nothing to migrate**: the new `hermx-state` volume
  starts empty and the receiver rebuilds state from the journaled ledgers on boot.

**✅ Verify Phase 5:** Both `/health` endpoints return success. If either fails, check logs
(`journalctl`, `docker compose logs`, or the terminal output) and resolve before continuing — see
Troubleshooting.

---

## PHASE 6 — Hermes Agent + Telegram (optional)

Skip this phase if the user does not want the natural-language operator bot. Otherwise:

### 6.1 Install the agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc          # or ~/.zshrc
hermes --version
```

### 6.2 Seed the agent's identity

By default a fresh Hermes instance uses Nous Research's generic default identity, with
no awareness it's operating HermX. `skills/hermx-identity/` ships a ready-made split
(validated against Nous's own SOUL.md vs AGENTS.md guidance — identity/tone stays
global, HermX mechanics stay project-scoped):

```bash
cd /opt/hermx     # or your repo root, so $PWD is correct
cp skills/hermx-identity/SOUL.md ~/.hermes/SOUL.md   # identity, tone, style (global)
cp skills/hermx-identity/AGENTS.md ./AGENTS.md       # HermX mechanics (repo-scoped)
```

`~/.hermes/SOUL.md` only exists if `hermes` has already run once (it auto-seeds a
starter on first run) — overwrite it here. This takes effect on the agent's next
session/restart, so do it before `hermes gateway start` (6.5). See
`skills/hermx-identity/README.md` for why the content is split this way.

### 6.3 Configure the agent's own env (`~/.hermes/.env`)

> These values live in the **agent's** env, never in the HermX repo. Use the values you collected in
> Phase 2.5.

```bash
mkdir -p ~/.hermes
# Configure your LLM provider (xAI/Grok, OpenAI, Anthropic, Ollama, etc.):
hermes setup
# Then add your Telegram values:
cat >> ~/.hermes/.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_ALLOWED_USERS=<your numeric telegram id>
EOF
chmod 600 ~/.hermes/.env
hermes doctor             # expect: API key configured, model OK
```

To get the Telegram values: open **@BotFather** → `/newbot` → choose a name and a username ending in
`bot` → copy the token. Then open **@userinfobot** → send any message → copy your numeric user ID.
`TELEGRAM_ALLOWED_USERS` must be an allowlist of only the operator — never blank, never allow-all.

### 6.4 Register the `hermx-control` skill

```bash
cd /opt/hermx     # or your repo root, so $PWD is correct
mkdir -p ~/.hermes/skills
ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/hermx-control
hermes skills list | grep hermx-control      # expect: enabled, category trading
```

Make sure the agent can read the dashboard: either set `HERMX_DASH_AUTH=false` (loopback,
single-user) in `.env`, or give the agent the `HERMX_SECRET` to send as the
`X-Dashboard-Token` header.

### 6.4.1 Register the slash-command skills

Beyond the single `hermx-control` skill, HermX ships focused **slash-command skills** — each
`skills/hx-*/SKILL.md` becomes a dynamic slash command in Hermes (`/hx-status`, `/hx-positions`,
`/hx-strategy-list`, `/hx-trace`, `/hx-strategy-mode`, `/hx-close`, `/hx-emergency-stop`, `/hx-restart`,
`/hx-upgrade`, `/hx-help`). Link them all in one pass:

```bash
cd /opt/hermx     # repo root, so $PWD is correct
mkdir -p ~/.hermes/skills
for d in skills/hermx-control skills/hx-status skills/hx-positions skills/hx-strategy-list skills/hx-trace skills/hx-tv-alerts skills/hx-help skills/hx-strategy-mode skills/hx-close skills/hx-emergency-stop skills/hx-restart skills/hx-upgrade skills/hx-exchange skills/hx-troubleshoot skills/hx-strategy; do
  ln -sfn "$PWD/$d" ~/.hermes/skills/"$(basename "$d")"
done
hermes skills list | grep -E 'hermx-control|hx-'   # expect hermx-control and each hx-* skill enabled
```

They share the helper library `skills/hermx-ops/lib/hermx_ops.py` (UNKNOWN-never-flat reads,
guarded loopback mutations) and speak the contract in
`skills/hermx-ops/references/api-contract.md`. Full command reference:
`skills/hx-help/SKILL.md`.

> **Telegram note:** in the Telegram gateway these commands appear with **underscores**
> (`/hx_status`, `/hx_positions`, …) since Telegram forbids hyphens, and by default they are
> **not** shown in the `/` menu (Hermes ranks built-ins ahead of skill commands under its
> menu cap). To surface them, add the `command_menu` block from
> [`setup/09-hermes-agent.md`](setup/09-hermes-agent.md) and restart the gateway — check
> `grep -n '^telegram:' ~/.hermes/config.yaml` first, since the block nests under the
> existing top-level `telegram:` key (`telegram.extra.command_menu`), not under `platforms:`.

### 6.5 Start the Telegram gateway

```bash
hermes gateway setup     # one-time wizard — select Telegram
hermes gateway start     # managed service (or `hermes gateway` for foreground)
```

### 6.6 Install the cron monitors

The Hermes gateway includes a built-in cron scheduler. Install the HermX monitor jobs once the gateway is running:

```bash
bash deploy/install-cron-monitors.sh
```

Dry-run first to see what it will do:
```bash
HERMX_CRON_DRY_RUN=1 bash deploy/install-cron-monitors.sh
```

After it runs, restart the gateway so it picks up the new env keys:
```bash
hermes gateway restart
```

Verify the jobs are registered:
```bash
hermes cron list
```

This registers five read-only monitors (all fail-closed, no money-path access):
- `hermx-weekly` — weekly status/positions/signal digest (Mon 09:00 UTC)
- `hermx-daily` — daily status/positions/signal digest (08:00 UTC)
- `hermx-reconcile` — stuck orders and reconcile alerts (every 5m)
- `hermx-health-check` — dashboard/receiver liveness (every 5m)
- `hermx-signal-late` — zero-intake detection (every 30m, 3-day threshold)

On subsequent upgrades (`bash deploy/deploy.sh`), missing monitor jobs are created automatically in "create-only" mode — existing jobs are never edited, so manual pauses and schedule changes are preserved.

Pause a noisy monitor with `/cron pause <name>`. Full design: `docs/7-EXECUTION_MONITORING.md`.

### 6.7 Test it

Ask the user to message their bot in Telegram: **"are you there?"** and confirm a sane reply.

**✅ Verify Phase 6:** `hermes skills list` shows `hermx-control` **and** the `hx-*`
slash-command skills enabled, `hermes doctor` is clean, `hermes cron list` shows the 5 hermx
monitor jobs, and the Telegram bot responds. Full details: `setup/09-hermes-agent.md`;
slash-command reference: `skills/hx-help/SKILL.md`.

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
> - The alert POSTs a small **JSON payload** to your webhook URL. The payload carries a
>   `secret_key` field holding your `HERMX_SECRET` — this is how a direct TradingView alert
>   authenticates, since TradingView's native webhook can't send custom HTTP headers. (Operators
>   running a relay/proxy may instead inject an `X-Webhook-Secret` header.)
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
  "source": "tradingview",
  "secret_key": "<the HERMX_SECRET value from Phase 2>"
}
```

> `secret_key` is the default auth for direct TradingView alerts (native webhook can't send
> headers). Replace the placeholder with your real `HERMX_SECRET`. The receiver strips it right
> after authenticating, so it never lands in a ledger. Omit it only for relay/header setups.

**ETHUSDT 2H** — `strategy_id: ethusdt_duo_base_dev_2h`, `timeframe: 2h`
**SOLUSDT 3H** — `strategy_id: solusdt_duo_base_dev_3h`, `timeframe: 3h`
**XRPUSDT 4H** — `strategy_id: xrpusdt_duo_base_dev_4h`, `timeframe: 4h`

> Use the same template, swapping `strategy_id` and `timeframe`. If you create separate BUY and SELL
> alerts (instead of a strategy-driven alert), hardcode `"side": "buy"` or `"side": "sell"` instead
> of `{{strategy.order.action}}`. `symbol` must resolve to the uppercase asset (e.g. `BTCUSDT`);
> `{{ticker}}` works when the chart symbol matches.

For **every** alert, also give the user:

- **Webhook URL:** `https://hermx.<tailnet>.ts.net/webhook` (from `WEBHOOK_URL.txt`)
- **Auth:** the `secret_key` field in the JSON payload = the `HERMX_SECRET` value from Phase 2
  (default). Relay/proxy setups may instead use a `X-Webhook-Secret: <HERMX_SECRET>` header.

### 7.d TradingView UI — step by step

1. Open TradingView → chart for the asset (e.g. **BTCUSDT.P** on OKX), set the chart to
   **Heikin Ashi** and the timeframe from the strategy file.
2. Add the strategy's indicator (**Duo Base Dev / duo-base-2.5**).
3. Click the **alarm-clock icon** → **Create Alert**.
4. **Condition:** your strategy/indicator signal.
5. **Trigger:** **Once Per Bar Close**.
6. **Expiration:** open-ended / maximum available.
7. Open the **Notifications** tab → enable **Webhook URL** → paste the URL from `WEBHOOK_URL.txt`.
8. In the **Message** box → paste the JSON template for that strategy, with `secret_key` set to
   your `HERMX_SECRET`. This is the default auth — TradingView's native webhook cannot send
   custom headers, so no header configuration is needed.
9. (Relay/proxy setups only) If you front the receiver with a relay, you may instead inject a
   `X-Webhook-Secret` header and drop `secret_key` from the body — see
   `setup/08-webhook-hmac-relay.md`.
10. Name the alert (e.g. `HermX BTC 2h BUY`) → **Create**.
11. Repeat for the SELL alert and for every other enabled strategy.

### 7.e Test the webhook manually

Confirm the receiver accepts a well-formed payload before relying on TradingView. In demo
(`execution_mode: "demo"`) this is validated and ledgered but **never** sent to a live account:

```bash
# Default (mirrors a direct TradingView alert): secret in the JSON body.
curl -s -X POST http://127.0.0.1:8891/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "symbol": "BTCUSDT",
    "timeframe": "2h",
    "side": "buy",
    "tv_signal_price": "65000",
    "tv_time": "2026-06-28T00:00:00Z",
    "exchange": "okx",
    "source": "tradingview",
    "secret_key": "<HERMX_SECRET>"
  }'
```

Then open the dashboard (`http://127.0.0.1:8098/dashboard/`, via Tailscale or an SSH tunnel)
and confirm the alert appears. A `401` means the `secret_key` (or, for relay setups, the
`X-Webhook-Secret` header) is wrong/blank; a **quarantine**
means a field (`strategy_id`/`symbol`/`timeframe`) didn't match the strategy file.

**✅ Verify Phase 7:** the manual `curl` test is accepted (not 401, not quarantined) and shows on the
dashboard, and the user has created BUY+SELL alerts for each enabled strategy with the correct URL
and JSON (including the `secret_key` field, or an `X-Webhook-Secret` header for relay setups).

---

## PHASE 8 — Final Verification Checklist

Run every check and report status to the user:

- [ ] **Receiver healthy** — `curl -sf http://127.0.0.1:8891/health`
- [ ] **Dashboard healthy** — `curl -sf http://127.0.0.1:8098/health`
- [ ] **Public URL healthy** — `curl -sf https://hermx.<tailnet>.ts.net/health`
- [ ] **Dashboard public URL healthy** — `curl -sf -H "X-Dashboard-Token: <HERMX_SECRET>" https://hermx.<tailnet>.ts.net:8443/health`
- [ ] **Tailscale Funnel active** — `tailscale funnel status` shows `https://hermx.<tailnet>.ts.net`
      (webhook on `:443` → 8891) **and** a `:8443` entry (dashboard → 8098)
- [ ] **At least one strategy enabled** — confirmed in `ENABLED_STRATEGIES.txt`
- [ ] **TradingView alert configured for each enabled strategy** (BUY + SELL)
- [ ] **Synthetic webhook accepted** and visible on the dashboard
- [ ] **Live switch still off** — `grep HERMX_LIVE_TRADING .env` shows `false` (or unset)
- [ ] **Hermes agent running** (if installed) — `hermes skills list | grep hermx-control`
- [ ] Monitors installed (`hermes cron list` shows 5 jobs)
- [ ] **Telegram bot responding** (if installed) — replies to "are you there?"

Then print the final summary (fill in the real values):

```text
=== HermX Installation Complete ===
Webhook URL:    https://hermx.XXXXX.ts.net/webhook
Dashboard URL:  https://hermx.XXXXX.ts.net:8443/dashboard/  (needs HERMX_SECRET)
Dashboard (local): http://localhost:8098
Receiver:       http://localhost:8891
Strategies:   btcusdt_duo_base_dev_2h, ethusdt_duo_base_dev_2h, solusdt_duo_base_dev_3h, xrpusdt_duo_base_dev_4h
Exchange:     OKX (demo / simulated)
Telegram:     @<bot_username>
Live switch:  HERMX_LIVE_TRADING=false  (demo, nothing sent to a live account)

Next step: Fire a test alert from TradingView and confirm it appears in the dashboard.
```

### Enabling LIVE execution later (only when the user asks)

Demo strategies (`execution_mode: "demo"`) already route to the exchange
**sandbox** account — no real money, nothing to flip. To move a strategy to the **real** account,
after synthetic tests pass and the user explicitly asks:

```bash
# 1) In the strategy JSON:   "execution_mode": "live"   (was "demo")
# 2) In .env:                HERMX_LIVE_TRADING=true     (global live switch; unset/false = disabled)
# 3) Restart the receiver:
sudo systemctl restart hermx-receiver      # systemd
# or
docker compose restart receiver            # docker
```

A `live` strategy submits to the real account ONLY when `HERMX_LIVE_TRADING` is truthy; otherwise the
order is blocked (`live_trading_disabled`). Never enable live unless the user explicitly asks.

---

## Troubleshooting

- **Port already in use** (`Address already in use`): find the holder with `sudo lsof -i :8891`
  (or `:8098`), stop it, or change `HERMX_RECEIVER_PORT` / `HERMX_DASHBOARD_PORT` in `.env` (legacy `SHADOW_PORT` / `CLEAN_DASHBOARD_PORT` still work) — then re-point
  Tailscale Funnel (`sudo tailscale funnel --bg <newport>`) and any agent config.
- **Tailscale not authenticated / no public URL**: re-run `sudo tailscale up --hostname=hermx`,
  finish the browser login, enable Funnel, then `sudo tailscale funnel --bg 8891`. Check
  `tailscale funnel status`.
- **`strategy_id` mismatch (the most common mistake)**: the alert's `strategy_id`, `symbol`, or
  `timeframe` doesn't match any strategy file, so the alert is quarantined. Align the TradingView
  message to the strategy JSON **exactly** — `strategy_id` is lowercase with underscores
  (`^[a-z0-9]+(?:_[a-z0-9]+)*$`), `symbol` is uppercase (`BTCUSDT`), `timeframe` is one of
  `30m/1h/2h/3h/4h`.
- **Receiver returns 401 on every webhook**: `HERMX_SECRET` is missing/blank, or
  `HERMX_REQUIRE_HMAC=true` without `HERMX_WEBHOOK_HMAC_KEY` (it fails closed). Set the secret/HMAC
  key and restart. For a direct alert, confirm the payload's `secret_key` field equals
  `HERMX_SECRET`; for a relay, the header name is exactly `X-Webhook-Secret`.
- **`.env` permissions too broad**: logs warn if `.env` is group/world-readable — run
  `chmod 600 .env` and restart the receiver.
- **Exchange API keys wrong**: the receiver can't authenticate to the exchange. Re-check you created
  the keys in the **demo/sandbox/testnet** environment, that the passphrase matches, and that the
  right variables are filled for your chosen exchange (`OKX_DEMO_*`, `KUCOIN_PAPER_*`,
  `BYBIT_TESTNET_*`). Demo strategies route to the sandbox via `execution_mode: "demo"` — no `OKX_SIMULATED_TRADING` flag is needed.
- **Orders never submit even with valid alerts**: confirm `execution_mode` is set in the strategy
  file; for a `live` strategy also confirm `HERMX_LIVE_TRADING=true` in `.env`. A demo strategy
  routes to the sandbox automatically.
- **Dashboard reads fail / agent says UNKNOWN**: `HERMX_DASH_AUTH=true` but no token supplied —
  either set `HERMX_DASH_AUTH=false` (loopback) or pass `HERMX_SECRET` via the
  `X-Dashboard-Token` header.
- **Docker healthcheck unhealthy**: `docker compose logs receiver` for the boot error; confirm
  `.env` and `engine-config.json` are present and readable (both are bind-mounted into the
  containers). The default compose uses bridge networking, so the host probes
  `127.0.0.1:8891`/`:8098` via the published ports; only `docker-compose.host.yml` uses
  `network_mode: host` (Linux-only).
- **Tailscale sidecar won't connect (Docker)**: `docker compose logs tailscale`. Confirm `TS_AUTHKEY`
  is set in `.env` and still valid (reusable/ephemeral keys expire), and that Funnel is allowed in
  your tailnet ACL — the sidecar serves per `config/tailscale/serve.json` (Funnel on `:443`,
  tailnet-only dashboard on `:8443`).
