#!/usr/bin/env bash
#
# smoke_run.sh — Boot the webhook receiver + dashboard against the DEMO profile
# for manual smoke testing. Submission is FORCED OFF for every strategy.
#
# REFACTOR_PLAN.md:181 — "Write a make/script target to run the receiver +
# dashboard against the demo profile locally for manual smoke testing."
#
# SAFETY: the real submit gate is the per-strategy submit_orders flag. This
# script seeds a throwaway SHADOW_ROOT with a demo engine-config.json and a
# strategies/ copy whose every submit_orders is false, then exports
# HERMX_LIVE_TRADING=false. No strategy can submit orders. It is a smoke-test
# harness, never a live launcher.
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
: "${HERMX_RECEIVER_PORT:=${SHADOW_PORT:-8891}}"
: "${HERMX_DASHBOARD_PORT:=${CLEAN_DASHBOARD_PORT:-8098}}"
export HERMX_RECEIVER_PORT SHADOW_PORT="$HERMX_RECEIVER_PORT"
export HERMX_DASHBOARD_PORT CLEAN_DASHBOARD_PORT="$HERMX_DASHBOARD_PORT"

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

# --- seed a throwaway dry-run SHADOW_ROOT (no strategy can submit orders) ----
# Copies config/runtime.demo.json -> $TMP_SHADOW/engine-config.json and copies
# strategies/*.json -> $TMP_SHADOW/strategies/ with every submit_orders forced
# false. The originals under the repo are never modified.
TMP_SHADOW=""
seed_shadow_root() {
  if [ ! -f "$ROOT/config/runtime.demo.json" ]; then
    echo "ERROR: config/runtime.demo.json missing; cannot seed shadow root." >&2
    return 1
  fi
  TMP_SHADOW="$(mktemp -d "${TMPDIR:-/tmp}/hermx-smoke.XXXXXX")"
  cp "$ROOT/config/runtime.demo.json" "$TMP_SHADOW/engine-config.json"
  mkdir -p "$TMP_SHADOW/strategies"
  "$PYTHON" - "$ROOT/strategies" "$TMP_SHADOW/strategies" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
for path in sorted(src.glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["submit_orders"] = False
    (dst / path.name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
  echo "Seeded dry-run SHADOW_ROOT: $TMP_SHADOW (submit_orders forced false)"
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
  echo "  dry-run posture:   strategies seeded with submit_orders=false; HERMX_LIVE_TRADING=false"
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
seed_shadow_root

# Run from the repo root (so src/ resolves) but point every config/strategy/
# ledger lookup at the throwaway dry-run shadow root seeded above.
export SHADOW_ROOT="$TMP_SHADOW"
export HERMX_ROOT="$TMP_SHADOW"
export SHADOW_PORT
export CLEAN_DASHBOARD_PORT
export HERMX_RECEIVER_PORT="$SHADOW_PORT"
export HERMX_DASHBOARD_PORT="$CLEAN_DASHBOARD_PORT"
# SAFETY: HERMX_LIVE_TRADING=false blocks live submission; combined with the
# submit_orders=false strategy copies, no strategy can submit orders at all.
export HERMX_LIVE_TRADING="false"

cat <<BANNER
============================================================
 HermX SMOKE RUN — DEMO profile
 DRY-RUN / NO-SUBMIT: every strategy seeded with submit_orders=false
 plus HERMX_LIVE_TRADING=false. No strategy can submit orders.
------------------------------------------------------------
 python:    $PYTHON
 shadow:    $TMP_SHADOW
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
  if [ -n "${TMP_SHADOW:-}" ] && [ -d "$TMP_SHADOW" ]; then
    rm -rf "$TMP_SHADOW"
  fi
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
