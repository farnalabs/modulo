# Security questionnaire answer bank

Answers to the standard security questionnaire for a **self-hosted** Modulo
deployment. Every answer cites the artefact or document that supports it, so
no answer is an unsupported claim.

**Rules for using this bank**

1. Never claim an attestation Modulo does not hold. There is no SOC 2, ISO
   27001/42001 or EU AI Act certificate. Say so plainly and point to the
   control-mapping pack instead.
2. Every answer below is a statement about the product plus the deployment the
   customer operates. Where the answer depends on the customer's own
   configuration, it says so and names who must supply the evidence.
3. If a question is not covered here, answer "not documented" and raise a
   documentation gap rather than improvising a claim.

## 1. Hosting model

**Q: Is Modulo a SaaS product?**

A: No. Modulo is self-hosted only. The customer runs it on their own
infrastructure. There is no Farnalabs-operated multi-tenant service and no
hosted production offering. A read-only demo with sample data exists at
`app.modulo.run/demo` for evaluation; it is not a hosted product and holds no
customer data.

Evidence: `README.md`; `docs/architecture.md`; `docs/core-principles.md`
("self-hosted, no telemetry").

**Q: Where does our data live?**

A: In the customer's own PostgreSQL database, on infrastructure the customer
controls. Modulo does not replicate customer data to Farnalabs. (Modulo has no
object-storage integration; there is no bucket to configure or to place
evidence in.)

Evidence: `docs/architecture.md` (Self-hosted constraint);
`docs/operations/network-egress.md` (default zero egress).

**Q: What does Modulo send outbound?**

A: Nothing by default. The base runtime has no telemetry endpoint and no
phone-home. Outbound calls happen only to systems the customer explicitly
configures: model backends (their provider account and key), connectors (their
GitHub/HTTP endpoints), notification destinations, and an OpenTelemetry
collector if the operator enables one (disabled by default).

Evidence: `docs/operations/network-egress.md`; `docs/security/secret-management.md`.

**Q: How is Modulo licensed?**

A: BUSL-1.1 (source-available, not OSI open source). The licence restricts
offering Modulo to third parties as a hosted service.

Evidence: `backend/pyproject.toml` (project licence); repository `LICENSE`.

## 2. Data flows

**Q: What data does Modulo process?**

A: Pipeline definitions and their immutable snapshots; run inputs, outputs and
node telemetry; audit events; HITL decisions; evaluation results; connector and
model credentials (encrypted); organisation, team and user records.

Evidence: `docs/architecture.md` ("Database Schema", "Key Components");
`docs/product-map/` feature entries.

**Q: Does Modulo read our source code or repositories?**

A: Only through a connector the customer configures, and only the data that
connector's operations request. Modulo does not receive a copy of the
repository; there is no Farnalabs-side mirror.

Evidence: `docs/product-map/configure/connectors.md`;
`docs/connector-authoring.md`.

**Q: How are model calls made?**

A: Through the model backend the customer configures (their own provider
account, key and endpoint). Prompts and outputs transit that provider, so the
provider's terms and data-processing agreement apply to that traffic - that is
a customer subprocessor decision, not a Farnalabs one.

Evidence: `docs/model-backend-authoring.md`;
`docs/security/secret-management.md`.

## 3. Encryption

**Q: Is data encrypted in transit?**

A: Modulo serves HTTP and is normally deployed behind the customer's TLS
terminating proxy. TLS configuration, certificate management and the cipher
policy are the customer's responsibility in a self-hosted deployment; the
product ships guidance and a hardening checklist, not a certificate.

Evidence: `docs/deployment-security.md`; `docs/deployment.md`.

**Q: Is data encrypted at rest?**

A: Credentials Modulo stores (connector and model-backend secrets) are
encrypted with Fernet using `FERNET_KEY`. Database and disk encryption at rest
are provided by the customer's platform (Postgres volume encryption, disk
encryption), not by Modulo. Startup refuses to run if `FERNET_KEY` or
`SECRET_KEY` is absent or shorter than 32 bytes.

Evidence: `docs/security/secret-management.md`;
`docs/configuration-reference.md` (`FERNET_KEY`, `SECRET_KEY`).

**Q: Are backups encrypted?**

A: Yes - the shipped backup script encrypts backups with AES-256-CBC via
OpenSSL. The key material and its custody are the customer's.

Evidence: `docs/operations/backup.md`.

**Q: How are credentials protected from logs and state?**

A: Decrypted credentials never enter LangGraph state, checkpoint blobs, OTel
spans or logs; a custom semgrep rule enforces it. Sensitive DOM values default
to masked (`•••••`) with a short server-authenticated reveal.

Evidence: `docs/security/secret-management.md`; `.semgrep/credential_in_state.py`.

## 4. Access control

**Q: How do users authenticate?**

A: Local accounts, OIDC, SAML 2.0, or scoped API keys. SCIM 2.0 is available for
provisioning and deprovisioning. Two conditions apply: SSO (OIDC/SAML) and SCIM
are **Team-tier** features - an unlicensed Community install answers HTTP 402 -
and SCIM additionally requires `MODULO_SCIM_TOKEN` to be configured, answering
"SCIM is not configured - set MODULO_SCIM_TOKEN" otherwise.

Evidence: `docs/product-map/auth/oidc-integration.md`;
`docs/product-map/auth/saml-integration.md`;
`docs/product-map/auth/scim-provisioning.md`; `docs/architecture.md`
("Authentication & Authorization"); the `sso` and `scim` feature flags in
`backend/src/modulo/core/feature_flags.py`; `backend/src/modulo/auth/scim_auth.py`.

**Q: How is authorisation enforced?**

A: A central permission registry maps every authenticated REST route that reads
or mutates organisation data, and every MCP tool, to a minimum organisation role
(viewer, runner, operator, admin). An unknown or misspelt permission key fails
closed at import. Tenant isolation is enforced by PostgreSQL row-level security
keyed to the principal's organisation.

Two scoping notes. First, the routes that carry no permission key are deliberate
and named: the pre-authentication entry points (local login, OIDC/SAML callback
and metadata) and `GET /api/v1/deployment` run before a principal exists, and
`GET /api/v1/auth/me` and `GET /api/v1/auth/csrf-token` are authenticated but
self-scoped - they return only the caller's own identity, or a CSRF token, and
read no other party's data. Second, team RBAC is a **Team-tier** feature
(`team_rbac`); the four-level organisation role hierarchy and row-level security
themselves are always active.

Evidence: `backend/src/modulo/auth/permissions.py`; `docs/architecture.md`
("Row-Level Security").

**Q: Do you support least-privilege API keys?**

A: Yes. Org API keys are always role-scoped (`operator` or `runner`), which is
the default least-privilege control. An explicit per-key permission grant set -
narrower than the role bundle - is available but sits behind the
`api_key_grants` feature flag, which is **default-OFF** on every tier; if the
customer has not enabled it, answer with the role-scoped answer, not the
grant-set one.

Evidence: `docs/architecture.md` ("API keys"); `modulo_create_api_key` /
`GET /api/v1/api-keys`; `backend/src/modulo/auth/permissions.py`; the
`api_key_grants` entry in `DEFAULT_OFF_FLAGS` in
`backend/src/modulo/core/feature_flags.py`.

**Q: What happens if an administrator account is lost?**

A: A documented break-glass recovery runbook and a break-glass admin mechanism
exist.

Evidence: `docs/operations/break-glass-admin-recovery-runbook.md`;
`docs/operations/admin-bypass.md`.

**Q: Do you enforce MFA?**

A: Not natively. MFA is delegated to the customer's identity provider through
OIDC or SAML. A local-password account is single-factor unless the deployment
puts it behind an authenticating proxy.

Evidence: `docs/product-map/auth/oidc-integration.md`;
`docs/product-map/auth/saml-integration.md`; `docs/deployment-security.md`.

## 5. SDLC and change management

**Q: How are changes to pipelines approved and controlled?**

A: Pipeline changes are versioned and every run executes against an immutable
snapshot (frozen graph, models, prompts, schemas, guardrails and policy gates)
recorded on the run. A human-in-the-loop gate marked `human_only` - the default
for every gate - interrupts the run until a decision is recorded, and it does so
at **every** autonomy level: `human_only` overrides autonomy. Such a gate rejects
API keys and programmatic tokens over REST and MCP. Two conditions must be
stated with that answer. (1) A browser session may decide a `human_only` gate,
and so may a consent-derived, human-linked OAuth credential over MCP while the
org-wide `mcp_human_linked_hitl_authority` flag is on - which is the default on
every tier; `deliver_manual` stays browser-only. (2) Autonomy only changes the
outcome for a gate that explicitly opts out with `human_only: false`:
`notify_on_complete` auto-approves it at run time and `fully_autonomous` skips
it entirely. The default gate is never skipped.

Evidence: `docs/product-map/pipelines/snapshot-versioning.md`;
`docs/product-map/hitl/hitl-gates.md`;
`docs/security/compliance-control-mapping.md` (SOC 2 CC8.1 rows).

**Q: How is agent output verified?**

A: Evaluation definitions score output against a pinned definition version;
policy gates can block, warn, escalate or continue based on the result, and the
result is persisted before the decision.

Evidence: `docs/evals.md`; `docs/product-map/configure/guardrails.md`.

**Q: How is the level of automation changed?**

A: Earned autonomy is never automatic. A promotion is an explicit approval
capped by the pipeline's ceiling, applies only to runs created afterwards, and
records the previous level, the new level and the reason in the audit trail. A
demotion is equally audited.

Evidence: `POST /api/v1/pipelines/{pipeline_id}/autonomy/promote` and
`POST /api/v1/pipelines/{pipeline_id}/autonomy/demote`; the
`pipeline.autonomy_promotion_decided` / `pipeline.autonomy_demoted` audit
events, recorded on every tier and read at `GET /api/v1/admin/audit` (Team-tier
`audit_viewer` feature plus `audit.manage`).

**Q: How do we know what actually happened in a run?**

A: A hash-chained, append-only audit trail records every governance-relevant
action with actor, resource, payload and outcome. **Recording is not
licence-gated**: the append-only triggers and hash chaining are always active,
so a Community deployment records the trail too. **Reading it through the
product is Team-tier**: browsing the trail, verifying the chain in place
(`GET /api/v1/admin/audit/verify`, which checks up to 10,000 events per call and
reports `truncated` when it stops short), exporting it, scanning it and taking
the offline-verifiable, Ed25519-signed evidence pack all need the Team-tier
`audit_viewer` feature plus the `audit.manage` permission. The signed evidence
pack additionally needs a configured `MODULO_EVIDENCE_SIGNING_KEY`. On a
Community install the events exist and are hash-chained in the customer's own
database, but no admin route through the product can browse, verify or export
them.

Evidence: `docs/offline-verifiable-evidence.md`;
`GET /api/v1/admin/audit`; `GET /api/v1/admin/audit/verify`;
`GET /api/v1/admin/audit/evidence-pack`;
`backend/src/modulo/api/routes/audit.py`.

**Q: How does this map to our compliance framework?**

A: The shipped control-mapping pack maps Modulo's controls and evidence to SOC
2 TSC (CC6, CC7, CC8 and the evidence layer), ISO/IEC 42001 and the EU AI Act,
with per-row coverage and provenance. `GET /api/v1/compliance/evidence-index`
generates the per-instance evidence index for an auditor.

Evidence: `docs/security/compliance-control-mapping.md`;
`backend/src/modulo/compliance/mappings/`.

**Q: Do you have a documented SDLC for the product itself?**

A: Yes - changes land through a reviewed pull request with CI gates (lint,
type-check, unit/integration/BDD tests, secret scan, semgrep, dependency audit,
migration checks).

Evidence: `.github/workflows/ci.yml`; `.pre-commit-config.yaml`.

## 6. Vulnerability management

**Q: How are dependencies tracked and vulnerabilities remediated?**

A: A documented dependency policy defines severity service levels and the
scanning gates. `pip-audit` (backend) and `pnpm audit` (frontend) run in CI; a
CycloneDX SBOM is generated for every release from the pinned dependency set.
The `Release: Unified Pipeline` workflow owns publication of the release
artefact and SBOM; stable publication occurs in its `promote` mode after RC
validation.

Evidence: `docs/security/dependency-policy.md`;
`backend/scripts/generate-sbom.py`; `.github/workflows/release-unified.yml`
(`produce-sbom`, and the `promote`-only stable publication steps); the
dependency-audit steps in `.github/workflows/ci.yml`.

**Q: How do you scan your own code?**

A: Semgrep rules for tenant isolation, credential handling, sandboxing and
async database access; Bandit; Gitleaks on every commit; import-linter for
module boundaries.

Evidence: `.semgrep/`; `.pre-commit-config.yaml`; `.github/workflows/ci.yml`.

**Q: Do you have a penetration test report?**

A: The repository ships a penetration-test plan and scope document; no
third-party penetration-test report is shipped in the repository. Do not
answer yes to a "do you have an external penetration test report" question
unless the operator can actually produce the report - state that a plan exists
and that no external report is attached to the product.

Evidence: `docs/security/penetration-test-plan.md`.

## 7. Incident response

**Q: Do you have an incident response plan?**

A: The product ships an incident-response playbook and a runbook for
containment and recovery. In a self-hosted deployment the customer owns
incident declaration, response and reporting; the playbook is guidance, and
Farnalabs has no access to the customer's instance to respond on their behalf.

Evidence: `docs/security/incident-response-playbook.md`;
`docs/operations/break-glass-admin-recovery-runbook.md`.

**Q: How would a security incident in the product be disclosed to us?**

A: Modulo has no phone-home channel, so a self-hosted deployment cannot push a
security advisory to the customer. Security-relevant fixes are recorded in
`CHANGELOG.md` and the GitHub Release for each version; the customer must watch
releases (or track the repository) to learn about them. This is a real
limitation of the self-hosted model and should be answered as such rather
than promising a notification channel that does not exist.

Evidence: `CHANGELOG.md`; `docs/release-process.md`;
`docs/upgrade-process.md`.

**Q: How are serious incidents reported under the EU AI Act?**

A: Modulo does not detect or report serious incidents as the Act defines them.
The control-mapping pack marks this `customer-responsibility` - the obligation
as provider/deployer stays entirely with the customer. The shipped
incident-response playbook includes incident-communication and post-mortem
templates, which support writing the report; they are documentation, not a
detection or reporting capability.

Evidence: `docs/security/compliance-control-mapping.md` (EU AI Act Article 73
row, coverage `customer-responsibility`);
`docs/security/incident-response-playbook.md`.

## 8. Subprocessors and third parties

**Q: Who are your subprocessors?**

A: For a self-hosted deployment, Farnalabs processes no customer data, so
there is no subprocessor chain for the customer's instance. The third parties
that DO see customer data are the ones the customer configures: model
providers, connected systems (source control, trackers, HTTP endpoints), and
any OpenTelemetry collector the operator enables. Those are the customer's
subprocessor decisions.

Evidence: `docs/operations/network-egress.md`;
`docs/security/secret-management.md`.

**Q: Where is the product built?**

A: Release builds run on GitHub Actions runners. That pipeline handles the
product source, not customer data.

Evidence: `.github/workflows/release-unified.yml`; `.github/workflows/ci.yml`.

## 9. Retention and deletion

**Q: How long is run data retained?**

A: Two distinct settings, with two different conditions - do not answer them as
one. Run retention (`GET /api/v1/admin/run-retention/candidates`,
`POST /api/v1/admin/run-retention/purge`) requires the **Team-tier**
`admin_run_retention` feature and answers HTTP 402 without it. Evidence
retention (`GET`/`PUT /api/v1/admin/evidence-retention`,
`POST /api/v1/admin/evidence-retention/purge`) is admin-permission only and not
plan-gated, so it is available on every tier. The audit trail itself is
append-only and is not automatically purged. The customer must configure
retention to the period their own policy (or the EU AI Act's six-month minimum
for logs) requires.

Evidence: `docs/deployment-security.md` (�6.3 Log Retention);
`backend/src/modulo/api/routes/admin_run_retention.py` (the
`admin_run_retention` feature dependency);
`backend/src/modulo/api/routes/admin_evidence_retention.py`.

**Q: Can we delete our data?**

A: Yes - runs and evidence can be purged under the configured retention
controls, and an organisation can be deleted through the admin surface. Run
purge is the Team-tier `admin_run_retention` feature; evidence purge is
admin-permission only. Audit events are append-only by design; their integrity
is what makes the audit trail evidence, so a request to delete audit events
needs a documented exception rather than a silent purge.

Evidence: `docs/architecture.md` ("Audit Logger");
`POST /api/v1/admin/run-retention/purge` and
`POST /api/v1/admin/evidence-retention/purge` (the admin routes, not a CI job);
`backend/src/modulo/core/audit_logger/append_only.py`.

## 10. Backup and restore

**Q: How are backups taken?**

A: A shipped CLI script backs up Postgres data, the Fernet keys and
configuration, encrypting the output with AES-256-CBC via OpenSSL.

Evidence: `docs/operations/backup.md`.

**Q: How often, and how is restore tested?**

A: Backup frequency and restore drills are the operator's runbook. Modulo ships
the tooling and the procedure; it does not schedule, execute or evidence the
customer's backups. This is marked `customer-responsibility` in the
control-mapping pack (SOC 2 CC7.5).

Evidence: `docs/operations/backup.md`;
`docs/security/compliance-control-mapping.md` (CC7.5 row).

**Q: What happens if the database is behind the code?**

A: The runtime applies migrations itself at boot: the release step runs
`alembic upgrade heads` once before machines roll out, and each process runs the
same upgrade (idempotent, behind a Postgres advisory lock) with a bounded retry
loop. If the schema cannot be brought to head, boot fails and the process does
not serve on an inconsistent schema. The outcome is that a behind-the-code
database gets migrated rather than silently served - not that the runtime
refuses to start whenever it is behind.

Evidence: `docs/upgrade-process.md`; `docs/operations/backup.md`.

## 11. Attestations and audits

**Q: Do you hold SOC 2 / ISO 27001 / ISO 42001 certification?**

A: No. This is a deliberate decision: for a self-hosted product the customer's
auditor audits the customer's instance, so the useful artefact is a mapping
from the instance's evidence to the clauses being tested, not our certificate.
That mapping ships in the product and the per-instance evidence index is
generated from it.

Evidence: `docs/security/compliance-control-mapping.md`;
`backend/src/modulo/compliance/mappings/`.

**Q: Can you complete our security questionnaire?**

A: Yes - this document is the answer bank. Answers that depend on the
customer's deployment are marked as such; answers Modulo cannot support are
stated as limitations rather than filled with a plausible claim.

Evidence: this document.
