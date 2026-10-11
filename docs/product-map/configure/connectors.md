---
id: feat-connectors
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/core/connector_hub
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/db/crud/team_scope.py
  - backend/src/modulo/connectors/base.py
  - backend/src/modulo/connectors/rest
  - backend/src/modulo/core/guardrails/conformance.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - frontend/src/views/AdminConnectorsView.vue
unit-tests:
  - backend/tests/unit/api/test_connectors_endpoint.py
  - backend/tests/unit/connectors/test_acl.py
  - backend/tests/unit/core/test_guardrail_conformance_midrun.py
  - backend/tests/unit/connectors/test_connector_base_seam.py
  - backend/tests/unit/connectors/test_connector_credential_redaction.py
  - backend/tests/unit/connectors/test_connector_egress_gate.py
  - backend/tests/unit/connector_hub/test_connector_hub.py
  - backend/tests/unit/api/test_pipeline_team_visibility.py
  - backend/tests/unit/core/test_team_visibility.py
  - backend/tests/unit/mcp/test_team_binding_enforcement.py
  - backend/tests/unit/db/crud/test_team_scope.py
  - backend/tests/integration/test_pipeline_team_gate_parity.py
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
  - backend/tests/bdd/features/connectors/azure_repos.feature
  - backend/tests/bdd/steps/test_azure_repos_connector.py
  - backend/tests/bdd/features/connectors/discord.feature
  - backend/tests/bdd/steps/test_discord_connector.py
  - backend/tests/bdd/features/connectors/microsoft_teams.feature
  - backend/tests/bdd/steps/test_microsoft_teams_connector.py
  - backend/tests/bdd/features/connectors/sharepoint.feature
  - backend/tests/bdd/steps/test_sharepoint_connector.py
  - backend/tests/bdd/features/connectors/swappable_binding.feature
  - backend/tests/bdd/steps/test_pipeline_connector_binding.py
  - backend/tests/bdd/features/connectors/connector_crud.feature
  - backend/tests/bdd/steps/test_connector_crud.py
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
- [x] Per-destination rate limiting enforces one budget per tenant:destination:
      a Redis-atomic shared bucket when a `redis_client` is wired at the
      composition root (fleet-wide), otherwise the connector-local per-process
      bucket is authoritative (single-worker / dev parity); a configured shared
      limiter FAILS CLOSED on a Redis outage — it never falls back to a
      per-process bucket that would multiply the effective cap by the worker
      count (`connectors/rest`)
- [x] Fan-out emits items sequentially with redacted outcome records; cardinality
      exceeds max_cardinality fails CLOSED before any request
- [x] The REST connector's health check issues the configured request with the
      credentials applied and reports OK only for a 2xx status — redirects are
      never followed, so a 3xx is raised as the typed `RESTStatusError` before
      the sub-400 check and is reported unhealthy
      (`backend/tests/bdd/features/connectors/connector_health.feature`)
- [x] AdminConnectorsView renders schema-driven structured forms for REST
      connector config (base_url, method, credentials, advanced JSON)
      (`frontend/src/views/AdminConnectorsView.vue`)
- [x] ConnectorHub registers and manages 30+ native connector types with
      per-connector BDD features (`backend/src/modulo/core/connector_hub`,
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
- [x] The Azure Repos connector is BDD-exercised against the real
      `AzureReposConnector` (respx-mocked Azure DevOps REST API v7.0): PAT
      validation via the profile endpoint (401 => unhealthy), listing
      repositories / pull requests / commits, reading a file from a branch,
      writing a file via a push, and creating a pull request
      (`azure_repos.feature`, `steps/test_azure_repos_connector.py`)
- [x] The Discord connector is BDD-exercised against the real
      `DiscordConnector` (respx-mocked Discord REST API v10): bot-token
      validation via `/users/@me` (200 => healthy, 401 => unhealthy), listing
      guilds / channels / messages / guild members / roles, getting a guild by
      id, and the message / reaction / channel write family
      (`discord.feature`, `steps/test_discord_connector.py`)
- [x] The Microsoft Teams connector is BDD-exercised against the real
      `MicrosoftTeamsConnector` (respx-mocked Microsoft Graph API v1.0): token
      validation via `/users` (200 => healthy, 401 => unhealthy), listing
      teams / channels / messages / members / users / groups, getting a team and
      a channel by id, and the message / channel write family
      (`microsoft_teams.feature`, `steps/test_microsoft_teams_connector.py`)
- [x] The SharePoint connector is BDD-exercised against the real
      `SharePointConnector` (respx-mocked Microsoft Graph API v1.0): token
      validation via `/sites/root` (200 => healthy reporting the site root
      name, 401 => unhealthy), listing sites / list items, reading a file, and
      creating a list item (`sharepoint.feature`,
      `steps/test_sharepoint_connector.py`)
- [x] Connector bindings on pipeline nodes are swappable and validated at graph
      save time: swapping a node's binding swaps the extracted snapshot binding
      without touching the topology, an unbound node extracts no binding, a
      binding to a missing instance is rejected (`CONNECTOR_NOT_FOUND`), a
      binding whose instance lacks a required operation is rejected
      (`CONNECTOR_MISSING_OPERATIONS`), and an active instance covering the
      required operations passes – BDD-exercised against the real
      `extract_connector_bindings` + `GraphValidator.validate_definition`
      surfaces (`swappable_binding.feature`,
      `steps/test_pipeline_connector_binding.py`)
- [x] The connector instance CRUD lifecycle is BDD-exercised against the real
      admin API: create (201) Fernet-encrypts credentials at rest and never
      echoes them, malformed REST credentials / config are rejected at the
      boundary (422), a connector is retrievable individually and in the
      paginated list (200) without exposing credentials, PATCH re-encrypts
      fresh credentials (200), DELETE removes the instance (204), and a
      foreign-org connector resolves to 404 before any read/update/delete
      (`connector_crud.feature`, `steps/test_connector_crud.py`)
- [x] A connector's operation scope is UNRESTRICTED unless a non-empty
      allowlist restrains it (FAR-1564): the single shared
      `unrestricted_allowed_operations` predicate — used by `ConnectorACL`, the
      graph validator's connector-binding check and the guardrail conformance
      reader so one stored value is never read restrictively on one path and
      permissively on another — treats `None` AND the `[]` that every
      REST/MCP/UI-created connector stores as unrestricted (a default-created
      connector is never locked out of every operation), a NON-EMPTY list is an
      allowlist (there is no explicit deny-all; removing the connector is the
      lock), and any MALFORMED non-list value fails CLOSED to the empty
      allowlist so every operation is denied — malformed input restricts, never
      grants (`backend/src/modulo/connectors/base.py`,
      `backend/src/modulo/core/graph_validator/__init__.py`,
      `backend/src/modulo/core/guardrails/conformance.py`;
      `backend/src/modulo/core/connector_hub/health_sweep.py` references the
      ACL-denial semantics in comments only and does not read the predicate;
      `unit-tests: test_acl.py, test_connectors_endpoint.py,
      test_guardrail_conformance_midrun.py`)
- [x] Capability spellings reduce through ONE canonical vocabulary shared by
      enforcement and certification (FAR-1582 / FAR-1594): `qualified_capability`
      / `canonical_capability` / `canonical_capability_set` live in the
      stdlib-only `connectors/base.py` leaf, so `ConnectorACL` (enforcement) and
      `core.guardrails.conformance` (certification) import the SAME code and can
      never give opposite answers for one stored value — before FAR-1594
      conformance certified a stored `["github.read"]` as `read` while the ACL
      denied the read. A stored ALLOWLIST entry is a declaration of GRANT, so a
      type qualifier is reduced to the bare `Capability` if it names the
      surface's OWN connector type (`github.read` -> `read`, `github:write` /
      `github.write` -> `write`); the qualifier is only TRUSTED for that surface
      type (FAR-1616), so a mis-typed qualifier (`github.write` on a filesystem
      connector), or a qualified entry whose surface type cannot be verified, is
      REJECTED fail-closed and logged (it grants nothing) — it is never dropped
      to the bare capability and granted on the wrong surface. A conformance
      CLAIM is a binding REQUEST, so it keeps its qualifier (`github.read` binds
      to the github surface specifically). `ConnectorACL` builds its restricted
      set through `canonical_capability_set` (the connector hub passes the
      instance's own `connector_type_id`) and canonicalises the requested
      operation through `canonical_capability`, so `check("read")` grants a
      stored same-type `["github.read"]` in the hub exactly as the conformance
      reader certifies `read`;
      entries that are not capabilities in any accepted spelling
      (`sandbox.egress`, `egress:github.com`) are never rewritten (a non-string
      list entry is dropped silently; a string entry that is not a capability is
      dropped with a log — both grant nothing), while a non-list value yields the
      empty set matching the fail-closed FAR-1564 treatment
      (`backend/src/modulo/connectors/base.py`,
      `backend/src/modulo/core/guardrails/conformance.py`;
      `unit-tests: test_acl.py, test_guardrail_conformance_midrun.py`)
- [x] A connector binding that crosses a team boundary is refused at graph
      save with 409 `connector_team_mismatch` (FAR-1515, PRD §9.3, model
      restated by FAR-1618). Teams are a VISIBILITY GROUPING, not a
      credential trust boundary, so the rule has exactly one shape: a
      team-private connector (`visibility: team`) is only usable by a
      pipeline owned by the SAME team — a different team's pipeline or an
      org pipeline is refused — and an org-visibility connector
      (`visibility: org`) is SHARED ACROSS THE ORGANISATION: it binds to ANY
      pipeline, including one owned by a team, and never produces a
      mismatch. (FAR-1618 removed the reverse direction FAR-1515 had added —
      a TEAM pipeline pinning an org connector, mirrored at run time by the
      FAR-516 `ConnectorACL.check` run-gate — because it contradicted this
      shared rule; there is no save-time or run-time rejection of an
      org-visibility connector on a team pipeline any more.) The
      candidate rows are read team-blind but org-scoped
      (`db.crud.team_scope.team_blind_org_scope`) because the
      `rls_team_isolation` policy would otherwise hide another team's
      team-private row from the caller's session — the very binding under
      judgement would look absent and the save would pass; with that widened
      read an absent id is DEFINITIVE and fails CLOSED as the same named
      `connector_team_mismatch` (`ConnectorBindingMissingError`), never a
      silent skip (FAR-1515 CRITICAL 1). The shared predicate and wire-code
      constants live in `core/team_visibility.py` and are enforced at every
      write path that can create the binding: the REST graph save /
      node-conversion chokepoint (`_enforce_connector_team_bindings`), the MCP
      graph-update tool and the MCP `bind_connector_to_node` tool, the workflow
      import confirm, the library collection install, and a connector
      visibility/owner re-scope
      (`_reject_re_scope_that_breaks_a_bound_pipeline`), each surfacing the
      shared named `connector_team_mismatch` code + detail builder (HTTP 409 on
      the REST/library surfaces, the shared error envelope on the MCP tools;
      `bind_connector_to_node` evaluates the predicate directly, so it refuses a
      caller-RLS-hidden row as `connector_not_found`, while the team-blind read
      behind the graph-save / import / MCP-graph-update paths fails closed as
      `ConnectorBindingMissingError`)
      (`backend/src/modulo/core/team_visibility.py`,
      `backend/src/modulo/db/crud/team_scope.py`,
      `backend/src/modulo/api/routes/pipelines.py`,
      `backend/src/modulo/api/mcp_server.py`,
      `backend/src/modulo/api/routes/library.py`,
      `backend/src/modulo/api/routes/connectors.py`;
      `unit-tests: test_pipeline_team_visibility.py,
      test_team_visibility.py, test_team_binding_enforcement.py,
      test_team_scope.py`; `integration: test_pipeline_team_gate_parity.py`)

## Known Gaps

- Per-item fan-out outcome trace spans are deferred — operation-level OTel trace
  spans and per-destination outcome/latency metrics are emitted in v1; only the
  per-item spans are deferred.
- A per-tenant weighted concurrency semaphore / bounded-concurrency fan-out fork
  is deferred — fan-out emits items sequentially in v1 (no concurrent fork);
  throughput relies on pipeline-level concurrency limits and the connector's
  single connection-pooled client.
- The distinct request-to-response classification layer is deferred — ingestion
  classification currently runs as one opaque hop (a separate
  deduplicate/classify layer is not modelled).

## QA History
- 2026-10-09: **Improve Architecture product-map walk** – closed the untracked
  FAR-1582 / FAR-1594 sub-surface (one canonical connector capability vocabulary
  shared by the ACL and the guardrail conformance reader, merged in PR #1451):
  the vocabulary helpers (`qualified_capability` / `canonical_capability` /
  `canonical_capability_set`) shipped with NO coverage in either product-map
  layer — the manifest `feat-connectors` registry had the FAR-1564
  `unrestricted_allowed_operations` semantics but not the canonicalisation that
  keeps enforcement and certification in step, and this tracker named neither.
  Added the checked behaviour line (ONE shared stdlib-only vocabulary in
  `connectors/base.py`, the grant-vs-claim asymmetry, `None`/malformed handling)
  plus the `connectors/base.py` / `core/guardrails/conformance.py` code and
  `test_acl.py` / `test_guardrail_conformance_midrun.py` unit-test citations.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1515 sub-surface (cross-team connector binding enforcement at graph save,
  merged in PR #1377): the team-scope rule for connector bindings shipped while
  neither the manifest `feat-connectors` registry nor this tracker named it, so
  the 409 `connector_team_mismatch` refusal, the reverse org-only direction, the
  team-blind (FAR-1515 CRITICAL 1) read that makes an unresolvable binding fail
  closed, and the every-write-path coverage were invisible to Assistant's docs
  indexer and to the graph. Added the checked behaviour line plus the
  `core/team_visibility.py` / `db/crud/team_scope.py` code and unit/integration
  test citations; the model-backend mirror is tracked under `feat-model-backends`.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1564 sub-surface (empty `allowed_operations` means unrestricted, not
  deny-all, merged in PR #1399): the operation-scope semantics shipped with NO
  coverage in either product-map layer. The manifest `feat-connectors` registry
  had no scope-semantics line and this tracker none either, after the FAR-935
  validation-level additions. Added the checked behaviour line (the shared
  `unrestricted_allowed_operations` predicate, `None` ↔ `[]`, fail-closed
  malformed, no deny-all) plus the connector/graph-validator/conformance code
  and unit-test citations.
- 2026-09-20: **product-map review pass** – closed the last
  `feat-connectors` BDD gap, "No BDD for connector CRUD lifecycle
  (create/update/delete via admin API)". Registered the new
  `connectors/connector_crud.feature` into the executing BDD suite from the new
  `steps/test_connector_crud.py`, driving the real `/api/v1/connectors`
  create / get / list / PATCH / delete routes with only the DB CRUD + RLS
  seams patched (the TestClient + mock-org-session pattern of the
  `test_connectors_endpoint.py` unit suite): 9 scenarios – create (201) with
  credentials Fernet-encrypted at rest and never echoed (the captured
  ciphertext round-trips to the exact credential), malformed REST credentials
  (422) and invalid REST `on_unknown` config (422) rejected at the boundary,
  individual retrieval (200, redacted) and list (200, paginated + redacted),
  foreign-org fetch 404, PATCH re-encrypting fresh REST credentials into an
  appended ciphertext (200), and DELETE removing the instance (204) with a
  foreign-org delete 404 – all collect and execute. `_ORPHANED_BDD_FEATURES`
  stays empty.
- 2026-09-16: **product-map review pass** – closed the last
  connector BDD orphan, `connectors/swappable_binding.feature` (a stale
  placeholder draft whose steps did not exist). It was rewritten into an
  accurate connector-binding spec and wired into the executing suite from the
  new `steps/test_pipeline_connector_binding.py`, which drives the REAL
  binding surfaces the pipeline save path uses: `extract_connector_bindings`
  (pure swap/extraction semantics) and
  `GraphValidator.validate_definition` → `_check_connector_bindings` with a
  mocked session (the DB-free pattern of `tests/unit/graph_validator`): 5
  scenarios – swap binding (exactly one binding, old one gone), unbound node
  extracts nothing, missing instance → `CONNECTOR_NOT_FOUND`, missing required
  operation → `CONNECTOR_MISSING_OPERATIONS`, valid active binding → pass –
  all collect and pass. `_ORPHANED_BDD_FEATURES` shrinks to zero.
- 2026-09-16: **product-map review pass** – closed the
  `azure_repos.feature`, `discord.feature`, `microsoft_teams.feature` and
  `sharepoint.feature` orphan gaps: all four features shipped under
  `tests/bdd/features/connectors/` but no step module registered them via
  `scenarios(...)`, so they never executed. Each is now wired from its own step
  module that drives the REAL connector against a respx-mocked API (mirroring
  the unit suites): `steps/test_azure_repos_connector.py` (7 scenarios – 401
  profile health, list repos / file / pull requests / commits, write a file via
  a push, create a pull request), `steps/test_discord_connector.py` (11
  scenarios – `/users/@me` health 200/401, list guilds / channels / messages /
  members / roles, get guild, send message, add reaction, create channel),
  `steps/test_microsoft_teams_connector.py` (12 scenarios – `/users` health
  200/401, list teams / channels / messages / members / users / groups, get
  team / channel, send message, create channel) and
  `steps/test_sharepoint_connector.py` (6 scenarios – `/sites/root` health
  200/401, list sites / list items, create list item, read file).
  36 scenarios now collect and execute. `_ORPHANED_BDD_FEATURES` shrinks by
  four; the remaining orphans (`swappable_binding`, `pipeline_config_validation`,
  `validation`) still await step modules.
- 2026-09-16: **product-map review pass** – closed the
  `azure_key_vault.feature` and `azure_pipelines.feature` orphan gaps: both
  features shipped under `tests/bdd/features/connectors/` but no step module
  registered them via `scenarios(...)`, so they never executed. Each is now
  wired from its own step module that drives the REAL connector against a
  respx-mocked API (mirroring the unit suites):
  `steps/test_azure_key_vault_connector.py` (10 scenarios – `/secrets` health
  200/401, list secrets/keys/certificates, get secret/key/certificate, create a
  secret, soft-delete a secret) and `steps/test_azure_pipelines_connector.py`
  (7 scenarios – query projects/pipelines/runs/releases, trigger a pipeline run
  and a release, fail closed on an unsupported resource).
  `_ORPHANED_BDD_FEATURES` shrinks by two; the remaining connector orphans
  (`azure_repos`, `discord`, `dropbox_paper`, `microsoft_teams`, `sharepoint`,
  `swappable_binding`) and the two pipeline-validation orphans still await step
  modules.
- 2026-09-15: **product-map review pass** – closed the
  `circleci.feature`, `jenkins.feature`, `teamcity_connector.feature` and
  `opsgenie_connector.feature` orphan gaps: the features shipped under
  `tests/bdd/features/connectors/` but no step module registered them via
  `scenarios(...)`, so they never executed. Each is now wired from its own
  step module that drives the REAL connector against a respx-mocked API
  (mirroring the unit suites): `steps/test_circleci_connector.py` (5 scenarios
  – `/me` health 200/401, trigger pipeline on a branch, get pipeline status,
  list recent runs, workflow+job logs), `steps/test_jenkins_connector.py`
  (7 scenarios – `/api/json` health 200/401, plain + parameterised build
  trigger, build status, list builds, console logs),
  `steps/test_teamcity_connector.py` (6 scenarios – query projects /
  buildTypes / agents, trigger build, create build type, fail closed on an
  unsupported resource) and `steps/test_opsgenie_connector.py` (16 scenarios –
  list alerts/teams/schedules/escalations, alert-by-id / notes / logs,
  on-calls, the create-acknowledge-close-note-snooze write family, and API-key
  health). `_ORPHANED_BDD_FEATURES` shrinks by four; the remaining connector
  orphans (`azure_key_vault`, `azure_pipelines`, `azure_repos`, `discord`,
  `dropbox_paper`, `microsoft_teams`, `sharepoint`, `swappable_binding`) and
  the two pipeline-validation orphans still await step modules.
- 2026-09-16: **product-map review pass** – closed the
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
- 2026-09-14: **product-map review pass** – closed the
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
- 2026-09-14: **product-map review pass** – closed the
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
- 2026-09-14: **product-map review pass** – closed the
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
- 2026-09-14: **product-map review pass** – closed the `sentry.feature`
  orphan gap: the feature shipped under `tests/bdd/features/connectors/` but no step
  module registered it via `scenarios(...)`, so it never executed. The feature is now
  wired from the new `steps/test_sentry_connector.py`, which drives the REAL
  `SentryConnector` against a respx-mocked Sentry API (mirroring
  `tests/unit/connectors/test_sentry.py`): six scenarios covering token validation
  (200/401), list issues/projects, issue-status update and release creation all collect
  and execute. `_ORPHANED_BDD_FEATURES` shrinks by one; the remaining connector orphans
  still await step modules.
- 2026-09-12: **product-map review pass** – registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/connectors`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-10: **product-map review pass** – registered the
  `RestConnectorConfigForm.vue` static testids (`rest-connector-base-url`,
  `rest-connector-method`, `rest-connector-timeout`, `rest-connector-verify-tls`,
  `rest-connector-on-unknown`, `rest-connector-records-path`,
  `rest-connector-allowed-hosts`, `rest-connector-legacy-auth-hint`,
  `rest-connector-auth-mode`, `rest-connector-token`, `rest-connector-username`,
  `rest-connector-password`, `rest-connector-api-key`, `rest-connector-api-key-in`,
  `rest-connector-header-name`, `rest-connector-query-param`,
  `rest-connector-advanced-json`) in the `/admin/connectors` manifest `elements:`
  inventory and added `AdminConnectorsView.vue` to the reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`) – the structured
  Generic REST connector form shipped on the page was previously invisible to
  Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-07: **product-map review pass** – added this
  behaviour-tracker for `feat-connectors`, which previously had behaviours only
  in `manifest.yaml` inline. Behaviours verified against `routes/connectors.py`,
  `connector_hub/`, `connectors/rest/`, and the connector unit+BDD suites.
  Status: covered.
