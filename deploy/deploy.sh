#!/usr/bin/env bash
# deploy.sh — HermX VPS deploy
# Steps: git pull -> pip install -> build React UI -> offline tests ->
# restart services -> health check. Any failing step aborts the deploy;
# tests must pass BEFORE services are restarted.
# Usage:
#   bash deploy/deploy.sh             # full deploy
#   bash deploy/deploy.sh --no-pull   # skip git pull (already pulled)
#   bash deploy/deploy.sh --no-tests  # skip pytest (hotfix only)
#   bash deploy/deploy.sh --no-ui     # skip React build (UI unchanged)
set -euo pipefail

# Repo root is the parent of deploy/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Colours + logging (same style as run.sh)
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

NO_PULL=false; NO_TESTS=false; NO_UI=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull)  NO_PULL=true; shift ;;
    --no-tests) NO_TESTS=true; shift ;;
    --no-ui)    NO_UI=true; shift ;;
    -h|--help)  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          err "Unknown argument: $1"; exit 2 ;;
  esac
done

PIP="$ROOT/.venv/bin/pip"
PYTHON="$ROOT/.venv/bin/python"

if [[ "$NO_PULL" != true ]]; then
  phase "1/6 — Pull latest code"
  git pull || { err "git pull failed"; exit 1; }
  ok "Pulled latest code"
else
  phase "1/6 — Pull skipped (--no-pull)"
fi

phase "2/6 — Install Python deps"
"$PIP" install -r requirements.txt -q || { err "pip install failed"; exit 1; }
ok "Python deps installed"
if [[ "$NO_UI" != true ]]; then
  phase "3/6 — Build React UI"
  (cd "$ROOT/dashboard-ui" && npm install --prefer-offline --silent) \
    || { err "npm install failed — aborting deploy"; exit 1; }
  ok "npm install done"
  (cd "$ROOT/dashboard-ui" && NEXT_PUBLIC_API_BASE="" npm run build) \
    || { err "React build failed — aborting deploy"; exit 1; }
  ok "React UI built → dashboard-ui/out/"
else
  phase "3/6 — UI build skipped (--no-ui)"
fi
# Tests must pass before services are restarted
if [[ "$NO_TESTS" != true ]]; then
  phase "4/6 — Run offline tests"
  "$PYTHON" -m pytest -q \
    -m "not integration and not okx_paper and not kucoin_paper and not hyperliquid_paper" \
    || { err "Tests failed — aborting deploy, services NOT restarted"; exit 1; }
  ok "Offline tests passed"
else
  phase "4/6 — Tests skipped (--no-tests)"
fi
phase "5/6 — Restart services"
sudo systemctl restart hermx-receiver hermx-dashboard || { err "systemctl restart failed"; exit 1; }
ok "Services restarted"
phase "6/6 — Health check"
info "Waiting 5s for services to come up..."
sleep 5
HEALTHY=true
for probe in "Receiver|http://127.0.0.1:8891/health" "Dashboard|http://127.0.0.1:8098/health"; do
  name="${probe%%|*}"; url="${probe#*|}"
  if curl -sf "$url" >/dev/null 2>&1; then
    ok "$name healthy ($url)"
  else
    err "$name NOT healthy ($url)"; HEALTHY=false
  fi
done
if [[ "$HEALTHY" == true ]]; then
  phase "Deploy succeeded"
  exit 0
else
  err "Deploy completed but a service is unhealthy — check: journalctl -u hermx-receiver -u hermx-dashboard"
  exit 1
fi
