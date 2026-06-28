---
id: feat-connectors-gitlab
prd: 8.6
delivery-tasks: []
  - backend/tests/bdd/features/connectors/connector_health.feature
unit-tests: []
code:
  - backend/src/modulo/connectors/gitlab/__init__.py
  - backend/src/modulo/connectors/base.py

status: partial
---

# GitLab Connector

Async GitLab REST API v4 connector implementing `ConnectorBase`. Provides read/write access to GitLab projects for agent pipelines. Authenticated via Personal Access Token. Part of the `git-host` connector type family alongside `FilesystemConnector` and `GitHubConnector`.

## Behaviours

### Authentication — PAT-based API access

- [x] Authenticate all requests via `Authorization: Bearer <token>` header
- [x] Set `Accept: application/json` header on all requests
- [x] Use `httpx.AsyncClient` with base URL defaulting to `https://gitlab.com/api/v4`
- [x] `health_check()` calls `GET /user` to validate token
- [x] Return `HealthResult(ok=False)` with HTTP status on non-200
- [x] Return `HealthResult(ok=True)` with authenticated user info on success
- [ ] Accept configurable base URL for self-hosted GitLab instances (hard-coded to `gitlab.com/api/v4`)
- [ ] Support token rotation via ConnectorHub without disrupting in-flight runs
- [ ] Rate-limit awareness — no 429 retry/backoff on `read()`/`write()` calls
- [ ] Report `X-Request-Id` on API errors for GitLab support debugging

### OAuth Scopes — capability verification

- [x] Declare required scopes: `read_api`, `write_repository`, `api` (code constant)
- [ ] Verify `read_api` scope by probing `GET /projects` during health check
- [ ] Verify `write_repository` scope during health check — not probed
- [ ] Verify `api` scope during health check — not probed
- [ ] Report missing scopes individually in health check detail
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)

### Project Operations — listing and discovery

- [x] List projects via `query("projects")` with optional search filter
- [x] Return project ID, name, and web URL in results
- [x] Raise `ValueError` for unsupported resources in `query()`
- [ ] Support pagination cursor via `Link` header or pagination query params
- [ ] Filter projects by membership, visibility, or ownership
- [ ] Support `limit` parameter in project queries

### File Operations — read and write via Repository Files API

- [x] Read file via `query("file")` with `project_id`, `path`, `ref` filters
- [x] Decode base64 content from GitLab API response
- [x] Write file via `write("file")` with `project_id`, `path`, `content`, `commit_message`, optional `branch`
- [x] Call `PUT /projects/{id}/repository/files/{path}` with JSON body
- [x] Default branch to `"main"` when not specified
- [x] Return created/updated file info from API
- [x] `raise_for_status()` on HTTP errors
- [ ] Recursive directory listing — not implemented
- [ ] Batch file operations — not implemented
- [ ] File deletion — not implemented
- [ ] Path traversal protection — relies on GitLab API server-side; no local validation

### Merge Request Operations — listing and creation

- [x] List merge requests via `query("mrs")` with `project_id` and optional `state` filter
- [x] Default MR state filter to `"opened"`
- [x] Create merge request via `write("mr")` with `project_id`, `title`, `description`, `source_branch`, `target_branch`
- [ ] Add merge request comment — not implemented
- [ ] Merge merge request — not implemented
- [ ] Accept merge request with squash — not implemented
- [ ] List MR diff/changed files — not implemented
- [ ] Set MR labels — not implemented
- [ ] Request MR approval — not implemented
- [ ] Approve MR via API — not implemented
- [ ] `query("mrs")` does not support pagination cursor
- [ ] Pagination for `query("mrs")` — `next_cursor` always `None`

### Capability Declaration

- [x] `ConnectorType.GITLAB` defined in `base.py` enum
- [x] `GitLabConnector.connector_type` returns `ConnectorType.GITLAB`
- [ ] `ConnectorType.GITLAB.capabilities` defaults to `frozenset()` — no capabilities assigned in `base.py`
- [ ] `CREATE_PR` / `GIT_PUSH` capabilities should be declared but are not
- [ ] Capability-based graph validation — agent requirements vs connector capabilities not yet wired in ConnectorHub

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe API access by calling `GET /projects` during health check
- [x] Return authenticated user info in `detail` on success
- [x] Skip per-project repository-level checks
- [ ] Detect expired tokens vs insufficient scopes vs network errors
- [ ] Per-operation scope verification — no granular check before `write()` calls
- [ ] Report self-hosted GitLab version for diagnostic purposes

### Credential Lifetime — ConnectorHub integration

- [ ] Credentials decrypted once at run-start by ConnectorHub — not yet wired
- [ ] Decrypted connector instance held in run-scoped context, never enters LangGraph state
- [ ] One Fernet decrypt call per connector per run — not per node invocation
- [ ] Discard decrypted connector at run end

## Known Gaps

- [ ] **No self-hosted GitLab support**: API base URL is hard-coded to `https://gitlab.com/api/v4`
- [ ] **Capabilities not declared**: `ConnectorType.GITLAB.capabilities` returns an empty frozenset — no optional capabilities (GIT_PUSH, CREATE_PR) are declared, so graph validation cannot verify agent requirements
- [ ] **File deletion unimplemented**: no `write("file_delete")` or equivalent
- [ ] **MR operations limited**: only listing and creation work — no comments, merges, approvals, or labels
- [ ] **Scope verification incomplete**: health check doesn't verify individual scopes
- [ ] **No pagination**: `query("projects")` and `query("mrs")` don't return `next_cursor`
- [ ] **BDD placeholder**: `backend/tests/bdd/features/connectors/github_connector.feature` only — no GitLab-specific feature file exists
- [ ] **No unit tests**: `unit-tests` field is empty
- [ ] **No rate-limit handling**: no 429 retry, no GitLab `RateLimit-*` header inspection
- [ ] **ConnectorHub pre-run health check not wired**: credentials are not yet decrypted and validated at run-start via ConnectorHub
