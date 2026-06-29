#!/usr/bin/env bash
#
# run.sh — HermX local runner + smoke test
#
# Run from the repo root:
#   bash run.sh
#
# This script does one complete local cycle:
#   1. Validates the package (required files, JSON, Python syntax)
#   2. Runs the offline test suite (no live exchange calls)
#   3. Starts the webhook receiver + dashboard on loopback
#   4. Waits for /health on both services
#   5. Prints the TradingView webhook URL and dashboard links
#   6. Waits for Ctrl-C, then cleanly shuts down both services
#
# Usage:
#   bash run.sh                # validate + test + start services (foreground)
#   bash run.sh --skip-tests   # validate + start services, skip pytest
#   bash run.sh --check        # validate + test only, do not start services
#   bash run.sh --honor-submit # do not force HERMX_LIVE_TRADING=false
#   bash run.sh --new-secret   # regenerate HERMX_SECRET (webhook + dashboard auth)
#
# SAFETY: order submission is hard-blocked (HERMX_LIVE_TRADING=false) unless
# you pass --honor-submit. This is a development / smoke runner, not a live
# trading launcher.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Locate repo root
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
cd "$ROOT"

# ---------------------------------------------------------------------------
# Colours + logging
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"
  BLUE="$(printf '\033[34m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

phase()  { printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info()   { printf '  %s\n' "$1"; }
ok()     { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()   { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()    { printf '  %sx%s %s\n' "$RED" "$RESET" "$1"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# Print one-line Hermes gateway status if hermes is installed. The gateway is a
# separate launchd/systemd service; we only report it, never start/stop it.
print_hermes_status() {
  if ! have hermes; then
    info "Hermes gateway: not installed (skip with --no-hermes)"
    return
  fi
  local gateway_status
  gateway_status=$(hermes gateway status 2>&1) || true
  if [[ -z "$gateway_status" ]]; then
    warn "Hermes gateway: status unknown"
    return
  fi
  if echo "$gateway_status" | grep -q "Gateway is.*running"; then
    ok "Hermes gateway: $(echo "$gateway_status" | grep -E "Gateway is.*running" | head -1)"
  elif echo "$gateway_status" | grep -q "not running"; then
    warn "Hermes gateway: not running (run: hermes gateway start)"
  else
    info "Hermes gateway: status available"
  fi
}

# Resolve a Python interpreter: prefer repo venv, then python3.11, then python3
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
  ok "Using repo venv: $PYTHON ($("$PYTHON" --version 2>&1))"
elif have python3.11; then
  PYTHON="$(command -v python3.11)"
  ok "Using system python3.11: $PYTHON ($("$PYTHON" --version 2>&1))"
elif have python3; then
  PYTHON="$(command -v python3)"
  ok "Using system python3: $PYTHON ($("$PYTHON" --version 2>&1))"
else
  err "No Python interpreter found (.venv/bin/python, python3.11, or python3)."
  exit 1
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SKIP_TESTS=false
HONOR_SUBMIT=false
CHECK_ONLY=false
NEW_SECRET=false
SKIP_UI_BUILD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests)    SKIP_TESTS=true; shift ;;
    --honor-submit)  HONOR_SUBMIT=true; shift ;;
    --check)         CHECK_ONLY=true; shift ;;
    --new-secret)    NEW_SECRET=true; shift ;;
    --skip-ui-build) SKIP_UI_BUILD=true; shift ;;
    -h|--help)
      sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      err "Unknown argument: $1"
      sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Load .env if present
# ---------------------------------------------------------------------------
load_env() {
  if [[ -f "$ROOT/.env" ]]; then
    # .env permissions check (security runbook)
    local mode
    mode="$(stat -c '%a' "$ROOT/.env" 2>/dev/null || stat -f '%Lp' "$ROOT/.env" 2>/dev/null || echo "unknown")"
    if [[ "$mode" != "600" && "$mode" != "unknown" ]]; then
      warn ".env permissions are $mode (recommended: 600) — run: chmod 600 .env"
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ "$line" =~ = ]] || continue
      key="${line%%=*}"
      key="$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [[ -z "$key" ]] && continue
      val="${line#*=}"
      # .env provides defaults; existing env vars take precedence
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    done < "$ROOT/.env"
    ok "Loaded .env"
  else
    warn "No .env found — services may fail without credentials"
  fi
}

load_env

# ---------------------------------------------------------------------------
# Unified secret (HERMX_SECRET): authenticates both the webhook and the dashboard
# ---------------------------------------------------------------------------
# Idempotently upsert KEY=VALUE in $ROOT/.env
set_env() {
  local key="$1"; shift
  local val="$*"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$ROOT/.env" ]]; then
    grep -v "^${key}=" "$ROOT/.env" > "$tmp" 2>/dev/null || true
  fi
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ROOT/.env"
  chmod 600 "$ROOT/.env" 2>/dev/null || true
}

# Generate a random secret using openssl, falling back to Python's secrets module.
gen_secret() {
  if have openssl; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_hex(32))' 2>/dev/null \
      || python -c 'import secrets, sys; sys.stdout.write(secrets.token_hex(32))'
  fi
}

# HERMX_SECRET is generated once if absent. Pass --new-secret to force a fresh one.
# No automatic rotation: the secret persists until you regenerate it on demand.
ensure_secret() {
  local secret
  if [[ "$NEW_SECRET" == true ]]; then
    secret="$(gen_secret)"
    set_env "HERMX_SECRET" "$secret"
    export HERMX_SECRET="$secret"
    ok "Regenerated HERMX_SECRET (--new-secret)"
  elif [[ -z "${HERMX_SECRET:-}" ]]; then
    secret="$(gen_secret)"
    set_env "HERMX_SECRET" "$secret"
    export HERMX_SECRET="$secret"
    ok "Generated HERMX_SECRET"
  else
    secret="$HERMX_SECRET"
    export HERMX_SECRET
  fi
  # Persist a readable copy so the secret is retrievable even when the script is
  # backgrounded and its stdout is lost.
  printf '%s\n' "$secret" > "$ROOT/HERMX_SECRET.txt"
  chmod 600 "$ROOT/HERMX_SECRET.txt" 2>/dev/null || true
  ok "HERMX_SECRET written to HERMX_SECRET.txt (mode 600)"
  info "Secret: $secret"
}

ensure_secret

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
: "${SHADOW_PORT:=8891}"
: "${CLEAN_DASHBOARD_PORT:=8098}"
export SHADOW_ROOT="$ROOT"
export SHADOW_PORT
export CLEAN_DASHBOARD_PORT
SUMMARY_SHOWN=false

if [[ "$HONOR_SUBMIT" != true ]]; then
  export HERMX_LIVE_TRADING="false"
fi

# ---------------------------------------------------------------------------
# Ensure shadow-config.json exists
# ---------------------------------------------------------------------------
if [[ ! -f "$ROOT/shadow-config.json" ]]; then
  if [[ -f "$ROOT/config/runtime.demo.json" ]]; then
    cp "$ROOT/config/runtime.demo.json" "$ROOT/shadow-config.json"
    ok "Created shadow-config.json from config/runtime.demo.json"
  else
    err "config/runtime.demo.json missing — cannot seed shadow-config.json"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Phase 1: package validation
# ---------------------------------------------------------------------------
phase "1/4 — Validate package"

if ! "$PYTHON" "$ROOT/scripts/validate_package.py"; then
  err "Package validation failed"
  exit 1
fi
ok "Package validation passed"

# ---------------------------------------------------------------------------
# Phase 2: offline tests
# ---------------------------------------------------------------------------
if [[ "$SKIP_TESTS" != true ]]; then
  phase "2/4 — Run offline tests"

  if ! "$PYTHON" -m pytest --version >/dev/null 2>&1; then
    warn "pytest not available in $PYTHON — skipping test run"
    warn "Install with: $PYTHON -m pip install -r requirements.txt"
  else
    "$PYTHON" -m pytest -q \
      -m "not integration and not okx_paper and not kucoin_paper and not hyperliquid_paper" \
      || { err "Offline tests failed"; exit 1; }
    ok "Offline tests passed"
  fi
else
  phase "2/4 — Tests skipped (--skip-tests)"
fi

if [[ "$CHECK_ONLY" == true ]]; then
  phase "Check complete"
  info "Validation/tests passed. Services were not started."
  exit 0
fi

# ---------------------------------------------------------------------------
# Build React dashboard UI (dashboard-ui/)
# ---------------------------------------------------------------------------
if [[ "$SKIP_UI_BUILD" != true ]] && [[ -d "$ROOT/dashboard-ui" ]]; then
  phase "2b/4 — Build React dashboard UI"
  UI_DIR="$ROOT/dashboard-ui"
  # npm install is idempotent: fast if node_modules is current, only works when package.json changed.
  if have npm; then
    (cd "$UI_DIR" && npm install --prefer-offline --silent) \
      && ok "npm install done" \
      || { warn "npm install failed — skipping UI build"; SKIP_UI_BUILD=true; }
    if [[ "$SKIP_UI_BUILD" != true ]]; then
      (cd "$UI_DIR" && NEXT_PUBLIC_API_BASE="" npm run build) \
        && ok "React dashboard built → dashboard-ui/out/" \
        || { warn "React dashboard build failed — will use legacy Python HTML dashboard"; }
    fi
  else
    warn "npm not found — skipping UI build (install Node.js to enable the React dashboard)"
  fi
fi

# ---------------------------------------------------------------------------
# Discover / publish public URLs via Tailscale Funnel
# ---------------------------------------------------------------------------
# The webhook is published on :443; the dashboard gets its OWN Funnel on :8443.
# Funnel only permits 443/8443/10000 as public ports, so the dashboard (loopback
# $CLEAN_DASHBOARD_PORT) cannot share the webhook's :443 — it gets :8443.
discover_urls() {
  WEBHOOK_URL=""
  DASHBOARD_URL=""
  if [[ -f "$ROOT/WEBHOOK_URL.txt" ]]; then
    WEBHOOK_URL="$(cat "$ROOT/WEBHOOK_URL.txt")"
    ok "Loaded public webhook URL from WEBHOOK_URL.txt"
  fi
  if [[ -f "$ROOT/DASHBOARD_URL.txt" ]]; then
    DASHBOARD_URL="$(cat "$ROOT/DASHBOARD_URL.txt")"
    ok "Loaded public dashboard URL from DASHBOARD_URL.txt"
  fi

  if have tailscale && tailscale status >/dev/null 2>&1; then
    # Publish the dashboard on its own Funnel (:8443 -> loopback dashboard).
    if [[ -z "$DASHBOARD_URL" ]]; then
      info "Enabling Tailscale Funnel for the dashboard (:8443 -> ${CLEAN_DASHBOARD_PORT})..."
      if tailscale funnel --bg --https=8443 "$CLEAN_DASHBOARD_PORT" >/dev/null 2>&1 \
         || sudo tailscale funnel --bg --https=8443 "$CLEAN_DASHBOARD_PORT" >/dev/null 2>&1; then
        ok "Dashboard Funnel enabled (:8443)"
      else
        warn "Could not enable the dashboard Funnel (enable Funnel for your tailnet first, then re-run)."
      fi
    fi

    # Derive the tailnet hostname from any active funnel entry.
    TS_HOST="$(tailscale funnel status 2>/dev/null | grep -oE 'https://[a-zA-Z0-9._-]+\.ts\.net' | head -1 || true)"
    if [[ -n "$TS_HOST" ]]; then
      if [[ -z "$WEBHOOK_URL" ]]; then
        WEBHOOK_URL="${TS_HOST}/webhook"
        ok "Discovered Tailscale Funnel URL: $WEBHOOK_URL"
      fi
      if [[ -z "$DASHBOARD_URL" ]]; then
        DASHBOARD_URL="${TS_HOST}:8443/dashboard/"
        ok "Dashboard Tailscale URL: $DASHBOARD_URL"
      fi
    fi
  fi
}

# ---------------------------------------------------------------------------
# Phase 4: summary
# ---------------------------------------------------------------------------
print_summary() {
  [[ "$SUMMARY_SHOWN" == true ]] && return
phase "4/4 — HermX is running"

cat <<SUMMARY
${BOLD}Local services:${RESET}
  Receiver health:  http://127.0.0.1:${SHADOW_PORT}/health
  Dashboard health: http://127.0.0.1:${CLEAN_DASHBOARD_PORT}/health
  Dashboard UI:     http://127.0.0.1:${CLEAN_DASHBOARD_PORT}/dashboard/

${BOLD}Dashboard auth:${RESET}
  Secret: ${HERMX_SECRET:-<not set>}
  Pass as X-Dashboard-Token header, or as Bearer/Basic password.
  (Same HERMX_SECRET is the webhook X-Webhook-Secret. Run --new-secret to rotate.)

${BOLD}Dashboard public URL:${RESET}
SUMMARY

if [[ -n "$DASHBOARD_URL" ]]; then
  echo "  $DASHBOARD_URL"
  echo "  Requires the dashboard auth token above (X-Dashboard-Token header,"
  echo "  or as the Bearer/Basic password)."
else
  echo "  (local only) http://127.0.0.1:${CLEAN_DASHBOARD_PORT}/dashboard/"
  echo "  For a public HTTPS URL (its own Funnel, separate from the webhook):"
  echo "    1. Install Tailscale:    https://tailscale.com/download"
  echo "    2. Connect:              sudo tailscale up --hostname=hermx"
  echo "    3. Funnel the dashboard: sudo tailscale funnel --bg --https=8443 ${CLEAN_DASHBOARD_PORT}"
  echo "    4. Open:                 https://hermx.<tailnet>.ts.net:8443/dashboard/"
  echo "    (The public dashboard URL still requires the auth token above.)"
fi

cat <<SUMMARY

${BOLD}TradingView webhook URL:${RESET}
SUMMARY

if [[ -n "$WEBHOOK_URL" ]]; then
  echo "  $WEBHOOK_URL"
  echo "  Header: X-Webhook-Secret: ${HERMX_SECRET:-<not set>}"
else
  echo "  (local only) http://127.0.0.1:${SHADOW_PORT}/webhook"
  echo "  For a public HTTPS URL:"
  echo "    1. Install Tailscale:   https://tailscale.com/download"
  echo "    2. Connect:             sudo tailscale up --hostname=hermx"
  echo "    3. Enable funnel:       sudo tailscale funnel --bg ${SHADOW_PORT}"
  echo "    4. Save the URL:         echo https://hermx.<tailnet>.ts.net/webhook > WEBHOOK_URL.txt"
fi

cat <<SUMMARY

${BOLD}Test alert (run in another terminal):${RESET}
  curl -s -X POST http://127.0.0.1:${SHADOW_PORT}/webhook \\
    -H "Content-Type: application/json" \\
    -H "X-Webhook-Secret: ${HERMX_SECRET:-<secret>}" \\
    -d '{"strategy_id":"btcusdt_duo_base_dev_2h","symbol":"BTCUSDT","timeframe":"2h","side":"buy","tv_signal_price":"65000","tv_time":"2026-06-28T00:00:00Z","exchange":"okx","source":"tradingview"}'

${BOLD}Submit gate:${RESET}
  HERMX_LIVE_TRADING=${HERMX_LIVE_TRADING:-<unset>}
SUMMARY

if [[ "$HONOR_SUBMIT" != true ]]; then
  cat <<SUMMARY
  ${YELLOW}Live submission is BLOCKED (HERMX_LIVE_TRADING=false); demo/paper orders${RESET}
  ${YELLOW}may still submit if a strategy has submit_orders=true.${RESET}
  Pass --honor-submit to let .env / shadow-config.json control submission.
SUMMARY
fi

echo
info "Press Ctrl-C to stop both services."
  SUMMARY_SHOWN=true
}

# ---------------------------------------------------------------------------
# Phase 3: start services
# ---------------------------------------------------------------------------
phase "3/4 — Start services"

if ! have curl; then
  err "curl is required for health probes. Install it and re-run."
  exit 1
fi

# Always surface the secret + known URLs before the port check, even if
# startup later fails. print_summary is idempotent (SUMMARY_SHOWN guard).
discover_urls
print_summary

# Port availability check (best-effort)
if have lsof; then
  for port in "$SHADOW_PORT" "$CLEAN_DASHBOARD_PORT"; do
    if lsof -i ":$port" >/dev/null 2>&1; then
      err "Port $port is already in use"
      exit 1
    fi
  done
  ok "Ports $SHADOW_PORT and $CLEAN_DASHBOARD_PORT are free"
else
  warn "lsof not available — skipping port-in-use check"
fi

mkdir -p "$ROOT/logs"
RECEIVER_PID=""
DASHBOARD_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  info "Shutting down services..."
  [[ -n "$RECEIVER_PID" ]] && { kill "$RECEIVER_PID" 2>/dev/null || true; }
  [[ -n "$DASHBOARD_PID" ]] && { kill "$DASHBOARD_PID" 2>/dev/null || true; }
  wait 2>/dev/null || true
  ok "Stopped. Logs written to logs/run.receiver.log and logs/run.dashboard.log"
}
trap cleanup INT TERM EXIT

info "Starting receiver (logs/run.receiver.log)"
"$PYTHON" "$ROOT/src/webhook_receiver.py" > "$ROOT/logs/run.receiver.log" 2>&1 &
RECEIVER_PID=$!

info "Starting dashboard (logs/run.dashboard.log)"
"$PYTHON" "$ROOT/src/dashboard.py" > "$ROOT/logs/run.dashboard.log" 2>&1 &
DASHBOARD_PID=$!

# ---------------------------------------------------------------------------
# Wait for health
# ---------------------------------------------------------------------------
wait_for_health() {
  local url="$1" name="$2" max_wait="${3:-30}"
  local waited=0
  while ! curl -sf "$url" >/dev/null 2>&1; do
    if (( waited >= max_wait )); then
      err "$name not healthy after ${max_wait}s ($url)"
      log_name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')
      err "Check logs/run.${log_name}.log for details"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  ok "$name healthy ($url)"
}

wait_for_health "http://127.0.0.1:$SHADOW_PORT/health" "Receiver" 30
wait_for_health "http://127.0.0.1:$CLEAN_DASHBOARD_PORT/health" "Dashboard" 30

# ---------------------------------------------------------------------------
# Synthetic webhook test (optional)
# ---------------------------------------------------------------------------
WEBHOOK_SECRET="${HERMX_SECRET:-}"
if [[ -n "$WEBHOOK_SECRET" ]]; then
  info "Sending synthetic test alert..."
  response=$(curl -s -w "\n%{http_code}" -X POST \
    "http://127.0.0.1:$SHADOW_PORT/webhook" \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
    -d '{"strategy_id":"btcusdt_duo_base_dev_2h","symbol":"BTCUSDT","timeframe":"2h","side":"buy","tv_signal_price":"65000","tv_time":"2026-06-28T00:00:00Z","exchange":"okx","source":"tradingview"}' 2>/dev/null || true)
  http_code=$(echo "$response" | tail -1)
  if [[ "$http_code" =~ ^(200|202|204)$ ]]; then
    ok "Synthetic webhook accepted (HTTP $http_code)"
  else
    warn "Synthetic webhook returned HTTP ${http_code:-unknown}"
  fi
else
  warn "No HERMX_SECRET set — skipping synthetic webhook test"
fi

print_summary
info "HERMX_SECRET: ${HERMX_SECRET:-<not set>}"
info "(also written to HERMX_SECRET.txt)"
print_hermes_status
wait
