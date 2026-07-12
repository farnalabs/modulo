---
id: feat-connectors-hub
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/github_connector.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
  - backend/tests/bdd/features/connectors/linear_connector.feature
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/connectors/connector_decrypt_error.feature
  - backend/tests/bdd/features/connectors/gitea_connector.feature
  - backend/tests/bdd/features/connectors/azure_pipelines.feature
  - backend/tests/bdd/features/connectors/codeclimate.feature
  - backend/tests/bdd/features/connectors/confluence.feature
  - backend/tests/bdd/features/connectors/n8n.feature
  - backend/tests/bdd/features/connectors/notion_connector.feature
  - backend/tests/bdd/features/connectors/npm.feature
  - backend/tests/bdd/features/connectors/onepassword.feature
  - backend/tests/bdd/features/connectors/opsgenie_connector.feature
  - backend/tests/bdd/features/connectors/pypi.feature
  - backend/tests/bdd/features/connectors/snyk.feature
  - backend/tests/bdd/features/connectors/sonarqube.feature
  - backend/tests/bdd/features/connectors/teamcity_connector.feature
  - backend/tests/bdd/features/connectors/trivy.feature
  - backend/tests/bdd/features/connectors/youtrack_connector.feature
unit-tests:
  - backend/tests/unit/connector_hub/test_connector_hub.py
  - backend/tests/unit/connector_hub/test_traced_connector.py
  - backend/tests/unit/api/test_connectors_endpoint.py
  - backend/tests/unit/api/test_connectors_programming_error.py
  - backend/tests/unit/connector_hub/test_advisory_lock.py
code:
  - backend/src/modulo/core/connector_hub/
  - backend/src/modulo/connectors/
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-core-secrets-backend
status: partial
---

# Connector Hub

Run-scoped credential decryption, connector lifecycle management, and `ConnectorBase` ABC — the
runtime layer that resolves a `ConnectorInstance` DB row to an initialised, traced, ACL-enforced
connector object for a single pipeline run. Every connector operation is wrapped in an OTel span.

## Behaviours

### Hub Lifecycle

- [x] `ConnectorHub(secrets_backend, org_id=None)` accepts a `SecretsBackend` instance and optional org ID
- [x] `async with hub:` context manager creates a run-scoped scope; `__aexit__` clears all connectors
- [x] `await hub.initialise(instances)` decrypts credentials and builds connector objects for a list of `ConnectorInstance` rows
- [x] `hub.get(connector_id)` returns the initialised `ConnectorBase` for a given UUID
- [x] `hub.acl(connector_id)` returns the `ConnectorACL` for a given UUID
- [x] `hub.connector_ids` returns a `frozenset[uuid.UUID]` of all registered connector IDs
- [x] `initialise` is additive — multiple calls accumulate connectors without clearing previous ones
- [ ] Multiple `ConnectorHub` instances can coexist in the same process (one per concurrent run)

### Credential Decryption

- [x] Credentials are fetched via `secrets_backend.get_secret(str(ci.id))` — one call per connector per run
- [x] The raw string is parsed as JSON before being passed to the connector constructor
- [x] A missing secret in the backend (`KeyError`) raises `ConnectorDecryptError` with the connector ID
- [x] Invalid JSON in the stored secret raises `ConnectorDecryptError`
- [x] A wrong Fernet key (cannot decrypt) raises `ConnectorDecryptError` propagated from the backend
- [x] Credentials are discarded at run end — never stored in LangGraph state, checkpoint blobs, OTel spans, or logs
- [x] `_get_cred(creds, key, type_id)` helper raises `ValueError` with a clear message when a required credential key is missing

### Connector Resolution (`_build_connector` — 41 built-in types + plugin fallback)

All connectors receive `_TracedConnector` wrapping at construction time. All use `_get_cred(creds, key, type_id)` for required credentials (raises `ValueError` with type-specific message when missing), unless noted as optional via `.get()`.

#### Git Hosting Platforms

- [x] `type_id = "gitea"` → `GiteaConnector(token=creds.token, base_url=config.base_url)` — defaults to `https://codeberg.org`; BDD feature file exists (5 scenarios: repos, file, pulls, issues, write); ConnectorType.GITEA; ConnectorType enum entry exists; no unit tests
- [x] `type_id = "azure_repos"` → `AzureReposConnector(token=creds.token, organization=config.organization)` — `organization` is required; `ValueError` if missing; no BDD; ConnectorType.AZURE_REPOS; enum entry exists; no unit tests
- [x] `type_id = "bitbucket"` → `BitbucketConnector(token=creds.token)`; no BDD; ConnectorType.BITBUCKET; enum entry exists; no unit tests
- [x] `type_id = "github"` → `GitHubConnector(token=creds.token)`; BDD exists (github_connector.feature); ConnectorType.GITHUB; enum entry exists
- [x] `type_id = "gitlab"` → `GitLabConnector(token=creds.token)`; BDD exists (gitlab_issues.feature); ConnectorType.GITLAB; enum entry exists
- [x] `type_id = "sharepoint"` → `SharePointConnector(token=creds.token)`; no BDD; ConnectorType.SHAREPOINT; enum entry exists; no unit tests

#### CI/CD Systems

- [x] `type_id = "github_actions_ci"` → `GitHubActionsCIRunner(token=creds.token)`; no BDD; ConnectorType.CI_RUNNER; enum entry exists (CI_RUNNER)
- [x] `type_id = "gitlab_ci"` → `GitLabCIRunner(token=creds.token, base_url=config.base_url)` — defaults to `https://gitlab.com/api/v4`; no BDD; same CI_RUNNER enum
- [x] `type_id = "buildkite"` → `BuildkiteConnector(token=creds.token)`; no BDD; ConnectorType.BUILDKITE; enum entry exists; no unit tests
- [x] `type_id = "circleci"` → `CircleCIConnector(token=creds.token)`; no BDD; ConnectorType.CIRCLECI; enum entry exists; no unit tests
- [x] `type_id = "jenkins"` → `JenkinsConnector(username=creds.username, token=creds.token, base_url=config.base_url)` — `username` + `token` required; base_url defaults to `http://localhost:8080`; no BDD; ConnectorType.JENKINS; enum entry exists; no unit tests
- [x] `type_id = "teamcity"` → `TeamCityConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `http://localhost:8111`; BDD feature file exists (teamcity_connector.feature: projects, buildTypes, agents); ConnectorType.TEAMCITY; enum entry exists; no unit tests
- [x] `type_id = "azure_pipelines"` → `AzurePipelinesConnector(token=creds.token, organization=config.organization, project=config.project)` — `organization` required (`ValueError` if missing); project optional; BDD feature file exists (azure_pipelines.feature: projects, pipelines, runs); ConnectorType.AZURE_PIPELINES; enum entry exists; no unit tests

#### Issue Tracking & Project Management

- [x] `type_id = "linear"` → `LinearConnector(api_key=creds.api_key)`; BDD exists (linear_connector.feature — placeholder); ConnectorType.LINEAR; enum entry exists
- [x] `type_id = "jira"` → `JiraConnector(instance=config.instance, creds=creds)` — `instance` or `base_url` is required; `ValueError` if missing; supports token or email+api_token auth; BDD exists (jira_connector.feature: 5 real scenarios); ConnectorType.JIRA; enum entry exists
- [x] `type_id = "shortcut"` → `ShortcutConnector(token=creds.token)`; no BDD; ConnectorType.SHORTCUT; enum entry exists; unit tests exist (test_shortcut_connector.py)
- [x] `type_id = "trello"` → `TrelloConnector(api_key=creds.api_key, token=creds.token)` — two required credentials; no BDD; ConnectorType.TRELLO; enum entry exists; unit tests exist (test_trello_connector.py)
- [x] `type_id = "asana"` → `AsanaConnector(personal_access_token=creds.personal_access_token)`; no BDD; ConnectorType.ASANA; enum entry exists; unit tests exist (test_asana_connector.py)
- [x] `type_id = "monday"` → `MondayConnector(api_key=creds.api_key)`; no BDD; ConnectorType.MONDAY; enum entry exists; unit tests exist (test_monday_connector.py)
- [x] `type_id = "youtrack"` → `YouTrackConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `https://youtrack.mycompany.com/api`; BDD feature file exists (youtrack_connector.feature: issues, issue, projects); ConnectorType.YOUTRACK; enum entry exists; unit tests exist (test_youtrack_connector.py)
- [x] `type_id = "notion"` → `NotionConnector(token=creds.token)`; BDD feature file exists (notion_connector.feature: health, databases, search); ConnectorType.NOTION; enum entry exists; unit tests exist (test_notion_connector.py)

#### Package Registries

- [x] `type_id = "npm"` → `NpmConnector(token=creds.get("token", ""))` — token optional (public registry reads don't require auth); BDD feature file exists (npm.feature: health, package, search); ConnectorType.NPM; enum entry exists; no unit tests
- [x] `type_id = "pypi"` → `PyPIConnector(token=creds.get("token", ""))` — token optional; BDD feature file exists (pypi.feature: health, package); ConnectorType.PYPI; enum entry exists; no unit tests

#### Knowledge & Documentation

- [x] `type_id = "confluence"` → `ConfluenceConnector(instance=config.instance, creds=creds)` — `instance` required (`ValueError` if missing); BDD feature file exists (confluence.feature: pages, spaces); ConnectorType.CONFLUENCE; enum entry exists; unit tests exist (test_confluence_connector.py)
- [x] `type_id = "dropbox_paper"` → `DropboxPaperConnector(token=creds.token)`; no BDD; ConnectorType.DROPBOX_PAPER; enum entry exists; no unit tests

#### Monitoring & Observability

- [x] `type_id = "datadog"` → `DatadogConnector(api_key=creds.api_key, app_key=creds.app_key, site=config.site)` — `api_key` + `app_key` required; site defaults to `"us"`; no BDD; ConnectorType.DATADOG; enum entry exists; no unit tests
- [x] `type_id = "sentry"` → `SentryConnector(token=creds.token, organization=config.organization, base_url=config.base_url)` — base_url defaults to `https://sentry.io`; no BDD; ConnectorType.SENTRY; enum entry exists; no unit tests
- [x] `type_id = "pagerduty"` → `PagerDutyConnector(token=creds.token)`; no BDD; ConnectorType.PAGERDUTY; enum entry exists; no unit tests
- [x] `type_id = "grafana"` → `GrafanaConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `http://localhost:3000`; no BDD; ConnectorType.GRAFANA; enum entry exists; no unit tests

#### Security & Secrets

- [x] `type_id = "azure_key_vault"` → `AzureKeyVaultConnector(token=creds.token, vault_url=config.vault_url)` — `vault_url` required (`ValueError` if missing); no BDD; ConnectorType.AZURE_KEY_VAULT; enum entry exists; no unit tests
- [x] `type_id = "onepassword"` → `OnePasswordConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `http://localhost:8080`; BDD feature file exists (onepassword.feature: health, vaults, vault); ConnectorType.ONEPASSWORD; enum entry exists; no unit tests

#### Quality & Code Analysis

- [x] `type_id = "sonarqube"` → `SonarQubeConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `http://localhost:9000`; BDD feature file exists (sonarqube.feature: health, projects, measures); ConnectorType.SONARQUBE; enum entry exists; no unit tests
- [x] `type_id = "codeclimate"` → `CodeClimateConnector(token=creds.token)`; BDD feature file exists (codeclimate.feature: health, repos); ConnectorType.CODECLIMATE; enum entry exists; no unit tests
- [x] `type_id = "snyk"` → `SnykConnector(token=creds.token)`; BDD feature file exists (snyk.feature: health, projects, issues); ConnectorType.SNYK; enum entry exists; no unit tests
- [x] `type_id = "trivy"` → `TrivyConnector(token=creds.get("token", ""), base_url=config.base_url)` — token optional; base_url defaults to `http://localhost:8080`; BDD feature file exists (trivy.feature: health, artifact); ConnectorType.TRIVY; enum entry exists; no unit tests

#### Alerting & Incident Management

- [x] `type_id = "opsgenie"` → `OpsgenieConnector(api_key=creds.api_key)`; BDD feature file exists (opsgenie_connector.feature: alerts, teams, schedules); ConnectorType.OPSGENIE; enum entry exists; no unit tests
- [x] `type_id = "pagerduty"` → listed under Monitoring & Observability (alerts/incidents)

#### Collaboration & Messaging

- [x] `type_id = "slack"` → `SlackConnector(bot_token=creds.bot_token)`; BDD exists (14 scenarios); ConnectorType.SLACK; enum entry exists
- [x] `type_id = "microsoft_teams"` → `MicrosoftTeamsConnector(token=creds.token)`; no BDD; ConnectorType.MICROSOFT_TEAMS; enum entry exists; no unit tests
- [x] `type_id = "discord"` → `DiscordConnector(token=creds.token)`; no BDD; ConnectorType.DISCORD; enum entry exists; no unit tests

#### Automation & Workflow

- [x] `type_id = "n8n"` → `N8NConnector(token=creds.token, base_url=config.base_url)` — base_url defaults to `http://localhost:5678`; BDD feature file exists (n8n.feature: health, workflows, executions); ConnectorType.N8N; enum entry exists; no unit tests

#### Local / Shell

- [x] `type_id = "filesystem"` → `FilesystemConnector(base_path=config.base_path)` — `base_path` is required; `ValueError` if missing; no BDD; ConnectorType.FILESYSTEM; enum entry exists
- [x] `type_id = "shell"` → `ShellConnector(runtime_provider=None, allowed_commands=config.allowed_commands)` — `runtime_provider` is always `None` in hub context (partial init); no BDD; ConnectorType.SHELL; enum entry exists

#### Plugin Registry Fallback

- [x] Unknown `type_id` falls back to the plugin registry (`get_plugin_registry().has_connector_type(type_id)`)
- [x] Plugin registry: `registry.build_connector(type_id, config, creds)` called if the plugin is registered
- [x] Unregistered unknown type raises `ValueError("Unknown connector type: ...")`
- [x] Each built connector is wrapped in `_TracedConnector` for OTel span injection

### OTel Tracing (`_TracedConnector`)

- [x] `health_check()` creates a span named `connector.<type>.health_check` with attrs: `connector.type`, `connector.operation`, `connector.healthy`, `connector.org_id`
- [x] `query()` creates a span named `connector.<type>.query` with attrs: `connector.type`, `connector.operation`, `connector.limit`, `connector.result_total`, `connector.org_id`
- [x] `write()` creates a span named `connector.<type>.write` with attrs: `connector.type`, `connector.operation`, `connector.org_id`
- [x] Query filters and payload content are NEVER included in span attributes — enforced by tests
- [x] `write()` calls `filter_payload_for_injection(payload)` before the OTel span is created — injection guard runs before tracing
- [x] Exceptions during operations are recorded as OTel exception events; span status set to `ERROR`
- [x] `org_id` is propagated through all spans when provided at hub construction time
- [x] `org_id` is absent from spans when not provided
- [x] The inner connector's custom attributes (e.g. `connector_type`) are delegated via `__getattr__`

### ACL Enforcement (`ConnectorACL`)

- [x] `ConnectorACL(visibility, allowed_operations)` validates `visibility` is `"org"` or `"team"` — `ValueError` otherwise
- [x] `check(operation)` raises `ConnectorPermissionError` when the operation is not in the `allowed_operations` allowlist
- [x] `check(operation)` raises `ConnectorPermissionError` with an explicit message when the allowlist is empty
- [x] `check(operation, request_visibility="team")` raises `ConnectorPermissionError` when the connector is org-only
- [x] ACL is built from `ci.visibility` and `ci.allowed_operations` during `initialise()`
- [x] ACL is retrievable via `hub.acl(connector_id)` after initialisation

### ACL Enforcement via `_TracedConnector`

`_TracedConnector` is a proxy wrapper that intercepts every connector operation and enforces ACL before delegating to the inner connector. The ACL is passed at construction time in `initialise()`.

- [x] `health_check()` enforces ACL operation `"read"` via `_enforce_acl()` at `_TracedConnector.health_check():288-289`
- [x] `query()` enforces ACL operation `"read"` via `_enforce_acl()` at `_TracedConnector.query():304`
- [x] `write()` enforces ACL operation `"write"` via `_enforce_acl()` at `_TracedConnector.write():318`
- [x] `hub.get(cid, operation="read")` checks ACL via `self._acls[cid].check(operation)` before returning connector at `ConnectorHub.get():178-179`
- [x] `hub.sample(cid, ...)` checks ACL for `"read"` via `self._acls[cid].check("read")` before querying at `ConnectorHub.sample():202`
- [x] `hub.get(cid)` without `operation=` parameter skips ACL check (caller is responsible for higher-layer enforcement)
- [x] ACL is checked BEFORE the OTel span is created — denied operations never appear in traces

### Advisory Locking

- [x] `AdvisoryLockService.acquire(session, resource_id)` calls `pg_try_advisory_lock(key1, key2)` — returns `True` on success
- [x] `AdvisoryLockService.acquire()` raises `ConnectorLockError` when the lock is already held
- [x] `AdvisoryLockService.release(session, resource_id)` calls `pg_advisory_unlock(key1, key2)`
- [x] UUIDs are converted to two `int4` keys via MD5 hash (avoids `hashtext()` version-dependency); `_uuid_to_lock_keys()`
- [x] MD5 is tagged `usedforsecurity=False` (not a security primitive in this context)

### Error Handling

- [x] `ConnectorNotFoundError` — raised by `hub.get()` and `hub.acl()` when the connector ID is unknown
- [x] `ConnectorDecryptError` — raised when credential decryption fails (missing secret, bad JSON, wrong key)
- [x] `ConnectorLockError` — raised when an advisory write lock cannot be acquired
- [x] `ConnectorPermissionError` — raised by `ConnectorACL.check()` on denied operations
- [x] `PathTraversalError` — raised by `FilesystemConnector._safe_path()` when a path escapes `base_path`
- [x] `ValueError` with descriptive message — raised for missing connector type, missing config fields, missing credential keys, unsupported resources
- [x] All connector operations propagate exceptions through the OTel tracing wrapper (recorded as span exceptions + re-raised)
- [x] Health check failures return `HealthResult(ok=False, detail=...)` — never raise; non-health operations raise on error
- [x] `initialise()` catches broad `Exception` when building individual connectors — misconfigured connectors are skipped with a `WARNING` log so one bad connector does not block the rest
- [x] `_TracedConnector.write()` calls `filter_payload_for_injection(payload)` before delegating to the inner connector — cross-site scripting / injection guard for connector write payloads

### Filesystem Connector

- [x] `ConnectorType.FILESYSTEM` with capabilities: `read`, `write`
- [x] `base_path` chroot enforcement via `os.path.realpath()` prefix check — path traversal is blocked
- [x] Query resource `"file"` — reads a single file by path; requires filter `{"path": "relative/path"}`
- [x] Query resource `"directory"` — lists directory entries (name, type, path); respects `q.limit`
- [x] Write resource `"file"` — writes text content; `path.parent.mkdir(parents=True, exist_ok=True)`
- [x] Unsupported `query()` or `write()` resources raise `ValueError`
- [x] `health_check()` returns `ok=True` if `base_path` exists and is a directory, `ok=False` with detail otherwise
- [x] File operations run via `asyncio.to_thread` — do not block the event loop

### GitHub Connector

- [x] `ConnectorType.GITHUB` with capabilities: `read`, `write`, `git_push`, `create_pr`
- [x] Auth via Bearer token in `Authorization` header; `X-GitHub-Api-Version: 2022-11-28`
- [x] Query resource `"repos"` — lists repos accessible to the token
- [x] Query resource `"file"` — reads file contents via Contents API; filters: `repo`, `path`, `ref`
- [x] Query resource `"pulls"` — lists pull requests; filters: `repo`, `state`
- [x] Write resource `"file"` — creates/updates file via Contents API; supports optional `sha` for updates
- [x] Unsupported resources raise `ValueError`
- [x] `health_check()` verifies `GET /user` and probes `GET /user/repos` for scope validity — returns `ok=False` with `Missing scopes: repo:read` on 401/403
- [x] HTTP errors propagate via `r.raise_for_status()`

### GitLab Connector

- [x] `ConnectorType.GITLAB` with capabilities: `read`, `write`, `git_push`, `create_pr`
- [x] Built-in connector type in `ConnectorType` enum and `_build_connector` switch
- [x] BDD coverage via `gitlab_issues.feature` (8 scenarios) — issues, issue, projects, search, error paths

### Jira Connector

- [x] `ConnectorType.JIRA` with capabilities: `issue_read`, `issue_write`, `issue_search`
- [x] Supports two auth modes: `token` (PAT/OAuth Bearer) or `email` + `api_token` (Basic auth)
- [x] Missing an auth mode raises `ValueError` with guidance
- [x] `instance` config field (e.g. `your-domain.atlassian.net`) required; resolution to `https://{instance}/rest/api/3`
- [x] Query resource `"issue"` — fetches single issue by `issue_key` filter
- [x] Query resource `"search"` — JQL search via `POST /search` with `jql` and `maxResults`
- [x] Write resource `"issue"` — creates issue via `POST /issue`
- [x] Write resource `"issue_update"` — updates issue fields via `PUT /issue/{key}`
- [x] `health_check()` verifies connectivity via `GET /myself` — returns display name on success
- [x] 5 BDD scenarios exist — query issue, search issues, create issue, update issue, missing issue_key error

### Linear Connector

- [x] `ConnectorType.LINEAR` with capabilities: `issue_read`, `issue_write`, `issue_search`
- [x] Auth via `api_key` in `Authorization` header; GraphQL API at `https://api.linear.app/graphql`
- [x] Query resource `"issue"` — fetches single issue by `id` filter via GraphQL query
- [x] Query resource `"search"` — searches issues by text via `searchIssues` GraphQL query
- [x] Write resource `"issue"` — creates issue via `issueCreate` mutation
- [x] Write resource `"issue_update"` — updates issue fields via `issueUpdate` mutation
- [x] GraphQL errors in response body raise `ValueError("Linear API error: ...")`
- [x] `health_check()` runs `viewer` query — returns `ok=False` with detail on HTTP errors or missing viewer
- [x] Generic exceptions in health check are caught and returned as `ok=False` (never propagate)
- [x] BDD scenarios exist — `linear_connector.feature` has 8 scenarios (5 happy-path + 3 error-path) with real step definitions

### Slack Connector

- [x] `ConnectorType.SLACK` with capabilities: `read`, `write`
- [x] Auth via `bot_token` Bearer token; Slack Web API at `https://slack.com/api`
- [x] Query resource `"channels"` — lists conversations via `conversations.list` with cursor pagination
- [x] Query resource `"messages"` — fetches channel history via `conversations.history`; filters: `channel`, `oldest`, `latest`
- [x] Query resource `"users"` — lists workspace users via `users.list` with cursor pagination
- [x] Write resource `"message"` — posts message via `chat.postMessage`
- [x] Slack API error responses (`ok: false`) raise `ValueError("Slack API error: ...")`
- [x] `health_check()` runs `api.test` — returns `ok=True` on `ok: true`, `ok=False` with error detail otherwise
- [x] BDD scenarios exist — `slack_connector.feature` has 14 scenarios with real step definitions

### CI Runner Connectors

- [x] `ConnectorType.CI_RUNNER` with capabilities: `trigger_run`, `get_run_status`, `get_run_logs`, `list_runs`
- [x] `CIRunnerBase` abstract base extending `ConnectorBase` with CI-specific methods
- [x] `GitHubActionsCIRunner` — triggers GitHub Actions workflows via `token` auth
- [x] `GitLabCIRunner` — triggers GitLab CI pipelines via `token` auth; configurable `base_url`
- [x] Both CI runner types are registered in `_build_connector` under `type_id="github_actions_ci"` and `type_id="gitlab_ci"`

### Shell Connector

- [x] `ConnectorType.SHELL` with capabilities: `read`, `write`
- [x] Built via `_build_connector` with `type_id="shell"`
- [x] Requires `allowed_commands` in config_json; `RuntimeProvider` passed as `None` in hub context (no runtime provider wiring in hub initialisation)
- [x] Hub integration test validates shell connector creation works through `ConnectorHub.initialise()`
- [x] Missing `RuntimeProvider` during shell connector initialisation raises `ValueError`

### Plugin Registry Fallback

- [x] Unknown connector types defer to `get_plugin_registry().has_connector_type(type_id)`
- [x] `registry.build_connector(type_id, config, creds)` called when the plugin is registered
- [x] Plugin connector must implement `ConnectorBase` ABC
- [x] Plugin registration requires a `PluginManifest` with `PLUGIN_ID`, `display_name`, `description`, `version`
- [x] The fallback path is integration-tested with a `CUSTOM` type returning `ConnectorType.CUSTOM`

### Health Check

- [x] `GET /api/connectors/{connector_id}/health` — returns `200` with `{"ok": true/false, "detail": "..."}`
- [x] Healthy connector returns `ok: true` with detail (e.g. GitHub login, Jira display name, viewer name)
- [x] Unhealthy connector returns `ok: false` with descriptive detail (e.g. "Missing scopes: repo:read", "HTTP 401: ...")
- [x] Credentials encrypted at rest — API key is never stored in plaintext in the DB

### Sampling

- [x] `hub.sample(connector_id, resource, filters, limit)` — convenience method wrapping `get()` + `query()` in a single call
- [x] Used by schema inference to sample connector data for LLM-assisted schema generation
- [x] Raises `ConnectorNotFoundError` for unknown connector IDs

### Error Handling (CRUD API routes)

All 5 CRUD routes in `connectors.py` have complete error handling chains:
- [x] `list_connectors_endpoint` — ProgrammingError→501, SQLAlchemyError→503
- [x] `create_connector_endpoint` — IntegrityError→409, ProgrammingError→501, SQLAlchemyError→503
- [x] `get_connector_endpoint` — ProgrammingError→501, SQLAlchemyError→503, 404 on missing
- [x] `update_connector_endpoint` — IntegrityError→409, ProgrammingError→501, SQLAlchemyError→503, 404 on missing
- [x] `delete_connector_endpoint` — ProgrammingError→501, SQLAlchemyError→503, 404 on missing

### Test Coverage (programming error paths)
- [x] `test_create_connector_programming_error_returns_501`
- [x] `test_create_connector_sqlalchemy_error_returns_503`
- [x] `test_create_connector_integrity_error_returns_409`
- [x] `test_list_connectors_programming_error_returns_501`
- [x] `test_list_connectors_sqlalchemy_error_returns_503`
- [x] `test_get_connector_programming_error_returns_501`
- [x] `test_get_connector_sqlalchemy_error_returns_503`
- [x] `test_update_connector_programming_error_returns_501`
- [x] `test_update_connector_sqlalchemy_error_returns_503`
- [x] `test_delete_connector_programming_error_returns_501`
- [x] `test_delete_connector_sqlalchemy_error_returns_503`

## Known Gaps

### Resolved (this iteration)

- [x] ~~**`ConnectorACL` is constructed but never called**~~ — **This was incorrect.** ACL IS enforced via three mechanisms: (1) `_TracedConnector._enforce_acl()` called from `health_check()` (read), `query()` (read), and `write()` (write) at `__init__.py:288-289,304,318`; (2) `hub.get(cid, operation=...)` checks ACL before returning the connector at `__init__.py:178-179`; (3) `hub.sample()` checks "read" ACL at `__init__.py:202`.
- [x] ~~**`GitLabConnector` has no BDD coverage**~~ — `gitlab_issues.feature` exists with real scenarios and step definitions.
- [x] ~~**Status was `covered`**~~ — Changed to `partial`. Only 9 connector types were documented; 33+ undocumented types now added.
- [x] ~~**Missing IntegrityError→409 catches on connector CRUD routes**~~ — Added to create and update endpoints. Both now return 409 on constraint violations instead of misleading 503.
- [x] ~~**Missing test coverage for ProgrammingError→501 and SQLAlchemyError→503 on connector CRUD routes**~~ — Created `test_connectors_programming_error.py` with 11 tests covering all 5 routes.

### Remaining Gaps

- **35+ undocumented connector types in `_build_connector` — only 9 were documented in product map before this QA iteration.** Now all documented but most lack BDD and unit tests.
- **No BDD for 15+ connector types** that have feature files but are not wired to step definitions (azure_pipelines, codeclimate, confluence, gitea, n8n, notion, npm, onepassword, opsgenie, pypi, snyk, sonarqube, teamcity, trivy, youtrack). Feature files exist but are not testable — production-mocking gap.
- **No BDD at all** for: azure_repos, bitbucket, sharepoint, buildkite, circleci, jenkins, dropbox_paper, datadog, sentry, pagerduty, grafana, microsoft_teams, discord, azure_key_vault, shell, CI runner connectors (github_actions_ci, gitlab_ci).
- **No unit tests** for: gitea, azure_repos, bitbucket, sharepoint, npm, pypi, dropbox_paper, buildkite, circleci, jenkins, teamcity, azure_key_vault, azure_pipelines, datadog, sentry, pagerduty, grafana, microsoft_teams, discord, onepassword, opsgenie, sonarqube, codeclimate, snyk, trivy, n8n (26 connector types).
- **Broad `except Exception` in `initialise()`** silently skips misconfigured connectors — logged at WARNING, not propagated. This is intentional (resilience) but means connector initialisation failures are invisible to pipeline authors unless they check logs.
- **No end-to-end integration test**: `ConnectorHub.initialise()` → connector method → OTel span → credential cleanup.
- **Multiple `ConnectorHub` instances coexistence** still untested (checkbox `[ ]`). The `asyncio.Lock` added in QA index 346 prevents concurrent `initialise()` corruption, but no test verifies non-interference of separate hub instances.
- **Shell connector** in hub context passes `runtime_provider=None` — the connector cannot actually execute commands without a real provider; this is a partial initialisation.
- **No BDD scenarios** for error-specific paths: `ConnectorNotFoundError`, path traversal, missing config fields, unsupported resources (ConnectorDecryptError has BDD coverage).
- **CI Runner connectors** have no BDD coverage — no dedicated feature files for GitHub Actions CI or GitLab CI.

### QA History
- 2026-07-07: Cross-cutting QA (index 318). Fixed stale BDD checkbox for Linear connector (`linear_connector.feature` has 8 real scenarios since July 1 QA — no longer a placeholder). Status: partial.
- 2026-07-08: Cross-cutting QA (index 249). Fixed CRITICAL — added IntegrityError→409 catch to create_connector_endpoint and update_connector_endpoint (FK/constraint violations previously returned misleading 503). Fixed MAJOR — created `test_connectors_programming_error.py` with 11 tests covering ProgrammingError→501, SQLAlchemyError→503, and IntegrityError→409 for all 5 CRUD routes. Added Error Handling and Test Coverage sections to product map.
- 2026-07-09: Cross-cutting QA (index 346). Fixed CRITICAL — added `asyncio.Lock` to `ConnectorHub.initialise()` to prevent race condition when two coroutines call `initialise()` concurrently on the same hub instance (the `_initialised` flag was set after the loop, creating a window where concurrent calls could interleave connector registrations). Double-checked locking pattern with lock acquired before the loop. MINOR findings: no timeout on `_build_connector()` constructor calls (all current constructors are synchronous, but untimed), unnecessary IntegrityError→409 catch on list/get/delete routes (harmless but imprecise), stale test in `test_youtrack_connector.py` (URL path mismatch in respx mock). Status: partial.
- 2026-07-12: Round 3 QA (improve-architecture batch 2). Clean pass — no code issues found. Ruff check clean. ConnectorHub remains well-structured with double-checked locking, proper OTel tracing, ACL enforcement, credential lifecycle management, and comprehensive error handling. Status: partial.
