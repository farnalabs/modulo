# Product Map

The product map is the single inventory of every shipped feature in Modulo. It has two
layers:

1. **`frontend/src/manifest.yaml`** — the machine-readable product surface (ADR 008 — Core
   Shared Manifest). It registers every UI route, its `product_map: [feat-*]` references,
   sidebar grouping, permissions/tiers, testable `data-testid` elements, and the
   `features:` registry of allowed feature ids. The backend serves it at
   `/api/v1/manifest`, the frontend router/nav consume it at build time, and Remy's
   `search_documentation` indexes each route's `product_map` refs.
2. **`docs/product-map/`** — the feature graph (this directory). One behaviour-tracker
   entry per feature, keyed by the same `feat-*` id, describing expected behaviours,
   happy paths, error semantics, coverage, and known gaps. Entries are the human-readable
   layer on top of the manifest registry.

This file is the graph root. Entries are nodes; the frontmatter fields are typed edges.

## Source of truth

`frontend/src/manifest.yaml` **must never shrink** the `features:` registry without
reason: every registered feature is referenced by at least one shipped route, and every
`feat-*` reference anywhere in the codebase must resolve either to a registered manifest
feature or to a behaviour-tracker entry below (enforced by
`backend/tests/architecture/test_product_map.py`). If a feature ships, it appears in one
of these two places — otherwise it is invisible to Remy and to this graph.

## Entry format

```markdown
---
id: feat-<domain>-<feature>        # unique feature id (match the manifest registry)
prd: N.N                           # PRD section (N/A for infra-only surfaces)
adr: [Repos/devtools/adr/...md]              # governing ADRs (optional)
code: [backend/src/...]            # code paths implementing this feature
bdd: [tests/.../feature]           # BDD feature files (missing = coverage gap)
unit-tests: [tests/...]            # unit/integration test files
delivery-tasks: [task-...]         # delivery-plan task ids
depends-on: [feat-...]             # prerequisite features
status: covered | partial | gap    # coverage status of the behaviours below
---

# <Feature Name>

<One-paragraph summary of what the feature is and who uses it.>

## Behaviours

- [x] every shipped behaviour, checked once verified against code + tests
- [ ] unchecked = genuine gap (moved to Known Gaps when acknowledged)

## Known Gaps

Bulleted, concrete gaps that are consciously not shipped or not covered.

## QA History

Dated audit notes: what was verified, when, and by which pass.
```

The node id in a feature-graph entry is the `feat-*` id (e.g. `feat-infra-health`).
Infra-only surfaces that have no UI route are tracked **here**, not in the manifest
`features:` registry — the manifest registry is for route-referenced product features.

## Contribution workflow

For **any** feature change (new feature, changed behaviour, deprecated behaviour):

1. Update the manifest: add/update the route's `product_map` references and keep the
   `features:` registry description accurate (`frontend/src/manifest.yaml`).
2. Update the behaviour-tracker entry in this directory (`docs/product-map/`) — tick
   verified behaviours, add tests to `code:`/`unit-tests:`/`bdd:`, and note new known
   gaps. If the change adds a genuinely new feature, create a new entry here **and**
   register it in `features:` (unless it is an infra-only surface with no UI route).
3. Run the architecture suite so the graph stays consistent:
   `uv run --project backend --no-sync pytest backend/tests/architecture/test_product_map*.py -q`
4. Update the PRD if the change introduces new behaviour (see CONTRIBUTING.md).

## Index — manifest feature registry (single source of truth)

Every registered manifest feature, its description, and the routes that reference it.
Fresh entries for these features are added to the graph below as behaviour trackers.

### Build
- **feat-dashboard** - Home dashboard and metrics overview (Saved Views deferred from the MVP nav — hidden via private_preview; see FAR-546) - routes: `/`
- **feat-pipelines** - Visual pipeline editor and composite editor (Node Categories deferred from the MVP nav — hidden via private_preview; see FAR-545) - routes: `/library/:id/create-pipeline`, `/pipelines`, `/pipelines/copy`, `/pipelines/:id/editor`, `/composites/:id/editor`
- **feat-router** - Router decision nodes and branching in the execution graph (FAR-402 P1 / F2-A) - routes: `/pipelines`
- **feat-library** - Reusable pipeline templates and the template library - routes: `/library/:id/create-pipeline`, `/library`
- **feat-runs** - Run execution, history, and detail (Output Diff deferred from the MVP nav — hidden via private_preview; see FAR-542) - routes: `/runs`, `/runs/:id`
- **feat-lifecycle-maps** - Lifecycle maps and stage workflows - routes: `/lifecycle-maps`, `/lifecycle-maps/:id/editor`, `/lifecycle-maps/:id`

### Monitor
- **feat-observability** - Error dashboard and observability exports (Error Forwarders and Browser Monitoring deferred from the MVP nav — hidden via private_preview; see FAR-547 / FAR-543) - routes: `/settings/observability`, `/admin/errors`, `/admin/errors/:id`
- **feat-notifications** - Notifications, email delivery, and notification logs - routes: `/notifications`, `/settings/email`, `/admin/notification-delivery`
- **feat-costs** - Cost tracking, spend limits, cost controls, and cost components - routes: `/admin/costs`, `/admin/costs/limits`, `/admin/costs/controls`, `/admin/costs/components`
- **feat-hitl** - Human-in-the-loop approval gates and review - routes: `/settings/hitl-review`
- **feat-analytics** - Reporting and analytics over runs, costs, and facts - routes: `/analytics`

### Improve
- **feat-evals** - Evaluation editor and proposal queue - routes: `/evals/editor`, `/evals/proposals`
- **feat-variants** - Variant comparison and AB test models - routes: `/variants/compare`, `/variants/compare/:batchId`, `/variants/ab-test`

### Configure
- **feat-schemas** - Typed JSON schemas, schema editor, inference, and parameter schemas - routes: `/schemas`, `/schemas/editor/:id`, `/schemas/infer`, `/admin/parameter-schemas`
- **feat-model-backends** - Model backend management and setup - routes: `/admin/model-backends`, `/setup/model-backend/:id`
- **feat-remy** - Remy assistant configuration and skills - routes: `/admin/remy`, `/settings/remy`, `/remy`
- **feat-mcp** - Model Context Protocol tool configuration - routes: `/settings/mcp`
- **feat-guardrails** - Guardrail policies - routes: `/settings/guardrails`
- **feat-connectors** - External tool connectors - routes: `/admin/connectors`
- **feat-environments** - Environment profiles and run environments - routes: `/admin/environments`, `/admin/sandbox-concurrency`, `/environment-profiles`, `/environment-profiles/new`, `/environment-profiles/:id/edit`
- **feat-triggers** - Manual, webhook, and scheduled triggers - routes: `/settings/triggers`

### Admin
- **feat-teams** - Users, teams, and role-based access - routes: `/settings/teams`, `/admin/users`
- **feat-org** - Organization settings (Feature Flags deferred from the MVP nav — hidden via private_preview; see FAR-548) - routes: `/admin/org`
- **feat-sso** - Single sign-on (SSO) - routes: `/settings/sso`
- **feat-plugins** - Plugin registry (deferred from the MVP nav — hidden via private_preview; see FAR-544) - routes: `/admin/plugins`
- **feat-audit** - Audit trail and audit log - routes: `/admin/audit`
- **feat-feedback** - Feedback inbox - routes: `/feedback/inbox`

### System
- **feat-license** - Feature licensing and plan tiers - routes: `/settings/license`
- **feat-runtime** - Runtime configuration, rate limits, retention, and sandbox concurrency (Runtime Config and Rate Limits deferred from the MVP nav — hidden via private_preview; see FAR-549 / FAR-550) - routes: `/admin/housekeeping`, `/admin/environments`, `/admin/run-retention`, `/admin/sandbox-concurrency`
- **feat-system-config** - System-level configuration administration - routes: `/admin/system/config`
- **feat-system-orgs** - System-level organization administration - routes: `/admin/system/orgs`
- **feat-product-analytics** - Product usage and adoption analytics for system administrators - routes: `/admin/product-analytics`

### Auth & onboarding
- **feat-auth** - OAuth authorization, sessions, and user profile - routes: `/oauth/authorize`, `/admin/my-profile`
- **feat-onboarding** - First-run onboarding wizard for new users and organizations - routes: `/onboarding`

## Index — feature graph entries

Behaviour-tracker entries in this directory. Infra-only surfaces (no UI route in
`manifest.yaml`) are tracked here as well, keyed by their `feat-*` id.

> **Known gaps:** registered manifest features that still lack a behaviour-tracker
> entry here are listed once, at the end of this file — see
> [Known graph gaps](#known-graph-gaps). Keeping a single list stops the two copies
> drifting apart (they did: the older inline copy still named features whose trackers
> had already landed in the index below).
>
> **Closed this walk:** `feat-environments` gained its behaviour-tracker entry
> (`environments/environments.md`) — see the graph index above.

### Admin
- [feat-product-analytics](admin/product-analytics.md) => PRD N/A

### Analytics
- [feat-analytics](analytics/analytics.md) => PRD N/A

### Audit
- [feat-audit](audit/audit-trail.md) => PRD N/A

### Auth and Security
- [feat-core-oidc-integration](auth/oidc-integration.md) => PRD 9.4
- [feat-core-saml-integration](auth/saml-integration.md) => PRD 9.4
- [feat-auth-jwt-auth](auth/jwt-auth.md) => PRD N/A
- [feat-auth-sso-provider-ui](auth/sso-provider-ui.md) => PRD 9.4
- [feat-onboarding](auth/onboarding.md) => PRD N/A

### Configure
- [feat-guardrails](configure/guardrails.md) => PRD N/A
- [feat-mcp](configure/mcp.md) => PRD N/A
- [feat-environments](environments/environments.md) => PRD N/A

### Core Platform
- [feat-hitl](hitl/hitl-gates.md) => PRD N/A
- [feat-core-runtime-provider-core](core/runtime-provider-core.md) => PRD 6
- [feat-core-db-abstraction-core](core/db-abstraction-core.md) => PRD N/A
- [feat-core-run-context](core/run-context.md) => PRD N/A
- [feat-core-ssrf](core/ssrf.md) => PRD N/A
- [feat-core-secrets-backend](core/secrets-backend.md) => PRD N/A

### Improve
- [feat-evals](improve/evals.md) => PRD N/A
- [feat-variants](improve/variants.md) => PRD N/A
- [feat-feedback](improve/feedback.md) => PRD 8.20

### Improve
- [feat-evals](improve/evals.md) => PRD N/A

### Infra
- [feat-infra-health](infra/health-checks.md) => PRD N/A

### Schema & Data
- [feat-schemas](schemas/schemas.md) => PRD N/A

### Build & Pipelines
- [feat-pipelines](pipelines/pipelines.md) => PRD N/A
- [feat-pipelines-pipeline-versioning](pipelines/snapshot-versioning.md) => PRD 8.13
- [feat-pipelines-pipeline-diff-rollback](pipelines/pipeline-diff-rollback.md) => PRD 8.13
- [feat-router (Router & HITL nodes)](pipelines/router-hitl-nodes.md) => PRD N/A

### Configure
- [feat-model-backends](configure/model-backends.md) => PRD N/A
- [feat-mcp](configure/mcp.md) => PRD N/A

### Notifications
- [feat-notifications](notifications/notifications.md) => PRD N/A

### Library
- [feat-library](library/library.md) => PRD N/A

### Licensing
- [feat-license](licensing/license.md) => PRD N/A

### Observability
- [feat-observability](observability/observability.md) => PRD N/A

### Teams
- [feat-teams](teams/teams.md) => PRD N/A
- [feat-teams-org-entity](teams/org-entity.md) => PRD 9.1, 6.2

### Triggers
- [feat-triggers](triggers/trigger-engine.md) => PRD N/A

### Build
- [feat-dashboard](build/dashboard.md) => PRD 8.20
- [feat-runs](build/runs.md) => PRD N/A

### Monitor
- [feat-costs](monitor/costs.md) => PRD 8.10, 9.3

### Configure
- [feat-remy](configure/remy.md) => PRD 8.23

### Admin
- [feat-plugins](admin/plugins.md) => PRD N/A

## Known graph gaps

Registered manifest features with no `docs/product-map/` behaviour-tracker yet (the
graph stays honest about what remains untracked; each improve-architecture product-map
walk closes a batch): `feat-auth`, `feat-connectors` (behaviours are tracked inline in
`manifest.yaml`), `feat-lifecycle-maps`, `feat-org`,
`feat-runtime`, `feat-sso`, `feat-system-config`, `feat-system-orgs` —
8 registered features remain untracked after this walk.
