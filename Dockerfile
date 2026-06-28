# HermX trading system image.
# Single image, two entrypoints (receiver + dashboard) selected via CMD override.
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

# Fallback baseline config. Compose bind-mounts the operator's shadow-config.json
# read-only over this, but the baked copy keeps the image runnable on its own.
COPY shadow-config.json /app/shadow-config.json

# Pre-create the mutable mount points and hand them (and the app tree) to the
# non-root user so the named volumes mount writable for receiver state + logs.
RUN mkdir -p /app/data /app/logs \
    && chown -R hermx:hermx /app

# Receiver webhook port (8891) and clean dashboard port (8098).
EXPOSE 8891 8098

# Probe the receiver's /health endpoint. The receiver binds HERMX_BIND_HOST
# (0.0.0.0 under bridge networking), so a loopback probe inside the container works.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8891/health || exit 1

# Drop privileges for the running processes.
USER hermx

# Default entrypoint: the webhook receiver. The dashboard service overrides CMD.
CMD ["python", "src/webhook_receiver.py"]
