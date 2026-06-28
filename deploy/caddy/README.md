# Modulo — Caddy v2 Reverse Proxy

Standalone TLS termination for environments outside Kubernetes (bare metal,
single-VM, staging, or any setup where you want Caddy handling HTTPS instead of
an ingress controller).

## Quick Start

```bash
# 1. Set environment variables (or edit Caddyfile placeholders)
export DOMAIN=modulo.example.com
export ADMIN_EMAIL=ops@example.com
export BACKEND_HOST=localhost:8000
export FRONTEND_HOST=localhost:5173

# 2. Validate config
caddy validate --config Caddyfile

# 3. Start (foreground)
caddy run --config Caddyfile

# 4. Or start (daemon)
caddy start --config Caddyfile
```

## Routes

| Path                     | Upstream      | Purpose                        |
|--------------------------|---------------|--------------------------------|
| `/healthz*`              | Backend:8000  | Liveness + readiness probes    |
| `/metrics`               | Backend:8000  | Prometheus metrics             |
| `/api/*`                 | Backend:8000  | REST API                       |
| `/mcp*`                  | Backend:8000  | MCP SSE endpoint (no buffering)|
| `/ws*`                   | Backend:8000  | WebSocket (legacy prefix)      |
| `/api/v1/runs/*/ws`      | Backend:8000  | Run event streaming (WebSocket)|
| Everything else (`/`)    | Frontend:5173 | Vue.js SPA                     |

Caddy v2 handles WebSocket upgrades transparently — no extra config needed.

## With Docker Compose

Add to your `docker-compose.yml`:

```yaml
caddy:
  image: caddy:2-alpine
  ports:
    - "80:80"
    - "443:443"
  volumes:
    - ./deploy/caddy/Caddyfile:/etc/caddy/Caddyfile
    - caddy_data:/data
    - caddy_config:/config
  environment:
    DOMAIN:        "modulo.example.com"
    ADMIN_EMAIL:   "ops@example.com"
    BACKEND_HOST:  "backend:8000"
    FRONTEND_HOST: "frontend:5173"

volumes:
  caddy_data:
  caddy_config:
```

## Security Headers Applied

| Header                         | Value                                          |
|--------------------------------|------------------------------------------------|
| `Content-Security-Policy`      | `default-src 'self'` …                         |
| `Strict-Transport-Security`    | `max-age=31536000; includeSubDomains; preload` |
| `X-Frame-Options`              | `DENY`                                         |
| `X-Content-Type-Options`       | `nosniff`                                      |
| `Referrer-Policy`              | `strict-origin-when-cross-origin`              |
| `Permissions-Policy`           | No camera, mic, geolocation                    |
| `Server`                       | Removed (no banner)                            |

## Rate Limiting

Default: 100 requests per IP per minute across the API zone. Caddy v2.7+
required. Disable by removing the `rate_limit` block for older versions.

## TLS

Caddy obtains Let's Encrypt certificates automatically on first request. No
cert-manager, no manual renewal. Set `ADMIN_EMAIL` for expiry notices.

For staging (to avoid LE rate limits during testing):

```caddy
tls {
    issuer acme {
        ca https://acme-staging-v02.api.letsencrypt.org/directory
    }
}
```

## Logging

JSON-structured logs rotate at 100 MB, keep 7 rotated files (720 hours).
Output path configurable via `LOG_FILE` env var (default:
`/var/log/caddy/modulo.access.log`).

## Verification

```bash
# Check TLS
curl -sI https://modulo.example.com/healthz | grep -i strict-transport

# Check proxying works
curl -s https://modulo.example.com/api/v1/me | head

# Check WebSocket upgrade (via run event stream)
python -c "
import asyncio, websockets
async def test():
    async with websockets.connect('wss://modulo.example.com/api/v1/runs/00000000-0000-0000-0000-000000000000/ws?token=test') as ws:
        print(await ws.recv())
asyncio.run(test())
"
```

## Comparison: Caddy vs k8s Ingress

| Concern                      | This Caddyfile                | k8s Ingress (nginx+cert-manager) |
|------------------------------|-------------------------------|-----------------------------------|
| TLS provisioning             | Automatic (Let's Encrypt)     | cert-manager + ClusterIssuer     |
| WebSocket                    | Transparent (built-in)        | Explicit Upgrade header config   |
| Rate limiting                | Built-in (v2.7+)              | nginx ConfigMap + limit_req      |
| CSP / security headers       | Single `header` block         | Ingress annotation per header    |
| State                        | File-based (JSON config)      | Kubernetes API                   |
| Best for                     | Single-VM, staging, bare metal| Multi-node, GitOps, auto-scaling |
