# Persona: Marcus — CISO at a Regulated Organisation

| Attribute | Value |
|---|---|
| **Role** | Chief Information Security Officer |
| **Org size** | 500–5,000 employees; 100–400 engineers |
| **Technical level** | Deep security domain expertise; does not write application code |
| **Industry** | Financial services, healthcare, or government contractor |
| **Compliance** | SOC 2 Type II, ISO 27001, FedRAMP / IL5, SOX |
| **Budget authority** | Signatory for Enterprise procurement |

## Goals

- Allow the organisation to capture AI-accelerated delivery speed *without* expanding the attack surface or violating regulatory commitments
- Ensure every agent action is auditable, attributable, and revertible — no "the AI did it" as an explanation
- Maintain data residency: no agent output, source code, or business logic ever leaves the org's infrastructure
- Approve a self-hosted governance layer that survives toolchain churn (GitHub→GitLab, Jira→Linear, etc.)
- Demonstrate to auditors that AI-in-SDLC has equivalent or stronger controls than human-only delivery

## Pain points / triggers

- An audit finding flagged unreviewed AI-generated code reaching production — management wants the speed but needs the control
- Regulator is asking "how do you know your AI agents are operating within bounds?"
- Current "AI policy" is a wiki page with no technical enforcement
- Legal won't sign a data processing agreement with any AI coding SaaS — data must stay on-prem
- Security team is too small to manually review every AI-generated artifact

## Key scenarios that must work

1. **`@persona-marcus`** `features/audit/event_recording.feature` — Immutable, append-only audit log; no agent or human can delete or alter events
2. **`@persona-marcus`** `features/audit/crypto_chain.feature` — Cryptographic hash chain on audit events; tamper evidence
3. **`@persona-marcus`** `features/auth/tenant_isolation.feature` — Organisational data strictly isolated via RLS; proven under concurrent load
4. **`@persona-marcus`** `features/hitl/human_only_gate.feature` — Critical gates (prod deploy, data access, config change) require a named human
5. **`@persona-marcus`** `features/notifications/failure_webhook.feature` — Alert SecOps on suspicious run patterns or repeated eval failures
6. **`@persona-marcus`** `features/connectors/health_check.feature` — Connector credential health; automatic disable on auth failure
7. **`@persona-marcus`** `features/model_backends/health_check.feature` — Model backend provenance verification; only approved providers
8. **`@persona-marcus`** `features/pipelines/checkpoint_resume.feature` — Checkpoints are encrypted at rest; no plaintext state leakage
9. **`@persona-marcus`** `features/security/credential_store.feature` — All credentials Fernet-encrypted; never in logs, state, or OTel spans
10. **`@persona-marcus`** `features/security/input_sanitization.feature` — Injection prevention on all prompt inputs
11. **`@persona-marcus`** `features/notifications/signing.feature` — Outbound webhooks signed; no forged notifications
12. **`@persona-marcus`** `features/orgs/member_management.feature` — Offboarding immediately revokes access; stale sessions killed

## Anti-scenarios (must NOT happen)

- Agent output that cannot be traced to a specific prompt and model version
- Credentials or secrets visible in logs, traces, or state checkpoints
- Any SaaS dependency in the audit or policy enforcement path
- Human-in-the-loop gates that can be bypassed with an API key
- A shared-secret deployment where individual actions cannot be attributed

## What success looks like

Marcus reviews Modulo's architecture: self-hosted, Fernet-encrypted credential store, immutable audit log with hash chain, RLS-enforced tenant isolation, `human_only` gate enforcement. He approves a pilot with the platform engineering team. After the first SOC 2 surveillance audit, the auditor notes "agentic SDLC controls exceed manual equivalent" — no findings. Marcus signs the Enterprise licence renewal.
