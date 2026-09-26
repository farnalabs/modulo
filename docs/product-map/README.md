# Product Map

The product map is the single inventory of every shipped feature in Modulo. It has two
layers:

1. **`frontend/src/manifest.yaml`** — the machine-readable product surface (ADR 008 — Core
   Shared Manifest). It registers every UI route, its `product_map: [feat-*]` references,
   sidebar grouping, permissions/tiers, testable `data-testid` elements, and the
   `features:` registry of allowed feature ids. The backend serves it at
   `/api/v1/manifest`, the frontend router/nav consume it at build time, and Assistant's
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
of these two places — otherwise it is invisible to Assistant and to this graph.

## Entry format

```markdown
---
id: feat-<domain>-<feature>        # unique feature id (match the manifest registry)
prd: N.N                           # PRD section (N/A for infra-only surfaces)
adr: [ADR <number> (<slug>)]                     # governing ADRs (optional)
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
- **feat-dashboard** - Home dashboard and metrics overview (Saved Views ship as the private_preview `/admin/views` CRUD surface behind `view_modes`; the apply-to-list `ViewToggle` is not yet wired into list pages — see FAR-546) - routes: `/`
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
- **feat-assistant** - Modulo assistant configuration and skills - routes: `/admin/assistant`, `/settings/assistant`, `/assistant`
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
> `feat-license`, `feat-mcp`, `feat-model-backends`, `feat-onboarding`, `feat-assistant`,
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
>
> **Closed this walk (2026-09-17):** closed `feat-audit`'s "No BDD scenario for
> append-only tampering" gap (`audit/audit-trail.md`). Registered the new
> `audit/append_only.feature` into the executing BDD suite
> (`steps/test_audit_append_only.py`), driving the real application-layer
> append-only guard — `register_append_only_guard()` plus its SQLAlchemy
> `before_update` / `before_delete` listeners — against persisted `AuditEvent`
> and `ErrorEvent` rows in an in-memory engine: UPDATE and DELETE are rejected
> on both models (`AppendOnlyViolationError` naming the event id and mutation)
> while a plain INSERT is not blocked. The placeholder "Audit events are
> immutable" scenario in `event_recording.feature` (which only asserted a
> generic 4xx from a nonexistent PATCH route) was removed with its dummy steps.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-17):** closed `feat-triggers`'s "Slack app-mention
> triggering is unit-tested only" gap (`triggers/trigger-engine.md`).
> Registered the new `triggers/slack_app_mention.feature` into the executing
> BDD suite (`steps/test_slack_app_mention_triggers.py`), driving the real
> `slack_app_mention.py` seams — Slack signed-request verification
> (HMAC-SHA256 `X-Slack-Signature` + ±300s `X-Slack-Request-Timestamp` replay
> window, wrong-secret and expired-timestamp refusals), the `url_verification`
> challenge echo, envelope parsing / payload mapping, Slack `event_id`
> deduplication, concurrency-queuing, pipeline rate limiting, and the
> advisory-lock busy refusal — with every delivery audited to a TriggerEvent
> (`accepted` / `hmac_failed` / `deduplicated` / `event_type_not_accepted` /
> `parse_failed` / `concurrency_limit_reached` / `rate_limited`).
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-18):** closed `feat-dashboard`'s "No BDD for
> `/summary` / `/trends` / `/daily-run-counts`" gap
> (`build/dashboard.md`). Registered `dashboard/dashboard_summary.feature`
> and `dashboard/daily_run_counts.feature` into the executing BDD suite from
> the colocated `features/dashboard/test_dashboard_summary_steps.py` /
> `test_daily_run_counts_steps.py`, driving the real `dashboard_summary` and
> `daily_run_counts` route handlers (mock-session dispatch on SQL text):
> summary widget shape, per-team metrics, eval pass-rate detail, 7-day trend,
> config warnings, idle folding of `pending`/`claimed` into `idle` with
> single-counted `total_runs`, the additive `days` `period` block, and the
> 1..90 `days` bound; day/status-keyed daily counts, cross-status
> accumulation, default 30 and custom `days` windows, and the 1..365
> `days` bound. `/trends` was already covered by `hitl_trends.feature`.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-18):** closed `feat-core-ssrf`'s "No BDD
> feature files" gap (`core/ssrf.md`). Registered `security/ssrf_guard.feature`
> into the executing BDD suite from the new `steps/test_ssrf_guard.py`,
> driving the real `modulo.core.ssrf` seams network-free — literal-IP
> fail-closed blocking (loopback / private / link-local metadata / CGNAT /
> Aliyun metadata / current-network), pre-DNS syntax rejection (scheme,
> userinfo, hostname, port, non-canonical IP literals), sync + async
> validation with the any-blocked and empty-resolution fail-closed semantics,
> the global + tenant-scoped allowlists with the non-negotiable floor,
> `resolve_pinned_ip`'s original-hostname / full-IP-set target shape, the
> pinned transport's `UnpinnedHostError` refusal, and the pinned-client
> `transport`-kwarg rejection. `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-18):** closed `feat-auth-jwt-auth`'s "No standalone
> BDD feature file" gap (`auth/jwt-auth.md`). Registered
> `auth/jwt_auth_crypto.feature` into the executing BDD suite from the new
> `steps/test_jwt_auth_crypto.py`, driving the real `modulo.auth.jwt` seams
> network-free and DB-free — access-token mint/decode round-trip (identity, role,
> tenant org, `client_kind`), wrong-secret / tampered-signature / expired /
> `alg=none` / missing-subject rejections, the purpose-isolation matrix (access /
> `ws` / `refresh` accepted only under their own purpose, the
> `refresh_access_token` rotation seam refusing a `ws` token), refresh-rotation
> propagation of identity + credential class into the new access token,
> `decode_claim_token` HITL-gate run/gate scoping with a wrong-gate refusal, and
> the legacy no-`client_kind` token decoding as `browser`.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-18):** closed `feat-feedback`'s "No standalone BDD
> step file for the inbox/proposals endpoints" gap (`improve/feedback.md`).
> Registered `eval/feedback_inbox.feature` into the executing BDD suite from the
> new `steps/test_feedback_inbox.py`, driving the real
> `modulo/api/routes/feedback.py` seams with only the FeedbackManager CRUD,
> RLS and audit seams patched — inbox list with pipeline-name enrichment + the
> type/status filter passthrough, inbox-item detail, the `/inbox/{id}/review`
> workflow (`mark_reviewed` → resolved, `dismiss` → dismissed,
> `create_correction_run` → spawned correction run, invalid action → 422, missing
> record → 404), `/feedback/{id}/detect-gap` eval-gap reporting, the proposals
> queue, and `/proposals/{id}/publish` (201 pipeline/node-scoped `EvalDefinition`
> + resolved transition, 422 non-gap, 409 non-pending, 404 missing).
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-19):** closed `feat-core-secrets-backend`'s
> "No BDD feature files" gap (`core/secrets-backend.md`). Registered
> `infra/secrets_backend.feature` into the executing BDD suite from the new
> `steps/test_secrets_backend.py`, driving the REAL
> `modulo.core.secrets_backend` seams network-free and DB-free — the
> `FernetSecretsBackend` persists/reads real rows in an in-memory aiosqlite
> engine (round-trip, in-place upsert, delete, whitespace-normalised keys),
> organisation scoping via the real `WHERE organisation_id = :oid` SQL the
> backend emits (org A reads what org A wrote; org B gets `KeyError`),
> `validate_key` / `_read_org_id_from_session` fail-closed rejections (blank
> key / no session / missing RLS context), no-downtime rotation fallback +
> alien/corrupt ciphertext `ValueError`, and the real factory default /
> unknown-name / unlicensed-`vault`-fallback logic (forced exactly as the unit
> suite does). `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-19):** closed `feat-apply`'s "No BDD feature file"
> gap (`configure/apply.md`). Registered `cli/apply.feature` into the executing
> BDD suite from the new `steps/test_apply_cli.py`, driving the REAL
> `modulo.cli.apply` seams network-free and DB-free — the loader / `ApplyConfig`
> validators (single + multi-document YAML merge, duplicate names within and
> across documents, empty file, trigger forward-references), refs-only secrets
> (`resolve_secret_refs`), the plan engine (created / unchanged / updated /
> blocked, incl. provider mismatch + immutable schema versions), the executor
> against a respx-mocked API (dry-run report, real-apply block on an unresolved
> env ref, health-check verification moving a stored-but-broken backend to
> `failed`), and the `--diff` drift surface (read-only `mode=drift` report,
> node-level `drift_detail`, `has_drift` gate, `drift create` / `drift summary`
> rendering). `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-19):** closed `feat-analytics`'s "No dedicated
> BDD feature files for `/analytics`" gap (`analytics/analytics.md`).
> Registered `analytics/query.feature` into the executing BDD suite from the
> colocated `features/analytics/test_analytics_query_steps.py`, driving the
> REAL `modulo/api/routes/analytics.py` routes with only the four advisory
> service functions (`run_analytics_query`, `run_concurrency_query`,
> `export_facts`, `run_guardrail_scorecard`) patched — the `/query` envelope +
> freshness indicators, the repeated `pipeline_id` A/B composition (both ids
> asserted to reach the service) and dimension echo, FastAPI-level validation
> (malformed date / `limit` bound → 422), the typed error mapping (inverted
> range → 422, rate limit → 429, statement timeout → 503), the org-context
> requirement (no org context → 403), the `analytics_page` feature gate
> (disabled → 402), an explicit unauthenticated 401 (HTTPBearer with no
> `Authorization` header), the JSON + CSV export surfaces (CSV carries a
> `Content-Disposition: attachment`), the concurrency slot-utilisation series
> and the advisory-only guardrail scorecard. `_ORPHANED_BDD_FEATURES` stays
> empty.
>
> **Closed this walk (2026-09-20):** closed `feat-lifecycle-maps`'s "No BDD for
> lifecycle map journey detail view" gap (`pipelines/lifecycle-maps.md`).
> Registered the new `lifecycle_maps/journeys.feature` into the executing BDD
> suite (steps in `steps/test_lifecycle_maps.py`), driving the real
> `/journeys`, `/journeys/{kind}/{ref}` and `/journeys/self-report` routes with
> only the DB seam functions patched: journey detail returns the current stage +
> run history shape, unknown journey → 404, keyset-paginated journey list with
> `next_cursor` + `ref`-filter pass-through, and the advisory self-report
> accepted/unmatched/rejected counters with the real
> `validate_and_normalise_reported_refs` (a malformed entry is counted, never a
> whole-request 422) plus a matched journey advanced with `status="complete"`.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-20):** closed `feat-variants`'s "BDD scenarios
> tagged `@awaiting-implementation`" gap (`improve/variants.md`). The
> sequential-order scenario now drives the REAL `run_variant_batch` seam (runs
> created in variant insertion order under one `batch_id`) and the eval-coverage
> draft was re-anchored to the REAL `get_coverage_gaps` seam; the two per-node
> eval-score / per-token breakdown comparison drafts were removed — they assert
> a wire shape the product does not ship (`get_batch_compare`'s per-run
> `eval_pass_rate` / `eval_count` / `total_tokens` / `total_cost_usd` +
> frozen-snapshot override-diff contract is already locked by the batch-scope
> comparison scenarios). No `@awaiting-implementation` scenarios remain.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-20):** closed `feat-model-backends`'s
> "no standalone model-backend health endpoint exists" `@awaiting-implementation`
> gap (`configure/model-backends.md`). The four `model_backends/health_check.feature`
> scenarios now drive the REAL `POST /api/v1/model-backends/{id}/health-check`
> route (PRD 8.1 re-check: decrypt stored credential, re-ping provider, persist
> and clear a sticky `last_health_check_error`) with only the DB lookup seam
> (`get_model_backend`), the secret-decryption seam
> (`decode_stored_secret_scoped`) and the post-commit persist seam
> (`_run_health_check_on_save_and_persist`) patched: healthy → `healthy`,
> invalid API key → `unhealthy` + auth-failure detail, other-org caller →
> 404 before any check, and the deterministic stub provider → `healthy`.
> Removed the four scenarios from `PINNED_AWAITING_IMPLEMENTATION`.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-21):** closed `feat-sso`'s "OIDC SSO flows
> (per-org OIDC, per-provider discovery) are covered by unit tests only;
> equivalent multi-org real-DB (RLS) integration regression not yet written"
> deferral (`auth/sso-provider-ui.md`). Registered the new
> `backend/tests/integration/auth/test_oidc_rls_resolution.py` (mirroring
> `test_saml_rls_resolution.py`), driving the REAL `modulo.auth.sso`
> `_resolve_oidc_provider` seam and the REAL `GET /oidc/{provider}/login` route
> through the real `modulo_system` (BYPASSRLS) / `modulo_app` (NOBYPASSRLS)
> roles — the system leg resolves an OIDC provider owned by a NON-first org,
> unknown slugs fail closed all-None (never RuntimeError→500), the app fallback
> resolves first-org-only inside a scoped transaction, an unbound app session
> sees zero OIDC providers, and a cross-org provider slug 307s to the IdP while
> an unknown slug is a 400 not a 500. Fixed the latent FAR-1058-class defect the
> regression exposes: `_resolve_oidc_provider` now opens its own
> `session.begin()` for the app fallback when the caller has no active
> transaction (previously it called `_set_default_rls_org` outside any
> transaction, matching the SAML bug FAR-1058 fixed — a provider read on a
> transaction-less autobegin=False app session raised `RuntimeError` → 500).
> Demoted the deferral and added the behaviour lines to
> `frontend/src/manifest.yaml` `feat-sso`. `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-20):** closed `feat-connectors`'s "No BDD for
> connector CRUD lifecycle (create/update/delete via admin API)" gap
> (`configure/connectors.md`). Registered the new
> `connectors/connector_crud.feature` into the executing BDD suite from the new
> `steps/test_connector_crud.py`, driving the real `/api/v1/connectors`
> create / get / list / PATCH / delete routes with only the DB CRUD + RLS
> seams patched (the TestClient + mock-org-session pattern of the
> `test_connector_endpoint.py` unit suite): 9 scenarios — 201 create with
> credentials Fernet-encrypted at rest and never echoed, 422 malformed REST
> credentials / invalid REST `on_unknown` config at the boundary, 200
> individual + paginated retrieval (redacted), PATCH re-encrypting fresh
> credentials, 204 DELETE, and the org-isolation 404 on a foreign-org
> fetch/delete. `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-21):** closed the composite content_json
> validation gap (`composites/composite_library.feature`, under `feat-library`).
> The "Composite content_json validation — missing required fields returns
> error" scenario — the last `@awaiting-implementation` draft in
> `composite_library.feature` — now drives the REAL `POST /api/v1/libraries`
> create route: `LibraryPrimitiveCreate` in `api/routes/library.py` gained a
> `model_validator` that rejects a `composite` primitive whose `content_json`
> lacks the `nodes`/`edges` graph body with 422 (10 empty/missing-key/pass
> cases are locked by the composite BDD steps and the
> `test_library_routes.py` unit suite), instead of falling through to a bogus
> 409 from the DB IntegrityError mapping. Removed the scenario from
> `PINNED_AWAITING_IMPLEMENTATION` (the composite entry is now empty).
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Review follow-up (2026-09-21):** extended the same composite graph
> validation to the update boundary — `PATCH /api/v1/libraries/{id}` now rejects
> (422) a composite whose patched `content_json` lacks the `nodes`/`edges` lists,
> so an existing composite can no longer be mutated into a structurally broken
> graph. The shared `_assert_composite_content_json` helper backs both the create
> `model_validator` and the update route; unit coverage added in
> `test_library_routes.py`. (PR #862 review feedback.)

> **Closed this walk (2026-09-21):** closed `feat-sso`'s "No BDD scenarios
> for admin provider CRUD" gap (`auth/sso-provider-ui.md`). Registered the new
> `auth/sso_admin_crud.feature` into the executing BDD suite from the new
> `steps/test_sso_admin_crud.py`, driving the real `/api/v1/admin/sso`
> provider CRUD routes with only the DB CRUD, RLS and outbound-network seams
> patched (the TestClient + mock-org-session pattern of the conftest): 16
> scenarios — 200 provider list with type badges, 201 OIDC create (client
> secret never echoed, computed callback URL) and SAML 2.0 create, the FAR-855
> unrestricted-provisioning 422 while the flag is off, duplicate-name 409,
> 422 invalid provider type, 200 update / toggle, 400 empty update body,
> 204 delete, 404 on a missing provider, the OIDC discovery-document and
> SAML metadata-XML connection tests, group-to-team mapping set/get, and the
> non-admin 403. `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-21):** closed `feat-observability`'s
> "`active_run_observability.feature` is deselected from CI" gap
> (`observability/observability.md`). Un-gated the two scenarios in
> `observability/active_run_observability.feature` and re-anchored them so they
> drive the REAL `GET /api/v1/runs/{id}` and `GET /api/v1/runs/{id}/events`
> routes with only the `_do_*` DB-fetch seams patched — the route handler, the
> `require_permission_any_credential` authz dependency, and the `RunResponse` /
> `RunEventsResponse` serialization all run for real. The event-stream scenario
> additionally drives the REAL per-run `RunEventBroker` from the shared registry
> (`replay_since` + the node-lifecycle filter asserted end to end), and the
> detail scenario locks `trigger_actor` / `heartbeat_at` / `capacity` /
> `work_item_refs` / `child_runs` on the wire. Removed the two scenarios from
> `PINNED_AWAITING_IMPLEMENTATION`; the feature is now executing BDD coverage,
> cited from both `feat-runs` (`build/runs.md`) and `feat-observability`.
> `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-21):** closed `feat-teams-org-entity`'s
> "No org-CRUD BDD feature file" gap
> (`teams/org-entity.md`). `system_admin_orgs.feature` gained 3 org-listing
> scenarios (system-admin list success, reserved infrastructure orgs — the
> nil-UUID error-ingest org and the modulo-library registry org — filtered from
> the list, and a regular-admin 403) and `system_admin_users.feature` gained 5
> create-user error-path scenarios (email already a member of the org → 409, a
> local account holding a password in another org → 409 — the SECURITY #1189
> cross-tenant adoption refusal —, an invalid `org_role` → 422, a weak password
> → 422, and a missing org → 404), all driving the REAL `/api/v1/admin/orgs`
> routes (`admin_create_org_user` / `admin_list_orgs`) with only DB seams
> patched (`steps/test_system_admin.py`). The `feat-system-orgs` tracker gained
> the corresponding behaviour lines (`system/system-orgs.md`) and the manifest
> registry entry is now `status: covered`.

> **Closed this walk (2026-09-22):** closed `feat-core-runtime-provider-core`'s
> "No BDD coverage for the platform-provider matrix" gap
> (`core/runtime-provider-core.md`). Registered the new
> `runtime_providers/provider_matrix.feature` into the executing BDD suite
> (steps in the colocated
> `features/runtime_providers/test_provider_matrix_steps.py`), driving the REAL
> `build_hub` / `RuntimeProviderHub.resolve` / factory `initialise` seams
> network-free and DB-free (real `LocalRuntimeProvider` / `DockerRuntimeProvider`
> / `E2BRuntimeProvider(api_key=...)` constructors open no connections): the
> env-gated registration matrix (local always; e2b / docker family gated on
> their documented signals; an unrelated `MODULO_RUNNER_*` var never registers
> Docker — FAR-996), the deterministic resolve matrix (hint-wins,
> docker-family aliases share one provider, known-but-unregistered →
> `ProviderNotConfiguredError` naming the remediation env var, unknown →
> `UnknownProviderTypeError` naming the valid vocabulary, missing type →
> unresolvable), and the config-driven `initialise` (docker-family aliases under
> a config name, e2b skipped without an api_key, unknown config types rejected).
> 13 scenarios execute in CI. `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-22):** closed `feat-observability`'s "No BDD for
> OTel *trace* span capture" gap (`observability/observability.md`). The four
> `otel_traces.feature` scenarios previously fabricated span dicts in `ctx` and
> never exercised the real bridge; they are now re-anchored (`steps/test_observability.py`)
> to drive the REAL `LangGraphOtelBridge` seams network-free and DB-free (the
> `InMemorySpanExporter` pattern of `tests/unit/otel_bridge/test_handler.py`):
> run-root trace seeding via `start_run_root`, chain callbacks per node
> execution with the org/pipeline `set_run_context` stamps, a tool callback
> parented under its agent node span (child `parent.span_id` == parent
> `context.span_id`), connector chain callbacks whose span attributes carry no
> credential fields, and a telemetry-disabled provider registered with NO span
> processor so nothing reaches the exporter. The closed gap moved to a ticked
> behaviour in the tracker + manifest; `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-22):** closed `feat-library-collections`'s "No BDD
> feature file" gap (`library/library-collections.md`). Registered the new
> `library/library_collections.feature` into the executing BDD suite from the new
> `steps/test_library_collections.py`, driving the real `/api/v1/libraries/collections`
> install/uninstall/grant routes with only the library_service + flag/RLS seams
> patched: installing a published collection → 201 with an `installed` record +
> runnability verdict, a non-published collection → 400, an unresolvable pin →
> 422, a repeat install → 400; uninstall's delete-vs-detach semantics (unmodified
> entities deleted, a modified schema detached) + unknown-install 404; and the
> community-sourced grant gate (200 with `agents_granted` for community installs,
> idempotent re-grant 200, 400 for local installs, 404 for unknown installs).
> 11 scenarios execute in CI. `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-22):** closed `feat-mcp`'s "No executing BDD for
> the trigger tool" gap (`configure/mcp.md`). The five `mcp/trigger.feature`
> scenarios previously targeted the dead legacy `/mcp/tools/call` HTTP surface
> (pinned `@awaiting-implementation`, never ran); they are rewritten to drive
> the REAL shipped contract by calling the `trigger_pipeline` / `review_hitl`
> handler functions directly (request ContextVars hydrated by hand) — exercising
> the real `_check_agent_tool_scope` scope-gate chokepoint (manual run with
> `trigger_type manual` and the caller's account via `create_run`, `input_payload`
> passthrough, unknown-pipeline `pipeline_not_found` refusal), the real
> `McpAuthMiddleware` 401 gate for unauthenticated requests, and the real
> role-hierarchy scope denial (a `runner` key triggers but cannot `review_hitl`
> `approve` → `insufficient_scope`), network-free and DB-free with only the auth
> re-validation and DB/dispatch seams patched (`steps/test_alpha_mcp.py`). The
> FastMCP invoke/dispatch layer itself is not exercised by these steps.
> Removed the five scenarios from `PINNED_AWAITING_IMPLEMENTATION`.
> `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-23):** implemented agent input/output schema
> (re)assignment + detachment on `PATCH /api/v1/agents/{id}` and closed the
> last `@awaiting-implementation` gap under `feat-schemas` — the "Remove schema
> assignment" scenario in `agents/schema_assignment.feature` (pinned
> `@awaiting-implementation`, never executed). `AgentUpdate` now exposes
> `input_schema_id` / `output_schema_id` + version fields; an omitted version
> resolves to the org's `latest` placeholder version (create parity), an explicit
> `null` detaches both id and version, a version-only entry is dropped, and a
> `(id, version)` pair that does not resolve to an org-owned schema version is a
> 422 (`_resolve_schema_binding` / `_normalise_schema_updates` in
> `api/routes/agents.py`). The BDD step `remove_input_schema` now drives the real
> PATCH route and the scenario was removed from `PINNED_AWAITING_IMPLEMENTATION`;
> unit coverage added (`test_update_agent_reassigns_input_output_schemas`,
> `test_update_agent_detaches_output_schema`) replacing the old
> immutable-schema test. `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-23):** closed the four stale pipeline-level BDD
> drafts tracked as `@awaiting-implementation` gaps under the feature graph —
> every one was a placeholder whose behaviour was already shipped and covered by
> the executing `feat-triggers` suite or unit coverage, so the duplicates were
> archived, not re-wired: `pipelines/webhook_trigger.feature` (deleted; HMAC
> valid/invalid, duplicate and flood scenarios live in
> `triggers/webhook_hmac.feature` / `triggers/flood_protection.feature`, expired
> timestamp unit-pinned), `pipelines/scheduling.feature` (cron-fire + three
> polling scenarios live in `triggers/cron.feature` / `triggers/polling.feature`;
> the file keeps its executing cron-CRUD scenarios), `pipelines/concurrency.feature`
> (deleted; targeted the dead per-pipeline runs endpoint, admission coverage
> lives in the `max_concurrent_runs` 429 path + `tests/unit/pipeline_engine`),
> and `pipelines/run_variants.feature` (its coverage-gaps draft duplicated
> `variants/variant_groups.feature`'s real `get_coverage_gaps` seam). Dead step
> definitions were dropped from `steps/test_pipelines.py` /
> `steps/test_alpha_pipelines.py` and `PINNED_AWAITING_IMPLEMENTATION` shrank by
> four entries. Trackers updated: `pipelines/pipelines.md`,
> `triggers/trigger-engine.md`, `improve/variants.md`.
> `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-23):** closed `feat-license`'s
> "`stripe_webhook.py` / `admin_tiers.py` cited as adjacents but not
> behaviour-covered" gap (`licensing/license.md`). Registered the new
> `licensing/stripe_billing.feature` into the executing BDD suite from the new
> `steps/test_stripe_billing.py` (10 scenarios), driving the REAL
> `POST /api/v1/webhooks/stripe` route with only the `fulfil_team_purchase`
> background-task seam and the `get_settings` seam patched — signature
> verification (HMAC-SHA256 over `<timestamp>.<raw_body>` with the ±300s replay
> window) runs for real: valid `invoice.paid` → 200 + exactly one fulfilment
> dispatch carrying `event_id`/`customer_email`/`org_name`, checkout→invoice
> pairs fulfil exactly once (the FAR-180 double-issue guard),
> `checkout.session.completed`, missing-email and unrelated events never
> dispatch, bad/tampered/stale signatures and non-JSON payloads fail closed at
> 400, and a Stripe-disabled instance 404s. Also wired the already-executing
> `admin/tier_catalog.feature` into `feat-license`'s citations as the
> tier-catalogue surface it belongs to. `_ORPHANED_BDD_FEATURES` stays empty.
>
> **Closed this walk (2026-09-24):** closed `feat-mcp`'s MCP HITL-review
> `@awaiting-implementation` gap (`configure/mcp.md`). The five
> `mcp/review_hitl.feature` scenarios previously targeted the dead legacy
> `/mcp/tools/call` HTTP surface (pinned since 2026-08) and never ran; they are
> rewritten to drive the REAL `review_hitl` / `list_pending_hitl` tool handler
> functions directly (request ContextVars hydrated by hand, the `trigger.feature`
> re-anchor pattern), exercising the real `_parse_hitl_action` claim-token guard
> (`claim_token_required`), the real `_check_agent_tool_scope` role-hierarchy
> scope gate (a `runner` is denied `hitl:review` → `insufficient_scope`), the
> real `_check_human_only_gate` policy hook, the real HITLManager approve/reject
> decision dispatch (`approved` / `rejected` + `gate_id`), and the real
> pending-gate serialisation with the shared gate description resolver —
> network-free and DB-free with only the auth re-validation and DB/HITLManager
> seams patched. Removed the five scenarios from
> `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`); added the
> `review_hitl.feature` citation + behaviour line to `feat-mcp`
> (`configure/mcp.md`, manifest registry). `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-25):** closed the last pinned MCP legacy-
> `/mcp/tools/call` draft under `feat-mcp` (`configure/mcp.md`). The four
> `mcp/library_browse.feature` scenarios previously targeted the dead HTTP
> surface (`@awaiting-implementation`, never ran); following the
> `trigger.feature` / `review_hitl.feature` re-anchor pattern, they now drive
> the REAL `search_library` tool handler directly (request ContextVars
> hydrated by hand) — the list surface (`id` / `name` / `type` wire items),
> the text-search passthrough reaching the real `list_primitives` seam, the
> read-only posture, and the scope-gate denial: a node-level
> `capability_scope.allowed_tools` that excludes the tool is denied the
> pinned `insufficient_scope` error by the REAL `_check_agent_tool_scope`
> chokepoint before any DB read. Product change closing the wire gap the
> scenarios describe: `search_library` (read-only allowlist, pinned at the
> `resource.read_only` viewer floor in `core/mcp/scope_validator.py`) now
> calls the shared handler-level scope gate (mirroring
> `copy_library_primitive` @ `library.copy`), so the FAR-436 node-level
> `allowed_tools` narrowing can restrict the browse surface. Removed the four
> scenarios from
> `PINNED_AWAITING_IMPLEMENTATION`; `_ORPHANED_BDD_FEATURES` stays empty and
> no `@awaiting-implementation` scenarios remain under `feat-mcp`.

> **Closed this walk (2026-09-24):** closed `feat-runs`'s deferred run
> recovery/retry BDD drafts (`build/runs.md` error-state coverage). The
> run-level `/resume` / `/retry` endpoints those scenarios targeted never
> shipped — recovery is per-node via
> `POST /api/v1/runs/{run_id}/nodes/{node_id}/recover` — so
> `errors/retry.feature` was deleted (its retry-from-node /
> retry-on-success semantics are the replay / already-completed-409 cases now
> locked by the recovery surface) and `errors/recovery.feature` now drives the
> REAL recover-node route with only the `recover_node` + `dispatch_run` seams
> patched (replay/skip resume with the dispatch payload assertion, HITL-gate
> node 422, node-missing 404, already-completed 409, non-recoverable state
> 409, concurrent recovery 409, failed-resume-enqueue 500, and the
> non-operator 403 gate). The 5 `recovery.feature` + 5 `retry.feature`
> `@awaiting-implementation` scenarios were removed from
> `PINNED_AWAITING_IMPLEMENTATION` and now execute in CI;
> `_ORPHANED_BDD_FEATURES` stays empty.

> **Closed this walk (2026-09-24):** closed `feat-mcp`'s `human_only` half of
> the remaining legacy-`/mcp/tools/call` `@awaiting-implementation` debt
> (`configure/mcp.md`). The three `mcp/human_only.feature` scenarios previously
> targeted the dead legacy `/mcp/tools/call` HTTP surface (pinned since 2026-08)
> and never ran; following the `trigger.feature` / `review_hitl.feature`
> re-anchor pattern, they now drive the REAL `review_hitl` / `list_pending_hitl`
> handler seams directly (request ContextVars hydrated by hand): the REAL
> `_check_human_only_gate` policy hook denies an API-key client on a `human_only`
> gate with the shared `human_only_denial` verdict (pinned
> `{"error": "human_only_gate", "detail": MSG_HUMAN_ONLY_DENY}`) and attempts
> the FAR-634 `hitl.human_only_denied` denial audit; `list_pending_hitl` lists
> the pending human-only gate with a real per-gate `human_only` flag; and the
> FAR-611 decision-audit `client_type` attribution is exercised on both sides —
> `browser` via the REST `hitl._client_type` for a browser principal and `mcp`
> stamped by the MCP `_dispatch_hitl_action`. Product improvement closing the
> wire gap the scenario describes: `list_pending_hitl` now surfaces each
> pending gate's `human_only` flag via the new shared batched resolver
> (`db/crud/hitl_gate_config.resolve_gate_human_only_map` — claim-stamped
> fire-time config preferred, snapshot-config fallback, fail-safe
> `DEFAULT_HUMAN_ONLY`), so an MCP agent can see which gates REQUIRE a browser
> human before it attempts an action. Removed the three scenarios from
> `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`) and they
> now execute in CI; `_ORPHANED_BDD_FEATURES` stays empty. `library_browse`
> remains the last pinned MCP legacy-`/mcp/tools/call` draft.

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
- [feat-assistant](configure/assistant.md) => PRD 8.23

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
