---
name: hermx-exchange
description: "Manage exchange credentials for HermX: add, update, remove, and validate API keys for OKX, KuCoin, Bybit, Binance, Bitget, Gate, Hyperliquid, Coinbase. Triggered by: '/hx-exchange list', '/hx-exchange add okx --demo', '/hx-exchange status bybit --live', '/hx-exchange remove kucoin --demo', 'add exchange credentials', 'how do I configure Bybit'."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SSH_TARGET
    prompt: "SSH target for the HermX VPS (user@host or an ssh_config alias)"
    help: "The host where HermX runs. This skill dispatches scripts/exchange.sh there; it never handles a credential locally."
    required_for: "Dispatching the credential script over SSH"
metadata:
  hermes:
    tags: [trading, hermx, exchange, credentials, operations, mutating]
    related_skills: [hermx-control, hermx-status, hermx-strategy-list, hermx-restart, hermx-help]
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
# /hx-exchange — manage exchange credentials

## Current posture (read this first)
- **This skill never touches a secret.** Every credential is captured by `read -s`
  **inside** `scripts/exchange.sh` on the VPS. You emit an SSH command; the *operator*
  types the key into their own SSH session. You never see, request, echo, log, or relay
  a key value — not on the command line, not in a heredoc, not "just to confirm".
- **Writes go to `.env` on the VPS**, upserted and backed up (`.env.bak`) by the script.
  Adding keys does **not** arm the system — `HERMX_LIVE_TRADING` and each strategy's
  `execution_mode` still gate real-money trading (see [[hermx-control]]).
- **One tool only:** `bash scripts/exchange.sh <sub> <exchange> [--demo|--live]` over SSH.
  Nothing else reads or writes credentials.

## Overview
HermX resolves exchange credentials from `.env` via `src/security/credentials.py`
(`resolve_exchange_credentials`). This skill is the operator-facing front end for the
standalone `scripts/exchange.sh` manager. Your job is to:

1. **Parse** the operator's request into `subcommand + exchange + env (--demo|--live)`.
2. **Validate** the exchange against the eight supported ids before emitting anything.
3. **Emit the correct SSH command** (interactive `ssh -t` for the mutating subcommands).
4. **Confirm** by re-running `status` and reporting the resolver result — never a key.

The safety lives in the script and in the Python resolver, not in this skill text.

## When to Use
- "add / set up / configure `<exchange>` demo|live keys" → `add`
- "rotate / change / update `<exchange>` keys" → `update`
- "remove / delete / clear `<exchange>` keys" → `remove`
- "are `<exchange>` keys set / valid?", "which exchanges are configured?" → `status` / `list`
- Trigger phrases: `/hx-exchange list`, `/hx-exchange add okx --demo`,
  `/hx-exchange status bybit --live`, `/hx-exchange remove kucoin --demo`,
  "add exchange credentials", "how do I configure Bybit".

Don't use for: arming/kill-switch (→ [[hermx-control]] / `/hx-emergency-stop`), strategy
modes (→ `/hx-strategy-mode`), or anything that places or relays an order.

## Config
Resolve the SSH target and repo dir from this skill's `metadata.hermes.config` (same
env-var-default pattern as `skills/hermx-ops/lib/hermx_ops.py`, e.g.
`HERMX_DASHBOARD_BASE`):

- `hermx.ssh_target` — `HERMX_SSH_TARGET` or default `hermx-vps`
- `hermx.repo_dir` — default `/opt/hermx`

All commands take the shape `ssh [-t] {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh …'`.

## Subcommands

### `list` (read-only)
- **Does:** prints every exchange × {demo, live} with SET / PARTIAL / not set, a masked
  primary value (first 4 chars + `****`), a warning when both demo and live are populated
  (resolver defaults to demo), and the current `HERMX_LIVE_TRADING` value.
- **Emit:** `ssh {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh list'`
- **After:** summarize the table; never reproduce a masked value as if it were the key.

### `status <exchange> [--demo|--live]` (read-only)
- **Does:** per-var presence for the chosen env, the resolver smoke result
  (`OK`/`PARTIAL`/`MISSING`), a precedence warning if the opposite env is also set, an
  adapter-wired note (coinbase is not wired), and — for `--live` with
  `HERMX_LIVE_TRADING=true` — an optional `fetch_balance` probe (prompted in the SSH
  session). No env flag → reports both demo and live.
- **Emit:** `ssh {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh status <exchange> [--demo|--live]'`
- **After:** report `OK`/`PARTIAL`/`MISSING` and any precedence/wiring warning verbatim.

### `add <exchange> --demo|--live` (mutating)
- **Does:** prompts for each field with `read -s`, previews masked values, backs up
  `.env`, upserts the vars, runs a resolver smoke test, prints the restart reminder.
  Requires an explicit `--demo`/`--live`; `--live` needs a typed
  `yes, add live <exchange>`; coinbase `--demo` is rejected (no ccxt sandbox).
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh add <exchange> --demo|--live'`
- **After:** tell the operator to complete the prompts **in their SSH session**, then
  re-run `status <exchange> <same env>` to confirm `OK`.

### `update <exchange> --demo|--live` (mutating)
- **Does:** like `add`, but shows each field's current masked value and keeps it when the
  operator presses Enter; aborts if nothing changed.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh update <exchange> --demo|--live'`
- **After:** re-run `status` to confirm.

### `remove <exchange> --demo|--live` (mutating)
- **Does:** scans `strategies/*.json` for strategies on that exchange and warns, requires
  a typed `yes, remove <demo|live> <exchange>`, backs up `.env`, blanks the vars
  (`VAR=`), and confirms the resolver now reports `MISSING`.
- **Emit (interactive):** `ssh -t {ssh_target} 'cd {repo_dir} && bash scripts/exchange.sh remove <exchange> --demo|--live'`
- **After:** re-run `status` to confirm `MISSING`; relay the strategy-impact warning.

## Supported exchanges

| exchange | demo/testnet vars | live vars | passphrase | adapter-wired | notes |
|---|---|---|---|---|---|
| `okx` | `OKX_DEMO_API_KEY` / `_SECRET_KEY` / `_PASSPHRASE` | `OKX_API_KEY` / `_SECRET_KEY` / `_PASSPHRASE` | yes | yes | default venue |
| `kucoin` | `KUCOIN_PAPER_API_KEY` / `_SECRET` / `_PASSPHRASE` | `KUCOIN_API_KEY` / `_SECRET` / `_PASSPHRASE` | yes | yes | secret var is `_SECRET`, not `_SECRET_KEY` |
| `bybit` | `BYBIT_TESTNET_API_KEY` / `_SECRET_KEY` | `BYBIT_API_KEY` / `_SECRET_KEY` | no | yes | |
| `binance` | `BINANCE_TESTNET_API_KEY` / `_SECRET_KEY` | `BINANCE_API_KEY` / `_SECRET_KEY` | no | yes | |
| `bitget` | `BITGET_DEMO_API_KEY` / `_SECRET_KEY` / `_PASSPHRASE` | `BITGET_API_KEY` / `_SECRET_KEY` / `_PASSPHRASE` | yes | yes | |
| `gate` | `GATE_TESTNET_API_KEY` / `_SECRET_KEY` | `GATE_API_KEY` / `_SECRET_KEY` | no | yes | |
| `hyperliquid` | `HYPERLIQUID_TESTNET_WALLET_ADDRESS` + `_PRIVATE_KEY` | `HYPERLIQUID_WALLET_ADDRESS` + `_PRIVATE_KEY` | no | yes | wallet + private key (no passphrase); fails closed unless both present |
| `coinbase` | — (not supported) | `COINBASE_API_KEY` / `_SECRET_KEY` | no | **no** | live/spot only; ccxt has no coinbase sandbox — credentials resolve but no adapter path |

## Security rules
- **Never intake a credential.** Do not ask the operator to paste a key to you, and never
  accept one. If they try, tell them to enter it into the `ssh -t` session's `read -s`
  prompt instead.
- **Never emit a key value.** No key in a command argument, heredoc, echo, log, or summary.
  Masked previews (`8b17****`) come from the script; treat them as status, not the key.
- **Interactive mutations use `ssh -t`.** `add`/`update`/`remove` prompt with `read -s`
  and need a TTY (the script hard-errors without one). `list`/`status` don't.
- **Live is explicit.** Confirm `--live` was intended; the script itself requires a typed
  `yes, add live <exchange>`. Never promote to live implicitly.
- **Adding keys ≠ arming.** State plainly that `HERMX_LIVE_TRADING` and strategy
  `execution_mode` still gate real trading; direct arming questions to [[hermx-control]].
- **Only the eight ids exist.** Validate before emitting; reject anything else with the
  supported list.

## Procedure
```python
# Pure orchestration: parse → validate → emit SSH → confirm. NEVER handles a key value.
import shlex

SUPPORTED = ["okx", "kucoin", "bybit", "binance", "bitget", "gate", "hyperliquid", "coinbase"]
NOT_WIRED = {"coinbase"}
MUTATING  = {"add", "update", "remove"}

def build(ssh_target, repo_dir, subcmd, exchange=None, env_flag=None):
    # 1) validate subcommand + exchange
    if subcmd not in {"list", "status", "add", "update", "remove"}:
        raise ValueError(f"unknown subcommand: {subcmd}")
    if subcmd != "list":
        if exchange not in SUPPORTED:
            raise ValueError(f"unknown exchange '{exchange}'; supported: {', '.join(SUPPORTED)}")
    if subcmd in MUTATING and env_flag not in ("--demo", "--live"):
        raise ValueError("add/update/remove require an explicit --demo or --live")
    if subcmd in MUTATING and exchange == "coinbase" and env_flag == "--demo":
        raise ValueError("coinbase sandbox not supported in ccxt; use --live (spot only)")

    # 2) assemble the remote command (NO credential ever appears here)
    parts = ["bash", "scripts/exchange.sh", subcmd]
    if exchange: parts.append(exchange)
    if env_flag: parts.append(env_flag)
    remote = f"cd {shlex.quote(repo_dir)} && {' '.join(parts)}"

    # 3) interactive (-t) for mutations so the script's read -s prompts get a TTY
    flag = "-t " if subcmd in MUTATING else ""
    return f"ssh {flag}{shlex.quote(ssh_target)} {shlex.quote(remote)}"

# Example emissions:
#   list             -> ssh hermx-vps 'cd /opt/hermx && bash scripts/exchange.sh list'
#   status bybit --live -> ssh hermx-vps 'cd /opt/hermx && bash scripts/exchange.sh status bybit --live'
#   add okx --demo   -> ssh -t hermx-vps 'cd /opt/hermx && bash scripts/exchange.sh add okx --demo'
```
1. **Parse** the request → `subcommand`, `exchange`, `env_flag`. Resolve `ssh_target`
   (`HERMX_SSH_TARGET` / config) and `repo_dir` (config, default `/opt/hermx`).
2. **Validate** with `build(...)`; on error, show the supported list and stop — emit nothing.
3. **Emit** the single SSH command. For `add`/`update`/`remove`, tell the operator:
   *"complete the `read -s` prompts in your SSH session — I never see the key."*
4. **Confirm** a mutation by re-emitting `status <exchange> <env>` and reporting the
   resolver result (`OK`/`PARTIAL`/`MISSING`) plus any precedence/wiring warning.

## Reporting
- **list:** which exchanges/envs are SET vs not set, any both-set precedence warning, and
  the `HERMX_LIVE_TRADING` value. Note coinbase is not adapter-wired.
- **status:** vars present (`n/total`), resolver result, precedence and wiring warnings,
  and the live-probe outcome if run. Never restate a masked value as the key.
- **add/update/remove:** confirm the operator completed the SSH prompts, report the
  post-change `status` (`OK`/`PARTIAL`/`MISSING`), relay the restart reminder
  (`sudo systemctl restart hermx-receiver hermx-dashboard`), and — for live — restate that
  the system is still not armed until `HERMX_LIVE_TRADING=true`.
- **Any failure/unreachable SSH → UNKNOWN**, never "keys are set/removed". Report the
  failure plainly.

## Verification checklist
- [ ] Exchange validated against the eight ids before any SSH command was emitted.
- [ ] Mutating subcommands used `ssh -t`; `list`/`status` did not.
- [ ] No credential value appeared in any command, heredoc, echo, log, or summary.
- [ ] `--live` intent confirmed; coinbase `--demo` rejected.
- [ ] A mutation was confirmed by a follow-up `status`, reporting the resolver result.
- [ ] Reminded the operator that adding keys does not arm the system.
