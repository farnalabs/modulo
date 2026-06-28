---
id: feat-connectors-jira
prd: 8.6
delivery-tasks: []
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
unit-tests: []
code:
  - backend/src/modulo/connectors/jira/__init__.py
  - backend/src/modulo/connectors/base.py

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
- [ ] Support API token rotation via ConnectorHub without disrupting in-flight runs
- [ ] Jira Data Center (self-hosted) API support — URL format differs
- [ ] Rate-limit awareness — no 429 retry/backoff
- [ ] Handle Jira Cloud 401 vs 403 distinction in health check detail

### Issue Operations — CRUD via Jira REST API

- [x] Get single issue via `query("issue")` with issue `key` filter
- [x] Return issue fields (summary, description, status, assignee, etc.)
- [x] Search issues via `query("search")` with JQL string and optional `maxResults`
- [x] Default `maxResults` to connector limit or Jira API default (50)
- [x] Create issue via `write("issue")` with `project`, `summary`, `issuetype`, optional `description`, `priority`, `assignee`
- [x] Update issue fields via `write("issue_update")` with `key` and fields to update
- [x] Raise `ValueError` for unsupported resources in `query()` and `write()`
- [ ] Transition issue status (e.g. In Progress → Done) — not implemented
- [ ] Add issue comment — not implemented
- [ ] Assign/reassign issue via dedicated operation
- [ ] Add issue attachment — not implemented
- [ ] List issue remote links — not implemented
- [ ] Set issue labels — not implemented
- [ ] List issue comments — not implemented
- [ ] Delete issue — not implemented
- [ ] JQL search does not support pagination cursor — `next_cursor` always `None`
- [ ] JQL search does not return total count

### Project Operations — discovery and metadata

- [ ] List accessible projects — not implemented
- [ ] Get project metadata (issue types, statuses, fields) — not implemented
- [ ] Get project components and versions — not implemented

### Capability Declaration

- [x] `ConnectorType.JIRA` defined in `base.py` enum
- [x] `ConnectorType.JIRA.capabilities` returns `{ISSUE_READ, ISSUE_WRITE, ISSUE_SEARCH}` in `base.py`
- [x] `JiraConnector.connector_type` returns `ConnectorType.JIRA`
- [ ] Capability-based graph validation — agent requirements vs connector capabilities not yet wired in ConnectorHub

### Health Check — connectivity and credential validation

- [x] Validate credentials by calling `GET /myself` — fail if status != 200
- [x] Return authenticated user display name in `detail` on success
- [x] Return HTTP status and response body on failure
- [ ] Detect expired tokens vs invalid instance URL vs network errors
- [ ] Per-operation permission check before write operations

### Credential Lifetime — ConnectorHub integration

- [ ] Credentials decrypted once at run-start by ConnectorHub — not yet wired
- [ ] Decrypted connector instance held in run-scoped context, never enters LangGraph state
- [ ] One Fernet decrypt call per connector per run — not per node invocation
- [ ] Discard decrypted connector at run end

### Prompt Portability — issue-tracker terminology

- [ ] Connector type abstraction handles API operations
- [ ] Agent prompt templates may use Jira-specific terminology ("issue", "story", "epic", "sprint")
- [ ] Prompt portability is user's responsibility — documented limitation

## Known Gaps

- [ ] **Jira Data Center not supported**: URL format is `https://{instance}/rest/api/3` — Jira Server/Data Center uses a different path structure
- [ ] **Issue transitions unimplemented**: cannot move issues through workflow states (To Do → In Progress → Done)
- [ ] **No comment operations**: cannot read or write issue comments
- [ ] **No attachment support**: cannot upload or download issue attachments
- [ ] **No project discovery**: `query("projects")` not implemented, agents cannot enumerate accessible projects at runtime
- [ ] **No field metadata**: agents cannot discover custom fields, available issue types, or statuses for a given project
- [ ] **No pagination**: JQL search results are limited to `maxResults` with no cursor-based continuation
- [ ] **BDD placeholder**: `backend/tests/bdd/features/connectors/jira_connector.feature` is a 3-line placeholder with no real scenarios
- [ ] **No unit tests**: `unit-tests` field is empty
- [ ] **No rate-limit handling**: no 429 retry, no `X-RateLimit-*` header inspection
- [ ] **ConnectorHub pre-run health check not wired**: credentials are not yet decrypted and validated at run-start via ConnectorHub
