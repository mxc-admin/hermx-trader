---
description: Developer workflow — rebuild the HermX Docker image from LOCAL source (multi-stage Node UI + Python runtime), then redeploy the local stack with the freshly built image, health-check it, and optionally tag + push to GHCR. Use this after editing code in this repo. Counterpart to /docker-update, which only PULLS the published image for repo-less operators.
---

# /docker-rebuild — Build the HermX Image from Local Source & Deploy

Rebuilds the `hermx-trader` image from **this repo's working tree** (multi-stage:
Node.js dashboard-ui build → Python runtime), retags the compose stack onto the
fresh local image via `HERMX_IMAGE`, restarts, and polls the receiver + dashboard
health endpoints. Optionally tags and pushes the build to a registry. It is **safe
by default**: it previews with `--dry-run`, refuses to redeploy a live-trading
install without confirmation, builds **before** touching the running stack (a
failed build changes nothing), and never destroys volumes or overwrites `.env` /
`engine-config.json` / your strategy edits.

## /docker-rebuild vs /docker-update

| | `/docker-rebuild` (this) | `/docker-update` |
|---|---|---|
| **Image source** | `docker build` from local repo source | `docker compose pull` from GHCR |
| **Audience** | Developers with the repo checked out | Repo-less operators (`/opt/hermx`) |
| **When** | You changed code and want to test it in Docker | A new release is published and you want it |
| **Publishes?** | Optional `--push` to GHCR | Never |

Why it exists: `docker-compose.yml` / `docker-compose.host.yml` use
`image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}` with **no `build:`
key**, so `docker compose up -d --build` no longer builds anything. Before this
workflow the only ways to run a local code change in Docker were to push to `main`
and wait for the `docker-publish.yml` CI job, or to hand-run `docker build`. This
workflow closes that gap.

## When to run

- **After editing code** in `src/`, `dashboard-ui/`, `config/`, `strategies/`,
  `requirements.txt`, or the `Dockerfile` — to run the change in the real container
  before pushing.
- **To reproduce the CI image locally** (`docker-publish.yml` builds the same
  multi-stage Dockerfile; this builds it on your machine).
- **To publish a new image to GHCR so others can install it** via
  `install-docker.sh` (fresh install) or `docker-update.sh` (update an existing
  install), run:

  ```bash
  bash scripts/docker-rebuild.sh --push
  ```

  This builds from local source, then tags + pushes to
  `ghcr.io/mxc-admin/hermx-trader:latest` — the exact ref those two scripts and the
  compose files default to (`image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}`).
  Once the push completes, anyone can install/update against the new image. Needs a
  prior `docker login ghcr.io`, and the script asks for an explicit confirmation
  before it pushes. Add `--no-deploy` to publish **without** also restarting your
  local stack.

### Publishing: CI vs. manual `--push`

The **normal** way to publish is through CI: push code to `main` and the
`docker-publish.yml` workflow builds the same multi-stage Dockerfile and pushes
`ghcr.io/mxc-admin/hermx-trader:latest` (plus `sha-` and, for `v*` tags, semver
tags) automatically. Use the manual `--push` when you need to publish **without**
going through CI — e.g. to ship a local patch urgently, or to push a one-off
version tag (`--push ghcr.io/mxc-admin/hermx-trader:v1.2.3`). Both paths land the
same GHCR ref that installers consume, so a manual `--push` to `:latest` is
functionally equivalent to what CI produces on a `main` push.

## Prerequisites

- This repo checked out, with a working `Dockerfile` and
  `dashboard-ui/package-lock.json` (the `ui-builder` stage runs `npm ci`).
- `docker` + `docker compose` v2 (`docker compose version`).
- A `.env` in the deploy dir (the repo root by default). If missing, seed it:
  `cp setup/env.example .env` and fill in demo/sandbox creds, or run
  `scripts/install-docker.sh` for a guided setup.
- For `--push`: `docker login` to the target registry first (GHCR:
  `docker login ghcr.io`).

## Run it

The script is committed at `scripts/docker-rebuild.sh`. From the repo root:

```bash
bash scripts/docker-rebuild.sh [flags]
```

### Flags

- `--dry-run` — preview the build/deploy/push plan; make no changes.
- `--host` — deploy with `docker-compose.host.yml` (host-networking fallback).
- `--no-cache` — `docker build --no-cache` (clean rebuild, re-runs `npm ci`).
- `--force` — skip all confirmations (CI/automation). Implies non-interactive.
- `--push [REF]` — after a successful build, tag + push to `REF`
  (default `ghcr.io/mxc-admin/hermx-trader:latest`). Requires a prior `docker login`.
- `--no-deploy` — build (and optionally `--push`) only; do **not** restart the stack.
- `--tag TAG` — local image tag to build (default `hermx-trader:local`).
- `--deploy-dir DIR` — compose/deploy dir holding `.env` + `engine-config.json` +
  `strategies/` (default: this repo root). Use to build here but deploy into a
  separate install (e.g. `/opt/hermx`).
- `-h`, `--help` — usage.

Env overrides: `HERMX_LOCAL_TAG`, `HERMX_PUSH_REF`, `HERMX_INSTALL_DIR` (deploy dir).

### Common invocations

```bash
# Preview what would happen (recommended first run):
bash scripts/docker-rebuild.sh --dry-run

# Rebuild from local source and redeploy the local stack:
bash scripts/docker-rebuild.sh

# Clean rebuild (bust the Docker layer cache), redeploy:
bash scripts/docker-rebuild.sh --no-cache

# Build only, don't touch the running stack:
bash scripts/docker-rebuild.sh --no-deploy

# Build and publish to GHCR (needs `docker login ghcr.io` first):
bash scripts/docker-rebuild.sh --push
bash scripts/docker-rebuild.sh --push ghcr.io/mxc-admin/hermx-trader:v1.2.3 --no-deploy

# Build here, deploy into a separate install dir:
bash scripts/docker-rebuild.sh --deploy-dir /opt/hermx
```

## What the script does

1. **Prerequisites** — confirms Docker + compose v2, a `Dockerfile` in the repo,
   and warns if `dashboard-ui/package-lock.json` is missing (`npm ci` would fail).
2. **Safety preview** — reads the deploy dir's `.env`, loudly warns if
   `HERMX_LIVE_TRADING=true`, and warns if the working tree has uncommitted changes
   (so it's clear the build bakes them, not just clean `HEAD`).
3. **Build** — `docker build -t hermx-trader:local .` (multi-stage: `ui-builder`
   Node stage emits `dashboard-ui/out`, then the Python runtime bakes it in). The
   build runs **before** any restart — a failed build leaves the running stack
   untouched and exits non-zero.
4. **Push (opt-in)** — with `--push`, tags the local build as the registry `REF`
   and `docker push`es it (extra confirmation first; needs a prior `docker login`).
5. **Sync Docker setup** — only when `--deploy-dir` differs from the repo: keeps the
   deploy dir's `docker-compose.yml` / `docker-compose.host.yml` in lockstep with
   the repo's (shows a diff and asks before overwriting). `.env`,
   `engine-config.json`, and `strategies/` are never touched. When the deploy dir is
   the repo root this is a no-op (the compose files already are the source).
6. **Deploy** — `HERMX_IMAGE=hermx-trader:local docker compose up -d`. The shell env
   overrides the `.env` interpolation, so compose uses the freshly built local image
   instead of the GHCR default; the changed image ID makes `up -d` recreate the
   containers.
7. **Health checks** — polls `http://127.0.0.1:8891/health` (receiver) and
   `:8098/health` (dashboard) for up to 60s each.
8. **Verify / rollback** — prints `ps` and the image id. On health failure it prints
   rollback options and exits non-zero.

## Safety notes

- **Live trading.** If `HERMX_LIVE_TRADING=true` on the deploy target, the script
  requires an explicit extra confirmation before redeploying (unless `--force` or
  `--no-deploy`). Redeploying bounces the receiver — run only during a safe window.
- **Build-before-deploy.** The image is built first; only after a successful build
  is the running stack recreated. A broken build never disrupts a running deploy.
- **Volume persistence.** `hermx-data` (append-only ledgers) and `hermx-state`
  (mutable snapshots incl. `control-state.json`) are named volumes and survive
  `up -d`. This workflow never runs `down -v`.
- **Operator files preserved.** `.env`, `engine-config.json`, and your
  `strategies/*.json` edits are never overwritten. The only file this workflow will
  overwrite (with a diff + confirmation) is a **separate** deploy dir's compose file.
- **`--push` publishes.** It sends the image to a **remote registry** — an outward
  action. It asks first and needs a prior `docker login`. Default target is
  `ghcr.io/mxc-admin/hermx-trader:latest`; override with `--push <REF>`.
- **`--dry-run`** previews the whole plan (build/push/deploy commands, live warning,
  dirty-tree warning) and makes zero changes.

## Verification — what healthy output looks like

```
=== PHASE 2: Build image from local source ===
  docker build -t hermx-trader:local /path/to/hermx
  ✓ Built hermx-trader:local (a1b2c3...)

=== PHASE 5: Deploy local stack ===
  ✓ Compose up -d issued against hermx-trader:local.

=== PHASE 6: Health checks (up to 60s) ===
  ✓ Receiver healthy (127.0.0.1:8891) after 4s
  ✓ Dashboard healthy (127.0.0.1:8098) after 6s

=== PHASE 7: Verify ===
  Image in use: hermx-trader:local (a1b2c3...)

  ✓ Rebuild + deploy complete — both services healthy on the new image.
```

Exit code `0` means the build succeeded and both health checks passed. Non-zero
means the build failed (stack untouched), a health check failed (read the
`ROLLBACK GUIDANCE` block — volumes are intact; roll back with
`docker compose pull && up -d` to return to the published image), or the operator
aborted.

## Rolling back to the published image

This workflow deploys a **local** tag (`hermx-trader:local`). To return to the
GHCR-published image, just drop the `HERMX_IMAGE` override:

```bash
docker compose pull && docker compose up -d   # uses ghcr.io/... default again
```
