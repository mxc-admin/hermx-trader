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
