#!/usr/bin/env bash
# deploy.sh — HermX VPS deploy (config-safe, snapshotted, auto-rollback)
#
# Pipeline:
#   snapshot state -> capture START_SHA -> config-safe pull -> pip install ->
#   build UI -> offline tests -> restart -> health check.
# If the health check fails, the deploy AUTOMATICALLY ROLLS BACK to START_SHA:
#   git reset --hard START_SHA -> restore operator config -> pip install (old
#   requirements) -> rebuild UI -> restart -> re-probe.
#
# Why pip install ALWAYS runs (even with --no-pull):
#   git pull only updates src/. Third-party deps live in .venv/, outside git.
#   The pulled commit may bump requirements.txt or import a new package; if the
#   venv isn't reconciled the service restarts into an ImportError crash-loop.
#   pip install -r requirements.txt is ~1s and a no-op when already satisfied,
#   so it is mandatory, not optional.
#
# Usage:
#   bash deploy/deploy.sh             # full deploy
#   bash deploy/deploy.sh --no-pull   # skip git fetch/pull (pip STILL runs)
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
    -h|--help)  sed -n '/^# Usage:/,/--no-ui/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          err "Unknown argument: $1"; exit 2 ;;
  esac
done

PIP="$ROOT/.venv/bin/pip"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PIP" || ! -x "$PYTHON" ]]; then
  err "venv missing at $ROOT/.venv — create it first (python -m venv .venv)"; exit 1
fi

# Operator-editable, git-TRACKED config. These conflict on pull and must be
# restored after a hard reset on rollback. control-state.json is gitignored, so
# it is NOT here (it never conflicts and must never be reverted).
CONFIG_PATHS=(engine-config.json strategies config)

# --- Snapshot ------------------------------------------------------------------
# Backup dir for this run: operator config (restored on rollback) + a copy of
# durable transaction state (safety net only — the WAL is append-only and is
# NEVER rewound on rollback; rewinding it would erase real trades).
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$ROOT/.deploy-backups/$TS"
CFG_SNAP="$BACKUP_DIR/config"
STATE_SNAP="$BACKUP_DIR/state"
mkdir -p "$CFG_SNAP" "$STATE_SNAP"

phase "0/7 — Snapshot state & capture rollback point"
START_SHA="$(git rev-parse HEAD)"
info "START_SHA = $START_SHA"
printf '%s\n' "$START_SHA" > "$BACKUP_DIR/START_SHA"

# Snapshot operator config (used to restore live edits after a rollback reset).
for p in "${CONFIG_PATHS[@]}"; do
  if [[ -e "$ROOT/$p" ]]; then
    mkdir -p "$CFG_SNAP/$(dirname "$p")"
    cp -a "$ROOT/$p" "$CFG_SNAP/$p"
  fi
done
ok "Operator config snapshotted → $CFG_SNAP"

# Snapshot durable transaction state (forensic safety net, not restored).
for f in logs/raw-webhooks.jsonl logs/pipeline.jsonl logs/alerts.jsonl \
         logs/order-journal.jsonl latest.json control-state.json; do
  [[ -e "$ROOT/$f" ]] && { mkdir -p "$STATE_SNAP/$(dirname "$f")"; cp -a "$ROOT/$f" "$STATE_SNAP/$f"; }
done
# Any sealed order-journal segments / checkpoints too, if present.
cp -a "$ROOT"/logs/order-journal*.jsonl "$STATE_SNAP/logs/" 2>/dev/null || true
ok "Transaction state snapshotted → $STATE_SNAP"

# --- Helpers -------------------------------------------------------------------
restore_config() {
  # Overlay snapshotted operator config back onto the working tree.
  for p in "${CONFIG_PATHS[@]}"; do
    if [[ -e "$CFG_SNAP/$p" ]]; then
      rm -rf "${ROOT:?}/$p"
      mkdir -p "$ROOT/$(dirname "$p")"
      cp -a "$CFG_SNAP/$p" "$ROOT/$p"
    fi
  done
}

build_ui() {
  (cd "$ROOT/dashboard-ui" && npm install --prefer-offline --silent) || return 1
  (cd "$ROOT/dashboard-ui" && NEXT_PUBLIC_API_BASE="" npm run build) || return 1
}

restart_services() {
  sudo systemctl restart hermx-receiver hermx-dashboard
}

probe_health() {
  # Returns 0 only if every service answers /health. Caller waits beforehand.
  local healthy=true name url
  for probe in "Receiver|http://127.0.0.1:8891/health" \
               "Dashboard|http://127.0.0.1:8098/health"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -sf "$url" >/dev/null 2>&1; then
      ok "$name healthy ($url)"
    else
      err "$name NOT healthy ($url)"; healthy=false
    fi
  done
  [[ "$healthy" == true ]]
}

validate_hermes_skills() {
  # Hermes agent is optional; the skills only matter when Hermes is on PATH.
  # Loop over every skill directory that ships a SKILL.md and ensure it is
  # symlinked into ~/.hermes/skills/. Directories without a SKILL.md (e.g. the
  # hermx-ops shared library) are not standalone skills and are skipped.
  # When Hermes is NOT on PATH, stay completely silent — no warnings.
  local hermes_present=false
  command -v hermes >/dev/null 2>&1 && hermes_present=true
  local skill_src skill_name skill_link
  for skill_src in "$ROOT"/skills/*/; do
    skill_src="${skill_src%/}"
    [[ -f "$skill_src/SKILL.md" ]] || continue
    skill_name="$(basename "$skill_src")"
    skill_link="$HOME/.hermes/skills/$skill_name"
    if [[ -L "$skill_link" && "$(readlink -f "$skill_link")" == "$(readlink -f "$skill_src")" ]]; then
      ok "Hermes skill symlink valid: $skill_link -> $skill_src"
    elif [[ "$hermes_present" == true ]]; then
      if mkdir -p "$(dirname "$skill_link")" && ln -sfn "$skill_src" "$skill_link"; then
        ok "Hermes skill symlink created: $skill_link -> $skill_src"
      else
        warn "Hermes installed but failed to create skill symlink: ln -sfn $skill_src $skill_link"
      fi
    fi
  done
}

rollback() {
  phase "ROLLBACK — reverting to $START_SHA"
  warn "Forward deploy failed health check; rolling back."
  # 1) Code back to the pre-deploy commit.
  git reset --hard "$START_SHA" || { err "git reset failed — MANUAL RECOVERY NEEDED"; return 1; }
  # 2) Restore operator config (the reset blew away local edits).
  restore_config
  ok "Code + operator config restored to pre-deploy state"
  # 3) Reconcile venv to the OLD requirements.txt (now back on disk).
  "$PIP" install -r requirements.txt -q || { err "pip install (rollback) failed — MANUAL RECOVERY NEEDED"; return 1; }
  ok "Python deps reconciled to old requirements"
  # 4) Rebuild UI from old code so dashboard-ui/out matches the rolled-back src.
  if [[ "$NO_UI" != true ]]; then
    build_ui || warn "UI rebuild during rollback failed — UI may be stale"
  fi
  # 5) Restart + re-probe.
  restart_services || { err "systemctl restart (rollback) failed — MANUAL RECOVERY NEEDED"; return 1; }
  info "Waiting 5s for rolled-back services to come up..."
  sleep 5
  if probe_health; then
    err "Rolled back to $START_SHA successfully. The NEW deploy was rejected — investigate before retrying."
    err "Backups: $BACKUP_DIR"
    return 0
  fi
  err "ROLLBACK ALSO UNHEALTHY — services down at $START_SHA. MANUAL RECOVERY NEEDED."
  err "Backups: $BACKUP_DIR  |  Logs: journalctl -u hermx-receiver -u hermx-dashboard"
  return 1
}

# --- 1/7 Config-safe pull ------------------------------------------------------
if [[ "$NO_PULL" != true ]]; then
  phase "1/7 — Config-safe pull"
  STASHED=false
  if [[ -n "$(git status --porcelain -- "${CONFIG_PATHS[@]}" 2>/dev/null)" ]]; then
    info "Operator config has local edits — stashing across pull"
    git stash push -m "deploy-autostash-$TS" -- "${CONFIG_PATHS[@]}" \
      && STASHED=true \
      || { err "Failed to stash operator config — aborting before pull"; exit 1; }
  fi
  if ! git pull --ff-only; then
    err "git pull --ff-only failed (diverged history?) — aborting"
    [[ "$STASHED" == true ]] && { git stash pop || warn "stash pop failed; your edits are in: git stash list"; }
    exit 1
  fi
  if [[ "$STASHED" == true ]]; then
    if ! git stash pop; then
      err "Operator config conflicts with pulled changes — resolve manually."
      err "Your edits are preserved in: git stash list  (deploy-autostash-$TS)"
      exit 1
    fi
  fi
  ok "Pulled latest code (operator config preserved)"
else
  phase "1/7 — Pull skipped (--no-pull)"
fi

# --- 2/7 pip install (MANDATORY — see header) ----------------------------------
phase "2/7 — Install Python deps (mandatory)"
"$PIP" install -r requirements.txt -q || { err "pip install failed — aborting (services NOT restarted)"; exit 1; }
ok "Python deps reconciled to requirements.txt"

# --- 3/7 Build UI --------------------------------------------------------------
if [[ "$NO_UI" != true ]]; then
  phase "3/7 — Build React UI"
  build_ui || { err "React build failed — aborting deploy"; exit 1; }
  ok "React UI built → dashboard-ui/out/"
else
  phase "3/7 — UI build skipped (--no-ui)"
fi

# --- 4/7 Tests (gate before restart) -------------------------------------------
if [[ "$NO_TESTS" != true ]]; then
  phase "4/7 — Run offline tests"
  "$PYTHON" -m pytest -q \
    -m "not integration and not okx_paper and not kucoin_paper and not hyperliquid_paper" \
    || { err "Tests failed — aborting deploy, services NOT restarted"; exit 1; }
  ok "Offline tests passed"
else
  phase "4/7 — Tests skipped (--no-tests)"
fi

# --- 5/7 Restart ---------------------------------------------------------------
phase "5/7 — Restart services"
restart_services || { err "systemctl restart failed"; rollback || true; exit 1; }
ok "Services restarted"

# --- 5.5/7 Cron monitors (best-effort, create-only) ----------------------------
# Provision monitoring so existing installs auto-get it on upgrade. CREATE_ONLY=1
# means we ONLY create jobs that are missing — a manually paused/edited job is
# never touched, so a deploy can never silently re-enable operator-disabled cron.
if command -v hermes >/dev/null 2>&1; then
  phase "5.5/7 — Provisioning cron monitors (create-only)"
  HERMX_CRON_CREATE_ONLY=1 HERMX_CRON_SMOKE=0 \
    bash "$ROOT/deploy/install-cron-monitors.sh" || warn "Cron monitor provisioning failed — inspect manually"
else
  info "Hermes not on PATH — skipping cron monitor provisioning"
fi

# --- 6/7 Health check ----------------------------------------------------------
phase "6/7 — Health check"
info "Waiting 5s for services to come up..."
sleep 5
if probe_health; then
  HEALTHY=true
else
  HEALTHY=false
fi

# --- 7/7 Verdict ---------------------------------------------------------------
phase "7/7 — Verdict"
if [[ "$HEALTHY" == true ]]; then
  phase "Deploy succeeded"
  validate_hermes_skills || true
  info "Backups for this run: $BACKUP_DIR"
  exit 0
fi
rollback && exit 1 || exit 1
