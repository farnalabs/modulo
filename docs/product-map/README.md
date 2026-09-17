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
- **feat-library** - Reusable pipeline templates, the community library, and library collections (collection authoring/publishing is flag-gated behind `library_collection`; see FAR-760) - routes: `/library/:id/create-pipeline`, `/library`, `/library/collections/new`, `/library/collections/:id`
- **feat-library-collections** - Install, uninstall, and manage library collection installs into runnable org entities (flag-gated behind `library_collection`; see FAR-760 / FAR-762 / FAR-764) - routes: `/library/collections/new`, `/library/collections/:id`
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
- **feat-variants** - Variant comparison and batch runs - routes: `/variants/compare`, `/variants/compare/:batchId`

### Configure
- **feat-schemas** - Typed JSON schemas, schema editor, inference, and parameter schemas - routes: `/schemas`, `/schemas/editor/:id`, `/schemas/infer`, `/admin/parameter-schemas`
- **feat-model-backends** - Model backend management and setup - routes: `/admin/model-backends`, `/setup/model-backend/:id`
- **feat-remy** - Remy assistant configuration and skills - routes: `/admin/remy`, `/settings/remy`, `/remy`
- **feat-mcp** - Model Context Protocol tool configuration - routes: `/settings/mcp`
- **feat-guardrails** - Guardrail policies - routes: `/settings/guardrails`
- **feat-connectors** - External tool connectors - routes: `/admin/connectors`
- **feat-environments** - Environment profiles and run environments (canonical UI is the Runners page; the `/environment-profiles*` and `/admin/environments` deep links redirect there — FAR-591 D5) - routes: `/admin/runners/profiles`, `/admin/runners/profiles/new`, `/admin/runners/profiles/:id/edit`, `/admin/runners/concurrency`
- **feat-triggers** - Manual, webhook, and scheduled triggers - routes: `/settings/triggers`
- **feat-apply** - `modulo apply` declarative configuration CLI (FAR-681) - routes: `/schemas`, `/admin/model-backends`, `/pipelines`, `/settings/triggers`

### Admin
- **feat-teams** - Users, teams, and role-based access - routes: `/settings/teams`, `/admin/users`
- **feat-org** - Organization settings (Feature Flags deferred from the MVP nav — hidden via private_preview; see FAR-548) - routes: `/admin/org`
- **feat-sso** - Single sign-on (SSO) - routes: `/settings/sso`
- **feat-plugins** - Plugin registry (deferred from the MVP nav — hidden via private_preview; see FAR-544). Supports connector, model-backend, eval, and schema-type entry point groups via `modulo.connectors`, `modulo.model_backends`, `modulo.evals`, and `modulo.schema_types` - routes: `/admin/plugins`
- **feat-audit** - Audit trail and audit log - routes: `/admin/audit`
- **feat-feedback** - Feedback inbox - routes: `/feedback/inbox`

### System
- **feat-license** - Feature licensing and plan tiers - routes: `/settings/license`
- **feat-runtime** - Runtime configuration, rate limits, retention, and sandbox concurrency on the Runners page (Runtime Config and Rate Limits deferred from the MVP nav — hidden via private_preview; see FAR-549 / FAR-550) - routes: `/admin/housekeeping`, `/admin/run-retention`, `/admin/runners/profiles`, `/admin/runners/concurrency`
- **feat-system-config** - System-level configuration administration - routes: `/admin/system/config`
- **feat-system-orgs** - System-level organization administration - routes: `/admin/system/orgs`
- **feat-product-analytics** - Product usage and adoption analytics for system administrators - routes: `/admin/product-analytics`

### Auth & onboarding
- **feat-auth** - OAuth authorization, sessions, and user profile - routes: `/accept-invite`, `/oauth/authorize`, `/admin/my-profile`
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
> **Closed this walk:** `feat-environments`, `feat-auth`, `feat-sso`, `feat-org`,
> `feat-runtime`, `feat-system-config`, `feat-system-orgs`, `feat-connectors`,
> `feat-lifecycle-maps`, `feat-feedback`, `feat-hitl`, `feat-router`, `feat-library`,
> `feat-license`, `feat-mcp`, `feat-model-backends`, `feat-onboarding`, `feat-remy`,
> `feat-schemas`, `feat-teams`, `feat-evals`, `feat-guardrails`, `feat-variants`,
> `feat-product-analytics`, `feat-pipelines`, `feat-runs`, `feat-dashboard`,
> `feat-costs`, `feat-notifications`, `feat-observability`, `feat-plugins`,
> `feat-triggers`, `feat-analytics`, `feat-audit` gained their behaviour-tracker
> entries — see the graph index below. The 2026-09-10 walk added `feat-apply`
> (registered in the manifest by FAR-681 but never tracked). A follow-up
> 2026-09-10 walk reconciled this registry's `routes:` lists with the manifest
> after the FAR-591 D5 Runners-page rename and the FAR-760 library collections
> ship (`test_graph_root_registry_routes_match_manifest` now guards the lists).
> The 2026-09-13 walk wired `library/contribute.feature` into the executing BDD
> suite (`steps/test_library_contributions.py`) and shrunk
> `_ORPHANED_BDD_FEATURES` by one, closing `feat-library`'s contribution
> BDD gap (`library.md`). The 2026-09-14 walk wired
> `licensing/feature_flag_inspection.feature` into the executing BDD suite
> (`steps/test_feature_flag_inspection.py`), shrunk `_ORPHANED_BDD_FEATURES` by
> one more, and closed `feat-license`'s feature-flag-inspection BDD gap
> (`licensing/license.md`). A second 2026-09-14 walk wired
> `rate_limiting/rate_limiting.feature` into the executing BDD suite (steps in
> `test_rate_limiting.py`), shrunk `_ORPHANED_BDD_FEATURES` by one more, and
> closed `feat-runtime`'s rate-limit middleware-integration BDD gap
> (`system/runtime.md`). A third 2026-09-14 walk wired
> `connectors/sentry.feature` into the executing BDD suite
> (`steps/test_sentry_connector.py`), shrunk `_ORPHANED_BDD_FEATURES` by one
> more, and closed `feat-connectors`'s Sentry per-connector BDD gap
> (`configure/connectors.md`). A fourth 2026-09-14 walk wired
> `connectors/pagerduty.feature` into the executing BDD suite
> (`steps/test_pagerduty_connector.py`), shrunk `_ORPHANED_BDD_FEATURES` by one
> more, and closed `feat-connectors`'s PagerDuty per-connector BDD gap
> (`configure/connectors.md`). A fifth 2026-09-14 walk wired
> `connectors/grafana.feature` into the executing BDD suite
> (`steps/test_grafana_connector.py`), shrunk `_ORPHANED_BDD_FEATURES` by one
> more, and closed `feat-connectors`'s Grafana per-connector BDD gap
> (`configure/connectors.md`). A sixth 2026-09-14 walk wired
> `connectors/buildkite.feature` into the executing BDD suite
> (`steps/test_buildkite_connector.py`), shrunk `_ORPHANED_BDD_FEATURES` by one
> more, and closed `feat-connectors`'s Buildkite per-connector BDD gap
> (`configure/connectors.md`). A seventh 2026-09-15 walk wired
> `connectors/circleci.feature`, `connectors/jenkins.feature`,
> `connectors/teamcity_connector.feature` and
> `connectors/opsgenie_connector.feature` into the executing BDD suite
> (`steps/test_circleci_connector.py`, `steps/test_jenkins_connector.py`,
> `steps/test_teamcity_connector.py`, `steps/test_opsgenie_connector.py`),
> shrunk `_ORPHANED_BDD_FEATURES` by four, and closed `feat-connectors`'s
> CircleCI / Jenkins / TeamCity / Opsgenie per-connector BDD gaps
> (`configure/connectors.md`). A 2026-09-16 walk wired
> `connectors/azure_key_vault.feature` and `connectors/azure_pipelines.feature`
> into the executing BDD suite (`steps/test_azure_key_vault_connector.py`,
> `steps/test_azure_pipelines_connector.py`), shrunk `_ORPHANED_BDD_FEATURES`
> by two, and closed `feat-connectors`'s Azure Key Vault / Azure Pipelines
> per-connector BDD gaps (`configure/connectors.md`).
>
> **Closed this walk (2026-09-16):** wired `connectors/dropbox_paper.feature`
> into the executing BDD suite (`steps/test_dropbox_paper_connector.py`),
> shrunk `_ORPHANED_BDD_FEATURES` by one more, and closed `feat-connectors`'s
> Dropbox Paper per-connector BDD gap (`configure/connectors.md`).
>
> **Closed this walk (2026-09-16):** wired the four remaining connector orphans
> `connectors/azure_repos.feature`, `connectors/discord.feature`,
> `connectors/microsoft_teams.feature` and `connectors/sharepoint.feature`
> into the executing BDD suite (`steps/test_azure_repos_connector.py`,
> `steps/test_discord_connector.py`, `steps/test_microsoft_teams_connector.py`,
> `steps/test_sharepoint_connector.py`), shrunk `_ORPHANED_BDD_FEATURES` by
> four, and closed `feat-connectors`'s Azure Repos / Discord / Microsoft Teams /
> SharePoint per-connector BDD gaps (`configure/connectors.md`).
>
> **Closed this walk (2026-09-16):** wired the last connector orphan
> `connectors/swappable_binding.feature` into the executing BDD suite from the
> new `steps/test_pipeline_connector_binding.py` (driving the real
> `extract_connector_bindings` + `GraphValidator.validate_definition`
> connector-binding surface) and closed `feat-connectors`'s swappable-binding
> BDD gap (`configure/connectors.md`). Wired the last pipeline-validation
> orphan `pipelines/validation.feature` into the executing BDD suite from the
> new `steps/test_pipeline_graph_validation.py` (driving the real
> `GraphValidator.validate_definition` save-time topology/connector binding
> checks) and deleted the redundant duplicate
> `pipelines/pipeline_config_validation.feature`, closing `feat-pipelines`'s
> graph/config-validation BDD gap (`pipelines/pipelines.md`).
> `_ORPHANED_BDD_FEATURES` is now empty — no orphaned feature files remain.
>
> **Closed this walk (2026-09-17):** added DELETE coverage to the executing
> system-admin BDD suite. `system_admin_config.feature` gained successful-delete,
> unknown-key-404 and regular-admin-403 scenarios, closing `feat-system-config`'s
> "No BDD for DELETE system config" gap (`system/system-config.md`).
> `system_admin_orgs.feature` gained the same three DELETE scenarios, closing the
> "No BDD for DELETE org" half of `feat-system-orgs`'s gap
> (`system/system-orgs.md`); the narrower org-license-management BDD gap remains
> tracked in that entry.
>
> **Closed this walk (2026-09-17):** closed the org-license-management half of
> `feat-system-orgs`'s BDD gap too. `system_admin_orgs.feature` gained 10
> license-management scenarios (GET org-key resolution / system fallback /
> invalid-stored-key fallback / missing-org 404; PUT valid-200 / invalid-422 /
> missing-org 404 before verification; DELETE clears the key / missing-org 404;
> and regular-org-admin 403 across GET/PUT/DELETE), driving the real
> `admin_orgs.py` license surface (`require_target_org_role` gating,
> `_resolve_org_license`, `_verify_license_key`, and the FOR-UPDATE locked
> read-modify-write). Removed the tracked known gap — `feat-system-orgs` is now
> fully BDD-covered (`system/system-orgs.md`).
>
> **Closed this walk (2026-09-17):** closed `feat-hitl`'s "No executing BDD
> surface for modify-then-approve, `human_only` refusal, or overdue warnings"
> gap (`hitl/hitl-gates.md`). Registered the new `gate_policies.feature` into
> the executing BDD suite (`steps/test_hitl_gate_policies.py`), driving the
> real `/approve-with-modification` route (200 + modified output in the resume
> payload, 422 without a claim token, 410 with an expired one), the real REST
> `human_only` denial verdict (API-key principal on a human_only gate → 403),
> and the real `get_overdue_claims` aggregation (warning vs escalated).
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-17):** closed `feat-auth`'s "No dedicated BDD for
> `/me` password-change forced flow" gap. `auth/change_password.feature` gained
> the "Forced password change clears the admin-reset flag in the same
> transaction" scenario (driven by `steps/test_change_password.py`), asserting
> the real `PUT /api/v1/me/password` route clears the `must_change_password`
> flag — the flag App.vue's forced-change gate arms on — in the same transaction
> as the hash swap (`auth/auth.md`).

### Admin
- [feat-product-analytics](admin/product-analytics.md) => PRD N/A
- [feat-plugins](admin/plugins.md) => PRD N/A

### Analytics
- [feat-analytics](analytics/analytics.md) => PRD N/A

### Audit
- [feat-audit](audit/audit-trail.md) => PRD N/A

### Auth and Security
- [feat-auth](auth/auth.md) => PRD N/A
- [feat-core-oidc-integration](auth/oidc-integration.md) => PRD 9.4
- [feat-core-saml-integration](auth/saml-integration.md) => PRD 9.4
- [feat-auth-jwt-auth](auth/jwt-auth.md) => PRD N/A
- [feat-sso](auth/sso-provider-ui.md) => PRD 9.4
- [feat-onboarding](auth/onboarding.md) => PRD N/A

### Build
- [feat-dashboard](build/dashboard.md) => PRD 8.20
- [feat-runs](build/runs.md) => PRD N/A

### Configure
- [feat-apply](configure/apply.md) => PRD N/A
- [feat-connectors](configure/connectors.md) => PRD N/A
- [feat-guardrails](configure/guardrails.md) => PRD N/A
- [feat-mcp](configure/mcp.md) => PRD N/A
- [feat-model-backends](configure/model-backends.md) => PRD N/A
- [feat-remy](configure/remy.md) => PRD 8.23

### Core Platform
- [feat-core-runtime-provider-core](core/runtime-provider-core.md) => PRD 6
- [feat-core-db-abstraction-core](core/db-abstraction-core.md) => PRD N/A
- [feat-core-run-context](core/run-context.md) => PRD N/A
- [feat-core-ssrf](core/ssrf.md) => PRD N/A
- [feat-core-secrets-backend](core/secrets-backend.md) => PRD N/A

### Environments
- [feat-environments](environments/environments.md) => PRD N/A

### HITL
- [feat-hitl](hitl/hitl-gates.md) => PRD N/A

### Improve
- [feat-evals](improve/evals.md) => PRD N/A
- [feat-variants](improve/variants.md) => PRD N/A
- [feat-feedback](improve/feedback.md) => PRD 8.20

### Infra
- [feat-infra-health](infra/health-checks.md) => PRD N/A

### Library
- [feat-library](library/library.md) => PRD N/A
- [feat-library-collections](library/library-collections.md) => PRD N/A

### Licensing
- [feat-license](licensing/license.md) => PRD N/A

### Monitor
- [feat-costs](monitor/costs.md) => PRD 8.10, 9.3
- [feat-observability](observability/observability.md) => PRD N/A

### Notifications
- [feat-notifications](notifications/notifications.md) => PRD N/A

### Pipelines
- [feat-pipelines](pipelines/pipelines.md) => PRD N/A
- [feat-pipelines-pipeline-versioning](pipelines/snapshot-versioning.md) => PRD 8.13
- [feat-pipelines-pipeline-diff-rollback](pipelines/pipeline-diff-rollback.md) => PRD 8.13
- [feat-router](pipelines/router-hitl-nodes.md) => PRD N/A
- [feat-lifecycle-maps](pipelines/lifecycle-maps.md) => PRD N/A

### Schema & Data
- [feat-schemas](schemas/schemas.md) => PRD N/A

### System
- [feat-org](admin/org.md) => PRD N/A
- [feat-runtime](system/runtime.md) => PRD N/A
- [feat-system-config](system/system-config.md) => PRD N/A
- [feat-system-orgs](system/system-orgs.md) => PRD N/A

### Teams
- [feat-teams](teams/teams.md) => PRD N/A
- [feat-teams-org-entity](teams/org-entity.md) => PRD 9.1, 6.2

### Triggers
- [feat-triggers](triggers/trigger-engine.md) => PRD N/A

## Known graph gaps

All registered manifest features now have a `docs/product-map/` behaviour-tracker
entry. No untracked features remain.
