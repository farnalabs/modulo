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

A: In the customer's own database and object storage, on infrastructure the
customer controls. Modulo does not replicate customer data to Farnalabs.

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

A: Local accounts, OIDC, SAML 2.0, or scoped API keys. SCIM is available for
provisioning and deprovisioning.

Evidence: `docs/product-map/auth/oidc-integration.md`;
`docs/product-map/auth/saml-integration.md`;
`docs/product-map/auth/scim-provisioning.md`; `docs/architecture.md`
("Authentication & Authorization").

**Q: How is authorisation enforced?**

A: A central permission registry maps every REST and MCP operation to a
minimum organisation role (viewer, runner, operator, admin) plus team RBAC. An
unknown or misspelt permission key fails closed at import. Tenant isolation is
enforced by PostgreSQL row-level security keyed to the principal's
organisation.

Evidence: `backend/src/modulo/auth/permissions.py`; `docs/architecture.md`
("Row-Level Security").

**Q: Do you support least-privilege API keys?**

A: Yes. Org API keys are role-scoped (`operator` or `runner`) and may be
restricted to an explicit permission grant set instead of the full role bundle.

Evidence: `docs/architecture.md` ("API keys"); `modulo_create_api_key` /
`GET /api/v1/api-keys`; `backend/src/modulo/auth/permissions.py`.

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
recorded on the run. Human-in-the-loop gates block execution until a decision
is recorded, and a gate marked `human_only` cannot be decided by any machine
credential over REST or MCP.

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
events at `GET /api/v1/admin/audit`.

**Q: How do we know what actually happened in a run?**

A: A hash-chained, append-only audit trail records every governance-relevant
action with actor, resource, payload and outcome, and the chain can be verified
and exported as an offline-verifiable, Ed25519-signed evidence pack.

Evidence: `docs/offline-verifiable-evidence.md`;
`GET /api/v1/admin/audit/verify`; `GET /api/v1/admin/audit/evidence-pack`.

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

Evidence: `docs/security/dependency-policy.md`;
`backend/scripts/generate-sbom.py`; `.github/workflows/release.yml`
(`produce-sbom`); the dependency-audit steps in `.github/workflows/ci.yml`.

**Q: How do you scan your own code?**

A: Semgrep rules for tenant isolation, credential handling, sandboxing and
async database access; Bandit; Gitleaks on every commit; import-linter for
module boundaries.

Evidence: `.semgrep/`; `.pre-commit-config.yaml`; `.github/workflows/ci.yml`.

**Q: Do you have a penetration test report?**

A: The repository ships a penetration-test plan and scope document. Farnalabs
does not currently hold an independently executed third-party penetration test
report for the product, so do not answer yes to that question - supply the plan
and state that no external report exists.

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
security advisory to the customer. Advisories are published in the release
notes / repository; the customer must subscribe. This is a real limitation of
the self-hosted model and should be answered as such rather than promising a
notification channel that does not exist.

Evidence: `docs/release-process.md`; `docs/upgrade-process.md`.

**Q: How are serious incidents reported under the EU AI Act?**

A: Modulo does not detect or report serious incidents as the Act defines them.
This is an explicit gap in the control-mapping pack and remains the customer's
obligation as provider/deployer.

Evidence: `docs/security/compliance-control-mapping.md` (EU AI Act Article 73
row, coverage `gap`).

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

Evidence: `.github/workflows/release.yml`; `.github/workflows/ci.yml`.

## 9. Retention and deletion

**Q: How long is run data retained?**

A: Run retention and evidence retention are configurable by the operator
through admin settings; the audit trail is append-only and is not automatically
purged. The customer must set retention to the period their own policy (or the
EU AI Act's six-month minimum for logs) requires.

Evidence: `docs/deployment-security.md` (§6.3 Log Retention);
`GET /api/v1/admin/run-retention/candidates` and
`POST /api/v1/admin/run-retention/purge`;
`GET` / `PUT /api/v1/admin/evidence-retention` and
`POST /api/v1/admin/evidence-retention/purge`.

**Q: Can we delete our data?**

A: Yes - runs and evidence can be purged under the configured retention
controls, and an organisation can be deleted through the admin surface. Audit
events are append-only by design; their integrity is what makes the audit trail
evidence, so a request to delete audit events needs a documented exception
rather than a silent purge.

Evidence: `docs/architecture.md` ("Audit Logger");
`.github/workflows` retention jobs; `backend/src/modulo/core/audit_logger/append_only.py`.

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

A: The runtime refuses to start against a database that is not at the migration
head rather than serving on an inconsistent schema; migrations are applied by a
single release step.

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
