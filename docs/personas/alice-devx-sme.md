# Persona: Alice — Head of DevX at an SME

| Attribute | Value |
|---|---|
| **Role** | Head of Developer Experience / Engineering Productivity |
| **Org size** | 30–150 engineers across 3–6 product teams |
| **Technical level** | Expert — former senior engineer, now focused on tooling and workflow |
| **Industry** | B2B SaaS (fintech / healthtech) |
| **Compliance** | SOC 2 Type II, preparing for ISO 27001 |
| **Budget authority** | Recommends; VP Engineering approves |

## Goals

- Move toward agentic delivery without a big-bang replacement of the current SDLC
- Represent the current SDLC (manual steps, tool switches, approval gates) as a Modulo pipeline first — then replace low-risk steps one at a time
- Prove to leadership (who are "slightly scared of the AI new world") that agentic delivery is auditable, reversible, and UNDER human control
- Achieve HITL-proof delivery: every decision that matters is reviewed by a named human; the audit trail proves it
- Maintain SOC 2 compliance throughout the transition — no audit gaps
- Use off-the-shelf library workflows as starting points rather than building from scratch

## Pain points / triggers

- VP Eng read about AI coding agents and wants the speed; legal wants the controls; Alice is caught in the middle
- Current SDLC is undocumented tribal knowledge — onboarding takes months
- Any automation she introduces must have a rollback plan and an audit trail
- Past attempts at "AI in SDLC" were black-box SaaS products that compliance rejected immediately
- Manual QA and release steps are bottlenecks, but automating them feels risky without governance

## Key scenarios that must work

1. **`@persona-alice`** `features/orgs/org_onboarding.feature` — Set up org with SOC 2-relevant settings (retention, audit config)
2. **`@persona-alice`** `features/pipelines/create.feature` — Create a pipeline with a mix of agent nodes and manual (placeholder) nodes that mirror the current SDLC
3. **`@persona-alice`** `features/hitl/approval_gate.feature` — Every deploy requires a HITL approval from a named team member
4. **`@persona-alice`** `features/hitl/human_only_gate.feature` — Enforce human-only decisions on deploy and prod-data access gates
5. **`@persona-alice`** `features/audit/event_recording.feature` — Every agent action and human decision recorded immutably
6. **`@persona-alice`** `features/library/browse.feature` — Browse library; find a PRD→tickets workflow that matches her team's conventions
7. **`@persona-alice`** `features/library/copy_to_adapt.feature` — Copy a library workflow and customise agent prompts and schemas
8. **`@persona-alice`** `features/workflows/import.feature` — Import a shared team pipeline from YAML bundle
9. **`@persona-alice`** `features/schemas/version.feature` — Version schemas so that a schema change doesn't break running pipelines
10. **`@persona-alice`** `features/notifications/hitl_webhook.feature` — Slack webhook when a HITL gate is waiting for her team
11. **`@persona-alice`** `features/auth/rbac.feature` — Team-scoped RBAC so DevX own pipeline config but QA can only view

## Anti-scenarios (must NOT require)

- Migrating her entire SDLC in one sprint
- Writing pipeline YAML by hand — the visual builder and library search are the primary surfaces
- Giving agents access to production credentials or unapproved tools
- A blank canvas with no guidance or library starting point

## What success looks like

Alice has a Modulo pipeline that mirrors her current SDLC: ticket grooming (manual) → PRD writing (agent) → implementation (manual) → code review (manual) → QA (agent+manual) → deploy approval (HITL). The QA step was the first replacement (low-risk); the team gained 5 hours/week. The deploy gate has a `human_only` flag. Leadership sees the audit trail and approves replacing the next manual step. SOC 2 auditor thumbs-ups the HITL evidence.
