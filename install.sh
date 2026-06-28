#!/usr/bin/env bash
#
# install.sh — HermX guided installer
#
# Automates INSTALL.md phases 1-5 + verification (phase 8) for the HermX
# deterministic crypto trading-signal execution system. Run from the repo root:
#
#   bash install.sh
#
# Safe by default: writes HERMX_LIVE_TRADING=false and copies a demo runtime
# profile. Nothing is ever sent to a live exchange by this script. INSTALL.md remains
# the reference for every decision made here — consult it when a step fails.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Globals + helpers
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"
OS=""               # "linux" | "macos"
IS_VPS="false"
PY=""               # resolved python interpreter (python3.11 preferred)

# Colours (fall back to empty strings if not a tty)
if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

phase()  { printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info()   { printf '  %s\n' "$1"; }
ok()     { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()   { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()    { printf '  %sx%s %s\n' "$RED" "$RESET" "$1"; }

# ask "Question" "default(y/n)"  -> returns 0 for yes, 1 for no
ask() {
  local prompt="$1" default="${2:-n}" reply suffix
  if [[ "$default" == "y" ]]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
  read -r -p "  $prompt $suffix " reply || true
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

# set_env KEY VALUE  -> idempotently upsert KEY=VALUE in $ENV_FILE
set_env() {
  local key="$1"; shift
  local val="$*"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$ENV_FILE" ]]; then
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  fi
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Exchange picker (STEP 3)
# ---------------------------------------------------------------------------
# id | label | env prefix | comma-separated credential fields
EXCHANGE_TABLE=(
  "okx|OKX (recommended)|OKX_DEMO|apiKey,secret,passphrase"
  "binance|Binance|BINANCE_TESTNET|apiKey,secret"
  "bybit|Bybit|BYBIT_TESTNET|apiKey,secret"
  "kucoin|KuCoin|KUCOIN_PAPER|apiKey,secret_bare,passphrase"
  "bitget|Bitget|BITGET_DEMO|apiKey,secret,passphrase"
  "gate|Gate.io|GATE_TESTNET|apiKey,secret"
  "coinbase|Coinbase Advanced|COINBASE_SANDBOX|apiKey,secret"
  "hyperliquid|Hyperliquid|HYPERLIQUID|wallet_address,private_key"
)

pick_exchange() {
  info "Which exchange do you want to use? Keys MUST come from that exchange's"
  info "demo / sandbox / testnet environment — never a live account."
  echo

  local i=1 row label
  for row in "${EXCHANGE_TABLE[@]}"; do
    label="$(echo "$row" | cut -d'|' -f2)"
    printf '    %2d) %s\n' "$i" "$label"
    i=$((i + 1))
  done
  echo

  local choice ex_id ex_label prefix fields
  while true; do
    read -r -p "  Pick an exchange [1-${#EXCHANGE_TABLE[@]}] (default 1=OKX): " choice
    choice="${choice:-1}"
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#EXCHANGE_TABLE[@]} )); then
      break
    fi
    warn "Enter a number between 1 and ${#EXCHANGE_TABLE[@]}."
  done

  row="${EXCHANGE_TABLE[$((choice - 1))]}"
  ex_id="$(echo "$row"    | cut -d'|' -f1)"
  ex_label="$(echo "$row" | cut -d'|' -f2)"
  prefix="$(echo "$row"   | cut -d'|' -f3)"
  fields="$(echo "$row"   | cut -d'|' -f4)"

  echo
  ok "Selected: $ex_label  (env prefix ${prefix}_*)"
  info "Enter the credentials from your ${ex_label} demo/sandbox/testnet account."
  echo

  # Prompt for each declared field and write the matching env var.
  local field val
  IFS=',' read -r -a field_arr <<< "$fields"
  for field in "${field_arr[@]}"; do
    case "$field" in
      apiKey)
        read -r -p "  API Key: " val
        set_env "${prefix}_API_KEY" "$val"
        ;;
      secret)
        read -r -s -p "  Secret: " val; echo
        set_env "${prefix}_SECRET_KEY" "$val"
        ;;
      secret_bare)
        # KuCoin's resolver reads <PREFIX>_SECRET (no _KEY suffix).
        read -r -s -p "  Secret: " val; echo
        set_env "${prefix}_SECRET" "$val"
        ;;
      passphrase)
        read -r -s -p "  Passphrase: " val; echo
        set_env "${prefix}_PASSPHRASE" "$val"
        ;;
      wallet_address)
        read -r -p "  Wallet Address: " val
        set_env "${prefix}_WALLET_ADDRESS" "$val"
        ;;
      private_key)
        read -r -s -p "  Private Key: " val; echo
        set_env "${prefix}_PRIVATE_KEY" "$val"
        ;;
      *)
        warn "Unknown credential field '$field' — skipping."
        ;;
    esac
  done

  # Exchange routing markers consumed by the runtime config selection.
  set_env "HERMX_EXCHANGE" "$ex_id"
  set_env "HERMX_CCXT_EXCHANGE" "$ex_id"

  # Global live-trading kill switch -- written false for every venue, so a fresh
  # install is always demo/sandbox until the operator deliberately goes live.
  set_env "HERMX_LIVE_TRADING" "false"

  # OKX keeps its legacy IPv4 pin.
  if [[ "$ex_id" == "okx" ]]; then
    set_env "OKX_FORCE_IPV4" "1"
  fi

  # Select the matching DISARMED runtime profile -> shadow-config.json. OKX is the
  # reference venue and ships as the generic config/runtime.demo.json (no okx-suffixed file).
  local cfg="config/runtime.${ex_id}.demo.json"
  [[ "$ex_id" == "okx" ]] && cfg="config/runtime.demo.json"
  if [[ -f "$cfg" ]]; then
    cp "$cfg" shadow-config.json
    ok "Copied $cfg -> shadow-config.json"
  elif [[ -f "config/runtime.demo.json" ]]; then
    cp "config/runtime.demo.json" shadow-config.json
    warn "$cfg not found — fell back to config/runtime.demo.json -> shadow-config.json"
  else
    err "No runtime config found to copy to shadow-config.json."
  fi
}

# ===========================================================================
# PHASE 0 — Detect OS + prerequisites
# ===========================================================================
phase "PHASE 0: Detect OS + Prerequisites"

case "$(uname -s)" in
  Linux)  OS="linux" ;;
  Darwin) OS="macos" ;;
  *) err "Unsupported OS: $(uname -s). HermX supports Linux (VPS) and macOS."; exit 1 ;;
esac
ok "Detected OS: $OS"

# Resolve a python interpreter (prefer 3.11).
if have python3.11; then PY="python3.11"; elif have python3; then PY="python3"; fi
if [[ -n "$PY" ]]; then
  ok "Python: $($PY --version 2>&1)"
else
  warn "python3 not found yet — Phase 1 will install it."
fi

for tool in git curl; do
  if have "$tool"; then ok "$tool present"; else warn "$tool missing — Phase 1 will install it."; fi
done

if [[ "$OS" == "linux" ]]; then
  if have systemctl; then ok "systemd available (systemd deploy path supported)"; else warn "systemctl not found — only the Docker deploy path will work."; fi
fi

echo
if ask "Is this a VPS (Linux server)?" "n"; then
  IS_VPS="true"
  ok "Running in VPS mode."
else
  IS_VPS="false"
  info "Running in local mode (foreground services)."
fi

# ===========================================================================
# PHASE 1 — System dependencies
# ===========================================================================
phase "PHASE 1: System Dependencies"

if [[ "$OS" == "linux" ]]; then
  if have python3.11 && have git && have curl; then
    ok "Core dependencies already satisfied — skipping apt install."
  else
    info "Installing python3.11, pip, git, curl via apt (needs sudo)..."
    sudo apt-get update
    sudo apt-get install -y python3.11 python3.11-venv python3-pip curl git
  fi
  # Re-resolve python after a possible install.
  if have python3.11; then PY="python3.11"; fi
else
  # macOS
  if have brew; then
    if have python3.11 && have git; then
      ok "Homebrew dependencies already satisfied — skipping brew install."
    else
      info "Installing python@3.11 and git via Homebrew..."
      brew install python@3.11 git
    fi
  else
    warn "Homebrew not found. Install it from https://brew.sh then re-run, or"
    warn "ensure python3.11 and git are already on PATH."
  fi
  if have python3.11; then PY="python3.11"; elif have python3; then PY="python3"; fi
fi

[[ -n "$PY" ]] || { err "No python interpreter available after Phase 1."; exit 1; }
ok "Using interpreter: $PY ($($PY --version 2>&1))"

# ===========================================================================
# PHASE 2 — Configure .env
# ===========================================================================
phase "PHASE 2: Configure .env"

WRITE_ENV="true"
if [[ -f "$ENV_FILE" ]]; then
  if ask ".env already exists — update it?" "n"; then
    WRITE_ENV="true"
    info "Updating existing .env in place."
  else
    WRITE_ENV="false"
    warn "Leaving existing .env untouched."
  fi
fi

if [[ "$WRITE_ENV" == "true" ]]; then
  # Seed from the template if .env is brand new.
  if [[ ! -f "$ENV_FILE" && -f setup/env.example ]]; then
    cp setup/env.example "$ENV_FILE"
    info "Seeded .env from setup/env.example."
  fi

  # Exchange selection + credentials + runtime profile (STEP 3).
  pick_exchange

  # Generate a strong webhook secret.
  if have openssl; then
    SECRET="$(openssl rand -hex 32)"
    set_env "SHADOW_WEBHOOK_SECRET" "$SECRET"
    ok "Generated SHADOW_WEBHOOK_SECRET (saved to .env)."
    info "Webhook secret: $SECRET"
    info "(Paste this exact value as the X-Webhook-Secret header in TradingView.)"
  else
    warn "openssl not found — set SHADOW_WEBHOOK_SECRET in .env manually."
  fi

  chmod 600 "$ENV_FILE"
  ok ".env written, HERMX_LIVE_TRADING=false (safe by default)"
else
  ok "Skipped .env configuration."
fi

# ===========================================================================
# PHASE 3 — Strategy selection
# ===========================================================================
phase "PHASE 3: Strategy Selection"

ENABLED_FILE="$REPO_ROOT/ENABLED_STRATEGIES.txt"
: > "$ENABLED_FILE"
enabled_count=0

# tiny JSON field reader (string or number) without requiring jq. Supports
# dotted paths for the schema_version 2 nested blocks (e.g. capital.budget_usd,
# instrument.inst_id).
json_get() {
  local file="$1" key="$2"
  $PY - "$file" "$key" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        data = json.load(fh)
    val = data
    for part in sys.argv[2].split("."):
        val = val.get(part, "") if isinstance(val, dict) else ""
    print(val if val is not None else "")
except Exception:
    print("")
PYEOF
}

shopt -s nullglob
strategy_files=(strategies/*.json)
shopt -u nullglob

if (( ${#strategy_files[@]} == 0 )); then
  warn "No strategy files found under strategies/ — skipping."
else
  for f in "${strategy_files[@]}"; do
    # schema_version 2 strategy files: instrument + budget live in nested blocks,
    # and there is no asset/status field. Derive the display asset from the id.
    sid="$(json_get "$f" strategy_id)"
    inst="$(json_get "$f" instrument.inst_id)"
    budget="$(json_get "$f" capital.budget_usd)"
    lev="$(json_get "$f" leverage)"
    mode="$(json_get "$f" execution_mode)"
    submit="$(json_get "$f" submit_orders)"
    asset="$(printf '%s' "${sid%%_*}" | tr '[:lower:]' '[:upper:]')"
    echo
    info "Strategy: ${BOLD}${sid}${RESET}"
    info "  asset=$asset  inst=$inst  budget_usd=$budget  leverage=${lev}x  execution_mode=$mode  submit_orders=$submit"
    if ask "Enable this strategy?" "y"; then
      # Confirm risk params (display only; editing JSON is left to the operator).
      if ! ask "Keep budget \$$budget and ${lev}x leverage?" "y"; then
        warn "To change risk, edit $f directly (capital.budget_usd / leverage), then re-run."
      fi
      printf '%s %s %s\n' "$sid" "$asset" "$(json_get "$f" timeframe)" >> "$ENABLED_FILE"
      enabled_count=$((enabled_count + 1))
      ok "Enabled $sid"
    else
      info "Skipped $sid (set submit_orders=false in $f to keep it inert)."
    fi
  done
fi

echo
ok "Enabled $enabled_count strategies. Each needs one BUY + one SELL alert in TradingView."
info "Enabled IDs saved to ENABLED_STRATEGIES.txt"

# ===========================================================================
# PHASE 4 — Tailscale
# ===========================================================================
phase "PHASE 4: Tailscale (stable public URL)"

if ! have tailscale; then
  warn "tailscale is not installed."
  if [[ "$OS" == "linux" ]]; then
    info "Install with:  curl -fsSL https://tailscale.com/install.sh | sh"
  else
    info "Install Tailscale for macOS from the App Store or https://tailscale.com/download"
  fi
  read -r -p "  Install Tailscale in another terminal, then press Enter to continue (or Ctrl-C to abort)... " _ || true
fi

if have tailscale; then
  if ! tailscale status >/dev/null 2>&1; then
    warn "Tailscale is installed but not connected."
    info "Connect with:  sudo tailscale up --hostname=hermx"
    read -r -p "  Run 'tailscale up' in another terminal, then press Enter to continue... " _ || true
  fi

  if tailscale status >/dev/null 2>&1; then
    if ask "Set this device's Tailscale hostname to 'hermx'?" "y"; then
      sudo tailscale set --hostname=hermx || warn "Could not set hostname (continuing)."
    fi

    info "Enabling Tailscale Funnel on port 8891..."
    sudo tailscale funnel --bg 8891 || warn "Funnel command failed — you may need to enable Funnel for your tailnet first."

    # Extract the public URL from funnel status.
    FUNNEL_URL="$(tailscale funnel status 2>/dev/null | grep -oE 'https://[a-zA-Z0-9._-]+\.ts\.net' | head -1 || true)"
    if [[ -n "$FUNNEL_URL" ]]; then
      WEBHOOK_URL="${FUNNEL_URL}/webhook"
      echo "$WEBHOOK_URL" > "$REPO_ROOT/WEBHOOK_URL.txt"
      ok "Webhook URL: $WEBHOOK_URL"
      info "(Saved to WEBHOOK_URL.txt — this is the URL you paste into TradingView.)"
    else
      warn "Could not parse a Funnel URL. Run 'tailscale funnel status' and save the"
      warn "https://hermx.<tailnet>.ts.net/webhook URL to WEBHOOK_URL.txt manually."
    fi

    # Dashboard Funnel (Option A): publish the read-only dashboard on its OWN
    # public port. Funnel only permits 443/8443/10000, so the dashboard takes
    # :8443 (the webhook keeps :443) and forwards to the loopback dashboard 8098.
    DASH_FUNNEL="false"
    if [[ -f "$ENV_FILE" ]] && grep -q '^TS_AUTHKEY=..*' "$ENV_FILE"; then
      DASH_FUNNEL="true"
      info "TS_AUTHKEY is set — enabling the dashboard Funnel automatically."
    elif ask "Also publish the dashboard via a separate Tailscale Funnel (:8443)?" "y"; then
      DASH_FUNNEL="true"
    fi

    if [[ "$DASH_FUNNEL" == "true" ]]; then
      info "Enabling Tailscale Funnel for the dashboard (:8443 -> 8098)..."
      sudo tailscale funnel --bg --https=8443 8098 || warn "Dashboard Funnel command failed — enable Funnel for your tailnet first, then re-run."
      DASH_HOST="$(tailscale funnel status 2>/dev/null | grep -oE 'https://[a-zA-Z0-9._-]+\.ts\.net' | head -1 || true)"
      if [[ -n "$DASH_HOST" ]]; then
        DASHBOARD_URL="${DASH_HOST}:8443/shadow/dashboard"
        echo "$DASHBOARD_URL" > "$REPO_ROOT/DASHBOARD_URL.txt"
        ok "Dashboard URL: $DASHBOARD_URL"
        info "(Saved to DASHBOARD_URL.txt — this public URL still requires HERMX_DASH_AUTH_TOKEN from .env.)"
      else
        warn "Could not parse a dashboard Funnel URL. Run 'tailscale funnel status' and use"
        warn "https://hermx.<tailnet>.ts.net:8443/shadow/dashboard (needs HERMX_DASH_AUTH_TOKEN)."
      fi
    else
      info "Skipped the dashboard Funnel — the dashboard stays loopback-only on :8098."
    fi
  fi
else
  warn "Skipping Funnel setup — tailscale still unavailable."
fi

# ===========================================================================
# PHASE 5 — Deploy
# ===========================================================================
phase "PHASE 5: Deploy"

build_venv() {
  info "Creating .venv and installing requirements..."
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
  ok "Virtualenv ready (.venv)."
}

if [[ "$IS_VPS" == "true" ]]; then
  echo
  info "Deploy options:"
  info "  [A] systemd  (recommended — OS-supervised services)"
  info "  [B] Docker   (containerised)"
  read -r -p "  Choose deploy method [A/b]: " deploy_choice
  deploy_choice="${deploy_choice:-A}"

  if [[ "$deploy_choice" =~ ^[Bb] ]]; then
    if have docker; then
      # Pre-flight: both files are bind-mounted read-only into the containers, so
      # a missing file makes `docker compose up` fail with an opaque mount error.
      preflight_ok="true"
      if [[ ! -f "$REPO_ROOT/shadow-config.json" ]]; then
        err "shadow-config.json is missing — it is bind-mounted into both containers."
        err "Re-run Phase 3 (exchange picker) or copy a config/runtime.*.demo.json profile."
        preflight_ok="false"
      else
        ok "Pre-flight: shadow-config.json present."
      fi
      if [[ ! -f "$ENV_FILE" ]]; then
        err ".env is missing — run Phase 2 (Configure .env) first."
        preflight_ok="false"
      else
        ok "Pre-flight: .env present."
      fi

      if [[ "$preflight_ok" != "true" ]]; then
        err "Aborting Docker deploy until the pre-flight items above are fixed."
      else
        # Tailscale is the public ingress for the Docker deploy: the bridge compose
        # ships a tailscale sidecar that Funnels the receiver and serves the
        # dashboard over the tailnet. cloudflared is not used.
        info "Public ingress: Tailscale (tailscale sidecar, default for Docker)."
        info "Generate a reusable/ephemeral auth key in the Tailscale admin console"
        info "(Settings -> Keys). The sidecar joins your tailnet as 'hermx' and"
        info "Funnels https -> receiver:8891; the dashboard is tailnet-only on :8443."
        if ask "Configure the Tailscale auth key (TS_AUTHKEY) now?" "y"; then
          read -r -p "  Tailscale auth key (tskey-...): " ts_authkey
          if [[ -n "${ts_authkey:-}" ]]; then
            set_env "TS_AUTHKEY" "$ts_authkey"
            ok "TS_AUTHKEY saved to .env (the tailscale sidecar will use it)."
          else
            warn "No key entered — the tailscale sidecar will not connect until TS_AUTHKEY is set in .env."
          fi
        else
          info "Skipped — set TS_AUTHKEY in .env later, or use docker-compose.host.yml (host Tailscale)."
        fi

        info "Building and starting containers (bridge networking + tailscale)..."
        docker compose up -d --build
        ok "Docker services started."
      fi
    else
      err "Docker is not installed. Install it (curl -fsSL https://get.docker.com | sh) and re-run,"
      err "or choose the systemd path instead."
    fi
  else
    build_venv
    info "Installing + starting systemd services (needs sudo)..."
    sudo bash deploy/install-services.sh
    ok "systemd services installed and started."
  fi
else
  # macOS / local foreground
  info "${BOLD}macOS / local detected — using foreground mode (Option A).${RESET}"
  build_venv
  echo
  info "Run these TWO commands, each in its own terminal, from $REPO_ROOT:"
  echo
  RX_CMD="cd '$REPO_ROOT/src' && set -a && source ../.env && set +a && ../.venv/bin/python webhook_receiver.py"
  DB_CMD="cd '$REPO_ROOT/src' && set -a && source ../.env && set +a && ../.venv/bin/python dashboard.py"
  printf '    %s# Terminal 1 — webhook receiver:%s\n' "$YELLOW" "$RESET"
  printf '    %s\n\n' "$RX_CMD"
  printf '    %s# Terminal 2 — dashboard:%s\n' "$YELLOW" "$RESET"
  printf '    %s\n' "$DB_CMD"
  echo
  if [[ "$OS" == "macos" ]] && have osascript; then
    if ask "Open two new Terminal windows with these commands now?" "n"; then
      osascript -e "tell application \"Terminal\" to do script \"$RX_CMD\"" >/dev/null || warn "Could not open receiver terminal."
      osascript -e "tell application \"Terminal\" to do script \"$DB_CMD\"" >/dev/null || warn "Could not open dashboard terminal."
      ok "Opened two Terminal windows."
    fi
  fi
fi

# ===========================================================================
# PHASE 8 — Verify
# ===========================================================================
phase "PHASE 8: Verify"

info "Waiting 5s for services to come up..."
sleep 5

probe() {
  local url="$1" name="$2"
  if curl -sf "$url" >/dev/null 2>&1; then
    ok "$name OK ($url)"
  else
    err "$name FAIL ($url) — check logs (journalctl / docker compose logs / terminal output)."
  fi
}

probe "http://127.0.0.1:8891/health" "Receiver"
probe "http://127.0.0.1:8098/health" "Dashboard"

echo
phase "Installation Summary"
WEBHOOK_DISPLAY="(see WEBHOOK_URL.txt)"
[[ -f "$REPO_ROOT/WEBHOOK_URL.txt" ]] && WEBHOOK_DISPLAY="$(cat "$REPO_ROOT/WEBHOOK_URL.txt")"
DASH_DISPLAY="http://127.0.0.1:8098  (loopback only — no public Funnel)"
[[ -f "$REPO_ROOT/DASHBOARD_URL.txt" ]] && DASH_DISPLAY="$(cat "$REPO_ROOT/DASHBOARD_URL.txt")  (needs HERMX_DASH_AUTH_TOKEN)"
info "Webhook URL:  $WEBHOOK_DISPLAY"
info "Dashboard:    $DASH_DISPLAY"
info "Receiver:     http://127.0.0.1:8891"
info "Enabled:      $enabled_count strategies (ENABLED_STRATEGIES.txt)"
info "Submit gate:  HERMX_LIVE_TRADING=false  (demo — nothing sent to live exchange)"
echo
info "Next: create the TradingView alerts (INSTALL.md Phase 7) and fire a test alert."

# ===========================================================================
# Optional — Hermes Agent + Telegram
# ===========================================================================
echo
if ask "Set up Hermes Agent + Telegram (natural-language operator bot)?" "n"; then
  phase "OPTIONAL: Hermes Agent + Telegram (manual steps)"
  cat <<'HERMES'
  Hermes is a SEPARATE install — these steps are printed, not automated.
  Full detail: INSTALL.md Phase 6 / setup/09-hermes-agent.md

  1) Install the agent:
       curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
       source ~/.bashrc        # or ~/.zshrc
       hermes --version

  2) Configure its own env (~/.hermes/.env — NOT the HermX repo):
       mkdir -p ~/.hermes
       hermes provider setup    # pick xAI/Grok, OpenAI, Anthropic, Ollama, ...
       cat >> ~/.hermes/.env <<'EOF'
       TELEGRAM_BOT_TOKEN=123456789:ABC...
       TELEGRAM_ALLOWED_USERS=<your numeric telegram id>
       EOF
       chmod 600 ~/.hermes/.env
       hermes doctor

  3) Register the hermx-control skill:
       mkdir -p ~/.hermes/skills
       ln -sfn "$PWD/skills/hermx-control" ~/.hermes/skills/hermx-control
       hermes skills list | grep hermx-control

  4) Start the Telegram gateway:
       hermes gateway setup     # select Telegram
       hermes gateway start

  Get Telegram values: @BotFather -> /newbot (token); @userinfobot -> numeric id.
HERMES
fi

echo
ok "Done. See INSTALL.md for any step that needs attention."
