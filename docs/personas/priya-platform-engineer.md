# Persona: Priya — Platform Engineer at a Scaling Org

| Attribute | Value |
|---|---|
| **Role** | Platform Engineer / AI Enablement Lead |
| **Org size** | 150–1,000+ engineers across 10–30 teams |
| **Technical level** | Expert — builds internal tooling and developer platforms |
| **Industry** | B2B SaaS, e-commerce |
| **Compliance** | SOC 2, GDPR; considering FedRAMP |
| **Budget authority** | Evaluates and recommends; VP Platform Engineering or CTO approves |

## Goals

- Evaluate and deploy a governed agentic SDLC platform after a near-miss audit (AI-generated code reached production unreviewed)
- Prove to CISO and legal that AI agents can operate within defined boundaries with full auditability
- Self-host on the org's existing Kubernetes infrastructure — no data leaves their VPC
- Integrate with existing identity provider (OIDC/Okta), issue tracker (Jira/Linear), and code host (GitHub Enterprise / GitLab Self-Managed)
- Roll out agentic delivery team-by-team with central policy controls
- Evaluate agent output quality systematically before expanding autonomy

## Pain points / triggers

- A developer used Cursor agent to write and merge a PR containing a security vulnerability — no one caught it because there was no review gating for agent-authored code
- CISO has banned "any AI that writes code" until there's a governance layer
- Existing toolchain (GitHub, Jira, Jenkins) is entrenched; any new platform must compose with it, not replace it
- Platform team is small (4 people) and cannot hand-roll guardrails for every team
- Need to show adoption metrics and quality trends to justify the platform investment

## Key scenarios that must work

1. **`@persona-priya`** `features/auth/api_keys.feature` — CI/CD pipeline triggers runs via API key with `runner` role
2. **`@persona-priya`** `features/auth/rbac.feature` — Team-scoped RBAC; each team sees only their own pipelines
3. **`@persona-priya`** `features/auth/sso.feature` — OIDC login via Okta; JIT provisioning for new engineers
4. **`@persona-priya`** `features/connectors/github_connector.feature` — Connect to GitHub Enterprise Server (self-hosted)
5. **`@persona-priya`** `features/connectors/jira_connector.feature` — Read/write Jira issues
6. **`@persona-priya`** `features/pipelines/concurrency.feature` — Enforce org-wide and team-level max concurrent runs
7. **`@persona-priya`** `features/pipelines/validation.feature` — Pre-run validation catches misconfigured nodes
8. **`@persona-priya`** `features/eval/eval_suite_crud.feature` — Define eval suites per team; enforce minimum pass thresholds
9. **`@persona-priya`** `features/eval/feedback_system.feature` — HITL rejections feed back into eval suite growth
10. **`@persona-priya`** `features/pipelines/run_variants.feature` — A/B test Sonnet vs Opus on the same pipeline; compare eval scores
11. **`@persona-priya`** `features/observability/metrics.feature` — Organisation-wide dashboard: runs/hour, token spend, avg eval pass rate by team
12. **`@persona-priya`** `features/audit/event_recording.feature` — Immutable audit trail; export for compliance review
13. **`@persona-priya`** `features/organisation/rls_isolation.feature` — Tenant isolation proven under load (multi-team on shared infra)
14. **`@persona-priya`** `features/model_backends/configure.feature` — Centralised model backend config; Fernet-encrypted credentials
15. **`@persona-priya`** `features/model_backends/health_check.feature` — Health check monitors provider latency; auto-failover on outage

## Anti-scenarios (must NOT require)

- Individual developers configuring their own model backends or connectors — these are platform-managed
- Giving teams visibility into other teams' pipelines, credentials, or costs
- Relying on a SaaS control plane or cloud-hosted component for policy enforcement
- Manual per-team setup of base infrastructure

## What success looks like

Priya deploys Modulo on the org's EKS cluster, configures Okta SSO, connects to GitHub Enterprise and Jira, and enables the first 3 teams with approved pipelines (PRD→tickets, code review, deploy gate). CISO sees the audit trail and lifts the AI ban. Priya's dashboard shows team A has 92% eval pass rate and saved 8 engineer-hours/week on release notes. She adds 3 more teams and schedules a quarterly eval review.
