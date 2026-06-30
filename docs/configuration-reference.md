# Configuration Reference

Complete reference for all environment variables supported by Modulo. Variables are grouped by function.

---

## Required

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://modulo:pass@localhost:5434/modulo` |
| `SECRET_KEY` | JWT signing key, minimum 32 bytes | `openssl rand -base64 32` |
| `FERNET_KEY` | Fernet encryption key, 44-char base64 | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

The application refuses to start if any required variable is absent or invalid.

---

## Database

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | **Yes** | — | `postgresql+asyncpg://user:pass@host:port/db` |
| `MODULO_DB` | No | `postgres` | Database backend: `postgres` or `sqlite` |

`MODULO_DB=sqlite` switches to SQLite for local development. See [`docs/system-requirements.md`](./system-requirements.md) for SQLite limitations.

---

## Authentication & Secrets

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | **Yes** | — | JWT signing key, minimum 32 bytes (256 bits) |
| `FERNET_KEY` | **Yes** | — | Fernet encryption key, exactly 44 base64-encoded bytes |
| `MODULO_USERS` | For seeding | — | Comma-separated `user:pass` pairs for initial user seed |
| `MODULO_ADMIN_SECRET` | No | — | Shared secret for `modulo-migrate` CLI auth bypass |
| `MODULO_ADMIN_TOKEN` | No | — | Admin token for `modulo-migrate` CLI (alternative to env) |
| `MODULO_SECRETS_BACKEND` | No | `fernet` | Secrets backend: `fernet`, `vault`, or `aws` |

See [`docs/deployment-security.md`](./deployment-security.md) for key rotation procedures and [`docs/security/secret-management.md`](./security/secret-management.md) for backend-specific configuration.

---

## Server & Networking

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_PUBLIC_URL` | For SSO | `http://localhost:8000` | Public-facing URL for OAuth redirects, webhook callbacks, email links |
| `CORS_ORIGINS` | No | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `CORS_MAX_AGE` | No | `600` | Preflight cache max-age in seconds |
| `MODULO_LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Redis & Task Queue

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | For multi-replica | — | `redis://host:port/db` for Celery broker and rate limiting |

Redis is optional for single-replica deployments but required for:
- Multi-replica coordination (cron triggers, polling, task queues)
- Distributed rate limiting
- WebSocket event broker (Redis pub/sub)

Without Redis: in-process scheduler, in-memory rate limiting, in-memory event broker.

---

## Observability

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_TELEMETRY_ENABLED` | No | `false` | Enable OpenTelemetry instrumentation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | — | OTel gRPC exporter endpoint (e.g. `http://otel-collector:4317`) |
| `MODULO_OTEL_SERVICE_NAME` | No | `modulo` | OTel service name attribute |

Telemetry is opt-in. With default settings, Modulo makes **zero** external network calls. See [`docs/operations/network-egress.md`](./operations/network-egress.md).

---

## Rate Limiting

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RATE_LIMIT_DEFAULT` | No | `100/minute` | Default rate limit per user/IP |
| `RATE_LIMIT_AUTH` | No | `20/minute` | Login/register endpoints |
| `RATE_LIMIT_MCP` | No | `300/minute` | MCP tool invocations |
| `RATE_LIMIT_WS_CONNECT` | No | `10/minute` | WebSocket connection requests |

Rate limiting uses a Redis token-bucket algorithm. Falls back to in-memory without Redis (single-process only).

---

## Runtime & Sandbox

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_E2B_API_KEY` | For E2B | — | E2B sandbox API key for runtime provider |
| `MODULO_MAX_LOCAL_CONCURRENCY` | No | `2` | Max concurrent local agents (LocalRuntimeProvider) |
| `OLLAMA_BASE_URL` | For Ollama | `http://localhost:11434` | Ollama server URL for local model backends |

---

## Feature Flags

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_DEMO_MODE` | No | `false` | Enables demo pipeline with StubModelBackend |
| `MODULO_PLUGIN_DISCOVERY` | No | `true` | Enable automatic plugin discovery |

---

## Backup & Recovery

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_BACKUP_PASSPHRASE` | For encryption | — | AES-256-CBC backup encryption passphrase (min 32 chars) |
| `MODULO_AUDIT_RETENTION_DAYS` | No | `365` | Audit log retention period in days |

See [`docs/operations/backup.md`](./operations/backup.md) for backup configuration.

---

## Gunicorn / Workers

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GUNICORN_WORKERS` | No | `4` | Number of Gunicorn worker processes |

---

## TLS / Connection Security

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VAULT_ADDR` | For Vault | — | HashiCorp Vault server address |
| `VAULT_TOKEN` | For Vault | — | Vault authentication token |
| `VAULT_ROLE_ID` | For Vault | — | Vault AppRole role ID |
| `VAULT_SECRET_ID` | For Vault | — | Vault AppRole secret ID |

See [`docs/security/secret-management.md`](./security/secret-management.md) for Vault and AWS Secrets Manager configuration.

---

## Migration CLI

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_ADMIN_SECRET` | For CLI | — | Shared secret for `modulo-migrate` CLI tool |
| `MODULO_ADMIN_TOKEN` | For CLI | — | Admin JWT for `modulo-migrate` CLI tool |

---

## Full Example (.env)

```env
# Required
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
SECRET_KEY=<random-64-char-string>
FERNET_KEY=<random-44-char-base64>

# Server
MODULO_PUBLIC_URL=https://modulo.example.com
CORS_ORIGINS=https://app.modulo.example.com,https://admin.modulo.example.com
CORS_MAX_AGE=3600
MODULO_LOG_LEVEL=INFO

# Redis (required for multi-replica)
REDIS_URL=redis://redis:6379/0

# Observability (optional)
MODULO_TELEMETRY_ENABLED=false

# Rate limiting
RATE_LIMIT_DEFAULT=100/minute
RATE_LIMIT_AUTH=20/minute
```

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| System requirements | [`docs/system-requirements.md`](./system-requirements.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment security | [`docs/deployment-security.md`](./deployment-security.md) |
| Secret management | [`docs/security/secret-management.md`](./security/secret-management.md) |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Startup troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) §1 |
