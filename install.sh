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
SELECTED_EX=""      # exchange id chosen in pick_exchange (or read from .env)
PROBE_OK="false"    # demo credential probe result (gate for strategy sizing)
PROBE_EQUITY=""     # usable equity parsed from the probe output

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
# Demo-capable venues only: coinbase and bitfinex have no ccxt sandbox, so a
# demo install cannot use them (they fail closed at the adapter).
EXCHANGE_TABLE=(
  "okx|OKX (recommended)|OKX_DEMO|apiKey,secret,passphrase"
  "binance|Binance|BINANCE_TESTNET|apiKey,secret"
  "bybit|Bybit|BYBIT_TESTNET|apiKey,secret"
  "kucoin|KuCoin|KUCOIN_PAPER|apiKey,secret_bare,passphrase"
  "bitget|Bitget|BITGET_DEMO|apiKey,secret,passphrase"
  "gate|Gate.io|GATE_TESTNET|apiKey,secret"
  "hyperliquid|Hyperliquid (testnet)|HYPERLIQUID_TESTNET|wallet_address,private_key"
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
  set_env "HERMX_CCXT_EXCHANGE" "$ex_id"
  SELECTED_EX="$ex_id"

  # Global live-trading kill switch -- written false for every venue, so a fresh
  # install is always demo/sandbox until the operator deliberately goes live.
  set_env "HERMX_LIVE_TRADING" "false"

  # Select the matching DISARMED runtime profile -> engine-config.json. OKX is the
  # reference venue and ships as the generic config/runtime.demo.json (no okx-suffixed file).
  local cfg="config/runtime.${ex_id}.demo.json"
  [[ "$ex_id" == "okx" ]] && cfg="config/runtime.demo.json"
  if [[ -f "$cfg" ]]; then
    cp "$cfg" engine-config.json
    ok "Copied $cfg -> engine-config.json"
  elif [[ -f "config/runtime.demo.json" ]]; then
    cp "config/runtime.demo.json" engine-config.json
    warn "$cfg not found — fell back to config/runtime.demo.json -> engine-config.json"
  else
    err "No runtime config found to copy to engine-config.json."
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
    sudo apt-get install -y python3.11 python3.11-venv python3-pip curl git || true
    if ! have python3.11; then
      warn "python3.11 not in the default repos — adding the deadsnakes PPA (INSTALL.md Phase 1)..."
      sudo apt-get install -y software-properties-common
      sudo add-apt-repository -y ppa:deadsnakes/ppa
      sudo apt-get update
      sudo apt-get install -y python3.11 python3.11-venv
    fi
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

  # Generate a single strong secret used for BOTH webhook and dashboard auth.
  if have openssl; then
    SECRET="$(openssl rand -hex 32)"
    set_env "HERMX_SECRET" "$SECRET"
    ok "Generated HERMX_SECRET (saved to .env)."
    info "Secret: $SECRET"
    info "(Put this exact value in the TradingView alert MESSAGE JSON as \"secret_key\" —"
    info " the webhook URL box takes the URL only, never the secret. It is also the"
    info " dashboard token / Bearer / Basic password.)"
  else
    warn "openssl not found — set HERMX_SECRET in .env manually."
  fi

  chmod 600 "$ENV_FILE"
  ok ".env written, HERMX_LIVE_TRADING=false (safe by default)"
else
  ok "Skipped .env configuration."
fi

# ===========================================================================
# PHASE 2.5 — Python environment + demo credential probe (install gate)
# ===========================================================================
phase "PHASE 2.5: Python Environment + Demo Probe"

build_venv() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]] && "$REPO_ROOT/.venv/bin/python" -c 'import ccxt' >/dev/null 2>&1; then
    ok "Virtualenv already present (.venv, ccxt importable)."
    return 0
  fi
  info "Creating .venv and installing requirements..."
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
  ok "Virtualenv ready (.venv)."
}

# The probe needs .venv + ccxt, so the venv is built BEFORE strategy sizing.
build_venv

# Resolve the exchange even when Phase 2 was skipped (.env left untouched).
if [[ -z "$SELECTED_EX" ]]; then
  SELECTED_EX="$(grep -E '^HERMX_CCXT_EXCHANGE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2)"
  SELECTED_EX="${SELECTED_EX:-okx}"
  info "Exchange from .env: $SELECTED_EX"
fi

info "Probing $SELECTED_EX demo credentials (read-only fetch_balance — gate before sizing)..."
while true; do
  probe_rc=0
  probe_out="$(bash scripts/exchange.sh probe "$SELECTED_EX" --demo 2>&1)" || probe_rc=$?
  printf '%s\n' "$probe_out"
  if [[ $probe_rc -eq 0 ]]; then
    PROBE_OK="true"
    PROBE_EQUITY="$(printf '%s\n' "$probe_out" | sed -n 's/.*usable equity: \([0-9][0-9.]*\).*/\1/p' | head -1)"
    ok "Demo credential probe PASSED."
    break
  fi
  err "Demo credential probe FAILED (exit $probe_rc) — auth or credentials are wrong."
  info "Fix in another terminal (edit .env, or: bash scripts/exchange.sh update $SELECTED_EX --demo), then retry."
  read -r -p "  [r]etry probe / [o]verride and continue anyway / [a]bort install? [r/o/a] " probe_choice || probe_choice="a"
  case "$probe_choice" in
    o|O) warn "Continuing WITHOUT a passing probe — strategy sizing is unverified against the venue."; break ;;
    a|A) err "Aborting install — demo credentials must authenticate before sizing strategies."; exit 1 ;;
    *) ;;
  esac
done

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

# write_strategy_risk FILE BUDGET LEV — write capital.budget_usd + leverage into
# the strategy JSON, preserving every other key (indent=2).
write_strategy_risk() {
  local file="$1" budget="$2" lev="$3"
  $PY - "$file" "$budget" "$lev" <<'PYEOF'
import json, sys
path, budget, lev = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)
def num(s):
    v = float(s)
    return int(v) if v.is_integer() else v
cap = data.get("capital")
if not isinstance(cap, dict):
    cap = {}
    data["capital"] = cap
cap["budget_usd"] = num(budget)
data["leverage"] = num(lev)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
import os
os.replace(tmp, path)
PYEOF
}

# set_reduce_only SID — mark a declined strategy reduce-only (risk_state: "reduce")
# in control-state.json, the EXISTING split-control mechanism (ExecutionService gate
# 5b blocks opens; close_only always passes). Prefers the production helper
# (src/control_state.py set_strategy_risk); falls back to a JSON merge that keeps
# every default_control_state() key so the load-time merge filter drops nothing.
set_reduce_only() {
  local sid="$1" py_bin="$PY"
  [[ -x "$REPO_ROOT/.venv/bin/python" ]] && py_bin="$REPO_ROOT/.venv/bin/python"
  "$py_bin" - "$REPO_ROOT" "$ENV_FILE" "$sid" <<'PYEOF'
import json, os, sys, datetime
repo, env_file, sid = sys.argv[1], sys.argv[2], sys.argv[3]
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
for key in ("HERMX_ROOT", "HERMX_DATA_DIR"):
    if env.get(key) and not os.environ.get(key):
        os.environ[key] = env[key]
try:
    sys.path.insert(0, os.path.join(repo, "src"))
    from control_state import set_strategy_risk
    if set_strategy_risk(sid, "reduce"):
        import webhook_receiver as _wr
        print(str(_wr.CONTROL_STATE_FILE))
        sys.exit(0)
except Exception:
    pass
# Fallback: direct merge, same path resolution and default shape as the app
# (webhook_receiver: DATA_DIR = HERMX_DATA_DIR or HERMX_ROOT or repo root).
root = os.environ.get("HERMX_ROOT") or repo
data_dir = os.environ.get("HERMX_DATA_DIR") or root
path = os.path.join(data_dir, "control-state.json")
now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
default = {
    "version": 1,
    "updated_at": now,
    "mode": "shadow_only",
    "live_trading": "paused",
    "manual_pause": False,
    "pause_reason": "",
    "symbol_pauses": {},
    "strategy_overrides": {},
    "accounting_windows": {},
    "trading_state": "active",
    "notes": "Shadow control file. Dashboard/Hermes may read this. Live execution remains disabled here.",
}
state = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            state = loaded
    except Exception:
        state = {}
for k, v in default.items():
    state.setdefault(k, v)
overrides = state.get("strategy_overrides")
if not isinstance(overrides, dict):
    overrides = {}
state["strategy_overrides"] = overrides
entry = dict(overrides.get(sid)) if isinstance(overrides.get(sid), dict) else {}
entry["risk_state"] = "reduce"
entry["mode"] = "pause"
entry["set_at"] = now
overrides[sid] = entry
state["updated_at"] = now
os.makedirs(data_dir, exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh, indent=2, ensure_ascii=False)
os.replace(tmp, path)
print(path)
PYEOF
}

shopt -s nullglob
strategy_files=(strategies/*.json)
shopt -u nullglob

TOTAL_BUDGET=0
TOTAL_NOTIONAL=0
ENABLED_INSTS=""

if (( ${#strategy_files[@]} == 0 )); then
  warn "No strategy files found under strategies/ — skipping."
else
  if [[ "$PROBE_OK" != "true" ]]; then
    warn "Sizing strategies WITHOUT a passing demo probe (operator override)."
  fi
  for f in "${strategy_files[@]}"; do
    # schema_version 2 strategy files: instrument + budget live in nested blocks,
    # and there is no asset/status field. Derive the display asset from the id.
    sid="$(json_get "$f" strategy_id)"
    inst="$(json_get "$f" instrument.inst_id)"
    budget="$(json_get "$f" capital.budget_usd)"
    lev="$(json_get "$f" leverage)"
    mode="$(json_get "$f" execution_mode)"
    asset="$(printf '%s' "${sid%%_*}" | tr '[:lower:]' '[:upper:]')"
    echo
    info "Strategy: ${BOLD}${sid}${RESET}"
    info "  asset=$asset  inst=$inst  budget_usd=$budget  leverage=${lev}x  execution_mode=$mode"
    if ask "Enable this strategy?" "y"; then
      # The operator MUST confirm the numbers: Enter accepts the shown current
      # value, anything else replaces it. Both are always written back to the JSON.
      new_budget=""
      while true; do
        read -r -p "  Budget USD for $sid [Enter = keep $budget]: " new_budget || new_budget=""
        new_budget="${new_budget:-$budget}"
        [[ "$new_budget" =~ ^[0-9]+(\.[0-9]+)?$ ]] && break
        warn "Enter a positive number (e.g. 1500)."
      done
      new_lev=""
      while true; do
        read -r -p "  Leverage for $sid [Enter = keep ${lev}x]: " new_lev || new_lev=""
        new_lev="${new_lev:-$lev}"
        [[ "$new_lev" =~ ^[0-9]+(\.[0-9]+)?$ ]] && break
        warn "Enter a positive number (e.g. 2)."
      done
      if write_strategy_risk "$f" "$new_budget" "$new_lev"; then
        # Re-validate by reading the values back out of the file.
        rb="$(json_get "$f" capital.budget_usd)"; rl="$(json_get "$f" leverage)"
        ok "Wrote $f: budget_usd=$rb leverage=${rl}x (execution_mode stays demo)"
      else
        err "Failed to write risk params to $f — fix the JSON and re-run."
        exit 1
      fi
      TOTAL_BUDGET="$(awk "BEGIN{print $TOTAL_BUDGET + $new_budget}")"
      TOTAL_NOTIONAL="$(awk "BEGIN{print $TOTAL_NOTIONAL + $new_budget * $new_lev}")"
      ENABLED_INSTS="${ENABLED_INSTS:+$ENABLED_INSTS,}$inst"
      printf '%s %s %s\n' "$sid" "$asset" "$(json_get "$f" timeframe)" >> "$ENABLED_FILE"
      enabled_count=$((enabled_count + 1))
      ok "Enabled $sid"
    else
      # Declined = reduce-only via the EXISTING control-state model: opens are
      # blocked at the ExecutionService risk gate, closes always pass. The
      # strategy file stays in place.
      if cs_path="$(set_reduce_only "$sid")"; then
        ok "$sid set reduce-only (risk_state: \"reduce\" in ${cs_path:-control-state.json})"
        info "  Opens/reversals are blocked; closes still pass. Re-enable later from the dashboard mode pill."
      else
        err "Could not write reduce-only override for $sid — set it from the dashboard after install."
      fi
    fi
  done
fi

echo
ok "Enabled $enabled_count strategies. Each needs one BUY + one SELL alert in TradingView."
info "Enabled IDs saved to ENABLED_STRATEGIES.txt"
if (( enabled_count > 0 )); then
  info "Total allocated:  Σ budget = \$$TOTAL_BUDGET   Σ budget×leverage = \$$TOTAL_NOTIONAL"
  if [[ -n "$PROBE_EQUITY" ]]; then
    info "Probed demo equity: \$$PROBE_EQUITY"
    if awk "BEGIN{exit !($TOTAL_BUDGET > $PROBE_EQUITY)}"; then
      warn "Σ budget exceeds the probed demo equity — downsize budgets or top up the demo account."
    elif awk "BEGIN{exit !($TOTAL_NOTIONAL > $PROBE_EQUITY)}"; then
      warn "Σ budget×leverage exceeds the probed demo equity — margin may run out if all strategies open at once."
    fi
  fi
  if [[ "$PROBE_OK" == "true" && -n "$ENABLED_INSTS" ]]; then
    info "Checking enabled instruments exist on $SELECTED_EX demo (warn only)..."
    bash scripts/exchange.sh probe "$SELECTED_EX" --demo --markets "$ENABLED_INSTS" || warn "Market check probe failed (non-fatal)."
  fi
fi

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
        DASHBOARD_URL="${DASH_HOST}:8443/dashboard/"
        echo "$DASHBOARD_URL" > "$REPO_ROOT/DASHBOARD_URL.txt"
        ok "Dashboard URL: $DASHBOARD_URL"
        info "(Saved to DASHBOARD_URL.txt — this public URL still requires HERMX_SECRET from .env.)"
      else
        warn "Could not parse a dashboard Funnel URL. Run 'tailscale funnel status' and use"
        warn "https://hermx.<tailnet>.ts.net:8443/dashboard/ (needs HERMX_SECRET)."
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

# Build the Next.js dashboard UI if node/npm are available; otherwise the
# dashboard silently falls back to the legacy server-rendered HTML (dashboard.py
# gates on dashboard-ui/out being a directory).
build_dashboard_ui() {
  [[ -d "$REPO_ROOT/dashboard-ui" ]] || return 0
  if have npm; then
    info "Building dashboard UI (npm)..."
    if (cd "$REPO_ROOT/dashboard-ui" && { npm ci || npm install; } && npm run build); then
      ok "Dashboard UI built (dashboard-ui/out)."
    else
      warn "Dashboard UI build failed — the dashboard will serve the legacy HTML fallback."
      warn "Fix later with: cd dashboard-ui && npm ci && npm run build"
    fi
  else
    warn "npm not found — the dashboard will serve the legacy HTML fallback."
    warn "Install node+npm, then: cd dashboard-ui && npm ci && npm run build"
  fi
}
build_dashboard_ui

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
      if [[ ! -f "$REPO_ROOT/engine-config.json" ]]; then
        err "engine-config.json is missing — it is bind-mounted into both containers."
        err "Re-run Phase 3 (exchange picker) or copy a config/runtime.*.demo.json profile."
        preflight_ok="false"
      else
        ok "Pre-flight: engine-config.json present."
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
[[ -f "$REPO_ROOT/DASHBOARD_URL.txt" ]] && DASH_DISPLAY="$(cat "$REPO_ROOT/DASHBOARD_URL.txt")  (needs HERMX_SECRET)"
info "Webhook URL:  $WEBHOOK_DISPLAY"
info "Dashboard:    $DASH_DISPLAY"
info "Receiver:     http://127.0.0.1:8891"
info "Enabled:      $enabled_count strategies (ENABLED_STRATEGIES.txt)"
info "Submit gate:  HERMX_LIVE_TRADING=false  (demo — nothing sent to live exchange)"

echo
phase "Post-install glossary (what you will see — none of these are broken)"
info "Kill switch engaged   — EXPECTED on a demo install: HERMX_LIVE_TRADING=false only blocks live venues."
info "DATA STALE            — a freshness label, not a dead system: a quiet market produces no new signals."
info "Test webhooks         — open REAL positions on the demo/sandbox account; flatten with an action=\"close\" alert or the dashboard."
info "SELL can open a short — a sell signal with no open long OPENS a short; use action \"close\" to flatten, not a counter-order."
info "Going live            — needs BOTH execution_mode: \"live\" in the strategy JSON AND HERMX_LIVE_TRADING=true in .env."
info "                        This installer never sets either — demo only."
echo
info "Next: create the TradingView alerts (INSTALL.md Phase 7) and fire a test alert."

# ===========================================================================
# Optional — Hermes Agent + Telegram gateway
# ===========================================================================
echo
if ask "Set up Hermes Agent + Telegram (natural-language operator bot)?" "n"; then
  phase "OPTIONAL: Hermes Agent + Telegram gateway"

  # 1. Ensure hermes binary is installed.
  if ! have hermes; then
    if ask "Hermes is not installed. Install it now (curl installer)?" "y"; then
      info "Installing Hermes Agent..."
      curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
      if [[ -f ~/.zshrc ]]; then source ~/.zshrc; fi
      if [[ -f ~/.bashrc ]]; then source ~/.bashrc; fi
    fi
  fi
  if ! have hermes; then
    err "Hermes binary still not on PATH after install. Add ~/.local/bin to PATH and re-run."
  else
    ok "Hermes installed: $(hermes --version 2>&1 | head -1)"
  fi

  # 2. Provider key (one-time; safe default is manual).
  if ask "Configure a model provider (xAI/OpenAI/Anthropic) for Hermes?" "y"; then
    hermes setup || warn "Provider setup failed or was skipped."
  fi

  # 3. Telegram credentials.
  mkdir -p ~/.hermes
  HERMES_ENV=~/.hermes/.env
  if [[ ! -f "$HERMES_ENV" ]] || ! grep -q "TELEGRAM_BOT_TOKEN=" "$HERMES_ENV" 2>/dev/null; then
    info "Telegram gateway needs a bot token and your numeric user ID."
    info "  Token: @BotFather -> /newbot"
    info "  User ID: @userinfobot -> numeric Id"
    read -r -p "  TELEGRAM_BOT_TOKEN: " tg_token
    read -r -p "  TELEGRAM_ALLOWED_USERS: " tg_user
    if [[ -n "$tg_token" && -n "$tg_user" ]]; then
      touch "$HERMES_ENV"
      chmod 600 "$HERMES_ENV"
      grep -v "^TELEGRAM_BOT_TOKEN=" "$HERMES_ENV" > "$HERMES_ENV.tmp" 2>/dev/null || true
      grep -v "^TELEGRAM_ALLOWED_USERS=" "$HERMES_ENV.tmp" > "$HERMES_ENV" 2>/dev/null || true
      rm -f "$HERMES_ENV.tmp"
      printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOWED_USERS=%s\n' "$tg_token" "$tg_user" >> "$HERMES_ENV"
      ok "Wrote Telegram credentials to $HERMES_ENV (mode 600)"
    else
      warn "Empty token or user ID — skipping Telegram credential write."
    fi
  else
    ok "Telegram credentials already present in $HERMES_ENV"
  fi

  # 4. Register all Hermes skills. Every skill directory that ships a SKILL.md
  #    is symlinked into ~/.hermes/skills/. Directories without a SKILL.md
  #    (e.g. the hermx-ops shared library) are not standalone skills; skip them.
  if [[ -d "$REPO_ROOT/skills" ]]; then
    mkdir -p ~/.hermes/skills
    for skill_src in "$REPO_ROOT"/skills/*/; do
      skill_src="${skill_src%/}"
      [[ -f "$skill_src/SKILL.md" ]] || continue
      skill_name="$(basename "$skill_src")"
      ln -sfn "$skill_src" ~/.hermes/skills/"$skill_name"
      if hermes skills list 2>/dev/null | grep -q "$skill_name"; then
        ok "$skill_name skill registered"
      else
        warn "$skill_name skill not yet discovered by Hermes. Try: hermes skills update"
      fi
    done
  else
    warn "Skill directory not found: $REPO_ROOT/skills"
  fi

  # 4b. Register the skills-guard pre_tool_call hook in ~/.hermes/config.yaml.
  #     Hermes has no "hooks add" subcommand (`hermes hooks` only lists/tests/
  #     revokes) and `hermes config set` takes only scalar dotted keys, so we
  #     merge the entry into config.yaml directly. The merge preserves every
  #     other top-level key and the file mode, and is idempotent: the hook is
  #     identified by the script basename (skills-guard.py), so re-running the
  #     installer — even after REPO_ROOT moves — upserts the entry instead of
  #     appending a duplicate. PyYAML lives in Hermes' own venv, which the
  #     installer's generic $PY often lacks, so we prefer the bundled venv
  #     python when it exists AND can import yaml; otherwise we fall back to
  #     $PY. If neither yields yaml the merge snippet exits 3 and we print a
  #     manual snippet rather than hard-fail.
  guard_hook="$REPO_ROOT/setup/hermes/skills-guard.py"
  if [[ -x "$guard_hook" ]]; then
    # Interpreter preference: Hermes' bundled venv python (ships PyYAML) first,
    # then the installer's $PY. The snippet itself exits 3 if the chosen
    # interpreter can't import yaml, driving the manual-snippet fallback below.
    hook_py=""
    bundled_py="$HOME/.hermes/hermes-agent/venv/bin/python3"
    if [[ -x "$bundled_py" ]] && "$bundled_py" -c 'import yaml' >/dev/null 2>&1; then
      hook_py="$bundled_py"
    elif [[ -n "$PY" ]]; then
      hook_py="$PY"
    fi
    if [[ -z "$hook_py" ]]; then
      hook_rc=3; hook_status=""
    else
      hook_status="$("$hook_py" - "$HOME/.hermes/config.yaml" "$guard_hook" <<'PYEOF'
import sys, os
cfg_path, cmd = sys.argv[1], sys.argv[2]
try:
    import yaml
except Exception:
    sys.exit(3)   # PyYAML unavailable -> caller prints a manual snippet
data = {}
orig_mode = None
if os.path.exists(cfg_path):
    orig_mode = os.stat(cfg_path).st_mode & 0o777
    with open(cfg_path) as fh:
        loaded = yaml.safe_load(fh)
    if isinstance(loaded, dict):
        data = loaded
hooks = data.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    data["hooks"] = hooks
lst = hooks.get("pre_tool_call")
if not isinstance(lst, list):
    lst = []
    hooks["pre_tool_call"] = lst
entry = {"matcher": "write_file|patch|terminal", "command": cmd, "timeout": 5}
# Identity is the script basename, not the full path, so a moved REPO_ROOT
# upserts the existing entry rather than appending a duplicate one.
ident = os.path.basename(cmd)
status = "appended"
for i, item in enumerate(lst):
    if isinstance(item, dict) and isinstance(item.get("command"), str) \
            and os.path.basename(item["command"]) == ident:
        status = "unchanged" if item == entry else "updated"
        lst[i] = entry
        break
else:
    lst.append(entry)
if status != "unchanged":
    parent = os.path.dirname(cfg_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False,
                       allow_unicode=True)
    os.chmod(tmp, orig_mode if orig_mode is not None else 0o600)
    os.replace(tmp, cfg_path)
print(status)
PYEOF
)"
      hook_rc=$?
    fi
    if [[ $hook_rc -eq 3 ]]; then
      warn "python+PyYAML unavailable — skills-guard hook not auto-registered."
      info "Add this to ~/.hermes/config.yaml by hand, then restart Hermes:"
      info "  hooks:"
      info "    pre_tool_call:"
      info "      - matcher: \"write_file|patch|terminal\""
      info "        command: \"$guard_hook\""
      info "        timeout: 5"
    elif [[ $hook_rc -ne 0 ]]; then
      warn "Could not register skills-guard hook (config merge failed, rc=$hook_rc)."
    else
      case "$hook_status" in
        unchanged) ok "skills-guard hook already registered — no change" ;;
        updated)   ok "skills-guard hook path updated in ~/.hermes/config.yaml" ;;
        *)         ok "skills-guard hook registered in ~/.hermes/config.yaml" ;;
      esac
      info "First real use prompts Hermes for hook consent (set HERMES_ACCEPT_HOOKS=1 for headless)."
    fi
  else
    warn "skills-guard hook script missing or not executable: $guard_hook"
  fi

  # 5. Health check and start gateway.
  hermes doctor 2>/dev/null || warn "hermes doctor reported an issue."
  if ask "Start the Hermes messaging gateway now?" "y"; then
    hermes gateway setup || warn "gateway setup failed or was cancelled."
    hermes gateway start || warn "gateway start failed."
    if hermes gateway status 2>/dev/null | grep -q "running"; then
      ok "Hermes gateway is running"
    else
      warn "Hermes gateway may not be running. Check: hermes gateway status"
    fi
  fi
  info "Chat with the bot: @BotFather -> open your bot; send 'what's open?'"
fi

echo
ok "Done. See INSTALL.md for any step that needs attention."
