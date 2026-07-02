---
description: Repeatable HermX Docker update — safely pull the latest image, optionally re-seed strategies, restart, and health-check. Run periodically, after a new release, or when the operator wants fresh strategies/config from the image. Named volumes (hermx-data, hermx-state) and operator edits to .env / engine-config.json / strategies survive the update.
---

# /docker-update — Refresh a HermX Docker Installation

Pulls the latest `hermx-trader` image into an existing `/opt/hermx` install,
optionally re-seeds `strategies/` from the new image, restarts the compose
stack, and polls the receiver + dashboard health endpoints. It is **safe by
default**: it previews changes, refuses to restart a live-trading install
without confirmation, and never touches the persistent volumes or the operator's
`.env` / `engine-config.json`.

## When to run

- **Periodic update** (weekly/monthly) to pull the latest published image.
- **After a new HermX release** is announced.
- **When you want fresh strategies/config** baked into a newer image (opt-in
  re-seed — see the prompt during the run).

## Prerequisites

- An **existing** HermX Docker install (created by `scripts/install-docker.sh`).
  This workflow updates; it does not install. If `/opt/hermx` is empty, run the
  installer first.
- `docker` + `docker compose` v2 present (`docker compose version`).
- Write access to the install dir (the installer chowns it to the invoking user;
  otherwise run with `sudo`).
- Network access to `ghcr.io` to pull the image.
- Optional: a `/backup` directory if you want the pre-update volume snapshot.

## The script

Save as `docker-update.sh` and run `bash docker-update.sh [flags]`, or paste the
whole block into a terminal. Flags:

- `--dry-run` — preview only; no pull, no re-seed, no restart.
- `--host` — use `docker-compose.host.yml` (host-networking fallback).
- `--force` — skip all confirmation prompts (CI/automation). Implies non-interactive.
  Does **not** re-seed strategies (safe default — it would overwrite operator edits).
- `--reseed` — re-seed `strategies/` from the image; required to re-seed under `--force`.
- `-h`, `--help` — usage.

```bash
#!/usr/bin/env bash
# docker-update.sh — refresh an existing HermX Docker install.
# Pulls the latest image, optionally re-seeds strategies/, restarts, health-checks.
# Preserves: named volumes (hermx-data, hermx-state), .env, engine-config.json,
#            operator strategy edits. Only control-state.json (in hermx-state) is
#            mutable runtime state and survives across pulls.
set -uo pipefail

IMAGE="${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}"
INSTALL_DIR="${HERMX_INSTALL_DIR:-/opt/hermx}"
COMPOSE_FILE="docker-compose.yml"
DRY_RUN=0; FORCE=0; RESEED=0

# Compose derives its project name from the install-dir basename, so the named
# volumes are created prefixed (e.g. hermx_hermx-state). Reference them by their
# full Docker volume names in `docker run` contexts (compose commands use the
# bare compose-file names).
COMPOSE_PROJECT="$(basename "$INSTALL_DIR")"
VOL_STATE="${COMPOSE_PROJECT}_hermx-state"
VOL_DATA="${COMPOSE_PROJECT}_hermx-data"

# --- colored output (mirrors install-docker.sh) ------------------------------
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
Usage: bash docker-update.sh [--dry-run] [--host] [--force] [--reseed]
  --dry-run   Preview only; no pull/re-seed/restart.
  --host      Use docker-compose.host.yml (host networking).
  --force     Skip confirmations (CI/automation). Does NOT re-seed strategies.
  --reseed    Re-seed strategies/ from the image (needed to re-seed under --force).
  -h, --help  This message.
Env: HERMX_IMAGE (default $IMAGE), HERMX_INSTALL_DIR (default $INSTALL_DIR).
EOF
}

# --- args --------------------------------------------------------------------
while [[ $# -gt 0 ]]; do case "$1" in
  --dry-run) DRY_RUN=1;;
  --host)    COMPOSE_FILE="docker-compose.host.yml";;
  --force)   FORCE=1;;
  --reseed)  RESEED=1;;
  -h|--help) usage; exit 0;;
  *) err "Unknown flag: $1"; usage; exit 2;;
esac; shift; done

DC="docker compose -f $COMPOSE_FILE"
(( DRY_RUN )) && info "${YELLOW}DRY RUN — no changes will be made.${RESET}"

# --- PHASE 0: locate install -------------------------------------------------
phase "PHASE 0: Locate install"
have docker || { err "Docker not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose v2 required."; exit 1; }
[[ -d "$INSTALL_DIR" ]] || { err "Install dir $INSTALL_DIR not found. Run install-docker.sh first."; exit 1; }
cd "$INSTALL_DIR" || { err "Cannot cd to $INSTALL_DIR"; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { err "$INSTALL_DIR/$COMPOSE_FILE missing — not a HermX install?"; exit 1; }
ENV_FILE="$INSTALL_DIR/.env"
ok "Install: $INSTALL_DIR (compose: $COMPOSE_FILE)"

# --- PHASE 1: safety preview -------------------------------------------------
phase "PHASE 1: Safety preview"
LIVE="false"
if [[ -f "$ENV_FILE" ]]; then
  LIVE="$(grep -E '^HERMX_LIVE_TRADING=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
  LIVE="${LIVE:-false}"
fi
if [[ "$LIVE" == "true" ]]; then
  warn "${RED}HERMX_LIVE_TRADING=true — this install can reach a LIVE exchange.${RESET}"
  warn "Updating restarts the receiver; in-flight orders/positions are unaffected on"
  warn "disk, but the process WILL bounce. Proceed only during a safe window."
else
  ok "HERMX_LIVE_TRADING=$LIVE (paper/demo — safe)."
fi

# current vs latest image
CUR_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || echo '<none>')"
info "Configured image : $IMAGE"
info "Current local id : ${CUR_ID#sha256:}"
RUNNING="$($DC ps --services --filter status=running 2>/dev/null | tr '\n' ' ')"
info "Running services : ${RUNNING:-<none>}"

# volume sizes (best-effort; needs a throwaway container)
if have docker; then
  info "Volume sizes (persist across update):"
  docker run --rm -v "$VOL_STATE":/state -v "$VOL_DATA":/data busybox \
    sh -c 'printf "    hermx-state %s\n" "$(du -sh /state 2>/dev/null | cut -f1)";
           printf "    hermx-data  %s\n" "$(du -sh /data  2>/dev/null | cut -f1)"' 2>/dev/null \
    || warn "Could not size volumes (they still persist across the update)."
fi

# backup reminder
info ""
info "${BOLD}Backup (optional):${RESET} volumes survive the pull, but for a restore point run:"
info "  mkdir -p /backup && docker run --rm -v $VOL_STATE:/state -v $VOL_DATA:/data \\"
info "    -v /backup:/backup busybox tar czf /backup/hermx-backup-\$(date +%Y%m%d).tgz /state /data"

# --- confirm to proceed ------------------------------------------------------
if (( DRY_RUN )); then
  phase "DRY RUN complete"
  info "Would pull: $IMAGE"
  info "Would restart with: $DC up -d"
  info "Re-run without --dry-run to apply."
  exit 0
fi
if [[ "$LIVE" == "true" ]] && ! (( FORCE )); then
  ask "LIVE trading is enabled. Pull + restart anyway?" "n" \
    || { warn "Aborted by operator (live trading)."; exit 1; }
else
  ask "Pull latest image and restart?" "y" \
    || { warn "Aborted by operator."; exit 1; }
fi

# --- PHASE 2: pull -----------------------------------------------------------
phase "PHASE 2: Pull latest image"
$DC pull || { err "docker compose pull failed."; exit 1; }
NEW_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || echo '<none>')"
if [[ "$NEW_ID" == "$CUR_ID" ]]; then
  ok "Image already up to date (${NEW_ID#sha256:})."
else
  ok "Pulled new image: ${NEW_ID#sha256:}"
fi

# --- PHASE 3: optional re-seed strategies ------------------------------------
phase "PHASE 3: Strategies"
# Re-seed is decoupled from --force: --force auto-accepts everything EXCEPT the
# re-seed (which overwrites operator strategy files). Under --force, re-seed only
# happens when --reseed is also given. Without --force, the operator is prompted.
do_reseed=0
if (( FORCE )) && ! (( RESEED )); then
  info "Re-seed skipped under --force (use --reseed to override)."
elif (( RESEED )); then
  do_reseed=1
elif ask "New strategies may be available in the image. Re-seed strategies/? (operator edits to existing files are preserved)" "n"; then
  do_reseed=1
fi
if (( do_reseed )); then
  # Copy /app/strategies/. from the NEW image onto the host, same throwaway-container
  # pattern as install-docker.sh. `cp -r .` merges: image files land alongside operator
  # files; existing same-named files are overwritten with the image version.
  mkdir -p "$INSTALL_DIR/strategies"
  docker run --rm --entrypoint sh -v "$INSTALL_DIR/strategies:/seed" "$IMAGE" \
    -c 'set -e; cp -r /app/strategies/. /seed/ 2>/dev/null || true' \
    && ok "Re-seeded strategies/ from $IMAGE." \
    || warn "Re-seed failed — leaving existing strategies/ untouched."
  warn "Image versions of same-named strategy files were overwritten. Review $INSTALL_DIR/strategies/ before the next signal."
else
  info "Skipped re-seed — keeping current strategies/ as-is."
fi
info "Note: .env and engine-config.json are never touched by this update."

# --- PHASE 4: restart --------------------------------------------------------
phase "PHASE 4: Restart stack"
$DC up -d || { err "docker compose up -d failed. See: $DC logs"; exit 1; }
ok "Compose up -d issued."

# --- PHASE 5: health checks --------------------------------------------------
phase "PHASE 5: Health checks (up to 60s)"
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

# --- PHASE 6: verify + report ------------------------------------------------
phase "PHASE 6: Verify"
info "Containers:"
$DC ps 2>/dev/null | sed 's/^/    /'
info "Image in use: $IMAGE (${NEW_ID#sha256:})"
docker run --rm -v "$VOL_STATE":/state -v "$VOL_DATA":/data busybox \
  sh -c 'printf "    hermx-state %s\n" "$(du -sh /state 2>/dev/null | cut -f1)";
         printf "    hermx-data  %s\n" "$(du -sh /data  2>/dev/null | cut -f1)"' 2>/dev/null \
  || true

if (( HEALTH_OK )); then
  echo; ok "${BOLD}Update complete — both services healthy.${RESET}"
  exit 0
fi

# --- rollback guidance -------------------------------------------------------
phase "ROLLBACK GUIDANCE"
err "One or more health checks failed. The persistent volumes are intact."
info "Inspect logs first:   $DC logs --tail=100"
info "Option A — restart:   $DC restart"
info "Option B — re-pull previous / clean image cache:"
info "    $DC down && docker image prune -f && $DC pull && $DC up -d"
info "Option C — restore from backup (if you made one in Phase 1):"
info "    $DC down"
info "    docker run --rm -v $VOL_STATE:/state -v $VOL_DATA:/data \\"
info "      -v /backup:/backup busybox tar xzf /backup/hermx-backup-YYYYMMDD.tgz -C /"
info "    $DC up -d"
exit 1
```

## What the script does

1. **Locate install** — resolves `HERMX_INSTALL_DIR` (default `/opt/hermx`),
   confirms Docker + compose v2, and that the chosen compose file exists.
2. **Safety preview** — reads `.env`, loudly warns if `HERMX_LIVE_TRADING=true`,
   shows current image id, running services, and `hermx-state` / `hermx-data`
   volume sizes, then prints the optional pre-update backup command.
3. **Pull** — `docker compose pull` (or the `--host` variant). Reports whether
   the image actually changed.
4. **Strategies (opt-in)** — asks before re-seeding. If yes, a throwaway
   container copies `/app/strategies/.` from the *new* image onto the host
   `strategies/` dir (same pattern as `install-docker.sh`). `.env` and
   `engine-config.json` are never touched.
5. **Restart** — `docker compose up -d`. Named volumes are reattached; state
   persists.
6. **Health checks** — polls `http://127.0.0.1:8891/health` (receiver) and
   `:8098/health` (dashboard) for up to 60s each.
7. **Verify / rollback** — prints `ps`, image id, and volume sizes. On health
   failure it prints rollback options and exits non-zero.

## Safety notes

- **Live trading.** If `HERMX_LIVE_TRADING=true`, the script requires an explicit
  extra confirmation before pulling/restarting (unless `--force`). Restarting
  bounces the receiver process — run only during a safe window.
- **Volume persistence.** `hermx-data` (append-only ledgers/logs) and
  `hermx-state` (mutable snapshots incl. `control-state.json`) are **named
  volumes** and survive `pull` + `up -d`. Per-strategy mode overrides in
  `control-state.json` are preserved.
- **Operator files preserved.** `.env`, `engine-config.json`, and your edits to
  existing `strategies/*.json` are never overwritten — except that an **opt-in
  re-seed** overwrites same-named strategy files with the image versions. Decline
  the re-seed prompt to keep everything as-is.
- **`--dry-run`** previews the whole plan (live warning, image ids, volume sizes,
  intended commands) and makes zero changes.
- **Rollback.** Volumes are never destroyed by this workflow. If health fails:
  `docker compose logs` → `restart` → re-pull → restore from the Phase-1 backup.
  `docker compose down` alone keeps the volumes; only `down -v` would delete them
  (this workflow never does that).

## Verification — what healthy output looks like

```
=== PHASE 5: Health checks (up to 60s) ===
  ✓ Receiver healthy (127.0.0.1:8891) after 4s
  ✓ Dashboard healthy (127.0.0.1:8098) after 6s

=== PHASE 6: Verify ===
  Containers:
    NAME               IMAGE                                    STATUS
    hermx-receiver     ghcr.io/mxc-admin/hermx-trader:latest    Up (healthy)
    hermx-dashboard    ghcr.io/mxc-admin/hermx-trader:latest    Up (healthy)
  Image in use: ghcr.io/mxc-admin/hermx-trader:latest (a1b2c3...)
    hermx-state 1.2M
    hermx-data  48M

  ✓ Update complete — both services healthy.
```

Exit code `0` means both health checks passed. Any non-zero exit means a health
check failed or the operator aborted — read the `ROLLBACK GUIDANCE` block, and
the volumes are still intact.
