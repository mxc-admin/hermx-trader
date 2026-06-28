# HermX trading system image.
# Single image, two entrypoints (receiver + dashboard) selected via CMD override.
FROM python:3.11-slim

# curl is used by the container HEALTHCHECK and by operators for /health probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the layer caches across source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code and operator-facing assets. Secrets (.env) are NOT copied;
# they are injected at runtime via env_file / bind mount (see docker-compose.yml).
COPY src/ ./src/
COPY config/ ./config/
COPY schemas/ ./schemas/
COPY strategies/ ./strategies/
COPY skills/ ./skills/
COPY setup/ ./setup/
COPY docs/ ./docs/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/

# Receiver webhook port (8891) and clean dashboard port (8098).
EXPOSE 8891 8098

# Probe the receiver's /health endpoint. The receiver binds 127.0.0.1, so this
# works as-is under network_mode: host (and within the container otherwise).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8891/health || exit 1

# Default entrypoint: the webhook receiver. The dashboard service overrides CMD.
CMD ["python", "src/webhook_receiver.py"]
