# 05 VPS Deploy

Goal: deploy after local/demo validation.

Steps:

1. Copy package to `/opt/hermx` on VPS.
2. Create venv and install dependencies: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. Add `.env` secrets at `/opt/hermx/.env` (owner-only: `chmod 600 .env`).
4. Configure runtime profile (`config/runtime.demo.json` or `runtime.live.json`).
5. Install Tailscale, authenticate, start Funnel:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up --hostname=hermx
   tailscale funnel --bg 8891
   ```
6. Install systemd services (keeps receiver + dashboard alive across reboots/crashes):
   ```bash
   sudo bash deploy/install-services.sh
   ```
   This creates a `hermx` service user, sets ownership, copies service files to
   `/etc/systemd/system/`, and enables + starts both units with `Restart=always`.
7. Verify services are running:
   ```bash
   systemctl status hermx-receiver hermx-dashboard
   journalctl -u hermx-receiver -f
   ```
8. Run health check: `curl http://127.0.0.1:8891/health`
9. Send synthetic alerts.
10. Confirm OKX demo behavior.

Success means:

- public webhook works
- public dashboard works
- valid alert reaches strategy log
- invalid alert is quarantined
- OKX demo order flow works

Notes:

- `tailscale funnel --bg 8891` persists across terminal sessions; survives reboots when Tailscale runs as a service.
- The `--hostname=hermx` flag makes the URL prefix predictable across any fresh install.
- Stable public URL: `https://hermx.<tailnet>.ts.net/webhook` (find `<tailnet>` via `tailscale funnel status`).

