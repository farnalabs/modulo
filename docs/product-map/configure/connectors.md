---
id: feat-connectors
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/core/connector_hub
  - backend/src/modulo/connectors/rest
  - frontend/src/views/AdminConnectorsView.vue
unit-tests:
  - backend/tests/unit/api/test_connectors_endpoint.py
  - backend/tests/unit/connectors/test_connector_base_seam.py
  - backend/tests/unit/connectors/test_connector_credential_redaction.py
  - backend/tests/unit/connectors/test_connector_egress_gate.py
  - backend/tests/unit/connector_hub/test_connector_hub.py
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/github_connector.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/connectors/sentry.feature
  - backend/tests/bdd/steps/test_sentry_connector.py
  - backend/tests/bdd/features/connectors/pagerduty.feature
  - backend/tests/bdd/steps/test_pagerduty_connector.py
  - backend/tests/bdd/features/connectors/grafana.feature
  - backend/tests/bdd/steps/test_grafana_connector.py
  - backend/tests/bdd/features/connectors/buildkite.feature
  - backend/tests/bdd/steps/test_buildkite_connector.py
  - backend/tests/bdd/features/connectors/circleci.feature
  - backend/tests/bdd/steps/test_circleci_connector.py
  - backend/tests/bdd/features/connectors/jenkins.feature
  - backend/tests/bdd/steps/test_jenkins_connector.py
  - backend/tests/bdd/features/connectors/teamcity_connector.feature
  - backend/tests/bdd/steps/test_teamcity_connector.py
  - backend/tests/bdd/features/connectors/opsgenie_connector.feature
  - backend/tests/bdd/steps/test_opsgenie_connector.py
  - backend/tests/bdd/features/connectors/azure_key_vault.feature
  - backend/tests/bdd/steps/test_azure_key_vault_connector.py
  - backend/tests/bdd/features/connectors/azure_pipelines.feature
  - backend/tests/bdd/steps/test_azure_pipelines_connector.py
  - backend/tests/bdd/features/connectors/dropbox_paper.feature
  - backend/tests/bdd/steps/test_dropbox_paper_connector.py
depends-on:
  - feat-model-backends
status: covered
---

# Connectors

External tool connectors providing a unified interface for querying and writing to
third-party services (GitHub, Jira, Slack, Linear, and 30+ others). Supports
REST integration with declarative endpoints, auth credential profiles, fan-out,
and per-destination rate limiting.

## Behaviours

- [x] A REST connector is creatable with a declarative endpoint (base_url + path
      + method) and auth credential profile (bearer / api_key / basic);
      credentials are Fernet-encrypted at rest and never exposed in responses
      (`backend/src/modulo/api/routes/connectors.py`,
      `backend/tests/unit/api/test_connectors_endpoint.py`)
- [x] query() is the read surface and write() is the write surface; mutating
      verbs retry only when an idempotency_header is declared
      (`backend/src/modulo/connectors/rest`)
- [x] Non-2xx/3xx responses surface typed RESTStatusError with status_code,
      location, and Retry-After metadata; response body capped at max_response_size
      (`backend/src/modulo/connectors/rest`)
- [x] Per-destination rate limiting (Redis-atomic when available, per-process
      fallback) enforces one budget per tenant:destination; shared limiter fails
      closed on Redis outage (`connectors/rest`)
- [x] Fan-out emits items sequentially with redacted outcome records; cardinality
      exceeds max_cardinality fails CLOSED before any request
- [x] Health check issues the configured request and reports OK only for sub-400
      status (`backend/tests/bdd/features/connectors/connector_health.feature`)
- [x] AdminConnectorsView renders schema-driven structured forms for REST
      connector config (base_url, method, credentials, advanced JSON)
      (`frontend/src/views/AdminConnectorsView.vue`)
- [x] ConnectorHub registers and manages 30+ native connector types with
      per-connector BDD features (`backend/src/modulo/connector_hub`,
      `backend/tests/unit/connector_hub/`)
- [x] The Sentry connector is BDD-exercised against the real `SentryConnector`
      (respx-mocked Sentry API): token validation via `/` (200 => healthy, 401
      => unhealthy), listing issues/projects, updating issue status, and
      creating releases (`sentry.feature`, `steps/test_sentry_connector.py`)
- [x] The PagerDuty connector is BDD-exercised against the real
      `PagerDutyConnector` (respx-mocked PagerDuty API): token validation via
      `/users` (200 => healthy, 401 => unhealthy), listing incidents/services,
      and trigger/acknowledge/resolve incident writes (`pagerduty.feature`,
      `steps/test_pagerduty_connector.py`)
- [x] The Grafana connector is BDD-exercised against the real
      `GrafanaConnector` (respx-mocked Grafana API): token validation via
      `/api/health` (200 => healthy, 401 => unhealthy), listing dashboards /
      dashboard-by-uid / alert-rules / datasources, and annotation writes
      (`grafana.feature`, `steps/test_grafana_connector.py`)
- [x] The Buildkite connector is BDD-exercised against the real
      `BuildkiteConnector` (respx-mocked Buildkite REST API v2): token
      validation via `/user` (200 => healthy, 401 => unhealthy), triggering a
      build on a branch, getting run status / listing runs / fetching per-job
      run logs (`buildkite.feature`, `steps/test_buildkite_connector.py`)
- [x] The CircleCI connector is BDD-exercised against the real
      `CircleCIConnector` (respx-mocked CircleCI REST API v2): token validation
      via `/me` (200 => healthy, 401 => unhealthy), triggering a pipeline on a
      branch, getting pipeline status / listing recent pipeline runs / fetching
      workflow+job logs (`circleci.feature`, `steps/test_circleci_connector.py`)
- [x] The Jenkins connector is BDD-exercised against the real
      `JenkinsConnector` (respx-mocked Jenkins REST API): token validation via
      `/api/json` (200 => healthy, 401 => unhealthy), triggering a build
      (plain + parameterised via `buildWithParameters`), getting build status /
      listing recent builds / fetching console logs
      (`jenkins.feature`, `steps/test_jenkins_connector.py`)
- [x] The TeamCity connector is BDD-exercised against the real
      `TeamCityConnector` (respx-mocked TeamCity REST API): querying projects /
      buildTypes / agents, triggering a build (buildQueue) and creating a build
      type, and failing closed on an unsupported query resource
      (`teamcity_connector.feature`, `steps/test_teamcity_connector.py`)
- [x] The Opsgenie connector is BDD-exercised against the real
      `OpsgenieConnector` (respx-mocked Opsgenie REST API v2): listing alerts /
      teams / schedules / escalations, single-alert / notes / logs lookups,
      on-call lookups, the alert write family (create / acknowledge / close /
      note / snooze), and API-key validation via `GET /alerts?limit=1`
      (`opsgenie_connector.feature`, `steps/test_opsgenie_connector.py`)
- [x] The Azure Key Vault connector is BDD-exercised against the real
      `AzureKeyVaultConnector` (respx-mocked Azure Key Vault REST API 7.4):
      token validation via `GET /secrets?maxresults=1` (200 => healthy, 401 =>
      unhealthy), listing secrets / keys / certificates, single-secret / key /
      certificate lookups, and the secret write family (create via PUT +
      soft-delete via DELETE) (`azure_key_vault.feature`,
      `steps/test_azure_key_vault_connector.py`)
- [x] The Azure Pipelines connector is BDD-exercised against the real
      `AzurePipelinesConnector` (respx-mocked Azure DevOps REST API 7.0):
      listing projects / pipelines / runs / releases, triggering a pipeline
      run (pipeline_id + branch) and a release (definition_id), and failing
      closed on an unsupported query resource
      (`azure_pipelines.feature`, `steps/test_azure_pipelines_connector.py`)
- [x] The Dropbox Paper connector is BDD-exercised against the real
      `DropboxPaperConnector` (respx-mocked Dropbox API v2 ``api.dropboxapi.com``):
      account validation via `/users/get_current_account` (200 => healthy with the
      authenticated email, 401 => unhealthy), listing Paper docs (``docs`` resource
      with ``filter_by``), downloading a Paper doc as markdown (``doc`` resource),
      listing folders (``folders`` resource), creating a Paper doc via markdown
      import (``doc`` write resource), and failing closed on unsupported query/write
      resources (`dropbox_paper.feature`, `steps/test_dropbox_paper_connector.py`)

## Known Gaps

- Per-item fan-out outcome trace spans are deferred to FAR-404 (operation-level
  OTel spans shipped in v1).
- No BDD for connector CRUD lifecycle (create/update/delete via admin API);
  coverage is via unit tests.

## QA History
- 2026-09-16: **improve-architecture (product-map walk)** — closed the
  `azure_key_vault.feature` and `azure_pipelines.feature` orphan gaps: both
  features shipped under `tests/bdd/features/connectors/` but no step module
  registered them via `scenarios(...)`, so they never executed. Each is now
  wired from its own step module that drives the REAL connector against a
  respx-mocked API (mirroring the unit suites):
  `steps/test_azure_key_vault_connector.py` (10 scenarios — `/secrets` health
  200/401, list secrets/keys/certificates, get secret/key/certificate, create a
  secret, soft-delete a secret) and `steps/test_azure_pipelines_connector.py`
  (7 scenarios — query projects/pipelines/runs/releases, trigger a pipeline run
  and a release, fail closed on an unsupported resource).
  `_ORPHANED_BDD_FEATURES` shrinks by two; the remaining connector orphans
  (`azure_repos`, `discord`, `dropbox_paper`, `microsoft_teams`, `sharepoint`,
  `swappable_binding`) and the two pipeline-validation orphans still await step
  modules.
- 2026-09-15: **improve-architecture (product-map walk)** — closed the
  `circleci.feature`, `jenkins.feature`, `teamcity_connector.feature` and
  `opsgenie_connector.feature` orphan gaps: the features shipped under
  `tests/bdd/features/connectors/` but no step module registered them via
  `scenarios(...)`, so they never executed. Each is now wired from its own
  step module that drives the REAL connector against a respx-mocked API
  (mirroring the unit suites): `steps/test_circleci_connector.py` (5 scenarios
  — `/me` health 200/401, trigger pipeline on a branch, get pipeline status,
  list recent runs, workflow+job logs), `steps/test_jenkins_connector.py`
  (7 scenarios — `/api/json` health 200/401, plain + parameterised build
  trigger, build status, list builds, console logs),
  `steps/test_teamcity_connector.py` (6 scenarios — query projects /
  buildTypes / agents, trigger build, create build type, fail closed on an
  unsupported resource) and `steps/test_opsgenie_connector.py` (16 scenarios —
  list alerts/teams/schedules/escalations, alert-by-id / notes / logs,
  on-calls, the create-acknowledge-close-note-snooze write family, and API-key
  health). `_ORPHANED_BDD_FEATURES` shrinks by four; the remaining connector
  orphans (`azure_key_vault`, `azure_pipelines`, `azure_repos`, `discord`,
  `dropbox_paper`, `microsoft_teams`, `sharepoint`, `swappable_binding`) and
  the two pipeline-validation orphans still await step modules.
- 2026-09-16: **improve-architecture (product-map walk)** — closed the
  `dropbox_paper.feature` orphan gap: the feature shipped under
  `tests/bdd/features/connectors/` but no step module registered it via
  `scenarios(...)`, so it never executed. The feature is now wired from the new
  `steps/test_dropbox_paper_connector.py`, which drives the REAL
  `DropboxPaperConnector` against a respx-mocked Dropbox API v2 (mirroring
  `tests/unit/connectors/test_dropbox_paper.py`): eight scenarios covering
  account validation via `/users/get_current_account` (200 => healthy reporting
  the authenticated email, 401 => unhealthy), listing Paper docs, downloading a
  Paper doc as markdown, listing folders, creating a Paper doc via markdown
  import, and failing closed on unsupported query/write resources all collect
  and execute. `_ORPHANED_BDD_FEATURES` shrinks by one; the remaining connector
  orphans (`azure_key_vault`, `azure_pipelines`, `azure_repos`, `discord`,
  `microsoft_teams`, `sharepoint`, `swappable_binding`) and the two
  pipeline-validation orphans still await step modules.
- 2026-09-14: **improve-architecture (product-map walk)** — closed the
  `buildkite.feature` orphan gap: the feature shipped under
  `tests/bdd/features/connectors/` but no step module registered it via
  `scenarios(...)`, so it never executed. The feature is now wired from the new
  `steps/test_buildkite_connector.py`, which drives the REAL `BuildkiteConnector`
  against a respx-mocked Buildkite REST API v2 (mirroring
  `tests/unit/connectors/test_buildkite.py`): six scenarios covering token
  validation via `/user` (200/401), triggering a build on a branch, get run
  status (scheduled => queued, running => in_progress), list recent builds
  (passed => success, failed => failure), and per-job run logs all collect and
  execute. `_ORPHANED_BDD_FEATURES` shrinks by one; the remaining connector
  orphans (`azure_key_vault`, `azure_pipelines`, `azure_repos`, `circleci`,
  `discord`, `dropbox_paper`, `jenkins`, `microsoft_teams`, `opsgenie`,
  `sharepoint`, `swappable_binding`, `teamcity`) and the two pipeline-validation
  orphans still await step modules.
- 2026-09-14: **improve-architecture (product-map walk)** — closed the
  `grafana.feature` orphan gap: the feature shipped under
  `tests/bdd/features/connectors/` but no step module registered it via
  `scenarios(...)`, so it never executed. The feature is now wired from the new
  `steps/test_grafana_connector.py`, which drives the REAL `GrafanaConnector`
  against a respx-mocked Grafana API (mirroring
  `tests/unit/connectors/test_grafana.py`): seven scenarios covering token
  validation (200/401), list dashboards / dashboard-by-uid / alert-rules /
  datasources, and annotation writes all collect and execute.
  `_ORPHANED_BDD_FEATURES` shrinks by one; the remaining connector orphans
  (`azure_key_vault`, `azure_pipelines`, `azure_repos`, `circleci`,
  `discord`, `dropbox_paper`, `jenkins`, `microsoft_teams`, `opsgenie`,
  `sharepoint`, `swappable_binding`, `teamcity`) and the two pipeline-validation
  orphans still await step modules.
- 2026-09-14: **improve-architecture (product-map walk)** — closed the
  `pagerduty.feature` orphan gap: the feature shipped under
  `tests/bdd/features/connectors/` but no step module registered it via
  `scenarios(...)`, so it never executed. The feature is now wired from the new
  `steps/test_pagerduty_connector.py`, which drives the REAL `PagerDutyConnector`
  against a respx-mocked PagerDuty API (mirroring
  `tests/unit/connectors/test_pagerduty.py`): seven scenarios covering token
  validation (200/401), list incidents/services, and trigger/acknowledge/resolve
  incident writes all collect and execute. `_ORPHANED_BDD_FEATURES` shrinks by one;
  the remaining connector orphans (`azure_key_vault`, `azure_pipelines`,
  `azure_repos`, `buildkite`, `circleci`, `discord`, `dropbox_paper`, `grafana`,
  `jenkins`, `microsoft_teams`, `opsgenie`, `sharepoint`, `swappable_binding`,
  `teamcity`) and the two pipeline-validation orphans still await step modules.
- 2026-09-14: **improve-architecture (product-map walk)** — closed the `sentry.feature`
  orphan gap: the feature shipped under `tests/bdd/features/connectors/` but no step
  module registered it via `scenarios(...)`, so it never executed. The feature is now
  wired from the new `steps/test_sentry_connector.py`, which drives the REAL
  `SentryConnector` against a respx-mocked Sentry API (mirroring
  `tests/unit/connectors/test_sentry.py`): six scenarios covering token validation
  (200/401), list issues/projects, issue-status update and release creation all collect
  and execute. `_ORPHANED_BDD_FEATURES` shrinks by one; the remaining connector orphans
  still await step modules.
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/connectors`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-10: **improve-architecture (product-map walk)** — registered the
  `RestConnectorConfigForm.vue` static testids (`rest-connector-base-url`,
  `rest-connector-method`, `rest-connector-timeout`, `rest-connector-verify-tls`,
  `rest-connector-on-unknown`, `rest-connector-records-path`,
  `rest-connector-allowed-hosts`, `rest-connector-legacy-auth-hint`,
  `rest-connector-auth-mode`, `rest-connector-token`, `rest-connector-username`,
  `rest-connector-password`, `rest-connector-api-key`, `rest-connector-api-key-in`,
  `rest-connector-header-name`, `rest-connector-query-param`,
  `rest-connector-advanced-json`) in the `/admin/connectors` manifest `elements:`
  inventory and added `AdminConnectorsView.vue` to the reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`) — the structured
  Generic REST connector form shipped on the page was previously invisible to
  Remy's docs indexer / `/api/v1/manifest`.
- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-connectors`, which previously had behaviours only
  in `manifest.yaml` inline. Behaviours verified against `routes/connectors.py`,
  `connector_hub/`, `connectors/rest/`, and the connector unit+BDD suites.
  Status: covered.
