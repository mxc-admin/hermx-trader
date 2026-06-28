#!/usr/bin/env bash
# install-services.sh — install and enable HermX systemd services on a Linux VPS
# Run as root from the repo root: sudo bash deploy/install-services.sh
set -euo pipefail

INSTALL_DIR="/opt/hermx"
SERVICE_USER="hermx"

echo "==> Creating service user (if needed)"
# Same definition as INSTALL.md Phase 1: home is the install dir, not auto-populated.
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Setting ownership on $INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"

echo "==> Locking down .env permissions"
chmod 600 "$INSTALL_DIR/.env"

echo "==> Copying service files"
cp deploy/hermx-receiver.service  /etc/systemd/system/
cp deploy/hermx-dashboard.service /etc/systemd/system/

echo "==> Reloading systemd"
systemctl daemon-reload

echo "==> Enabling + starting services"
systemctl enable --now hermx-receiver
systemctl enable --now hermx-dashboard

echo ""
echo "==> Status"
systemctl status hermx-receiver  --no-pager -l | head -15
systemctl status hermx-dashboard --no-pager -l | head -15

echo ""
echo "Done. Logs: journalctl -u hermx-receiver -f"
