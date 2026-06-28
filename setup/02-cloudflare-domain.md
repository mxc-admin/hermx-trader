# 02 Public Ingress

Goal: give TradingView a stable HTTPS URL to reach the local webhook receiver.

## Option A — Tailscale Funnel (recommended, no domain needed)

Free, stable URL, no domain purchase. Works on macOS and any Linux VPS.

```bash
# macOS
brew install tailscale
sudo brew services start tailscale
tailscale up --hostname=hermx
tailscale funnel --bg 8891
```

```bash
# Ubuntu/Debian VPS
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=hermx
tailscale funnel --bg 8891
```

First run prints a browser login URL — sign in with a free Tailscale account and
approve the device. On first Funnel use also visit the prompted URL to enable Funnel
on your tailnet.

Stable URL (replace `<tailnet>` with your network name from `tailscale funnel status`):

```text
https://hermx.<tailnet>.ts.net/webhook
```

The `hermx` prefix is stable because `--hostname=hermx` overrides the OS hostname.
Multiple machines in the same tailnet append a suffix (`hermx-2`, etc.).

Verify:

```bash
tailscale funnel status
curl https://hermx.<tailnet>.ts.net/health
```

## Option B — Cloudflare Tunnel (requires a domain)

Use if you already own a domain and prefer Cloudflare routing.

```bash
cloudflared tunnel login
cloudflared tunnel create hermx-webhook
# define ingress in ~/.cloudflared/config.yml
cloudflared tunnel run hermx-webhook
```

Rules:

- webhook route must use HTTPS
- route must include secret validation
- dashboard route should not expose secrets
- tunnel should be monitored

