# 09 Hermes Agent operator interface (OPTIONAL)

Goal: let an operator talk to HermX in natural language — ask state and relay
sanctioned signals — through an **external** Hermes Agent (Nous Research).

This is optional. The deterministic system (dashboard, receiver, gate chain) runs
fine without it. The agent only **reads** state and **relays** a TradingView-originated
or human-instructed signal to the existing local API. It **never** self-initiates,
never calls an exchange, never sets an order size, and cannot override any gate or the
kill switch. All money-safety stays in Python. Design: `docs/8-HERMES_AGENT_DESIGN.md`.
What it talks to: `skills/hermx-control/SKILL.md`.

## What the agent can reach

The agent uses only the local loopback API that already runs:

- Read  — dashboard `127.0.0.1:8098`: `GET /api` (positions, PnL, executor health),
  `GET /health` (config gates + the read-only `arm` block).
- Act   — receiver `127.0.0.1:8891`: `POST /webhook` (relay a TradingView alert JSON;
  there is no size/notional/leverage field — the receiver computes notional from the
  strategy file).

> Port note: the receiver listens on `SHADOW_PORT` (legacy naming — the code still
> reads this env var for backward compatibility; default 8891 everywhere — code,
> `setup/env.example`, the scripts, and this skill). If you override the legacy `SHADOW_PORT`
> in your `.env`, update the skill's port to match. See `INSTALL.md` (Phase 2 / Phase 5).

## Steps

1. **Install the Hermes Agent (macOS).** Use the documented installer:

   ```bash
   curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
   source ~/.zshrc   # or ~/.bashrc
   ```

   (A desktop installer is also offered at https://hermes-agent.nousresearch.com/.)
   Verify:

   ```bash
   hermes --version
   ```

2. **Set a provider / LLM key — USER action.** Hermes needs a model provider. Pick one:
   - **xAI / Grok (API key, what this deployment uses):** add `XAI_API_KEY=xai-...` to
     `~/.hermes/.env`, then point Hermes at it:

     ```bash
     hermes config set model.provider xai
     # model.base_url becomes https://api.x.ai/v1 automatically; default model is grok-*
     hermes doctor          # verify: "API key or custom endpoint configured", no retired model
     hermes -z "Reply with exactly: HERMX_OK"   # one-shot smoke test of the key
     ```

   - `hermes setup --portal` — Nous Portal OAuth, no API key file.
   - `hermes model` — interactive picker for any other provider.

   Do not commit any key. This repo never stores the provider key — it lives only in
   `~/.hermes/.env`. Rotate the key if it is ever pasted into a chat or shell history.

3. **Register the hermx-control skill.** Symlink it into the Hermes skills tree (reversible;
   stays in sync with the repo). From the repo root:

   ```bash
   mkdir -p ~/.hermes/skills
   ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/hermx-control
   ```

   Confirm discovery (look for a `hermx-control` row, category `trading`, `enabled`):

   ```bash
   hermes skills list | grep hermx-control
   ```

   To unregister: `rm ~/.hermes/skills/hermx-control` (removes only the symlink).

4. **Let the local agent reach the dashboard.** The agent runs on the same host. Choose one:
   - **No-auth loopback:** start the dashboard with `HERMX_DASH_AUTH=false` while it is bound
     to `127.0.0.1` only. Simplest on a single-user host.
   - **Token:** keep `HERMX_DASH_AUTH=true`, set `HERMX_SECRET=<secret>` in `.env`,
     and give the agent that secret to
     send as the `X-Dashboard-Token` header (the skill already documents this header).

   The receiver's `POST /webhook` uses the `X-Webhook-Secret` header — the same
   `HERMX_SECRET` — regardless of dashboard auth.

## Verification checklist

- [ ] `hermes --version` prints a version.
- [ ] `hermes skills list` shows `hermx-control` as `enabled`.
- [ ] Ask the agent **"what's open?"** — it summarizes `GET /api` positions (or says
      UNKNOWN if the read fails / data is stale — never "flat").
- [ ] Ask **"are we armed?"** — it reports the `arm` block from `GET /health`
      (`armed_summary` plus which gate / the kill switch is blocking, if any).
- [ ] Stopping the agent does **not** affect the deterministic webhook→execute path.

## Talk to the agent from your phone (Telegram / WhatsApp) — OPTIONAL

Hermes has a built-in **messaging gateway**: one background process that connects chat
platforms and loads the same `hermx-control` skill, so "what's open?" works from your
phone exactly like the CLI. The gateway makes **outbound** connections (Telegram
long-polls; WhatsApp uses an outbound Web session), so **no inbound public port** is
needed — ideal on a VPS.

1. **Telegram (recommended):** create a bot with `@BotFather`, then add to `~/.hermes/.env`:

   ```
   TELEGRAM_BOT_TOKEN=<from BotFather>
   TELEGRAM_ALLOWED_USERS=<your numeric Telegram user id>
   ```

2. **WhatsApp (optional; personal-account Web bridge, carries ban risk):**

   ```
   WHATSAPP_ENABLED=true
   WHATSAPP_ALLOWED_USERS=<your number, country code, no +>
   ```
   then `hermes whatsapp` to pair via QR.

3. **Run it:** `hermes gateway setup` (guided wizard) then `hermes gateway` (foreground)
   or `hermes gateway install` + `hermes gateway start` (launchd/systemd service).

> **Money-safety must:** always set the `*_ALLOWED_USERS` allowlist to *only you*. Without
> it the gateway denies all (safe default); never allow-all on a trading agent.

## Pre-execution advisor (in-loop veto) — OPTIONAL, default OFF

Independent of the Hermes runtime above, the **receiver** can consult an LLM as a
pre-execution **risk overseer** on each sanctioned strategy signal (Design 1 in
`docs/8-HERMES_AGENT_DESIGN.md`). It can only return `proceed` or `skip` (+ a risk note);
it can **never** change symbol, side, size, leverage, or strategy — those stay locked in
code. Any timeout / error / malformed reply **fails open** to deterministic execution, so
the front door is never down because of the LLM.

The advisor invokes the **Hermes Agent** as a one-shot **with our skills loaded** —
`hermes -z "<prompt>" --skills hermx-control` — so the agent runs through Hermes (its
configured provider + credentials) and can use the `hermx-control` skill to read the live
local API before its verdict. It is **not** a bare LLM call. Requires the `hermes` binary
on the receiver's `PATH` (step 1) and the skill registered (step 3).

It is OFF by default. Defaults are built in; env vars override. (Note: `shadow-config.json` is dead code — not a config source; file-based overrides live in the `advisor` block of `engine-config.json`, loaded via `load_engine_config()`.) The env vars:

```
HERMX_ADVISOR_ENABLED=true        # single live-veto switch (default OFF); a 'skip' blocks the trade
HERMX_ADVISOR_COMMAND=hermes      # the Hermes CLI on PATH
HERMX_ADVISOR_SKILLS=hermx-control  # comma-separated skills to load (grows over time)
HERMX_ADVISOR_MODEL=              # optional -m override; empty = Hermes default
HERMX_ADVISOR_TIMEOUT_SECONDS=30  # the agent loop is heavier than a raw call
```

Behaviour:
- `HERMX_ADVISOR_ENABLED` is a **single live-veto switch**, default **OFF**. There is no
  annotate-only middle mode — if you turn it on, the veto is live: a `skip` verdict
  **blocks** the trade (nothing submitted). Leave it OFF and the advisor is not consulted
  at all and execution is byte-identical to before.
- Every decision is appended to `logs/advisor-decisions.jsonl` with latency, the loaded
  skills, and the read-only snapshot it reasoned over.

> If the `hermes` binary is missing, the agent times out, or it returns a malformed reply,
> the advisor **fails open** and execution proceeds deterministically — the agent is never
> the front door.

## Telegram Operator Interface

Concrete, copy-paste steps to talk to HermX from Telegram. This expands the
"Talk to the agent from your phone" section above into an exact runbook.

1. **Create a bot.** In Telegram, open a chat with **@BotFather** → send `/newbot`
   → choose a name and a username ending in `bot` → BotFather replies with a token
   like `123456789:ABCdefGhIJKlmNoPQRstuVWxyz`. Copy it — this is your
   `TELEGRAM_BOT_TOKEN`.

2. **Get your numeric user ID.** In Telegram, open a chat with **@userinfobot** and
   send any message. It replies with your numeric `Id` (e.g. `987654321`). This is
   your `TELEGRAM_ALLOWED_USERS` value.

3. **Add both to `~/.hermes/.env`** (the Hermes agent's env, NOT the HermX `.env`):

   ```
   TELEGRAM_BOT_TOKEN=123456789:ABC...
   TELEGRAM_ALLOWED_USERS=987654321
   ```

   > Money-safety must: `TELEGRAM_ALLOWED_USERS` is an allowlist of *only you*.
   > For more than one operator, comma-separate the IDs. Never leave it blank or
   > allow-all on a trading agent.

4. **Register the `hermx-control` skill** (if not already done in step 3 above):

   ```bash
   mkdir -p ~/.hermes/skills
   ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/hermx-control
   hermes skills list | grep hermx-control   # confirm: enabled, category trading
   ```

5. **Start the gateway:**

   ```bash
   hermes gateway setup   # one-time wizard — select Telegram when prompted
   hermes gateway start   # run as a managed service
   # or, to run in the foreground for debugging:
   # hermes gateway
   ```

6. **Test it.** From your phone, message the bot and confirm it answers each:
   - `what's open?`
   - `what's our PnL?`
   - `is the system armed?`
   - `what was the last signal?`

7. **Dashboard auth note.** The agent reads state from the dashboard
   (`127.0.0.1:8098`). Either run the dashboard with `HERMX_DASH_AUTH=false` (loopback,
   single-user host) **or** keep `HERMX_DASH_AUTH=true` and give the agent the
   `HERMX_SECRET` so it can send the
   `X-Dashboard-Token` header. If auth is on
   and the agent has no token, reads will fail and it will report UNKNOWN — never "flat".

## WhatsApp (optional)

For a managed, lower-ban-risk path than the personal-account Web bridge, use **Twilio
WhatsApp**. Add to `~/.hermes/.env`:

```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

For quick testing, use the **Twilio WhatsApp Sandbox** (Twilio Console → Messaging →
Try it out → WhatsApp Sandbox): join the sandbox from your phone with the provided
`join <code>` message, then point `TWILIO_WHATSAPP_FROM` at the sandbox number. Re-run
`hermes gateway setup` and select WhatsApp. The same `*_ALLOWED_USERS` allowlist rule
applies — restrict to your own number only.

## Uninstall

- Skill: `rm ~/.hermes/skills/hermx-control` (removes only the symlink).
- Runtime: remove the `hermes` binary from `~/.local/bin` and the `~/.hermes` directory.
  (The installer ships no documented uninstall command; deleting those two is sufficient.)
