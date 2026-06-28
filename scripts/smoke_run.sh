#!/usr/bin/env bash
#
# smoke_run.sh — Boot the webhook receiver + dashboard against the DEMO profile
# for manual smoke testing. Submission is FORCED OFF (kill switch armed).
#
# REFACTOR_PLAN.md:181 — "Write a make/script target to run the receiver +
# dashboard against the demo profile locally for manual smoke testing."
#
# SAFETY: this script exports HERMX_SUBMIT_ENABLED=false, which hard-blocks all
# OKX order submission before any subprocess is spawned (see Level 0 in
# skills/emergency-stop.md). It is a smoke-test harness, never a live launcher.
#
# Usage:
#   ./scripts/smoke_run.sh            boot both services (dry-run, no submit)
#   ./scripts/smoke_run.sh --check    validate prerequisites, do not launch
#   ./scripts/smoke_run.sh --help     show this help
#
set -euo pipefail

# --- locate repo root (scripts/.. ) -----------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# --- pick a python interpreter: prefer repo venv ----------------------------
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "ERROR: no python interpreter found (.venv/bin/python or python3)." >&2
  exit 1
fi

# --- defaults ---------------------------------------------------------------
: "${SHADOW_PORT:=8891}"
: "${CLEAN_DASHBOARD_PORT:=8098}"

usage() {
  sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- load .env (ignore comments / blank lines), without clobbering kill sw. --
load_env() {
  if [ -f "$ROOT/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        ''|\#*) continue ;;
      esac
      case "$line" in
        *=*) ;;
        *) continue ;;
      esac
      key="${line%%=*}"
      val="${line#*=}"
      # trim surrounding whitespace from key
      key="$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -z "$key" ] && continue
      export "$key=$val"
    done < "$ROOT/.env"
  fi
}

# --- ensure shadow-config.json exists (copy from demo profile if missing) ----
ensure_config() {
  if [ ! -f "$ROOT/shadow-config.json" ]; then
    if [ ! -f "$ROOT/config/runtime.demo.json" ]; then
      echo "ERROR: config/runtime.demo.json missing; cannot seed shadow-config.json." >&2
      return 1
    fi
    cp "$ROOT/config/runtime.demo.json" "$ROOT/shadow-config.json"
    echo "Created shadow-config.json from config/runtime.demo.json"
  fi
}

# --- prerequisite / sanity check (no servers launched) ----------------------
do_check() {
  local ok=0
  echo "smoke_run --check (repo: $ROOT)"
  echo "  python:            $PYTHON ($("$PYTHON" --version 2>&1))"

  if [ -f "$ROOT/.env" ]; then
    echo "  .env:              present"
  else
    echo "  .env:              MISSING (services may fail without credentials)"
  fi

  if [ -f "$ROOT/config/runtime.demo.json" ]; then
    echo "  demo profile:      present (config/runtime.demo.json)"
  else
    echo "  demo profile:      MISSING (config/runtime.demo.json)"; ok=1
  fi

  for src in src/webhook_receiver.py src/dashboard.py; do
    if [ ! -f "$ROOT/$src" ]; then
      echo "  $src: MISSING"; ok=1; continue
    fi
    if "$PYTHON" -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/$src" 2>/dev/null; then
      echo "  $src: parses OK"
    else
      echo "  $src: SYNTAX ERROR"; ok=1
    fi
  done

  echo "  SHADOW_PORT:       $SHADOW_PORT"
  echo "  DASHBOARD_PORT:    $CLEAN_DASHBOARD_PORT"
  echo "  HERMX_SUBMIT_ENABLED would be forced to: false (dry-run / no submit)"
  if [ "$ok" -eq 0 ]; then
    echo "CHECK: OK"
  else
    echo "CHECK: FAILED" >&2
  fi
  return "$ok"
}

# --- argument parsing -------------------------------------------------------
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --check)   load_env; do_check; exit $? ;;
  "" )       ;;
  *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
esac

# --- launch -----------------------------------------------------------------
load_env
ensure_config

export SHADOW_ROOT="$ROOT"
export SHADOW_PORT
export CLEAN_DASHBOARD_PORT
# SAFETY: force the global kill switch ON for smoke runs. Hard-blocks all order
# submission regardless of config (skills/emergency-stop.md, Level 0).
export HERMX_SUBMIT_ENABLED="false"

cat <<BANNER
============================================================
 HermX SMOKE RUN — DEMO profile
 DRY-RUN / NO-SUBMIT: HERMX_SUBMIT_ENABLED=false (kill switch ARMED)
 No OKX orders can be submitted in this mode.
------------------------------------------------------------
 python:    $PYTHON
 receiver:  http://127.0.0.1:$SHADOW_PORT
 dashboard: http://127.0.0.1:$CLEAN_DASHBOARD_PORT
============================================================
BANNER

RECEIVER_PID=""
DASHBOARD_PID=""

cleanup() {
  echo
  echo "Shutting down smoke run..."
  for pid in "$DASHBOARD_PID" "$RECEIVER_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  echo "Stopped."
}
trap cleanup INT TERM EXIT

"$PYTHON" "$ROOT/src/webhook_receiver.py" &
RECEIVER_PID=$!
echo "receiver  PID=$RECEIVER_PID  (http://127.0.0.1:$SHADOW_PORT)"

"$PYTHON" "$ROOT/src/dashboard.py" &
DASHBOARD_PID=$!
echo "dashboard PID=$DASHBOARD_PID  (http://127.0.0.1:$CLEAN_DASHBOARD_PORT)"

echo "Both services up. Press Ctrl-C to stop."
# Wait on either child; cleanup() (EXIT trap) tears both down.
wait
