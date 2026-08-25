# VPC-Only Deployment Verification Checklist

> Use this checklist when deploying Modulo in a VPC with no public internet
> access. Each item must be verified before marking the deployment as
> compliant with data residency requirements (PRD §10.5).

---

## 1. Telemetry & Observability

- [ ] **`MODULO_TELEMETRY_ENABLED` is not set** (defaults to `false`)
  - verify: `docker compose -f docker-compose.prod.yml exec modulo env | grep MODULO_TELEMETRY`
  - expected: no output or `MODULO_TELEMETRY_ENABLED=false`
- [ ] **`OTEL_EXPORTER_OTLP_ENDPOINT` is not set**
  - verify: `docker compose -f docker-compose.prod.yml exec modulo env | grep OTEL_EXPORTER_OTLP`
  - expected: no output
- [ ] **No OTel config saved in database** (if DB was migrated from non-VPC env)
  - verify: query `otel_config` table – `otlp_endpoint` should be empty string
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
  - verify: `docker compose -f docker-compose.prod.yml config | grep image`
  - expected: all images from internal registry URL

## 7. Secrets

- [ ] **No secrets reference external key management services** across VPC boundaries
  - verify: secret references resolve to VPC-internal KMS / key vault, not external providers

## 8. Runtime verification

- [ ] **Deployed container can reach database and Redis**
  - verify: `docker compose -f docker-compose.prod.yml exec modulo nc -zv postgres 5432`
  - verify: `docker compose -f docker-compose.prod.yml exec modulo nc -zv redis 6379`
- [ ] **Deployed container can NOT reach the public internet**
  - verify: `docker compose -f docker-compose.prod.yml exec modulo curl -s --connect-timeout 5 https://google.com`
  - expected: connection timeout or refused
- [ ] **Deployed container can NOT resolve public DNS names**
  - verify: `docker compose -f docker-compose.prod.yml exec modulo nslookup google.com`
  - expected: failure or NXDOMAIN (depending on DNS policy)

## 9. Documentation

- [ ] **Network egress audit** (`docs/operations/network-egress.md`) has been reviewed and matches the current deployment topology
- [ ] **Architecture diagram** reflects VPC boundaries, subnets, and security groups
- [ ] **Runbook** includes VPC egress troubleshooting steps

---

## Quick Reference: Key Env Vars for VPC Mode

| Variable | Required Value | Reason |
|---|---|---|
| `MODULO_TELEMETRY_ENABLED` | `false` (default) | Prevents OTel egress |
| `CORS_ORIGINS` | VPC-internal domain | No external-origin CORS needed |
| `MODULO_PUBLIC_URL` | VPC-internal URL | Links must resolve inside VPC |
| `SECRET_KEY` | Set to random 32+ byte value | Must not use placeholder |
| `FERNET_KEY` | Set to valid Fernet key | Must not use placeholder |

---

## Prior Art

Modulo's data residency architecture was inspired by the [AWS VPC Design
Guide](https://docs.aws.amazon.com/whitepapers/latest/building-saas-vpc/building-saas-vpc.html)
and follows the principle of **no implicit trust of external networks**.
