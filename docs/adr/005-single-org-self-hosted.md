# ADR 005 — Self-Hosted Deployments Use One Org; Teams Are the Separation Boundary

**Date**: 2026-06-30  
**Status**: Active

---

## Context

Modulo is multi-tenant from day one — every table carries `organisation_id`, RLS enforces tenant isolation, and the same codebase powers both self-hosted and SaaS (PRD §6.2). The Organisation entity exists because SaaS requires it.

This creates an architectural ambiguity: a self-hosting team *can* create multiple orgs inside their single deployment. Nothing in the codebase prevents it. But the question is whether we should encourage, support, or design for that pattern.

There are real reasons a self-hosting team might want internal separation — different departments with different compliance boundaries, separate billing entities, different retention policies. But none of these are unmet by the Team model:

| Need | Team solution |
|---|---|
| Department isolation | Team-scoped pipelines, stages, connectors, model backends |
| Different retention policies | Per-org setting in SaaS; self-hosted is single-org |
| Separate billing entities | Irrelevant to self-hosted — no billing |
| Compliance firewalls | Compliance boundary is the deployment, not the org |
| Admin visibility across groups | Admins see all teams within the single org |

## Decision

Self-hosted deployments are **one org**. Teams are the internal separation boundary.

Concretely:

- The self-hosted installation flow seeds exactly one org (the `default` org).
- Product documentation presents self-hosted as single-org with team-based workspace isolation.
- No cross-org admin tooling, multi-org management UI, or org-switcher is built for the self-hosted context.
- The `organisation_id` column exists because the same schema serves SaaS, not because self-hosted users should create multiple orgs.
- If a self-hosted team genuinely needs a separate compliance or data boundary, the answer is a second deployment — not a second org inside the same Postgres cluster.

## What This Means for Product Decisions

| Consider building | Do not build |
|---|---|
| Team management UI (RBAC, visibility, ownership) | Cross-org admin panel for self-hosted |
| Team-scoped pipeline visibility | Org-switcher dropdown |
| Team-scoped connector/model backend access | Multi-org bulk export from one deployment |
| Admin sees-all-teams view within one org | "Create new org" button in self-hosted UI |
| Team cost attribution | Org-level billing per department (SaaS concern) |

## Why Not Multi-Org in Self-Hosted

1. **RLS is hard.** `SET LOCAL app.organisation_id` is a transaction-scoped session variable. Browsing org A while holding a session for org B is not possible without the modulo-cloud routing layer. Building a cross-org admin role means either a super-user bypass of RLS (defeating tenant isolation) or a complex token-switching layer. Neither is worth the complexity for a self-hosted deployment that could simply use teams.

2. **LangGraph checkpoint isolation is incomplete** (PRD §6.2). Checkpoint tables lack `organisation_id`. We'd need to subclass `PostgresSaver` before multi-org SaaS launch. Until then, multiple orgs sharing one Postgres share checkpoint data — a data leak. This is a known gap for SaaS, but it means we must actively discourage multi-org self-hosted until it's fixed.

3. **Teams already cover the workspace pattern.** The Team entity provides pipeline visibility scoping, team-scoped connector access, team-scoped HITL gates, and team membership with roles. A 5-department company gets one org and 5 teams. This is the intended pattern (§8.3).

4. **Second deployment is the escape hatch.** If a team needs true data separation (different Postgres cluster, different network boundary, different upgrade cadence), they deploy a second instance. Self-hosted means they own the infrastructure — deploying another copy is a `docker compose up` away.

## Related Documents

- PRD §6.2 — SaaS-First Multi-Tenant Architecture
- PRD §6.2 — LangGraph Checkpoint Isolation (known gap)
- PRD §8.3 — Team entity and team-scoped RBAC
- ADR 002 — Database Abstraction Strategy (RLS vs generic tenant filtering)
