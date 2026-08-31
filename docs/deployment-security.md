# Deployment Security Hardening Guide

**Audience:** Platform engineers, SREs, and security teams deploying Modulo in
production. This guide covers configuration and verification steps beyond the
basic deployment instructions in `docs/deployment.md`.

**Prerequisite reading:**
- `docs/deployment.md` – base deployment reference
- `docs/security/secret-management.md` – key rotation, leak response
- `docs/operations/network-egress.md` – data residency verification
- `docs/system-requirements.md` – hardware and platform requirements
- `docs/configuration-reference.md` – all env vars reference
- `docs/upgrade-process.md` – upgrade and rollback procedures

---

## 1. Network Security

### 1.1 TLS Termination

Terminate TLS at the reverse proxy. Modulo ships without TLS built in – it
expects a proxy to handle it.

| Proxy | Setup |
|-------|-------|
| **Caddy** | Automatic ACME (Let's Encrypt). Reference Caddyfile in `deploy/caddy/`. |
| **nginx** | Manual cert + `ssl_certificate` directives. See `docs/deployment.md` §TLS/HTTPS. |

Cipher requirements (from `configs/nginx/ssl.conf` where shipped):

```
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
ssl_prefer_server_ciphers on;
```

Disable TLS 1.0, 1.1, and all null/export-grade ciphers. Verify with:

```bash
sslscan modulo.example.com
# or
nmap --script ssl-enum-ciphers -p 443 modulo.example.com
```

### 1.2 Firewall Rules

| Direction | Source | Destination | Port | Purpose |
|-----------|--------|-------------|------|---------|
| Inbound | Internet | Reverse proxy | 443 (TCP) | HTTPS |
| Inbound | Internal | Backend | 8000 (TCP) | API (proxy→backend) |
| Inbound | Internal | Frontend | 80 (TCP) | HTTP→HTTPS redirect |
| Inbound | Internal | Postgres | 5432 (TCP) | Database |
| Inbound | Internal | Redis | 6379 (TCP) | Task queue / rate limiter |
| Outbound | Backend | Internet | 443 (TCP) | Connectors, webhooks, SSO |

Block all other inbound ports. For VPC deployments, see
`docs/deployment/vpc-checklist.md`.

### 1.3 VPC Isolation

- Deploy Postgres and Redis in private subnets with no public IPs.
- Backend and frontend in private or public subnets based on your access model.
- The reverse proxy (Caddy/nginx/Ingress) is the only component exposed to the
  internet.
- See `docs/operations/network-egress.md` for the full egress audit.

### 1.4 Private Networking for DB/Redis

- **Postgres:** Bind to the private IP, not `0.0.0.0`. Use `listen_addresses` in
  `postgresql.conf` to restrict to the VPC subnet.
- **Redis:** Bind to the private IP with `protected-mode yes`. Set
  `requirepass` to a strong random value (stored as `REDIS_URL`).
- Both should use a separate security group / firewall rule scoped to the
  backend's subnet.

### 1.5 Ingress Hardening

- Set `proxy-read-timeout` and `proxy-send-timeout` to match pipeline duration
  (minimum 600s for long-running agents).
- Restrict `proxy-body-size` to 50 MB (default) unless file uploads require
  more.
- Enable rate limiting at the reverse proxy as defense in depth (see §5.1).

---

## 2. Authentication & Secrets

### 2.1 SECRET_KEY Requirements

| Property | Requirement |
|----------|-------------|
| Minimum length | 32 bytes (44 base64 chars) |
| Generation | `openssl rand -base64 32` |
| Storage | environment variable or secrets manager – never in code |
| Rotation | On suspicion of compromise, or quarterly for compliance |
| Impact of rotation | Invalidates all existing JWT sessions |

Modulo refuses to start if `SECRET_KEY` is absent or shorter than 32 bytes.

### 2.2 FERNET_KEY Rotation

| Property | Requirement |
|----------|-------------|
| Format | 44-char base64 (32 bytes decoded) |
| Generation | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| Rotation impact | Does NOT re-encrypt existing secrets automatically |

After rotating `FERNET_KEY`, run the credential re-encryption command:

```bash
uv run modulo rotate-credentials
```

Secret rotation procedure is documented in `docs/security/secret-management.md`
§How to Rotate Secrets.

### 2.3 Database Passwords

- Generate with `openssl rand -base64 16` (minimum 16 bytes).
- Use a separate credential per environment (dev/staging/prod).
- Store in `DATABASE_URL` as `postgresql+asyncpg://user:pass@host:5432/db`.
- Never use the Postgres superuser for the application connection – create a
  dedicated role with least-privilege permissions (see §3.3).

### 2.4 Admin User Setup

The `MODULO_USERS` environment variable seeds initial admin credentials:

```env
MODULO_USERS=admin:<bcrypt-hashed-password>
```

**Security considerations:**
- `MODULO_USERS` is evaluated only on first startup when the admin table is
  empty. Removing it after seeding does not delete the user.
- Do not commit `MODULO_USERS` to any configuration file – inject it via the
  runtime environment (Docker secret, or vault).
- Use bcrypt-hashed passwords. The format is `$2b$12$<hash>` (generate with any
  bcrypt tool, e.g. `htpasswd -bnBC 12 "" <password> | tr -d ':\n'`).
- For SSO-enabled deployments, seed a single emergency local admin and manage
  all other users via OIDC/SAML JIT provisioning.

### 2.5 Secrets Backend Selection

| Backend | When to use | Key management |
|---------|-------------|----------------|
| Fernet (default) | Single-server, no external infra | `FERNET_KEY` env var |
| HashiCorp Vault | Existing vault infra, audit logging | `VAULT_TOKEN` or AppRole |
| AWS Secrets Manager | AWS-native, automatic rotation | IAM role |

See `docs/security/secret-management.md` for detailed configuration of each.

---

## 3. Database Security

### 3.1 RLS Enforcement Verification

Row-Level Security is enabled for Postgres deployments. Verify enforcement:

```sql
-- Connect as the application user
SET session_modulo.org_id = 'other-org-uuid';
SELECT * FROM users;
-- Expected: empty set or only rows visible to the session org
```

The application sets `session_modulo.org_id` at connection pool checkout.
If RLS is bypassed, the query returns rows across organisations.

**Checklist:**
- [ ] Every table has `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;`
- [ ] Every table has a policy: `CREATE POLICY org_isolation ON <name> USING (organisation_id = current_setting('session_modulo.org_id')::uuid);`
- [ ] Run the RLS test suite: `uv run pytest tests/unit/test_rls.py -v`

**SQLite note:** RLS is not available in SQLite. `WHERE-clause` filtering is
used instead, which is application-level only – do not rely on it for
multi-tenant isolation.

### 3.2 Connection Encryption

- **In-transit:** Postgres connections must use TLS. Set `sslmode=require` in
  `DATABASE_URL`: `postgresql+asyncpg://user:pass@host:5432/db?sslmode=require`
- **Certificate validation:** Use `sslmode=verify-full` with a CA certificate
  in production to prevent MITM within the VPC.
- Redis connections should use TLS if Redis is configured with
  `tls-port` and `tls-cert-file`:
  `rediss://user:pass@host:6379/0`

### 3.3 Least-Privilege DB User

Create a dedicated role for the application – never use the Postgres superuser:

```sql
CREATE ROLE modulo_app WITH LOGIN PASSWORD '<random>';
GRANT CONNECT ON DATABASE modulo TO modulo_app;
GRANT USAGE ON SCHEMA public TO modulo_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO modulo_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO modulo_app;
-- For Alembic migrations in startup hook:
GRANT CREATE ON SCHEMA public TO modulo_app;
```

**For migration-only operations** (separate user for safety):

```sql
CREATE ROLE modulo_migrate WITH LOGIN PASSWORD '<random>';
GRANT modulo_app TO modulo_migrate;
GRANT CREATE ON SCHEMA public TO modulo_migrate;
```

Then use `modulo_migrate` in migration contexts and `modulo_app` for runtime.

### 3.4 Migration Safety

Migrations use PostgreSQL advisory locks to prevent concurrent execution:

```python
# alembic/env.py
await conn.execute(sa.text("SELECT pg_advisory_xact_lock(19910914)"))
```

This works across replicas. **The lock ID (19910914) must not conflict** with
other applications sharing the same Postgres instance.

**Safety rules:**
- Never run `alembic upgrade head` on a replica connected to a production DB
  without an explicit lock timeout.
- Always test migrations against a staging copy of production data.
- If a migration fails mid-flight, the advisory lock is released at transaction
  end – the next attempt will re-acquire it.
- See `docs/upgrade-process.md` for rollback procedures.

### 3.5 Backup Encryption

Backups are encrypted with AES-256-CBC (PBKDF2, 600K iterations). See
`docs/operations/backup.md` for the full procedure.

**Key requirements:**
- `MODULO_BACKUP_PASSPHRASE` must be at least 32 characters.
- Store the passphrase separately from the backup file (different vault, different
  location, or hardware security module).
- Verify backup integrity with `--dry-run` before relying on it for DR.

---

## 4. API Security

### 4.1 Rate Limiting Configuration

Rate limiting uses a Redis token-bucket algorithm. Configuration is per-route
in the application settings or environment:

```env
# Default rate limits (requests per window)
RATE_LIMIT_DEFAULT=100/minute
RATE_LIMIT_AUTH=20/minute      # Login/register endpoints
RATE_LIMIT_MCP=300/minute      # MCP tool invocations
RATE_LIMIT_WS_CONNECT=10/minute  # WebSocket connection requests
```

**Key derivation** is auth-aware – unauthenticated requests are keyed by IP,
authenticated requests by user ID. This prevents IP-based collisions behind NAT.

**Verify:**
```bash
# Trigger rate limit
for i in $(seq 1 120); do curl -s -o /dev/null -w "%{http_code}\n" https://modulo.example.com/healthz/ready; done
# Expected: after ~100 requests, responses return 429
```

Without Redis, rate limiting falls back to in-memory token bucket – this does
not coordinate across replicas. See `docs/deployment.md` §Scaling for details.

### 4.2 CSRF Protection

Modulo uses the **double-submit cookie** pattern:
1. Server sets a cryptographically random CSRF token as a non-HttpOnly cookie
   (JavaScript needs to read it) with `Secure` and `SameSite=Lax` attributes.
2. Client includes the same token in a custom header (`X-CSRF-Token`).
3. Server validates the header value matches the cookie value.

**Do not:**
- Disable CSRF protection for any state-changing endpoint (POST, PUT, PATCH,
  DELETE).
- Set `SameSite=None` on the CSRF cookie unless your frontend is on a different
  origin and you fully understand the trade-off.
- Remove CSRF protection for `text/plain` content type endpoints – they are
  still vulnerable to cross-origin form submissions.

### 4.3 CORS Hardening

From `docs/deployment.md` §CORS Configuration:

```env
CORS_ORIGINS=https://app.modulo.example.com,https://admin.modulo.example.com
CORS_MAX_AGE=3600
```

**Rules:**
- Never use `*` in production – startup rejects it when `debug=False`.
- List every origin explicitly. No trailing slashes.
- CORS is a browser-only control – it does not protect against non-browser
  clients. Always pair with authentication and rate limiting.

### 4.4 Security Headers Checklist

Verify the backend response includes all headers below:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' wss://modulo.example.com; base-uri 'self'; form-action 'self'
Strict-Transport-Security: max-age=31536000; includeSubDomains
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
```

**Verify:**
```bash
curl -si https://modulo.example.com/health | grep -iE "^(content-security-policy|strict-transport-security|x-frame-options|x-content-type-options|referrer-policy):"
```

### 4.5 API Key Management

Connector credentials and third-party API keys are stored encrypted at rest
using the configured secrets backend (Fernet, Vault, or AWS Secrets Manager).

- Decrypted values appear only in run-scoped context objects – never in logs,
  checkpoint blobs, OTel spans, or API responses (masked as `●●●●●`).
- API keys can be revealed to authenticated users for 30 seconds via the
  unmask endpoint, logged as an audit event.
- See `docs/security/secret-management.md` for rotation and incident response.

---

## 5. Container Security

### 5.1 Image Vulnerability Scanning

All Modulo Docker images are scanned with Trivy as part of CI. The workflow scans
the exact locally loaded image produced by the cached Buildx build, and only pushes
that image to GHCR after the critical-vulnerability gate passes:

```bash
# Manual scan
trivy image ghcr.io/farnalabs/modulo-backend:latest
trivy image ghcr.io/farnalabs/modulo-frontend:latest

# Scan with severity filter
trivy image --severity CRITICAL,HIGH ghcr.io/farnalabs/modulo-backend:latest
```

**SLAs** (from `docs/security/dependency-policy.md`):

| Severity | Fix deadline |
|----------|-------------|
| Critical | 72 hours |
| High | 7 days |
| Medium | 30 days |

### 5.2 Minimal Base Images

- Backend image: `python:3.12-slim` (Debian-based, ~120 MB).
- Frontend image: `nginx:alpine` (~25 MB) – multi-stage build, only nginx +
  compiled assets in the final layer.
- Do not replace these with `:latest` or full distroless images without
  verifying that `uv`, Python, and all native dependencies (`psycopg` C
  extension, `cryptography` Rust extensions) are available.

### 5.3 Non-Root User

Both backend and frontend containers run as a non-root user by default:

```dockerfile
# Dockerfile
RUN groupadd -r modulo && useradd -r -g modulo modulo
USER modulo
```

**Verify:**
```bash
docker run --rm ghcr.io/farnalabs/modulo-backend:latest whoami
# Expected: modulo (not root)
```

The Dockerfiles already enforce a non-root user (`USER modulo` in the final
image stage), so no runtime override is needed under Docker Compose or Fly.io.

### 5.4 Read-Only Filesystem

The backend container does not require a writable filesystem at runtime (all
state is in Postgres/Redis). Enable a read-only root filesystem in the Docker
Compose service definition:

```yaml
modulo:
  read_only: true
  tmpfs:
    - /tmp
```

The `tmpfs` mount gives the container a writable `/tmp` for file uploads and
export staging without persisting to disk. Under Fly.io, machine filesystems
are ephemeral by default – no writable volume is required.

### 5.5 Image Pinning

In production, pin the Docker image to a specific digest instead of a moving
tag. This prevents tag-mutation attacks on the registry:

```yaml
modulo:
  image: ghcr.io/farnalabs/modulo-backend@sha256:<digest>
```

For `docker compose` development, use `pull_policy: always` to pick up newly
built images.

---

## 6. Logging & Monitoring

### 6.1 Audit Log Configuration

The audit log uses cryptographic chaining – each entry includes the SHA-256 hash
of the previous entry, forming a tamper-evident chain:

```python
# Audit log entry structure
{
    "id": "uuid",
    "timestamp": "2026-06-28T12:00:00Z",
    "actor_id": "uuid",
    "action": "user.deleted",
    "resource_type": "user",
    "resource_id": "uuid",
    "organisation_id": "uuid",
    "previous_hash": "sha256-of-previous-entry",
    "hash": "sha256-of-this-entry",
}
```

**Verify chain integrity:**

`GET /api/v1/admin/audit/verify` (`audit.manage` permission required). There is
no CLI wrapper for audit verification.

**Configuration:**
- Export audit events as paginated JSON via `GET /api/v1/admin/audit/export`
  (paginate to cover the window).
- Audit events are **not** sent to OTel by default to avoid mixing
  tamper-evident logs with general telemetry.

### 6.2 OTel Exporter Setup

OpenTelemetry is disabled by default (`MODULO_TELEMETRY_ENABLED=false`). When
enabled, configure the exporter:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
MODULO_TELEMETRY_ENABLED=true
```

**Security considerations:**
- Do not send secrets or PII in span attributes. The backend explicitly
  excludes decrypted credentials and user tokens from span data.
- If the OTel collector is on a different network, use gRPC with TLS.
- See `docs/operations/network-egress.md` for telemetry egress details.

### 6.3 Log Retention

| Log type | Retention | Location |
|----------|-----------|----------|
| Application logs (stdout) | 30 days | Container stdout / log aggregator |
| Audit log | Append-only; no automated purge | Database (`audit_events` table) |
| Alembic migration log | Permanent | `alembic_version` table |
| OTel traces | 7 days (configurable in collector) | OTel collector / Prometheus |
| Nginx access logs | 30 days | Log aggregator or persistent volume |

### 6.4 Alerting on Security Events

Configure alerts for these events in your monitoring system:

| Event | Signal | Severity | Channel |
|-------|--------|----------|---------|
| Rate limit threshold crossed | 429 responses > 5% of total | Warning | Ops channel |
| Audit chain integrity failure | `audit verify` exit code != 0 | Critical | Security on-call |
| Failed login rate spike | Auth 401s > 10/min per IP | High | Security on-call |
| Migration failure | Backend crash loop on startup | Critical | Ops channel |
| CSRF token mismatch | 403 on state-changing endpoint | Medium | Ops channel |
| Container image with Critical CVE | Trivy scan output | High | Security channel |
| Unexpected outbound connection | Network policy violation | Critical | Security on-call |

---

## 7. Upgrade Procedure

### 7.1 Migration Safety

1. **Before deploying a new version**, review the Alembic migration:
   ```bash
   uv run alembic history
   uv run alembic upgrade head --sql  # preview SQL
   ```

2. **Back up the database** before upgrading:
   ```bash
   uv run scripts/backup.py --output /backups/pre-upgrade-$(date +%Y%m%d).tar.gz.enc
   ```

3. **Deploy the new image** (Docker Compose):
   ```bash
   docker compose -f docker-compose.prod.yml pull modulo
   docker compose -f docker-compose.prod.yml up -d
   ```
   For Fly.io deployments, `fly deploy` performs a rolling/bluegreen update.

4. **Verify migration completed:**
   ```bash
   docker compose -f docker-compose.prod.yml logs modulo | grep alembic
   # Expected: "Migration successful"
   ```

### 7.2 Rollback Procedure

```bash
# Docker Compose – re-tag and restart the previous image
docker compose -f docker-compose.prod.yml stop modulo
docker tag modulo-backend:old modulo-backend:latest
docker compose -f docker-compose.prod.yml up -d
```

For Fly.io, roll back by re-deploying the previous image tag via `fly deploy`
(or `fly machine update` to a pinned prior image).

**Warning:** Application rollback does NOT revert database migrations. If the
previous code version expects an older schema and a downgrade migration exists:

```bash
# Check current Alembic version
docker compose -f docker-compose.prod.yml exec modulo uv run alembic current

# Check that the target migration has a downgrade() function
docker compose -f docker-compose.prod.yml exec modulo uv run alembic downgrade --sql -1  # preview SQL

# Downgrade (use with extreme caution – data loss possible)
docker compose -f docker-compose.prod.yml exec modulo uv run alembic downgrade -1
```

⚠️ Not all migrations include a `downgrade()` function. If `alembic downgrade`
fails with `Target database is not up to date` or the migration lacks a
downgrade path, **do not force it** – write a forward-fix migration instead.

**Prefer a forward-fix over downgrading** – write a new migration that reverts
the schema change rather than using `alembic downgrade`.

See `docs/upgrade-process.md` for the full rollback guide.

### 7.3 Zero-Downtime Considerations

- **Fly.io:** The managed path performs rolling/bluegreen deployments between
  machine groups with health-checked cutover – keep at least 2 backend
  machines so traffic keeps flowing during an upgrade.
- **Readiness probe:** Backend `/healthz` must return 200 before a new
  deployment receives traffic. It checks DB connectivity and migration status.
- **Docker Compose:** Stop + start is the only update mode – schedule upgrades
  in a maintenance window.
- **WebSocket connections:** In-flight agent runs are lost on machine/container
  termination. Use the RuntimeProvider's reconnect mechanism or design
  pipelines to be idempotent at the step level.

---

## 8. Security Checklist

Use this checklist as a deployment sign-off sheet. Every item must be verified
before marking a production deployment as complete.

### Network

- [ ] TLS 1.2+ only – no TLS 1.0/1.1, no null ciphers
- [ ] Cipher suite restricted to AEAD ciphers (AES-GCM, ChaCha20-Poly1305)
- [ ] HSTS enabled with `max-age=31536000; includeSubDomains`
- [ ] Postgres bound to private IP, not `0.0.0.0`
- [ ] Redis bound to private IP with `protected-mode yes` and `requirepass`
- [ ] Firewall blocks all inbound ports except 443 (HTTPS)
- [ ] VPC egress restricted to known endpoints (verify with `docs/deployment/vpc-checklist.md`)
- [ ] Caddy ACME configured with Let's Encrypt production issuer

### Authentication & Secrets

- [ ] `SECRET_KEY` is 32+ random bytes – not a default or placeholder
- [ ] `FERNET_KEY` is a valid 44-char base64 Fernet key
- [ ] `DATABASE_URL` uses a least-privilege role, not the Postgres superuser
- [ ] `MODULO_USERS` uses bcrypt-hashed passwords (or SSO is the primary auth)
- [ ] No secrets committed to git (gitleaks CI pass)
- [ ] Secrets are injected via environment, not hardcoded in any file
- [ ] `MODULO_ADMIN_SECRET` (for migration CLI) is rotated separately from `SECRET_KEY`
- [ ] Fernet/Vault/AWS Secrets Manager backend is correctly configured and tested

### Database

- [ ] RLS is enabled on all tenant-scoped tables
- [ ] RLS test suite passes: `uv run pytest tests/unit/test_rls.py -v`
- [ ] Postgres connections use `sslmode=require` (or `verify-full`)
- [ ] Alembic advisory lock ID does not conflict with other applications on the same Postgres instance
- [ ] Backup encryption passphrase is 32+ characters and stored separately
- [ ] Backup integrity verified: `uv run scripts/restore.py --input <backup> --dry-run`

### API Security

- [ ] `CORS_ORIGINS` lists exact production origins – no wildcards, no trailing slashes
- [ ] Rate limits configured and verified (expected 429 after threshold)
- [ ] CSRF protection enabled on all state-changing endpoints
- [ ] Security headers present in response: CSP, HSTS, XFO, X-Content-Type-Options, Referrer-Policy
- [ ] CSRF cookie is `SameSite=Lax` (or `Strict`) – never `None` in production

### Containers

- [ ] Trivy scan shows zero critical/high CVEs on deployed image tags
- [ ] Container runs as non-root user (`whoami` != `root`)
- [ ] Read-only root filesystem enabled for backend containers
- [ ] Image pinned by digest, not tag (production)
- [ ] Image pull policy is `Always` (or `IfNotPresent` only when digest-pinned)

### Logging & Monitoring

- [ ] Audit log chain integrity verified: `GET /api/v1/admin/audit/verify`
- [ ] Security alerts configured (rate limit threshold, failed login spike, audit chain failure)
- [ ] Log retention set to at least 30 days for application logs; audit log is append-only (no automated purge)
- [ ] OTel exporter (if enabled) is correctly scoped – no secrets in spans
- [ ] OTel telemetry is disabled if data residency prohibits egress

### Upgrade

- [ ] Pre-upgrade backup created and verified
- [ ] Alembic migration SQL previewed before deployment
- [ ] Rolling/bluegreen deploy strategy used for Fly.io (or pinned digest for Docker Compose)
- [ ] Rollback tested in staging before production
- [ ] At least 2 backend machines running on Fly.io (zero-downtime requirement)

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Deployment basics | `docs/deployment.md` |
| VPC isolation checklist | `docs/deployment/vpc-checklist.md` |
| Secret management | `docs/security/secret-management.md` |
| Input validation | `docs/security/input-validation-guide.md` |
| Dependency policy | `docs/security/dependency-policy.md` |
| Penetration test plan | `docs/security/penetration-test-plan.md` |
| Backup & restore | `docs/operations/backup.md` |
| Self-hosted admin operations | `docs/operations/self-hosted-admin.md` |
| Network egress audit | `docs/operations/network-egress.md` |
| System requirements | `docs/system-requirements.md` |
| Configuration reference | `docs/configuration-reference.md` |
| Upgrade process | `docs/upgrade-process.md` |
| Public launch checklist | `docs/public-launch-checklist.md` |
