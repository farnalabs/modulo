---
id: feat-connectors-jira
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
unit-tests: [backend/tests/unit/connectors/test_jira.py]
code:
  - backend/src/modulo/connectors/jira/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# Jira Connector

Async Jira Cloud REST API v3 connector implementing `ConnectorBase`. Provides read/write access to Jira issues for agent pipelines. Supports both Personal Access Token and email+API token authentication. Belongs to the `issue-tracker` connector type.

## Behaviours

### Authentication — PAT and Basic Auth

- [x] Accept `instance` (domain) and credentials on construction
- [x] Support PAT authentication via `token` parameter
- [x] Support Basic Auth via `email` + `api_token` parameters
- [x] Set appropriate `Authorization` header based on auth method
- [x] Use `httpx.AsyncClient` with base URL `https://{instance}/rest/api/3`
- [x] `health_check()` calls `GET /myself` to validate credentials
- [x] Return authenticated user displayName on success
- [x] Return `HealthResult(ok=False)` with HTTP status on non-200
- [x] Return `HealthResult(ok=False)` with detail containing status code — Jira Cloud 401 vs 403 distinction is visible in the detail string
- [ ] Jira Data Center (self-hosted) API support — URL format differs

### Issue Operations — CRUD via Jira REST API

- [x] Get single issue via `query("issue")` with issue `key` filter
- [x] Return issue fields (summary, description, status, assignee, etc.)
- [x] Search issues via `query("search")` with JQL string and optional `maxResults`
- [x] Default `maxResults` to connector limit or Jira API default (50)
- [x] Create issue via `write("issue")` with `project`, `summary`, `issuetype`, optional `description`, `priority`, `assignee`
- [x] Update issue fields via `write("issue_update")` with `key` and fields to update
- [x] Raise `ValueError` for unsupported resources in `query()` and `write()`
- [x] Transition issue status via `write("transition")` with `transition_id`
- [x] List available transitions via `query("transitions")`
- [x] Add issue comment via `write("issue_comment")` with `body`
- [x] List issue comments via `query("issue_comments")` with pagination
- [ ] Assign/reassign issue via dedicated operation — not implemented
- [ ] Add issue attachment — not implemented
- [ ] List issue remote links — not implemented
- [ ] Set issue labels — not implemented
- [ ] Delete issue — not implemented
- [x] JQL search supports pagination cursor via `startAt` parsing
- [x] JQL search returns total count

### Project Operations — discovery and metadata

- [x] List accessible projects via `query("projects")` — returns key, name, lead, avatarUrls
- [ ] Get project metadata (issue types, statuses, fields) — not implemented
- [ ] Get project components and versions — not implemented

### Capability Declaration

- [x] `ConnectorType.JIRA` defined in `base.py` enum
- [x] `ConnectorType.JIRA.capabilities` returns `{ISSUE_READ, ISSUE_WRITE, ISSUE_SEARCH}` in `base.py`
- [x] `JiraConnector.connector_type` returns `ConnectorType.JIRA`

### Health Check — connectivity and credential validation

- [x] Validate credentials by calling `GET /myself` — fail if status != 200
- [x] Return authenticated user display name in `detail` on success
- [x] Return HTTP status and response body on failure
- [x] Catch `httpx.HTTPStatusError` — returns `HealthResult(ok=False)` with status code and response text
- [x] Catch `httpx.TimeoutException` and `httpx.ConnectError` — returns `HealthResult(ok=False)` with connection error detail
- [x] Catch any generic `Exception` — returns `HealthResult(ok=False)` with truncated message
- [ ] Detect expired tokens vs invalid instance URL vs network errors — partially covered: HTTP errors and network errors distinguished, but token expiry vs invalid URL both produce HTTP 401
- [ ] Per-operation permission check before write operations

### Prompt Portability — issue-tracker terminology

- [ ] Connector type abstraction handles API operations
- [ ] Agent prompt templates may use Jira-specific terminology ("issue", "story", "epic", "sprint")
- [ ] Prompt portability is user's responsibility — documented limitation

### Error Handling

- [x] `health_check` catches `ValueError` (from `_call_api`) — returns `HealthResult(ok=False)` with truncated message
- [x] `health_check` catches generic `Exception` — returns `HealthResult(ok=False)` with truncated message
- [x] Query/write methods catch `httpx.HTTPStatusError` — raises `ValueError` with status code and response text
- [x] Query/write methods catch `httpx.TimeoutException` — raises `ValueError` after retries exhausted
- [x] Query/write methods catch `httpx.ConnectError` — raises `ValueError` after retries exhausted
- [x] Query/write methods catch JSON decode errors — raises `ValueError` with response text snippet
- [x] `query("issue")` with missing `issue_key` — raises `ValueError` with descriptive message
- [x] `write("issue_update")` with missing `issue_key` — raises `ValueError` with descriptive message
- [x] `query()` with unsupported resource — raises `ValueError`
- [x] `write()` with unsupported resource — raises `ValueError`
- [x] `_call_api` retries 429/502/503/504 with exponential backoff + jitter (max 3 retries)
- [x] `_call_api` raises `ValueError` for 304 Not Modified — resource unchanged
- [x] `_call_api` respects `Retry-After` header from Jira API
- [x] `_parse_json` narrowed to `json.JSONDecodeError` only — prevents masking programming errors

## Known Gaps

- [ ] **Jira Data Center not supported**: URL format is `https://{instance}/rest/api/3` — Jira Server/Data Center uses a different path structure
- [ ] **No attachment support**: cannot upload or download issue attachments
- [ ] **No field metadata**: agents cannot discover custom fields, available issue types, or statuses for a given project
- [ ] **No assign/reassign via dedicated operation**: issue assignment only possible through `issue_update` with `fields.assignee`
- [ ] **No issue labels management**: cannot add/remove labels via dedicated write resource
- [ ] **No issue delete**: deletion not exposed through connector interface
- [ ] **No X-RateLimit-* header inspection**: rate-limit retry is blind (no remaining/quota tracking)

