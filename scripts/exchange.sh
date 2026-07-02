#!/usr/bin/env bash
# exchange.sh — HermX exchange-credential manager (run ON the VPS over SSH)
#
# Captures API keys via `read -s` INSIDE this script and writes them to .env.
# A credential is NEVER accepted as a command-line argument and is NEVER echoed
# back or passed to an external command as an argv token.
#
# Usage:
#   bash scripts/exchange.sh list
#   bash scripts/exchange.sh status <exchange> [--demo|--live]
#   bash scripts/exchange.sh add    <exchange> --demo|--live
#   bash scripts/exchange.sh update <exchange> --demo|--live
#   bash scripts/exchange.sh remove <exchange> --demo|--live
#
# Subcommands: list, status, add, update, remove
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT/.env"
STRATEGIES_DIR="$ROOT/strategies"
PYTHON="$ROOT/.venv/bin/python3"

# --- Colours + logging (same style as deploy.sh) ------------------------------
if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
phase() { printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info()  { printf '  %s\n' "$1"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()   { printf '  %sx%s %s\n' "$RED" "$RESET" "$1" >&2; }

# --- Supported exchanges ------------------------------------------------------
SUPPORTED_EXCHANGES="okx kucoin bybit binance bitget gate hyperliquid coinbase"
# Exchanges whose credentials resolve but are NOT wired into the CCXT adapter yet
# (see src/executors/ccxt_adapter.py:217 — coinbase has no ccxt sandbox; live/spot only).
NOT_WIRED_EXCHANGES="coinbase"

usage() {
  cat <<EOF
Usage: bash scripts/exchange.sh <subcommand> <exchange> [--demo|--live]

Subcommands:
  list                         Show every exchange × env credential status
  status <exchange> [--env]    Presence + resolver smoke test for one exchange
  add    <exchange> --demo|--live    Prompt for and write credentials
  update <exchange> --demo|--live    Change existing credentials (blank = keep)
  remove <exchange> --demo|--live    Blank out credentials for one env

Exchanges: $SUPPORTED_EXCHANGES
Secrets are captured interactively (read -s) — never pass a key as an argument.
EOF
}

# --- Per-exchange field tables ------------------------------------------------
# get_fields <exchange> <demo|live> → prints "VARNAME:label:is_secret" lines.
# is_secret: y = read -s (no echo), n = read -r (echo). The FIRST line's var is
# the "primary" var used for presence/masking in list/status.
# Returns 1 when the (exchange, env) combination is unsupported (coinbase demo).
get_fields() {
  local ex="$1" env="$2"
  case "$ex:$env" in
    okx:demo)  printf '%s\n' "OKX_DEMO_API_KEY:API key:y" "OKX_DEMO_SECRET_KEY:secret key:y" "OKX_DEMO_PASSPHRASE:passphrase:y" ;;
    okx:live)  printf '%s\n' "OKX_API_KEY:API key:y" "OKX_SECRET_KEY:secret key:y" "OKX_PASSPHRASE:passphrase:y" ;;
    kucoin:demo) printf '%s\n' "KUCOIN_PAPER_API_KEY:API key:y" "KUCOIN_PAPER_SECRET:secret key:y" "KUCOIN_PAPER_PASSPHRASE:passphrase:y" ;;
    kucoin:live) printf '%s\n' "KUCOIN_API_KEY:API key:y" "KUCOIN_SECRET:secret key:y" "KUCOIN_PASSPHRASE:passphrase:y" ;;
    bybit:demo) printf '%s\n' "BYBIT_TESTNET_API_KEY:API key:y" "BYBIT_TESTNET_SECRET_KEY:secret key:y" ;;
    bybit:live) printf '%s\n' "BYBIT_API_KEY:API key:y" "BYBIT_SECRET_KEY:secret key:y" ;;
    binance:demo) printf '%s\n' "BINANCE_TESTNET_API_KEY:API key:y" "BINANCE_TESTNET_SECRET_KEY:secret key:y" ;;
    binance:live) printf '%s\n' "BINANCE_API_KEY:API key:y" "BINANCE_SECRET_KEY:secret key:y" ;;
    bitget:demo) printf '%s\n' "BITGET_DEMO_API_KEY:API key:y" "BITGET_DEMO_SECRET_KEY:secret key:y" "BITGET_DEMO_PASSPHRASE:passphrase:y" ;;
    bitget:live) printf '%s\n' "BITGET_API_KEY:API key:y" "BITGET_SECRET_KEY:secret key:y" "BITGET_PASSPHRASE:passphrase:y" ;;
    gate:demo) printf '%s\n' "GATE_TESTNET_API_KEY:API key:y" "GATE_TESTNET_SECRET_KEY:secret key:y" ;;
    gate:live) printf '%s\n' "GATE_API_KEY:API key:y" "GATE_SECRET_KEY:secret key:y" ;;
    hyperliquid:demo) printf '%s\n' "HYPERLIQUID_TESTNET_WALLET_ADDRESS:wallet address (0x…):n" "HYPERLIQUID_TESTNET_PRIVATE_KEY:private key:y" ;;
    hyperliquid:live) printf '%s\n' "HYPERLIQUID_WALLET_ADDRESS:wallet address (0x…):n" "HYPERLIQUID_PRIVATE_KEY:private key:y" ;;
    coinbase:demo) return 1 ;;
    coinbase:live) printf '%s\n' "COINBASE_API_KEY:API key:y" "COINBASE_SECRET_KEY:secret key:y" ;;
    *) return 1 ;;
  esac
}

# --- Small helpers ------------------------------------------------------------
is_supported() {
  local ex="$1" e
  for e in $SUPPORTED_EXCHANGES; do [ "$e" = "$ex" ] && return 0; done
  return 1
}

is_wired() {
  local ex="$1" e
  for e in $NOT_WIRED_EXCHANGES; do [ "$e" = "$ex" ] && return 1; done
  return 0
}

validate_exchange() {
  local ex="$1"
  if ! is_supported "$ex"; then
    err "Unknown exchange: '${ex:-<none>}'"
    info "Supported: $SUPPORTED_EXCHANGES"
    exit 2
  fi
}

# env_get VARNAME → prints the value from .env (last definition wins), or nothing.
# Never fails the pipeline under `set -e`/pipefail.
env_get() {
  local name="$1" line
  [ -f "$ENV_FILE" ] || return 0
  line="$(grep -E "^(export[[:space:]]+)?${name}=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
  [ -n "$line" ] || return 0
  line="${line#export }"
  line="${line#*=}"
  # strip one layer of surrounding quotes
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# mask VALUE → first 4 chars + **** (or (empty)/**** for short values). Reveals no
# more than 4 leading chars; used only for confirmation display, never the secret.
mask() {
  local v="$1"
  if [ -z "$v" ]; then printf '(empty)'; return; fi
  if [ "${#v}" -le 4 ]; then printf '****'; else printf '%s****' "${v:0:4}"; fi
}

# primary_var <exchange> <env> → the first field's VARNAME (api_key / wallet).
primary_var() { get_fields "$1" "$2" 2>/dev/null | head -n1 | cut -d: -f1; }

# Count how many of an env's required vars are present in .env → "present total".
count_present() {
  local ex="$1" env="$2" present=0 total=0 var val line
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    var="${line%%:*}"
    total=$((total + 1))
    val="$(env_get "$var")"
    [ -n "$val" ] && present=$((present + 1))
  done < <(get_fields "$ex" "$env" 2>/dev/null || true)
  printf '%s %s\n' "$present" "$total"
}

# backup_env — one rolling backup before any write.
backup_env() { [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null || true; }

# write_env_var NAME VALUE — upsert without exposing VALUE in any external argv.
# VALUE flows only through shell builtins (printf/read); external `ps` never sees it.
write_env_var() {
  local name="$1" value="$2" tmp found=0 line
  umask 077
  tmp="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  chmod 600 "$tmp"
  if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "${name}="*|"export ${name}="*)
          printf '%s=%s\n' "$name" "$value" >> "$tmp"; found=1 ;;
        *) printf '%s\n' "$line" >> "$tmp" ;;
      esac
    done < "$ENV_FILE"
  fi
  if [ "$found" -eq 0 ]; then
    printf '%s=%s\n' "$name" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# trim trailing CR / whitespace / newline from a captured value (in-memory only).
trim_value() {
  local v="$1"
  v="${v%$'\r'}"
  v="${v%$'\n'}"
  while [ "$v" != "${v% }" ]; do v="${v% }"; done
  while [ "$v" != "${v%$'\t'}" ]; do v="${v%$'\t'}"; done
  printf '%s' "$v"
}

require_tty() {
  [ -t 0 ] || { err "stdin is not a TTY — run this script in an interactive SSH session (ssh -t)"; exit 1; }
}

# resolver_result <exchange> <mode> → prints OK | PARTIAL | MISSING by asking the
# production resolver what it would hand the adapter for that mode.
resolver_result() {
  local ex="$1" mode="$2" expected
  expected="$(get_fields "$ex" "$mode" 2>/dev/null | wc -l | tr -d ' ')"
  [ -n "$expected" ] || expected=0
  "$PYTHON" - "$ROOT" "$ENV_FILE" "$ex" "$mode" "$expected" <<'PY' 2>/dev/null || echo "ERROR"
import sys
root, env_file, exchange, mode, expected = sys.argv[1:6]
expected = int(expected)
sys.path.insert(0, root + "/src")
env = {}
try:
    with open(env_file) as fh:
        for raw in fh:
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[len("export "):]
            k, _, v = s.partition("=")
            v = v.strip()
            for q in ('"', "'"):
                if len(v) >= 2 and v[0] == q and v[-1] == q:
                    v = v[1:-1]
            env[k.strip()] = v
except FileNotFoundError:
    pass
from security.credentials import resolve_exchange_credentials
creds = resolve_exchange_credentials(exchange, env, mode=mode)
n = len([v for v in creds.values() if str(v).strip()])
if n == 0:
    print("MISSING")
elif expected and n < expected:
    print("PARTIAL")
else:
    print("OK")
PY
}

# live_probe <exchange> — ccxt fetch_balance smoke against LIVE creds. Best-effort.
live_probe() {
  local ex="$1"
  "$PYTHON" - "$ROOT" "$ENV_FILE" "$ex" <<'PY' || true
import sys
root, env_file, exchange = sys.argv[1:4]
sys.path.insert(0, root + "/src")
env = {}
try:
    with open(env_file) as fh:
        for raw in fh:
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[len("export "):]
            k, _, v = s.partition("=")
            v = v.strip()
            for q in ('"', "'"):
                if len(v) >= 2 and v[0] == q and v[-1] == q:
                    v = v[1:-1]
            env[k.strip()] = v
except FileNotFoundError:
    pass
from security.credentials import resolve_exchange_credentials
creds = resolve_exchange_credentials(exchange, env, mode="live")
if not creds:
    print("  x no live credentials resolved — cannot probe")
    sys.exit(0)
try:
    import ccxt
except Exception as exc:
    print(f"  ! ccxt not importable ({exc}) — skipping probe")
    sys.exit(0)
try:
    if exchange == "hyperliquid":
        client = ccxt.hyperliquid({
            "walletAddress": creds.get("HYPERLIQUID_WALLET_ADDRESS"),
            "privateKey": creds.get("HYPERLIQUID_PRIVATE_KEY"),
        })
    else:
        cfg = {"apiKey": creds.get(f"{exchange.upper()}_API_KEY"),
               "secret": creds.get(f"{exchange.upper()}_SECRET_KEY") or creds.get(f"{exchange.upper()}_SECRET")}
        pw = creds.get(f"{exchange.upper()}_PASSPHRASE")
        if pw:
            cfg["password"] = pw
        client = getattr(ccxt, exchange)(cfg)
    client.fetch_balance()
    print("  ✓ live fetch_balance succeeded")
except Exception as exc:
    print(f"  x live fetch_balance FAILED: {type(exc).__name__}")
PY
}

# --- Subcommands --------------------------------------------------------------
cmd_list() {
  phase "Exchange credential status"
  printf '  %-13s %-5s %-9s %s\n' "EXCHANGE" "ENV" "STATUS" "VALUE (masked)"
  printf '  %-13s %-5s %-9s %s\n' "--------" "---" "------" "--------------"
  local ex env present total status pvar pval both_warn=""
  for ex in $SUPPORTED_EXCHANGES; do
    for env in demo live; do
      if ! get_fields "$ex" "$env" >/dev/null 2>&1; then
        printf '  %-13s %-5s %-9s %s\n' "$ex" "$env" "n/a" "(not supported)"
        continue
      fi
      read -r present total < <(count_present "$ex" "$env")
      pvar="$(primary_var "$ex" "$env")"
      pval="$(env_get "$pvar")"
      if [ "$present" -eq 0 ]; then
        status="not set"
      elif [ "$present" -lt "$total" ]; then
        status="PARTIAL"
      else
        status="SET"
      fi
      printf '  %-13s %-5s %-9s %s\n' "$ex" "$env" "$status" "$(mask "$pval")"
    done
    # Warn if BOTH demo and live primaries are populated for this exchange.
    if [ -n "$(env_get "$(primary_var "$ex" demo)")" ] 2>/dev/null && \
       get_fields "$ex" demo >/dev/null 2>&1 && \
       [ -n "$(env_get "$(primary_var "$ex" live)")" ]; then
      both_warn="$both_warn $ex"
    fi
  done
  echo
  if [ -n "$both_warn" ]; then
    for ex in $both_warn; do
      warn "$ex: BOTH demo and live keys set — the resolver defaults to DEMO (mode=demo)."
    done
  fi
  local lt; lt="$(env_get HERMX_LIVE_TRADING)"
  info "HERMX_LIVE_TRADING = ${lt:-(unset → false)}"
  info "Note: coinbase is credential-resolvable but NOT wired into the CCXT adapter (live/spot only)."
}

status_one() {
  local ex="$1" mode="$2" present total res
  read -r present total < <(count_present "$ex" "$mode")
  phase "$ex — $mode"
  local line var val
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    var="${line%%:*}"
    val="$(env_get "$var")"
    if [ -n "$val" ]; then
      ok "$var = $(mask "$val")"
    else
      warn "$var = (not set)"
    fi
  done < <(get_fields "$ex" "$mode" 2>/dev/null || true)
  info "vars present: $present/$total"
  res="$(resolver_result "$ex" "$mode")"
  info "resolver ($mode): $res"

  # Precedence warning if the opposite env is also populated.
  local other; [ "$mode" = "demo" ] && other="live" || other="demo"
  if get_fields "$ex" "$other" >/dev/null 2>&1 && [ -n "$(env_get "$(primary_var "$ex" "$other")")" ]; then
    warn "$other keys are also set — resolver mode selects which one is actually used."
  fi

  if ! is_wired "$ex"; then
    warn "$ex is NOT wired into the CCXT adapter (credentials resolve but no adapter path)."
  fi

  # Optional live network probe.
  if [ "$mode" = "live" ]; then
    local lt; lt="$(env_get HERMX_LIVE_TRADING)"
    if [ "$(printf '%s' "${lt:-}" | tr '[:upper:]' '[:lower:]')" = "true" ] && [ -t 0 ]; then
      local ans=""
      read -rp "  Run live fetch_balance probe against $ex? [y/N] " ans || ans=""
      case "$ans" in
        y|Y|yes|YES) live_probe "$ex" ;;
        *) info "skipped live probe" ;;
      esac
    else
      info "live probe skipped (HERMX_LIVE_TRADING is not true, or no TTY)."
    fi
  fi
}

cmd_status() {
  shift # drop "status"
  local EX="" MODE="" a
  for a in "$@"; do
    case "$a" in
      --demo) MODE="demo" ;;
      --live) MODE="live" ;;
      --*) err "Unknown flag: $a"; usage; exit 2 ;;
      *) [ -z "$EX" ] && EX="$a" || { err "Unexpected argument: $a"; exit 2; } ;;
    esac
  done
  validate_exchange "$EX"
  if [ -n "$MODE" ]; then
    status_one "$EX" "$MODE"
  else
    status_one "$EX" demo
    status_one "$EX" live
  fi
}

# Shared arg parse for add/update/remove: requires exchange + explicit env flag.
parse_mutate_args() {
  MX_EX=""; MX_MODE=""
  shift # drop subcommand
  local a
  for a in "$@"; do
    case "$a" in
      --demo) MX_MODE="demo" ;;
      --live) MX_MODE="live" ;;
      --*) err "Unknown flag: $a"; usage; exit 2 ;;
      *) [ -z "$MX_EX" ] && MX_EX="$a" || { err "Unexpected argument: $a"; exit 2; } ;;
    esac
  done
  validate_exchange "$MX_EX"
  if [ -z "$MX_MODE" ]; then
    err "You must pass an explicit --demo or --live (ambiguity is a safety footgun)."
    exit 2
  fi
  if ! get_fields "$MX_EX" "$MX_MODE" >/dev/null 2>&1; then
    err "coinbase sandbox not supported in current ccxt version. Use --live for spot trading only."
    exit 1
  fi
}

cmd_add() {
  parse_mutate_args "$@"
  local ex="$MX_EX" mode="$MX_MODE"
  require_tty

  phase "Add $mode credentials for $ex"

  # Precedence warning if the opposite env is already populated.
  local other; [ "$mode" = "demo" ] && other="live" || other="demo"
  if get_fields "$ex" "$other" >/dev/null 2>&1 && [ -n "$(env_get "$(primary_var "$ex" "$other")")" ]; then
    warn "$other keys are already populated — with both set the resolver defaults to DEMO and will SHADOW the other env."
  fi

  # Live requires an extra typed confirmation.
  if [ "$mode" = "live" ]; then
    warn "You are about to store LIVE (real-money) credentials for $ex."
    local confirm=""
    read -rp "  Type exactly 'yes, add live $ex' to continue: " confirm || confirm=""
    if [ "$confirm" != "yes, add live $ex" ]; then
      err "confirmation mismatch — aborted."; exit 1
    fi
  fi

  # Prompt each field.
  # Field list on FD 3 so the interactive prompts below can read the terminal (FD 0).
  local line var label secret VALUE
  local -a NAMES=() VALUES=()
  while IFS= read -r line <&3; do
    [ -n "$line" ] || continue
    var="${line%%:*}"
    label="${line#*:}"; label="${label%:*}"
    secret="${line##*:}"
    VALUE=""
    if [ "$secret" = "y" ]; then
      read -rs -p "  $label: " VALUE || VALUE=""; echo
    else
      read -rp "  $label: " VALUE || VALUE=""
    fi
    VALUE="$(trim_value "$VALUE")"
    if [ -z "$VALUE" ]; then err "empty value for $var — aborted (no partial write)."; exit 1; fi
    NAMES+=("$var"); VALUES+=("$VALUE")
  done 3< <(get_fields "$ex" "$mode")

  # Preview (masked) then write.
  phase "Preview"
  local i
  for i in "${!NAMES[@]}"; do info "${NAMES[$i]} = $(mask "${VALUES[$i]}")"; done
  read -rp "  Press Enter to write, or Ctrl+C to abort " _ || true

  backup_env
  for i in "${!NAMES[@]}"; do write_env_var "${NAMES[$i]}" "${VALUES[$i]}"; done
  ok "wrote ${#NAMES[@]} var(s) to .env (backup at .env.bak)"

  if [ "$mode" = "live" ]; then
    local lt; lt="$(env_get HERMX_LIVE_TRADING)"
    if [ "$(printf '%s' "${lt:-}" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
      warn "HERMX_LIVE_TRADING is still false — adding live keys does NOT arm the system."
      info "To arm: set HERMX_LIVE_TRADING=true in .env (and promote a strategy to execution_mode: live)."
    fi
  fi

  post_write_smoke "$ex" "$mode"
  restart_reminder
}

cmd_update() {
  parse_mutate_args "$@"
  local ex="$MX_EX" mode="$MX_MODE"
  require_tty

  phase "Update $mode credentials for $ex"

  if [ "$mode" = "live" ]; then
    warn "Updating LIVE (real-money) credentials for $ex."
    local confirm=""
    read -rp "  Type exactly 'yes, add live $ex' to continue: " confirm || confirm=""
    if [ "$confirm" != "yes, add live $ex" ]; then
      err "confirmation mismatch — aborted."; exit 1
    fi
  fi

  # Field list on FD 3 so the interactive prompts below can read the terminal (FD 0).
  local line var label secret VALUE cur changed=0
  local -a NAMES=() VALUES=()
  while IFS= read -r line <&3; do
    [ -n "$line" ] || continue
    var="${line%%:*}"
    label="${line#*:}"; label="${label%:*}"
    secret="${line##*:}"
    cur="$(env_get "$var")"
    info "$var  Current: $(mask "$cur")  (Enter to keep)"
    VALUE=""
    if [ "$secret" = "y" ]; then
      read -rs -p "  new $label: " VALUE || VALUE=""; echo
    else
      read -rp "  new $label: " VALUE || VALUE=""
    fi
    VALUE="$(trim_value "$VALUE")"
    if [ -n "$VALUE" ]; then
      NAMES+=("$var"); VALUES+=("$VALUE"); changed=$((changed + 1))
    fi
  done 3< <(get_fields "$ex" "$mode")

  if [ "$changed" -eq 0 ]; then
    warn "no fields changed — nothing to write."; exit 0
  fi

  phase "Preview (changed only)"
  local i
  for i in "${!NAMES[@]}"; do info "${NAMES[$i]} = $(mask "${VALUES[$i]}")"; done
  read -rp "  Press Enter to write, or Ctrl+C to abort " _ || true

  backup_env
  for i in "${!NAMES[@]}"; do write_env_var "${NAMES[$i]}" "${VALUES[$i]}"; done
  ok "updated ${#NAMES[@]} var(s) in .env (backup at .env.bak)"

  post_write_smoke "$ex" "$mode"
  restart_reminder
}

cmd_remove() {
  parse_mutate_args "$@"
  local ex="$MX_EX" mode="$MX_MODE"

  phase "Remove $mode credentials for $ex"

  # Strategy-impact check.
  local hits=""
  if [ -d "$STRATEGIES_DIR" ]; then
    local f venue emode name
    for f in "$STRATEGIES_DIR"/*.json; do
      [ -e "$f" ] || continue
      venue="$("$PYTHON" - "$f" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
inst = d.get("instrument", {}) if isinstance(d, dict) else {}
print(str(inst.get("exchange", "")).lower())
print(str(d.get("execution_mode", "")))
PY
)"
      emode="$(printf '%s\n' "$venue" | sed -n '2p')"
      venue="$(printf '%s\n' "$venue" | sed -n '1p')"
      name="$(basename "$f")"
      if [ "$venue" = "$ex" ]; then
        hits="$hits\n    $name (execution_mode: ${emode:-?})"
      fi
    done
  fi
  if [ -n "$hits" ]; then
    warn "Strategies reference exchange '$ex':"
    printf '%b\n' "$hits"
    warn "Removing these credentials may disarm/error those strategies."
  fi

  # Explicit typed confirmation.
  local confirm=""
  read -rp "  Type exactly 'yes, remove $mode $ex' to continue: " confirm || confirm=""
  if [ "$confirm" != "yes, remove $mode $ex" ]; then
    err "confirmation mismatch — aborted."; exit 1
  fi

  backup_env
  local line var
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    var="${line%%:*}"
    write_env_var "$var" ""
  done < <(get_fields "$ex" "$mode")
  ok "blanked $mode credentials for $ex (backup at .env.bak)"

  local res; res="$(resolver_result "$ex" "$mode")"
  if [ "$res" = "MISSING" ]; then
    ok "resolver ($mode) now reports MISSING — credentials cleared."
  else
    warn "resolver ($mode) reports $res — the opposite env may still supply keys (precedence)."
  fi
  restart_reminder
}

# --- Post-write helpers -------------------------------------------------------
post_write_smoke() {
  local ex="$1" mode="$2" res
  res="$(resolver_result "$ex" "$mode")"
  case "$res" in
    OK)      ok "smoke test: resolver ($mode) = OK" ;;
    PARTIAL) warn "smoke test: resolver ($mode) = PARTIAL (some required vars still missing)" ;;
    *)       warn "smoke test: resolver ($mode) = $res" ;;
  esac
}

restart_reminder() {
  echo
  info "To apply: sudo systemctl restart hermx-receiver hermx-dashboard"
  info "  (fallback: bash run.sh --skip-tests, or docker compose restart)"
}

# --- Main dispatch ------------------------------------------------------------
main() {
  local subcmd="${1:-}"
  case "$subcmd" in
    list)          cmd_list ;;
    status)        cmd_status "$@" ;;
    add)           cmd_add    "$@" ;;
    update)        cmd_update "$@" ;;
    remove)        cmd_remove "$@" ;;
    -h|--help|help|"") usage; [ -n "$subcmd" ] && exit 0 || exit 2 ;;
    *)             err "Unknown subcommand: $subcmd"; usage; exit 2 ;;
  esac
}
main "$@"
