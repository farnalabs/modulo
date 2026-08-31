# Public Launch Checklist

Production deployment readiness checklist for the Modulo V1 Core public launch. Each item must be verified before marking the launch as complete.

---

## 1. Infrastructure

- [ ] **PostgreSQL 16+** is provisioned and reachable
  - Connection string uses a least-privilege role (not superuser)
  - TLS enabled (`sslmode=require`)
- [ ] **Redis 7+** is provisioned (required for multi-replica)
  - `protected-mode yes` with `requirepass`
  - TLS enabled if Redis is configured with `tls-port`
- [ ] **Docker images** are published to ghcr.io with anonymous pull enabled
  - `ghcr.io/anomalyco/modulo-backend:latest`
  - `ghcr.io/anomalyco/modulo-frontend:latest`
- [ ] **`install.sh`** is uploaded to `https://modulo.run/install.sh`
- [ ] **DNS** is configured for the target domain
  - A/AAAA record or CNAME to the reverse proxy or load balancer
- [ ] **TLS certificate** is provisioned (Let's Encrypt via Caddy ACME or a manual certificate)
- [ ] **Reverse proxy** is configured (nginx or Caddy)
  - TLS 1.2+ only, AEAD ciphers
  - HSTS enabled (`max-age=31536000; includeSubDomains`)
  - WebSocket support (`proxy_set_header Upgrade $http_upgrade`)
  - MCP SSE support (`proxy_buffering off` for `/mcp`)
  - Ingress timeouts: `proxy-read-timeout: 600`, `proxy-send-timeout: 600`
- [ ] **Rate limiting** is configured and verified

---

## 2. Environment Configuration

- [ ] All required env vars are set (see [`docs/configuration-reference.md`](./configuration-reference.md))
- [ ] `SECRET_KEY` is 32+ random bytes, not a default or placeholder
- [ ] `FERNET_KEY` is a valid 44-char base64 Fernet key
- [ ] `DATABASE_URL` points to the production database
- [ ] `REDIS_URL` is set (for multi-replica deployments)
- [ ] `MODULO_PUBLIC_URL` matches the production domain
- [ ] `CORS_ORIGINS` lists exact production origins, with no wildcards and no trailing slashes
- [ ] `MODULO_LOG_LEVEL` is set to `INFO` (not `DEBUG`)
- [ ] `MODULO_TELEMETRY_ENABLED` is set appropriately for data residency requirements
- [ ] `MODULO_USERS` uses bcrypt-hashed passwords and is **not** committed to git

---

## 3. Security

- [ ] **TLS configuration** verified with `sslscan` or `nmap --script ssl-enum-ciphers`
- [ ] **Security headers** present in response (CSP, HSTS, XFO, X-Content-Type-Options, Referrer-Policy)
- [ ] **CORS** verified: no wildcard, correct origins, no trailing slashes
- [ ] **CSRF protection** enabled on all state-changing endpoints
- [ ] **Row-Level Security** is enabled on all tenant-scoped tables
  - RLS test suite passes: `uv run pytest tests/unit/test_rls.py -v`
- [ ] **Secrets** are injected via environment, not hardcoded in any file or committed to git
- [ ] **Container images** have zero critical/high CVEs (Trivy scan)
- [ ] **Containers** run as non-root user
- [ ] **Read-only root filesystem** enabled for backend containers
- [ ] **Firewall** blocks all inbound ports except 443 (HTTPS)
- [ ] **Network egress** is restricted to known endpoints (see [`docs/deployment/vpc-checklist.md`](./deployment/vpc-checklist.md))
- [ ] **`SECURITY.md`** exists at the repo root with contact and disclosure policy

See [`docs/deployment-security.md`](./deployment-security.md) for the full security hardening guide.

---

## 4. Observability

- [ ] **Health endpoint** (`/health` or `/healthz`) is reachable and returns 200
- [ ] **OpenTelemetry** is configured (if desired) with `MODULO_TELEMETRY_ENABLED=true`
- [ ] **Grafana** dashboards are loaded (optional):
  - Pipeline performance
  - HITL review activity
  - Cost tracking
- [ ] **Audit log chain** integrity is verified (`GET /api/v1/admin/audit/verify`; requires `audit.manage`, no CLI wrapper)
- [ ] **Log retention** is configured:
  - Application logs: 30 days minimum
  - Audit logs: append-only table (`audit_events`); not purged automatically
- [ ] **Alerts** are configured for:
  - Rate limit threshold exceeded (>5% 429s)
  - Audit chain integrity failure
  - Failed login rate spike (>10/min per IP)
  - Migration failure (crash loop)
  - Container image with Critical CVE

---

## 5. Backup & Disaster Recovery

- [ ] **Automated daily backups** are configured:
  ```cron
  0 2 * * * cd /opt/modulo/codebase && uv run scripts/backup.py --output /backups/daily/$(date +\%Y\%m\%d).tar.gz.enc
  ```
- [ ] **Backup encryption passphrase** is 32+ characters and stored separately from the backup
- [ ] **Backup retention** is configured (7 daily, 4 weekly, 12 monthly)
- [ ] **Restore procedure** is tested:
  ```bash
  uv run scripts/restore.py --input /backups/latest.tar.gz.enc --dry-run
  ```
- [ ] **Disaster recovery plan** is documented (see [`docs/operations/backup.md`](./operations/backup.md) §Disaster Recovery Guide)
- [ ] **`FERNET_KEY` backup copy** is stored in a separate vault/password manager
- [ ] **Postgres WAL archiving** is configured (optional, for point-in-time recovery)

---

## 6. Deployment Configuration

- [ ] **Docker Compose** production config is validated:
  - `docker-compose.prod.yml` (or equivalent) references correct images
  - Port mappings are correct
  - Volume mounts are configured for Postgres data persistence

---

## 7. Upgrade Readiness

- [ ] **Upgrade procedure** is documented and tested in staging
- [ ] **Rollback procedure** is documented and tested
- [ ] **Alembic migration** SQL is previewed and reviewed
- [ ] **Pre-upgrade backup** procedure is verified
- [ ] **Zero-downtime** requirements are met (2+ backend replicas)

See [`docs/upgrade-process.md`](./upgrade-process.md) for full upgrade and rollback procedures.

---

## 8. Documentation

- [ ] **Quickstart guide** is up to date ([`docs/quickstart.md`](./quickstart.md))
- [ ] **Deployment guide** covers all supported platforms
- [ ] **Configuration reference** lists all env vars ([`docs/configuration-reference.md`](./configuration-reference.md))
- [ ] **System requirements** document exists ([`docs/system-requirements.md`](./system-requirements.md))
- [ ] **Troubleshooting guide** covers common startup and operation issues
- [ ] **Architecture overview** is current ([`docs/architecture.md`](./architecture.md))
- [ ] **Security guide** covers hardening steps ([`docs/deployment-security.md`](./deployment-security.md))
- [ ] **Operations docs** cover backup, restore, admin bypass, and incident response
- [ ] **API documentation** is published (OpenAPI/Swagger UI live at `/docs`)
- [ ] **Changelog / release notes** exist for the current version

---

## 9. Testing

- [ ] **All unit tests pass**: `uv run pytest tests/unit/ -v`
- [ ] **All integration tests pass**: `uv run pytest tests/integration/ -v`
- [ ] **All BDD scenarios pass**: `uv run pytest tests/bdd/ -v`
- [ ] **RLS test suite passes**: `uv run pytest tests/unit/test_rls.py -v`
- [ ] **Load tests** within baseline targets (see [`docs/performance.md`](./performance.md))
- [ ] **Frontend builds without errors**: `npm run build` in `frontend/`
- [ ] **TypeScript checks pass**: `vue-tsc --noEmit` in `frontend/`
- [ ] **End-to-end smoke test**: create pipeline, trigger run, view results

---

## 10. Post-Launch Verification

- [ ] **Dashboard loads** at the production URL
- [ ] **Login works** with seeded admin credentials
- [ ] **API health endpoint** returns 200
- [ ] **OpenAPI docs** load at `/docs`
- [ ] **MCP endpoint** responds at `/mcp`
- [ ] **WebSocket connects** on the run inspection page
- [ ] **Pipeline creation** and execution works end-to-end
- [ ] **CORS** allows requests from the configured frontend origin
- [ ] **Rate limiting** returns 429 after threshold
- [ ] **Static assets** (JS, CSS, images) load without 404s
- [ ] **`install.sh`** downloads and runs successfully on a fresh machine

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| System requirements | [`docs/system-requirements.md`](./system-requirements.md) |
| Quickstart | [`docs/quickstart.md`](./quickstart.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment journeys | [`docs/deployment-journey.md`](./deployment-journey.md) |
| Security hardening | [`docs/deployment-security.md`](./deployment-security.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Upgrade process | [`docs/upgrade-process.md`](./upgrade-process.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Human tasks | `Repos/devtools/harness/docs/human-tasks.md` |
