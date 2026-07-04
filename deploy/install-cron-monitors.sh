#!/usr/bin/env bash
# install-cron-monitors.sh — provision HermX monitoring on the Hermes built-in cron.
#
# Idempotent. Registers the read-only skills, installs the bridge scripts into the
# gateway's script dir, ensures the Telegram delivery target, and creates/updates the
# five monitor cron jobs. Safe to re-run.
#
#   bash deploy/install-cron-monitors.sh            # provision + smoke-test
#   HERMX_CRON_DRY_RUN=1 bash deploy/install-cron-monitors.sh     # print actions only
#   HERMX_CRON_SMOKE=0   bash deploy/install-cron-monitors.sh     # skip `cron run` smoke test
#   HERMX_CRON_CREATE_ONLY=1 bash deploy/install-cron-monitors.sh # only create missing jobs,
#                                                                 # never edit existing ones
#                                                                 # (preserves operator pauses/edits)
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
BRIDGE_SCRIPTS=(hermx_gate_lib.py hermx-reconcile-gate.py hermx-health-watch.py hermx-intake-gate.py hermx-ledger-reconcile.py hermx-reconcile-lag-gate.py)

DRY_RUN="${HERMX_CRON_DRY_RUN:-0}"
DO_SMOKE="${HERMX_CRON_SMOKE:-1}"
CREATE_ONLY="${HERMX_CRON_CREATE_ONLY:-0}"

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
# hermx-risk-watch intentionally OMITTED: its gate keys on `risk_index_gate_enabled`, a flag
# that does not exist anywhere in the codebase, so the job could never fire — worse than no
# monitor (false reassurance). The hermx-risk-gate.py script is kept in the repo but unwired.
WEEKLY_PROMPT="You are HermX's read-only weekly reporter. Using the loaded skills, read current status, open positions/exposure, and recent signal history. Produce a concise WEEKLY operator summary: arm state & mode, executor health, exposure and unrealized PnL, notable reconcile/stuck-order events this week, and the single most useful thing for the operator to check. Report UNKNOWN for any read that is stale or unavailable — never assume 'flat' or 'healthy'."
RECONCILE_PROMPT="A reconcile/watchdog condition changed on HermX (details in the injected context). Use the loaded skills to trace the affected signal(s) end-to-end and confirm current exposure. Report a short operator summary: what fired, current CONFIRMED state, and the next check. If, after tracing, the condition is already resolved and benign, reply with only [SILENT]. Never assume 'flat' — report UNKNOWN if a read is unavailable."
DAILY_PROMPT="Produce the HermX DAILY digest using the loaded skills: arm state & mode, executor/freshness health, open positions with exposure and uPnL, count of reconcile/operator alerts in the last 24h, and any strategy-mode overrides currently set. Keep it under 200 words. UNKNOWN for stale reads."
SIGNAL_LATE_PROMPT="HermX has received NO TradingView intake for over 3 days (details in the injected context). The receiver may be up while alerts stopped arriving — a silent observability hole. Produce a short operator note: how long since the last intake, the likely cause (TV alert paused, webhook URL changed, network), and the recommended human check. If, on inspection, a recent intake is present after all, reply with only [SILENT]. Never assume 'quiet market' — report the gap plainly."

job_exists() { hermes cron list 2>/dev/null | grep -qiw "$1"; }

# ensure_job NAME <create-args...>
# Name-based idempotency. Two behaviours:
#   default          — create if absent, else `cron edit` to enforce the definition.
#   CREATE_ONLY=1    — create if absent, else SKIP entirely (preserves operator state:
#                      manual pauses, schedule edits, prompt changes are left untouched).
#                      Used by deploy.sh so an upgrade never silently re-enables a paused job.
ensure_job() {
  local name="$1"; shift
  if job_exists "$name"; then
    if [ "$CREATE_ONLY" = "1" ]; then
      say "  job '$name' exists — preserving operator state (unset HERMX_CRON_CREATE_ONLY to enforce definition)"
      return 0
    fi
    say "  job '$name' exists — enforcing definition via 'cron edit'"
    run "hermes cron edit \"$name\" $*" || warn "  'cron edit $name' failed — inspect manually"
  else
    say "  creating job '$name'"
    run "hermes cron create $* --name \"$name\""
  fi
}

say "Creating/updating monitor cron jobs"

ensure_job "hermx-weekly" \
  "\"0 9 * * 1\" \"$WEEKLY_PROMPT\" \
   --skill hermx-status --skill hermx-positions --skill signal-memory \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-reconcile" \
  "\"every 5m\" \"$RECONCILE_PROMPT\" \
   --script hermx-reconcile-gate.py \
   --skill hermx-trace --skill hermx-positions \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-daily" \
  "\"0 8 * * *\" \"$DAILY_PROMPT\" \
   --skill hermx-status --skill hermx-positions --skill signal-memory \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-health-check" \
  "\"every 5m\" --no-agent \
   --script hermx-health-watch.py \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

# P&L-ledger reconcile safety net (Phase 3). Non-LLM (--no-agent): folds each active
# strategy's recent order history into closed-trades.jsonl on a cadence so a close is
# captured even when the dashboard is idle (History-window race mitigation). Distinct
# from the LLM hermx-reconcile watchdog above and from HERMX_RECONCILE_ENABLED.
ensure_job "hermx-ledger-reconcile" \
  "\"every 10m\" --no-agent \
   --script hermx-ledger-reconcile.py \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

# Reconcile-lag observability gate (P1-2). Non-LLM (--no-agent): reads the ledger's
# newest recorded_at_ms (schema v3) and wakes the operator when now - max(recorded_at_ms)
# exceeds HERMX_MAX_RECONCILE_LAG_MS (default 20m > this 15m cadence). Fail-open on a
# missing ledger / unreachable dashboard, like hermx-ledger-reconcile.
ensure_job "hermx-reconcile-lag" \
  "\"every 15m\" --no-agent \
   --script hermx-reconcile-lag-gate.py \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

ensure_job "hermx-signal-late" \
  "\"every 30m\" \"$SIGNAL_LATE_PROMPT\" \
   --script hermx-intake-gate.py \
   --workdir \"$WORKDIR\" --deliver $DELIVER"

# --------------------------------------------------------------------------- #
# 6. Smoke test                                                               #
# --------------------------------------------------------------------------- #
say "Skill resolution smoke test"
run "hermes -z \"ping\" --skills hermx-status >/dev/null && echo '  hermx-status resolves OK'" \
  || warn "  skill resolution smoke test failed"

if [ "$DO_SMOKE" = "1" ]; then
  say "Firing each job once ('hermes cron run'); inspect ~/.hermes/cron/output/<job_id>/"
  for name in hermx-weekly hermx-reconcile hermx-daily hermx-health-check hermx-signal-late hermx-ledger-reconcile hermx-reconcile-lag; do
    run "hermes cron run \"$name\"" || warn "  'cron run $name' failed — inspect manually"
  done
else
  say "Skipping 'cron run' smoke test (HERMX_CRON_SMOKE=0)"
fi

say "Done. Review with 'hermes cron list'. Pause a noisy monitor with '/cron pause <name>'."
