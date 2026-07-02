#!/usr/bin/env bash
# install-cron-monitors.sh — provision HermX monitoring on the Hermes built-in cron.
#
# Idempotent. Registers the read-only skills, installs the bridge scripts into the
# gateway's script dir, ensures the Telegram delivery target, and creates/updates the
# five monitor cron jobs. Safe to re-run.
#
#   bash deploy/install-cron-monitors.sh            # provision + smoke-test
#   HERMX_CRON_DRY_RUN=1 bash deploy/install-cron-monitors.sh   # print actions only
#   HERMX_CRON_SMOKE=0   bash deploy/install-cron-monitors.sh   # skip `cron run` smoke test
#
# See docs/HERMES_CRON_MONITOR_DESIGN.md for the design this implements.
set -euo pipefail

# --------------------------------------------------------------------------- #
# Paths & config                                                              #
# --------------------------------------------------------------------------- #
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMX_SKILLS="$REPO_ROOT/skills"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILLS_HUB="$HERMES_HOME/skills/trading"
SCRIPTS_DIR="$HERMES_HOME/scripts"
ENV_FILE="$HERMES_HOME/.env"

TELEGRAM_HOME_CHANNEL="7000111380"          # operator DM "Tiger (MomentumX) Quant"
WORKDIR="$REPO_ROOT"
DELIVER="telegram"

READONLY_SKILLS=(hermx-status hermx-positions hermx-trace signal-memory)
BRIDGE_SCRIPTS=(hermx_gate_lib.py hermx-reconcile-gate.py hermx-risk-gate.py hermx-health-watch.py)

DRY_RUN="${HERMX_CRON_DRY_RUN:-0}"
DO_SMOKE="${HERMX_CRON_SMOKE:-1}"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
run()  { if [ "$DRY_RUN" = "1" ]; then printf 'DRY: %s\n' "$*"; else eval "$*"; fi; }

# --------------------------------------------------------------------------- #
# 1. Preconditions                                                            #
# --------------------------------------------------------------------------- #
say "Checking hermes is on PATH"
if ! command -v hermes >/dev/null 2>&1; then
  echo "ERROR: 'hermes' not found on PATH. Install/authorize the Hermes agent first." >&2
  exit 1
fi
say "hermes: $(command -v hermes)"

# --------------------------------------------------------------------------- #
# 2. Register read-only skills (symlink into the Hermes skills hub)           #
# --------------------------------------------------------------------------- #
say "Registering read-only skills into $SKILLS_HUB"
run "mkdir -p \"$SKILLS_HUB\""
for s in "${READONLY_SKILLS[@]}"; do
  src="$HERMX_SKILLS/$s"
  if [ ! -d "$src" ]; then warn "skill source missing: $src (skipping)"; continue; fi
  run "ln -sfn \"$src\" \"$SKILLS_HUB/$s\""
done

# --------------------------------------------------------------------------- #
# 3. Install bridge scripts into the gateway script dir                       #
# --------------------------------------------------------------------------- #
say "Installing bridge scripts into $SCRIPTS_DIR"
run "mkdir -p \"$SCRIPTS_DIR\""
for f in "${BRIDGE_SCRIPTS[@]}"; do
  src="$REPO_ROOT/deploy/hermes-scripts/$f"
  if [ ! -f "$src" ]; then warn "bridge script missing: $src (skipping)"; continue; fi
  run "cp \"$src\" \"$SCRIPTS_DIR/$f\""
  case "$f" in
    *.py) run "chmod +x \"$SCRIPTS_DIR/$f\"" ;;
  esac
done

# --------------------------------------------------------------------------- #
# 4. Ensure delivery target + HERMX_DATA_DIR in the gateway env               #
# --------------------------------------------------------------------------- #
say "Ensuring $ENV_FILE has the delivery target + HERMX_DATA_DIR"
ensure_env() {   # ensure_env KEY VALUE
  local key="$1" val="$2"
  [ -f "$ENV_FILE" ] || { warn "$ENV_FILE missing — creating it"; run "touch \"$ENV_FILE\""; }
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    say "  $key already set (leaving as-is)"
  elif grep -qE "^#\\s*${key}=" "$ENV_FILE" 2>/dev/null; then
    warn "  $key is commented out in $ENV_FILE — uncommenting to $val"
    run "sed -i.bak -E \"s|^#\\s*${key}=.*|${key}=${val}|\" \"$ENV_FILE\""
  else
    run "printf '%s=%s\n' \"$key\" \"$val\" >> \"$ENV_FILE\""
  fi
}
ensure_env "TELEGRAM_HOME_CHANNEL" "$TELEGRAM_HOME_CHANNEL"
ensure_env "HERMX_DATA_DIR" "$WORKDIR"
warn "TELEGRAM_CRON_THREAD_ID intentionally NOT set (cron topic deferred)."
warn "Gateway must be restarted to pick up new env: 'hermes gateway restart' (or your service manager)."

# --------------------------------------------------------------------------- #
# 5. Create / update the five monitor cron jobs                               #
# --------------------------------------------------------------------------- #
# Provider/model intentionally NOT pinned (use default; accepts fail-closed skip risk).
WEEKLY_PROMPT="You are HermX's read-only weekly reporter. Using the loaded skills, read current status, open positions/exposure, and recent signal history. Produce a concise WEEKLY operator summary: arm state & mode, executor health, exposure and unrealized PnL, notable reconcile/stuck-order events this week, and the single most useful thing for the operator to check. Report UNKNOWN for any read that is stale or unavailable — never assume 'flat' or 'healthy'."
RECONCILE_PROMPT="A reconcile/watchdog condition changed on HermX (details in the injected context). Use the loaded skills to trace the affected signal(s) end-to-end and confirm current exposure. Report a short operator summary: what fired, current CONFIRMED state, and the next check. If, after tracing, the condition is already resolved and benign, reply with only [SILENT]. Never assume 'flat' — report UNKNOWN if a read is unavailable."
DAILY_PROMPT="Produce the HermX DAILY digest using the loaded skills: arm state & mode, executor/freshness health, open positions with exposure and uPnL, count of reconcile/operator alerts in the last 24h, and any strategy-mode overrides currently set. Keep it under 200 words. UNKNOWN for stale reads."
RISK_PROMPT="The HermX risk index changed state (snapshot in context). Using the loaded skills, read current posture and open exposure, and produce a short operator note: new risk state, what exposure is affected, and the recommended human check. If the change is benign on inspection, reply [SILENT]."

job_exists() { hermes cron list 2>/dev/null | grep -qiw "$1"; }

# ensure_job NAME <create-args...>
# Name-based idempotency: create if absent, else `cron edit` to enforce the definition.
ensure_job() {
  local name="$1"; shift
  if job_exists "$name"; then
    say "  job '$name' exists — enforcing definition via 'cron edit'"
    run "hermes cron edit \"$name\" $*" || warn "  'cron edit $name' failed — inspect manually"
  else
    say "  creating job '$name'"
    run "hermes cron create $* --name \"$name\""
  fi
}

say "Creating/updating monitor cron jobs"

ensure_job "hermx-weekly-summary" \
  "\"0 9 * * 1\" \"$WEEKLY_PROMPT\" \
   --skill hermx-status --skill hermx-positions --skill signal-memory \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-reconcile-watch" \
  "\"every 5m\" \"$RECONCILE_PROMPT\" \
   --script hermx-reconcile-gate.py \
   --skill hermx-trace --skill hermx-positions \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-daily-digest" \
  "\"0 8 * * *\" \"$DAILY_PROMPT\" \
   --skill hermx-status --skill hermx-positions --skill signal-memory \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-risk-watch" \
  "\"every 15m\" \"$RISK_PROMPT\" \
   --script hermx-risk-gate.py \
   --skill hermx-status --skill signal-memory \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-health-watch" \
  "\"every 5m\" --no-agent \
   --script hermx-health-watch.py \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

# --------------------------------------------------------------------------- #
# 6. Smoke test                                                               #
# --------------------------------------------------------------------------- #
say "Skill resolution smoke test"
run "hermes -z \"ping\" --skills hermx-status >/dev/null && echo '  hermx-status resolves OK'" \
  || warn "  skill resolution smoke test failed"

if [ "$DO_SMOKE" = "1" ]; then
  say "Firing each job once ('hermes cron run'); inspect ~/.hermes/cron/output/<job_id>/"
  for name in hermx-weekly-summary hermx-reconcile-watch hermx-daily-digest hermx-risk-watch hermx-health-watch; do
    run "hermes cron run \"$name\"" || warn "  'cron run $name' failed — inspect manually"
  done
else
  say "Skipping 'cron run' smoke test (HERMX_CRON_SMOKE=0)"
fi

say "Done. Review with 'hermes cron list'. Pause a noisy monitor with '/cron pause <name>'."
