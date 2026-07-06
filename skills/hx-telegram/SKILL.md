---
name: hx-telegram
description: "Manage the Telegram operator gateway for the Hermes Agent: set up, rotate the bot token, allow/revoke operator user ids, start/stop the gateway, smoke-test. Triggered by: '/hx-telegram status', '/hx-telegram setup', '/hx-telegram allow 987654321', '/hx-telegram rotate', 'set up telegram control', 'rotate the telegram bot token'."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SSH_TARGET
    prompt: "SSH target for the HermX VPS (user@host or an ssh_config alias)"
    help: "The host where HermX and the Hermes Agent run. This skill dispatches scripts/hermes-gateway.sh there; it never handles the bot token locally."
    required_for: "Dispatching the gateway script over SSH"
metadata:
  hermes:
    tags: [trading, hermx, telegram, gateway, credentials, operations, mutating]
    related_skills: [hermx-control, hx-exchange, hx-status, hx-restart, hx-help]
    config:
      - key: hermx.ssh_target
        description: "SSH target for the HermX VPS (user@host or ssh_config alias)"
        default: "hermx-vps"
        prompt: "SSH target for the HermX VPS"
      - key: hermx.repo_dir
        description: "HermX install/repo directory on the VPS"
        default: "/opt/hermx"
        prompt: "HermX repo directory on the VPS"
---
# /hx-telegram — manage the Telegram operator gateway

## Current posture (read this first)
- **This skill never touches the bot token.** The token is captured by `read -s`
  **inside** `scripts/hermes-gateway.sh` on the VPS. You emit an SSH command; the
  *operator* types the token into their own SSH session. You never see, request,
  echo, log, or relay a token value — not on the command line, not in a heredoc,
  not "just to confirm".
- **Writes go to `~/.hermes/.env` on the VPS** (the Hermes agent's env — **NOT** the
  HermX `.env`), upserted and backed up (`~/.hermes/.env.bak`, restored on a failed
  write) by the script, `chmod 600`. Comms remain Hermes' native gateway
  (`hermes gateway`) — this skill only manages its config and lifecycle
  (docs/8-HERMES_AGENT_DESIGN.md §5.2 note still holds).
- **The allowlist is a money-safety control.** `TELEGRAM_ALLOWED_USERS` must be
  *only the operator's* numeric ids — never blank-as-configured, never allow-all.
  The script rejects blank/allow-all allowlists; an *empty* allowlist means the
  gateway denies everyone (safe default).
- **Gateway down never blocks trading.** The messaging gateway is comms-only;
  the webhook→execute path does not depend on it (§5.5).
- **One tool only:** `bash scripts/hermes-gateway.sh <sub> [args]` over SSH.
  Nothing else reads or writes the gateway config.

## Overview
The Hermes Agent's Telegram operator interface (setup/09-hermes-agent.md, "Telegram
Operator Interface") is configured via `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS`
in `~/.hermes/.env` and run by `hermes gateway`. This skill is the operator-facing
front end for the standalone `scripts/hermes-gateway.sh` manager. Your job is to:

1. **Parse** the operator's request into `subcommand [+ user-id]`.
2. **Validate** the subcommand, and for `allow`/`revoke` that the id is numeric,
   before emitting anything.
3. **Emit the correct SSH command** (interactive `ssh -t` for the mutating subcommands).
4. **Confirm** by re-running `status` and reporting masked posture — never a token.

The safety lives in the script and in the gateway's own allowlist enforcement,
not in this skill text.

## When to Use
- "set up / configure telegram control", "connect the bot" → `setup`
- "rotate / change the telegram bot token" → `rotate`
- "add operator `<id>` on telegram", "let this user id talk to the bot" → `allow <id>`
- "remove / revoke user `<id>`" → `revoke <id>`
- "turn off / tear down telegram", "disconnect the bot" → `remove` (alias `disable`)
- "is telegram set up?", "is the gateway running?" → `status`
- "start / stop / restart the gateway" → `start` / `stop` / `restart`
- "test the bot from my phone" → `test`
- Trigger phrases: `/hx-telegram status`, `/hx-telegram setup`,
  `/hx-telegram allow 987654321`, `/hx-telegram rotate`, "set up telegram control".

Don't use for: exchange API keys (→ [[hx-exchange]]), arming/kill-switch
(→ [[hermx-control]] / `/hx-emergency-stop`), or restarting the HermX
dashboard/receiver services (→ `/hx-restart` — the gateway is a separate process).

## Config
Resolve the SSH target and repo dir from this skill's `metadata.hermes.config` (same
pattern as [[hx-exchange]]):

- `hermx.ssh_target` — `HERMX_SSH_TARGET` or default `hermx-vps`
- `hermx.repo_dir` — default `/opt/hermx`

All commands take the shape `ssh [-t] {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh …'`.

## Subcommands

### `status` (read-only)
- **Does:** masked token preview (first 4 chars + `****`), the allowlist verbatim
  (user ids are not secrets), a warning when the token is set but the allowlist is
  empty (gateway denies all) or contains an allow-all entry, and the gateway process
  state via `hermes gateway status` (best-effort — failure reports UNKNOWN).
- **Emit:** `ssh {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh status'`
- **After:** summarize posture; never reproduce a masked value as if it were the token.

### `setup` (mutating)
- **Does:** prompts for the bot token with `read -s` and the allowlist with plain
  `read`, validates the allowlist (comma-separated numeric ids; blank/allow-all
  rejected), previews masked values, backs up `~/.hermes/.env`, upserts both vars
  (`chmod 600`, restore-on-failure), then offers the one-time `hermes gateway setup`
  wizard and reminds about `start`.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh setup'`
- **After:** tell the operator to complete the prompts **in their SSH session**
  (token from @BotFather, numeric id from @userinfobot), then re-run `status`.

### `rotate` (mutating)
- **Does:** like `setup` but token-only; shows the current masked token and keeps it
  (aborts cleanly) when the operator presses Enter; reminds them to `/revoke` the old
  token with @BotFather.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh rotate'`
- **After:** re-run `status`; remind that the gateway must be restarted to pick up
  the new token.

### `allow <id>` / `revoke <id>` (mutating)
- **Does:** adds/removes one numeric user id in `TELEGRAM_ALLOWED_USERS`. Previews
  current→new list, requires a typed `yes, allow <id>` / `yes, revoke <id>`.
  `allow` of an already-listed id is a no-op; `revoke` of an unlisted id errors;
  revoking the last id warns that the gateway will deny ALL senders.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh allow <id>'`
  (same shape for `revoke`).
- **After:** re-run `status` and confirm the allowlist; remind about the restart.
- **Validate first:** the id must be all digits — reject anything else *before*
  emitting (a username or `@handle` is not a user id; point them at @userinfobot).

### `remove` (mutating; alias `disable`)
- **Does:** requires a typed `yes, remove telegram`, backs up `~/.hermes/.env`,
  blanks both vars, and stops the gateway (`hermes gateway stop`, best-effort with
  honest UNKNOWN on failure). Notes that `.env.bak` still holds the old token and
  that it should be revoked with @BotFather.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh remove'`
- **After:** re-run `status` to confirm both vars unset and the gateway stopped.

### `start` / `stop` / `restart` (lifecycle, non-interactive)
- **Does:** drives `hermes gateway start|stop|restart` on the VPS, then reports the
  resulting state (`hermes gateway status`). A non-zero exit is reported as
  FAILED/UNKNOWN — never "running"/"stopped" on faith.
- **Emit:** `ssh {ssh_target} 'cd {repo_dir} && bash scripts/hermes-gateway.sh start'`
  (same shape for `stop`/`restart`).
- **After:** relay the reported state verbatim.

### `test` (read-only)
- **Does:** pure text — instructs the operator to send the 4 smoke-test messages
  from their phone (`what's open?`, `what's our PnL?`, `is the system armed?`,
  `what was the last signal?`) and what a healthy answer looks like (UNKNOWN on a
  failed read — never "flat"). You may answer this from the skill text without SSH,
  or emit the script's `test` subcommand for a terminal copy.

## Security rules
- **Never intake the bot token.** Do not ask the operator to paste it to you, and
  never accept one. If they try, tell them to enter it into the `ssh -t` session's
  `read -s` prompt instead — and, since they exposed it in chat, to rotate it via
  @BotFather.
- **Never emit a token value.** No token in a command argument, heredoc, echo, log,
  or summary. Masked previews (`1234****`) come from the script; treat them as
  status, not the token.
- **Interactive mutations use `ssh -t`.** `setup`/`rotate` prompt with `read -s` and
  need a TTY (the script hard-errors without one); `allow`/`revoke`/`remove` need
  the TTY for their typed confirmations. `status`/`test` and the lifecycle verbs don't.
- **Allowlist is never blank-as-configured or allow-all.** Only comma-separated
  numeric ids of the operator(s). The script enforces this; you validate the id
  shape before emitting.
- **`~/.hermes/.env` is the agent's env, not HermX's.** Never point this skill at
  the HermX `.env` (exchange keys live there → [[hx-exchange]]); never suggest a
  dashboard write path for the token.
- **Gateway config ≠ trading authority.** Connecting Telegram grants conversation
  with the agent only; order authority stays gated by the deterministic Python
  chain (`HERMX_LIVE_TRADING`, strategy `execution_mode` — see [[hermx-control]]).

## Procedure
```python
# Pure orchestration: parse → validate → emit SSH → confirm. NEVER handles the token.
import shlex

READ_ONLY = {"status", "test"}
LIFECYCLE = {"start", "stop", "restart"}
MUTATING  = {"setup", "rotate", "allow", "revoke", "remove", "disable"}
NEEDS_ID  = {"allow", "revoke"}

def build(ssh_target, repo_dir, subcmd, user_id=None):
    # 1) validate subcommand (+ numeric user id for allow/revoke)
    if subcmd not in READ_ONLY | LIFECYCLE | MUTATING:
        raise ValueError(f"unknown subcommand: {subcmd}")
    if subcmd in NEEDS_ID and not (user_id and user_id.isdigit()):
        raise ValueError("allow/revoke require a numeric Telegram user id (from @userinfobot)")
    if subcmd not in NEEDS_ID and user_id:
        raise ValueError(f"{subcmd} takes no user id")

    # 2) assemble the remote command (NO secret ever appears here)
    parts = ["bash", "scripts/hermes-gateway.sh", subcmd]
    if user_id: parts.append(user_id)
    remote = f"cd {shlex.quote(repo_dir)} && {' '.join(parts)}"

    # 3) interactive (-t) for mutations so read -s / typed confirmations get a TTY
    flag = "-t " if subcmd in MUTATING else ""
    return f"ssh {flag}{shlex.quote(ssh_target)} {shlex.quote(remote)}"

# Example emissions:
#   status          -> ssh hermx-vps 'cd /opt/hermx && bash scripts/hermes-gateway.sh status'
#   setup           -> ssh -t hermx-vps 'cd /opt/hermx && bash scripts/hermes-gateway.sh setup'
#   allow 987654321 -> ssh -t hermx-vps 'cd /opt/hermx && bash scripts/hermes-gateway.sh allow 987654321'
```
1. **Parse** the request → `subcommand`, optional `user_id`. Resolve `ssh_target`
   (`HERMX_SSH_TARGET` / config) and `repo_dir` (config, default `/opt/hermx`).
2. **Validate** with `build(...)`; on error, show the subcommand list and stop — emit nothing.
3. **Emit** the single SSH command. For mutations, tell the operator:
   *"complete the prompts in your SSH session — I never see the token."*
4. **Confirm** a mutation by re-emitting `status` and reporting masked posture and
   gateway state — never a token value.

## Reporting
- **status:** token SET/not set (masked), the allowlist ids, empty-allowlist /
  allow-all warnings, and the gateway process state. Never restate a masked value
  as the token.
- **setup/rotate/allow/revoke/remove:** confirm the operator completed the SSH
  prompts, report the post-change `status`, and relay the apply reminder
  (`hermes gateway restart` — env changes are inert until the gateway restarts).
- **start/stop/restart:** relay the script's reported state; a non-zero exit is
  FAILED/UNKNOWN, never assumed done.
- **Any failure/unreachable SSH → UNKNOWN**, never "configured/stopped". Report the
  failure plainly.

## Verification checklist
- [ ] Subcommand validated (and user id all-digits for `allow`/`revoke`) before any
      SSH command was emitted.
- [ ] Mutating subcommands used `ssh -t`; `status`/`test`/lifecycle did not need it.
- [ ] No token value appeared in any command, heredoc, echo, log, or summary.
- [ ] Allowlist changes previewed current→new; blank/allow-all never suggested.
- [ ] A mutation was confirmed by a follow-up `status`, with the apply-restart reminder.
- [ ] Reminded the operator that the gateway is comms-only — it grants no trading
      authority and its downtime never blocks execution.
