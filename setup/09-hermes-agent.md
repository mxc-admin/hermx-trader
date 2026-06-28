# 09 Hermes Agent operator interface (OPTIONAL)

Goal: let an operator talk to HermX in natural language — ask state and relay
sanctioned signals — through an **external** Hermes Agent (Nous Research).

This is optional. The deterministic system (dashboard, receiver, gate chain) runs
fine without it. The agent only **reads** state and **relays** a TradingView-originated
or human-instructed signal to the existing local API. It **never** self-initiates,
never calls an exchange, never sets an order size, and cannot override any gate or the
kill switch. All money-safety stays in Python. Design: `docs/HERMES_AGENT_DESIGN.md`.
What it talks to: `skills/hermx-control/SKILL.md`.

## What the agent can reach

The agent uses only the local loopback API that already runs:

- Read  — dashboard `127.0.0.1:8098`: `GET /api` (positions, PnL, executor health),
  `GET /health` (config gates + the read-only `arm` block).
- Act   — receiver `127.0.0.1:8891`: `POST /webhook` (relay a TradingView alert JSON;
  there is no size/notional/leverage field — the receiver computes notional from the
  strategy file).

> Port note: the receiver listens on `SHADOW_PORT` (default 8891 everywhere — code,
> `setup/env.example`, the scripts, and this skill). If you override `SHADOW_PORT` in
> your `.env`, update the skill's port to match. See `SETUP.md` section 7.

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
   - `hermes setup --portal` — Nous Portal OAuth, no API key file (recommended quick path).
   - `hermes model` — choose another provider; its credentials go in `~/.hermes/.env`.

   Do not commit any key. This repo never stores the Hermes provider key — it lives only
   in `~/.hermes/.env`.

3. **Register the hermx-control skill.** Symlink it into the Hermes skills tree (reversible;
   stays in sync with the repo). From the repo root:

   ```bash
   mkdir -p ~/.hermes/skills/trading
   ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/trading/hermx-control
   ```

   Confirm discovery (look for a `hermx-control` row, category `trading`, `enabled`):

   ```bash
   hermes skills list | grep hermx-control
   ```

   To unregister: `rm ~/.hermes/skills/trading/hermx-control` (removes only the symlink).

4. **Let the local agent reach the dashboard.** The agent runs on the same host. Choose one:
   - **No-auth loopback:** start the dashboard with `HERMX_DASH_AUTH=false` while it is bound
     to `127.0.0.1` only. Simplest on a single-user host.
   - **Token:** keep `HERMX_DASH_AUTH=true`, set `HERMX_DASH_AUTH_TOKEN=<token>` in `.env`,
     and give the agent that token to send as the `X-Dashboard-Token` header (the skill
     already documents this header).

   The receiver's `POST /webhook` uses the `X-Webhook-Secret` header
   (`SHADOW_WEBHOOK_SECRET`) regardless of dashboard auth.

## Verification checklist

- [ ] `hermes --version` prints a version.
- [ ] `hermes skills list` shows `hermx-control` as `enabled`.
- [ ] Ask the agent **"what's open?"** — it summarizes `GET /api` positions (or says
      UNKNOWN if the read fails / data is stale — never "flat").
- [ ] Ask **"are we armed?"** — it reports the `arm` block from `GET /health`
      (`armed_summary` plus which gate / the kill switch is blocking, if any).
- [ ] Stopping the agent does **not** affect the deterministic webhook→execute path.

## Uninstall

- Skill: `rm ~/.hermes/skills/trading/hermx-control` (and `rmdir ~/.hermes/skills/trading`
  if empty).
- Runtime: remove the `hermes` binary from `~/.local/bin` and the `~/.hermes` directory.
  (The installer ships no documented uninstall command; deleting those two is sufficient.)
