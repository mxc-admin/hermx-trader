#!/usr/bin/env bash
# install-docker.sh — HermX repo-less Docker installer.
#   curl -fsSL https://raw.githubusercontent.com/mxc-admin/hermx-trader/main/scripts/install-docker.sh | bash
# Seeds /opt/hermx from the published image, configures .env, and starts compose.
# Safe by default: HERMX_LIVE_TRADING=false. Nothing reaches a live exchange.
set -euo pipefail

IMAGE="${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}"
INSTALL_DIR="${HERMX_INSTALL_DIR:-/opt/hermx}"
ENV_FILE="$INSTALL_DIR/.env"

if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"
  RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""; fi
phase(){ printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info(){ printf '  %s\n' "$1"; }
ok(){ printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn(){ printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err(){ printf '  %sx%s %s\n' "$RED" "$RESET" "$1"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ask(){ local p="$1" d="${2:-n}" r s; [[ "$d" == y ]] && s="[Y/n]" || s="[y/N]"; read -r -p "  $p $s " r||true; r="${r:-$d}"; [[ "$r" =~ ^[Yy] ]]; }

# --- set_env: copied verbatim from install.sh:50-61 (package script can't source the repo) ---
set_env(){ local key="$1"; shift; local val="$*"; local tmp; tmp="$(mktemp)";
  [[ -f "$ENV_FILE" ]] && { grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null||true; }
  printf '%s=%s\n' "$key" "$val" >> "$tmp"; mv "$tmp" "$ENV_FILE"; }

# --- pick_exchange: copied from install.sh:69-176, BUT no `cp config/... engine-config.json`
#     step (the image already baked a venue-agnostic engine-config.json; venue lives in .env). ---
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
pick_exchange(){
  info "Pick the exchange. Keys MUST be demo/sandbox/testnet — never a live account."; echo
  local i=1 row label
  for row in "${EXCHANGE_TABLE[@]}"; do label="$(echo "$row"|cut -d'|' -f2)"; printf '    %2d) %s\n' "$i" "$label"; i=$((i+1)); done; echo
  local choice; while true; do read -r -p "  Pick [1-${#EXCHANGE_TABLE[@]}] (default 1=OKX): " choice; choice="${choice:-1}"
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice>=1 && choice<=${#EXCHANGE_TABLE[@]} )) && break; warn "1-${#EXCHANGE_TABLE[@]}."; done
  row="${EXCHANGE_TABLE[$((choice-1))]}"
  local ex_id ex_label prefix fields
  ex_id="$(echo "$row"|cut -d'|' -f1)"; ex_label="$(echo "$row"|cut -d'|' -f2)"
  prefix="$(echo "$row"|cut -d'|' -f3)"; fields="$(echo "$row"|cut -d'|' -f4)"
  echo; ok "Selected: $ex_label (env prefix ${prefix}_*)"
  info "Enter the credentials from your ${ex_label} demo/sandbox/testnet account."; echo
  local field val; IFS=',' read -r -a field_arr <<< "$fields"
  for field in "${field_arr[@]}"; do case "$field" in
    apiKey) read -r -p "  API Key: " val; set_env "${prefix}_API_KEY" "$val";;
    secret) read -r -s -p "  Secret: " val; echo; set_env "${prefix}_SECRET_KEY" "$val";;
    secret_bare) read -r -s -p "  Secret: " val; echo; set_env "${prefix}_SECRET" "$val";;
    passphrase) read -r -s -p "  Passphrase: " val; echo; set_env "${prefix}_PASSPHRASE" "$val";;
    wallet_address) read -r -p "  Wallet Address: " val; set_env "${prefix}_WALLET_ADDRESS" "$val";;
    private_key) read -r -s -p "  Private Key: " val; echo; set_env "${prefix}_PRIVATE_KEY" "$val";;
    *) warn "Unknown field '$field' — skipping.";; esac; done
  set_env "HERMX_EXCHANGE" "$ex_id"; set_env "HERMX_CCXT_EXCHANGE" "$ex_id"
  set_env "HERMX_LIVE_TRADING" "false"
  [[ "$ex_id" == "okx" ]] && set_env "OKX_FORCE_IPV4" "1"
}

phase "PHASE 0: Prerequisites"
have docker || { err "Docker not installed. Run: curl -fsSL https://get.docker.com | sh"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose v2 required."; exit 1; }
ok "Docker + compose present."
sudo mkdir -p "$INSTALL_DIR"; sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR"
cd "$INSTALL_DIR"

phase "PHASE 1: Pull image"
docker pull "$IMAGE"; ok "Pulled $IMAGE"

phase "PHASE 2: Seed host files from the image"
# One throwaway container copies the baked assets to the host install dir. The
# bind-mounts in compose then point at THESE host copies (operator-editable).
SEED='set -e; cp -r /app/strategies/. /seed/strategies/ 2>/dev/null||true;
 cp /app/engine-config.json /seed/engine-config.json;
 mkdir -p /seed/config/tailscale; cp /app/config/tailscale/serve.json /seed/config/tailscale/serve.json;
 cp /app/docker-compose.yml /seed/docker-compose.yml; cp /app/docker-compose.host.yml /seed/docker-compose.host.yml;
 cp /app/setup/env.example /seed/env.example'
mkdir -p "$INSTALL_DIR/strategies" "$INSTALL_DIR/config/tailscale"
docker run --rm --entrypoint sh -v "$INSTALL_DIR:/seed" "$IMAGE" -c "$SEED"
ok "Seeded strategies/, engine-config.json, config/tailscale/serve.json, compose files."

phase "PHASE 3: Configure .env"
if [[ -f "$ENV_FILE" ]] && ! ask ".env exists — overwrite credentials?" "n"; then
  warn "Leaving existing .env untouched."
else
  [[ -f "$ENV_FILE" ]] || cp env.example "$ENV_FILE"
  pick_exchange
  if have openssl; then SECRET="$(openssl rand -hex 32)"; set_env "HERMX_SECRET" "$SECRET"
    ok "Generated HERMX_SECRET."; info "Secret (X-Webhook-Secret + dashboard token): $SECRET"
  else warn "openssl missing — set HERMX_SECRET in .env manually."; fi
fi

phase "PHASE 4: Tailscale auth key"
if ! grep -q '^TS_AUTHKEY=..*' "$ENV_FILE" 2>/dev/null; then
  info "Generate a reusable/ephemeral key: Tailscale admin -> Settings -> Keys."
  read -r -p "  TS_AUTHKEY (tskey-...) [blank to skip]: " k
  [[ -n "${k:-}" ]] && { set_env "TS_AUTHKEY" "$k"; ok "TS_AUTHKEY saved."; } || warn "Sidecar won't connect until TS_AUTHKEY is set."
fi
chmod 600 "$ENV_FILE"

phase "PHASE 5: Start"
docker compose up -d
sleep 5
curl -sf http://127.0.0.1:8891/health >/dev/null 2>&1 && ok "Receiver OK (127.0.0.1:8891)" || err "Receiver FAIL — docker compose logs receiver"
curl -sf http://127.0.0.1:8098/health >/dev/null 2>&1 && ok "Dashboard OK (127.0.0.1:8098)" || err "Dashboard FAIL — docker compose logs dashboard"
echo; ok "Installed to $INSTALL_DIR."
info "Webhook URL: https://hermx.<tailnet>.ts.net/webhook"
info "Edit strategies: $INSTALL_DIR/strategies/*.json then: (cd $INSTALL_DIR && docker compose restart)"
info "Update later:   (cd $INSTALL_DIR && docker compose pull && docker compose up -d)"
