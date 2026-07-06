#!/usr/bin/env bash
# hermes-gateway.sh — HermX Telegram operator-gateway manager (run ON the VPS over SSH)
#
# Manages TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS in ~/.hermes/.env (the Hermes
# agent's env — NOT the HermX .env) and drives the `hermes gateway` lifecycle.
# The bot token is captured via `read -s` INSIDE this script. A secret is NEVER
# accepted as a command-line argument and is NEVER echoed back or passed to an
# external command as an argv token. (Same posture as scripts/exchange.sh.)
#
# Usage:
#   bash scripts/hermes-gateway.sh status
#   bash scripts/hermes-gateway.sh setup
#   bash scripts/hermes-gateway.sh rotate
#   bash scripts/hermes-gateway.sh allow  <numeric-user-id>
#   bash scripts/hermes-gateway.sh revoke <numeric-user-id>
#   bash scripts/hermes-gateway.sh remove              (alias: disable)
#   bash scripts/hermes-gateway.sh start|stop|restart
#   bash scripts/hermes-gateway.sh test
#
# Subcommands: status, setup, rotate, allow, revoke, remove|disable, start, stop, restart, test
# -E (errtrace) so the restore-on-failure ERR trap fires inside functions too.
set -Eeuo pipefail

HERMES_DIR="$HOME/.hermes"
ENV_FILE="$HERMES_DIR/.env"

# --- Colours + logging (same style as exchange.sh / deploy.sh) ----------------
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

usage() {
  cat <<EOF
Usage: bash scripts/hermes-gateway.sh <subcommand> [args]

Subcommands:
  status               Masked token + allowlist + gateway process state
  setup                Prompt for bot token + allowlist and write ~/.hermes/.env
  rotate               Change the bot token (Enter = keep current, abort)
  allow  <user-id>     Add a numeric Telegram user id to the allowlist
  revoke <user-id>     Remove a numeric Telegram user id from the allowlist
  remove | disable     Blank both vars and stop the gateway
  start|stop|restart   Drive the hermes gateway service
  test                 Print the 4 smoke-test messages to send from your phone

The bot token is captured interactively (read -s) — never pass it as an argument.
Config target: ~/.hermes/.env (Hermes agent env — NOT the HermX .env).
EOF
}

# --- Small helpers (mirrors scripts/exchange.sh) -------------------------------
# env_get VARNAME → prints the value from ~/.hermes/.env (last definition wins).
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

# backup_env — one rolling backup before any write.
backup_env() { [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null || true; }

# restore_env — ERR-trap target: put the pre-write backup back so a failed write
# section can never leave ~/.hermes/.env half-written.
restore_env() {
  if [ -f "$ENV_FILE.bak" ]; then
    cp "$ENV_FILE.bak" "$ENV_FILE" 2>/dev/null || true
    err "write failed — restored $ENV_FILE from $ENV_FILE.bak"
  fi
}

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

# valid_user_id ID → 0 iff ID is a non-empty string of digits.
valid_user_id() {
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac
}

# valid_allowlist VALUE → 0 iff comma-separated numeric ids; rejects blank and
# allow-all forms ('*', 'all') — never allow-all on a trading agent.
valid_allowlist() {
  local v="${1:-}" u OLDIFS
  [ -n "$v" ] || return 1
  case "$v" in *'*'*|all|ALL) return 1 ;; esac
  OLDIFS="$IFS"; IFS=','
  for u in $v; do
    case "$u" in ''|*[!0-9]*) IFS="$OLDIFS"; return 1 ;; esac
  done
  IFS="$OLDIFS"
  return 0
}

have_hermes() { command -v hermes >/dev/null 2>&1; }

# gateway_state — best-effort process state; any failure is UNKNOWN, never a guess.
gateway_state() {
  if have_hermes; then
    hermes gateway status || warn "hermes gateway status exited non-zero — gateway state UNKNOWN"
  else
    warn "hermes CLI not found on PATH — gateway process state UNKNOWN"
  fi
}

apply_reminder() {
  echo
  info "To apply: hermes gateway restart   (or: bash scripts/hermes-gateway.sh restart)"
  info "  The gateway reads ~/.hermes/.env at start — changes are inert until restarted."
}

# --- Subcommands ---------------------------------------------------------------
cmd_status() {
  phase "Telegram operator gateway — status"
  local tok al
  tok="$(env_get TELEGRAM_BOT_TOKEN)"
  al="$(env_get TELEGRAM_ALLOWED_USERS)"
  if [ -n "$tok" ]; then
    ok "TELEGRAM_BOT_TOKEN = $(mask "$tok")"
  else
    warn "TELEGRAM_BOT_TOKEN = (not set)"
  fi
  if [ -n "$al" ]; then
    ok "TELEGRAM_ALLOWED_USERS = $al"
  else
    warn "TELEGRAM_ALLOWED_USERS = (not set)"
  fi
  if [ -n "$tok" ] && [ -z "$al" ]; then
    warn "token set but allowlist empty — the gateway denies ALL senders (safe default). Use: allow <id>"
  fi
  case ",$al," in
    *",*,"*|*",all,"*|*",ALL,"*) warn "allowlist contains an allow-all entry — NEVER allow-all on a trading agent" ;;
  esac
  phase "Gateway process"
  gateway_state
}

cmd_setup() {
  require_tty
  phase "Set up Telegram operator gateway"
  info "Config target: $ENV_FILE (Hermes agent env — NOT the HermX .env)"
  local existing; existing="$(env_get TELEGRAM_BOT_TOKEN)"
  if [ -n "$existing" ]; then
    warn "a bot token is already set ($(mask "$existing")) — setup will overwrite it (use 'rotate' to change only the token)"
  fi
  info "Create the bot via @BotFather; get your numeric id from @userinfobot (see setup/09-hermes-agent.md)."

  local TOKEN="" USERS=""
  read -rs -p "  bot token (from @BotFather): " TOKEN || TOKEN=""; echo
  TOKEN="$(trim_value "$TOKEN")"
  if [ -z "$TOKEN" ]; then err "empty token — aborted (no partial write)."; exit 1; fi

  read -rp "  allowed user id(s), comma-separated numbers only: " USERS || USERS=""
  USERS="$(trim_value "$USERS")"
  if ! valid_allowlist "$USERS"; then
    err "invalid allowlist — must be comma-separated numeric Telegram user ids; never blank or allow-all on a trading agent."
    exit 1
  fi

  phase "Preview"
  info "TELEGRAM_BOT_TOKEN = $(mask "$TOKEN")"
  info "TELEGRAM_ALLOWED_USERS = $USERS"
  read -rp "  Press Enter to write, or Ctrl+C to abort " _ || true

  mkdir -p "$HERMES_DIR"
  backup_env
  trap restore_env ERR
  write_env_var TELEGRAM_BOT_TOKEN "$TOKEN"
  write_env_var TELEGRAM_ALLOWED_USERS "$USERS"
  trap - ERR
  ok "wrote 2 var(s) to $ENV_FILE (backup at $ENV_FILE.bak, mode 600)"

  if have_hermes; then
    local ans=""
    read -rp "  Run 'hermes gateway setup' (one-time platform wizard) now? [y/N] " ans || ans=""
    case "$ans" in
      y|Y|yes|YES) hermes gateway setup || warn "wizard exited non-zero — re-run 'hermes gateway setup' before starting" ;;
      *) info "skipped wizard — first-time installs need 'hermes gateway setup' once before 'start'." ;;
    esac
  else
    warn "hermes CLI not found — install it, then run 'hermes gateway setup' + 'hermes gateway start'."
  fi
  apply_reminder
}

cmd_rotate() {
  require_tty
  phase "Rotate Telegram bot token"
  local cur TOKEN=""
  cur="$(env_get TELEGRAM_BOT_TOKEN)"
  if [ -z "$cur" ]; then err "no token set — use 'setup' first."; exit 1; fi
  info "TELEGRAM_BOT_TOKEN  Current: $(mask "$cur")  (Enter to keep)"
  read -rs -p "  new bot token: " TOKEN || TOKEN=""; echo
  TOKEN="$(trim_value "$TOKEN")"
  if [ -z "$TOKEN" ]; then warn "no change — nothing to write."; exit 0; fi

  phase "Preview"
  info "TELEGRAM_BOT_TOKEN = $(mask "$TOKEN")"
  read -rp "  Press Enter to write, or Ctrl+C to abort " _ || true

  backup_env
  trap restore_env ERR
  write_env_var TELEGRAM_BOT_TOKEN "$TOKEN"
  trap - ERR
  ok "rotated token in $ENV_FILE (backup at $ENV_FILE.bak)"
  info "Remember to also revoke the OLD token with @BotFather (/revoke) if it may be compromised."
  apply_reminder
}

cmd_allow() {
  local id="${1:-}"
  if ! valid_user_id "$id"; then
    err "allow requires a numeric Telegram user id (got '${id:-<none>}')"; exit 2
  fi
  local cur new
  cur="$(env_get TELEGRAM_ALLOWED_USERS)"
  case ",$cur," in
    *",$id,"*) ok "user $id is already on the allowlist ($cur) — nothing to do."; exit 0 ;;
  esac
  [ -n "$cur" ] && new="$cur,$id" || new="$id"

  phase "Allow Telegram user $id"
  info "current allowlist: ${cur:-(empty — gateway denies all)}"
  info "new allowlist:     $new"
  local confirm=""
  read -rp "  Type exactly 'yes, allow $id' to continue: " confirm || confirm=""
  if [ "$confirm" != "yes, allow $id" ]; then
    err "confirmation mismatch — aborted."; exit 1
  fi

  mkdir -p "$HERMES_DIR"
  backup_env
  trap restore_env ERR
  write_env_var TELEGRAM_ALLOWED_USERS "$new"
  trap - ERR
  ok "allowlist is now: $new (backup at $ENV_FILE.bak)"
  apply_reminder
}

cmd_revoke() {
  local id="${1:-}"
  if ! valid_user_id "$id"; then
    err "revoke requires a numeric Telegram user id (got '${id:-<none>}')"; exit 2
  fi
  local cur new="" u OLDIFS
  cur="$(env_get TELEGRAM_ALLOWED_USERS)"
  case ",$cur," in
    *",$id,"*) : ;;
    *) err "user $id is not on the allowlist (${cur:-(empty)}) — nothing to revoke."; exit 1 ;;
  esac
  OLDIFS="$IFS"; IFS=','
  for u in $cur; do
    [ "$u" = "$id" ] && continue
    [ -n "$u" ] && new="${new:+$new,}$u"
  done
  IFS="$OLDIFS"

  phase "Revoke Telegram user $id"
  info "current allowlist: $cur"
  info "new allowlist:     ${new:-(EMPTY)}"
  if [ -z "$new" ]; then
    warn "allowlist will be EMPTY — the gateway denies ALL senders (safe default, but operator control goes dark)."
    warn "Use 'remove' instead if you also want the gateway stopped."
  fi
  local confirm=""
  read -rp "  Type exactly 'yes, revoke $id' to continue: " confirm || confirm=""
  if [ "$confirm" != "yes, revoke $id" ]; then
    err "confirmation mismatch — aborted."; exit 1
  fi

  backup_env
  trap restore_env ERR
  write_env_var TELEGRAM_ALLOWED_USERS "$new"
  trap - ERR
  ok "allowlist is now: ${new:-(empty — denies all)} (backup at $ENV_FILE.bak)"
  apply_reminder
}

cmd_remove() {
  phase "Remove Telegram gateway configuration"
  warn "This blanks TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS and stops the gateway."
  info "Execution (webhook→order path) is unaffected — the gateway is comms-only."
  local confirm=""
  read -rp "  Type exactly 'yes, remove telegram' to continue: " confirm || confirm=""
  if [ "$confirm" != "yes, remove telegram" ]; then
    err "confirmation mismatch — aborted."; exit 1
  fi

  backup_env
  trap restore_env ERR
  write_env_var TELEGRAM_BOT_TOKEN ""
  write_env_var TELEGRAM_ALLOWED_USERS ""
  trap - ERR
  ok "blanked telegram vars in $ENV_FILE (backup at $ENV_FILE.bak — it still holds the old token)"
  info "Also revoke the bot token with @BotFather (/revoke) so the backup copy is inert."

  if have_hermes; then
    if hermes gateway stop; then
      ok "gateway stopped."
    else
      warn "hermes gateway stop exited non-zero — gateway state UNKNOWN (check: hermes gateway status)"
    fi
  else
    warn "hermes CLI not found — stop the gateway manually if it is running."
  fi
}

cmd_lifecycle() {
  local verb="$1"
  if ! have_hermes; then
    err "hermes CLI not found on PATH — cannot $verb the gateway (state UNKNOWN)."; exit 1
  fi
  phase "Gateway $verb"
  if hermes gateway "$verb"; then
    ok "hermes gateway $verb ok"
  else
    err "hermes gateway $verb FAILED — gateway state UNKNOWN (check: hermes gateway status)"; exit 1
  fi
  if [ "$verb" != "stop" ]; then
    gateway_state
  fi
}

cmd_test() {
  phase "Telegram smoke test"
  info "From your phone, message the bot and confirm it answers each:"
  info "  1. what's open?"
  info "  2. what's our PnL?"
  info "  3. is the system armed?"
  info "  4. what was the last signal?"
  info "A failed or stale read must come back UNKNOWN — never 'flat'."
  info "No reply at all → check: bash scripts/hermes-gateway.sh status"
}

# --- Main dispatch --------------------------------------------------------------
main() {
  local subcmd="${1:-}"
  case "$subcmd" in
    status)             cmd_status ;;
    setup)              cmd_setup ;;
    rotate)             cmd_rotate ;;
    allow)              shift; cmd_allow  "${1:-}" ;;
    revoke)             shift; cmd_revoke "${1:-}" ;;
    remove|disable)     cmd_remove ;;
    start|stop|restart) cmd_lifecycle "$subcmd" ;;
    test)               cmd_test ;;
    -h|--help|help|"")  usage; [ -n "$subcmd" ] && exit 0 || exit 2 ;;
    *)                  err "Unknown subcommand: $subcmd"; usage; exit 2 ;;
  esac
}
# Source guard: `source scripts/hermes-gateway.sh` exposes the functions (tests
# exercise the production upsert/backup/mask code directly) without dispatching.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
