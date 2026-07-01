---
id: feat-connectors-hub
prd: §8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/github_connector.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
  - backend/tests/bdd/features/connectors/linear_connector.feature
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/connectors/connector_decrypt_error.feature
unit-tests:
  - backend/tests/unit/connector_hub/test_connector_hub.py
  - backend/tests/unit/connector_hub/test_traced_connector.py
  - backend/tests/unit/api/test_connectors_endpoint.py
code:
  - backend/src/modulo/core/connector_hub/
  - backend/src/modulo/connectors/
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-core-secrets-backend
status: covered
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

### Connector Resolution (`_build_connector`)

- [x] `type_id = "filesystem"` → `FilesystemConnector(base_path=config.base_path)` — `base_path` is required; `ValueError` if missing
- [x] `type_id = "github"` → `GitHubConnector(token=creds.token)`
- [x] `type_id = "github_actions_ci"` → `GitHubActionsCIRunner(token=creds.token)`
- [x] `type_id = "gitlab_ci"` → `GitLabCIRunner(token=creds.token, base_url=config.base_url)` — defaults to `https://gitlab.com/api/v4`
- [x] `type_id = "gitlab"` → `GitLabConnector(token=creds.token)`
- [x] `type_id = "shell"` → `ShellConnector(runtime_provider=None, allowed_commands=config.allowed_commands)`
- [x] `type_id = "linear"` → `LinearConnector(api_key=creds.api_key)`
- [x] `type_id = "jira"` → `JiraConnector(instance=config.instance, creds=creds)` — `instance` or `base_url` is required; `ValueError` if missing
- [x] `type_id = "slack"` → `SlackConnector(bot_token=creds.bot_token)`
- [x] Unknown `type_id` falls back to the plugin registry (`get_plugin_registry().has_connector_type(type_id)`)
- [x] Plugin registry fallback: `registry.build_connector(type_id, config, creds)` called if the plugin is registered
- [x] Unregistered unknown type raises `ValueError("Unknown connector type: ...")`
- [x] Each built connector is wrapped in `_TracedConnector` for OTel span injection

### OTel Tracing (`_TracedConnector`)

- [x] `health_check()` creates a span named `connector.<type>.health_check` with attrs: `connector.type`, `connector.operation`, `connector.healthy`, `connector.org_id`
- [x] `query()` creates a span named `connector.<type>.query` with attrs: `connector.type`, `connector.operation`, `connector.limit`, `connector.result_total`, `connector.org_id`
- [x] `write()` creates a span named `connector.<type>.write` with attrs: `connector.type`, `connector.operation`, `connector.org_id`
- [x] Query filters and payload content are NEVER included in span attributes — enforced by tests
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
- [x] Missing: comprehensive BDD scenarios — no GitLab-specific feature file exists

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
- [ ] BDD scenarios missing — `linear_connector.feature` is a placeholder

### Slack Connector

- [x] `ConnectorType.SLACK` with capabilities: `read`, `write`
- [x] Auth via `bot_token` Bearer token; Slack Web API at `https://slack.com/api`
- [x] Query resource `"channels"` — lists conversations via `conversations.list` with cursor pagination
- [x] Query resource `"messages"` — fetches channel history via `conversations.history`; filters: `channel`, `oldest`, `latest`
- [x] Query resource `"users"` — lists workspace users via `users.list` with cursor pagination
- [x] Write resource `"message"` — posts message via `chat.postMessage`
- [x] Slack API error responses (`ok: false`) raise `ValueError("Slack API error: ...")`
- [x] `health_check()` runs `api.test` — returns `ok=True` on `ok: true`, `ok=False` with error detail otherwise
- [ ] BDD scenarios missing — `slack_connector.feature` is a placeholder

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

## Known Gaps

- **No BDD scenarios** for error-specific paths: `ConnectorNotFoundError`, path traversal, missing config fields, unsupported resources (ConnectorDecryptError has BDD coverage)
- **`ConnectorACL` is constructed but never called** in the connector operation flow — the ACL `check()` method exists but is not invoked before `query()` or `write()` on the hub-returned connector
- **Shell connector** in hub context passes `runtime_provider=None` — the connector cannot actually execute commands without a real provider; this is a partial initialisation
- **No integration test** validates end-to-end: `ConnectorHub.initialise()` → connector method call → OTel span emission → credential cleanup
- **`GitLabConnector`** has no BDD coverage at all — no feature file exists
- **CI Runner connectors** have no BDD coverage — no feature files for GitHub Actions or GitLab CI
