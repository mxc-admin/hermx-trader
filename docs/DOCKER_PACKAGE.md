# HermX — Docker Compose Package Deployment Plan

**Status:** validated against actual code on 2026-06-29. Every claim below traces to a
file:line reference. Confidence: 9.5/10. The remaining 0.5 is the two items in
§7 "Unknowns" that need a one-time manual confirmation (GHCR org visibility,
multi-arch need).

**Goal:** ship HermX as a published Docker image + a one-line installer so a user on
a fresh Ubuntu VPS can run it **without cloning the repo**. State (jsonl ledgers +
JSON snapshots) survives image updates via named volumes. Strategies and config are
host-editable post-install.

---

## 0. Ground truth (what the code actually does)

| Claim | Verified at | Verdict |
|---|---|---|
| `shadow-config.json` is dead | `src/dashboard_core.py:107-111` (`shadow_config()` returns `{}`) | **DEAD** — no-op kept for callers |
| Live config is `engine-config.json` | `src/webhook_receiver.py:285-286`, `src/webhook/config.py:52-76` | **TRUE** |
| App survives missing `engine-config.json` | `webhook/config.py:66-67` returns full defaults | **TRUE** |
| Dashboard UI not in image | Dockerfile has no `dashboard-ui` COPY; `dashboard.py:2335` `STATIC_DIR=/app/dashboard-ui/out` | **TRUE — UI broken in Docker today** |
| `install.sh` still uses `shadow-config.json` | `grep shadow install.sh` → **none**; pre-flight checks `engine-config.json` (`install.sh:459`) | **BRIEF IS STALE** — already fixed |
| Dockerfile copies `engine-config.json` | working-tree diff: `COPY engine-config.json /app/engine-config.json` (line 35) | **BRIEF IS STALE** — already changed, **but the change is buggy (see §1.A)** |
| Runtime profiles differ per venue | `config/runtime.demo.json` vs `runtime.binance.demo.json` — both only carry `strategy_engine`; identical to defaults | Profiles are **venue-agnostic**; venue lives in `.env` |
| Dashboard is read-only | `dashboard.py:2497-2518`, `_save_control_state:214-231` — it **writes** `control-state.json` | **FALSE — dashboard is a writer**; current compose breaks this (§1.C) |

**Path resolution (container, `/app` = `ROOT`):**

- `webhook_receiver.py:126` `ROOT = SHADOW_ROOT or parents[1]` → `/app`
- `webhook_receiver.py:127` `LOG_DIR = ROOT/"logs"` → `/app/logs` (ledgers; `hermx-data` volume)
- `webhook_receiver.py:132` `DATA_DIR = HERMX_DATA_DIR or ROOT` → compose sets `/app/data` (`hermx-state` volume)
- `webhook_receiver.py:288` `STRATEGIES_DIR = ROOT/"strategies"` → `/app/strategies`
- `dashboard.py:38-39` `REPO_ROOT = parents[1]`, `ROOT = SHADOW_ROOT or REPO_ROOT` → `/app`
- `dashboard.py:52` `STRATEGIES_DIR = ROOT/"strategies"` → `/app/strategies`
- `dashboard.py:57` `CONTROL_STATE_FILE = HERMX_DATA_DIR or ROOT /"control-state.json"`
- `dashboard.py:2335` `STATIC_DIR = REPO_ROOT/"dashboard-ui"/"out"` → `/app/dashboard-ui/out`
- ports: receiver `8891` (`webhook_receiver.py:91`), dashboard `8098` (`dashboard.py:41`)

---

## A. Exact file changes

### A.1 `Dockerfile` — multi-stage: build UI, bake safe baseline config, drop dead file

**Problems with the current (working-tree) Dockerfile:**

1. `COPY engine-config.json /app/engine-config.json` (line 35) — `engine-config.json`
   is **gitignored** (`.gitignore:7`). A fresh clone / CI checkout does **not** contain
   it, so `docker build` fails with `COPY failed: ... no such file or directory`.
2. `dashboard-ui/out` is never copied → dashboard serves legacy HTML, not the React SPA.

**Replace the entire `Dockerfile` with:**

```dockerfile
# HermX trading system image — multi-stage.
#   Stage 1 (ui-builder): build the Next.js static export (dashboard-ui/out).
#   Stage 2 (runtime):     python app + baked UI, two entrypoints (receiver/dashboard).
# Single image; the service is selected via CMD override in docker-compose.yml.

# ---- Stage 1: build the dashboard SPA ------------------------------------
FROM node:20-slim AS ui-builder
WORKDIR /ui
# Lockfile-first so npm ci caches across source-only changes.
COPY dashboard-ui/package.json dashboard-ui/package-lock.json ./
RUN npm ci
# Source needed for `next build` (output: 'export' -> ./out).
COPY dashboard-ui/ ./
RUN npm run build   # next.config.ts has output:'export' -> emits /ui/out

# ---- Stage 2: python runtime --------------------------------------------
FROM python:3.11-slim

# curl is used by the container HEALTHCHECK and by operators for /health probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root runtime user (fixed uid/gid so host-side volume chown is predictable;
# see docs migration note about chown -R 10001:10001 on existing hermx-data).
RUN groupadd --gid 10001 hermx \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin hermx

# Install Python deps first so the layer caches across source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code and operator-facing assets. Secrets (.env) are NOT copied;
# they are injected at runtime via env_file (see docker-compose.yml).
COPY src/ ./src/
COPY config/ ./config/
COPY schemas/ ./schemas/
COPY strategies/ ./strategies/
COPY skills/ ./skills/
COPY setup/ ./setup/
COPY docs/ ./docs/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/

# Package-install seed files: extracted on the host by scripts/install-docker.sh
# (the user never clones the repo). Baked here so the image is the single source.
COPY docker-compose.yml docker-compose.host.yml ./

# Built dashboard SPA from stage 1. dashboard.py serves /app/dashboard-ui/out when
# present (dashboard.py:2335), otherwise it falls back to legacy server-rendered HTML.
COPY --from=ui-builder /ui/out /app/dashboard-ui/out

# Baked baseline engine config. We copy the TRACKED, venue-agnostic demo profile
# (config/runtime.demo.json is identical to webhook/config.py's defaults) rather than
# the gitignored engine-config.json, so clean/CI builds never fail. Compose may
# bind-mount the operator's engine-config.json:ro over this for overrides.
COPY config/runtime.demo.json /app/engine-config.json

# Pre-create the mutable mount points and hand them (and the app tree) to the
# non-root user so the named volumes mount writable for receiver state + logs.
RUN mkdir -p /app/data /app/logs \
    && chown -R hermx:hermx /app

# Receiver webhook port (8891) and clean dashboard port (8098).
EXPOSE 8891 8098

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8891/health || exit 1

USER hermx

# Default entrypoint: the webhook receiver. The dashboard service overrides CMD.
CMD ["python", "src/webhook_receiver.py"]
```

**Why each change:**

- **`COPY config/runtime.demo.json /app/engine-config.json`** instead of
  `COPY engine-config.json` — fixes the clean-build failure (gitignored source). The
  baked content is byte-identical in meaning to the code defaults (`webhook/config.py:54-65`),
  so the image is fully runnable standalone and is **safe** to bake.
- **`COPY --from=ui-builder /ui/out /app/dashboard-ui/out`** — fixes the broken React UI.
- **`COPY docker-compose.yml docker-compose.host.yml ./`** — lets the installer extract
  version-matched compose files from the image (single source of truth; no curl drift).
- **`shadow-config.json` COPY removed** — dead file (`dashboard_core.py:108`).

**Gotchas:**

- The `ui-builder` stage needs `dashboard-ui/package-lock.json` (present, 81 KB) for
  `npm ci`. If absent, switch to `npm install`.
- `next build` needs Node ≥20 (`dashboard-ui/package.json` `engines.node`). `node:20-slim` satisfies it.
- The build context must NOT carry the host's stale `dashboard-ui/out` / `.next` /
  `node_modules` into stage 1 — handled by `.dockerignore` (A.2).

### A.2 `.dockerignore` — exclude UI build artifacts so stage 1 rebuilds clean

**Current** (`.dockerignore`) has bare `node_modules` (matches `dashboard-ui/node_modules`)
but does **not** exclude `dashboard-ui/.next` or `dashboard-ui/out`. Replace with:

```gitignore
# Secrets — never bake into the image.
.env
.env.*

# Local virtualenv / caches
.venv
__pycache__
*.pyc
.pytest_cache
*.egg-info
.ruff_cache

# VCS + dev artifacts
.git
.gitignore
tests/
*.log

# Node / Next.js — stage 1 reinstalls + rebuilds from lockfile + source only.
node_modules
dashboard-ui/node_modules
dashboard-ui/.next
dashboard-ui/out
dashboard-ui/tsconfig.tsbuildinfo

# Runtime state (persisted via named volumes, never the image)
/data
logs/
runtime/
exports/

# Operator-generated, host-only (never baked)
WEBHOOK_URL.txt
DASHBOARD_URL.txt
ENABLED_STRATEGIES.txt
HERMX_SECRET.txt
control-state.json
latest.json
seen-signals.json
paper-state.json
```

**Why:** excluding `dashboard-ui/out`/`.next` forces stage 1 to produce a fresh,
reproducible export rather than copying whatever the developer last built locally.
Excluding the host state files keeps stale snapshots out of the build context.

**Gotcha:** do **not** exclude `config/`, `strategies/`, `scripts/`, `docker-compose*.yml`
— the runtime stage and the seed step depend on them.

### A.3 `docker-compose.yml` — published image, fix dashboard state, keep tailscale

The working tree already migrated the bind-mounts shadow→engine. Two further changes:
**(1)** switch `build:` → `image:` (package model — no repo on host); **(2)** fix the
dashboard's broken `control-state.json` write path. Full corrected file:

```yaml
# HermX — two services from one published image, plus a Tailscale sidecar.
#   receiver  -> src/webhook_receiver.py  (TradingView webhook intake + execution)
#   dashboard -> src/dashboard.py         (operator dashboard; also writes mode overrides)
#   tailscale -> public https Funnel -> receiver:8891; tailnet-only serve -> dashboard:8098
#
# Package model: the image is pulled from GHCR, not built locally. Bind-mounts point at
# files the installer seeded into the install dir (engine-config.json, strategies/,
# config/tailscale/serve.json). Override the tag with `HERMX_IMAGE` in .env if needed.

services:
  receiver:
    image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}
    command: ["python", "src/webhook_receiver.py"]
    restart: always
    env_file: .env
    environment:
      - HERMX_BIND_HOST=0.0.0.0
      - HERMX_DATA_DIR=/app/data
    ports:
      - "127.0.0.1:8891:8891"
    volumes:
      - ./engine-config.json:/app/engine-config.json:ro
      - ./strategies:/app/strategies:ro
      - hermx-data:/app/logs            # append-only ledgers (rw)
      - hermx-state:/app/data           # mutable snapshots (rw)
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8891/health"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3

  dashboard:
    image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}
    command: ["python", "src/dashboard.py"]
    restart: always
    env_file: .env
    environment:
      - HERMX_BIND_HOST=0.0.0.0
      # MUST match the receiver so control-state.json overrides write to the shared
      # volume and the receiver actually sees them (dashboard.py:57 + 2497-2518).
      - HERMX_DATA_DIR=/app/data
    ports:
      - "127.0.0.1:8098:8098"
    depends_on:
      - receiver
    read_only: true            # root fs read-only; mounted volumes below stay writable
    cap_drop:
      - ALL
    tmpfs:
      - /tmp
    volumes:
      - ./engine-config.json:/app/engine-config.json:ro
      - ./strategies:/app/strategies:ro
      - hermx-data:/app/logs:ro         # reads receiver's ledgers (ro)
      - hermx-state:/app/data           # rw: dashboard writes control-state.json here
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8098/health"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3

  tailscale:
    image: tailscale/tailscale:latest
    hostname: hermx
    restart: always
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_STATE_DIR=${TS_STATE_DIR:-/var/lib/tailscale}
      - TS_USERSPACE=true
      - TS_SERVE_CONFIG=/config/serve.json
    volumes:
      - tailscale-state:/var/lib/tailscale
      - ./config/tailscale/serve.json:/config/serve.json:ro

volumes:
  hermx-data:
  hermx-state:
  tailscale-state:
```

**Changes vs current working tree, and why:**

1. `build: .` removed, `image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}`
   — the package user has no repo/build context. `HERMX_IMAGE` override lets them pin a
   tag or use a private registry.
2. **Dashboard now gets `HERMX_DATA_DIR=/app/data` + `hermx-state:/app/data` (rw).**
   This is the fix for the broken mode-toggle: without it, `_save_control_state`
   (`dashboard.py:214`) writes to `/app/control-state.json` on a `read_only` layer
   (crashes) and, even if it didn't, the receiver (`HERMX_DATA_DIR=/app/data`) would
   never read it. `read_only: true` is preserved — a named volume mounted **without**
   `:ro` stays writable under a read-only root fs, and `_save_control_state` writes its
   tempfile into `dir=/app/data` (`dashboard.py:219`), not `/tmp`, so the `tmpfs:/tmp`
   is irrelevant to it. Concurrent receiver+dashboard writes are last-writer-wins via
   atomic `os.replace` — explicitly the documented design (`dashboard.py:216-218`).
3. `shadow-config.json` bind-mount — already removed in working tree; stays removed.

**Gotcha:** the `image:` tag pins `mxc-admin/hermx-trader`. If the GHCR package name
differs, set `HERMX_IMAGE` in `.env`. Confirm the package path in §7.

### A.4 `docker-compose.host.yml` — host-networking fallback, same two fixes

Still useful (Linux hosts already running Tailscale, no sidecar). Apply the same
`build:`→`image:` and dashboard-state fixes; it currently mounts only `hermx-data` and
sets no `HERMX_DATA_DIR`, so its dashboard write path is broken too, and it still mounts
`./.env` (fine) but bind-mounts no `engine-config.json` (falls back to baked baseline — OK).

Replace `build: .` + `image: hermx:latest` in **both** services with:

```yaml
    image: ${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}
```

Add to **both** services' `environment:` (host.yml currently has none):

```yaml
    environment:
      - HERMX_DATA_DIR=/app/data
```

Add a shared state volume. Receiver `volumes:` becomes:

```yaml
    volumes:
      - ./.env:/app/.env:ro
      - ./engine-config.json:/app/engine-config.json:ro   # NEW: honor host overrides
      - ./strategies:/app/strategies                       # rw, operator-editable
      - hermx-data:/app/logs
      - hermx-state:/app/data                              # NEW
```

Dashboard `volumes:`:

```yaml
    volumes:
      - ./.env:/app/.env:ro
      - hermx-data:/app/logs:ro
      - hermx-state:/app/data                              # NEW: shared control-state
```

And declare the new volume:

```yaml
volumes:
  hermx-data:
  hermx-state:
```

**Why:** parity with the bridge compose so the mode-toggle and state survival behave
identically across both deploy shapes. **Gotcha:** `network_mode: host` is Linux-only
(noted `INSTALL.md:639`); leave that caveat in docs.

### A.5 `scripts/install-docker.sh` (NEW) — one-line, repo-less installer

Self-contained: pulls the image, **seeds host files out of the image** (so the user never
clones), generates `.env` interactively (reusing the `pick_exchange`/`set_env` logic from
`install.sh:50-176`), then `docker compose up -d`. It must NOT reference `shadow-config.json`.

```bash
#!/usr/bin/env bash
# install-docker.sh — HermX repo-less Docker installer.
#   curl -fsSL https://raw.githubusercontent.com/mxc-admin/hermx-trader/main/scripts/install-docker.sh | bash
# Seeds /opt/hermx from the published image, configures .env, and starts compose.
# Safe by default: HERMX_LIVE_TRADING=false. Nothing reaches a live exchange.
set -euo pipefail

IMAGE="${HERMX_IMAGE:-ghcr.io/mxc-admin/hermx-trader:latest}"
INSTALL_DIR="${HERMX_INSTALL_DIR:-/opt/hermx}"
ENV_FILE="$INSTALL_DIR/.env"

if [[ -t 1 ]]; then
  BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"
  RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""; fi
phase(){ printf '\n%s=== %s ===%s\n' "$BOLD" "$1" "$RESET"; }
info(){ printf '  %s\n' "$1"; }
ok(){ printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn(){ printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err(){ printf '  %sx%s %s\n' "$RED" "$RESET" "$1"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ask(){ local p="$1" d="${2:-n}" r s; [[ "$d" == y ]] && s="[Y/n]" || s="[y/N]"; read -r -p "  $p $s " r||true; r="${r:-$d}"; [[ "$r" =~ ^[Yy] ]]; }

# --- set_env: copied verbatim from install.sh:50-61 (package script can't source the repo) ---
set_env(){ local key="$1"; shift; local val="$*"; local tmp; tmp="$(mktemp)";
  [[ -f "$ENV_FILE" ]] && { grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null||true; }
  printf '%s=%s\n' "$key" "$val" >> "$tmp"; mv "$tmp" "$ENV_FILE"; }

# --- pick_exchange: copied from install.sh:69-176, BUT no `cp config/... engine-config.json`
#     step (the image already baked a venue-agnostic engine-config.json; venue lives in .env). ---
EXCHANGE_TABLE=(
  "okx|OKX (recommended)|OKX_DEMO|apiKey,secret,passphrase"
  "binance|Binance|BINANCE_TESTNET|apiKey,secret"
  "bybit|Bybit|BYBIT_TESTNET|apiKey,secret"
  "kucoin|KuCoin|KUCOIN_PAPER|apiKey,secret_bare,passphrase"
  "bitget|Bitget|BITGET_DEMO|apiKey,secret,passphrase"
  "gate|Gate.io|GATE_TESTNET|apiKey,secret"
  "coinbase|Coinbase Advanced|COINBASE_SANDBOX|apiKey,secret"
  "hyperliquid|Hyperliquid|HYPERLIQUID|wallet_address,private_key"
)
pick_exchange(){
  info "Pick the exchange. Keys MUST be demo/sandbox/testnet — never a live account."; echo
  local i=1 row label
  for row in "${EXCHANGE_TABLE[@]}"; do label="$(echo "$row"|cut -d'|' -f2)"; printf '    %2d) %s\n' "$i" "$label"; i=$((i+1)); done; echo
  local choice; while true; do read -r -p "  Pick [1-${#EXCHANGE_TABLE[@]}] (default 1=OKX): " choice; choice="${choice:-1}"
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice>=1 && choice<=${#EXCHANGE_TABLE[@]} )) && break; warn "1-${#EXCHANGE_TABLE[@]}."; done
  row="${EXCHANGE_TABLE[$((choice-1))]}"
  local ex_id ex_label prefix fields
  ex_id="$(echo "$row"|cut -d'|' -f1)"; ex_label="$(echo "$row"|cut -d'|' -f2)"
  prefix="$(echo "$row"|cut -d'|' -f3)"; fields="$(echo "$row"|cut -d'|' -f4)"
  echo; ok "Selected: $ex_label (env prefix ${prefix}_*)"
  info "Enter the credentials from your ${ex_label} demo/sandbox/testnet account."; echo
  local field val; IFS=',' read -r -a field_arr <<< "$fields"
  for field in "${field_arr[@]}"; do case "$field" in
    apiKey) read -r -p "  API Key: " val; set_env "${prefix}_API_KEY" "$val";;
    secret) read -r -s -p "  Secret: " val; echo; set_env "${prefix}_SECRET_KEY" "$val";;
    secret_bare) read -r -s -p "  Secret: " val; echo; set_env "${prefix}_SECRET" "$val";;
    passphrase) read -r -s -p "  Passphrase: " val; echo; set_env "${prefix}_PASSPHRASE" "$val";;
    wallet_address) read -r -p "  Wallet Address: " val; set_env "${prefix}_WALLET_ADDRESS" "$val";;
    private_key) read -r -s -p "  Private Key: " val; echo; set_env "${prefix}_PRIVATE_KEY" "$val";;
    *) warn "Unknown field '$field' — skipping.";; esac; done
  set_env "HERMX_EXCHANGE" "$ex_id"; set_env "HERMX_CCXT_EXCHANGE" "$ex_id"
  set_env "HERMX_LIVE_TRADING" "false"
  [[ "$ex_id" == "okx" ]] && set_env "OKX_FORCE_IPV4" "1"
}

phase "PHASE 0: Prerequisites"
have docker || { err "Docker not installed. Run: curl -fsSL https://get.docker.com | sh"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose v2 required."; exit 1; }
ok "Docker + compose present."
sudo mkdir -p "$INSTALL_DIR"; sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR"
cd "$INSTALL_DIR"

phase "PHASE 1: Pull image"
docker pull "$IMAGE"; ok "Pulled $IMAGE"

phase "PHASE 2: Seed host files from the image"
# One throwaway container copies the baked assets to the host install dir. The
# bind-mounts in compose then point at THESE host copies (operator-editable).
SEED='set -e; cp -r /app/strategies/. /seed/strategies/ 2>/dev/null||true;
 cp /app/engine-config.json /seed/engine-config.json;
 mkdir -p /seed/config/tailscale; cp /app/config/tailscale/serve.json /seed/config/tailscale/serve.json;
 cp /app/docker-compose.yml /seed/docker-compose.yml; cp /app/docker-compose.host.yml /seed/docker-compose.host.yml;
 cp /app/setup/env.example /seed/env.example'
mkdir -p "$INSTALL_DIR/strategies" "$INSTALL_DIR/config/tailscale"
docker run --rm --entrypoint sh -v "$INSTALL_DIR:/seed" "$IMAGE" -c "$SEED"
ok "Seeded strategies/, engine-config.json, config/tailscale/serve.json, compose files."

phase "PHASE 3: Configure .env"
if [[ -f "$ENV_FILE" ]] && ! ask ".env exists — overwrite credentials?" "n"; then
  warn "Leaving existing .env untouched."
else
  [[ -f "$ENV_FILE" ]] || cp env.example "$ENV_FILE"
  pick_exchange
  if have openssl; then SECRET="$(openssl rand -hex 32)"; set_env "HERMX_SECRET" "$SECRET"
    ok "Generated HERMX_SECRET."; info "Secret (X-Webhook-Secret + dashboard token): $SECRET"
  else warn "openssl missing — set HERMX_SECRET in .env manually."; fi
fi

phase "PHASE 4: Tailscale auth key"
if ! grep -q '^TS_AUTHKEY=..*' "$ENV_FILE" 2>/dev/null; then
  info "Generate a reusable/ephemeral key: Tailscale admin -> Settings -> Keys."
  read -r -p "  TS_AUTHKEY (tskey-...) [blank to skip]: " k
  [[ -n "${k:-}" ]] && { set_env "TS_AUTHKEY" "$k"; ok "TS_AUTHKEY saved."; } || warn "Sidecar won't connect until TS_AUTHKEY is set."
fi
chmod 600 "$ENV_FILE"

phase "PHASE 5: Start"
docker compose up -d
sleep 5
curl -sf http://127.0.0.1:8891/health >/dev/null 2>&1 && ok "Receiver OK (127.0.0.1:8891)" || err "Receiver FAIL — docker compose logs receiver"
curl -sf http://127.0.0.1:8098/health >/dev/null 2>&1 && ok "Dashboard OK (127.0.0.1:8098)" || err "Dashboard FAIL — docker compose logs dashboard"
echo; ok "Installed to $INSTALL_DIR."
info "Webhook URL: https://hermx.<tailnet>.ts.net/webhook"
info "Edit strategies: $INSTALL_DIR/strategies/*.json then: (cd $INSTALL_DIR && docker compose restart)"
info "Update later:   (cd $INSTALL_DIR && docker compose pull && docker compose up -d)"
```

**Key design points & gotchas:**

- **Seeding solves the strategy-shadowing trap.** `./strategies:/app/strategies:ro`
  would shadow the baked strategies with an empty host dir → zero strategies → all
  alerts quarantined. The seed step copies the baked `strategies/` out of the image to
  `/opt/hermx/strategies` first, so the bind-mount overlays real files. Same for
  `engine-config.json` and `config/tailscale/serve.json` (the tailscale sidecar
  bind-mounts the latter `:ro`).
- **No `cp config/runtime.*.json engine-config.json`.** The original `install.sh`
  `pick_exchange` copies a venue profile to `engine-config.json` — but every profile is
  byte-equivalent to the baked baseline (only `strategy_engine`; venue is `.env`-driven).
  So the package script seeds the baked baseline and skips the profile copy. **No dead
  `shadow-config.json` (dead code) reference anywhere — confirmed absent.**
- `cp -r /app/strategies/. /seed/strategies/` (trailing `/.`) copies contents, not the
  dir, so re-runs are idempotent and don't nest.
- `--entrypoint sh` overrides the image's default `python` CMD for the seed container.
- Reusing `set_env`/`pick_exchange` **verbatim** keeps behavior identical to `install.sh`
  (same env keys: `HERMX_EXCHANGE`, `HERMX_CCXT_EXCHANGE`, per-venue creds, `OKX_FORCE_IPV4`).

### A.6 `.github/workflows/docker-publish.yml` (NEW) — build + push to GHCR

```yaml
name: docker-publish
on:
  push:
    branches: [main]
    tags: ['v*']
  workflow_dispatch:

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}   # mxc-admin/hermx-trader

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Metadata (tags + labels)
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=ref,event=branch
            type=semver,pattern={{version}}
            type=sha
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          # platforms: linux/amd64,linux/arm64   # enable if ARM VPS support is needed (§7)
```

**Why / gotchas:**

- The Node build runs **inside** the Dockerfile (stage 1), so CI is just `buildx` — no
  separate Node setup needed. This guarantees the published image always contains a
  fresh `dashboard-ui/out`.
- `packages: write` + `GITHUB_TOKEN` is sufficient; no PAT needed for same-org GHCR.
- After the first push, the GHCR package defaults to **private**. For a public
  `curl | bash` install with `docker pull` from an unauthenticated VPS, set the package
  visibility to **public** in GHCR settings (one-time, §7). Otherwise the installer must
  `docker login ghcr.io` first.
- `cache-from/to: gha` keeps rebuilds fast; the `npm ci` layer dominates cold builds.

### A.7 `INSTALL.md` — add the Docker **package** path

Insert a new sub-section after `### Option B — Docker (bridge networking + Tailscale)`
(before `#### Migrating an existing Docker deployment`, ~line 643), titled
**`### Option C — Docker package (no clone)`**:

```markdown
### Option C — Docker package (no repo clone)

For a fresh VPS where you want HermX without cloning the source. One command pulls the
published image, seeds an `/opt/hermx` install dir from it, walks you through `.env`, and
starts the stack:

​```bash
curl -fsSL https://raw.githubusercontent.com/mxc-admin/hermx-trader/main/scripts/install-docker.sh | bash
​```

You will be prompted for: the exchange (1–8), its demo/sandbox/testnet credentials, and a
Tailscale auth key. A `HERMX_SECRET` is generated for you. `HERMX_LIVE_TRADING=false` is
written by default — nothing reaches a live exchange.

What ends up on the VPS (`/opt/hermx/`):

​```
/opt/hermx/
├── docker-compose.yml          # extracted from the image
├── docker-compose.host.yml     # host-networking fallback
├── .env                        # your secrets (chmod 600)
├── engine-config.json          # baked baseline; edit to tune the strategy engine
├── strategies/                 # seeded from the image; edit these post-install
│   ├── btcusdt_duo_base_dev_2h.json ...
└── config/tailscale/serve.json # tailscale sidecar serve/funnel config
​```
Named volumes (survive updates): `hermx_hermx-data`, `hermx_hermx-state`, `hermx_tailscale-state`.

Verify, update, and edit:

​```bash
cd /opt/hermx
docker compose ps
curl -sf http://127.0.0.1:8891/health && echo " receiver OK"
curl -sf http://127.0.0.1:8098/health && echo " dashboard OK"

# Update to a new release (state is preserved by the named volumes):
docker compose pull && docker compose up -d

# Edit strategies / engine config, then apply:
nano strategies/btcusdt_duo_base_dev_2h.json
docker compose restart
​```
```

Also update the existing Option B command block (line 624): since the image is now
published, `docker compose up -d --build` still works **inside a clone**, but the
canonical package flow is Option C. Add one line under Option B: *"Source clones build
locally; for a repo-less install use Option C."* Update the bullet at line 595-596 to note
`engine-config.json` is the live config (it already says this — keep) and that the
dashboard now shares `hermx-state` rw for mode overrides (correct the line 598-600 bullet
that says control-state is "receiver only").

---

## B. Complete user installation process (fresh Ubuntu VPS)

**1. What they type** (after a base Ubuntu 22.04+ box with Docker installed — if Docker is
absent the script tells them to run `curl -fsSL https://get.docker.com | sh`):

```bash
curl -fsSL https://raw.githubusercontent.com/mxc-admin/hermx-trader/main/scripts/install-docker.sh | bash
```

**2. Prompts they see** (in order):

```
=== PHASE 0: Prerequisites ===
  ✓ Docker + compose present.
=== PHASE 1: Pull image ===
  ✓ Pulled ghcr.io/mxc-admin/hermx-trader:latest
=== PHASE 2: Seed host files from the image ===
  ✓ Seeded strategies/, engine-config.json, config/tailscale/serve.json, compose files.
=== PHASE 3: Configure .env ===
  Pick the exchange. Keys MUST be demo/sandbox/testnet — never a live account.
     1) OKX (recommended)
     2) Binance
     ... (8 options)
  Pick [1-8] (default 1=OKX): 1
  ✓ Selected: OKX (env prefix OKX_DEMO_*)
  API Key: ********
  Secret: (hidden)
  Passphrase: (hidden)
  ✓ Generated HERMX_SECRET.
  Secret (X-Webhook-Secret + dashboard token): a1b2c3...   <- copy this
=== PHASE 4: Tailscale auth key ===
  TS_AUTHKEY (tskey-...) [blank to skip]: tskey-auth-...
  ✓ TS_AUTHKEY saved.
=== PHASE 5: Start ===
  ✓ Receiver OK (127.0.0.1:8891)
  ✓ Dashboard OK (127.0.0.1:8098)
  ✓ Installed to /opt/hermx.
```

**3. Files on the VPS** — see the `/opt/hermx/` tree in A.7. Secrets live only in
`/opt/hermx/.env` (mode 600); never in the image.

**4. Verify it works:**

```bash
cd /opt/hermx
docker compose ps                                   # 3 services Up (receiver/dashboard healthy)
curl -sf http://127.0.0.1:8891/health && echo OK    # receiver
curl -sf http://127.0.0.1:8098/health && echo OK    # dashboard
docker compose logs --tail=30 tailscale             # node authed + funnel/serve up
# Public webhook URL: https://hermx.<tailnet>.ts.net/webhook  (paste into TradingView,
# X-Webhook-Secret = the HERMX_SECRET printed in Phase 3)
# Dashboard (tailnet-only): https://hermx.<tailnet>.ts.net:8443/dashboard/  (token = HERMX_SECRET)
```

**5. Update later:**

```bash
cd /opt/hermx
docker compose pull            # fetch the new image
docker compose up -d           # recreate containers; named volumes (state) persist
```

**6. Edit strategies post-install** (no rebuild — strategies are a host bind-mount):

```bash
cd /opt/hermx
nano strategies/btcusdt_duo_base_dev_2h.json   # change budget/leverage/execution_mode
# add a new strategy by dropping another <id>.json in strategies/
docker compose restart                          # receiver + dashboard re-read the dir
```

Toggle a strategy's mode live from the dashboard (`POST /api/control/strategy/{id}`
`{"mode":"demo"|"live"|"pause"|"clear"}`) — this now works because the dashboard writes
`control-state.json` into the shared `hermx-state` volume that the receiver reads (the
A.3 fix).

---

## C. State isolation guarantee (survives image updates)

**What is mutable state and where it lives in the container:**

| State | Container path | Volume | Writer | Reader |
|---|---|---|---|---|
| Append-only ledgers (`raw-webhooks.jsonl`, `signals.jsonl`, `order-journal*.jsonl`, `receiver.log`, …) | `/app/logs` (`webhook_receiver.py:127`) | `hermx-data` | receiver (rw) | dashboard (`:ro`) |
| Snapshots: `latest.json`, `control-state.json`, `seen-signals.json`, `paper-state.json` | `/app/data` (`webhook_receiver.py:132-133`, `dashboard.py:57`) | `hermx-state` | receiver + dashboard (rw) | both |
| Tailscale node identity | `/var/lib/tailscale` | `tailscale-state` | sidecar | sidecar |

**Why named volumes survive `docker compose pull && up -d`:**

- A Docker **named volume** is a host-managed storage object (under
  `/var/lib/docker/volumes/<project>_<name>/_data`) with a lifecycle **independent of any
  container**. `docker compose up -d` after a `pull` **recreates** the containers from the
  new image but **re-attaches the same named volumes** by name. The image layers
  (`/app/src`, `/app/dashboard-ui/out`, baked baseline config) are replaced; the volume
  data at `/app/logs` and `/app/data` is mounted **on top** of the new image's empty mount
  points and is byte-for-byte preserved. Only `docker compose down -v` (note the `-v`) or
  an explicit `docker volume rm` destroys them — neither is in the update path.
- The bind-mounts (`./engine-config.json`, `./strategies`, `./config/tailscale/serve.json`)
  are host files in `/opt/hermx`; they are untouched by image updates by definition.

**Permissions (the uid 10001 mechanism):**

- The image creates `hermx` as **uid 10001 / gid 10001** (`Dockerfile:14-15`) and
  `chown -R hermx:hermx /app` including the pre-created `/app/data` and `/app/logs`
  mount points (`Dockerfile:39-40`). Containers run `USER hermx` (`Dockerfile:51`).
- **First mount of a fresh named volume:** Docker initializes the empty volume by
  **copying the ownership/permissions of the image's mount point** (`/app/data`,
  `/app/logs` — already `10001:10001`). So a brand-new install yields volumes owned by
  10001 and the non-root process can write immediately. No manual chown needed on fresh
  installs.
- **Upgrades from an older root-written volume:** if a volume predates the non-root user
  (was written as root), re-own it once — exactly the documented migration
  (`INSTALL.md:650-654`):

  ```bash
  cd /opt/hermx && docker compose down
  docker run --rm -v hermx_hermx-data:/v alpine chown -R 10001:10001 /v
  docker run --rm -v hermx_hermx-state:/v alpine chown -R 10001:10001 /v
  docker compose up -d
  ```

- Because uid/gid are **fixed at 10001**, a re-pulled image always matches the volume's
  ownership across updates — the reason the Dockerfile pins them rather than letting
  `useradd` auto-assign.

**Recovery correctness on restart** (why this is safe with `restart: always`): the
receiver replays `raw-webhooks.jsonl` (the durable WAL on `hermx-data`) at startup and
rebuilds in-memory state; `signals.jsonl` is the dedupe backstop. State snapshots on
`hermx-state` are rebuildable from the journaled ledgers — so even an empty `hermx-state`
after a fresh deploy is correct (`INSTALL.md:658-662`). Named volumes simply make this a
no-op instead of a replay on every update.

---

## D. No dead code / corrected assumptions (audit trail)

- **`shadow-config.json`** is referenced **nowhere** in the proposed Dockerfile, compose
  files, or installer. Source proof it's dead: `dashboard_core.py:107-111`. The host file
  `shadow-config.json` and its `.bak`s can be deleted from the repo (separate cleanup; not
  required for this plan, but they should not be packaged — `.dockerignore` and the absence
  of any `COPY`/mount ensure they never enter the image).
- **`engine-config.json` baking decision:** **bake** the tracked, venue-agnostic baseline
  (`config/runtime.demo.json`) and **also bind-mount** the host copy `:ro` for operator
  overrides. Rationale: the app already tolerates a missing file
  (`webhook/config.py:66-67`); baking keeps the image self-runnable; bind-mount lets
  operators tune `enforce_alert_schema` etc. without a rebuild. We do **not**
  `COPY engine-config.json` directly — it's gitignored and would break clean builds.
- **Dashboard UI:** confirmed broken in Docker today (no `dashboard-ui` in image). Fixed
  via multi-stage build. If stage 1 ever fails, the runtime still falls back to legacy HTML
  (`dashboard.py:2331-2335`) — graceful, not fatal.
- **Dashboard is NOT read-only in behavior:** it writes `control-state.json`
  (`dashboard.py:2497-2518`). Corrected in A.3/A.4 by giving it `HERMX_DATA_DIR=/app/data`
  + a writable `hermx-state` mount, while keeping the `read_only` root fs hardening.
- **`install.sh` Docker branch is NOT stale re: shadow-config** (brief was wrong);
  `grep shadow install.sh` returns nothing and pre-flight checks `engine-config.json`
  (`install.sh:459`). No change required to `install.sh` for this plan — it remains the
  **source-install** tool; the new `scripts/install-docker.sh` is the **package** tool.

**Edge cases & suggested tests** (per dev-rules §5):

1. Clean checkout build (no host `engine-config.json`) → `docker build` succeeds (proves
   A.1 fix). *Test:* CI build job is exactly this.
2. Empty `/opt/hermx/strategies` before first `up` → all alerts quarantined. *Test:* assert
   seed step ran (`ls /opt/hermx/strategies/*.json` non-empty) before `compose up`.
3. Dashboard mode-toggle persists and is read by receiver. *Test:* `POST
   /api/control/strategy/<id> {"mode":"pause"}`; assert `/opt/hermx` → `hermx-state`
   volume `control-state.json` updated and receiver's `/api` reflects `effective_mode`.
4. `docker compose pull && up -d` preserves ledgers. *Test:* write a known
   `raw-webhooks.jsonl` line, bump the image tag, re-up, assert the line survives.
5. Re-running the installer (idempotency) doesn't duplicate strategies or clobber `.env`
   without consent. *Test:* second run with existing `.env` → "overwrite?" prompt defaults no.
6. uid 10001 can write a fresh volume. *Test:* fresh `up`, exec `id` in receiver → 10001,
   `touch /app/data/x` succeeds.
7. Tailscale sidecar missing `TS_AUTHKEY` → sidecar restarts but receiver/dashboard stay
   healthy on loopback. *Test:* `up` with blank `TS_AUTHKEY`, assert both `/health` pass.

---

## 7. Unknowns (the 0.5)

1. **GHCR package name & visibility.** Remote is `mxc-admin/hermx-trader`, so the image is
   `ghcr.io/mxc-admin/hermx-trader`. Confirm the org allows GHCR Actions publishing and set
   the package **public** for unauthenticated `docker pull` (else the installer needs
   `docker login`). One-time, in GitHub → Packages settings.
2. **ARM VPS support.** The Dockerfile is arch-neutral, but the workflow builds `amd64`
   only unless the commented `platforms:` line is enabled. Enable `linux/arm64` if targeting
   ARM VPS (Oracle Ampere, AWS Graviton). Adds build time.

Everything else is verified against code.
