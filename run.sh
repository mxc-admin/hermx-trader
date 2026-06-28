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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests)   SKIP_TESTS=true; shift ;;
    --honor-submit) HONOR_SUBMIT=true; shift ;;
    --check)        CHECK_ONLY=true; shift ;;
    -h|--help)
      sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      err "Unknown argument: $1"
      sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
# Defaults
# ---------------------------------------------------------------------------
: "${SHADOW_PORT:=8891}"
: "${CLEAN_DASHBOARD_PORT:=8098}"
export SHADOW_ROOT="$ROOT"
export SHADOW_PORT
export CLEAN_DASHBOARD_PORT

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
# Phase 3: start services
# ---------------------------------------------------------------------------
phase "3/4 — Start services"

if ! have curl; then
  err "curl is required for health probes. Install it and re-run."
  exit 1
fi

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
if [[ -n "${SHADOW_WEBHOOK_SECRET:-}" ]]; then
  info "Sending synthetic test alert..."
  response=$(curl -s -w "\n%{http_code}" -X POST \
    "http://127.0.0.1:$SHADOW_PORT/webhook" \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Secret: $SHADOW_WEBHOOK_SECRET" \
    -d '{"strategy_id":"btcusdt_duo_base_dev_2h","symbol":"BTCUSDT","timeframe":"2h","side":"buy","tv_signal_price":"65000","tv_time":"2026-06-28T00:00:00Z","exchange":"okx","source":"tradingview"}' 2>/dev/null || true)
  http_code=$(echo "$response" | tail -1)
  if [[ "$http_code" =~ ^(200|202|204)$ ]]; then
    ok "Synthetic webhook accepted (HTTP $http_code)"
  else
    warn "Synthetic webhook returned HTTP ${http_code:-unknown}"
  fi
else
  warn "No SHADOW_WEBHOOK_SECRET set — skipping synthetic webhook test"
fi

# ---------------------------------------------------------------------------
# Discover public TradingView webhook URL
# ---------------------------------------------------------------------------
WEBHOOK_URL=""
if [[ -f "$ROOT/WEBHOOK_URL.txt" ]]; then
  WEBHOOK_URL="$(cat "$ROOT/WEBHOOK_URL.txt")"
  ok "Loaded public webhook URL from WEBHOOK_URL.txt"
elif have tailscale && tailscale status >/dev/null 2>&1; then
  public="$(tailscale funnel status 2>/dev/null | grep -oE 'https://[a-zA-Z0-9._-]+\.ts\.net' | head -1 || true)"
  if [[ -n "$public" ]]; then
    WEBHOOK_URL="${public}/webhook"
    ok "Discovered Tailscale Funnel URL: $WEBHOOK_URL"
  fi
fi

# ---------------------------------------------------------------------------
# Phase 4: summary
# ---------------------------------------------------------------------------
phase "4/4 — HermX is running"

cat <<SUMMARY
${BOLD}Local services:${RESET}
  Receiver health:  http://127.0.0.1:${SHADOW_PORT}/health
  Dashboard health: http://127.0.0.1:${CLEAN_DASHBOARD_PORT}/health
  Dashboard UI:     http://127.0.0.1:${CLEAN_DASHBOARD_PORT}/shadow/dashboard

${BOLD}TradingView webhook URL:${RESET}
SUMMARY

if [[ -n "$WEBHOOK_URL" ]]; then
  echo "  $WEBHOOK_URL"
  echo "  Header: X-Webhook-Secret: ${SHADOW_WEBHOOK_SECRET:-<not set>}"
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
    -H "X-Webhook-Secret: ${SHADOW_WEBHOOK_SECRET:-<secret>}" \\
    -d '{"strategy_id":"btcusdt_duo_base_dev_2h","symbol":"BTCUSDT","timeframe":"2h","side":"buy","tv_signal_price":"65000","tv_time":"2026-06-28T00:00:00Z","exchange":"okx","source":"tradingview"}'

${BOLD}Submit gate:${RESET}
  HERMX_LIVE_TRADING=${HERMX_LIVE_TRADING:-<unset>}
SUMMARY

if [[ "$HONOR_SUBMIT" != true ]]; then
  cat <<SUMMARY
  ${YELLOW}Order submission is BLOCKED in this run (kill switch armed).${RESET}
  Pass --honor-submit to let .env / shadow-config.json control submission.
SUMMARY
fi

echo
info "Press Ctrl-C to stop both services."
wait
