#!/usr/bin/env bash
# deploy.sh — HermX VPS deploy (config-safe, snapshotted, auto-rollback)
#
# Pipeline:
#   self-update deploy/ (if stale & not --no-pull, then re-exec) -> snapshot state ->
#   capture START_SHA -> auto-migrate legacy tracked ledger -> config-safe pull ->
#   pip install -> build UI -> offline tests -> restart -> health check.
#
# Self-update (self-healing distribution — see the block after the log helpers):
#   Operators, cron, and the hx-upgrade skill all run a *possibly-stale* on-disk
#   copy of this script. Before doing anything else it refreshes ONLY the deploy/
#   folder from origin's default branch and re-execs, so the rest of the pipeline
#   always runs current deploy logic (e.g. a newly-added self-heal step). This is
#   skipped under --no-pull, which means no git network calls at all.
#
# One-time closed-trades.jsonl untrack migration (automatic, no flag):
#   The realized-P&L ledger used to be git-tracked. It is now a per-host,
#   append-only live file (.gitignore'd). On hosts where it is still tracked, the
#   upstream untrack commit would make `git pull --ff-only` abort on the local
#   live rows. Before pulling, this script backs up and un-stages the ledger so
#   the pull applies, then restores the live rows on top once it is untracked.
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
#   bash deploy/deploy.sh --no-pull   # zero git network: skip self-update + pull (pip STILL runs)
#   bash deploy/deploy.sh --no-tests  # skip pytest (hotfix only)
#   bash deploy/deploy.sh --no-ui     # skip React build (UI unchanged)
#   bash deploy/deploy.sh --check-drift-only  # report un-pulled local drift (exit 3 if any) then exit; no venv/network
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

# --- Self-update bootstrap (self-healing distribution) -------------------------
# WHY: every caller — an operator at a shell, a cron job, the hx-upgrade skill —
# invokes a *possibly-stale* on-disk copy of THIS script (`bash deploy/deploy.sh`).
# If the host is behind, its first deploy after an upgrade runs OLD pipeline logic
# and can silently miss a newly-added self-heal step (this is exactly how a stale
# host would skip the closed-trades.jsonl untrack migration below). To close that
# gap we refresh ONLY the deploy/ folder from the remote's default branch and, if
# it changed, re-exec ourselves so the rest of the pipeline always runs current
# deploy code. The migration and every other step then run once, in the fresh
# process — never doubled, because we re-exec BEFORE reaching any of them.
#
# This is DELIBERATELY NARROW and a SEPARATE git operation from the main pull
# (step 1/7). They exist for two different purposes:
#   * self-update  — touches ONLY deploy/ (the deploy machinery). It runs here,
#                    but is SKIPPED when --no-pull is passed (--no-pull means
#                    zero git network calls). It never
#                    touches src/, dashboard-ui/, operator config, or the P&L
#                    ledger, so it cannot change what the services run — only how
#                    they get deployed.
#   * main pull    — the full config-safe `git pull --ff-only` of the whole repo
#                    (step 1/7), which IS governed by --no-pull. That is the
#                    application-code sync.
#
# Safety guarantees:
#   * Loop guard: the sentinel HERMX_DEPLOY_REEXECED=1 is exported before the
#     re-exec and checked at the top here, so self-update + re-exec happens AT
#     MOST ONCE per deploy. No infinite re-exec.
#   * Fail-open: every git step is best-effort. On any failure (not a git repo,
#     no remote, offline, detached HEAD, checkout conflict) we warn and continue
#     with the current on-disk script. Self-update is a nice-to-have, never a
#     hard gate on the deploy.
#   * Runs BEFORE arg parsing, START_SHA capture, and the migration block, so it
#     cannot disturb the rollback point (the re-exec'd process captures its own
#     START_SHA) and cannot double-run any pipeline step.
# --no-pull means "zero git network calls of any kind" — so self-update is
# skipped too, not just the step-1/7 pull. We must know this BEFORE the arg
# parser exists (it lives after this block), so do a deliberately dumb pre-scan
# of "$@" for the exact token --no-pull. This is NOT flag validation: unknown
# flags are still left untouched for the real parser below (which, on a normal
# run, executes only in the fresh process after re-exec). set -e safe: the match
# lives in an `if`, never a bare failing test.
SELF_UPDATE=true
for _arg in "$@"; do
  # --check-drift-only is a read-only probe: it must never trigger a network
  # fetch/re-exec either, so it suppresses self-update exactly like --no-pull.
  if [[ "$_arg" == "--no-pull" || "$_arg" == "--check-drift-only" ]]; then SELF_UPDATE=false; break; fi
done
if [[ "${HERMX_DEPLOY_REEXECED:-}" != "1" && "$SELF_UPDATE" == true ]]; then
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # Resolve the branch to track from origin's default branch; fall back to main.
    SU_BRANCH="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
    if [[ -z "$SU_BRANCH" || "$SU_BRANCH" == "HEAD" ]]; then
      SU_BRANCH="main"
    fi
    # Fetch just that branch's refs (no working-tree changes, no merge).
    if git fetch --quiet origin "$SU_BRANCH" 2>/dev/null; then
      # Does the committed deploy/ tree differ from origin's? `git diff --quiet`
      # exits non-zero when it differs — that is the "stale, update me" signal,
      # NOT an error, so it lives in an `if` and never trips `set -e`.
      if ! git diff --quiet "HEAD" "origin/$SU_BRANCH" -- deploy/ 2>/dev/null; then
        warn "deploy/ is behind origin/$SU_BRANCH — self-updating deploy machinery before running"
        # Narrow checkout: ONLY deploy/ is touched; HEAD is not moved. The
        # checkout + re-exec are in one brace group so bash parses all of it
        # before the checkout overwrites this file mid-run (avoids re-reading a
        # changed script). On checkout failure we warn and fall through to run
        # the current on-disk script instead of aborting the deploy.
        if { git checkout --quiet "origin/$SU_BRANCH" -- deploy/ \
             && ok "deploy/ refreshed from origin/$SU_BRANCH — re-executing with fresh deploy code" \
             && HERMX_DEPLOY_REEXECED=1 exec bash "$ROOT/deploy/deploy.sh" "$@"; }; then
          : # unreachable: exec replaces the process on success
        else
          warn "Narrow deploy/ self-update failed — continuing with current on-disk script"
        fi
      fi
    else
      warn "git fetch failed (offline / no remote / detached?) — continuing with current on-disk deploy.sh"
    fi
  else
    warn "Not inside a git work tree — skipping deploy self-update"
  fi
fi

NO_PULL=false; NO_TESTS=false; NO_UI=false; CHECK_DRIFT_ONLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull)          NO_PULL=true; shift ;;
    --no-tests)         NO_TESTS=true; shift ;;
    --no-ui)            NO_UI=true; shift ;;
    --check-drift-only) CHECK_DRIFT_ONLY=true; shift ;;
    -h|--help)  sed -n '/^# Usage:/,/--check-drift-only/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          err "Unknown argument: $1"; exit 2 ;;
  esac
done

# Operator-editable, git-TRACKED config. These conflict on pull and must be
# restored after a hard reset on rollback. control-state.json is gitignored, so
# it is NOT here (it never conflicts and must never be reverted).
# Defined BEFORE the venv check so the drift probe below can run on a host that
# has no .venv yet (the --check-drift-only path must not require Python).
CONFIG_PATHS=(engine-config.json strategies config)

# --- Drift detection -----------------------------------------------------------
# "Drift" = tracked, non-config files that differ from HEAD in the working tree.
# It matters because a failed deploy rolls back with `git reset --hard START_SHA`,
# which would DESTROY such local edits. Operator config (CONFIG_PATHS) and the
# live P&L ledger (closed-trades.jsonl) are exempt: config is snapshotted+restored
# across rollback, and the ledger is handled by its own untrack migration.
detect_drift() {
  # `git diff --name-only HEAD` = staged+unstaged changes vs HEAD (not untracked).
  # The unquoted $(printf ...) is intentional word-splitting to expand one
  # ':(exclude)<path>' pathspec per CONFIG_PATHS entry. `|| true` keeps set -e
  # happy (a non-repo / no-diff still returns empty, never aborts the deploy).
  git diff --name-only HEAD -- . $(printf ':(exclude)%s ' "${CONFIG_PATHS[@]}") ':(exclude)closed-trades.jsonl' 2>/dev/null || true
}

print_drift() {
  # Space-tolerant line-by-line print of a newline-delimited drift list.
  while IFS= read -r _line; do [[ -n "$_line" ]] && printf '  %s\n' "$_line"; done <<< "$1"
}

# --check-drift-only: read-only probe. Report drift and exit 3; else exit 0.
# Runs BEFORE the venv check on purpose — it needs neither Python nor network.
if [[ "$CHECK_DRIFT_ONLY" == true ]]; then
  _drift="$(detect_drift)"
  if [[ -n "$_drift" ]]; then
    err "Local drift detected — tracked non-config files differ from HEAD:"
    print_drift "$_drift"
    err "A deploy would refuse this (rollback resets to HEAD and would destroy the edits)."
    err "Commit or revert them, then retry."
    exit 3
  fi
  ok "No drift"
  exit 0
fi

PIP="$ROOT/.venv/bin/pip"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PIP" || ! -x "$PYTHON" ]]; then
  err "venv missing at $ROOT/.venv — create it first (python -m venv .venv)"; exit 1
fi

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

# --- 0.5/7 One-time closed-trades.jsonl untrack migration ----------------------
# closed-trades.jsonl is a per-host, append-only live P&L ledger that must NOT be
# git-tracked (see .gitignore). Hosts installed before the untrack commit still
# have it tracked; there, `git pull --ff-only` would abort because the local live
# rows collide with the incoming delete-from-index. Self-heal such hosts here:
# back up the live ledger, make the working tree clean for that one file so the
# pull can fast-forward, then (after the pull untracks it) restore the live rows
# on top so no financial history is lost. On already-migrated hosts (file no
# longer tracked) this whole block no-ops silently — the ls-files check fails.
# Skipped under --no-pull: with no pull there is nothing to untrack the file, so
# cleaning the working tree would wipe live rows with no restore to follow.
LEDGER_MIGRATED=false
LEDGER_BACKUP="$BACKUP_DIR/closed-trades.jsonl.premigrate"
if [[ "$NO_PULL" != true ]] && git ls-files --error-unmatch closed-trades.jsonl >/dev/null 2>&1; then
  phase "0.5/7 — Migrate closed-trades.jsonl (untrack live P&L ledger)"
  if [[ -e "$ROOT/closed-trades.jsonl" ]]; then
    cp -a "$ROOT/closed-trades.jsonl" "$LEDGER_BACKUP"
    ok "Live ledger backed up → $LEDGER_BACKUP ($(wc -l < "$ROOT/closed-trades.jsonl") rows)"
  else
    warn "closed-trades.jsonl is tracked but absent on disk — nothing to back up"
  fi
  # Clean the working tree for this one file so `git pull --ff-only` won't abort.
  git checkout -- closed-trades.jsonl 2>/dev/null || true
  LEDGER_MIGRATED=true
  info "Working tree cleaned for closed-trades.jsonl; the pull will untrack it"
fi

# --- 0.75/7 Drift gate (refuse to deploy over un-pulled local edits) -----------
# Hard stop BEFORE the pull. If a deploy proceeds and later fails its health
# check, rollback runs `git reset --hard START_SHA`, which permanently DESTROYS
# any local edits to tracked non-config files. Operator config and the P&L ledger
# are exempt (snapshotted/migrated); everything else is drift and must be
# committed or reverted first. Runs regardless of --no-pull, since rollback (and
# thus the data-loss risk) can happen on a --no-pull run too.
_drift="$(detect_drift)"
if [[ -n "$_drift" ]]; then
  phase "Drift gate — refusing to deploy"
  err "Tracked non-config files have un-pulled local edits:"
  print_drift "$_drift"
  err "Refusing to deploy: a rollback (git reset --hard) would DESTROY these edits."
  err "Commit or revert them, then retry."
  exit 3
fi

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

# --- 1.5/7 Restore live P&L ledger after untrack migration ---------------------
# If the migration ran and the pull has now untracked closed-trades.jsonl, overlay
# the backed-up live rows so the (now gitignored) ledger keeps its full history.
if [[ "$LEDGER_MIGRATED" == true ]]; then
  if ! git ls-files --error-unmatch closed-trades.jsonl >/dev/null 2>&1; then
    if [[ -e "$LEDGER_BACKUP" ]]; then
      cp -a "$LEDGER_BACKUP" "$ROOT/closed-trades.jsonl"
      ok "Live P&L ledger restored → closed-trades.jsonl ($(wc -l < "$ROOT/closed-trades.jsonl") rows), now untracked"
    else
      ok "closed-trades.jsonl untracked (no live rows existed to restore)"
    fi
  else
    # Unexpected: pull did not untrack it. Working-tree edits were reverted by the
    # pre-pull checkout, so leave the backup in place and tell the operator.
    warn "closed-trades.jsonl still tracked after pull — untrack commit not present upstream?"
    warn "Live rows preserved in: $LEDGER_BACKUP (restore manually if needed)"
  fi
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

# --- 4.5/7 Positions-First migration (one-time, idempotent) --------------------
# Wipe-clean restart of the transaction ledgers (operator-approved 2026-07-16):
# backs up then removes closed-trades.jsonl + the order-attribution maps so the
# leg-aware (leg_kind open|close) ledger starts clean. Stamped under the data
# dir (.migrations/positions-v1.done) — already-migrated hosts no-op instantly.
# Runs BEFORE restart so services come up on the clean state; best-effort so a
# migration hiccup never blocks a deploy (legacy rows still read correctly).
phase "4.5/7 — Positions-First migration (idempotent)"
if [[ -f "$ROOT/scripts/migrate-positions-v1.sh" ]]; then
  bash "$ROOT/scripts/migrate-positions-v1.sh" \
    && ok "Positions migration checked/applied" \
    || warn "Positions migration failed — inspect scripts/migrate-positions-v1.sh output"
else
  warn "scripts/migrate-positions-v1.sh missing — skipping positions migration"
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
