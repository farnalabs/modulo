# VPC-Only Deployment Verification Checklist

> Use this checklist when deploying Modulo in a VPC with no public internet
> access. Each item must be verified before marking the deployment as
> compliant with data residency requirements (principle:
> `docs/core-principles.md` §9 "Self-hosted, no telemetry by default";
> the PRD is retired).

---

## 1. Telemetry & Observability

- [ ] **`MODULO_TELEMETRY_ENABLED` is not set** (defaults to `false`)
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo env | grep MODULO_TELEMETRY`
  - expected: no output or `MODULO_TELEMETRY_ENABLED=false`
  - **the env var alone is not sufficient.** A deployment-wide, process-global
    runtime override can flip it on without a restart: `GET /api/v1/admin/telemetry`
    reports the effective value (`is_telemetry_enabled()` reads the runtime-config store
    first, and only falls back to the env-var-backed `Settings` value).
  - verify: `GET /api/v1/admin/telemetry` (needs `system.config.manage`) returns
    `{"enabled": false}`
  - the override applies to the web process immediately but the SAQ workers are separate
    processes with their own store instance, so set the env var in the deployment
    environment rather than relying on the override (see
    `docs/operations/network-egress.md` §Cross-process note)
- [ ] **`OTEL_EXPORTER_OTLP_ENDPOINT` is not set**
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo env | grep OTEL_EXPORTER_OTLP`
  - expected: no output
- [ ] **No OTel config saved in database** (if DB was migrated from non-VPC env)
  - verify: the organisation row's `otel_config_json` carries no exporter endpoint – `SELECT otel_config_json FROM organisations;`
  - expected: no `otlp_endpoint` key, or an empty value
- [ ] **LangSmith tracing is disabled** per-org in settings UI
  - verify: check observability settings page shows "disabled"

## 2. Connectors

- [ ] **No ConnectorInstance targets an external URL**
  - verify: `GET /api/v1/connectors` returns empty list, or each entry's `base_url` points to VPC-internal services
- [ ] **No ModelBackend uses external API endpoints**
  - verify: Anthropic/OpenAI model backends cannot reach `api.anthropic.com` or `api.openai.com` without internet access – confirm no model backends are configured, or they point to VPC-internal model serving endpoints

## 3. Webhooks & Notifications

- [ ] **All webhook URLs point to VPC-internal endpoints**
  - verify: `GET /api/v1/admin/notifications` – ensure every `url` field is an internal address (`https://internal-alb.*`, VPC-internal hostnames, etc.)
- [ ] **No external webhook delivery logs show successful deliveries**
  - verify: check notification delivery log for external URLs

## 4. SSO / Authentication

- [ ] **OIDC provider discovery URL points to VPC-internal IdP**
  - verify: SSO settings show internal URL, not `https://accounts.google.com` or `https://login.microsoftonline.com`
- [ ] **SAML IdP metadata URL points to VPC-internal IdP**
  - verify: metadata URL is internal or metadata XML is uploaded directly

## 5. Network Policy (defense in depth)

- [ ] **Host firewall / security group blocks all egress except to known VPC services**
  - verify: host firewall (iptables/nftables) or security-group rules allow outbound only to Postgres, Redis, and VPC-internal services
- [ ] **No `0.0.0.0/0` egress rules** exist in the host firewall or security groups
- [ ] **DNS resolution is restricted to VPC-internal DNS** (VPC resolver / split-horizon zones)
- [ ] **Reverse proxy is configured for internal-only load balancer** (e.g., AWS `alb.scheme: internal`)

## 6. Container image security

- [ ] **Image is pulled from VPC-internal registry** (ECR, GAR, ACR) – not Docker Hub
  - the shipped `deploy/compose/docker-compose.prod.yml` does **not** satisfy this out of
    the box: it pins `ghcr.io/farnalabs/modulo:${TAG:-latest}`, `postgres:16-alpine` and
    `redis:8-alpine`. The last two are Docker Hub official images, so an air-gapped or
    egress-restricted VPC cannot pull them.
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml config | grep image`
  - expected **baseline**: the three images above
  - expected **compliant**: every image resolves to an internal registry URL, which means
    mirroring all three into it (the Modulo image via the `${TAG}` variable, Postgres and
    Redis by mirroring the official images or pointing those service blocks at your own
    internal tags) before running the check again
  - note that `backend/tests/architecture/test_prod_compose_env_contract.py` guards the
    compose **environment** contract only (every variable reaches the app, every required
    `Settings` field is satisfied, every required variable is in the `.env` example); it
    does not check the `image:` lines, so a registry change is yours to keep correct

## 7. Secrets

- [ ] **No secrets reference external key management services** across VPC boundaries
  - verify: secret references resolve to VPC-internal KMS / key vault, not external providers

## 8. Runtime verification

- [ ] **Deployed container can reach database and Redis**
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo nc -zv postgres 5432`
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo nc -zv redis 6379`
- [ ] **Deployed container can NOT reach the public internet**
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo curl -s --connect-timeout 5 https://google.com`
  - expected: connection timeout or refused
- [ ] **Deployed container can NOT resolve public DNS names**
  - verify: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo nslookup google.com`
  - expected: failure or NXDOMAIN (depending on DNS policy)

## 9. Documentation

- [ ] **Network egress audit** (`docs/operations/network-egress.md`) has been reviewed and matches the current deployment topology
- [ ] **Architecture diagram** reflects VPC boundaries, subnets, and security groups
- [ ] **Runbook** includes VPC egress troubleshooting steps

---

## Quick Reference: Key Env Vars for VPC Mode

| Variable | Required Value | Reason |
|---|---|---|
| `MODULO_TELEMETRY_ENABLED` | `false` (default) | Prevents OTel egress; see §1 for the runtime override |
| `CORS_ORIGINS` | VPC-internal domain, comma-separated, no trailing slash | No external-origin CORS needed; a trailing `/` is rejected at boot |
| `MODULO_PUBLIC_URL` | VPC-internal URL | Browser-facing base for post-login redirects, SSO and the MCP OAuth authorize link |
| `SECRET_KEY` | Random 32+ byte value | Below 32 bytes, or one of the blocked placeholders (`changeme`, `secret`, `your-secret-key`, `development`, `test`, `insecure`), is rejected at boot |
| `FERNET_KEY` | Valid Fernet key | Same 32-byte minimum; the value is loaded by `cryptography.fernet.Fernet`, so it must be a real Fernet key, not an arbitrary 32-byte string |

---

## Prior Art

Modulo's multi-VPC network design follows the VPC segmentation and IP address
planning guidance in [Building a Scalable and Secure Multi-VPC AWS Network
Infrastructure](https://docs.aws.amazon.com/whitepapers/latest/building-scalable-secure-multi-vpc-network-infrastructure/welcome.html).
The default-deny egress posture in §5 (no implicit trust of external networks)
is Modulo's own design choice, not a whitepaper recommendation.
