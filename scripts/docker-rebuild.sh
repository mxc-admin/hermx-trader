#!/usr/bin/env bash
# docker-rebuild.sh — DEVELOPER workflow: rebuild the HermX image from LOCAL source
# and (re)deploy the local stack with it. This is the counterpart to docker-update.sh:
#
#   docker-update.sh   PULLS the published GHCR image   (repo-less operators, /opt/hermx)
#   docker-rebuild.sh  BUILDS from this repo's source    (developers, working-tree changes)
#
# Why this exists: docker-compose.yml/.host.yml use `image: ${HERMX_IMAGE:-ghcr.io/...}`
# with NO `build:` key, so `docker compose up -d --build` no longer builds anything. The
# only ways to ship a code change were (a) push to main and wait for docker-publish.yml, or
# (b) hand-run `docker build`. This script closes that gap: multi-stage build (Node UI +
# Python runtime) from the working tree, retag compose onto the fresh local image via
# HERMX_IMAGE, restart, health-check, and OPTIONALLY tag + push to GHCR.
#
# Preserves: named volumes (hermx-data, hermx-state), .env, engine-config.json, and the
# operator's strategy edits — same guarantees as docker-update.sh. Build happens BEFORE any
# restart, so a failed build leaves the running stack untouched.
set -uo pipefail

# REPO_ROOT is the build context (Dockerfile lives here). DEPLOY_DIR is where compose runs
# (holds .env, engine-config.json, strategies/). For a developer these are the SAME dir; the
# --deploy-dir flag lets you build here but deploy into a separate install (e.g. /opt/hermx).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="${HERMX_INSTALL_DIR:-$REPO_ROOT}"

LOCAL_TAG="${HERMX_LOCAL_TAG:-hermx-trader:local}"   # tag the local build carries
PUSH_REF="${HERMX_PUSH_REF:-ghcr.io/mxc-admin/hermx-trader:latest}"  # default push target
COMPOSE_FILE="docker-compose.yml"
DRY_RUN=0; FORCE=0; NO_CACHE=0; DO_PUSH=0; SKIP_DEPLOY=0
PUSH_REF_OVERRIDE=""

# --- colored output (mirrors docker-update.sh / install-docker.sh) -----------
if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"
  RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""; fi
phase(){ printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info(){ printf '  %s\n' "$1"; }
ok(){   printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn(){ printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err(){  printf '  %sx%s %s\n' "$RED" "$RESET" "$1"; }
have(){ command -v "$1" >/dev/null 2>&1; }
# ask: honors --force (auto-yes). $2 default = y|n.
ask(){ local p="$1" d="${2:-n}" r s; (( FORCE )) && return 0
  [[ "$d" == y ]] && s="[Y/n]" || s="[y/N]"; read -r -p "  $p $s " r || true
  r="${r:-$d}"; [[ "$r" =~ ^[Yy] ]]; }

usage(){ cat <<EOF
Usage: bash docker-rebuild.sh [--dry-run] [--host] [--no-cache] [--force]
                              [--push [REF]] [--no-deploy] [--tag TAG]
                              [--deploy-dir DIR]

Rebuild the HermX image from LOCAL source, then redeploy the local stack with it.

  --dry-run       Preview the plan (build/deploy/push commands); make no changes.
  --host          Deploy with docker-compose.host.yml (host-networking fallback).
  --no-cache      docker build --no-cache (force a clean rebuild, incl. npm ci).
  --force         Skip confirmations (CI/automation). Implies non-interactive.
  --push [REF]    After a successful build, tag + push to REF (default $PUSH_REF).
                  Requires a prior 'docker login'. Still deploys locally too —
                  add --no-deploy to publish WITHOUT restarting the local stack.
  --no-deploy     Build (and optionally --push) only; do NOT restart the local stack.
  --tag TAG       Local image tag to build (default $LOCAL_TAG).
  --deploy-dir DIR  Compose/deploy dir holding .env + engine-config.json + strategies/
                  (default: this repo root, $REPO_ROOT).
  -h, --help      This message.

Env: HERMX_LOCAL_TAG (default $LOCAL_TAG), HERMX_PUSH_REF (default $PUSH_REF),
     HERMX_INSTALL_DIR (deploy dir; default repo root).
EOF
}

# --- args --------------------------------------------------------------------
while [[ $# -gt 0 ]]; do case "$1" in
  --dry-run)    DRY_RUN=1;;
  --host)       COMPOSE_FILE="docker-compose.host.yml";;
  --no-cache)   NO_CACHE=1;;
  --force)      FORCE=1;;
  --no-deploy)  SKIP_DEPLOY=1;;
  --push)       DO_PUSH=1
                # optional REF argument: consume next token if it isn't another flag.
                if [[ $# -ge 2 && "$2" != -* ]]; then PUSH_REF_OVERRIDE="$2"; shift; fi;;
  --tag)        [[ $# -ge 2 ]] || { err "--tag needs a value"; exit 2; }; LOCAL_TAG="$2"; shift;;
  --deploy-dir) [[ $# -ge 2 ]] || { err "--deploy-dir needs a value"; exit 2; }; DEPLOY_DIR="$2"; shift;;
  -h|--help)    usage; exit 0;;
  *) err "Unknown flag: $1"; usage; exit 2;;
esac; shift; done

[[ -n "$PUSH_REF_OVERRIDE" ]] && PUSH_REF="$PUSH_REF_OVERRIDE"
DC="docker compose -f $COMPOSE_FILE"
(( DRY_RUN )) && info "${YELLOW}DRY RUN — no build, no deploy, no push.${RESET}"

# --- PHASE 0: prerequisites --------------------------------------------------
phase "PHASE 0: Prerequisites"
have docker || { err "Docker not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose v2 required."; exit 1; }
[[ -f "$REPO_ROOT/Dockerfile" ]] || { err "No Dockerfile in $REPO_ROOT — run this from the repo."; exit 1; }
[[ -f "$REPO_ROOT/dashboard-ui/package-lock.json" ]] \
  || warn "dashboard-ui/package-lock.json missing — the ui-builder stage's 'npm ci' will fail."
ok "Docker + compose present; build context: $REPO_ROOT"
info "Local tag       : $LOCAL_TAG"
info "Deploy dir      : $DEPLOY_DIR $( [[ "$DEPLOY_DIR" == "$REPO_ROOT" ]] && echo '(== repo root)' )"
info "Compose file    : $COMPOSE_FILE"
(( DO_PUSH ))     && info "Push target     : $PUSH_REF"
(( SKIP_DEPLOY )) && info "Deploy          : SKIPPED (--no-deploy)"

# --- PHASE 1: safety preview -------------------------------------------------
phase "PHASE 1: Safety preview"
ENV_FILE="$DEPLOY_DIR/.env"
LIVE="false"
if [[ -f "$ENV_FILE" ]]; then
  LIVE="$(grep -E '^HERMX_LIVE_TRADING=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
  LIVE="${LIVE:-false}"
fi
if [[ "$LIVE" == "true" ]]; then
  warn "${RED}HERMX_LIVE_TRADING=true — the deploy target can reach a LIVE exchange.${RESET}"
  warn "Deploying restarts the receiver; disk state (positions/ledgers) is unaffected,"
  warn "but the process WILL bounce. Deploy only during a safe window."
else
  ok "HERMX_LIVE_TRADING=$LIVE (paper/demo — safe)."
fi
# Warn about uncommitted working-tree changes so it's clear WHAT is being baked.
if have git && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    warn "Working tree has uncommitted changes — the build bakes them (HEAD=$GIT_SHA + dirty)."
  else
    ok "Building clean HEAD ($GIT_SHA)."
  fi
fi

# --- confirm to proceed ------------------------------------------------------
if (( DRY_RUN )); then
  phase "DRY RUN complete"
  info "Would build : docker build $( ((NO_CACHE)) && echo --no-cache ) -t $LOCAL_TAG $REPO_ROOT"
  (( DO_PUSH ))     && info "Would tag   : $LOCAL_TAG -> $PUSH_REF   then docker push $PUSH_REF"
  if (( SKIP_DEPLOY )); then
    info "Would deploy: (skipped — --no-deploy)"
  else
    info "Would deploy: (cd $DEPLOY_DIR && HERMX_IMAGE=$LOCAL_TAG $DC up -d)"
  fi
  info "Re-run without --dry-run to apply."
  exit 0
fi
if [[ "$LIVE" == "true" ]] && ! (( FORCE )) && ! (( SKIP_DEPLOY )); then
  ask "LIVE trading is enabled on the deploy target. Rebuild + redeploy anyway?" "n" \
    || { warn "Aborted by operator (live trading)."; exit 1; }
else
  ask "Rebuild image from local source$( ((SKIP_DEPLOY)) || echo ' and redeploy' )?" "y" \
    || { warn "Aborted by operator."; exit 1; }
fi

# --- PHASE 2: build ----------------------------------------------------------
phase "PHASE 2: Build image from local source"
BUILD_ARGS=(build -t "$LOCAL_TAG")
(( NO_CACHE )) && BUILD_ARGS+=(--no-cache)
BUILD_ARGS+=("$REPO_ROOT")
info "docker ${BUILD_ARGS[*]}"
if ! docker "${BUILD_ARGS[@]}"; then
  err "Build failed. Running stack (if any) is UNTOUCHED. Fix the build and re-run."
  exit 1
fi
NEW_ID="$(docker image inspect "$LOCAL_TAG" --format '{{.Id}}' 2>/dev/null || echo '<none>')"
ok "Built $LOCAL_TAG (${NEW_ID#sha256:})"

# --- PHASE 3: optional push --------------------------------------------------
if (( DO_PUSH )); then
  phase "PHASE 3: Tag + push to registry"
  warn "Publishing $LOCAL_TAG as $PUSH_REF — this goes to a REMOTE registry."
  if ask "Push $PUSH_REF now? (needs a prior 'docker login' to its registry)" "n"; then
    docker tag "$LOCAL_TAG" "$PUSH_REF" || { err "docker tag failed."; exit 1; }
    if docker push "$PUSH_REF"; then
      ok "Pushed $PUSH_REF"
    else
      err "docker push failed — is this shell logged in? Try: docker login ${PUSH_REF%%/*}"
      exit 1
    fi
  else
    warn "Skipped push."
  fi
else
  phase "PHASE 3: Push (skipped)"
  info "Not pushing (pass --push [REF] to publish this build)."
fi

# --- PHASE 4: sync Docker setup/config into a separate deploy dir ------------
# When deploying into a dir OTHER than the repo, keep its Docker setup in lockstep with the
# repo's (compose files are the single source of truth — the installer seeds them from the
# image; here we seed them from source). When DEPLOY_DIR == REPO_ROOT there is nothing to
# sync (compose files already ARE the repo's). Operator files (.env, engine-config.json,
# strategies/) are NEVER overwritten.
if (( SKIP_DEPLOY )); then
  phase "PHASE 4: Deploy (skipped)"
  info "Build-only run (--no-deploy). Image $LOCAL_TAG is ready locally."
  echo; ok "${BOLD}Rebuild complete — image built$( ((DO_PUSH)) && echo ' and push attempted' ).${RESET}"
  exit 0
fi

phase "PHASE 4: Sync Docker setup"
if [[ "$DEPLOY_DIR" != "$REPO_ROOT" ]]; then
  for f in docker-compose.yml docker-compose.host.yml; do
    src="$REPO_ROOT/$f"; dst="$DEPLOY_DIR/$f"
    [[ -f "$src" ]] || continue
    if [[ ! -f "$dst" ]]; then
      cp "$src" "$dst" && ok "Installed $f into $DEPLOY_DIR."
    elif ! diff -q "$src" "$dst" >/dev/null 2>&1; then
      warn "$f differs between repo and $DEPLOY_DIR:"
      diff -u "$dst" "$src" | sed 's/^/    /' | head -40
      if ask "Overwrite $DEPLOY_DIR/$f with the repo version?" "y"; then
        cp "$src" "$dst" && ok "Updated $f."
      else
        info "Left $DEPLOY_DIR/$f as-is."
      fi
    else
      ok "$f already matches the repo."
    fi
  done
else
  info "Deploy dir is the repo root — compose files are already source-of-truth (no sync)."
fi

# --- PHASE 5: deploy ---------------------------------------------------------
phase "PHASE 5: Deploy local stack"
[[ -d "$DEPLOY_DIR" ]] || { err "Deploy dir $DEPLOY_DIR not found."; exit 1; }
cd "$DEPLOY_DIR" || { err "Cannot cd to $DEPLOY_DIR"; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { err "$DEPLOY_DIR/$COMPOSE_FILE missing."; exit 1; }
if [[ ! -f "$ENV_FILE" ]]; then
  err "$ENV_FILE missing — compose env_file requires it."
  info "Seed it first:  cp $REPO_ROOT/setup/env.example $ENV_FILE   (then fill in creds)"
  info "or run the installer for a guided setup: bash $REPO_ROOT/scripts/install-docker.sh"
  exit 1
fi
# Shell env wins over .env interpolation, so exporting HERMX_IMAGE pins compose to the fresh
# local build. Because the local tag's image ID changed, `up -d` recreates the containers.
info "HERMX_IMAGE=$LOCAL_TAG $DC up -d"
if ! HERMX_IMAGE="$LOCAL_TAG" $DC up -d; then
  err "docker compose up -d failed. See: HERMX_IMAGE=$LOCAL_TAG $DC logs"
  exit 1
fi
ok "Compose up -d issued against $LOCAL_TAG."

# --- PHASE 6: health checks --------------------------------------------------
phase "PHASE 6: Health checks (up to 60s)"
poll(){ # $1=name $2=port
  local name="$1" port="$2" i
  for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      ok "$name healthy (127.0.0.1:$port) after $((i*2))s"; return 0
    fi
    sleep 2
  done
  err "$name did NOT become healthy on 127.0.0.1:$port within 60s"; return 1
}
HEALTH_OK=1
poll "Receiver"  8891 || HEALTH_OK=0
poll "Dashboard" 8098 || HEALTH_OK=0

# --- PHASE 7: verify + report ------------------------------------------------
phase "PHASE 7: Verify"
info "Containers:"
HERMX_IMAGE="$LOCAL_TAG" $DC ps 2>/dev/null | sed 's/^/    /'
info "Image in use: $LOCAL_TAG (${NEW_ID#sha256:})"

if (( HEALTH_OK )); then
  echo; ok "${BOLD}Rebuild + deploy complete — both services healthy on the new image.${RESET}"
  exit 0
fi

# --- rollback guidance -------------------------------------------------------
phase "ROLLBACK GUIDANCE"
err "One or more health checks failed. Persistent volumes are intact."
info "Inspect logs first:   HERMX_IMAGE=$LOCAL_TAG $DC logs --tail=100"
info "Option A — restart:   HERMX_IMAGE=$LOCAL_TAG $DC restart"
info "Option B — roll back to the published image (discard this local build):"
info "    $DC pull && $DC up -d      # uses ghcr.io/... default, not $LOCAL_TAG"
info "Option C — rebuild clean:      bash scripts/docker-rebuild.sh --no-cache"
exit 1
